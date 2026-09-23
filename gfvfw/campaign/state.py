"""组装完整战役态势。

把 :mod:`gfvfw.campaign.bundle` / :mod:`cmpfile` / :mod:`units` /
:mod:`camdata` 与剧场数据（:mod:`gfvfw.campaign.theater`）合成一个
:class:`CampaignState`，供上报管线入库与页面渲染。

与 CamReader 的 ``campaign_state.json`` 的关系
----------------------------------------------
本模块不产出 ``campaign_state.json``；它产出面向本项目的结构。字段语义、
优先级、坐标口径均与 CamReader 对齐，并由开发期探针
``scripts/cam_state_probe.py`` 与参考 JSON 逐项对拍。

目标点状态的优先级（照搬 ``JsonExporter.cs`` 第 829-842 行）
------------------------------------------------------------
::

    .obd 增量  >  .obj 记录  >  .uni Objective 记录  >  无（owner=-1, supply=-1）

已知与 CamReader 的两处**有意差异**
-----------------------------------
1. 中队坐标：CamReader 直接把世界英尺当网格输出（``JsonExporter`` 第 289 行），
   本实现按 :mod:`gfvfw.campaign.coords` 正确换算成网格，否则中队长会画到图外。
2. 目标点 ``typeName``：CamReader 走 ``OcdTypeTable`` 两步映射；本实现同样两步，
   但把映射失败的目标点记为哨兵名（``"Type-1"`` / ``"Unknown"``）并在警告里列出，
   而不是静默给一个错名。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .bundle import Bundle
from .cmpfile import CmpData, read_cmp
from .coords import clamp_grid, world_xy_to_grid
from .units import Unit, read_units

__all__ = [
    "TeamState", "ObjectiveRec", "UnitRec", "SquadronRec", "EventRec",
    "CampaignState", "camp_time_label", "build_state",
]

#: 无主的标记（BMS 用 -1 表示目标点没有任何占有方）
NO_OWNER = -1

#: 队伍关系名（照搬 ``JsonExporter.StanceName``）
STANCE_NAME = {0: "Hostile", 1: "Allied", 2: "Friendly", 3: "Neutral",
               4: "Unfriendly", 5: "AtWar"}


def stance_name(value: int) -> str:
    """队伍关系号 → 名称（未知值形如 ``Unknown(7)``）。"""
    return STANCE_NAME.get(value, "Unknown(%d)" % value)


def camp_time_label(ms: int) -> str:
    """战役内毫秒 → ``"Day 3  02:00:48"``（照搬 ``R.CampTime``）。

    注意 ``Day`` 后是**两个空格**，且 0 与 0xFFFFFFFF 视为无时间。
    """
    if ms in (0, 0xFFFFFFFF):
        return "(none)"
    h = ms // 3600000
    day = h // 24 + 1
    h %= 24
    m = ms % 3600000 // 60000
    s = ms % 60000 // 1000
    return "Day %d  %02d:%02d:%02d" % (day, h, m, s)


# ── 结构 ──────────────────────────────────────────────────────────────

@dataclass
class TeamState:
    """一个队伍在某次存档时的状态。"""

    team_id: int = 0
    name: str = ""
    motto: str = ""
    active: bool = False
    flag: int = 0
    color: int = 0

    equipment: int = 0
    initiative: int = 0
    reinforcement: int = 0
    player_rating: float = 0.0
    offensive_loss: int = 0
    attack_time: int = 0

    exp_air: int = 0
    exp_air_def: int = 0
    exp_ground: int = 0
    exp_naval: int = 0

    supply: int = 0
    fuel: int = 0
    replacements: int = 0

    st_aircraft: int = 0
    st_air_def: int = 0
    st_ground: int = 0
    st_ships: int = 0
    st_bases: int = 0
    supply_lvl: int = 0
    fuel_lvl: int = 0

    start_aircraft: int = 0
    start_air_def: int = 0
    start_ground: int = 0
    start_ships: int = 0
    start_bases: int = 0

    stances: list[int] = field(default_factory=list)
    #: 本队伍占有多少目标点（由 :func:`build_state` 统计填入）
    owned_objectives: int = 0


@dataclass
class ObjectiveRec:
    """一个战役目标点。"""

    camp_id: int = 0
    name: str = ""
    #: OCD 两步映射后的类型号
    type_id: int = 0
    type_name: str = "Unknown"
    ocd_index: int = 0
    team_id: int = NO_OWNER
    first_owner: int = 0
    priority: int = 0
    supply: int = -1
    fuel: int = -1
    losses: int = 0
    #: 网格坐标（东、北）
    east: Optional[float] = None
    north: Optional[float] = None
    heading: float = 0.0
    feature_count: int = 0
    damaged_features: int = 0
    #: 状态来源：obd / obj / uni / none
    source: str = "none"


@dataclass
class UnitRec:
    """战役单位（编队/飞行/营/旅/师/特混舰队）。"""

    unit_kind: str = ""
    unit_id: int = 0
    id_creator: int = 0
    entity_type_id: int = 0
    team_id: int = 0
    name_id: int = 0
    name: str = ""
    callsign: str = ""
    #: 网格坐标（东、北）；None 表示原记录坐标越界、不可上图
    east: Optional[float] = None
    north: Optional[float] = None
    z: float = 0.0
    dest_east: Optional[int] = None
    dest_north: Optional[int] = None
    on_map: bool = True

    aircraft_type: str = ""
    mission_code: Optional[int] = None
    mission_name: str = ""
    current_wp: int = 0
    total_wp: int = 0
    tot_ms: Optional[int] = None
    package_unit_id: Optional[int] = None
    squadron_unit_id: Optional[int] = None

    supply: Optional[int] = None
    morale: Optional[int] = None
    fatigue: Optional[int] = None
    heading: Optional[int] = None
    orders: Optional[int] = None
    division: Optional[int] = None
    parent_unit_id: Optional[int] = None
    vehicles: list[str] = field(default_factory=list)

    losses: int = 0
    moved: int = 0
    tactic: int = 0
    roster: int = 0
    unit_flags: int = 0
    spotted: int = 0
    spotted_by: list[int] = field(default_factory=list)

    #: 类型特有字段（武器挂载、雷达、航路点……）
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SquadronRec:
    """中队（来自 ``.cmp`` 的 SquadInfo，用 ``.uni`` 补充战果）。"""

    squadron_id: int = 0
    name: str = ""
    airbase: str = ""
    airbase_camp_id: Optional[int] = None
    airbase_east: Optional[float] = None
    airbase_north: Optional[float] = None
    team_id: int = 0
    strength_pct: int = 0
    specialty: int = 0
    camp_id: int = 0
    tex_set: int = 0
    #: .uni 补充
    fuel: Optional[int] = None
    aa_kills: Optional[int] = None
    ag_kills: Optional[int] = None
    as_kills: Optional[int] = None
    an_kills: Optional[int] = None
    missions_flown: Optional[int] = None
    mission_score: Optional[int] = None
    total_losses: Optional[int] = None
    pilot_losses: Optional[int] = None
    has_uni: bool = False


@dataclass
class EventRec:
    """战役情报事件。"""

    kind: str = "recent"
    at_ms: int = 0
    label: str = ""
    team_id: int = 0
    #: 网格坐标（东、北）
    east: int = 0
    north: int = 0
    flags: int = 0
    text: str = ""


@dataclass
class CampaignState:
    """一次存档解出的全部战役态势。"""

    # ── 容器与头部 ────────────────────────────────────────────────────
    cam_version: int = 0
    theater: str = ""
    scenario: str = ""
    save_name: str = ""
    ui_name: str = ""

    campaign_time_ms: int = 0
    campaign_time_label: str = "(none)"
    campaign_day: int = 0
    day_zero: int = 0
    active_teams: int = 0
    endgame_result: int = 0
    situation: int = 0
    tempo: Optional[int] = None
    enemy_air_exp: int = 0
    enemy_ad_exp: int = 0

    bullseye_east: int = 0
    bullseye_north: int = 0
    theater_size_x: int = 1024
    theater_size_y: int = 1024

    te_start_time: int = 0
    te_time_limit: int = 0
    te_victory_pts: int = 0
    te_num_teams: int = 0

    ground_ratio: int = 0
    air_ratio: int = 0
    air_def_ratio: int = 0
    naval_ratio: int = 0

    # ── 态势内容 ──────────────────────────────────────────────────────
    teams: list[TeamState] = field(default_factory=list)
    objectives: list[ObjectiveRec] = field(default_factory=list)
    units: list[UnitRec] = field(default_factory=list)
    squadrons: list[SquadronRec] = field(default_factory=list)
    events: list[EventRec] = field(default_factory=list)
    #: SAM 系统名 → 交战半径（海里）
    sam_threat: dict[str, float] = field(default_factory=dict)

    #: 投影参数（供经纬度换算）
    projection: dict[str, Any] = field(default_factory=dict)

    # ── 诊断 ──────────────────────────────────────────────────────────
    #: 各内嵌文件是否解出：{"cmp": True, "uni": True, ...}
    sections: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: 单位流按下标种类统计
    unit_counts: dict[str, int] = field(default_factory=dict)

    # ── 便捷统计 ──────────────────────────────────────────────────────

    def count_of(self, kind: str) -> int:
        return sum(1 for u in self.units if u.unit_kind == kind)

    @property
    def owned_by_team(self) -> dict[int, int]:
        """队伍号 → 占有的目标点数。"""
        out: dict[int, int] = {}
        for o in self.objectives:
            if o.team_id != NO_OWNER:
                out[o.team_id] = out.get(o.team_id, 0) + 1
        return out

    @property
    def objectives_by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.objectives:
            out[o.type_name] = out.get(o.type_name, 0) + 1
        return out

    def active_team_list(self) -> list[TeamState]:
        return [t for t in self.teams if t.active]

    def to_json(self, *, indent: int | None = None) -> str:
        """序列化（用于缓存与对拍）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent,
                          default=_json_default)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def _json_default(o: Any) -> Any:
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, bytes):
        return o.hex()
    return str(o)


# ── 剧场数据访问适配层 ────────────────────────────────────────────────
# theater.py 的公开方法名与最初的接口约定略有出入（例如类型名有
# ``objective_type_for_ocd`` 与 ``ocd_type_name`` 两种叫法）。这里做一层
# 兼容，任一命名可用即可，避免两端改名就互相失效。

def _all_camp_objs(theater) -> list[Any]:
    """取全部目标点（剧院静态表）。"""
    fn = getattr(theater, "all_camp_objs", None)
    if callable(fn):
        try:
            return list(fn())
        except Exception:  # noqa: BLE001
            pass
    d = getattr(theater, "camp_obj_data", None)
    if isinstance(d, dict):
        return list(d.values())
    return []


def _ocd_type(theater, ocd_index: int) -> int:
    for name in ("ocd_type", "objective_type"):
        fn = getattr(theater, name, None)
        if callable(fn):
            try:
                return int(fn(ocd_index))
            except Exception:  # noqa: BLE001
                continue
    return -1


def _ocd_type_name(theater, ocd_index: int) -> str:
    for name in ("ocd_type_name", "objective_type_for_ocd"):
        fn = getattr(theater, name, None)
        if callable(fn):
            try:
                got = fn(ocd_index)
                if got:
                    return str(got)
            except Exception:  # noqa: BLE001
                continue
    return "Unknown"


#: 目标类型"没解析出来"的两种哨兵取值：
#: * ``"Type-1"`` —— theater 侧照搬 C# ``GetTypeName(-1)`` 的返回值
#: * ``"Unknown"`` —— 本模块在 theater 侧取不到映射时的兜底
#: 两者都必须在统计与告警里被认出来。
_UNRESOLVED_TYPE_NAMES = frozenset({"Type-1", "Unknown"})


def _aircraft_type_name(theater, u: Unit) -> str:
    """飞行器机型名。

    优先用 ``aircraft_entry(...)``（返回完整 VCD 记录）；没有就退回
    ``aircraft_name(entity_type_id)``（只返回名字）。
    """
    fn = getattr(theater, "aircraft_entry", None)
    if callable(fn):
        try:
            ct = theater.ct_get(u.entity_type_id)
            ucd = None
            if ct is not None and getattr(ct, "entity_idx", -1) >= 0:
                ucd = theater.unit_def(ct.entity_idx)
            entry = fn(u.entity_type_id, ct, ucd)
            if entry is not None:
                return str(getattr(entry, "name", "") or "")
        except Exception:  # noqa: BLE001
            pass
    fn = getattr(theater, "aircraft_name", None)
    if callable(fn):
        try:
            return str(fn(u.entity_type_id) or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _callsign(theater, callsign_id: Any, callsign_num: Any) -> str:
    fn = getattr(theater, "get_callsign", None)
    if callable(fn):
        try:
            got = fn(int(callsign_id), int(callsign_num or 0))
            if got:
                return str(got)
        except Exception:  # noqa: BLE001
            pass
    return ""


def _finite(v, default: float = 0.0) -> float:
    """把任意解析出来的数值收敛成**有限浮点数**。

    ``.cam`` 里未初始化的槽位字段常是 0xFF 填满，读成 float 就是 NaN/Inf。
    ``nan or 0.0`` 这种写法**挡不住** NaN —— NaN 是"真值"，会原样穿过去，
    最终在 SQLite 里变成 NULL 撞上 NOT NULL 约束。所以统一走这里。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _co_pos(co) -> tuple[float, float]:
    """取目标点的世界英尺坐标（``CampObjEntry`` 用的是 ``pos_x``/``pos_y``）。"""
    x = getattr(co, "pos_x", None)
    if x is None:
        x = getattr(co, "position_x", 0.0)
    y = getattr(co, "pos_y", None)
    if y is None:
        y = getattr(co, "position_y", 0.0)
    return _finite(x), _finite(y)


def _projection(theater) -> dict[str, Any]:
    p = getattr(theater, "projection", None)
    if isinstance(p, dict):
        return dict(p)
    return {}


# ── 组装 ──────────────────────────────────────────────────────────────

def _resolve_unit_name(u: Unit, theater) -> tuple[str, str]:
    """返回 ``(name, callsign)``。名字主要靠 ``name_id`` 查 strings。"""
    name = ""
    callsign = ""
    if u.unit_kind == "Flight":
        cid = u.extra.get("callsign_id")
        cnum = u.extra.get("callsign_num")
        if cid is not None and theater is not None:
            callsign = _callsign(theater, cid, cnum)
        if not callsign:
            callsign = "#%s/%s" % (cid, cnum)
    if u.name_id and theater is not None:
        try:
            name = theater.string(int(u.name_id))
        except Exception:  # noqa: BLE001
            name = ""
    return name, callsign


def _mission_name(code: Optional[int], theater) -> str:
    """任务代号名：strings 索引 = 300 + 任务码（照搬 JsonExporter 第 446 行）。"""
    if code is None or theater is None:
        return ""
    try:
        return theater.string(300 + int(code))
    except Exception:  # noqa: BLE001
        return "#%d" % code


def _unit_to_rec(u: Unit, theater, warnings: list[str]) -> UnitRec:
    # .uni 里的单位坐标已经是网格整数（x=东、y=北）
    on_map = 0 <= u.x <= 1024 and 0 <= u.y <= 1024
    if not on_map:
        warnings.append("单位 %s#%d 坐标越界 (%d,%d)，不上图"
                        % (u.unit_kind, u.id.num, u.x, u.y))
    # ⚠️ z 必须是有限值。
    #    .cam 里存在**未初始化的实体槽**（实测 unit_id=0xFFFF0001、
    #    id_creator=0xFFFFFFFF，字段全是 0xFF），把它当作 Objective 读出来
    #    会得到 z=NaN。SQLite 把 NaN 存成 NULL，而 campaign_units.z 是
    #    NOT NULL —— 结果是整次上传以 IntegrityError 500 收场。
    #    camdata 的 f32/f64 已经做了一层拦截，这里是**入库前的最后一道**：
    #    即便将来有别的读取路径绕过 _Reader，也不会再把 NaN 写进库。
    #    只改值、不丢记录：单位本身照常保留（宁可少一个高度，不要少一条数据）。
    z = u.z
    if z is None or not math.isfinite(z):
        warnings.append("单位 %s#%d 的高度不是有限数（%r），按 0 处理"
                        % (u.unit_kind, u.id.num, z))
        z = 0.0
    name, callsign = _resolve_unit_name(u, theater)
    r = UnitRec(
        unit_kind=u.unit_kind,
        unit_id=u.id.num,
        id_creator=u.id.creator,
        entity_type_id=u.entity_type_id,
        team_id=u.owner,
        name_id=u.name_id,
        name=name,
        callsign=callsign,
        east=float(u.x) if on_map else None,
        north=float(u.y) if on_map else None,
        z=z,
        dest_east=u.dest_x or None,
        dest_north=u.dest_y or None,
        on_map=on_map,
        losses=u.losses,
        moved=u.moved,
        tactic=u.tactic,
        roster=u.roster,
        unit_flags=u.unit_flags & 0xFFFFFFFF,
        spotted=u.spotted,
        spotted_by=u.spotted_by_teams,
        extra=u.extra,
    )
    e = u.extra
    if u.unit_kind == "Flight":
        r.mission_code = e.get("mission")
        r.mission_name = _mission_name(e.get("mission"), theater)
        r.total_wp = len(u.waypoints)
        r.current_wp = u.current_wp
        r.tot_ms = e.get("time_on_target")
        pkg = e.get("package")
        sqd = e.get("squadron")
        r.package_unit_id = pkg.num if pkg else None
        r.squadron_unit_id = sqd.num if sqd else None
        if theater is not None:
            r.aircraft_type = _aircraft_type_name(theater, u)
    elif u.unit_kind in ("Battalion", "Brigade", "Division"):
        r.supply = e.get("supply")
        r.morale = e.get("morale")
        r.fatigue = e.get("fatigue")
        r.heading = e.get("heading")
        r.orders = e.get("orders")
        r.division = e.get("division")
        par = e.get("parent_id") or e.get("aobj")
        r.parent_unit_id = par.num if par else None
    elif u.unit_kind == "TaskForce":
        r.supply = e.get("supply")
        r.orders = e.get("orders")
    elif u.unit_kind == "Package":
        r.mission_code = None
        r.total_wp = 0
    return r


def _team_from_tea(tea: dict, team_id: int) -> TeamState:
    """把 ``.tea`` 解出的某队伍数据转成 :class:`TeamState`。

    字段名对齐 ``camdata.read_tea`` 的实际键名（与 CamReader 的 JSON 命名不同，
    映射关系已逐项与参考 ``campaign_state.json`` 核对）：

    ==========================  ====================
    .tea（本实现）               参考 JSON
    ==========================  ====================
    ``supply_available``        ``resources.supply``
    ``fuel_available``          ``resources.fuel``
    ``replacements_available``  ``resources.replacements``
    ``current_stats.aircraft``  ``strength.aircraft``
    ``current_stats.*_vehicles````strength.airDef/ground``
    ``current_stats.airbases``  ``strength.bases``
    ``current_stats.*_level``   ``strength.supplyLvl/fuelLvl``
    ``start_stats.*``           ``startStrength.*``
    ``stance[i]``               ``stances[i].value``
    ==========================  ====================
    """
    t = TeamState(team_id=team_id)
    t.name = tea.get("name") or ""
    t.motto = (tea.get("motto") or "").strip()
    t.flag = int(tea.get("team_flag") or 0)
    t.color = int(tea.get("team_color") or 0)
    t.equipment = int(tea.get("equipment") or 0)
    t.initiative = int(tea.get("initiative") or 0)
    t.reinforcement = int(tea.get("reinforcement") or 0)
    t.player_rating = _finite(tea.get("player_rating"))
    t.offensive_loss = int(tea.get("offensive_loss") or 0)
    t.attack_time = int(tea.get("attack_time") or 0)

    t.supply = int(tea.get("supply_available") or 0)
    t.fuel = int(tea.get("fuel_available") or 0)
    t.replacements = int(tea.get("replacements_available") or 0)

    exp = tea.get("experience") or {}
    t.exp_air = int(exp.get("air") or 0)
    t.exp_air_def = int(exp.get("air_def", exp.get("airDef", 0)) or 0)
    t.exp_ground = int(exp.get("ground") or 0)
    t.exp_naval = int(exp.get("naval") or 0)

    def _apply_stats(src: dict, cur: bool) -> None:
        if not src:
            return
        if cur:
            t.st_aircraft = int(src.get("aircraft") or 0)
            t.st_air_def = int(src.get("air_def_vehicles") or 0)
            t.st_ground = int(src.get("ground_vehicles") or 0)
            t.st_ships = int(src.get("ships") or 0)
            t.st_bases = int(src.get("airbases") or 0)
            t.supply_lvl = int(src.get("supply_level") or 0)
            t.fuel_lvl = int(src.get("fuel_level") or 0)
        else:
            t.start_aircraft = int(src.get("aircraft") or 0)
            t.start_air_def = int(src.get("air_def_vehicles") or 0)
            t.start_ground = int(src.get("ground_vehicles") or 0)
            t.start_ships = int(src.get("ships") or 0)
            t.start_bases = int(src.get("airbases") or 0)

    _apply_stats(tea.get("current_stats") or {}, True)
    _apply_stats(tea.get("start_stats") or {}, False)

    # 队伍"是否在场"由当前兵力决定（照搬 JsonExporter 第 178-180 行），
    # 而不是看名字是否为空。
    t.active = bool(t.st_aircraft or t.st_ground or t.st_air_def
                    or t.st_ships or t.st_bases)

    t.stances = [int(v) for v in (tea.get("stance") or [])]
    return t


def build_state(cam: bytes | Path | str, theater=None, *,
                state_dir: str | Path | None = None) -> CampaignState:
    """把一份 ``.cam`` 解成 :class:`CampaignState`。

    :param cam: ``.cam`` 路径或其字节内容
    :param theater: :class:`gfvfw.campaign.theater.TheaterData`；
        传 ``None`` 时仍能解出头部与事件，但单位/目标点将缺少名字与位置。
    """
    from . import camdata  # 延迟导入：该模块由并行任务产出

    if isinstance(cam, (str, Path)):
        bundle = Bundle.load(cam)
    else:
        bundle = Bundle.from_bytes(bytes(cam))

    st = CampaignState(cam_version=bundle.version)
    ver = bundle.version

    def _section(ext: str) -> Optional[bytes]:
        raw = bundle.get_by_ext(ext)
        st.sections[ext.lstrip(".")] = raw is not None
        return raw

    # ── 1. .cmp ───────────────────────────────────────────────────────
    cmp_raw = _section(".cmp")
    if cmp_raw is None:
        raise ValueError(".cam 里没有 .cmp，文件可能损坏")
    c: CmpData = read_cmp(cmp_raw, ver)
    st.theater = c.theater_name
    st.scenario = c.scenario
    st.save_name = c.save_file
    st.ui_name = c.ui_name
    st.campaign_time_ms = c.current_time
    st.campaign_time_label = camp_time_label(c.current_time)
    st.campaign_day = c.current_day
    st.day_zero = c.day_zero
    st.active_teams = c.active_teams
    st.endgame_result = c.endgame_result
    st.situation = c.situation
    st.tempo = c.tempo
    st.enemy_air_exp = c.enemy_air_exp
    st.enemy_ad_exp = c.enemy_ad_exp
    st.bullseye_east = c.bullseye_x
    st.bullseye_north = c.bullseye_y
    st.theater_size_x = c.theater_size_x or 1024
    st.theater_size_y = c.theater_size_y or 1024
    st.te_start_time = c.te_start_time
    st.te_time_limit = c.te_time_limit
    st.te_victory_pts = c.te_victory_pts
    st.te_num_teams = c.te_num_teams
    st.ground_ratio = c.ground_ratio
    st.air_ratio = c.air_ratio
    st.air_def_ratio = c.air_def_ratio
    st.naval_ratio = c.naval_ratio

    # ── 2. 队伍 ───────────────────────────────────────────────────────
    # 以 .cmp 的 8 个队伍槽位为骨架（名字/格言/旗色），再用 .tea 的详细状态覆盖。
    # ⚠️ .tea 的队伍号取记录里的 ``who`` 而不是数组下标 —— 下标只是排列顺序。
    by_id: dict[int, TeamState] = {}
    for i, t in enumerate(c.teams):
        by_id[i] = TeamState(team_id=i, name=t.name, motto=t.motto,
                             flag=t.flag, color=t.color)
    tea_raw = _section(".tea")
    if tea_raw is not None:
        try:
            tea = camdata.read_tea(tea_raw, ver)
            for i, tt in enumerate(tea.get("teams") or []):
                who = tt.get("who")
                tid = int(who) if who is not None else i
                parsed = _team_from_tea(tt, tid)
                b = by_id.get(tid)
                if b is not None:
                    # .cmp 有名字/格言而 .tea 没有时保留 .cmp 的
                    if not parsed.name:
                        parsed.name = b.name
                    if not parsed.motto:
                        parsed.motto = b.motto
                    if not parsed.color:
                        parsed.color = b.color
                    if not parsed.flag:
                        parsed.flag = b.flag
                by_id[tid] = parsed
        except Exception as exc:  # noqa: BLE001
            st.warnings.append(".tea 解析失败，改用 .cmp 的队伍信息：%s" % exc)
    else:
        st.warnings.append(".cam 里没有 .tea，队伍详情缺失")
    # 按队伍号排序输出，槽位稳定（0..7），前端可直接按索引上色
    st.teams = [by_id[k] for k in sorted(by_id)]

    # ── 3. .uni 单位 ──────────────────────────────────────────────────
    uni_raw = _section(".uni")
    uni_units: list[Unit] = []
    if uni_raw is not None:
        if theater is None:
            st.warnings.append("未提供剧场数据，.uni 无法解析（单位流靠类表路由）")
        else:
            try:
                res = read_units(uni_raw, ver, theater)
                uni_units = res.units
                st.unit_counts = dict(res.kind_counts)
                if res.skipped:
                    st.warnings.append(
                        ".uni 跳过 %d 条（noEntry=%d noRouter=%d errors=%d）"
                        % (res.skipped, res.skipped_no_entry,
                           res.skipped_no_router, res.skipped_error))
                for e in res.errors[:5]:
                    st.warnings.append(".uni: " + e)
            except Exception as exc:  # noqa: BLE001
                st.warnings.append(".uni 解析失败：%s" % exc)
    else:
        st.warnings.append(".cam 里没有 .uni，单位态势缺失")

    for u in uni_units:
        try:
            st.units.append(_unit_to_rec(u, theater, st.warnings))
        except Exception as exc:  # noqa: BLE001
            st.warnings.append("单位 %s#%d 转换失败：%s" % (u.unit_kind, u.id.num, exc))

    # ── 4. 中队（.cmp SquadInfo + .uni 战果）───────────────────────────
    uni_sq = {u.id.num: u for u in uni_units if u.unit_kind == "Squadron"}
    obj_name_to_camp: dict[str, int] = {}
    camp_objs: list[Any] = []
    if theater is not None:
        try:
            camp_objs = _all_camp_objs(theater)
            for co in camp_objs:
                obj_name_to_camp[(co.name or "").strip()] = co.camp_id
        except Exception as exc:  # noqa: BLE001
            st.warnings.append("目标点表读取失败：%s" % exc)

    for s in c.squadrons:
        airbase = (s.airbase or "").strip()
        if not airbase:
            # CamReader 也会跳过没有基地名的中队（47 → 46）
            continue
        east, north = world_xy_to_grid(s.x, s.y)
        rec = SquadronRec(
            squadron_id=s.id.num,
            name=(s.squad_name or "").strip(),
            airbase=airbase,
            airbase_camp_id=obj_name_to_camp.get(airbase),
            airbase_east=clamp_grid(east, expand=32),
            airbase_north=clamp_grid(north, expand=32),
            team_id=s.country,
            strength_pct=s.strength,
            specialty=s.specialty,
            camp_id=s.camp_id,
            tex_set=s.tex_set,
        )
        u = uni_sq.get(s.id.num)
        if u is not None:
            e = u.extra
            rec.has_uni = True
            rec.fuel = e.get("fuel")
            rec.aa_kills = e.get("aa_kills")
            rec.ag_kills = e.get("ag_kills")
            rec.as_kills = e.get("as_kills")
            rec.an_kills = e.get("an_kills")
            rec.missions_flown = e.get("missions_flown")
            rec.mission_score = e.get("mission_score")
            rec.total_losses = e.get("total_losses")
            rec.pilot_losses = e.get("pilot_losses")
        st.squadrons.append(rec)

    # ── 5. 目标点（CampObjData + .obd > .obj > .uni）──────────────────
    if theater is None:
        st.warnings.append("未提供剧场数据，目标点无法解析")
    else:
        # .obd 增量
        deltas: dict[int, dict] = {}
        obd_raw = _section(".obd")
        if obd_raw is not None:
            try:
                from .obd import read_obd
                obd = read_obd(obd_raw, ver)
                for dl in obd.get("deltas", []):
                    cid = getattr(dl, "camp_id", None)
                    if cid is None and isinstance(dl, dict):
                        cid = dl.get("camp_id")
                    if cid:
                        if isinstance(dl, dict):
                            deltas[int(cid)] = dl
                        else:
                            deltas[int(cid)] = {
                                "owner": dl.owner, "supply": dl.supply,
                                "fuel": dl.fuel, "losses": dl.losses,
                                "num_fstatus": dl.num_fstatus,
                                "f_status": dl.f_status,
                            }
                if obd.get("error"):
                    st.warnings.append(".obd: " + str(obd["error"]))
            except Exception as exc:  # noqa: BLE001
                st.warnings.append(".obd 解析失败：%s" % exc)
        # .obj 记录
        obj_recs: dict[int, dict] = {}
        obj_raw = _section(".obj")
        if obj_raw is not None:
            try:
                obj = camdata.read_obj(obj_raw, ver)
                obj_recs = {int(k): v for k, v in (obj.get("by_camp_id") or {}).items()}
            except Exception as exc:  # noqa: BLE001
                st.warnings.append(".obj 解析失败：%s" % exc)
        # .uni 里的 Objective
        uni_objs = {u.camp_id: u for u in uni_units if u.unit_kind == "Objective"}

        unknown_types: dict[int, int] = {}
        for co in camp_objs:
            cid = co.camp_id
            o = ObjectiveRec(camp_id=cid, name=co.name or "",
                             ocd_index=co.ocd_index, heading=co.heading or 0.0)
            # 目标类型名走 OCD 两步映射；两个命名都试，缺一个不至于全丢成 Unknown。
            o.type_id = _ocd_type(theater, co.ocd_index)
            o.type_name = _ocd_type_name(theater, co.ocd_index)
            # ⚠️ theater 侧映射不出来时返回的是 C# 原样的哨兵 ``"Type-1"``
            #    （等价于 ``GetTypeName(-1)``），我们自己的兜底则是 ``"Unknown"``。
            #    两个都要认，否则映射链断掉时页面会静默显示 "Type-1" 而没有任何告警。
            if o.type_name in _UNRESOLVED_TYPE_NAMES:
                unknown_types[co.ocd_index] = unknown_types.get(co.ocd_index, 0) + 1

            east, north = world_xy_to_grid(*_co_pos(co))
            o.east = clamp_grid(east, expand=64)
            o.north = clamp_grid(north, expand=64)

            d = deltas.get(cid)
            r = obj_recs.get(cid)
            u = uni_objs.get(cid)
            if d is not None:
                o.source = "obd"
                o.team_id = d.get("owner", NO_OWNER)
                o.supply = d.get("supply", -1)
                o.fuel = d.get("fuel", -1)
                o.losses = d.get("losses", 0)
                o.feature_count = d.get("num_fstatus", 0)
                o.damaged_features = sum(1 for x in (d.get("f_status") or []) if x)
            elif r is not None:
                o.source = "obj"
                o.team_id = r.get("owner", NO_OWNER)
                o.supply = r.get("supply", -1)
                o.fuel = r.get("fuel", -1)
                o.losses = r.get("losses", 0)
                o.first_owner = r.get("first_owner", 0)
                fs = r.get("f_status") or []
                o.feature_count = len(fs)
                o.damaged_features = sum(1 for x in fs if x)
            elif u is not None:
                o.source = "uni"
                o.team_id = u.owner
                o.supply = u.extra.get("supply", -1)
                o.fuel = u.extra.get("fuel", -1)
                o.losses = u.extra.get("losses", 0)
                o.first_owner = u.extra.get("first_owner", 0)
                fs = u.extra.get("f_status") or []
                o.feature_count = len(fs)
                o.damaged_features = sum(1 for x in fs if x)
                o.priority = u.extra.get("priority", 0)
            else:
                o.source = "none"
            st.objectives.append(o)

        if unknown_types:
            st.warnings.append(
                "有 %d 个 OCD 目标类型映射不出名称（涉及 %d 种 OCD 序号）"
                % (sum(unknown_types.values()), len(unknown_types)))

    # ── 6. 事件 ───────────────────────────────────────────────────────
    for ev in c.recent_events:
        st.events.append(EventRec(
            kind="recent", at_ms=ev.time, label=camp_time_label(ev.time),
            team_id=ev.team, east=ev.x, north=ev.y, flags=ev.flags, text=ev.text))
    for ev in c.priority_events:
        st.events.append(EventRec(
            kind="priority", at_ms=ev.time, label=camp_time_label(ev.time),
            team_id=ev.team, east=ev.x, north=ev.y, flags=ev.flags, text=ev.text))

    # ── 7. SAM 威胁与投影 ─────────────────────────────────────────────
    if theater is not None:
        try:
            for name, radii in (theater.sam_radii or {}).items():
                if isinstance(radii, dict):
                    nm = (radii.get("engagementRangeNm")
                          or radii.get("engagement_range_nm")
                          or radii.get("long"))
                else:
                    nm = radii
                if nm:
                    st.sam_threat[name] = _finite(nm)
        except Exception as exc:  # noqa: BLE001
            st.warnings.append("SAM 半径读取失败：%s" % exc)
        try:
            st.projection = _projection(theater)
        except Exception:  # noqa: BLE001
            st.projection = {}

    # ── 8. 目标点占有统计 ─────────────────────────────────────────────
    owned = st.owned_by_team
    for t in st.teams:
        t.owned_objectives = owned.get(t.team_id, 0)

    return st
