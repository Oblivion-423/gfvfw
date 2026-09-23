"""BMS Logbook（``.lbk``）上传与名册同步路由。

页面形态
--------
* ``/account/logbook`` —— **成员自助**：上传自己的 ``.lbk``，查看解析结果与归档。
* ``/members/{id}/logbook`` —— **代他人**（教官/指挥）：同一套操作，针对指定成员。

**上传即自动解析，无需手动输入。** ``.lbk`` 格式已经解出（见
:mod:`gfvfw.lbk_parser`），所以上传后系统直接把文件里的
军衔 / 累计飞行时长 / 累计架次 / 勋章写入名册；页面上没有任何手填表单。

⚠️ **也没有"审核/确认"环节** —— 联队要求 Logbook 数据直接归档。
``POST /logbook/{id}/reparse`` 用**已归档的原件**重跑解析并同步名册
（解析器改进后回填历史存档，不必让成员重传）。

为什么没有独立的一级菜单
------------------------
Logbook 是"某个成员的资料"，跟着**成员**走最自然：入口放在账号页与成员详情页，
避免再开一个需要先选人的空页面（与 ACMI 工作台嵌进宿主页是同一个思路）。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    Member, MemberQualification, Qualification, Rank, USER_STATUS_LABELS,
)
from ...permissions import LOGBOOK_UPLOAD, LOGBOOK_UPLOAD_ANY
from ...security import verify_csrf
from ...services import logbook as LB
from ...services.audit import record_audit
from ..deps import Principal, get_db, require, require_login
from ..templating import render

log = logging.getLogger("gfvfw.web.logbook")

router = APIRouter()


# --------------------------------------------------------------------------
# 上下文
# --------------------------------------------------------------------------

def _options(db: Session) -> dict:
    return {
        "ranks": list(db.scalars(
            select(Rank).where(Rank.is_active.is_(True)).order_by(Rank.level)).all()),
        "qualifications": list(db.scalars(
            select(Qualification).where(Qualification.is_active.is_(True))
            .order_by(Qualification.category, Qualification.level)).all()),
    }


def _held_qualification_ids(db: Session, member_id: str) -> set[str]:
    return set(db.scalars(
        select(MemberQualification.qualification_id)
        .where(MemberQualification.member_id == member_id,
               MemberQualification.revoked_at.is_(None))).all())


def _page_context(db: Session, principal: Principal, member: Member,
                  *, error: str = "", warning: str = "",
                  message: str = "", is_self: bool) -> dict:
    files = LB.list_for_member(db, member.id)
    held = _held_qualification_ids(db, member.id)
    # ⚠️ 解析结果取自**最新一份解析成功的**归档，而不是"最新一份归档"。
    #    否则成员先传一份好文件、再误传一个坏文件时，页面会被失败信息占满，
    #    名册里明明有值却看不到来源 —— 而 current 的好坏另用失败面板提示。
    parsed_from = next((f for f in files if f.parsed_json), None)
    ctx = {
        "member": member,
        "is_self": is_self,
        "files": files,
        "current": files[0] if files else None,
        "applied": LB.applied_for_member(db, member.id),
        "held_qualification_ids": held,
        # 能否上传：自己的看 LOGBOOK_UPLOAD，他人的看 LOGBOOK_UPLOAD_ANY
        "can_upload": principal.can(LOGBOOK_UPLOAD_ANY) or (
            is_self and principal.can(LOGBOOK_UPLOAD)),
        "can_manage_any": principal.can(LOGBOOK_UPLOAD_ANY),
        "status_labels": USER_STATUS_LABELS,
        "max_kb": LB.MAX_LOGBOOK_BYTES // 1024,
        "error": error, "warning": warning, "message": message,
        "awards": LB.list_awards(db, member.id),
        # 解析结果的展示视图 + 它来自哪一份归档
        "parsed": _parsed_view(parsed_from),
        "parsed_source": parsed_from,
    }
    return ctx


def _flash_from_query(request: Request) -> dict:
    """把重定向带回来的 ``did`` / ``error`` / ``warning`` 变成页面提示。

    ⚠️ 这三个参数**必须在 GET 里读出来**，否则上传的失败原因与
    "已自动填入名册：…" 的解析摘要在 303 之后就被丢掉了 ——
    用户只会看到一个没有反馈的页面。
    """
    q = request.query_params
    return {
        "did": q.get("did", ""),
        "error": q.get("error", ""),
        "warning": q.get("warning", ""),
        "message": q.get("message", ""),
    }


def _parsed_view(rec) -> dict | None:
    """把 ``parsed_json`` 整理成模板好用的结构。

    分三块呈现，**已确证与推断分开**，避免把猜出来的偏移当成事实展示：
    ``certain`` / ``medals`` / ``others``。
    """
    import json as _json

    from ... import lbk_parser as LBP

    if not rec or not rec.parsed_json:
        return None
    try:
        data = _json.loads(rec.parsed_json)
    except ValueError:
        return None
    fields = data.get("fields") or {}
    certain_names = set(data.get("certain") or [])

    certain = [{"name": s.name, "label": s.label or s.name, "offset": s.offset,
                "note": s.note, "value": fields.get(s.name)}
               for s in LBP.FIELDS if s.certain]
    medals = [{"offset": off, "code": code, "label": label,
               "value": fields.get("medal_%s" % code)}
              for off, code, label in LB.MEDAL_FIELDS]
    #: 未确证字段的显示名（有 label 用 label，没有就显示偏移名）。
    #: ⚠️ `raw_u32_*` 是解析器额外读出的只读 u32（官方工具不作输入框），
    #:    也一并展示 —— 折叠在 <details> 里，但**不隐瞒**。
    labels = {s.name: (s.label or s.name) for s in LBP.FIELDS}

    def _label(key: str) -> str:
        if key in labels:
            return labels[key]
        if key.startswith("raw_u32_"):
            return "只读 u32 @0x%s" % key[len("raw_u32_"):]
        return key

    others = [{"name": _label(k), "key": k, "value": v}
              for k, v in sorted(fields.items())
              if k not in certain_names and not k.startswith("medal_")]
    return {"certain": certain, "medals": medals, "others": others,
            "warnings": data.get("warnings") or [],
            "rank_code": fields.get("rank_index") is not None
            and LBP.RANKS[fields["rank_index"]]
            if isinstance(fields.get("rank_index"), int)
            and 0 <= fields["rank_index"] < len(LBP.RANKS) else None}


# --------------------------------------------------------------------------
# 自助页
# --------------------------------------------------------------------------

@router.get("/account/logbook")
def own_logbook(request: Request,
                principal: Principal = Depends(require_login),
                db: Session = Depends(get_db)):
    """成员自己的 Logbook 页。"""
    member = principal.member
    if member is None:
        # 账号没绑名册（例如纯管理员账号）—— 明确说明，而不是空白页
        return render(request, "logbook/no_member.html", {}, status_code=200)
    ctx = _page_context(db, principal, member, is_self=True)
    ctx.update(_flash_from_query(request))
    return render(request, "logbook/page.html", ctx)


@router.get("/members/{member_id}/logbook")
def member_logbook(member_id: str, request: Request,
                   principal: Principal = Depends(require(LOGBOOK_UPLOAD_ANY)),
                   db: Session = Depends(get_db)):
    """代他人管理 Logbook（教官/指挥）。"""
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")
    ctx = _page_context(db, principal, member, is_self=False)
    ctx.update(_flash_from_query(request))
    return render(request, "logbook/page.html", ctx)


# --------------------------------------------------------------------------
# 上传
# --------------------------------------------------------------------------

@router.post("/account/logbook/upload")
async def upload_own(request: Request,
                     file: UploadFile = File(...),
                     note: str = Form(""),
                     csrf_token: str = Form(""),
                     principal: Principal = Depends(require_login),
                     db: Session = Depends(get_db)):
    if principal.member is None:
        raise HTTPException(status_code=400, detail="当前账号未绑定名册成员")
    if not principal.can(LOGBOOK_UPLOAD):
        raise HTTPException(status_code=403, detail="没有上传 Logbook 的权限")
    return await _do_upload(request, principal, db, principal.member,
                            file, note, csrf_token, is_self=True)


@router.post("/members/{member_id}/logbook/upload")
async def upload_for_member(member_id: str, request: Request,
                            file: UploadFile = File(...),
                            note: str = Form(""),
                            csrf_token: str = Form(""),
                            principal: Principal = Depends(require(LOGBOOK_UPLOAD_ANY)),
                            db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return await _do_upload(request, principal, db, member,
                            file, note, csrf_token, is_self=False)


async def _do_upload(request: Request, principal: Principal, db: Session,
                     member: Member, file: UploadFile, note: str,
                     csrf_token: str, *, is_self: bool) -> RedirectResponse:
    verify_csrf(request, csrf_token)

    page = "/account/logbook" if is_self else "/members/%s/logbook" % member.id
    if not file.filename:
        return RedirectResponse(page + "?did=nofile", status_code=303)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(file.filename).suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(256 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)

        rec, warnings, status = LB.store_upload(
            db, member.id, file.filename, Path(tmp_path),
            uploaded_by=principal.user.id, note=note)
        record_audit(db, principal.user.id, "logbook.upload", "logbook_files",
                     rec.id,
                     after={"member_id": member.id,
                            "filename": rec.original_filename,
                            "sha256": rec.sha256, "size": rec.size_bytes,
                            "parse_status": status},
                     reason="上传 BMS Logbook 归档",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except LB.LogbookError as exc:
        db.rollback()
        return RedirectResponse("%s?error=%s" % (page, _q(str(exc))),
                               status_code=303)
    except Exception as exc:                                # noqa: BLE001
        db.rollback()
        log.exception("上传 logbook 失败 member=%s", member.id)
        return RedirectResponse("%s?error=%s" % (page, _q("上传失败：%s" % exc)),
                               status_code=303)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    # ⚠️ did 必须区分"解析成功"与"仅归档、解析失败" —— 否则页面会谎报
    #    「已自动解析写入名册」，而名册其实没动。
    did = {"stored": "uploaded",
           "stored_unparsed": "uploaded_unparsed",
           "duplicate": "duplicate"}.get(status, "uploaded")
    # ⚠️ 把所有提示都带上（可能同时有"呼号不符"和"已自动填入…"两条）——
    #    只带 warnings[0] 会把"到底填了什么"丢掉。
    if warnings:
        return RedirectResponse("%s?did=%s&warning=%s"
                               % (page, did, _q("；".join(warnings), 500)),
                               status_code=303)
    return RedirectResponse("%s?did=%s" % (page, did), status_code=303)


# --------------------------------------------------------------------------
# 重新解析
# --------------------------------------------------------------------------

@router.post("/logbook/{logbook_id}/reparse")
def reparse(logbook_id: str, request: Request,
            csrf_token: str = Form(""),
            principal: Principal = Depends(require_login),
            db: Session = Depends(get_db)):
    """用**已归档的原件**重新解析一次并同步名册。

    用途：解析器改进后回填历史存档 —— 不必让成员重新上传。
    """
    verify_csrf(request, csrf_token)
    rec = LB.get(db, logbook_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")

    is_self = principal.member is not None and rec.member_id == principal.member.id
    if not (is_self and principal.can(LOGBOOK_UPLOAD)) \
            and not principal.can(LOGBOOK_UPLOAD_ANY):
        raise HTTPException(status_code=403, detail="没有权限操作这份归档")

    page = "/account/logbook" if is_self else "/members/%s/logbook" % rec.member_id
    try:
        summary = LB.reparse(db, rec, actor_user_id=principal.user.id)
        record_audit(db, principal.user.id, "logbook.reparse", "members",
                     rec.member_id,
                     before=summary.get("before"), after=summary.get("after"),
                     reason="重新解析已归档的 Logbook：%s"
                            % ("、".join(summary.get("changed") or []) or "无变化"),
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except LB.LogbookError as exc:
        # 回滚掉半截解析（不能污染名册），但**失败这件事要留在归档上** ——
        # 否则列表会一直显示旧的「已写入名册」徽标，与事实不符。
        db.rollback()
        stale = LB.get(db, logbook_id)
        if stale is not None:
            LB.record_parse_failure(db, stale, str(exc))
            db.commit()
        return RedirectResponse(page + "?error=" + _q(str(exc)), status_code=303)

    if summary.get("no_change"):
        return RedirectResponse(page + "?did=nothing", status_code=303)
    return RedirectResponse(page + "?did=applied", status_code=303)


# --------------------------------------------------------------------------
# 下载 / 删除
# --------------------------------------------------------------------------

@router.get("/logbook/{logbook_id}/download")
def download(logbook_id: str,
             principal: Principal = Depends(require_login),
             db: Session = Depends(get_db)):
    rec = LB.get(db, logbook_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")

    is_self = principal.member is not None and rec.member_id == principal.member.id
    if not is_self and not principal.can(LOGBOOK_UPLOAD_ANY):
        raise HTTPException(status_code=403, detail="只能下载自己的 Logbook")

    path = LB.absolute_path(rec)
    if not path.exists():
        raise HTTPException(status_code=410, detail="原件已不在服务器上，请联系管理员")
    return FileResponse(path, filename=rec.original_filename,
                        media_type="application/octet-stream")


@router.post("/logbook/{logbook_id}/delete")
def delete(logbook_id: str, request: Request,
           csrf_token: str = Form(""),
           principal: Principal = Depends(require_login),
           db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)
    rec = LB.get(db, logbook_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")

    is_self = principal.member is not None and rec.member_id == principal.member.id
    if not (is_self and principal.can(LOGBOOK_UPLOAD)) \
            and not principal.can(LOGBOOK_UPLOAD_ANY):
        raise HTTPException(status_code=403, detail="没有权限删除这份归档")

    page = "/account/logbook" if is_self else "/members/%s/logbook" % rec.member_id

    before = {"filename": rec.original_filename, "sha256": rec.sha256,
              "member_id": rec.member_id,
              "confirmed": rec.is_confirmed}
    LB.delete_upload(db, rec)
    record_audit(db, principal.user.id, "logbook.delete", "logbook_files",
                 logbook_id, before=before,
                 reason="删除 Logbook 归档（原件一并清理，可重新上传）",
                 actor_role=principal.primary_role, request=request)
    db.commit()
    return RedirectResponse(page + "?did=deleted", status_code=303)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _q(text: str, maxlen: int = 200) -> str:
    from urllib.parse import quote
    return quote(text[:maxlen], safe="")
