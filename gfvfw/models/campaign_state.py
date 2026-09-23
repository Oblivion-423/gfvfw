"""
BMS 战役态势数据层：``.cam`` 存档快照 → 队伍状态 → 目标点/单位/事件。

对应需求「战役管理」与 docs/database-design.md 的战役解析一节。

存储策略（重要）
----------------
一次上传 = 一个**存档快照**。165 个真实存档 × 6944 个目标点 ≈ 110 万行，
对小型 VPS 上的 SQLite 过重，因此：

* :class:`CampaignSave` / :class:`CampaignTeamState` / :class:`CampaignEvent`
  —— **每次存档都留**（行数很小），构成战役进程时间线的骨架。
* :class:`CampaignObjective` / :class:`CampaignUnit`
  —— **只保留最新一次存档**的行。新存档入库时先与旧行对拍，把易手情况写入
  :class:`CampaignObjectiveChange`，再整体替换。历史归属由"变更表"重建，
  不保留逐次全量快照。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .identity import IdMixin, TimestampMixin

#: 解析状态
SAVE_PARSE_STATUS = ("parsed", "failed")

#: 统一后的单位种类（把 .uni 的 8 种读取器归一到一张表）
UNIT_KINDS = (
    "Flight", "Package", "Squadron",
    "Battalion", "Brigade", "Division", "TaskForce",
    "Objective", "Feature", "CampBase",
)

#: 队伍间关系（.cmp stances）
STANCE_NAMES = ("Allied", "Friendly", "Neutral", "Hostile", "War", "Team",
                "Unknown", "Hostile")


class CampaignSave(IdMixin, TimestampMixin, Base):
    """一次上传的 ``.cam`` 战役存档。"""

    __tablename__ = "campaign_saves"

    campaign_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("campaigns.id"))
    #: 内容寻址去重：同一文件重复上传直接返回既有记录
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # ── .cam 容器与 .cmp 头部 ────────────────────────────────────────
    cam_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    theater: Mapped[Optional[str]] = mapped_column(String(64))
    scenario: Mapped[Optional[str]] = mapped_column(String(64))
    save_name: Mapped[Optional[str]] = mapped_column(String(128))
    ui_name: Mapped[Optional[str]] = mapped_column(String(128))

    #: 战役内时间（.cmp currentTime，毫秒）；**不是**真实世界时间
    campaign_time_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 形如 "Day 3  02:00:48"
    campaign_time_label: Mapped[Optional[str]] = mapped_column(String(32))
    campaign_day: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    day_zero: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_teams: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 0=未结束，非 0 表示战役已出结果
    endgame_result: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    situation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tempo: Mapped[Optional[int]] = mapped_column(Integer)
    enemy_air_exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enemy_ad_exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    bullseye_x: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bullseye_y: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    theater_size_x: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    theater_size_y: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)

    #: TE（战术交战）参数
    te_start_time: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    te_time_limit: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    te_victory_pts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    te_num_teams: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 兵力比（万分比，直接用 .cmp 的 i16）
    ground_ratio: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    air_ratio: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    air_def_ratio: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    naval_ratio: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── 解析出的实体计数 ──────────────────────────────────────────────
    squadron_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    package_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flight_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ground_unit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    naval_unit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    objective_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 各队伍占有的目标点数，JSON 数组（索引=队伍号）
    owned_by_team_json: Mapped[Optional[str]] = mapped_column(Text)

    # ── 解析状态 ──────────────────────────────────────────────────────
    parse_status: Mapped[str] = mapped_column(
        String(16), default="parsed", nullable=False)
    parse_error: Mapped[Optional[str]] = mapped_column(Text)
    #: 解析告警（错位恢复、跳过条数、未知表项等），JSON 数组
    parse_warnings_json: Mapped[Optional[str]] = mapped_column(Text)
    #: .cmp 之外的辅助文件是否解出（.uni/.tea/...），JSON 对象
    sections_json: Mapped[Optional[str]] = mapped_column(Text)

    uploaded_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"))

    team_states: Mapped[list["CampaignTeamState"]] = relationship(
        back_populates="save", cascade="all, delete-orphan")
    events: Mapped[list["CampaignEvent"]] = relationship(
        back_populates="save", cascade="all, delete-orphan")
    objectives: Mapped[list["CampaignObjective"]] = relationship(
        back_populates="save", cascade="all, delete-orphan")
    units: Mapped[list["CampaignUnit"]] = relationship(
        back_populates="save", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_campaign_saves_campaign", "campaign_id"),
        Index("ix_campaign_saves_time", "campaign_time_ms"),
    )


class CampaignTeamState(IdMixin, TimestampMixin, Base):
    """某次存档中某个队伍的状态（每存档 8 行）。"""

    __tablename__ = "campaign_team_states"

    save_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[Optional[str]] = mapped_column(String(64))
    motto: Mapped[Optional[str]] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    color: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    equipment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    initiative: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reinforcement: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    player_rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    offensive_loss: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attack_time: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    #: 经验（0-100）
    exp_air: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exp_air_def: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exp_ground: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exp_naval: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 资源
    supply: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fuel: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replacements: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 当前兵力
    st_aircraft: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    st_air_def: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    st_ground: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    st_ships: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    st_bases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    supply_lvl: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fuel_lvl: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 起始兵力（用于对比消耗）
    start_aircraft: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_air_def: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_ground: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_ships: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_bases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 本队伍占有的目标点数（入库时按 objectives 统计写入，便于时间线）
    owned_objectives: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 与其他队伍的关系，JSON 数组（索引=对方队伍号，值为 STANCE_NAMES 下标）
    stances_json: Mapped[Optional[str]] = mapped_column(Text)

    save: Mapped[CampaignSave] = relationship(back_populates="team_states")

    __table_args__ = (
        UniqueConstraint("save_id", "team_id", name="uq_team_state_save_team"),
        Index("ix_team_states_team", "team_id"),
    )


class CampaignObjective(IdMixin, TimestampMixin, Base):
    """目标点（基地、城市、SAM 阵地……）——**只保留最新存档**的行。"""

    __tablename__ = "campaign_objectives"

    save_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"), nullable=False)

    #: BMS 战役对象号，跨存档稳定，用于识别同一目标点
    camp_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(128))
    #: OCD 目标类型索引
    type_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 目标类型名，如 "Airbase"、"SAM / AAA Site"
    type_name: Mapped[Optional[str]] = mapped_column(String(48))

    #: 占有方队伍号；-1 = 中立/无主
    team_id: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    first_owner: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: -1 表示该目标点未启用补给/燃油概念
    supply: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    fuel: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 战役网格坐标（0..1023，x=东、y=北，原点在西南角）
    grid_x: Mapped[Optional[float]] = mapped_column(Float)
    grid_y: Mapped[Optional[float]] = mapped_column(Float)

    feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    damaged_features: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    save: Mapped[CampaignSave] = relationship(back_populates="objectives")

    __table_args__ = (
        Index("ix_campaign_obj_save", "save_id"),
        Index("ix_campaign_obj_type", "type_name"),
        Index("ix_campaign_obj_team", "team_id"),
        Index("ix_campaign_obj_campid", "camp_id"),
    )


class CampaignObjectiveChange(IdMixin, TimestampMixin, Base):
    """目标点易手记录（相邻两次存档之间）。战役进程时间线的主要素材。"""

    __tablename__ = "campaign_objective_changes"

    campaign_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("campaigns.id"))
    from_save_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"))
    to_save_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"), nullable=False)

    camp_id: Mapped[int] = mapped_column(Integer, nullable=False)
    objective_name: Mapped[Optional[str]] = mapped_column(String(128))
    type_name: Mapped[Optional[str]] = mapped_column(String(48))

    from_team: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    to_team: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)
    #: 变更时所在存档的战役内时刻
    at_campaign_time_ms: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    at_campaign_time_label: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_obj_change_campaign", "campaign_id"),
        Index("ix_obj_change_to_save", "to_save_id"),
    )


class CampaignUnit(IdMixin, TimestampMixin, Base):
    """战役单位（飞行/编队/中队/营/旅/师/特混舰队）——**只保留最新存档**的行。"""

    __tablename__ = "campaign_units"

    save_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"), nullable=False)

    #: UNIT_KINDS 之一
    unit_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: .uni 里的 VU 编号（同一存档内唯一，跨存档通常稳定）
    unit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    id_creator: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entity_type_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 占有方队伍号
    team_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    name_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 由 name_id 或机号/呼号解析出的可读名
    name: Mapped[Optional[str]] = mapped_column(String(128))
    callsign: Mapped[Optional[str]] = mapped_column(String(64))

    #: 战役网格坐标
    grid_x: Mapped[Optional[float]] = mapped_column(Float)
    grid_y: Mapped[Optional[float]] = mapped_column(Float)
    z: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dest_x: Mapped[Optional[int]] = mapped_column(Integer)
    dest_y: Mapped[Optional[int]] = mapped_column(Integer)

    #: 飞行/编队专有
    aircraft_type: Mapped[Optional[str]] = mapped_column(String(64))
    mission_code: Mapped[Optional[int]] = mapped_column(Integer)
    mission_name: Mapped[Optional[str]] = mapped_column(String(48))
    current_wp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_wp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tot_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    package_unit_id: Mapped[Optional[int]] = mapped_column(Integer)
    squadron_unit_id: Mapped[Optional[int]] = mapped_column(Integer)

    #: 地面/海军专有
    supply: Mapped[Optional[int]] = mapped_column(Integer)
    morale: Mapped[Optional[int]] = mapped_column(Integer)
    fatigue: Mapped[Optional[int]] = mapped_column(Integer)
    heading: Mapped[Optional[int]] = mapped_column(Integer)
    orders: Mapped[Optional[int]] = mapped_column(Integer)
    division: Mapped[Optional[int]] = mapped_column(Integer)
    parent_unit_id: Mapped[Optional[int]] = mapped_column(Integer)
    vehicles_json: Mapped[Optional[str]] = mapped_column(Text)

    #: 通用战斗/状态
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    moved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tactic: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    roster: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_flags: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    spotted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spotted_by_json: Mapped[Optional[str]] = mapped_column(Text)

    #: 类型特有字段全量留存，避免为每种单位继续加列
    extra_json: Mapped[Optional[str]] = mapped_column(Text)

    save: Mapped[CampaignSave] = relationship(back_populates="units")

    __table_args__ = (
        Index("ix_campaign_units_save_kind", "save_id", "unit_kind"),
        Index("ix_campaign_units_team", "team_id"),
    )


class CampaignEvent(IdMixin, TimestampMixin, Base):
    """战役情报事件（.cmp 的 recentEvents / priorityEvents）。"""

    __tablename__ = "campaign_events"

    save_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_saves.id"), nullable=False)

    #: recent / priority
    kind: Mapped[str] = mapped_column(String(16), default="recent", nullable=False)
    at_campaign_time_ms: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    at_campaign_time_label: Mapped[Optional[str]] = mapped_column(String(32))
    team_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grid_x: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grid_y: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flags: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text)

    save: Mapped[CampaignSave] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_campaign_events_save", "save_id"),
        Index("ix_campaign_events_time", "at_campaign_time_ms"),
    )
