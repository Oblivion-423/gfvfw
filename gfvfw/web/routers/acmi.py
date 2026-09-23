"""
ACMI 摄入：上传 → 解析 → 认领 → 归并确认。

⚠️ **本模块不提供任何独立页面。**
按联队要求，ACMI 操作以「ACMI 工作台」内嵌区块的形式出现在业务页面里：

    飞行记录 · 战役记录   ``/log/campaign``        战役下拉可选，归并即归入该战役
    飞行记录 · 训练记录   ``/log/training``        固定为日常训练（不归入战役）
    战役管理 · 战役详情   ``/theater/{id}``        战役固定为当前战役，归并即归入

本模块对外提供两样东西：

* :func:`wizard_context` —— 宿主页面渲染工作台区块所需的上下文；
* 一组 POST 动作（上传 / 认领 / 忽略 / 撤销 / 归并），完成后 302 回宿主页面。

旧路径 ``/acmi``、``/acmi/upload``、``/acmi/claim``、``/acmi/merge`` 一律 302 跳到
宿主页面，保证旧书签与外部链接仍然可用。

为什么不做页面而做内嵌区块
--------------------------
上传本身没有独立语义 —— 它总是"为了往某个列表里补数据"。
把它挂在列表页上，用户看到的上下文（这是哪场战役、这是训练还是战役）
与即将生成的数据天然一致，也就省掉了"上传完再去手动归入战役"这一步。
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import AcmiFile, Campaign, Member, PilotMapping, User
from ...permissions import (
    ACMI_CLAIM_PILOT, ACMI_CONFIRM, ACMI_UPLOAD, ACMI_UPLOAD_ANY,
)
from ...services.audit import record_audit
from ...services.ingest import AcmiIngestService
from ..deps import Principal, get_db, require

log = logging.getLogger(__name__)
router = APIRouter(prefix="/acmi")

#: 工作台三个阶段，顺序即流程顺序。
ACMI_STAGES = ("upload", "claim", "merge")

#: 允许工作台回跳的宿主页面 —— **白名单**，不是"以 / 开头就算本地"。
#: ⚠️ ``//evil.com`` 同样以 ``/`` 开头，但浏览器会当外部站点，
#:    拿它做重定向就是开放重定向漏洞。
_RETURN_PATHS = ("/log/campaign", "/log/training")
_RETURN_THEATER_RE = re.compile(r"^/theater/[A-Za-z0-9_-]{1,64}$")
DEFAULT_RETURN_TO = "/log/campaign"

#: 工作台自己的查询参数 —— 计算"宿主页面其余筛选条件"时要排掉。
_WIZARD_KEYS = ("acmi", "did", "n", "campaign_id")

#: 动作完成后在工作台顶部显示的一句话。**服务端写死**，
#: 不接受 URL 里的任意文本，避免把 query 参数当消息回显。
ACMI_DID_MESSAGES = {
    "uploaded": "ACMI 已上传并解析完成。",
    "duplicate": "这些文件此前已经上传过，未重复入库。",
    "partial": "部分文件已入库，其余此前已存在、未重复入库。",
    "failed": "有文件处理失败 —— 详情见下方「最近的归档文件」。",
    "none": "没有选择任何文件。",
    "claimed": "已认领飞行员名。",
    "revoked": "已撤销认领。",
    "ignored": "已忽略该名字（不再进入统计）。",
    "unignored": "已恢复为待认领。",
    "deleted": "已删除该 ACMI 文件（磁盘原件一并删除）。",
}

PARSE_LABELS = {
    "pending": "待解析",
    "parsing": "解析中",
    "parsed": "已解析",
    "failed": "解析失败",
}


# --------------------------------------------------------------------------
# 回跳地址与工作台上下文
# --------------------------------------------------------------------------

def safe_return_to(value: Optional[str]) -> str:
    """校验工作台的动作完成后该跳回哪个宿主页面。"""
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//"):
        return DEFAULT_RETURN_TO
    if "\r" in v or "\n" in v or "\\" in v:
        return DEFAULT_RETURN_TO
    if v in _RETURN_PATHS or _RETURN_THEATER_RE.match(v):
        return v
    return DEFAULT_RETURN_TO


def back_to(path: str, stage: str, campaign_id: str = "",
            did: str = "", count: int = 0) -> str:
    """构造回到宿主页面并停在指定阶段的 URL。"""
    url = "%s?acmi=%s" % (safe_return_to(path), stage)
    if campaign_id:
        url += "&campaign_id=%s" % campaign_id
    if did:
        url += "&did=%s" % did
    if count:
        url += "&n=%d" % count
    return url


def _pilots_of(rec: AcmiFile) -> list[str]:
    try:
        return json.loads(rec.pilot_names_json) if rec.pilot_names_json else []
    except (TypeError, ValueError):
        return []


def did_from(request: Request) -> tuple[str, int]:
    """从查询串里取动作回执（服务端白名单消息 + 计数）。

    ⚠️ 刻意**不**回显 query 里的任意文本。早期版本往 URL 里塞
    ``?message=已认领`` 这类自由文本，但 ``render()`` 从不回填它，
    于是提示根本不显示；若改成回填，就等于把任意 query 文本
    反射到页面上。这里只认白名单 key，文案由服务端写死。
    """
    did = request.query_params.get("did", "")
    try:
        n = int(request.query_params.get("n", "0") or "0")
    except (TypeError, ValueError):
        n = 0
    return did, max(0, n)


def stage_from(request: Request) -> str:
    """宿主页面从 ``?acmi=<stage>`` 取当前阶段。"""
    return request.query_params.get("acmi", "")


def wizard_context(db: Session, principal: Principal,
                   stage: str = "upload",
                   campaign_id: str = "",
                   return_to: str = DEFAULT_RETURN_TO,
                   did: str = "", count: int = 0,
                   lock_campaign: bool = False,
                   lock_mission_type: bool = False,
                   default_mission_type: str = "other",
                   opened: bool = False) -> dict:
    """构建「ACMI 工作台」内嵌区块的上下文。

    只填充**当前用户有权操作**的部分：没有 ACMI_UPLOAD 就不给上传表单，
    没有 ACMI_CLAIM_PILOT 就不列出待认领名单 —— 避免靠页面把数据读出来。

    ``opened`` 为真时区块默认展开（用户是带着 ``?acmi=`` 来的）；
    否则折叠成一行摘要，让宿主页面本身仍是主角。

    ``lock_campaign`` / ``lock_mission_type`` 用于**保证"上传的东西会出现在本页"**：
    训练记录页只列 ``mission_type == 'training'`` 的任务，若允许在那里选别的类型，
    归并出的任务就会当场从列表里消失；战役详情页同理，战役是确定的。
    """
    stage = stage if stage in ACMI_STAGES else "upload"
    can_upload = principal.can(ACMI_UPLOAD)
    can_claim = principal.can(ACMI_CLAIM_PILOT)
    can_merge = principal.can(ACMI_CONFIRM)

    cid = (campaign_id or "").strip()
    camp = db.get(Campaign, cid) if cid else None
    camp_label = camp.name if camp is not None and camp.deleted_at is None else ""

    ctx: dict = {
        "acmi_stage": stage,
        "acmi_return_to": safe_return_to(return_to),
        "acmi_campaign_id": cid,
        "acmi_campaign_label": camp_label,
        "acmi_lock_campaign": bool(lock_campaign),
        "acmi_lock_mission_type": bool(lock_mission_type),
        "acmi_default_mission_type": default_mission_type,
        "acmi_can_upload": can_upload,
        "acmi_can_claim": can_claim,
        "acmi_can_merge": can_merge,
        #: 删除上传的 ACMI —— 与上传同权限；他人的文件另需 acmi.upload.any，
        #: 该判定在删除路由里逐条做（这里只决定"要不要显示删除按钮"）
        "acmi_can_delete": can_upload,
        #: 三种权限一个都没有就不必渲染工作台（纯查看者看到空壳只是噪声）
        "acmi_any": can_upload or can_claim or can_merge,
        "acmi_max_mb": settings.max_acmi_bytes // (1024 * 1024),
        "acmi_parse_labels": PARSE_LABELS,
        "acmi_did_message": ACMI_DID_MESSAGES.get(did, ""),
        "acmi_count": count,
        "acmi_open": bool(opened) or bool(ACMI_DID_MESSAGES.get(did)),
        # 折叠标题上的"待办"计数
        "acmi_pending_files": int(db.scalar(
            select(func.count(AcmiFile.id))
            .where(AcmiFile.parse_status == "parsed",
                   AcmiFile.mission_id.is_(None))) or 0),
        "acmi_unclaimed_total": (
            len(AcmiIngestService.unclaimed_pilots(db)) if can_claim else 0),
        # 上传阶段
        "acmi_recent": None,
        # 认领阶段
        "acmi_unclaimed": [], "acmi_claimed": [], "acmi_ignored": [],
        "acmi_members": [],
        # 归并阶段
        "acmi_files": [],
        # 战役记录页的可选战役
        "acmi_campaigns": [],
        # 宿主页面自己的筛选条件，原样带在阶段跳转链接上（见 wizard_for_request）
        "acmi_extra_q": "",
    }

    if can_upload:
        rows = db.execute(
            select(AcmiFile, User.username)
            .join(User, User.id == AcmiFile.uploaded_by, isouter=True)
            .order_by(AcmiFile.created_at.desc())
            .limit(10)
        ).all()
        ctx["acmi_recent"] = [
            {"id": f.id, "original_filename": f.original_filename,
             "parse_status": f.parse_status, "mission_id": f.mission_id,
             "batch_id": f.batch_id,
             "parse_error": f.parse_error,
             "pilot_count": len(_pilots_of(f)),
             "uploader": uploader, "created_at": f.created_at}
            for f, uploader in rows
        ]
        ctx["acmi_campaigns"] = list(db.scalars(
            select(Campaign).where(Campaign.deleted_at.is_(None))
            .order_by(Campaign.sort_order, Campaign.started_at.desc())))

    if stage == "claim" and can_claim:
        ctx["acmi_unclaimed"] = AcmiIngestService.unclaimed_pilots(db)
        ctx["acmi_claimed"] = AcmiIngestService.claimed_pilots(db)
        ctx["acmi_ignored"] = AcmiIngestService.ignored_pilots(db)
        ctx["acmi_members"] = list(db.scalars(
            select(Member).where(Member.deleted_at.is_(None))
            .order_by(Member.callsign)))

    if stage == "merge" and can_merge:
        # 可选文件 = 已解析成功、尚未归属任务的
        candidates = list(db.scalars(
            select(AcmiFile)
            .where(AcmiFile.parse_status == "parsed", AcmiFile.mission_id.is_(None))
            .order_by(AcmiFile.recorded_start_at)))
        files = []
        for f in candidates:
            pilots = _pilots_of(f)
            mapped = set(db.scalars(
                select(PilotMapping.raw_name)
                .where(PilotMapping.raw_name.in_(pilots))).all()) if pilots else set()
            files.append({
                "id": f.id, "filename": f.original_filename,
                "start": f.recorded_start_at, "end": f.recorded_end_at,
                "duration": f.duration_seconds, "size": f.size_bytes,
                "pilots": pilots,
                "claimed": [p for p in pilots if p in mapped],
                "unclaimed": [p for p in pilots if p not in mapped],
            })
        ctx["acmi_files"] = files

    return ctx


def wizard_for_request(db: Session, principal: Principal, request: Request,
                       *, return_to: str,
                       campaign_id: str = "",
                       lock_campaign: bool = False,
                       lock_mission_type: bool = False,
                       default_mission_type: str = "other") -> dict:
    """宿主页面一行接入工作台：从 request 解出阶段与回执，再构建上下文。

    三个宿主页面都走这个函数，保证"停在哪个阶段、回执怎么说"完全一致。

    阶段跳转链接会**带上宿主页面自己的筛选条件**（如战役记录的日期范围），
    否则在工作台里点一下"归并确认"，页面的筛选就悄悄被重置了。
    """
    stage = stage_from(request)
    did, count = did_from(request)
    ctx = wizard_context(
        db, principal,
        stage=stage or "upload",
        campaign_id=campaign_id,
        return_to=return_to,
        did=did, count=count,
        opened=bool(stage),
        lock_campaign=lock_campaign,
        lock_mission_type=lock_mission_type,
        default_mission_type=default_mission_type,
    )
    ctx["acmi_extra_q"] = urlencode([
        (k, v) for k, v in request.query_params.multi_items()
        if k not in _WIZARD_KEYS
    ])
    return ctx


# --------------------------------------------------------------------------
# 旧路径：一律跳转到宿主页面（页面本身已不存在）
# --------------------------------------------------------------------------

def _legacy(stage: str):
    return RedirectResponse(back_to(DEFAULT_RETURN_TO, stage), status_code=302)


@router.get("")
def acmi_legacy_index():
    """旧的 ACMI 列表页 —— 已并入「飞行记录 · 战役记录」。"""
    return _legacy("upload")


@router.get("/upload")
def acmi_legacy_upload():
    return _legacy("upload")


@router.get("/claim")
def acmi_legacy_claim():
    return _legacy("claim")


@router.get("/merge")
def acmi_legacy_merge():
    return _legacy("merge")


# --------------------------------------------------------------------------
# 动作：删除一份**未归并**的 ACMI
# --------------------------------------------------------------------------

@router.post("/{file_id}/delete")
def acmi_delete(file_id: str, request: Request,
                return_to: str = Form(DEFAULT_RETURN_TO),
                campaign_id: str = Form(""),
                reason: str = Form(""),
                csrf_token: str = Form(""),
                principal: Principal = Depends(require(ACMI_UPLOAD)),
                db: Session = Depends(get_db)):
    """删掉传错的 ACMI（文件行 + 磁盘原件）。

    规则（有意保守）：
    * **已归并**的文件不允许在这里删 —— 那等于绕过软删除把飞行日志挖掉一块。
      要撤就先到任务详情页「删除任务」，它会把这些文件拆回待归并（= 撤销归并）。
    * 别人的上传需要 ``acmi.upload.any``（代他人上传）权限。
    * 硬删除而非软删：``acmi_files`` 没有 ``deleted_at``，且**必须硬删** ——
      否则同一份文件再次上传会被 sha256 判成重复而永远传不进来。
      谁删了什么由审计日志留痕。
    """
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    rec = db.get(AcmiFile, file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    if rec.mission_id:
        raise HTTPException(
            status_code=400,
            detail="该文件已归并到任务，不能直接删除。请先到任务详情页删除任务"
                   "（会把文件拆回待归并），再删除文件。")

    if (rec.uploaded_by and rec.uploaded_by != principal.user.id
            and not principal.can(ACMI_UPLOAD_ANY)):
        raise HTTPException(
            status_code=403,
            detail="只能删除自己上传的文件；删除他人上传需要 acmi.upload.any 权限")

    fname = rec.original_filename
    stored = rec.stored_path
    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="acmi_files", target_id=rec.id,
                 before={"original_filename": fname, "sha256": rec.sha256,
                         "size_bytes": rec.size_bytes,
                         "parse_status": rec.parse_status},
                 reason=reason.strip() or "删除上传的 ACMI", request=request)
    db.delete(rec)
    db.commit()

    # 磁盘原件一并删掉（解析结果已随行消失，留着只是垃圾）
    removed = False
    if stored:
        p = Path(stored)
        if not p.is_absolute():
            p = Path(settings.storage_dir) / p
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError as exc:      # 删不掉不影响数据一致性，只记日志
            log.warning("ACMI 原件删除失败 %s：%s", p, exc)

    log.info("ACMI %s（%s）已删除，磁盘原件%s", fname, rec.id[:8],
             "已删" if removed else "未找到")
    return RedirectResponse(
        back_to(return_to, "upload", campaign_id.strip(), did="deleted"),
        status_code=303)


# --------------------------------------------------------------------------
# 动作：上传
# --------------------------------------------------------------------------

@router.post("/upload")
async def acmi_upload(request: Request,
                      files: list[UploadFile] = File(default=[]),
                      return_to: str = Form(DEFAULT_RETURN_TO),
                      campaign_id: str = Form(""),
                      csrf_token: str = Form(""),
                      principal: Principal = Depends(require(ACMI_UPLOAD)),
                      db: Session = Depends(get_db)):

    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    back = safe_return_to(return_to)
    cid = campaign_id.strip()

    if not files:
        return RedirectResponse(back_to(back, "upload", cid, did="none"),
                                status_code=303)

    svc = AcmiIngestService()
    results, errors = [], []

    for up in files:
        if not up.filename:
            continue
        # ⚠️ 上传到临时文件后交给服务处理：
        #    大文件（实测最大 107.9 MB）不能整体读进内存。
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False,
                                             suffix=Path(up.filename).suffix) as tmp:
                tmp_path = tmp.name
                while True:
                    chunk = await up.read(1024 * 1024)
                    if not chunk:
                        break
                    tmp.write(chunk)
        except Exception as exc:                        # noqa: BLE001
            errors.append("%s：写入临时文件失败 %s" % (up.filename, exc))
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            continue

        try:
            res = svc.ingest_file(db, tmp_path,
                                  original_filename=up.filename,
                                  uploaded_by=principal.user.id)
            results.append(res)
            db.commit()
        except Exception as exc:                        # noqa: BLE001
            db.rollback()
            log.exception("上传处理失败：%s", up.filename)
            errors.append("%s：%s" % (up.filename, exc))
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    record_audit(db, actor_user_id=principal.user.id, action="import",
                 target_table="acmi_files", target_id=None,
                 after={"count": len(results),
                        "files": [r.acmi_file.original_filename for r in results]},
                 reason="上传 ACMI（工作台）", request=request)
    db.commit()

    # 上传完直接进入认领阶段 —— 未认领的名字不处理，架次就进不了统计。
    # 但若整批都失败，就**留在上传阶段**：此时把人推去认领只会更迷惑，
    # 真正的错误信息在上传页的「最近的归档文件」里。
    if errors:
        return RedirectResponse(
            back_to(back, "upload", cid, did="failed", count=len(errors)),
            status_code=303)

    added = sum(1 for r in results if not r.duplicate)
    dupes = len(results) - added
    if results and added == 0:
        return RedirectResponse(
            back_to(back, "upload", cid, did="duplicate", count=dupes),
            status_code=303)

    return RedirectResponse(
        back_to(back, "claim", cid,
                did="partial" if dupes else ("uploaded" if added else ""),
                count=added or dupes),
        status_code=303)


# --------------------------------------------------------------------------
# 动作：飞行员认领
# --------------------------------------------------------------------------

@router.post("/claim")
def claim_submit(request: Request,
                 raw_name: str = Form(...),
                 member_id: str = Form(...),
                 return_to: str = Form(DEFAULT_RETURN_TO),
                 campaign_id: str = Form(""),
                 csrf_token: str = Form(""),
                 principal: Principal = Depends(require(ACMI_CLAIM_PILOT)),
                 db: Session = Depends(get_db)):

    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    if db.get(Member, member_id) is None:
        raise HTTPException(status_code=400, detail="目标成员不存在")

    svc = AcmiIngestService()
    svc.claim_pilot(db, raw_name.strip(), member_id, created_by=principal.user.id)
    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="pilot_mappings", target_id=raw_name,
                 after={"raw_name": raw_name, "member_id": member_id},
                 reason="认领飞行员名", request=request)
    db.commit()
    return RedirectResponse(
        back_to(return_to, "claim", campaign_id.strip(), did="claimed"),
        status_code=303)


@router.post("/claim/{mapping_id}/revoke")
def claim_revoke(mapping_id: str, request: Request,
                 return_to: str = Form(DEFAULT_RETURN_TO),
                 campaign_id: str = Form(""),
                 csrf_token: str = Form(""),
                 principal: Principal = Depends(require(ACMI_CLAIM_PILOT)),
                 db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)
    svc = AcmiIngestService()
    svc.revoke_claim(db, mapping_id)
    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="pilot_mappings", target_id=mapping_id,
                 reason="撤销认领", request=request)
    db.commit()
    return RedirectResponse(
        back_to(return_to, "claim", campaign_id.strip(), did="revoked"),
        status_code=303)


@router.post("/ignore")
def ignore_pilot(request: Request,
                 raw_name: str = Form(...),
                 return_to: str = Form(DEFAULT_RETURN_TO),
                 campaign_id: str = Form(""),
                 csrf_token: str = Form(""),
                 principal: Principal = Depends(require(ACMI_CLAIM_PILOT)),
                 db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)
    svc = AcmiIngestService()
    svc.ignore_pilot(db, raw_name.strip(), created_by=principal.user.id,
                     reason="判定为非联队人员")
    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="ignored_pilots", target_id=raw_name,
                 reason="忽略飞行员名", request=request)
    db.commit()
    return RedirectResponse(
        back_to(return_to, "claim", campaign_id.strip(), did="ignored"),
        status_code=303)


@router.post("/claim/{ignored_id}/unignore")
def unignore_pilot(ignored_id: str, request: Request,
                   return_to: str = Form(DEFAULT_RETURN_TO),
                   campaign_id: str = Form(""),
                   csrf_token: str = Form(""),
                   principal: Principal = Depends(require(ACMI_CLAIM_PILOT)),
                   db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)
    svc = AcmiIngestService()
    svc.unignore_pilot(db, ignored_id)
    db.commit()
    return RedirectResponse(
        back_to(return_to, "claim", campaign_id.strip(), did="unignored"),
        status_code=303)


# --------------------------------------------------------------------------
# 动作：归并确认
# --------------------------------------------------------------------------

@router.post("/merge")
def merge_confirm(request: Request,
                  file_ids: list[str] = Form(default=[]),
                  mission_name: str = Form(""),
                  mission_type: str = Form("other"),
                  visibility: str = Form("members"),
                  campaign_id: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(ACMI_CONFIRM)),
                  db: Session = Depends(get_db)):

    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    if not file_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")

    # 归入战役是"上传即归入"的关键一步。这里显式校验，
    # 免得把不存在的 campaign_id 写进 Mission 之后才由外键报错。
    cid = campaign_id.strip() or None
    if cid is not None:
        camp = db.get(Campaign, cid)
        if camp is None or camp.deleted_at is not None:
            raise HTTPException(status_code=400, detail="战役不存在或已删除")

    svc = AcmiIngestService()
    try:
        plan = svc.suggest_merge(db, file_ids)
        mission = svc.confirm_merge(
            db, plan.batch.id,
            confirmed_by=principal.user.id,
            mission_name=mission_name.strip() or None,
            mission_type=mission_type,
            visibility=visibility,
            campaign_id=cid,
        )
        record_audit(db, actor_user_id=principal.user.id, action="import",
                     target_table="missions", target_id=mission.id,
                     after={"name": mission.name, "files": len(file_ids),
                            "campaign_id": cid},
                     reason="归并确认并入库（工作台）", request=request)
        db.commit()
    except Exception as exc:                            # noqa: BLE001
        db.rollback()
        log.exception("归并确认失败")
        raise HTTPException(status_code=400, detail="归并失败：%s" % exc)

    # 跳到任务详情：那里能同时看到生成的任务与它的所属战役，便于立刻核对。
    tail = "&message=任务已创建并归入战役" if cid else "&message=任务已创建"
    return RedirectResponse("/missions/%s?%s" % (mission.id, tail.lstrip("&")),
                            status_code=303)
