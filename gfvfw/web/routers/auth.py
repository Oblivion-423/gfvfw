"""认证路由：登录 / 退出。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import LOGIN_ALLOWED_STATUSES, User
from ...security import (
    MAX_FAILED_LOGINS, SESSION_USER_KEY, hash_password, is_locked, lockout_until,
    needs_rehash, privacy_hash, verify_password, verify_csrf,
)
from ..deps import get_db, get_principal, require_login, Principal
from ..templating import render

log = logging.getLogger("gfvfw.auth")

router = APIRouter()


def _safe_next(value: str | None) -> str:
    """只接受站内相对路径，防止开放重定向。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/login")
def login_form(request: Request, next: str | None = None,
               principal: Principal = Depends(get_principal)):
    if principal.is_authenticated:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next_url": _safe_next(next)})


@router.post("/login")
def login_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...),
                 csrf_token: str = Form(""),
                 next: str = Form("/"),
                 db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)
    target = _safe_next(next)

    user = db.scalar(select(User).where(User.username == username.strip()))

    # ⚠️ 统一错误信息：不区分"用户不存在"与"密码错误"，避免账号枚举。
    generic = "用户名或密码不正确"

    if user is None:
        # 仍然执行一次哈希校验，避免用响应时间区分用户是否存在
        verify_password(hash_password("dummy"), password)
        return render(request, "login.html",
                      {"error": generic, "username": username, "next_url": target},
                      status_code=401)

    if is_locked(user):
        return render(request, "login.html", {
            "error": "该账号因多次登录失败已被临时锁定，请稍后再试。",
            "username": username, "next_url": target,
        }, status_code=429)

    if not verify_password(user.password_hash, password):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = lockout_until()
            user.failed_login_count = 0
        db.commit()
        return render(request, "login.html",
                      {"error": generic, "username": username, "next_url": target},
                      status_code=401)

    # ⚠️ 状态判定用**白名单**（fail closed）：只有 LOGIN_ALLOWED_STATUSES 里的
    #    取值能建立会话。这里曾经只判 `== "suspended"`，于是任何未列出的取值
    #    （线上真实出现过 `disabled`）都能正常登录。
    if user.status == "suspended":
        return render(request, "login.html", {
            "error": "该账号已被停用，请联系管理员。",
            "username": username, "next_url": target,
        }, status_code=403)

    if user.status not in LOGIN_ALLOWED_STATUSES:
        # 未知取值：不告诉对方具体状态（可能是脏数据），但坚决不放行
        log.warning("拒绝登录：账号 %s 的状态取值未知（%r）", user.username, user.status)
        return render(request, "login.html", {
            "error": "该账号当前不可用，请联系管理员。",
            "username": username, "next_url": target,
        }, status_code=403)

    # 登录成功：清理计数、必要时升级哈希参数
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    db.commit()

    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    verify_csrf(request, csrf_token)
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse("/", status_code=303)
