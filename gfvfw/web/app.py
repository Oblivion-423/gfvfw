"""
FastAPI 应用装配。

启动顺序（lifespan）
--------------------
1. 建目录（``var/``、``var/storage``、``var/backups``）
2. 建表（幂等）
3. 播种基础数据（军衔 / 机型 / 角色权限，幂等）

中间件顺序
----------
Starlette 中间件是**后进先出**（后加的靠外）。此处：
``IdentityMiddleware``（最内，设置 request.state.principal）
  ← ``SessionMiddleware``（提供 request.session）
  ← 静态文件/路由
所以 IdentityMiddleware 能读到 session。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import settings
from ..db import SessionLocal, engine
from ..security import SESSION_COOKIE
from ..services.bootstrap import ensure_schema, seed
from .deps import LoginRequired, PermissionDenied, load_principal
from .routers import (
    acmi, auth, campaigns, home, members, missions, placeholders, sorties,
    stats, theater,
)
from .templating import STATIC_DIR, render

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    ensure_schema(engine)
    with SessionLocal() as db:
        created = seed(db)
    log.info("%s 启动完成，基础数据：%s", settings.site_abbr, created)
    yield


async def identity_middleware(request: Request, call_next):
    """把当前身份挂到 ``request.state``，供模板与错误处理器使用。"""
    try:
        with SessionLocal() as db:
            request.state.principal = load_principal(db, request)
    except Exception:                                   # noqa: BLE001
        log.exception("加载身份失败，按访客处理")
        from .deps import ANONYMOUS
        request.state.principal = ANONYMOUS
    return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.site_name,
        description=settings.site_name_en,
        docs_url=None,      # 一期不暴露 API 文档
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # 中间件：后加的更靠外
    app.middleware("http")(identity_middleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=60 * 60 * 24 * 14,          # 14 天
        same_site="lax",
        # ⚠️ 生产环境必须 GFVFW_HTTPS_ONLY=true（见 config.py 的说明）。
        #    写死 False 会让会话 Cookie 缺少 Secure 属性，在用户走 http://
        #    的那一次请求里明文外泄 —— 那一次发生在代理跳转之前。
        https_only=settings.https_only,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- 异常处理：统一渲染错误页，而不是返回 JSON ----
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login?next=%s" % request.url.path,
                                status_code=303)

    @app.exception_handler(PermissionDenied)
    async def _permission_denied(request: Request, exc: PermissionDenied):
        return render(request, "error.html", {
            "code": 403,
            "message": "你没有执行此操作的权限。",
            "needed": list(exc.needed),
        }, status_code=403)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):        # noqa: ANN001
        return render(request, "error.html", {
            "code": 404, "message": "页面或记录不存在。",
        }, status_code=404)

    # ---- 路由 ----
    # ⚠️ 顺序：具体前缀在前，避免被更宽泛的路径抢占。
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(members.router)
    app.include_router(acmi.router)
    app.include_router(missions.router)
    app.include_router(sorties.router)
    app.include_router(campaigns.router)
    app.include_router(theater.router)
    app.include_router(stats.router)
    app.include_router(placeholders.router)

    return app


app = create_app()
