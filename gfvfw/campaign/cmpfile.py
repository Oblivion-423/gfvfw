"""``.cmp`` 解析（战役元数据、队伍、近期事件、中队）。

移植自 ``CamReader/Parsers/CmpFile.cs``。头部字段多按版本号门控，
条件全部照搬 C# 原样，并在注释里标出版本号。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .lzss import expand_cmp

__all__ = ["CmpData", "TeamInfo", "EventNode", "SquadInfo", "VUId",
           "read_cmp", "Reader", "clip"]


def clip(s: str) -> str:
    """C# ``Clip``：截到第一个 NUL 为止。"""
    i = s.find("\0")
    return s[:i] if i >= 0 else s


class Reader:
    """little-endian 顺序读取器，带越界检查。"""

    __slots__ = ("d", "p", "_kind")

    def __init__(self, data: bytes, kind: str = ".cmp"):
        self.d = data
        self.p = 0
        self._kind = kind

    def _need(self, n: int) -> None:
        if self.p + n > len(self.d):
            raise ValueError(
                "%s 读取越界：偏移 %d 需要 %d 字节，实际只剩 %d 字节（总计 %d）"
                % (self._kind, self.p, n, len(self.d) - self.p, len(self.d)))

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
        self._need(4); v = struct.unpack_from("<f", self.d, self.p)[0]; self.p += 4; return v

    def raw(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("读取长度不能为负：%d" % n)
        self._need(n)
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def text(self, n: int) -> str:
        """定宽 ASCII 字符串，NUL 截断。"""
        return clip(self.raw(n).decode("ascii", "replace"))

    def skip(self, n: int) -> None:
        self._need(n)
        self.p += n

    @property
    def remaining(self) -> int:
        return len(self.d) - self.p


@dataclass
class VUId:
    num: int = 0
    creator: int = 0


@dataclass
class TeamInfo:
    flag: int = 0
    color: int = 0
    name: str = ""
    motto: str = ""


@dataclass
class EventNode:
    """战役事件。注意 C# 注释：``Y`` 是东向、``X`` 是北向（字段名与直觉相反）。"""

    x: int = 0          # north
    y: int = 0          # east
    time: int = 0
    flags: int = 0
    team: int = 0
    text: str = ""


@dataclass
class SquadInfo:
    x: float = 0.0
    y: float = 0.0
    id: VUId = field(default_factory=VUId)
    desc_idx: int = 0
    name_id: int = 0
    icon: int = 0
    path: int = 0
    specialty: int = 0
    strength: int = 0
    country: int = 0
    airbase: str = ""
    flags: int = 0
    camp_id: int = 0
    tex_set: int = 0
    squad_name: str = ""


@dataclass
class CmpData:
    """``.cmp`` 解出的战役头部。字段名用 snake_case。"""

    version: int = 0
    current_time: int = 1
    te_start_time: int = 0
    te_time_limit: int = 0
    te_victory_pts: int = 0
    te_type: int = 0
    te_num_teams: int = 0
    te_aircraft: list[int] = field(default_factory=list)
    te_f16s: list[int] = field(default_factory=list)
    te_team: int = 0
    te_team_pts: list[int] = field(default_factory=list)
    te_flags: int = 0
    teams: list[TeamInfo] = field(default_factory=list)
    last_major_event: int = 0
    last_resupply: int = 0
    last_repair: int = 0
    last_reinforce: int = 0
    time_stamp: int = 0
    group: int = 0
    ground_ratio: int = 0
    air_ratio: int = 0
    air_def_ratio: int = 0
    naval_ratio: int = 0
    brief: int = 0
    theater_size_x: int = 0
    theater_size_y: int = 0
    current_day: int = 0
    active_teams: int = 0
    day_zero: int = 0
    endgame_result: int = 0
    situation: int = 0
    enemy_air_exp: int = 0
    enemy_ad_exp: int = 0
    bullseye_name: int = 0
    bullseye_x: int = 0
    bullseye_y: int = 0
    theater_name: str = ""
    scenario: str = ""
    save_file: str = ""
    ui_name: str = ""
    player_squad_id: VUId = field(default_factory=VUId)
    recent_events: list[EventNode] = field(default_factory=list)
    priority_events: list[EventNode] = field(default_factory=list)
    camp_map_size: int = 0
    camp_map: bytes | None = None
    last_index_num: int = 0
    num_squadrons: int = 0
    squadrons: list[SquadInfo] = field(default_factory=list)
    tempo: int | None = None
    creator_ip: int | None = None
    creation_time: int | None = None
    creation_rand: int | None = None
    camp_period_start: int | None = None
    camp_period_end: int | None = None
    #: 诊断：解压申报长度 / 实际长度 / 消费字节数
    compressed_size: int = 0
    declared_size: int = 0
    consumed_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict（``camp_map`` 只留长度）。"""
        d = self.__dict__.copy()
        d["teams"] = [t.__dict__.copy() for t in self.teams]
        d["recent_events"] = [e.__dict__.copy() for e in self.recent_events]
        d["priority_events"] = [e.__dict__.copy() for e in self.priority_events]
        d["squadrons"] = [
            {**s.__dict__, "id": s.id.__dict__.copy()} for s in self.squadrons
        ]
        d["player_squad_id"] = self.player_squad_id.__dict__.copy()
        d["camp_map_bytes"] = len(self.camp_map) if self.camp_map else 0
        d.pop("camp_map", None)
        return d


def _read_event(r: Reader) -> EventNode:
    """C# ``CmpFile.ReadEvent``：i16 x, i16 y, u32 time, u8 flags, u8 team,
    跳过 2+4+4 字节，再 i16 文本长度 + 文本。"""
    e = EventNode(x=r.i16(), y=r.i16(), time=r.u32(), flags=r.u8(), team=r.u8())
    r.skip(2)
    r.skip(4)
    r.skip(4)
    ln = r.i16()
    e.text = r.text(ln) if ln > 0 else ""
    return e


def read_cmp(raw: bytes, version: int, *, kind: str = ".cmp") -> CmpData:
    """解析 ``.cmp`` 内嵌文件的**原始（压缩）字节**。"""
    comp_sz, u_sz, d = expand_cmp(raw)
    if not d:
        raise ValueError(".cmp 解压长度申报为 0，无法解析")

    r = Reader(d, kind)
    out = CmpData(version=version, compressed_size=comp_sz, declared_size=u_sz)

    out.current_time = r.u32() or 1                     # 0 → 1
    if version >= 48:
        out.te_start_time = r.u32()
        out.te_time_limit = r.u32()
        out.te_victory_pts = r.i32() if version >= 49 else 0
    else:
        out.te_start_time = out.current_time
        out.te_time_limit = out.current_time + 18000000

    if version >= 52:
        out.te_type = r.i32()
        out.te_num_teams = r.i32()
        out.te_aircraft = [r.i32() for _ in range(8)]
        out.te_f16s = [r.i32() for _ in range(8)]
        out.te_team = r.i32()
        out.te_team_pts = [r.i32() for _ in range(8)]
        out.te_flags = r.i32()
        for _ in range(8):
            out.teams.append(TeamInfo(flag=r.u8(), color=r.u8(),
                                      name=r.text(20), motto=r.text(200)))

    if version >= 19:
        out.last_major_event = r.u32()
    out.last_resupply = r.u32()
    out.last_repair = r.u32()
    out.last_reinforce = r.u32()

    out.time_stamp = r.i16()
    out.group = r.i16()
    out.ground_ratio = r.i16()
    out.air_ratio = r.i16()
    out.air_def_ratio = r.i16()
    out.naval_ratio = r.i16()
    out.brief = r.i16()
    out.theater_size_x = r.i16()
    out.theater_size_y = r.i16()

    out.current_day = r.u8()
    out.active_teams = r.u8()
    out.day_zero = r.u8()
    out.endgame_result = r.u8()
    out.situation = r.u8()
    out.enemy_air_exp = r.u8()
    out.enemy_ad_exp = r.u8()
    out.bullseye_name = r.u8()
    out.bullseye_x = r.i16()
    out.bullseye_y = r.i16()

    out.theater_name = r.text(40)
    out.scenario = r.text(40)
    out.save_file = r.text(40)
    out.ui_name = r.text(40)

    out.player_squad_id = VUId(num=r.u32(), creator=r.u32())

    n_recent = r.i16()
    for _ in range(max(0, n_recent)):
        out.recent_events.append(_read_event(r))

    n_prio = r.i16()
    for _ in range(max(0, n_prio)):
        out.priority_events.append(_read_event(r))

    out.camp_map_size = r.i16()
    if out.camp_map_size > 0:
        out.camp_map = r.raw(out.camp_map_size)

    out.last_index_num = r.i16()
    out.num_squadrons = r.i16()
    # 版本 >= 102 时中队名/基地名是 80 字节，否则 40 字节
    name_len = 80 if version >= 102 else 40
    for _ in range(max(0, out.num_squadrons)):
        s = SquadInfo()
        s.x = r.f32()
        s.y = r.f32()
        s.id = VUId(num=r.u32(), creator=r.u32())
        s.desc_idx = r.i16()
        s.name_id = r.i16()
        if version >= 42:
            s.icon = r.i16()
            s.path = r.i16()
        s.specialty = r.u8()
        s.strength = r.u8()
        s.country = r.u8()
        s.airbase = r.text(name_len)
        r.skip(1)                                        # 填充字节
        if version >= 102:
            s.flags = r.i32()
            s.camp_id = r.i16()
            s.tex_set = r.i16()
            s.squad_name = r.text(name_len)
        out.squadrons.append(s)

    out.consumed_bytes = r.p
    if version >= 31 and r.remaining > 0:
        out.tempo = r.u8()
    if version >= 43 and r.remaining >= 12:
        out.creator_ip = r.u32()
        out.creation_time = r.u32()
        out.creation_rand = r.u32()
    if version >= 110 and r.remaining >= 4:
        out.camp_period_start = r.i16()
        out.camp_period_end = r.i16()

    return out
