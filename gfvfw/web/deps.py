"""
Web 层依赖：数据库会话、当前身份（Principal）、权限校验。

设计要点
--------
* **权限每请求从库读取**，不缓存进会话 Cookie —— 管理员撤销权限后立即生效，
  不会出现"改了权限但对方还能操作"的滞后窗口。
* 未登录用户是一个合法的 :class:`Principal`（``visitor``），
  只需 ``MEMBER_VIEW`` 这类基础权限的页面可被访客正常访问，
  不需要在每个路由写"if 已登录"。
* 权限校验失败抛 :class:`PermissionDenied`，由异常处理器统一渲染 403 页面，
  而不是让每个路由自己拼错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ACTIVE_STATUS, Member, MemberRole, Role, User
from ..permissions import highest_role, permissions_for, role_name
from ..security import SESSION_USER_KEY


# --------------------------------------------------------------------------
# 数据库会话
# --------------------------------------------------------------------------

def get_db() -> Session:
    """每请求一个数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# 主体（当前身份）
# --------------------------------------------------------------------------

@dataclass
class Principal:
    """当前请求的身份。未登录时 ``user`` 为 None。"""

    user: Optional[User] = None
    member: Optional[Member] = None
    role_codes: list[str] = field(default_factory=list)
    permissions: frozenset[str] = field(default_factory=frozenset)

    # ---- 便捷判定 ----

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_active_user(self) -> bool:
        """已登录**且**账号已激活（``pending`` 只能看公开内容）。"""
        return self.user is not None and self.user.status == "active"

    @property
    def display_name(self) -> str:
        if self.member is not None:
            return self.member.callsign
        if self.user is not None:
            return self.user.username
        return "访客"

    @property
    def primary_role(self) -> Optional[str]:
        return highest_role(self.role_codes)

    @property
    def primary_role_name(self) -> str:
        code = self.primary_role
        return role_name(code) if code else "访客"

    def can(self, *permissions: str) -> bool:
        """是否具备**全部**给定权限点。"""
        if not permissions:
            return True
        return all(p in self.permissions for p in permissions)

    def can_any(self, *permissions: str) -> bool:
        """是否具备**任一**给定权限点。"""
        if not permissions:
            return True
        return any(p in self.permissions for p in permissions)


ANONYMOUS = Principal()


# --------------------------------------------------------------------------
# 加载身份
# --------------------------------------------------------------------------

def load_principal(db: Session, request: Request) -> Principal:
    """从签名会话还原身份。

    ⚠️ **只有明确激活（``active``）的账号才获得权限点**，其余一律清空。
    这是**白名单**判定，不是黑名单 —— 原因见下方注释。
    """
    uid = request.session.get(SESSION_USER_KEY) if hasattr(request, "session") else None
    if not uid:
        return ANONYMOUS

    user = db.get(User, uid)
    if user is None:
        # 账号已被删除 —— 清理失效会话
        request.session.pop(SESSION_USER_KEY, None)
        return ANONYMOUS

    member = db.get(Member, user.member_id) if user.member_id else None

    is_active = user.status == ACTIVE_STATUS

    # 角色：只取未撤销的分配
    role_codes: list[str] = []
    if member is not None and is_active:
        rows = db.execute(
            select(Role.code)
            .join(MemberRole, MemberRole.role_id == Role.id)
            .where(MemberRole.member_id == member.id,
                   MemberRole.revoked_at.is_(None))
        ).scalars().all()
        role_codes = list(rows)
    elif not is_active:
        # ⚠️ 非激活账号**连角色名都不给**。
        #    否则界面上会出现"这个待审批账号显示为超级管理员"这种自相矛盾的画面 ——
        #    实测 `pending` 与 `disabled` 账号正是如此（见 tests/account_selfcheck.py）。
        role_codes = []

    # 未分配角色但账号已激活 → 兜底为最小角色 member，
    # 避免"已激活却什么都看不到"的荒谬状态。
    if not role_codes and is_active:
        role_codes = ["member"]

    # ⚠️ 白名单：只有 `active` 才有权限点。
    #    这里曾经写成 `if user.status == "suspended": perms = frozenset()` ——
    #    那是**黑名单**：任何未列出的状态取值（`pending`、`disabled`、
    #    以及将来新增的任何值）都会**保留全部权限**。
    #    而权限判定不只出现在 `Depends(require(...))` 里，也出现在
    #    路由体与模板的 `principal.can(...)` 中 —— 黑名单等于给这些调用点
    #    埋了一颗"加个新状态就爆炸"的雷。
    perms = permissions_for(role_codes) if is_active else frozenset()

    return Principal(user=user, member=member,
                     role_codes=role_codes, permissions=perms)


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    return load_principal(db, request)


CurrentPrincipal = Depends(get_principal)


# --------------------------------------------------------------------------
# 权限守卫
# --------------------------------------------------------------------------

class LoginRequired(HTTPException):
    def __init__(self, next_url: str = "/"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail="需要登录")
        self.next_url = next_url


class PermissionDenied(HTTPException):
    def __init__(self, needed: tuple[str, ...]):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN,
                         detail="权限不足")
        self.needed = needed


def require_login(principal: Principal = Depends(get_principal)) -> Principal:
    """要求已登录**且账号已激活**。"""
    if not principal.is_active_user:
        raise LoginRequired()
    return principal


def require(*permissions: str) -> Callable[..., Principal]:
    """生成一个要求指定权限点的依赖。

    用法::

        @router.post("/members")
        def create_member(p: Principal = Depends(require(MEMBER_CREATE))):
            ...
    """
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.is_active_user:
            raise LoginRequired()
        if not principal.can(*permissions):
            raise PermissionDenied(permissions)
        return principal
    return _dep


def require_any(*permissions: str) -> Callable[..., Principal]:
    """要求具备任一权限点（用于"看得到就能操作"的场景）。"""
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.is_active_user:
            raise LoginRequired()
        if not principal.can_any(*permissions):
            raise PermissionDenied(permissions)
        return principal
    return _dep
