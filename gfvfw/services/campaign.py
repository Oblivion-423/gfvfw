"""战役存档上报与查询服务。

把上传的 ``.cam`` 存档解析成战役态势并入库，同时维护"与上一份存档相比
目标点易手"的变更记录（战役进程时间线的素材）。

存储策略见 :mod:`gfvfw.models.campaign_state` 的模块文档：
每份存档都留元数据/队伍状态/事件，但目标点与单位**只保留最新一份**，
旧的一份在同一次事务里换成变更记录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..campaign.state import CampaignState, build_state
from ..campaign.theater import TheaterData
from ..config import settings
from ..db import utcnow
from ..models.campaign_state import (
    CampaignEvent, CampaignObjective, CampaignObjectiveChange, CampaignSave,
    CampaignTeamState, CampaignUnit,
)
from ..models.flight import Campaign

log = logging.getLogger(__name__)

__all__ = [
    "CamStoreResult", "CampaignService", "theater_data",
    "resolve_campaign", "latest_save", "saves_for", "save_detail",
    "objective_changes", "campaign_overview", "OBJECTIVE_TYPE_ORDER",
]

#: 上传后落盘的子目录
CAM_SUBDIR = "campaign"

#: 目标点类型的展示顺序（大目标在前，杂项在后）
OBJECTIVE_TYPE_ORDER = (
    "Airbase", "Airstrip", "Army Base", "Headquarters", "Port",
    "Depot", "Refinery", "Factory", "Chemical Plant", "Nuclear Plant",
    "Power / Dam", "Bridge", "SAM / AAA Site", "SAM Site (Dedicated)",
    "Radar Site", "Nav Beacon", "Radio Tower", "Fortification",
    "Mountain Pass", "Border", "City", "Town", "Village", "Intersection",
    "Range", "Special", "Misc",
)


@dataclass
class CamStoreResult:
    """一次 ``.cam`` 入库的结果。"""

    save: CampaignSave
    state: Optional[CampaignState] = None
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)
    #: 本份存档相对上一份的目标点易手条数
    objective_changes: int = 0
    #: 被替换掉的上一份存档的目标点/单位行数
    replaced_objectives: int = 0
    replaced_units: int = 0

    @property
    def ok(self) -> bool:
        return self.save.parse_status == "parsed" and not self.duplicate


# --------------------------------------------------------------------------
# 剧场数据缓存
# --------------------------------------------------------------------------

_theater_cache: dict[tuple[str, str], TheaterData] = {}


def theater_data(theater: str, install: str | Path | None = None,
                 *, refresh: bool = False) -> TheaterData:
    """按 (安装目录, 剧场名) 缓存加载剧场数据。

    ``Falcon4_CT.xml`` 有 8~11 MB，每次上传都重解析会明显拖慢，故进程内缓存。
    """
    raw = install or settings.bms_install_path
    # ⚠️ 不能写 ``Path(install or settings... or "")`` —— 那会得到 ``Path('.')``，
    #    而 ``'.'` 是存在的，于是"未配置"会被误判成"配置了当前目录"。
    if not raw:
        raise FileNotFoundError(
            "未配置 Falcon BMS 安装目录（GFVFW_BMS_INSTALL_PATH），无法解析 .cam："
            "解析单位与目标点需要安装目录下的剧场数据表")
    inst = Path(raw)
    if not inst.is_dir():
        raise FileNotFoundError("Falcon BMS 安装目录不存在：%s" % inst)
    key = (str(inst), (theater or "").lower())
    if refresh or key not in _theater_cache:
        _theater_cache[key] = TheaterData.load(inst, theater)
    return _theater_cache[key]


def clear_theater_cache() -> None:
    _theater_cache.clear()


# --------------------------------------------------------------------------
# 战役归属
# --------------------------------------------------------------------------

def resolve_campaign(db: Session, state: CampaignState, *,
                     campaign_id: str | None = None,
                     create_if_missing: bool = True) -> Optional[Campaign]:
    """决定这份存档归到哪个战役。

    * 显式给了 ``campaign_id`` → 用它（不存在则报错）
    * 否则按 ``剧场 + 剧本`` 找已有战役
    * 再找不到就按 ``<剧场> <剧本>`` 新建一个（``status=active``）
    """
    if campaign_id:
        camp = db.get(Campaign, campaign_id)
        if camp is None or camp.deleted_at is not None:
            raise ValueError("战役不存在：%s" % campaign_id)
        return camp

    theater = state.theater or ""
    scenario = state.scenario or ""
    stmt = select(Campaign).where(Campaign.deleted_at.is_(None))
    if theater:
        stmt = stmt.where(Campaign.theater == theater)
    camp = db.scalars(stmt.order_by(Campaign.created_at)).first()
    if camp is not None:
        return camp

    if not create_if_missing:
        return None
    name = ("%s %s" % (theater, scenario)).strip() or "未命名战役"
    camp = Campaign(name=name, theater=theater or None, status="active",
                    visibility="members", summary="由上传的 BMS 存档自动建立")
    db.add(camp)
    db.flush()
    log.info("按存档自动新建战役：%s（%s）", name, camp.id)
    return camp


# --------------------------------------------------------------------------
# 上报
# --------------------------------------------------------------------------

class CampaignService:
    """``.cam`` 存档入库与替换。"""

    def __init__(self, storage_dir: Optional[Path] = None,
                 bms_install_path: Optional[Path | str] = None):
        self.storage_dir = Path(storage_dir or settings.storage_dir)
        self.bms_install_path = bms_install_path or settings.bms_install_path

    # -- 落盘 ------------------------------------------------------------

    def _park_path(self, sha256: str, filename: str) -> Path:
        """按 ``campaign/<年月>/<sha256 前 16>__<原名>`` 落盘。"""
        sub = self.storage_dir / CAM_SUBDIR / utcnow().strftime("%Y-%m")
        sub.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)
        return sub / ("%s__%s" % (sha256[:16], safe.strip()))

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    # -- 入库 ------------------------------------------------------------

    def ingest(self, db: Session, source_path: str | Path,
               original_filename: Optional[str] = None,
               uploaded_by: Optional[str] = None,
               campaign_id: Optional[str] = None) -> CamStoreResult:
        """解析并入库一份 ``.cam``。同一 SHA256 重复上传直接返回既有记录。"""
        src = Path(source_path)
        filename = original_filename or src.name
        sha256, size = self._hash_file(src)

        existing = db.scalar(select(CampaignSave).where(CampaignSave.sha256 == sha256))
        if existing is not None:
            log.info("战役存档已存在（SHA256 命中），跳过：%s", filename)
            return CamStoreResult(save=existing, duplicate=True,
                                  warnings=["此存档已存在，未重复入库"])

        if size > settings.max_cam_bytes:
            raise ValueError("存档超过上限 %d 字节：%d" % (settings.max_cam_bytes, size))

        # 先落盘（保留原始文件便于重新解析）
        dest = self._park_path(sha256, filename)
        if settings.keep_cam_files and not dest.exists():
            shutil.copy2(src, dest)

        save = CampaignSave(
            sha256=sha256,
            original_filename=filename,
            stored_path=str(dest.relative_to(self.storage_dir)).replace("\\", "/"),
            size_bytes=size,
            uploaded_by=uploaded_by,
            parse_status="parsing",
        )
        db.add(save)
        db.flush()

        result = CamStoreResult(save=save)
        try:
            state = self._parse(src)
        except Exception as exc:  # noqa: BLE001 - 解析失败也要留记录
            log.warning("战役存档解析失败：%s（%s）", filename, exc)
            # ⚠️ 这里**不能**改成 db.rollback()：
            #    save 这条"解析失败"记录正是我们要留下的东西，回滚会把它删掉。
            #    _parse() 只读文件、不碰 Session，所以此刻会话仍是干净的，
            #    commit 是安全的。若将来 _parse() 开始写库，必须先 rollback
            #    再重新 db.add(save)，否则 commit 会抛 PendingRollbackError，
            #    真正的解析错误反而被它盖掉。
            save.parse_status = "failed"
            save.parse_error = str(exc)[:4000]
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("写入解析失败记录时又失败：%s", filename)
                raise
            result.warnings.append("解析失败：%s" % exc)
            return result

        result.state = state
        result.warnings = list(state.warnings)
        self._apply_state(db, save, state, campaign_id, result)
        db.commit()
        log.info("战役存档入库：%s → %s（%d 目标点 / %d 单位 / %d 易手）",
                 filename, save.id, len(state.objectives), len(state.units),
                 result.objective_changes)
        return result

    def _parse(self, path: Path) -> CampaignState:
        """解析 ``.cam``；剧场名先从 ``.cmp`` 读出来再加载对应剧场数据。

        剧场数据（``Falcon4_CT.xml`` 等）是解析 ``.uni`` 单位流与目标点的**前提**：
        没有它就只剩一个空壳。因此这里**宁可明确失败**，也不落一份
        "状态=parsed 但 0 单位 0 目标点"的误导性存档。
        """
        from ..campaign.bundle import Bundle
        from ..campaign.cmpfile import read_cmp

        b = Bundle.load(path)
        cmp_raw = b.get_by_ext(".cmp")
        if cmp_raw is None:
            raise ValueError(".cam 里没有 .cmp，文件可能损坏")
        theater_name = read_cmp(cmp_raw, b.version).theater_name
        if not theater_name:
            raise ValueError(
                ".cmp 里读不出剧场名，无法加载剧场数据（BMS 安装目录：%s）"
                % (self.bms_install_path or "未配置"))
        th = theater_data(theater_name, self.bms_install_path)
        miss = list(getattr(th, "missing", None) or [])
        if miss:
            log.warning("剧场 %s 缺少部分数据表：%s", theater_name, miss)
        return build_state(path, th)

    def _apply_state(self, db: Session, save: CampaignSave, st: CampaignState,
                     campaign_id: Optional[str], result: CamStoreResult) -> None:
        camp = resolve_campaign(db, st, campaign_id=campaign_id)
        if camp is not None:
            save.campaign_id = camp.id

        # ── 头部 ──────────────────────────────────────────────────────
        save.cam_version = st.cam_version
        save.theater = st.theater
        save.scenario = st.scenario
        save.save_name = st.save_name
        save.ui_name = st.ui_name
        save.campaign_time_ms = st.campaign_time_ms
        save.campaign_time_label = st.campaign_time_label
        save.campaign_day = st.campaign_day
        save.day_zero = st.day_zero
        save.active_teams = st.active_teams
        save.endgame_result = st.endgame_result
        save.situation = st.situation
        save.tempo = st.tempo
        save.enemy_air_exp = st.enemy_air_exp
        save.enemy_ad_exp = st.enemy_ad_exp
        save.bullseye_x = st.bullseye_east
        save.bullseye_y = st.bullseye_north
        save.theater_size_x = st.theater_size_x
        save.theater_size_y = st.theater_size_y
        save.te_start_time = st.te_start_time
        save.te_time_limit = st.te_time_limit
        save.te_victory_pts = st.te_victory_pts
        save.te_num_teams = st.te_num_teams
        save.ground_ratio = st.ground_ratio
        save.air_ratio = st.air_ratio
        save.air_def_ratio = st.air_def_ratio
        save.naval_ratio = st.naval_ratio
        save.squadron_count = len(st.squadrons)
        save.package_count = st.count_of("Package")
        save.flight_count = st.count_of("Flight")
        save.ground_unit_count = (st.count_of("Battalion") + st.count_of("Brigade")
                                  + st.count_of("Division"))
        save.naval_unit_count = st.count_of("TaskForce")
        save.objective_count = len(st.objectives)
        save.event_count = len(st.events)
        owned = st.owned_by_team
        save.owned_by_team_json = json.dumps(
            {str(k): v for k, v in sorted(owned.items())}, ensure_ascii=False)
        save.parse_status = "parsed"
        save.parse_error = None
        save.parse_warnings_json = json.dumps(st.warnings[:200], ensure_ascii=False)
        save.sections_json = json.dumps(st.sections, ensure_ascii=False)

        # ── 队伍状态 ──────────────────────────────────────────────────
        for t in st.teams:
            db.add(CampaignTeamState(
                save_id=save.id, team_id=t.team_id, name=t.name, motto=t.motto,
                active=t.active, flag=t.flag, color=t.color,
                equipment=t.equipment, initiative=t.initiative,
                reinforcement=t.reinforcement, player_rating=t.player_rating,
                offensive_loss=t.offensive_loss, attack_time=t.attack_time,
                exp_air=t.exp_air, exp_air_def=t.exp_air_def,
                exp_ground=t.exp_ground, exp_naval=t.exp_naval,
                supply=t.supply, fuel=t.fuel, replacements=t.replacements,
                st_aircraft=t.st_aircraft, st_air_def=t.st_air_def,
                st_ground=t.st_ground, st_ships=t.st_ships, st_bases=t.st_bases,
                supply_lvl=t.supply_lvl, fuel_lvl=t.fuel_lvl,
                start_aircraft=t.start_aircraft, start_air_def=t.start_air_def,
                start_ground=t.start_ground, start_ships=t.start_ships,
                start_bases=t.start_bases,
                owned_objectives=t.owned_objectives,
                stances_json=json.dumps(t.stances),
            ))

        # ── 事件 ──────────────────────────────────────────────────────
        for e in st.events:
            db.add(CampaignEvent(
                save_id=save.id, kind=e.kind, at_campaign_time_ms=e.at_ms,
                at_campaign_time_label=e.label, team_id=e.team_id,
                grid_x=e.east, grid_y=e.north, flags=e.flags, text=e.text))

        # ── 目标点：先与上一份对比，再替换 ────────────────────────────
        prev = self._previous_save(db, save)
        prev_objs: dict[int, CampaignObjective] = {}
        if prev is not None:
            prev_objs = {o.camp_id: o for o in db.scalars(
                select(CampaignObjective).where(CampaignObjective.save_id == prev.id))}

        changes = 0
        for o in st.objectives:
            po = prev_objs.get(o.camp_id)
            if po is not None and po.team_id != o.team_id:
                db.add(CampaignObjectiveChange(
                    campaign_id=save.campaign_id, from_save_id=prev.id,
                    to_save_id=save.id, camp_id=o.camp_id,
                    objective_name=o.name, type_name=o.type_name,
                    from_team=po.team_id, to_team=o.team_id,
                    at_campaign_time_ms=save.campaign_time_ms,
                    at_campaign_time_label=save.campaign_time_label))
                changes += 1
        result.objective_changes = changes

        for o in st.objectives:
            db.add(CampaignObjective(
                save_id=save.id, camp_id=o.camp_id, name=o.name,
                type_id=o.type_id, type_name=o.type_name, team_id=o.team_id,
                first_owner=o.first_owner, priority=o.priority,
                supply=o.supply, fuel=o.fuel, losses=o.losses,
                grid_x=o.east, grid_y=o.north,
                feature_count=o.feature_count,
                damaged_features=o.damaged_features))

        # ── 单位：只留最新一份 ────────────────────────────────────────
        # .uni 里的 Squadron 记录信息较薄，改用 .cmp SquadInfo 派生的
        # SquadronRec（有基地名/兵力/战果），避免同一张表里出现两套中队。
        for u in st.units:
            if u.unit_kind == "Squadron":
                continue
            db.add(CampaignUnit(
                save_id=save.id, unit_kind=u.unit_kind, unit_id=u.unit_id,
                id_creator=u.id_creator, entity_type_id=u.entity_type_id,
                team_id=u.team_id, name_id=u.name_id, name=u.name,
                callsign=u.callsign, grid_x=u.east, grid_y=u.north, z=u.z,
                dest_x=u.dest_east, dest_y=u.dest_north,
                aircraft_type=u.aircraft_type, mission_code=u.mission_code,
                mission_name=u.mission_name, current_wp=u.current_wp,
                total_wp=u.total_wp, tot_ms=u.tot_ms,
                package_unit_id=u.package_unit_id,
                squadron_unit_id=u.squadron_unit_id,
                supply=u.supply, morale=u.morale, fatigue=u.fatigue,
                heading=u.heading, orders=u.orders, division=u.division,
                parent_unit_id=u.parent_unit_id,
                vehicles_json=json.dumps(u.vehicles, ensure_ascii=False) if u.vehicles else None,
                losses=u.losses, moved=u.moved, tactic=u.tactic,
                roster=u.roster, unit_flags=u.unit_flags, spotted=u.spotted,
                spotted_by_json=json.dumps(u.spotted_by),
                extra_json=_safe_json(u.extra)))

        for sq in st.squadrons:
            extra = {
                "airbase_name": sq.airbase,
                "airbase_camp_id": sq.airbase_camp_id,
                "strength_pct": sq.strength_pct,
                "specialty": sq.specialty,
                "camp_id": sq.camp_id,
                "tex_set": sq.tex_set,
                "has_uni": sq.has_uni,
                "aa_kills": sq.aa_kills, "ag_kills": sq.ag_kills,
                "as_kills": sq.as_kills, "an_kills": sq.an_kills,
                "missions_flown": sq.missions_flown,
                "mission_score": sq.mission_score,
                "total_losses": sq.total_losses,
                "pilot_losses": sq.pilot_losses,
                "fuel": sq.fuel,
            }
            db.add(CampaignUnit(
                save_id=save.id, unit_kind="Squadron", unit_id=sq.squadron_id,
                team_id=sq.team_id, name=sq.name,
                grid_x=sq.airbase_east, grid_y=sq.airbase_north,
                losses=sq.total_losses or 0,
                extra_json=_safe_json(extra)))

        # ── 丢弃上一份的明细（历史留在"易手记录"里）──────────────────
        if prev is not None:
            result.replaced_objectives = db.execute(
                delete(CampaignObjective)
                .where(CampaignObjective.save_id == prev.id)).rowcount or 0
            result.replaced_units = db.execute(
                delete(CampaignUnit)
                .where(CampaignUnit.save_id == prev.id)).rowcount or 0

    def _previous_save(self, db: Session, save: CampaignSave) -> Optional[CampaignSave]:
        """同一战役里**战役内时刻**早于本份的最近一份存档。"""
        if not save.campaign_id:
            return None
        return db.scalars(
            select(CampaignSave)
            .where(CampaignSave.campaign_id == save.campaign_id,
                   CampaignSave.id != save.id,
                   CampaignSave.parse_status == "parsed",
                   CampaignSave.campaign_time_ms < save.campaign_time_ms)
            .order_by(CampaignSave.campaign_time_ms.desc())
            .limit(1)).first()


def _jsonable(obj: Any, depth: int = 0) -> Any:
    """把解析结果收敛成**严格合法**的 JSON 值。

    ``json.dumps`` 默认 ``allow_nan=True``，会把 NaN/Infinity 写成裸 ``NaN``
    / ``Infinity`` 记号 —— Python 自己读得回来，但那不是合法 JSON，
    浏览器里的 ``JSON.parse`` 会直接抛错。.cam 里未初始化的 0xFF 槽位
    恰好最容易产出 NaN，所以这里统一换成 ``null``。
    """
    if depth > 12:
        return "…"
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in obj]
    return obj


def _safe_json(obj: Any, limit: int = 20000) -> Optional[str]:
    """尽量序列化；超大或不可序列化时退化成摘要，避免拖垮入库。"""
    if not obj:
        return None
    try:
        s = json.dumps(_jsonable(obj), ensure_ascii=False, default=str,
                       allow_nan=False)
    except Exception:  # noqa: BLE001
        return None
    if len(s) > limit:
        return json.dumps({"_truncated": True, "size": len(s)}, ensure_ascii=False)
    return s


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def saves_for(db: Session, campaign_id: str, *, limit: int = 200,
              include_failed: bool = True) -> list[CampaignSave]:
    """某战役的存档，按战役内时刻排序（旧 → 新）。"""
    stmt = select(CampaignSave).where(CampaignSave.campaign_id == campaign_id)
    if not include_failed:
        stmt = stmt.where(CampaignSave.parse_status == "parsed")
    return list(db.scalars(stmt.order_by(CampaignSave.campaign_time_ms)
                           .limit(limit)))


def latest_save(db: Session, campaign_id: str) -> Optional[CampaignSave]:
    """某战役最新一份可用存档。"""
    return db.scalars(
        select(CampaignSave)
        .where(CampaignSave.campaign_id == campaign_id,
               CampaignSave.parse_status == "parsed")
        .order_by(CampaignSave.campaign_time_ms.desc())
        .limit(1)).first()


def save_detail(db: Session, save_id: str) -> Optional[CampaignSave]:
    return db.scalar(
        select(CampaignSave)
        .options(selectinload(CampaignSave.team_states))
        .where(CampaignSave.id == save_id))


def objective_changes(db: Session, campaign_id: str, *,
                      limit: int = 200) -> list[CampaignObjectiveChange]:
    """目标点易手记录，最新在前。"""
    return list(db.scalars(
        select(CampaignObjectiveChange)
        .where(CampaignObjectiveChange.campaign_id == campaign_id)
        .order_by(CampaignObjectiveChange.at_campaign_time_ms.desc())
        .limit(limit)))


@dataclass
class CampaignOverview:
    """战役总览（页面用）。"""

    campaign: Campaign
    save: Optional[CampaignSave]
    save_count: int
    teams: list[CampaignTeamState] = field(default_factory=list)
    last_changes: list[CampaignObjectiveChange] = field(default_factory=list)
    change_count: int = 0
    objective_types: list[tuple[str, int]] = field(default_factory=list)
    units_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return self.save is not None


def campaign_overview(db: Session, campaign: Campaign) -> CampaignOverview:
    """组装战役总览：最新存档 + 队伍状态 + 类型分布 + 近期易手。"""
    sv = latest_save(db, campaign.id)
    o = CampaignOverview(campaign=campaign, save=sv, save_count=0)
    o.save_count = db.scalar(
        select(func.count()).select_from(CampaignSave)
        .where(CampaignSave.campaign_id == campaign.id,
               CampaignSave.parse_status == "parsed")) or 0
    if sv is None:
        return o

    o.teams = list(db.scalars(
        select(CampaignTeamState)
        .where(CampaignTeamState.save_id == sv.id)
        .order_by(CampaignTeamState.team_id)))

    rows = db.execute(
        select(CampaignObjective.type_name, func.count())
        .where(CampaignObjective.save_id == sv.id)
        .group_by(CampaignObjective.type_name)).all()
    counted = {t: n for t, n in rows}
    # 按固定顺序输出，未出现在数据里的类型不显示
    o.objective_types = [(t, counted[t]) for t in OBJECTIVE_TYPE_ORDER if t in counted]
    o.objective_types += [(t, n) for t, n in sorted(counted.items())
                          if t not in OBJECTIVE_TYPE_ORDER]

    o.units_by_kind = dict(db.execute(
        select(CampaignUnit.unit_kind, func.count())
        .where(CampaignUnit.save_id == sv.id)
        .group_by(CampaignUnit.unit_kind)).all())

    o.change_count = db.scalar(
        select(func.count()).select_from(CampaignObjectiveChange)
        .where(CampaignObjectiveChange.campaign_id == campaign.id)) or 0
    o.last_changes = objective_changes(db, campaign.id, limit=12)
    return o
