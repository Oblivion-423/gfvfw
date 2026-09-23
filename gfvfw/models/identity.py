"""
数据模型 —— 对应 docs/database-design.md v0.6。

设计约定（见设计文档 §0）
------------------------
* 主键：``String(36)``，UUID4 字符串
* 时间：``DateTime(timezone=True)``，**一律存 UTC**，展示层转 UTC+8
* 软删除：业务表带 ``deleted_at``；查询默认过滤 ``deleted_at IS NULL``
* 可见性：``visibility`` ∈ ``public`` / ``members`` / ``command``
* 来源标记：``*_source`` ∈ ``acmi`` / ``manual`` / ``self_reported`` / ``logbook``
* 审计列：``created_at`` / ``updated_at`` 由 :class:`TimestampMixin` 提供
* ⚠️ 不使用 SQLite 专有类型，:func:`gfvfw.db.check_portability` 会强制校验
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, new_id, utcnow

# --------------------------------------------------------------------------
# 通用 Mixin
# --------------------------------------------------------------------------

class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


# 允许的枚举取值（用 TEXT 存储，见设计文档 §0 D4）
VISIBILITY_VALUES = ("public", "members", "command")
DATA_SOURCE_VALUES = ("acmi", "manual", "self_reported", "logbook")


def visibility_column(default: str = "members"):
    return mapped_column(String(16), default=default, nullable=False)


# ==========================================================================
# 一、身份与权限层
# ==========================================================================

class User(IdMixin, TimestampMixin, Base):
    """登录账号。与名册 :class:`Member` **分开**存储（设计文档 §7 Q-1）。"""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(254), nullable=True)
    #: pending 待审批 / active 现役 / suspended 停用
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    member_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("members.id"), unique=True, nullable=True)

    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 上次改密时间。用于回答"这个账号的密码多久没换了"，
    #: 也为将来做"强制定期改密"留出依据。新增列由 schema_sync 自动补上。
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    member: Mapped[Optional["Member"]] = relationship(
        back_populates="user",
        foreign_keys="User.member_id",
    )

    __table_args__ = (
        Index("ix_users_status", "status"),
    )


class Member(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """联队名册成员。

    🔒 **明确不收集**真实姓名、身份证、手机号、住址（需求 Q7）。
    如将来需要，请单开 ``member_private`` 表并严格限权，不要加到本表。
    """

    __tablename__ = "members"

    callsign: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    service_number: Mapped[Optional[str]] = mapped_column(String(32), unique=True)

    #: active 现役 / reserve 休整 / retired 退役 / probation 预备
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    # ---- 军衔（身份侧，与资质完全独立）----
    rank_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("ranks.id"))
    rank_source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    rank_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rank_updated_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    bio: Mapped[Optional[str]] = mapped_column(Text)
    avatar_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("documents.id"))
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)

    #: ⚠️ 必须显式指定 foreign_keys：``members`` 与 ``users`` 之间有**两条**外键路径
    #: （``users.member_id`` 与 ``members.rank_updated_by``），
    #: SQLAlchemy 无法自动判定，须明确指出这是账号绑定关系。
    user: Mapped[Optional["User"]] = relationship(
        back_populates="member",
        foreign_keys="User.member_id",
    )
    rank: Mapped[Optional["Rank"]] = relationship()
    qualifications: Mapped[list["MemberQualification"]] = relationship(
        back_populates="member")
    roles: Mapped[list["MemberRole"]] = relationship(back_populates="member")

    __table_args__ = (
        Index("ix_members_status_callsign", "status", "callsign"),
        CheckConstraint(
            "visibility IN ('public','members','command')", name="ck_members_visibility"),
    )


class Rank(IdMixin, TimestampMixin, Base):
    """军衔定义。level 1~7 对齐 BMS Logbook 序号（设计文档 §7.1）。"""

    __tablename__ = "ranks"

    level: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)          # 中文
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)       # 英文
    abbrev: Mapped[Optional[str]] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(16), default="officer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("ix_ranks_tier_level", "tier", "level"),)


class AircraftType(IdMixin, TimestampMixin, Base):
    """标准机型字典。联队执飞 9 型（设计文档 §7.2）。"""

    __tablename__ = "aircraft_types"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family: Mapped[Optional[str]] = mapped_column(String(32))   # F-16 / F-15
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("ix_aircraft_types_family", "family"),)


class Qualification(IdMixin, TimestampMixin, Base):
    """资质类型字典（与军衔独立）。"""

    __tablename__ = "qualifications"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: aircraft 机型 / role 职务 / weapon 武器
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    aircraft_type_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("aircraft_types.id"))
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "category", name="uq_qualifications_name_category"),
    )


class MemberQualification(IdMixin, TimestampMixin, Base):
    """成员持有的资质。"""

    __tablename__ = "member_qualifications"

    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    qualification_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("qualifications.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    granted_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    note: Mapped[Optional[str]] = mapped_column(Text)

    member: Mapped["Member"] = relationship(back_populates="qualifications")
    qualification: Mapped["Qualification"] = relationship()

    __table_args__ = (
        Index("ix_member_quals_member", "member_id", "qualification_id"),
    )


class Role(IdMixin, Base):
    """角色定义。一期固定 5 个，但存表以便二期扩展为自定义角色。"""

    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RolePermission(IdMixin, Base):
    """角色 → 权限点。代码只判权限点，不判角色名（需求 §3）。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id"), nullable=False)
    permission: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("role_id", "permission", name="uq_role_permissions"),
    )


class MemberRole(IdMixin, TimestampMixin, Base):
    __tablename__ = "member_roles"

    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id"), nullable=False)
    granted_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    member: Mapped["Member"] = relationship(back_populates="roles")
    role: Mapped["Role"] = relationship()

    __table_args__ = (
        Index("ix_member_roles_member", "member_id"),
    )


class Application(IdMixin, TimestampMixin, Base):
    """招新与入队流水线（需求 §4.7）：申请 → 审批 → 邀请 → 注册 → 激活。"""

    __tablename__ = "applications"

    desired_callsign: Mapped[str] = mapped_column(String(64), nullable=False)
    experience: Mapped[Optional[str]] = mapped_column(Text)
    intent: Mapped[Optional[str]] = mapped_column(Text)
    contact: Mapped[Optional[str]] = mapped_column(String(255))

    #: submitted / screening / approved / rejected / invited / activated
    status: Mapped[str] = mapped_column(String(16), default="submitted", nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[Optional[str]] = mapped_column(Text)

    #: 邀请码只存哈希
    invite_token_hash: Mapped[Optional[str]] = mapped_column(String(128))
    invite_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    invite_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resulting_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))

    #: 防刷：只存哈希，不存明文 IP（R7）
    source_ip_hash: Mapped[Optional[str]] = mapped_column(String(128))

    __table_args__ = (
        Index("ix_applications_status", "status"),
        Index("ix_applications_callsign", "desired_callsign"),
        Index("ix_applications_token", "invite_token_hash"),
    )
