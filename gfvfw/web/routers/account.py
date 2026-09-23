"""账号自助路由：修改自己的密码。

为什么单独一个页面
------------------
密码是 argon2id 单向哈希，**找不回来**。没有自助改密，成员改密码就只能找运维，
运维改密码就只能直接动数据库 —— 那会绕过审计，而且没有强度校验。

设计要点
--------
* **必须验原密码**：仅靠登录态不够。浏览器被借用 / Cookie 被窃时，
  只凭会话就能改密码等于把账号彻底交出去。
* 改密成功后**不踢出当前会话**（我们的会话只存 ``user_id``，没有版本号，
  无法只失效其他会话），但会写审计，并在页面上明确告知这一限制。
* 强度规则与 CLI **共用** :func:`password_problem`，避免两处规则漂移。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...security import (
    LOCKOUT_MINUTES, MAX_FAILED_LOGINS, hash_password, new_token,
    password_problem, verify_csrf, verify_password,
)
from ...services.audit import record_audit
from ..deps import Principal, get_db, get_principal, require_login
from ..templating import render

router = APIRouter()


def _page_context(principal: Principal, **extra) -> dict:
    """账号页的公共上下文。

    ⚠️ 锁定策略的数字从 ``security`` 取，**不写死在模板里** ——
    否则改了策略常量，页面上的提示会继续骗人。
    """
    ctx = {
        "user": principal.user,
        "role_name": principal.primary_role_name,
        "max_failed_logins": MAX_FAILED_LOGINS,
        "lockout_minutes": LOCKOUT_MINUTES,
    }
    ctx.update(extra)
    return ctx


@router.get("/account")
def account_page(request: Request,
                 principal: Principal = Depends(require_login)):
    """账号页（目前只有改密一栏，后续可放会话/隐私设置）。"""
    return render(request, "account/password.html", _page_context(
        principal, changed=request.query_params.get("did") == "password"))


@router.post("/account/password")
def change_password(request: Request,
                    current_password: str = Form(""),
                    new_password: str = Form(""),
                    confirm_password: str = Form(""),
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require_login),
                    db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)
    user = principal.user

    def fail(message: str, status: int = 400):
        return render(request, "account/password.html",
                      _page_context(principal, error=message),
                      status_code=status)

    # 1) 必须验原密码 —— 仅凭登录态不足以证明是本人
    if not verify_password(user.password_hash, current_password):
        # ⚠️ 不把失败计入 failed_login_count：那不是登录尝试，
        #    否则用户改密时打错两次原密码就会被锁在门外。
        record_audit(db, user.id, "user.password.change_failed", "users", user.id,
                     reason="原密码不正确", actor_role=principal.primary_role,
                     request=request)
        db.commit()
        return fail("原密码不正确")

    if not new_password:
        return fail("新密码不能为空")

    if new_password == current_password:
        return fail("新密码不能与原密码相同")

    if new_password != confirm_password:
        return fail("两次输入的新密码不一致")

    problem = password_problem(new_password)
    if problem:
        return fail(problem)

    from ...db import utcnow                     # 局部导入，避免顶层循环依赖

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    # 改密时顺手清掉登录失败计数：本人已证明身份，没必要继续留着锁
    user.failed_login_count = 0
    user.locked_until = None

    record_audit(db, user.id, "user.password.change", "users", user.id,
                 after={"password_changed_at": str(user.password_changed_at)},
                 reason="本人在账号页修改密码",
                 actor_role=principal.primary_role, request=request)
    db.commit()

    return RedirectResponse("/account?did=password", status_code=303)


@router.post("/account/token")
def rotate_csrf(request: Request,
                csrf_token: str = Form(""),
                principal: Principal = Depends(require_login)):
    """轮换本会话的 CSRF 令牌（怀疑令牌外泄时用）。"""
    verify_csrf(request, csrf_token)
    request.session["csrf"] = new_token(24)
    return RedirectResponse("/account", status_code=303)
