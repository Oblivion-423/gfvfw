"""
占位页路由：资料查询。

⚠️ 资料查询**尚未实现**。本模块刻意把它做成**明确的"未实现"页面**，
而不是隐藏或留空 —— 菜单里点了却看到空白页，会让人以为是故障。

可见性
------
``/library`` 是**列表页**，按"只开列表，不开详情"的口径用
:func:`require_login` 守卫：**游客也能看资料目录**（有哪些资料、多少份），
未登录访客会被送到登录页。

但**下载/上传**仍然仅队员：本页只给目录与数量，真正的文件下载入口
（将来实现）必须是 ``require_member``，否则"公开部分"就变成了
"公开全部资料"。这也是为什么这里没有任何下载链接。

将来真正实现时，``Document.visibility``（``public`` / ``members`` / ``command``）
要在这里落地 —— 公开文档可以让游客下载，但那需要按 ``visibility`` 逐条判断，
而不是把整页开放。

「战役管理」原先也是占位页，现已由 :mod:`gfvfw.web.routers.theater`
真正实现（解析 BMS ``.cam`` 存档），故从本模块移除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, require_login
from ..templating import render

router = APIRouter()


@router.get("/library")
def library(request: Request,
            principal: Principal = Depends(require_login),
            db: Session = Depends(get_db)):
    """资料查询 —— 计划用于手册/检查单/地图等资料的检索（尚未实现）。

    列表/目录对游客开放，下载与上传仅队员（见模块 docstring）。
    """
    from ...models import Document

    docs = db.scalar(select(func.count()).select_from(Document)
                     .where(Document.deleted_at.is_(None))) or 0

    return render(request, "library/index.html", {
        "docs": docs,
        "can_upload": principal.can("document.upload"),
    })
