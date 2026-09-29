"""Tacview XML「Export Flight Log」上传与战斗分析路由。

页面形态
--------
* **任务详情页** ``/missions/{id}`` 的「Tacview 战斗分析」区块 —— 上传入口
  与已归档 XML 的列表（本模块的 ``panel_context`` 提供该区块的上下文）。
* ``/tacview/{id}`` —— 单份 XML 的**战斗分析页**：击杀链、武器效能、
  按机型分组的空战胜果、飞行结局。

什么是「Export Flight Log」
--------------------------
用户在 Tacview 里打开 ``.acmi`` 录像 → ``File → Export Flight Log`` 导出的
**XML 事件文件**。它带结构化事件（谁开火 / 命中了谁 / 摧毁了谁 / 起飞降落），
因此能做 ``.acmi`` 直接入库做不到的**击杀归属**与命中链分析
（分析器本体见 :mod:`gfvfw.tacview_analyzer`，内置自 TacviewLogAnalyzer，
MIT）。

设计取舍
--------
* **挂在任务上**：战斗分析脱离"这是哪次任务"就没有意义，入口跟着任务走
  （与 ACMI 工作台嵌进业务页面是同一思路），不设独立一级菜单。
* **分析结果存快照**（``vm_json``）：分析只取决于文件内容（sha256 去重），
  不随名册/认领变化 —— 解析一次，查看零成本，没有重算入口；
  分析器升级后可用「重新解析」回填历史（原件已归档）。
* 权限：上传用 ``acmi.upload``（同一批"交录像"的人）；查看分析页与
  任务详情同档（仅队员）—— 里面有逐飞行员的击杀链，口径与任务详情一致。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...models import Mission
from ...permissions import ACMI_UPLOAD, ACMI_UPLOAD_ANY
from ...security import verify_csrf
from ...services import tacview as TV
from ...services.audit import record_audit
from ..deps import Principal, get_db, require_member
from ..templating import render

log = logging.getLogger("gfvfw.web.tacview")

router = APIRouter(prefix="/tacview")

#: 动作完成后在任务详情页顶部显示的一句话。**服务端写死**，
#: 不回显 query 里的任意文本（同 ACMI 工作台的 ``did`` 白名单口径）。
DID_MESSAGES = {
    "uploaded": "Tacview XML 已上传并完成战斗分析。",
    "duplicate": "这份 XML 此前已上传过（内容相同），未重复入库。",
    "failed": "文件已归档，但解析失败 —— 原因见「Tacview 战斗分析」区块。",
    "none": "没有选择任何文件。",
    "deleted": "已删除该 Tacview XML（磁盘原件一并删除）。",
    "reparsed": "已用归档原件重新完成战斗分析。",
    "toolarge": "文件超过大小上限，未上传。",
}


def did_message(did: str) -> str:
    return DID_MESSAGES.get(did, "")


def _q(text: str, maxlen: int = 200) -> str:
    from urllib.parse import quote
    return quote(text[:maxlen], safe="")


async def _read_to_tmp(file: UploadFile) -> tuple[str | None, str]:
    """把上传写进临时文件（大文件不能整体读进内存），返回 (路径, 后缀)。"""
    suffix = Path(file.filename or "x.xml").suffix or ".xml"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
    except Exception:                                   # noqa: BLE001
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise
    return tmp_path, suffix


@router.post("/upload")
async def upload(request: Request,
                 file: UploadFile = File(default=None),
                 mission_id: str = Form(""),
                 csrf_token: str = Form(""),
                 principal: Principal = Depends(require_member),
                 db: Session = Depends(get_db)):
    """上传一份 Tacview XML 导出（挂在任务上）并立即解析。"""
    if not principal.can(ACMI_UPLOAD):
        raise HTTPException(status_code=403, detail="没有上传 Tacview 分析文件的权限")
    verify_csrf(request, csrf_token)

    back = "/missions/%s" % mission_id if mission_id else "/missions"
    if not mission_id:
        raise HTTPException(status_code=400, detail="必须指定所属任务")
    mission = db.get(Mission, mission_id)
    if mission is None or mission.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not file or not file.filename:
        return RedirectResponse(back + "?did=none", status_code=303)

    tmp_path = None
    try:
        tmp_path, _suffix = await _read_to_tmp(file)
        rec, created = TV.store_and_parse(
            db, tmp_path, file.filename,
            uploaded_by=principal.user.id, mission_id=mission.id)
        record_audit(db, principal.user.id, "import", "tacview_xml_files", rec.id,
                     after={"mission_id": mission.id,
                            "filename": rec.original_filename,
                            "sha256": rec.sha256, "size": rec.size_bytes,
                            "duplicate": not created,
                            "parse_ok": bool(rec.vm_json)},
                     reason="上传 Tacview XML 导出（战斗分析）",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except TV.TacviewError as exc:
        db.rollback()
        return RedirectResponse("%s?did=toolarge&error=%s"
                                % (back, _q(str(exc))), status_code=303)
    except Exception as exc:                            # noqa: BLE001
        db.rollback()
        log.exception("Tacview XML 上传失败 mission=%s", mission.id)
        return RedirectResponse("%s?did=failed&error=%s"
                                % (back, _q("上传失败：%s" % exc)), status_code=303)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    did = ("duplicate" if not created
           else ("uploaded" if rec.vm_json else "failed"))
    return RedirectResponse("%s?did=%s" % (back, did), status_code=303)


def _load_for_write(db: Session, file_id: str, principal: Principal) -> TV.TacviewXmlFile:
    """删除/重解析共用的加载与授权：自己的文件即可，他人的要 ``acmi.upload.any``。"""
    rec = TV.get(db, file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")
    is_self = rec.uploaded_by and rec.uploaded_by == principal.user.id
    if not is_self and not principal.can(ACMI_UPLOAD_ANY):
        raise HTTPException(
            status_code=403,
            detail="只能操作自己上传的文件；操作他人上传需要 acmi.upload.any 权限")
    return rec


@router.post("/{file_id}/delete")
def delete(file_id: str, request: Request,
           csrf_token: str = Form(""),
           principal: Principal = Depends(require_member),
           db: Session = Depends(get_db)):
    """删除一份归档（行 + 磁盘原件）。

    与 ACMI 不同：XML 分析**不参与架次入库**，删除不涉及"撤销归并"，
    直接硬删即可（同一文件重传靠 sha256 判重，必须真删才能重传）。
    """
    verify_csrf(request, csrf_token)
    rec = _load_for_write(db, file_id, principal)
    mission_id = rec.mission_id

    before = {"filename": rec.original_filename, "sha256": rec.sha256,
              "size_bytes": rec.size_bytes, "mission_id": mission_id}
    TV.delete_upload(db, rec)
    record_audit(db, principal.user.id, "delete", "tacview_xml_files", file_id,
                 before=before, reason="删除 Tacview XML 归档（原件一并删除）",
                 actor_role=principal.primary_role, request=request)
    db.commit()

    back = "/missions/%s" % mission_id if mission_id else "/missions"
    return RedirectResponse("%s?did=deleted" % back, status_code=303)


@router.post("/{file_id}/reparse")
def reparse(file_id: str, request: Request,
            csrf_token: str = Form(""),
            principal: Principal = Depends(require_member),
            db: Session = Depends(get_db)):
    """用**已归档的原件**重新解析（分析器升级后回填历史存档）。"""
    verify_csrf(request, csrf_token)
    rec = _load_for_write(db, file_id, principal)
    mission_id = rec.mission_id
    try:
        TV.reparse(db, rec)
        record_audit(db, principal.user.id, "update", "tacview_xml_files", rec.id,
                     after={"parse_ok": bool(rec.vm_json),
                            "parse_error": rec.parse_error},
                     reason="重新解析 Tacview XML 导出",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except TV.TacviewError as exc:
        db.rollback()
        back = "/missions/%s" % mission_id if mission_id else "/missions"
        return RedirectResponse("%s?did=failed&error=%s"
                                % (back, _q(str(exc))), status_code=303)

    if mission_id:
        return RedirectResponse("/tacview/%s?message=%s"
                                % (rec.id, _q(DID_MESSAGES["reparsed"])),
                                status_code=303)
    return RedirectResponse("/missions?did=reparsed", status_code=303)


@router.get("/{file_id}/download")
def download(file_id: str,
             principal: Principal = Depends(require_member),
             db: Session = Depends(get_db)):
    rec = TV.get(db, file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")
    path = TV.absolute_path(rec)
    if not path.exists():
        raise HTTPException(status_code=410, detail="原件已不在服务器上，请联系管理员")
    return FileResponse(path, filename=rec.original_filename,
                        media_type="application/xml")


@router.get("/{file_id}")
def analysis(file_id: str, request: Request,
             principal: Principal = Depends(require_member),
             db: Session = Depends(get_db)):
    """战斗分析页：击杀链 / 武器效能 / 空战按目标机型 / 飞行结局。"""
    rec = TV.get(db, file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="归档记录不存在")
    mission = db.get(Mission, rec.mission_id) if rec.mission_id else None
    vm = TV.view_model(rec)

    is_self = rec.uploaded_by and rec.uploaded_by == principal.user.id
    can_manage = is_self or principal.can(ACMI_UPLOAD_ANY)
    return render(request, "tacview/analysis.html", {
        "rec": rec,
        "mission": mission,
        "vm": vm,
        "can_manage": can_manage,
    })
