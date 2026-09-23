"""
占位页路由：资料查询。

⚠️ 资料查询**尚未实现**。本模块刻意把它做成**明确的"未实现"页面**，
而不是隐藏或留空 —— 菜单里点了却看到空白页，会让人以为是故障。

可见性
------
``/library`` 是**仅限队内**的内容（联队要求"队员可查看仅限队内的资料"），
所以用 :func:`require_member` 守卫：游客会看到"需要队员身份"的说明页，
未登录访客会被送到登录页。

将来真正实现时，``Document.visibility``（``public`` / ``members`` / ``command``）
要在这里落地 —— 公开文档可以让游客也看到，但那需要一个公开的资料列表页，
而不是把整页开放。

「战役管理」原先也是占位页，现已由 :mod:`gfvfw.web.routers.theater`
真正实现（解析 BMS ``.cam`` 存档），故从本模块移除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, require_member
from ..templating import render

router = APIRouter()


@router.get("/library")
def library(request: Request,
            principal: Principal = Depends(require_member),
            db: Session = Depends(get_db)):
    """资料查询 —— 计划用于手册/检查单/地图等资料的检索（尚未实现）。

    ⚠️ 仅限队员。
    """
    from ...models import Document

    docs = db.scalar(select(func.count()).select_from(Document)
                     .where(Document.deleted_at.is_(None))) or 0

    return render(request, "library/index.html", {
        "docs": docs,
        "can_upload": principal.can("document.upload"),
    })
