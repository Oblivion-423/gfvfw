"""呼号 / 用户名的唯一性与**相互**冲突判定（唯一实现点）。

为什么需要这个模块
------------------
系统里有**两个独立的命名空间**：

* ``members.callsign`` —— 名册呼号，游戏内身份，ACMI 归并靠它认人；
* ``users.username``   —— 登录名。

它们各自有唯一约束，但**跨命名空间的冲突没人管**。后果是真实存在的：
网页注册只校验 ``users.username``，不看名册呼号；而游客在界面上的显示名
会回落成用户名（``deps.Principal.display_name``）—— 于是一个用户名恰好
等于现有成员呼号的游客，**在名册和审批页里看起来就是那位成员**。
提升时的呼号校验会拦住他（不会真的出现两个同名成员），但"看起来像"
本身就足以让管理员看错人。

四个入口都必须用同一套判定，否则各写一份、迟早不一致：

======================  ==========================  ============================
入口                    要防的                      用哪个函数
======================  ==========================  ============================
``/register``（公开）    用户名冒充名册呼号          :func:`username_shadows_callsign`
``/members/new``        呼号撞上已有登录名          :func:`callsign_shadows_username`
``/enroll``（隐藏）      两者都要 + 呼号唯一         :func:`callsign_owner`
``cli create-member``   两者都要 + 用户名唯一       :func:`username_owner`
======================  ==========================  ============================

⚠️ 全部**不区分大小写**，且**排除已软删除**的名册成员 ——
软删的成员呼号可以重新启用（联队确实会有人退役后新人接呼号）。
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


def callsign_shadows_username(db: Session, callsign: str,
                              *,
                              ignore_username: Optional[str] = None) -> Optional[str]:
    """**呼号**是否与某个已有**登录名**相同 → 返回可读原因。

    ``ignore_username``：正在为同一个人同时创建账号时，那个登录名要排除掉
    （否则 ``--callsign Viper --username Viper`` 会自己撞自己）。
    """
    if not callsign or not callsign.strip():
        return None
    cs = callsign.strip()
    stmt = select(User.username).where(_lower(User.username) == cs.lower())
    if ignore_username:
        stmt = stmt.where(_lower(User.username) != ignore_username.strip().lower())
    hit = db.scalar(stmt.limit(1))
    if hit is not None:
        return ("呼号「%s」与已有的**登录名**相同（账号：%s）—— "
                "两者同名会让人分不清谁是谁" % (cs, hit))
    return None


def username_shadows_callsign(db: Session, username: str) -> Optional[str]:
    """**登录名**是否与某个名册**呼号**相同 → 返回可读原因。

    这是"游客看起来像现有成员"的那个漏洞的正面拦截。
    """
    if not username or not username.strip():
        return None
    un = username.strip()
    hit = db.scalar(
        select(Member.callsign)
        .where(_lower(Member.callsign) == un.lower(),
               Member.deleted_at.is_(None))
        .limit(1))
    if hit is not None:
        return ("用户名「%s」与名册里的呼号相同（成员：%s）。"
                "换个登录名 —— 否则你在名册/审批页里看起来就是那位成员" % (un, hit))
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
