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

import json
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

#: 账号状态全集。**只有这三个是已知取值**，未知取值一律按最严格处理。
#:
#: ⚠️ 判定必须用**白名单**，不要写 ``status != "suspended"`` 这种黑名单：
#: 线上曾经存在一个取值 ``disabled``（早期权限测试留下的账号），
#: 它既不等于 ``suspended``、也不在已知集合里，于是被当作"正常账号"通过了登录，
#: 还带着名册里的角色名显示成「超级管理员」。
USER_STATUSES = (
    "pending",      # 入职待审批：可登录，但只能看公开内容
    "active",       # 正常：**唯一**会获得权限点的取值
    "suspended",    # 停用：不允许登录（已登录的会话保留但无权限，以便看到提示）
)

#: 允许**建立会话**的状态。其余一律拒绝，**包括未知取值**（fail closed）。
LOGIN_ALLOWED_STATUSES = ("pending", "active")

#: 唯一会获得权限点的状态。
ACTIVE_STATUS = "active"

#: 状态的中文标签（界面展示用）。未知取值原样显示，便于发现问题。
USER_STATUS_LABELS = {
    "pending": "待审批",
    "active": "正常",
    "suspended": "已停用",
}


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
    #: 账号状态。取值见 :data:`USER_STATUSES`，判定一律走**白名单**
    #: （只有 ``active`` 有权限、只有 ``LOGIN_ALLOWED_STATUSES`` 能登录）。
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    member_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("members.id"), unique=True, nullable=True)

    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 上次改密时间。用于回答"这个账号的密码多久没换了"，
    #: 也为将来做"强制定期改密"留出依据。新增列由 schema_sync 自动补上。
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    #: **注册时**的来源 IP 哈希（防刷，需求 R7；只存哈希不存明文 IP）。
    #:
    #: ⚠️ 为什么不能复用 ``Application.source_ip_hash``：注册与申请现在是
    #:    两步（联队要求"注册之后再提交申请"），注册那一刻还没有申请记录。
    #:    要限制"同 IP 每天注册几个"，就必须在**注册**时把来源记下来。
    #:    复用 ``privacy_hash``，与审计记录同一套哈希。
    registration_ip_hash: Mapped[Optional[str]] = mapped_column(String(128))

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

    # ---- 累计飞行量（Logbook 口径，需求 §4.8）----
    #
    # ⚠️ 这三个字段是**已确认的名册记录**，只有具备敏感权限的人才能写入。
    #    成员自己上传 logbook 时填的值先落在 ``logbook_files.declared_*``，
    #    确认后才写到这里 —— 否则任何成员都能自封飞行时数。
    #
    # ⚠️ 与 ACMI 统计的时长**口径不同，不要混用**：
    #    这里是 Logbook 的**跨存档历史累计**（含本系统上线前的飞行），
    #    ACMI 统计的是本系统内已归档架次之和。页面上必须分别标注。
    logbook_hours_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    logbook_sorties: Mapped[Optional[int]] = mapped_column(Integer)
    logbook_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    logbook_updated_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))

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


class LogbookFile(IdMixin, TimestampMixin, Base):
    """成员上传的 BMS Logbook（``.lbk``）存档。

    为什么"只归档、不解析"
    ----------------------
    ``.lbk`` 是 BMS 私有的定长二进制格式，官方读取实现是 ``LogbookEditor.exe``
    （32 位 MinGW/Qt4）。实测四个真实样本后确认（见 ``scripts/lbk_probe.py``、
    ``lbk_keyrecover.py``、``pe_strings.py`` 与 requirements §8.1/§5.6）:

    * 四个样本都是 **372 字节**，尾部有 **周期 42** 的重复块；
    * 文件不是简单的常量 XOR —— "明文含呼号 + 常量密钥"模型不成立；
    * ``LogbookEditor.exe`` **没有任何系统加密 API 导入**（只依赖 KERNEL32/
      msvcrt/Qt4/MinGW），所以混淆是内联自实现的。

    即使把格式逆向出来，BMS 版本升级也可能改动它并**静默**解析出错数据 ——
    这正是当初把自动导入降级为可选优化的原因。

    因此本表存**文件本身**与**成员从 LogbookEditor 界面读出后填写的声明值**，
    两者分开保存：声明值在未确认前**不影响名册**。

    ⚠️ **硬删除（无 ``deleted_at``）**，与 ``acmi_files`` 同理：
    只有硬删除，``(member_id, sha256)`` 唯一约束才不会挡住"删了再传同一个文件"。
    """

    __tablename__ = "logbook_files"

    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False)
    # ---- 解析结果（上传时自动解析，联队要求"不需要手动输入"）----
    #: 解析出的全部字段（JSON）。**保留全部字段**而不是只留我们认识的几个 ——
    #: 将来把某个偏移的含义确认下来时，历史存档无需重新上传即可回填。
    parsed_json: Mapped[Optional[str]] = mapped_column(Text)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 解析失败原因（格式变了/不是 Logbook）。失败不影响归档本身。
    parse_error: Mapped[Optional[str]] = mapped_column(Text)
    #: 解析使用的格式说明版本，便于日后判断要不要重解析
    parser_version: Mapped[Optional[str]] = mapped_column(String(32))

    #: 上传者填写的一句话说明（例如"换电脑后重开档"）
    note: Mapped[Optional[str]] = mapped_column(Text)

    # ---- 成员从 LogbookEditor 读出的声明值（**未确认为名册记录**）----
    declared_rank_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ranks.id"))
    declared_hours_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    declared_sorties: Mapped[Optional[int]] = mapped_column(Integer)
    #: 勾选的资质 id 列表（JSON 仅存储，不查询 —— 与 pilot_names_json 同策略）
    declared_qualification_ids_json: Mapped[Optional[str]] = mapped_column(Text)

    # ---- 确认（把声明值写入名册；需要敏感权限）----
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))

    member: Mapped["Member"] = relationship()
    uploader: Mapped["User"] = relationship(foreign_keys=[uploaded_by])
    confirmer: Mapped[Optional["User"]] = relationship(foreign_keys=[confirmed_by])
    declared_rank: Mapped[Optional["Rank"]] = relationship()

    __table_args__ = (
        # 同一成员不能重复上传**完全相同**的文件；硬删除后可以重传。
        UniqueConstraint("member_id", "sha256",
                         name="uq_logbook_files_member_sha256"),
        Index("ix_logbook_files_member", "member_id", "created_at"),
        Index("ix_logbook_files_sha256", "sha256"),
    )

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    def declared_qualification_ids(self) -> list[str]:
        """解析勾选的资质 id 列表（容错：脏数据当作空）。"""
        if not self.declared_qualification_ids_json:
            return []
        try:
            val = json.loads(self.declared_qualification_ids_json)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in val] if isinstance(val, list) else []

    def has_declaration(self) -> bool:
        """是否填了至少一项声明值。"""
        return bool(self.declared_rank_id
                    or self.declared_hours_seconds is not None
                    or self.declared_sorties is not None
                    or self.declared_qualification_ids())


class MemberAward(IdMixin, TimestampMixin, Base):
    """成员获得的荣誉/勋章（需求 §4.6 的 ``Award``）。

    数据来源主要是 BMS Logbook —— 官方 Logbook 有 6 个 ``edtMedal*`` 字段，
    解析后同步到这里（``source='logbook'``）。

    ⚠️ ``level`` 存的是**文件里的原始字节值**，不是"第几级"。
    实测该值可随飞行增加（例如同一人在 7 个月内从 6 变 8），
    所以它更像位域或计数，而不是布尔。
    """

    __tablename__ = "member_awards"

    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    #: 稳定标识，如 ``silver_star``
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    #: 文件里的原始字节值（0 表示未获得）
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="logbook", nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    updated_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    member: Mapped["Member"] = relationship()

    __table_args__ = (
        # 一个成员同一枚勋章只有一行（重复解析走更新而不是新增）
        UniqueConstraint("member_id", "code", name="uq_member_awards_member_code"),
        Index("ix_member_awards_member", "member_id"),
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
