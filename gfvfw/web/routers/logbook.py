"""BMS Logbook（``.lbk``）上传与名册同步路由。

页面形态
--------
* ``/account/logbook`` —— **成员自助**：上传自己的 ``.lbk``、填写从 LogbookEditor
  读出的数值、查看已归档文件与名册里已登记的值。
* ``/members/{id}/logbook`` —— **代他人**（教官/指挥）：同一套操作，针对指定成员。

⚠️ **没有"审核/确认"环节** —— 联队要求 Logbook 数据直接归档，
所以 POST ``/logbook/{id}/declare`` 保存时**立即写入名册**
（``/logbook/{id}/reapply`` 只是把某份历史归档的值再应用一次）。

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
from ...permissions import LOGBOOK_UPLOAD, LOGBOOK_UPLOAD_ANY, MEMBER_EDIT_RANK
from ...security import verify_csrf
from ...services import logbook as LB
from ...services.audit import record_audit
from ..deps import Principal, get_db, get_principal, require, require_login
from ..forms import FieldError, parse_float, parse_int
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
        # 表单预填用**最新一份归档**的值
        "declared": files[0] if files else None,
    }
    ctx.update(_options(db))
    return ctx


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
    ctx["did"] = request.query_params.get("did", "")
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
    ctx["did"] = request.query_params.get("did", "")
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

        rec, warnings = LB.store_upload(
            db, member.id, file.filename, Path(tmp_path),
            uploaded_by=principal.user.id, note=note)
        record_audit(db, principal.user.id, "logbook.upload", "logbook_files",
                     rec.id,
                     after={"member_id": member.id,
                            "filename": rec.original_filename,
                            "sha256": rec.sha256, "size": rec.size_bytes},
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

    did = "uploaded"
    if warnings:
        return RedirectResponse("%s?did=%s&warning=%s" % (page, did, _q(warnings[0])),
                               status_code=303)
    return RedirectResponse("%s?did=%s" % (page, did), status_code=303)


# --------------------------------------------------------------------------
# 声明值
# --------------------------------------------------------------------------

@router.post("/logbook/{logbook_id}/declare")
def declare_values(logbook_id: str, request: Request,
                   rank_id: str = Form(""),
                   hours: str = Form(""),
                   sorties: str = Form(""),
                   qualification_ids: list[str] = Form(default=[]),
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
        raise HTTPException(status_code=403, detail="没有权限修改这份归档")

    page = ("/account/logbook" if is_self
            else "/members/%s/logbook" % rec.member_id)

    # 小时是**展示单位**，入库转秒 —— 换算集中在 web/forms.py，不在路由里手写
    try:
        hours_value = parse_float(hours, "累计飞行时长（小时）", minimum=0.0)
        hours_seconds = (int(round(hours_value * 3600))
                         if hours_value is not None else None)
    except FieldError as exc:
        return RedirectResponse(page + "?error=" + _q(str(exc)), status_code=303)

    try:
        sortie_count = parse_int(sorties, "累计架次", minimum=0)
    except FieldError as exc:
        return RedirectResponse(page + "?error=" + _q(str(exc)), status_code=303)

    try:
        summary = LB.declare(db, rec, rank_id=rank_id.strip() or None,
                             hours_seconds=hours_seconds,
                             sortie_count=sortie_count,
                             qualification_ids=qualification_ids,
                             apply_now=True,
                             actor_user_id=principal.user.id,
                             actor_role=principal.primary_role)
        # 按联队要求：Logbook 数据**直接归档，不需要审核** ——
        # 所以这一步就已经写入名册，审计记的是"改前/改后"而不是"待确认"。
        record_audit(db, principal.user.id, "logbook.apply", "members",
                     rec.member_id,
                     before=summary.get("before"), after=summary.get("after"),
                     reason="按 Logbook 登记名册记录：%s"
                            % ("、".join(summary.get("changed") or []) or "无变化"),
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except LB.LogbookError as exc:
        db.rollback()
        return RedirectResponse(page + "?error=" + _q(str(exc)), status_code=303)

    if summary.get("no_change"):
        return RedirectResponse(page + "?did=nothing", status_code=303)
    return RedirectResponse(page + "?did=applied", status_code=303)


# --------------------------------------------------------------------------
# 重新写入名册（把历史归档的值再应用一次）
# --------------------------------------------------------------------------
#
# ⚠️ 这里**没有独立的"审核/确认"步骤** —— 联队要求 Logbook 数据直接归档。
#    保存在 :func:`declare_values` 里就已经写入名册了。
#    本路由只是"把某份历史归档的值重新应用一次"（例如名册被别处改过、
#    或换回了旧档），同样直接生效。

@router.post("/logbook/{logbook_id}/reapply")
def reapply(logbook_id: str, request: Request,
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
        raise HTTPException(status_code=403, detail="没有权限操作这份归档")

    page = "/account/logbook" if is_self else "/members/%s/logbook" % rec.member_id

    try:
        summary = LB.apply_to_roster(db, rec, actor_user_id=principal.user.id,
                                     actor_role=principal.primary_role)
        record_audit(db, principal.user.id, "logbook.apply", "members",
                     summary["member_id"],
                     before=summary["before"], after=summary["after"],
                     reason="重新应用 Logbook 归档的值：%s"
                            % ("、".join(summary["changed"]) or "无变化"),
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except LB.LogbookError as exc:
        db.rollback()
        return RedirectResponse(page + "?error=" + _q(str(exc)), status_code=303)

    if summary["no_change"]:
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

def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text[:200], safe="")
