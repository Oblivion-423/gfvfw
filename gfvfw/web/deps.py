"""
Web 层依赖：数据库会话、当前身份（Principal）、权限校验。

三档身份（需求 §4.7 / §3）
--------------------------
联队明确要求区分**游客**与**队员**，所以本模块把"登录"和"是队员"拆成两件事：

===============  ==========================================  ==========================
档位             判定                                         能看到什么
===============  ==========================================  ==========================
未登录访客       ``user is None``                             公开部分（首页 / 申请 / 登录）
**游客**         ``user.status == "pending"``                 公开部分 + 自己的申请进度 + 账号页
**队员**         ``user.status == "active"``                  队内全部（名册/日志/战役/统计…）
===============  ==========================================  ==========================

游客**没有任何权限点**（白名单判定，见 :func:`load_principal`），所以
``require(...)`` 系列的页面它一律进不去。但它**是已登录用户**，因此：

* :func:`require_login` —— 只要已登录就行（游客也算）。用于账号页、申请进度页。
* :func:`require_member` —— 必须是队员。用于一切"仅限队内"的内容。

⚠️ 为什么游客访问队内页面时**不能**重定向到登录页
--------------------------------------------------
游客已经登录了。把他重定向到 ``/login`` 会形成
「点队内页面 → 回登录页 → 登录页说已登录 → 再点 → 再回」的死循环，
用户完全不知道自己差的是"被提升为队员"这一步。
所以 :class:`MemberRequired` 渲染一个**说清楚原因的 403 页面**。

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
from ..permissions import (
    APPLICATION_REVIEW, highest_role, permissions_for, role_name,
)
from ..security import SESSION_USER_KEY

#: 游客（已登录但未成为队员）的账号状态。
#: ⚠️ 与 ``models.identity.LOGIN_ALLOWED_STATUSES`` 一致：
#: 能建立会话但拿不到权限点的那些取值，语义上就是"游客"。
GUEST_STATUS = "pending"


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
    #: 待审批的游客账号数，只对具备 ``application.review`` 的人计算（否则恒为 0）。
    #: 导航栏用它显示角标，让管理员一眼看到有新申请。
    pending_applications: int = 0

    # ---- 便捷判定 ----

    @property
    def is_authenticated(self) -> bool:
        """已登录（**游客也算**）。"""
        return self.user is not None

    @property
    def is_member(self) -> bool:
        """**队员**：已登录且账号已激活（``active``）。

        ⚠️ 权限点判定也必须叠加这个属性 —— 见 :func:`load_principal` 的白名单说明。
        """
        return self.user is not None and self.user.status == ACTIVE_STATUS

    @property
    def is_guest(self) -> bool:
        """**游客**：已登录，但还不是队员（``pending``）。

        能看公开部分与自己的申请进度，拿不到任何权限点。
        """
        return self.user is not None and self.user.status == GUEST_STATUS

    @property
    def is_active_user(self) -> bool:
        """向后兼容别名 —— 等价于 :attr:`is_member`。

        ⚠️ 老代码里 ``require_login`` 依赖它来要求"已激活"，而
        ``require_login`` 现在的语义是"已登录（含游客）"。保留这个别名是为了
        不让改动散落到各处，但**新代码请用** :attr:`is_member` / :attr:`is_guest`，
        名字能自解释。
        """
        return self.is_member

    @property
    def display_name(self) -> str:
        if self.member is not None:
            return self.member.callsign
        if self.user is not None:
            return self.user.username
        return "访客"

    @property
    def identity_label(self) -> str:
        """界面上的身份标签：「访客」/「游客」/角色名。

        刻意**不**用 ``primary_role_name`` 直接展示游客 ——
        游客没有角色分配，那个属性会回落到"访客"，与"已注册但未入队"混淆。
        """
        if self.user is None:
            return "访客"
        if self.is_guest:
            return "游客"
        return self.primary_role_name

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

    # 待审批角标：只对有权审批的人查一次 COUNT。
    # ⚠️ 放在这里（而不是 render()）是因为本函数**已经有**数据库会话，
    #    而 render() 没有；否则要么给每个模板调用点加参数，要么多开一个会话。
    pending = 0
    if APPLICATION_REVIEW in perms:
        from sqlalchemy import func
        pending = db.scalar(
            select(func.count()).select_from(User)
            .where(User.status == "pending")) or 0

    return Principal(user=user, member=member,
                     role_codes=role_codes, permissions=perms,
                     pending_applications=pending)


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


class MemberRequired(HTTPException):
    """已登录，但**还不是队员**（游客）。

    ⚠️ 与 :class:`LoginRequired` 分开是刻意的：游客已经登录，
    把他重定向到登录页会造成「点→回登录→再点→再回」的死循环，
    而且他看不出自己缺的是"被提升为队员"这一步。
    这个异常渲染一个说明原因的 403 页面。
    """

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN,
                         detail="需要队员身份")


class PermissionDenied(HTTPException):
    def __init__(self, needed: tuple[str, ...]):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN,
                         detail="权限不足")
        self.needed = needed


def require_login(principal: Principal = Depends(get_principal)) -> Principal:
    """要求**已登录**（游客也算）。

    适用于"任何账号本人都有权做"的事：改自己的密码、看自己的申请进度。

    ⚠️ 本函数**不**保证对方是队员。要队内内容的页面请用 :func:`require_member`
    或 :func:`require`；要"游客也能看"的公开内容不必加守卫。
    """
    if not principal.is_authenticated:
        raise LoginRequired()
    return principal


def require_member(principal: Principal = Depends(get_principal)) -> Principal:
    """要求**队员**身份（已登录且已激活）。

    用于一切"仅限队内"的内容：名册、飞行日志、战役态势、统计、资料查询。
    """
    if principal.user is None:
        raise LoginRequired()
    if not principal.is_member:
        raise MemberRequired()
    return principal


def require(*permissions: str) -> Callable[..., Principal]:
    """生成一个要求指定权限点的依赖（隐含**队员**身份）。

    用法::

        @router.post("/members")
        def create_member(p: Principal = Depends(require(MEMBER_CREATE))):
            ...
    """
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.user is None:
            raise LoginRequired()
        if not principal.is_member:
            # 游客：权限点必然为空。给"需要队员身份"的说明页，
            # 而不是笼统的"权限不足"——否则用户不知道该去申请入队。
            raise MemberRequired()
        if not principal.can(*permissions):
            raise PermissionDenied(permissions)
        return principal
    return _dep


def require_any(*permissions: str) -> Callable[..., Principal]:
    """要求具备任一权限点（用于"看得到就能操作"的场景），隐含**队员**身份。"""
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.user is None:
            raise LoginRequired()
        if not principal.is_member:
            raise MemberRequired()
        if not principal.can_any(*permissions):
            raise PermissionDenied(permissions)
        return principal
    return _dep
