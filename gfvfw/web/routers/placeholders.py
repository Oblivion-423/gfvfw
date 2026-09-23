"""
占位页路由：资料查询。

⚠️ 资料查询**尚未实现**。本模块刻意把它做成**明确的"未实现"页面**，
而不是隐藏或留空 —— 菜单里点了却看到空白页，会让人以为是故障。

「战役管理」原先也是占位页，现已由 :mod:`gfvfw.web.routers.theater`
真正实现（解析 BMS ``.cam`` 存档），故从本模块移除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, get_principal
from ..templating import render

router = APIRouter()


@router.get("/library")
def library(request: Request,
            principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    """资料查询 —— 计划用于手册/检查单/地图等资料的检索（尚未实现）。"""
    from ...models import Document

    docs = db.scalar(select(func.count()).select_from(Document)
                     .where(Document.deleted_at.is_(None))) or 0

    return render(request, "library/index.html", {
        "docs": docs,
        "can_upload": principal.can("document.upload"),
    })
