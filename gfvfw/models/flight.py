"""
飞行数据核心层：战役 → 任务 → 个人架次，以及事件流水与上传状态。

对应 docs/database-design.md §3。
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
from .identity import IdMixin, SoftDeleteMixin, TimestampMixin

#: 任务类型（用 TEXT + 应用层枚举，见设计文档 §0 D4）
MISSION_TYPES = (
    "training", "patrol", "cap", "intercept", "escort",
    "strike", "sead", "cas", "recon", "transport", "other",
)

#: 架次事件类型
EVENT_TYPES = (
    "takeoff", "landing", "weapon_release", "hit", "kill", "shot_down",
    "crash", "ejection", "exceedance_overspeed", "exceedance_overg",
    "exceedance_terrain", "other",
)

#: 数据可信度（需求 §5.1）
DATA_CONFIDENCE_VALUES = ("exact", "partial", "estimated")


class Campaign(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """战役。可不属于任何战役的任务即日常训练。"""

    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    theater: Mapped[Optional[str]] = mapped_column(String(64))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: planning / active / finished
    status: Mapped[str] = mapped_column(String(16), default="planning", nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    cover_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("documents.id"))
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    missions: Mapped[list["Mission"]] = relationship(back_populates="campaign")

    __table_args__ = (
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_started", "started_at"),
    )


class Mission(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """任务 = 一次出击。归并确认的落点。"""

    __tablename__ = "missions"

    campaign_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("campaigns.id"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mission_number: Mapped[Optional[str]] = mapped_column(String(32))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: 冗余汇总，须提供"重算"入口，否则架次变更后会失真
    duration_seconds: Mapped[Optional[int]] = mapped_column(BigInteger)

    mission_type: Mapped[str] = mapped_column(
        String(16), default="other", nullable=False)
    base: Mapped[Optional[str]] = mapped_column(String(64))
    brief: Mapped[Optional[str]] = mapped_column(Text)
    debrief: Mapped[Optional[str]] = mapped_column(Text)
    #: success / partial / failure / aborted
    outcome: Mapped[Optional[str]] = mapped_column(String(16))

    visibility: Mapped[str] = mapped_column(String(16), default="members", nullable=False)
    #: complete / partial / missing —— 上传完整性（需求 §5.1）
    acmi_completeness: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False)

    confirmed_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    campaign: Mapped[Optional["Campaign"]] = relationship(back_populates="missions")
    sorties: Mapped[list["Sortie"]] = relationship(back_populates="mission")
    acmi_files: Mapped[list["AcmiFile"]] = relationship(back_populates="mission")

    __table_args__ = (
        Index("ix_missions_campaign_started", "campaign_id", "started_at"),
        Index("ix_missions_started", "started_at"),
        Index("ix_missions_type", "mission_type"),
        Index("ix_missions_visibility", "visibility"),
    )


class Sortie(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """个人架次。

    ⚠️ ``member_id`` 可为空 = **未认领飞行员**。
    实测存在飞行员名不在名册的情况（如 AI 或已离队者），
    **不为其建名册行**，只保留 ``raw_pilot_name``，界面显示"未认领"。

    起降为**联队口径**（设计文档 §7.4）：
      起飞 = 名字在本文件出现即计 1 次
      降落 = 文件结束时速度为零
    """

    __tablename__ = "sorties"

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id"), nullable=False)
    member_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("members.id"))

    #: ACMI 里的原始飞行员名，**永远保留**，用于审计与重新认领
    raw_pilot_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ACMI 的 CallSign（战术呼号），与飞行员名不同
    tactical_callsign: Mapped[Optional[str]] = mapped_column(String(32))

    aircraft_type_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("aircraft_types.id"))
    aircraft_raw_name: Mapped[Optional[str]] = mapped_column(String(64))
    coalition: Mapped[Optional[str]] = mapped_column(String(32))

    # ---- 时间（绝对 UTC，由 文件名时间 − t_max + 相对秒 换算）----
    takeoff_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    landing_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ---- 六类指标 ----
    flight_seconds: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    takeoff_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    landing_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    distance_meters: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    kills: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deaths: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crashed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ejected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weapons_fired: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exceedance_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    max_g: Mapped[Optional[float]] = mapped_column(Float)
    max_mach: Mapped[Optional[float]] = mapped_column(Float)
    max_cas_kts: Mapped[Optional[float]] = mapped_column(Float)
    end_cas_kts: Mapped[Optional[float]] = mapped_column(Float)
    end_altitude_m: Mapped[Optional[float]] = mapped_column(Float)
    max_altitude_m: Mapped[Optional[float]] = mapped_column(Float)
    speed_sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---- 来源与可信度（需求 §4.8 双源冲突消解 / §5.1）----
    data_source: Mapped[str] = mapped_column(String(16), default="acmi", nullable=False)
    data_confidence: Mapped[str] = mapped_column(
        String(16), default="exact", nullable=False)

    acmi_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("acmi_files.id"))
    edited_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    edit_note: Mapped[Optional[str]] = mapped_column(Text)

    mission: Mapped["Mission"] = relationship(back_populates="sorties")
    #: ⚠️ 必须显式指定 foreign_keys：``sortie_events`` 有**两条**指向 ``sorties``
    #: 的外键（``sortie_id`` 所属关系、``target_sortie_id`` 目标关系），
    #: SQLAlchemy 无法自动判定。此处只取"所属关系"。
    events: Mapped[list["SortieEvent"]] = relationship(
        back_populates="sortie",
        foreign_keys="SortieEvent.sortie_id",
    )

    __table_args__ = (
        Index("ix_sorties_mission", "mission_id"),
        Index("ix_sorties_member_mission", "member_id", "mission_id"),
        Index("ix_sorties_member", "member_id"),
        Index("ix_sorties_aircraft", "aircraft_type_id"),
        Index("ix_sorties_confidence", "data_confidence"),
        Index("ix_sorties_pilot_name", "raw_pilot_name"),
        CheckConstraint(
            "data_confidence IN ('exact','partial','estimated')",
            name="ck_sorties_confidence"),
    )


class SortieEvent(IdMixin, TimestampMixin, Base):
    """架次内事件流水（武器投放、击毁、超限等）。"""

    __tablename__ = "sortie_events"

    sortie_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sorties.id"), nullable=False)
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: ACMI 原始相对秒数，保留以便对回原件核对
    relative_seconds: Mapped[Optional[float]] = mapped_column(Float)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    weapon: Mapped[Optional[str]] = mapped_column(String(64))
    target_raw_name: Mapped[Optional[str]] = mapped_column(String(128))
    #: ⚠️ 跨文件配对，一期允许为空（设计文档 §3.4）
    target_sortie_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("sorties.id"))
    target_aircraft_type_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("aircraft_types.id"))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="acmi", nullable=False)

    sortie: Mapped["Sortie"] = relationship(
        back_populates="events", foreign_keys=[sortie_id])

    __table_args__ = (
        Index("ix_sortie_events_sortie_time", "sortie_id", "occurred_at"),
        Index("ix_sortie_events_type", "event_type"),
        Index("ix_sortie_events_type_time", "event_type", "occurred_at"),
    )


class UploadStatus(IdMixin, TimestampMixin, Base):
    """任务级上传状态追踪（R10 / 需求 §5.1）。"""

    __tablename__ = "upload_status"

    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id"), nullable=False)
    member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("members.id"), nullable=False)
    #: expected 应上传 / uploaded 已上传 / missing 未上传 / excused 已说明
    state: Mapped[str] = mapped_column(String(16), default="expected", nullable=False)
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    remind_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("mission_id", "member_id", name="uq_upload_status"),
        Index("ix_upload_status_state", "state"),
    )
