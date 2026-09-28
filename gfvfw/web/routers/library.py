"""资料库路由。"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ...permissions import DOC_MANAGE, DOC_UPLOAD
from ...security import verify_csrf
from ...services import library as LIB
from ..deps import Principal, get_db, require, require_login, require_member
from ..templating import render

from typing import Optional

log = logging.getLogger("gfvfw.web.library")

router = APIRouter(prefix="/library", tags=["library"])


def _visible_levels(principal: Principal) -> list[str]:
    """当前身份能看到哪些 visibility 层级。"""
    if principal.can(DOC_MANAGE):
        return ["public", "members", "command"]
    if principal.is_member:
        return ["public", "members"]
    return ["public"]

def _actor_id(principal: Principal) -> Optional[str]:
    """当前用户 id。``require_login`` 之后 ``principal.user`` 必非空。"""
    return principal.user.id if principal.user else None

# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@router.get("")
def index(
    request: Request,
    principal: Principal = Depends(require_login),
):
    return render(request, "library/index.html", {
        "principal": principal,
        "can_upload": principal.can(DOC_UPLOAD),
        "can_manage": principal.can(DOC_MANAGE),
        "can_download": principal.is_member,
    })


# ---------------------------------------------------------------------------
# 只读 API
# ---------------------------------------------------------------------------

@router.get("/api/tree")
def api_tree(
    path: str = "",
    principal: Principal = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        folder = LIB.normalize_folder(path)
    except LIB.InvalidFolder as e:
        raise HTTPException(400, str(e))

    visible = _visible_levels(principal)
    subs = LIB.list_subfolders(db, folder, visibility_in=visible)
    files = LIB.list_folder(db, folder, visibility_in=visible)

    return {
        "path": folder,
        "folders": subs,
        "files": [
            {
                "id": d.id,
                "name": d.original_filename or d.title,
                "title": d.title,
                "category": d.category,
                "size_bytes": d.size_bytes,
                "visibility": d.visibility,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            }
            for d in files
        ],
    }


@router.get("/api/preview")
def api_preview(
    id: str,
    principal: Principal = Depends(require_login),
    db: Session = Depends(get_db),
):
    doc = LIB.get_document(db, id)
    if doc is None:
        raise HTTPException(404, "资料不存在")
    if doc.visibility not in _visible_levels(principal):
        raise HTTPException(403, "无权查看此资料")

    payload = LIB.preview_payload(doc)
    return {"id": doc.id, "title": doc.title, "visibility": doc.visibility,
            **payload}


@router.get("/api/raw")
def api_raw(
    id: str,
    principal: Principal = Depends(require_login),
    db: Session = Depends(get_db),
):
    doc = LIB.get_document(db, id)
    if doc is None:
        raise HTTPException(404, "资料不存在")
    if doc.visibility not in _visible_levels(principal):
        raise HTTPException(403, "无权查看此资料")

    path = LIB.storage_path_for(doc)
    if not path.is_file():
        raise HTTPException(404, "文件缺失")

    return FileResponse(
        path,
        media_type=doc.mime_type or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/api/download")
def api_download(
    id: str,
    principal: Principal = Depends(require_member),
    db: Session = Depends(get_db),
):
    doc = LIB.get_document(db, id)
    if doc is None:
        raise HTTPException(404, "资料不存在")
    if doc.visibility not in _visible_levels(principal):
        raise HTTPException(403, "无权下载此资料")

    path = LIB.storage_path_for(doc)
    if not path.is_file():
        raise HTTPException(404, "文件缺失")

    LIB.bump_download(db, doc.id, actor_user_id=_actor_id(principal))
    db.commit()

    safe_name = quote(doc.original_filename or doc.title or doc.id)
    return FileResponse(
        path,
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.original_filename or doc.title,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/api/search")
def api_search(
    q: str = "",
    principal: Principal = Depends(require_login),
    db: Session = Depends(get_db),
):
    visible = _visible_levels(principal)
    docs = LIB.search(db, q, visibility_in=visible)
    return {
        "q": q,
        "results": [
            {"id": d.id, "title": d.title,
             "name": d.original_filename or d.title,
             "folder": d.folder, "category": d.category}
            for d in docs
        ],
    }


# ---------------------------------------------------------------------------
# 写操作
# ---------------------------------------------------------------------------

@router.post("/api/upload")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(""),
    title: str = Form(""),
    category: str = Form("other"),
    visibility: str = Form("members"),
    description: str = Form(""),
    csrf_token: str = Form(""),
    principal: Principal = Depends(require(DOC_UPLOAD)),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)

    if visibility not in ("public", "members", "command"):
        raise HTTPException(400, "visibility 非法")
    if visibility == "command" and not principal.can(DOC_MANAGE):
        raise HTTPException(403, "无权创建 command 层资料")

    try:
        doc = LIB.save_upload(
            session=db,
            stream=file.file,
            original_filename=file.filename or "unnamed",
            folder=folder,
            category=category,
            title=title or None,
            description=description or None,
            visibility=visibility,
            actor_user_id=_actor_id(principal),
            request=request,
        )
        db.commit()
    except LIB.DuplicateUpload as e:
        db.rollback()
        raise HTTPException(409, {
            "message": str(e),
            "existing_doc_id": e.existing_doc_id,
        })
    except (LIB.UploadRejected, LIB.InvalidFolder) as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return {"id": doc.id, "title": doc.title, "folder": doc.folder}


@router.post("/api/document/{doc_id}/edit")
async def api_edit(
    doc_id: str,
    request: Request,
    folder: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    visibility: str = Form(...),
    description: str = Form(""),
    csrf_token: str = Form(""),
    principal: Principal = Depends(require(DOC_MANAGE)),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)

    if visibility not in ("public", "members", "command"):
        raise HTTPException(400, "visibility 非法")
    if visibility == "command" and not principal.can(DOC_MANAGE):
        raise HTTPException(403, "无权把资料提到 command 层")

    try:
        LIB.update_document(
            db, doc_id,
            actor_user_id=_actor_id(principal), request=request,
            folder=folder, title=title, category=category,
            visibility=visibility, description=description or None,
        )
        db.commit()
    except LIB.DocumentNotFound:
        raise HTTPException(404, "资料不存在")
    except LIB.InvalidFolder as e:
        raise HTTPException(400, str(e))

    return {"ok": True}


@router.post("/api/document/{doc_id}/delete")
async def api_delete(
    doc_id: str,
    request: Request,
    csrf_token: str = Form(""),
    principal: Principal = Depends(require(DOC_MANAGE)),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)

    try:
        LIB.delete_document(db, doc_id,
                            actor_user_id=_actor_id(principal), request=request)
        db.commit()
    except LIB.DocumentNotFound:
        raise HTTPException(404, "资料不存在")
    return {"ok": True}


@router.post("/api/document/{doc_id}/restore")
async def api_restore(
    doc_id: str,
    request: Request,
    csrf_token: str = Form(""),
    principal: Principal = Depends(require(DOC_MANAGE)),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    try:
        LIB.restore_document(db, doc_id,
                             actor_user_id=_actor_id(principal), request=request)
        db.commit()
    except LIB.DocumentNotFound:
        raise HTTPException(404, "资料不存在")
    return {"ok": True}