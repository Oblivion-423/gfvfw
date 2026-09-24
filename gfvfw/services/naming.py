"""呼号 / 用户名的唯一性判定（唯一实现点）。

系统里有**两个独立的命名空间**：

* ``members.callsign`` —— 名册呼号，游戏内身份，ACMI 归并靠它认人；
* ``users.username``   —— 登录名。

它们各自有唯一约束。本模块把这两条约束**集中在一处**，别在各入口重写：

==========================  ==========================  ==========================
入口                        要防的                      用哪个函数
==========================  ==========================  ==========================
``/register``（公开）        登录名重复                  :func:`username_owner`
``/enroll``（公开）          两者都查 + 呼号唯一          :func:`callsign_owner`
``/members/new``            呼号重复                    :func:`callsign_owner`
``cli create-member``       两者都查                    :func:`callsign_owner`
==========================  ==========================  ==========================

⚠️ 全部**不区分大小写**，且**排除已软删除**的名册成员 ——
软删的成员呼号可以重新启用（联队确实会有人退役后新人接呼号）。

关于"两个命名空间不得同名"这条规则（**已按联队口径取消**）
----------------------------------------------------------
这里曾经禁止 ``username == callsign``，理由是：游客在界面上的显示名会回落成
用户名（``deps.Principal.display_name``），于是一个用户名恰好等于现有成员呼号
的账号，**在名册与审批页里看起来就是那位成员**（冒充）。

但联队的实际口径是**用呼号当登录名**（"登录名可以和名册中的呼号相同"），
这条禁令会把联队最自然的用法挡在门外。所以改为**消除'看起来像'本身**，
而不是禁止重名：

* 导航栏的名字旁边**始终**跟着身份标签（``principal.identity_label``：
  访客 / 游客 / 角色名），所以"Viper 游客"与"Viper 队员"不会混为一谈；
* 入队审批页把该列明确标成**登录名**，并注明"登录名可能与呼号相同"；
* 真正会把关的是**呼号唯一性**（:func:`callsign_owner`）：提升为队员时仍会
  拦下重名，不会出现两个同名成员；
* ACMI 认领与归并走的是 ``ACMI_CLAIM_PILOT`` 权限点（**仅队员**），
  与登录名无关，所以重名拿不到别人的架次。

也就是说：**重名现在允许，但重名换不来任何权限。**
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Application, Member, User

#: 申请处于这些状态时，其 ``desired_callsign`` 仍算"占着"呼号。
#: ``rejected`` 不算 —— 被拒的人不应该长期霸占一个呼号。
_ACTIVE_APPLICATION_STATUSES = ("submitted", "screening", "approved", "invited", "activated")


def _lower(column):                                     # noqa: ANN001, ANN202
    return func.lower(column)


def member_by_callsign(db: Session, callsign: str,
                       *, exclude_member_id: Optional[str] = None) -> Optional[Member]:
    """按呼号找名册成员（不区分大小写，排除已软删除）。"""
    if not callsign:
        return None
    stmt = select(Member).where(_lower(Member.callsign) == callsign.strip().lower(),
                                Member.deleted_at.is_(None))
    if exclude_member_id:
        stmt = stmt.where(Member.id != exclude_member_id)
    return db.scalar(stmt.limit(1))


def user_by_username(db: Session, username: str) -> Optional[User]:
    """按登录名找账号（不区分大小写）。"""
    if not username:
        return None
    return db.scalar(
        select(User).where(_lower(User.username) == username.strip().lower()).limit(1))


def callsign_owner(db: Session, callsign: str,
                   *, exclude_member_id: Optional[str] = None) -> Optional[str]:
    """呼号被谁占了？返回**可读原因**；没人占则返回 ``None``。

    既看名册成员，也看**未撤销的申请**（先到先得：有人正在申请这个呼号时，
    管理员不该把它直接开给别人 —— 那会让那个申请人永远无法被提升）。
    """
    if not callsign or not callsign.strip():
        return None
    cs = callsign.strip()

    taken = member_by_callsign(db, cs, exclude_member_id=exclude_member_id)
    if taken is not None:
        return "呼号「%s」已在名册里（成员：%s）" % (cs, taken.callsign)

    hit = db.scalar(
        select(Application.desired_callsign)
        .where(_lower(Application.desired_callsign) == cs.lower(),
               Application.status.in_(_ACTIVE_APPLICATION_STATUSES))
        .limit(1))
    if hit is not None:
        return "呼号「%s」已被一份未处理的入队申请占用" % cs
    return None


def username_owner(db: Session, username: str) -> Optional[str]:
    """登录名被谁占了？返回可读原因；没人占则返回 ``None``。"""
    if not username or not username.strip():
        return None
    if user_by_username(db, username) is not None:
        return "用户名「%s」已被占用" % username.strip()
    return None


def members_without_account(db: Session) -> list[Member]:
    """名册成员里**还没有登录账号**的那些（可用于"给他开个号"）。

    典型来源：只用 ``/members/new`` 或 ``cli create-member --callsign`` 登记了人，
    但没开账号。按呼号排序，方便在下拉里找。
    """
    with_account = select(User.member_id).where(User.member_id.is_not(None))
    return list(db.scalars(
        select(Member)
        .where(Member.deleted_at.is_(None), Member.id.notin_(with_account))
        .order_by(Member.callsign)).all())


def has_account(db: Session, member_id: str) -> bool:
    """该名册成员是否已经有登录账号。"""
    return db.scalar(
        select(User.id).where(User.member_id == member_id).limit(1)) is not None
