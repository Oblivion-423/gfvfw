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
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import settings
from ..db import SessionLocal, engine
from ..security import SESSION_COOKIE
from ..services.bootstrap import ensure_schema, seed
from .deps import (
    LoginRequired, MemberRequired, PermissionDenied, load_principal,
)
from .routers import (
    account, acmi, applications, apply, auth, campaigns, enroll, home, logbook,
    members, missions, placeholders, sorties, stats, theater,
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

    @app.exception_handler(MemberRequired)
    async def _member_required(request: Request, exc: MemberRequired):
        """游客访问队内内容 —— 说清楚原因，**不要**重定向到登录页。

        游客已经登录了，重定向会造成「点→回登录→再点」的死循环，
        而且他看不出差的是"被提升为队员"这一步。

        ⚠️ 文案不要写成 Markdown（``**队员**``）—— 这个字符串是直接
        插进 HTML 的，星号会原样显示出来。加粗交给模板里的标签。
        """
        return render(request, "error.html", {
            "code": 403,
            "message": "这部分内容（详情页 / 写操作）仅限队员。你目前是游客。",
            "member_required": True,
        }, status_code=403)

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

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """兜底：未捕获异常 → 记全量堆栈 + 给用户一个**可回报的编号**。

        ⚠️ 加这个处理器的直接动机是一次真实故障：线上点"上传 .cam"得到
        光秃秃的 **Internal Server Error**，而使用者手上**没有任何可提供的信息**
        —— 不知道是哪个请求、该怎么描述、去哪儿看。于是排查只能靠猜。

        现在：
        * 服务端 `log.exception` 打出完整堆栈，**并带同一个编号**，
          所以 `journalctl -u gfvfw | grep <编号>` 一条就能定位；
        * 用户看到编号，可以直接把"编号 + 当时在做什么"告诉管理员。

        ⚠️ 编号只是**关联用**，不是安全边界 —— 它不泄露堆栈内容，
        页面上也不显示异常文本（异常里可能有文件路径等信息）。

        ⚠️ Starlette 的 `ServerErrorMiddleware` 在调用本处理器后仍会
        重新抛出异常，所以 TestClient(raise_server_exceptions=True) 的
        行为不变（测试里该炸的还是炸）。
        """
        error_id = uuid.uuid4().hex[:8]
        log.exception("未处理的服务器错误 #%s  %s %s",
                      error_id, request.method, request.url.path)
        return render(request, "error.html", {
            "code": 500,
            "error_id": error_id,
            "message": "服务器内部错误 —— 这多半是个 Bug，不是你操作错了。",
        }, status_code=500)

    # ---- 路由 ----
    # ⚠️ 顺序：具体前缀在前，避免被更宽泛的路径抢占。
    app.include_router(home.router)
    app.include_router(auth.router)
    # 公开申请（未登录即可提交）与管理员审批 —— 必须早于 members，
    # 否则 /applications 可能被 /members/{id} 之类的宽泛路径抢走。
    app.include_router(apply.router)
    app.include_router(applications.router)
    # 隐藏的「直接开队员」页（/enroll）—— 不在导航里，靠 require(application.review)
    # 把门。放在 members 之前，免得 /enroll 被更宽泛的路径抢走。
    app.include_router(enroll.router)
    app.include_router(account.router)
    app.include_router(logbook.router)
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
