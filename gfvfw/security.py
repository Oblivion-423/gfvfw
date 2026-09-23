"""
安全基础设施：密码哈希、CSRF、会话。

设计要点
--------
* 密码用 **argon2id**（内存硬化，抗 GPU 爆破）。参数用库默认值 —— 比手调更稳。
* CSRF 用 **会话内令牌 + 表单隐藏字段**（double-submit 的会话变体）。
  公开注册表单同样需要（R7 防刷只是第一层，CSRF 是另一层）。
* 会话用 Starlette ``SessionMiddleware`` 的签名 Cookie，只存 ``user_id`` 与
  ``csrf_token``，**不在 Cookie 里放任何权限数据**（权限每次从库里读，避免失效滞后）。
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, status

#: argon2id 默认参数。库会按机器性能自动选参，勿手工调低。
_hasher = PasswordHasher()

#: 会话 Cookie 名
SESSION_COOKIE = "gfvfw_session"
#: 会话中存放的键
SESSION_USER_KEY = "uid"
SESSION_CSRF_KEY = "csrf"

#: 登录失败锁定策略
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """生成 argon2id 哈希。"""
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    """校验密码。任何异常都视为校验失败（不泄露失败原因）。"""
    if not stored_hash or plain is None:
        return False
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:                                   # noqa: BLE001
        return False


def needs_rehash(stored_hash: str) -> bool:
    """参数升级后是否需要重新哈希（登录成功时顺带升级）。"""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:                                   # noqa: BLE001
        return False


#: 密码最短长度。联队是内部系统，不强制复杂度规则（那只会逼出 `Passw0rd!` 这类
#: 弱密码），只设下限 + 拦几个最常见的弱口令。
MIN_PASSWORD_LENGTH = 8

#: 明显弱口令黑名单（小写比较）。不追求完备 —— 真正的防线是 argon2id + 登录锁定。
_WEAK_PASSWORDS = frozenset({
    "password", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyui", "qwerty123", "admin123", "administrator", "letmein1",
    "iloveyou", "football", "baseball", "sunshine", "gfvfw", "gfvfw123",
    "falcon", "falconbms", "bms12345", "11111111", "88888888", "abc12345",
})


def password_problem(plain: str) -> Optional[str]:
    """校验密码强度，返回**问题描述**；合规时返回 ``None``。

    ⚠️ CLI 与 Web 改密**共用这一个函数** —— 否则会出现"网页不让设的密码
    命令行能设"，两套规则迟早不一致。
    """
    if plain is None:
        return "密码不能为空"
    if len(plain) < MIN_PASSWORD_LENGTH:
        return "密码至少 %d 位" % MIN_PASSWORD_LENGTH
    if len(plain) > 200:
        # argon2 对超长输入会把整串喂进去；设个上限避免有人用 1 MB 密码打服务
        return "密码过长（最多 200 位）"
    if plain.strip() != plain:
        return "密码首尾不要有空格"
    if plain.lower() in _WEAK_PASSWORDS:
        return "这个密码太常见了，请换一个"
    return None


# --------------------------------------------------------------------------
# 令牌与哈希
# --------------------------------------------------------------------------

def new_token(nbytes: int = 32) -> str:
    """生成 URL 安全的一次性令牌（邀请码等）。"""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """令牌只存哈希（数据库泄露时不能直接使用）。"""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def privacy_hash(value: Optional[str]) -> Optional[str]:
    """对 IP / User-Agent 等做不可逆哈希，仅用于防刷与审计关联。

    🔒 不存明文 —— 需求 Q7 已确认不收集可识别个人的信息。
    """
    if not value:
        return None
    import hashlib
    return hashlib.sha256(("gfvfw|" + value).encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def get_csrf_token(request: Request) -> str:
    """取出（或首次生成）会话 CSRF 令牌。模板渲染时调用。"""
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = new_token(24)
        request.session[SESSION_CSRF_KEY] = token
    return token


def verify_csrf(request: Request, submitted: Optional[str]) -> None:
    """校验表单提交的 CSRF 令牌。

    ⚠️ 使用 :func:`hmac.compare_digest` 做**常数时间比较**，
    避免通过响应时间差异逐字节猜测令牌。
    """
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF 校验失败，请刷新页面后重试",
        )


# --------------------------------------------------------------------------
# 登录锁定
# --------------------------------------------------------------------------

def lockout_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)


def is_locked(user) -> bool:                            # noqa: ANN001
    """账号是否处于锁定状态。"""
    locked = getattr(user, "locked_until", None)
    if not locked:
        return False
    if locked.tzinfo is None:                           # SQLite 会丢失 tzinfo
        locked = locked.replace(tzinfo=timezone.utc)
    return locked > datetime.now(timezone.utc)
