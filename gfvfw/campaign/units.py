"""``.uni`` 单位流解析。

移植自 ``CamReader/Units/UniFile.cs`` 及 ``UnitBase/UnitObjective/UnitFlight/
UnitGround/UnitSquadron/UnitPackage/UnitNaval.cs``。

流的结构
--------
``.uni`` 是一串**自描述**记录：每条记录以 ``uint16 entityTypeId`` 开头，
用它到剧场的**类表**（``Falcon4_CT.xml``，见 :mod:`gfvfw.campaign.theater`）
查出 ``(Domain, Class, Type, EntityType)``，再由这些属性决定该记录用哪种
C++ 结构序列化：

===========  ==========================================================
EntityType   C++ 基类与对应的读取器
===========  ==========================================================
1            Feature（Land Class=2，用 Objective 结构）
2            CampaignBase（抽象管理器，用 Objective 结构）
3            Objective（Land Class=4，用 Objective 结构）
4            Unit（Class=6：Flight / Package / Squadron）
5            GroundUnit（Class=6：Battalion / Brigade / Division / TaskForce）
6            Weapon / Model（用 Objective 结构）
===========  ==========================================================

``Class=7`` 是单个载具/飞机模型，同样用 Objective 结构。

因此**没有类表就无法解析 .uni** —— 这是本模块必须拿到
:class:`~gfvfw.campaign.theater.TheaterData` 的原因。

鲁棒性
------
真实存档里记录边界偶尔会错位（CamReader 为此写了"失败后向前扫一个
疑似起点"的恢复逻辑）。本模块照搬该策略：单条记录解析失败时跳过若干字节
寻找下一个合法的 entityTypeId，并在结果里如实记录跳过条数与错误。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any

from .cmpfile import VUId, clip
from .lzss import expand_with_count

__all__ = [
    "Unit", "Waypoint", "UniParseResult", "read_units",
    "StructError", "DOM", "AIR_C", "AIR_T", "LND_C", "LND_T", "SEA_C", "SEA_T",
]

# ── 域 / 类 / 型 常量（对应 C# UnitBase.cs 顶部的 Dom/AirC/AirT/LndC/LndT/SeaC/SeaT）──
DOM_AIR, DOM_LAND, DOM_SEA = 2, 3, 4
AIR_C_UNIT = 6
AIR_T_FLIGHT, AIR_T_PACKAGE, AIR_T_SQUADRON = 1, 2, 3
LND_C_GROUND_UNIT, LND_C_FEATURE, LND_C_OBJECTIVE = 6, 2, 4
LND_T_BATTALION, LND_T_BRIGADE, LND_T_DIVISION, LND_T_MIN_FEATURE = 1, 2, 3, 4
SEA_C_UNIT = 6
SEA_T_TASK_FORCE = 1

#: 为便于外部按名字引用而导出的常量组
DOM = {"air": DOM_AIR, "land": DOM_LAND, "sea": DOM_SEA}
AIR_C = {"unit": AIR_C_UNIT}
AIR_T = {"flight": AIR_T_FLIGHT, "package": AIR_T_PACKAGE, "squadron": AIR_T_SQUADRON}
LND_C = {"ground_unit": LND_C_GROUND_UNIT, "feature": LND_C_FEATURE,
         "objective": LND_C_OBJECTIVE}
LND_T = {"battalion": LND_T_BATTALION, "brigade": LND_T_BRIGADE,
         "division": LND_T_DIVISION, "min_feature": LND_T_MIN_FEATURE}
SEA_C = {"unit": SEA_C_UNIT}
SEA_T = {"task_force": SEA_T_TASK_FORCE}

#: 航路点数量上限 —— 超过即认为错位（照搬 C# 的 512）
MAX_WAYPOINTS = 512


class StructError(ValueError):
    """记录解析越界或结构非法（带定位信息，供恢复逻辑与日志使用）。"""


# ── 读取器 ────────────────────────────────────────────────────────────

class _R:
    """带越界检查的 little-endian 读取器。

    C# 用 ``BitConverter`` 裸读并靠捕获 ``IndexOutOfRangeException`` 兜底；
    这里显式检查并抛 :class:`StructError`，错误信息里带记录类型与偏移，
    便于定位错位。
    """

    __slots__ = ("d", "p", "kind")

    def __init__(self, data: bytes, kind: str):
        self.d = data
        self.p = 0
        self.kind = kind

    def _need(self, n: int) -> None:
        if self.p + n > len(self.d):
            raise StructError(
                "%s 越界：偏移 %d 需要 %d 字节，只剩 %d（总长 %d）"
                % (self.kind, self.p, n, len(self.d) - self.p, len(self.d)))

    def u8(self) -> int:
        self._need(1); v = self.d[self.p]; self.p += 1; return v

    def i16(self) -> int:
        self._need(2); v = struct.unpack_from("<h", self.d, self.p)[0]; self.p += 2; return v

    def u16(self) -> int:
        self._need(2); v = struct.unpack_from("<H", self.d, self.p)[0]; self.p += 2; return v

    def i32(self) -> int:
        self._need(4); v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def u32(self) -> int:
        self._need(4); v = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4; return v

    def f32(self) -> float:
        self._need(4)
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        # ⚠️ 非有限值归 0.0 —— 见 :meth:`f64` 的说明。
        return v if math.isfinite(v) else 0.0

    def f64(self) -> float:
        """读取双精度浮点，**非有限值（NaN / ±Inf）一律归 0.0**。

        ``.cam`` 里未初始化的实体槽位是**全 1 位模式**（``0xFFFFFFFF``），
        按 f32 解释恰好是 NaN。它会顺着 ``Unit.z`` 一路走到
        ``campaign_units.z``（``NOT NULL``）：SQLite 把 NaN 存成 NULL，
        于是整份存档以 ``IntegrityError`` 收场。
        归 0.0 而不是保留 NaN：NaN 在 SQL 里没有合法表示，在高度语义上
        本来也就是"未知/未设置"。
        """
        self._need(8)
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v if math.isfinite(v) else 0.0

    def vu(self) -> VUId:
        return VUId(num=self.u32(), creator=self.u32())

    def text(self, n: int) -> str:
        self._need(n)
        v = clip(self.d[self.p:self.p + n].decode("ascii", "replace"))
        self.p += n
        return v

    def skip(self, n: int, why: str = "") -> None:
        if n < 0:
            raise StructError("%s 跳过负数 %d 字节（%s）" % (self.kind, n, why))
        self._need(n)
        self.p += n

    def peek_u16(self, at: int) -> int | None:
        if at + 2 > len(self.d):
            return None
        return struct.unpack_from("<H", self.d, at)[0]


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class Waypoint:
    """航路点（``UnitBase.ReadWaypoint``）。"""

    haves: int = 0
    grid_x: int = 0
    grid_y: int = 0
    grid_z: int = 0
    arrive: int = 0
    depart: int = 0
    action: int = 0
    route_action: int = 0
    formation: int = 0
    flags: int = 0
    target_id: VUId = field(default_factory=VUId)
    target_building: int = 0


@dataclass
class Unit:
    """一条 ``.uni`` 记录。

    公共字段（CampaignBase + Unit）直接落成属性；各子类特有字段放进
    :attr:`extra`，避免为 8 种类型各写一个类，同时保持字段名与 C# 一致。
    """

    unit_kind: str = ""
    entity_type_id: int = 0
    id: VUId = field(default_factory=VUId)
    entity_type: int = 0
    x: int = 0
    y: int = 0
    z: float = 0.0
    spot_time: int = 0
    spotted: int = 0
    base_flags: int = 0
    owner: int = 0
    camp_id: int = 0
    # Unit-only（Objective 没有这些）
    is_unit: bool = False
    last_check: int = 0
    roster: int = 0
    unit_flags: int = 0
    dest_x: int = 0
    dest_y: int = 0
    target_id: VUId = field(default_factory=VUId)
    cargo_id: VUId = field(default_factory=VUId)
    moved: int = 0
    losses: int = 0
    tactic: int = 0
    current_wp: int = 0
    name_id: int = 0
    reinforcement: int = 0
    waypoints: list[Waypoint] = field(default_factory=list)
    # 类型特有字段
    extra: dict[str, Any] = field(default_factory=dict)
    #: 该记录在 .uni 流中的字节区间（诊断用）
    byte_start: int = 0
    byte_end: int = 0

    @property
    def spotted_by_teams(self) -> list[int]:
        """``spotted`` 位掩码解出的队伍号（只取低 8 位）。

        C# ``DecodeSpottedBy``：高 8 位是自身感知标志，需屏蔽掉。
        """
        mask = self.spotted & 0xFF
        return [i for i in range(8) if mask & (1 << i)]


@dataclass
class UniParseResult:
    """:func:`read_units` 的结果。"""

    units: list[Unit] = field(default_factory=list)
    declared_count: int = 0
    skipped: int = 0
    #: 触发"找不到类表条目"而按 Objective 解析的条数
    skipped_no_entry: int = 0
    #: 触发"无处可路由"而跳过的条数
    skipped_no_router: int = 0
    #: 解析异常后跳过的条数
    skipped_error: int = 0
    #: 实际消费的字节数 / 解压总长
    consumed: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)
    #: 各 unit_kind 的计数
    kind_counts: dict[str, int] = field(default_factory=dict)

    def count_of(self, kind: str) -> int:
        return self.kind_counts.get(kind, 0)


# ── 单条记录读取 ──────────────────────────────────────────────────────

def _read_campaign_base(r: _R, ver: int, u: Unit) -> None:
    """``UnitBase.ReadCampaignBase``：所有实体（含 Objective）共有的头部。

    ⚠️ 注意 ``x`` 是**北向**、``y`` 是**东向**（C# ``EventNode`` 注释同样如此）。
    """
    u.id = r.vu()
    u.entity_type = r.u16()
    u.x = r.i16()
    u.y = r.i16()
    if ver >= 70:
        u.z = r.f32()
    u.spot_time = r.u32()
    u.spotted = r.i16()
    u.base_flags = r.i16()
    u.owner = r.u8()
    u.camp_id = r.i16()


def _read_unit_header(r: _R, ver: int, u: Unit) -> None:
    """``UnitBase.ReadUnit``：Unit 专有字段 + 航路点。"""
    u.is_unit = True
    _read_campaign_base(r, ver, u)
    u.last_check = r.u32()
    u.roster = r.i32()
    u.unit_flags = r.i32()
    u.dest_x = r.i16()
    u.dest_y = r.i16()
    u.target_id = r.vu()
    if ver > 1:
        u.cargo_id = r.vu()
    u.moved = r.u8()
    u.losses = r.u8()
    u.tactic = r.u8()
    if 83 <= ver < 100:
        r.skip(4, "ver 83-99 额外 4 字节")
    u.current_wp = r.u16() if ver >= 71 else r.u8()
    u.name_id = r.i16()
    u.reinforcement = r.i16()
    nwp = r.u16() if ver >= 71 else r.u8()
    if nwp == 0xFFFF:              # 未初始化哨兵值
        nwp = 0
    if nwp > MAX_WAYPOINTS:
        raise StructError("航路点数量不合理：%d（上限 %d）" % (nwp, MAX_WAYPOINTS))
    u.waypoints = [_read_waypoint(r, ver) for _ in range(nwp)]


def _read_waypoint(r: _R, ver: int) -> Waypoint:
    """``UnitBase.ReadWaypoint``。"""
    w = Waypoint()
    w.haves = r.u8()
    w.grid_x = r.i16()
    w.grid_y = r.i16()
    w.grid_z = r.i16()
    w.arrive = r.u32()
    w.action = r.u8()
    w.route_action = r.u8()
    w.formation = r.u8()
    w.flags = r.u32() if ver >= 73 else r.u16()
    if w.haves & 2:
        w.target_id = r.vu()
        w.target_building = r.u8()
        if ver > 103:
            for _ in range(4):
                r.skip(8 + 1, "DesignatedTargetID[4]+building[4]")
    if w.haves & 1:
        w.depart = r.u32()
    else:
        w.depart = w.arrive
    if 86 <= ver < 100:
        r.skip(4, "ver 86-99 尾部")
    return w


def _skip_waypoint(r: _R, ver: int) -> None:
    """只跳过不保留（``UnitPackage`` 用）。"""
    _read_waypoint(r, ver)


def _read_objective_fields(r: _R, ver: int, u: Unit) -> None:
    """``UnitObjective.ReadObjectiveFields``。"""
    e = u.extra
    e["last_repair"] = r.u32()
    e["obj_flags"] = r.u32() if ver > 1 else r.u16()
    e["supply"] = r.u8()
    e["fuel"] = r.u8()
    e["losses"] = r.u8()
    if ver < 100:
        if ver >= 86:
            r.skip(8, "ver 86-99")
        elif ver >= 84:
            r.skip(12, "ver 84-85")
        elif ver >= 83:
            r.skip(8, "ver 83")
    n = r.u8()
    e["num_statuses"] = n
    e["f_status"] = list(r.d[r.p:r.p + n]) if n else []
    r.skip(n, "f_status")
    e["priority"] = r.u8()
    e["obj_name_id"] = r.i16()
    e["parent"] = r.vu()
    e["first_owner"] = r.u8()
    links = r.u8()
    e["links"] = links
    r.skip(links * 16, "links[%d]" % links)
    if ver >= 20:
        has_radar = r.u8()
        e["has_radar_data"] = has_radar
        if has_radar > 0:
            e["detect_ratio"] = [r.f32() for _ in range(8)]
    if ver >= 103:
        e["sim_x"] = r.f64()
        e["sim_y"] = r.f64()
        e["sim_z"] = r.f64()
        e["sim_heading"] = r.f32()
    if ver >= 106:
        e["camp_name"] = r.text(80).strip()
    if 86 <= ver < 100:
        r.skip(24, "ver 86-99 尾部")


def _read_flight(r: _R, ver: int, u: Unit) -> None:
    """``UnitFlight.Read``。"""
    e = u.extra
    e["z2"] = r.f32()
    fuel_burnt = r.i32()
    e["fuel_burnt"] = 0 if ver < 65 else fuel_burnt
    if ver == 74 or ver >= 100:
        e["fuel_initial"] = [r.i32() for _ in range(4)]
        e["laser_code"] = [r.i16() for _ in range(4)]
    e["last_move"] = r.u32()
    e["last_combat"] = r.u32()
    e["time_on_target"] = r.u32()
    e["mission_over_time"] = r.u32()
    e["mission_target"] = r.i16()
    if 83 <= ver < 100:
        r.skip(6, "ver 83-99")

    if ver < 24:
        loadouts = 1
        wid: list[list[int]] = [[0] * 16]
        wcount: list[list[int]] = [[0] * 16]
        if ver >= 8:
            use_loadout = r.u8()
            if use_loadout != 0:
                r.skip(5 * (16 * 2 + 16), "ver<24 备用挂载")
        for i in range(16):
            wid[0][i] = r.u16() if ver < 18 else r.u8()
        for i in range(16):
            wcount[0][i] = r.u8()
        e["loadouts"] = loadouts
        e["weapon_id"] = wid
        e["weapon_count"] = wcount
    else:
        loadouts = r.u8()
        e["loadouts"] = loadouts
        wid = []
        wcount = []
        for _ in range(loadouts):
            wid.append([(r.u16() if ver >= 73 else r.u8()) for _ in range(16)])
            wcount.append([r.u8() for _ in range(16)])
        e["weapon_id"] = wid
        e["weapon_count"] = wcount

    e["mission"] = r.u8()
    mission = e["mission"]
    e["old_mission"] = r.u8() if ver > 65 else mission
    e["last_direction"] = r.u8()
    e["priority"] = r.u8()
    e["mission_id"] = r.u8()
    if ver < 14:
        r.skip(1, "ver<14")
    e["eval_flags"] = r.u8()
    e["mission_context"] = r.u8() if ver > 65 else 0
    e["package"] = r.vu()
    e["squadron"] = r.vu()
    if ver > 65:
        e["requester"] = r.vu()
    e["slots"] = list(r.d[r.p:r.p + 4]); r.skip(4)
    e["pilots"] = list(r.d[r.p:r.p + 4]); r.skip(4)
    e["plane_stats"] = list(r.d[r.p:r.p + 4]); r.skip(4)
    e["player_slots"] = list(r.d[r.p:r.p + 4]); r.skip(4)
    e["last_player_slot"] = r.u8()
    e["callsign_id"] = r.u8()
    e["callsign_num"] = r.u8()
    e["refuel_quantity"] = r.u32()
    if ver >= 105:
        e["tex_set"] = [r.i32() for _ in range(4)]
    if ver >= 108:
        e["tacan_channel"] = list(r.d[r.p:r.p + 4]); r.skip(4)
        e["tacan_band"] = list(r.d[r.p:r.p + 4]); r.skip(4)
    if ver >= 109:
        e["loaded_cft"] = [r.u8() > 0 for _ in range(4)]
    if 83 <= ver < 100:
        r.skip(244, "ver 83-99")


def _read_battalion(r: _R, ver: int, u: Unit) -> None:
    """``UnitBattalion.Read``。"""
    e = u.extra
    e["orders"] = r.u8()
    e["division"] = r.i16()
    e["aobj"] = r.vu()
    e["last_move"] = r.u32()
    e["last_combat"] = r.u32()
    e["parent_id"] = r.vu()
    e["last_obj"] = r.vu()
    e["supply"] = r.u8()
    e["fatigue"] = r.u8()
    e["morale"] = r.u8()
    e["heading"] = r.u8()
    e["final_heading"] = r.u8()
    if ver < 15:
        r.skip(1, "ver<15 占位")
    position = r.u8()
    e["position"] = position
    if 83 <= ver < 100 and position > 0:
        r.skip(1, "ver 83-99 extraAF")


def _read_brigade(r: _R, ver: int, u: Unit) -> None:
    """``UnitBrigade.Read``。"""
    e = u.extra
    e["orders"] = r.u8()
    e["division"] = r.i16()
    e["aobj"] = r.vu()
    n = r.u8()
    e["elements"] = n
    e["element"] = [r.vu() for _ in range(n)]


def _read_division(r: _R, ver: int, u: Unit) -> None:
    """``UnitDivision.Read`` —— Division 是**扁平结构**，没有 CampaignBase/Unit 头。"""
    e = u.extra
    u.is_unit = False
    e["div_x"] = r.i16()
    e["div_y"] = r.i16()
    e["nid"] = r.i16()
    e["div_owner"] = r.i16()
    e["div_type"] = r.u8()
    n = r.u8()
    e["elements"] = n
    e["element"] = [r.vu() for _ in range(n)]


def _read_task_force(r: _R, ver: int, u: Unit) -> None:
    """``UnitTaskForce.Read``。"""
    e = u.extra
    e["orders"] = r.u8()
    e["supply"] = r.u8()
    if 83 <= ver < 100:
        r.skip(9, "ver 83-99")


def _read_squadron(r: _R, ver: int, u: Unit) -> None:
    """``UnitSquadron.Read``。"""
    e = u.extra
    e["fuel"] = r.i32()
    e["specialty"] = r.u8()
    if ver >= 101:
        e["camp_specific_rating"] = list(r.d[r.p:r.p + 16]); r.skip(16)
    stores_size = (200 if ver < 69 else 600 if ver == 73
                   else 1000 if (ver == 74 or ver >= 100) else 600 if ver >= 83 else 0)
    if stores_size > 0:
        e["stores_size"] = stores_size
        e["stores"] = list(r.d[r.p:r.p + stores_size])
        r.skip(stores_size)
    n_pilots = 48 if ver >= 29 else 36
    e["pilot_count"] = n_pilots
    pilots = []
    for _ in range(n_pilots):
        p = {
            "callsign_id": r.u8(),
            "callsign_num": r.u8(),
            "aa_kills": r.u8(),
            "ag_kills": r.u8(),
            "as_kills": r.u8(),
            "an_kills": r.u8(),
        }
        if ver >= 47:
            p["missions_flown"] = r.i16()
            p["status"] = r.u8()
            p["flight_hours"] = r.u8()
        else:
            # ver<47 的 8 字节布局未经确认（现有存档都 >= 47），字段位置是推测
            p["missions_flown"] = 0
            p["status"] = r.u8()
            p["flight_hours"] = r.u8()
        pilots.append(p)
    e["pilots"] = pilots
    r.skip(16 * 4, "schedule[16*4]")
    e["airbase_id"] = r.vu()
    e["hot_spot"] = r.vu()
    if 6 <= ver < 16:
        r.skip(8, "ver 6-15 垃圾 VU_ID")
    e["rating"] = list(r.d[r.p:r.p + 16]); r.skip(16)
    e["aa_kills"] = r.i16()
    e["ag_kills"] = r.i16()
    e["as_kills"] = r.i16()
    e["an_kills"] = r.i16()
    e["missions_flown"] = r.i16()
    e["mission_score"] = r.i16()
    e["total_losses"] = r.u8()
    e["pilot_losses"] = r.u8() if ver >= 9 else 0
    if ver >= 100:
        e["squadron_patch"] = r.u16()
    elif ver >= 45:
        e["squadron_patch"] = r.u8()
    if 83 <= ver < 100:
        r.skip(3, "ver 83-99")
    if ver >= 102:
        e["squadron_retask_at"] = r.u32()
        e["veh_relocate"] = r.u8()
    if ver >= 105:
        e["tex_set"] = r.i32()


def _read_package(r: _R, ver: int, u: Unit) -> None:
    """``UnitPackage.Read``。

    ⚠️ "进行中编队"分支末尾有一大段**盲跳**（C# 里是一串 ``pos += n``，
    合计 76 字节），这些偏移是反推出来的、没有字段定义佐证。本实现照搬
    以保持与 CamReader 一致；若将来发现错位，应从这些偏移查起。
    """
    e = u.extra
    n = r.u8()
    e["elements"] = n
    e["element"] = [r.vu() for _ in range(n)]
    e["interceptor"] = r.vu()
    if ver >= 7:
        e["awacs"] = r.vu()
        e["jstar"] = r.vu()
        e["ecm"] = r.vu()
        e["tanker"] = r.vu()
    wait_cycles = r.u8()
    e["wait_cycles"] = wait_cycles
    is_final = (u.unit_flags & 0x100000) != 0
    e["is_final"] = is_final
    if is_final and wait_cycles == 0:
        e["requests"] = r.i16()
        if ver < 35:
            r.skip(2, "ver<35 threat_stats")
        e["responses"] = r.i16()
        r.skip(4, "mission/aircraft/context/roe")
        r.skip(16, "requesterID+targetID")
        if ver >= 16:
            r.skip(4, "tot")
        if ver >= 35:
            r.skip(1, "action_type")
        if ver >= 41:
            r.skip(2, "priority")
    else:
        e["flights"] = r.u8()
        e["wait_for"] = r.i16()
        e["iax"] = r.i16()
        e["iay"] = r.i16()
        e["eax"] = r.i16()
        e["eay"] = r.i16()
        e["bpx"] = r.i16()
        e["bpy"] = r.i16()
        e["tpx"] = r.i16()
        e["tpy"] = r.i16()
        e["takeoff"] = r.u32()
        e["tp_time"] = r.u32()
        e["package_flags"] = r.u32()
        e["caps"] = r.i16()
        e["requests"] = r.i16()
        if ver < 35:
            r.skip(2, "ver<35")
        e["responses"] = r.i16()
        n_ing = r.u8()
        e["num_ingress_wps"] = n_ing
        for _ in range(n_ing):
            _skip_waypoint(r, ver)
        n_eg = r.u8()
        e["num_egress_wps"] = n_eg
        for _ in range(n_eg):
            _skip_waypoint(r, ver)
        # ↓↓ 以下均为照搬 C# 的盲跳（偏移未经字段定义佐证）
        r.skip(8 + 8 + 8 + 8, "4x VU_ID mis_request")
        r.skip(1 + 1 + 2, "who+vs+pad")
        r.skip(4 + 2 + 2, "tot+tx+ty")
        r.skip(4 + 2 + 2 + 2 + 2 + 2, "flags+caps+etc")
        r.skip(1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1, "byte 字段")
        r.skip(4 + 1 + 1 + 3, "slots+min/max_to+3pad")


# ── 路由 ──────────────────────────────────────────────────────────────

def _kind_label(entry) -> str:
    """照搬 C# UniFile.cs 的诊断用类型名计算。"""
    et = entry.entity_type
    if et <= 3 or et == 6:
        return "Objective" if et == 3 else "Feature" if et == 1 else "CampBase"
    if entry.domain == DOM_AIR:
        if entry.type == AIR_T_FLIGHT:
            return "Flight"
        if entry.type == AIR_T_PACKAGE:
            return "Package"
        if entry.type == AIR_T_SQUADRON:
            return "Squadron"
        return "AirOther"
    if entry.domain == DOM_LAND:
        if entry.type == LND_T_BATTALION:
            return "Battalion"
        if entry.type == LND_T_BRIGADE:
            return "Brigade"
        if entry.type == LND_T_DIVISION:
            return "Division"
        if entry.type >= LND_T_MIN_FEATURE:
            return "Feature"
        return "LandOther"
    if entry.domain == DOM_SEA:
        return "TaskForce" if entry.type == SEA_T_TASK_FORCE else "SeaOther"
    return "D%dC%dT%d" % (entry.domain, entry.cls, entry.type)


def _read_record(r: _R, ver: int, entry, entity_type_id: int) -> Unit:
    """按类表条目分派到具体读取器（``UniFile.Parse`` 的 switch）。"""
    kind = _kind_label(entry)
    u = Unit(unit_kind=kind, entity_type_id=entity_type_id)
    et = entry.entity_type

    if et in (1, 2, 3, 6):
        _read_campaign_base(r, ver, u)
        _read_objective_fields(r, ver, u)
        return u

    if et in (4, 5):
        if entry.domain == DOM_AIR and entry.cls == AIR_C_UNIT:
            if entry.type == AIR_T_FLIGHT:
                _read_unit_header(r, ver, u); _read_flight(r, ver, u); return u
            if entry.type == AIR_T_PACKAGE:
                _read_unit_header(r, ver, u); _read_package(r, ver, u); return u
            if entry.type == AIR_T_SQUADRON:
                _read_unit_header(r, ver, u); _read_squadron(r, ver, u); return u
        elif entry.domain == DOM_LAND and entry.cls == LND_C_GROUND_UNIT:
            if entry.type == LND_T_BATTALION:
                _read_unit_header(r, ver, u); _read_battalion(r, ver, u); return u
            if entry.type == LND_T_BRIGADE:
                _read_unit_header(r, ver, u); _read_brigade(r, ver, u); return u
            if entry.type == LND_T_DIVISION:
                _read_division(r, ver, u); return u
        elif entry.domain == DOM_SEA and entry.cls == SEA_C_UNIT:
            if entry.type == SEA_T_TASK_FORCE:
                _read_unit_header(r, ver, u); _read_task_force(r, ver, u); return u

    # 其余一律退回 Objective 结构
    u.unit_kind = "Objective"
    _read_campaign_base(r, ver, u)
    _read_objective_fields(r, ver, u)
    return u


# ── 主入口 ────────────────────────────────────────────────────────────

def read_units(raw: bytes, version: int, theater) -> UniParseResult:
    """解析 ``.uni`` 内嵌文件的**原始（压缩）字节**。

    :param theater: 提供 ``ct_get(entity_type_id)`` 的剧场数据对象。
    """
    count, u_sz, d = expand_with_count(raw)
    res = UniParseResult(declared_count=count, total=len(d))
    if not d:
        return res

    r = _R(d, ".uni")
    # 流长度上限合理值，用于判断"是否已到尾部"
    near_end = max(0, len(d) - 5000)
    i = 0
    while i < count and r.p < len(d) - 1:
        i += 1
        pos_before_id = r.p
        entity_type_id = r.u16()
        entry = theater.ct_get(entity_type_id)

        if entry is None:
            # 类表里没有 → 按 Objective 解析以保持流同步
            obj_start = r.p
            try:
                u = Unit(unit_kind="Objective", entity_type_id=entity_type_id)
                _read_campaign_base(r, version, u)
                _read_objective_fields(r, version, u)
                u.byte_start = obj_start
                u.byte_end = r.p
                res.units.append(u)
                res.kind_counts["Objective"] = res.kind_counts.get("Objective", 0) + 1
            except StructError as exc:
                if r.p >= near_end:
                    res.errors.append("流尾部结束于偏移 %d" % r.p)
                else:
                    res.errors.append(
                        "Objective entityType=%d 解析失败于偏移 %d：%s"
                        % (entity_type_id, obj_start, exc))
                    res.skipped += 1
                    res.skipped_error += 1
                break
            continue

        start = r.p
        try:
            u = _read_record(r, version, entry, entity_type_id)
            u.byte_start = start
            u.byte_end = r.p
            res.units.append(u)
            res.kind_counts[u.unit_kind] = res.kind_counts.get(u.unit_kind, 0) + 1
        except StructError as exc:
            if r.p >= near_end:
                res.errors.append("流尾部结束于偏移 %d" % r.p)
                break
            res.skipped += 1
            res.skipped_error += 1
            res.errors.append(
                "第 %d 条记录（%s entityType=%d）解析失败于偏移 %d：%s"
                % (i, _kind_label(entry), entity_type_id, start, exc))
            # 恢复：向前扫一个疑似起点（照搬 C# 的启发式）
            n_entries = len(getattr(theater, "entries", []) or [])
            recovered = False
            scan = start
            while scan < len(d) - 2:
                cand = struct.unpack_from("<H", d, scan)[0]
                cand_entry = theater.ct_get(cand)
                null_ok = cand < 100 or cand > n_entries + 100
                if ((cand_entry is not None and DOM_AIR <= cand_entry.domain <= DOM_SEA)
                        or (null_ok and scan - start >= 1)):
                    r.p = scan
                    recovered = True
                    res.errors.append("已恢复：跳过 %d 字节，下一个 entityType=%d 于偏移 %d"
                                      % (scan - start, cand, scan))
                    break
                scan += 1
            if not recovered:
                break

    res.consumed = r.p
    return res
