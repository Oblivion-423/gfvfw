"""
ACMI 摄入层：上传文件 → 解析 → 归并建议 → 人工确认 → 认领映射。

对应 docs/database-design.md §4。

⚠️ 核心规则（设计文档 §7.2）
    ``Pilot=`` 存在 **且** 飞行员名命中名册 → 判定为联队成员飞行。
    * 不按机型过滤（实测同一飞行员驾驶多国多型飞机）
    * AI 不生成 ``sorties``，因此"不统计 AI"无需过滤逻辑
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
from .identity import IdMixin, TimestampMixin

#: [0]经度 [1]纬度 [2]海拔(米) [3]roll [4]pitch [5]yaw [6]东(m) [7]北(m) [8]航向
ACMI_T_FORMAT_DOC = (
    "T=[0]lon [1]lat [2]alt_m [3]roll [4]pitch [5]yaw [6]u_east_m [7]v_north_m [8]heading"
)


class AcmiFile(IdMixin, TimestampMixin, Base):
    """上传的 ACMI 原始文件。

    ⚠️ ``sha256`` 唯一 —— 多人都传同一份主机录像时，第二次上传命中已有记录，
    直接提示"此文件已存在"，而不是产生重复数据（R4）。
    """

    __tablename__ = "acmi_files"

    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 相对 :data:`gfvfw.config.settings.storage_dir` 的路径（不存 BLOB）
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ---- 容器（实测 .zip.acmi 实为 ZIP，内含 acmi.txt）----
    container_format: Mapped[Optional[str]] = mapped_column(String(16))   # zip / plain
    inner_entry_name: Mapped[Optional[str]] = mapped_column(String(128))

    # ---- 头部 ----
    format_version: Mapped[Optional[str]] = mapped_column(String(16))
    data_recorder: Mapped[Optional[str]] = mapped_column(String(64))
    data_source: Mapped[Optional[str]] = mapped_column(String(64))
    #: ⚠️ 剧本地图纪元时间，**不可当真实日期**（设计文档 §7.3）
    reference_time: Mapped[Optional[str]] = mapped_column(String(64))

    # ---- 时间 ----
    #: 文件名里的时间戳 = 本次 ACS 录制**写出文件**的时刻。
    #: ⚠️ 它同时被当作"t=0 基准点"的反推依据，见 recorded_start_at 的说明。
    filename_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: t=0 基准点 + 末标记 = 录制终点（数值上等于 filename_time）
    recorded_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: t=0 基准点 + **首**标记 = 录制起点。
    #: ⚠️ 早期实现错写成"t=0 基准点"本身（即把剧本纪元当成了录制起点），
    #:    导致时间窗比真实录制宽出整个"首标记"，任务时长因此被撑大。
    recorded_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 文件内**最小**时间标记（秒）
    min_relative_seconds: Mapped[Optional[float]] = mapped_column(Float)
    #: 文件内**最大**时间标记（秒）
    max_relative_seconds: Mapped[Optional[float]] = mapped_column(Float)
    #: **录制时长** = max − min。不是 max 本身！
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)

    # ---- 统计（用于核对解析完整性）----
    line_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    timestamp_line_count: Mapped[Optional[int]] = mapped_column(Integer)
    object_count: Mapped[Optional[int]] = mapped_column(Integer)
    objects_with_pilot_name: Mapped[Optional[int]] = mapped_column(Integer)
    unnamed_ai_actors: Mapped[Optional[int]] = mapped_column(Integer)

    #: JSON 仅用于存储，不用于查询（设计文档 §0 禁用 SQLite 专有 JSON 函数）
    pilot_names_json: Mapped[Optional[str]] = mapped_column(Text)

    #: ⚠️ 解析结果的持久化快照（JSON，仅存储不查询）。
    #:
    #: 为什么需要它：``sorties`` 的时长/航程/武器等指标是**在确认入库时**才写入的，
    #: 而确认发生在上传之后的某个时刻（可能隔天）。若不在此保存，确认时就得把
    #: 少则几 MB、多则 100+ MB 的 ACMI **重新解析一遍**。
    #: 保存快照后，确认步骤变成纯数据库操作，不再触碰原始文件。
    sortie_summaries_json: Mapped[Optional[str]] = mapped_column(Text)

    # ---- 解析状态 ----
    #: pending / parsing / parsed / failed
    parse_status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False)
    #: 解析失败必须留原因，**不静默丢弃**（R2）
    parse_error: Mapped[Optional[str]] = mapped_column(Text)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    parse_warnings_json: Mapped[Optional[str]] = mapped_column(Text)

    uploaded_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))
    mission_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("missions.id"))
    batch_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("import_batches.id"))

    mission: Mapped[Optional["Mission"]] = relationship(back_populates="acmi_files")
    actors: Mapped[list["AcmiActor"]] = relationship(back_populates="acmi_file")

    __table_args__ = (
        Index("ix_acmi_files_parse_status", "parse_status"),
        Index("ix_acmi_files_mission", "mission_id"),
        Index("ix_acmi_files_started", "recorded_start_at"),
    )


class AcmiActor(IdMixin, TimestampMixin, Base):
    """ACMI 文件中的对象（只对载人平台建行，避免大量地面目标噪声）。"""

    __tablename__ = "acmi_actors"

    acmi_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("acmi_files.id"), nullable=False)
    #: ⚠️ 对象 ID 是 **16 进制**（实测 9 之后是 a）
    acmi_object_id: Mapped[str] = mapped_column(String(16), nullable=False)

    pilot_name: Mapped[Optional[str]] = mapped_column(String(64))
    tactical_callsign: Mapped[Optional[str]] = mapped_column(String(32))
    aircraft_raw_name: Mapped[Optional[str]] = mapped_column(String(64))
    type_raw: Mapped[Optional[str]] = mapped_column(String(64))
    coalition: Mapped[Optional[str]] = mapped_column(String(32))

    #: `Pilot=` 是否存在。⚠️ **≠ 人驾**（AI 也可以有名，但联队口径认为 AI 不带）
    has_pilot_name: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False)
    #: 是否判定为联队成员飞行 —— **由名册命中判定**，NULL = 未判定
    is_member_flight: Mapped[Optional[bool]] = mapped_column(Boolean)

    #: member / external / enemy / unknown（人工标注）
    role: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    member_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("members.id"))

    acmi_file: Mapped["AcmiFile"] = relationship(back_populates="actors")

    __table_args__ = (
        UniqueConstraint("acmi_file_id", "acmi_object_id",
                         name="uq_acmi_actors_file_object"),
        Index("ix_acmi_actors_pilot", "pilot_name"),
        Index("ix_acmi_actors_member", "member_id"),
        Index("ix_acmi_actors_has_pilot", "has_pilot_name"),
    )


class PilotMapping(IdMixin, TimestampMixin, Base):
    """原始飞行员名 → 名册成员 的别名表。

    ✅ **配一次，永久复用** —— 把 R3 从"每次导入都要人工配对"
    变成一次性成本（设计文档 §4.3）。
    """

    __tablename__ = "pilot_mappings"

    #: ACMI 里的原始名，如 ``Oblivion``
    raw_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    #: exact / confirmed / guessed
    confidence: Mapped[str] = mapped_column(
        String(16), default="confirmed", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    note: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_pilot_mappings_member", "member_id"),
    )


class AircraftAlias(IdMixin, TimestampMixin, Base):
    """ACMI 原始机型名 → 标准机型（如 ``F-16C B52M HAF`` → ``F-16C Block 52M``）。"""

    __tablename__ = "aircraft_aliases"

    raw_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    aircraft_type_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("aircraft_types.id"), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))


class IgnoredPilot(IdMixin, TimestampMixin, Base):
    """被判定为"非联队人员"的飞行员名（AI、路人、已离队且不入册者）。

    为什么单独一张表而不是给 ``acmi_actors`` 加字段：
    AC MI 里同一个名字会出现在成百上千条 actor 行上，逐行标记既冗余又易不一致。
    用一张"名字级"名单，一处标记即全局生效。
    """

    __tablename__ = "ignored_pilots"

    raw_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))


class ImportBatch(IdMixin, TimestampMixin, Base):
    """归并批次 —— 人工确认页面对的对象。

    把 N 份 ACMI 归并为 1 个 Mission，或拆分为多个。
    """

    __tablename__ = "import_batches"

    #: suggested / confirmed / imported / rejected
    status: Mapped[str] = mapped_column(
        String(16), default="suggested", nullable=False)

    suggested_mission_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("missions.id"))
    proposed_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    proposed_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 归并置信度（时间窗重叠 + 呼号重合度）
    overlap_score: Mapped[Optional[float]] = mapped_column(Float)

    pilot_names_json: Mapped[Optional[str]] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: ⚠️ 确认页的完整方案快照。
    #: 刻意存 JSON：归并方案的结构会随功能演进而变，存快照可避免为每种方案形态建表。
    #: 代价是不能用 SQL 查询方案内容 —— 但确认是一次性交互，不需要查询。
    plan_json: Mapped[Optional[str]] = mapped_column(Text)

    reviewed_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_import_batches_status", "status"),
        Index("ix_import_batches_mission", "suggested_mission_id"),
    )
