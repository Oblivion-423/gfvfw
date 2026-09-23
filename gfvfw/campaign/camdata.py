"""BMS 战役存档内嵌小文件解析（``.obj`` ``.tea`` ``.evt`` ``.pol`` ``.pst``）。

移植自 CamReader（C#）的 ``Parsers/ObjFile.cs``、``Parsers/TeaFile.cs``、
``Parsers/EvtFile.cs``、``Parsers/PolFile.cs``、``Parsers/PstFile.cs``，
以及它们依赖的 ``Units/UnitBase.cs``、``Units/UnitObjective.cs``、
``Core/Types.cs``。所有布局均标注了对应的 C# 文件与行号。

模块边界
--------
``.obd`` **不在本模块内** —— 它由 :mod:`gfvfw.campaign.obd` 单独实现
（``read_obd``）。本模块只负责 ``.obj/.tea/.evt/.pol/.pst`` 与共用的
``read_objective_record``。

目标状态的优先级链（``Output/JsonExporter.cs:838-842``）
--------------------------------------------------------
::

    .obd delta        （最高，实时状态） —— 见 gfvfw.campaign.obd
      > .obj record                      —— 见本模块 read_obj
        > .uni Objective record
          > 都没有 → teamId = -1, supply = -1, fuel = -1, losses = 0

⚠️ 但要注意：``firstOwner`` / ``nameId`` / ``links`` / ``baseFlags`` /
``priority`` / ``objFlags`` / ``lastRepair`` 这 7 个字段**只从 ``.uni`` 的
Objective 记录取**（``JsonExporter.cs:843-850``），``.obj`` 的对应字段
**不参与导出** —— 即使 ``.obj`` 里读得到值也不会出现在 JSON 里。

输入约定
--------
每个 ``read_*`` 接收的是 **从 ``.cam`` 容器里按目录项切出来的原始字节**
（``gfvfw.campaign.bundle.Bundle.get_by_ext`` 的返回值），并**自行处理压缩**。
压缩与否由 :func:`_expand_if_compressed` 自动判别（先看长度头形状，
再"试解一次并核对解出长度 == 申报长度"）。实测结论：

* ``.obj``：头是 ``[int16 目标数][int32 解压长度][int32 忽略][LZSS 数据]``，
  CamReader 自己解压（ObjFile.cs:24-33）。**不要**用 ``expand_with_count``
  —— 它的 10 字节头对这个布局不成立。
* ``.tea`` ``.evt`` ``.pol`` ``.pst``：**实测为明文**，CamReader 也**从不**
  解压它们（TeaFile/EvtFile/PolFile/PstFile 的 ``Parse`` 都直接当明文读）。
  本模块仍会在"头部形状像压缩头 **且** 试解压长度精确吻合"时才解压。

返回结构
--------
全部返回**普通 ``dict``**（不是 dataclass），键名为 snake_case；
所有 C# 读到的字段都会出现。``VU_ID`` 一律展开成
``{"num": int, "creator": int}``，不返回裸元组。

⚠️ 与 C# 的**有意偏差**（都写在对应函数的 docstring 里）
---------------------------------------------------------
1. ``.pst``：C# 每条读 26 字节，实测存档是 **24 字节/条**；按 26 字节读会在
   第 783 条越界抛 ``IndexOutOfRangeException``。本模块用 24 字节。
   详见 :func:`read_pst`。
2. ``.obj``：C# 遇到任何记录异常就 ``break`` 且**静默丢弃**后续全部目标，
   还把 ``errors`` 只写进日志。本模块把异常记进返回值的 ``errors`` 列表，
   并在 ``complete=False`` 时明确暴露截断位置。详见 :func:`read_obj`。
3. 记录级的循环上界（``num_statuses``、``links``、``num_bases``、
   ``num_requests``、``num_waypoints`` 之类）C# 直接用量来循环，畸形数据会让
   它跑飞。本模块对"逐字节/逐条记录"的循环加了一致性上限，超限即报错。

版本门槛
--------
``version`` 参数来自内嵌 ``.ver`` 文件（实测该存档为 109）。所有 ``ver``
条件都逐条照抄 C#，并在注释里标明来源与含义。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "CamDataError",
    "read_tea",
    "read_obj",
    "read_evt",
    "read_pol",
    "read_pst",
    "read_plt",
    "read_any",
    "read_objective_record",
]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class CamDataError(ValueError):
    """内嵌文件结构非法 / 缓冲区被截断。

    消息里一定带上**文件种类**、**当前字节偏移**、**想要的字节数 vs 实际可用**，
    方便对着原始存档定位（需求要求"绝不静默返回垃圾数据"）。
    """


# --------------------------------------------------------------------------
# 常量上限（防御畸形数据把循环跑飞）
# --------------------------------------------------------------------------

#: BMS 固定 8 个队伍槽位（TeaFile.Parse 里 `if (NumTeams > 8) NumTeams = 8;`，TeaFile.cs:82）
MAX_TEAMS = 8

#: ``.obj`` 目标状态字节数上限。C# 无上限（ObjFile.cs/UnitObjective.cs:50-52），
#: 但 NumStatuses 是 u8，正常最多 255；这里用 255 兜底。
MAX_STATUSES = 255

#: 目标的结构连接数上限。C# 无上限（UnitObjective.cs:60-63 直接 `for i < Links`），
#: Links 是 u8，正常最多 255。
MAX_LINKS = 255

#: 单个 ATM 的机场数上限。C# 无上限（TeaFile.cs:190-197 直接 `for i < numBases`），
#: numBases 是 u8，正常最多 255。
MAX_AIRBASES = 255

#: 单个 ATM 的任务请求数上限。C# 无上限（TeaFile.cs:199-201），numReqs 是 int16；
#: 这里按 BMS 实际规模给一个宽松上限。
MAX_MISSION_REQUESTS = 4096

#: 单条记录里"按数值循环"的总字节数上限 —— 兜住量本身合法但乘积爆掉的畸形记录
#: （例如 numBases=200 × 40 字节）。
MAX_VARIABLE_BYTES_PER_RECORD = 1 << 16


# --------------------------------------------------------------------------
# 带边界检查的读取器
# --------------------------------------------------------------------------


@dataclass
class _Reader:
    """小端定长字段读取器，**每次读取都做边界检查**。

    C# 用的是 ``BitConverter.ToXxx(d, pos)``；越界会抛
    ``ArgumentException``/``IndexOutOfRangeException``，且消息里不含位置信息。
    这里换成带 文件种类 / 偏移 / 需求量 / 可用量 的 :class:`CamDataError`。
    """

    data: bytes
    kind: str                      # 文件种类标签，只用于报错消息，例如 ".tea"
    pos: int = 0

    # -- 报错 ------------------------------------------------------------

    def _need(self, offset: int, size: int, what: str) -> None:
        avail = len(self.data) - offset
        if offset < 0 or avail < size:
            raise CamDataError(
                "%s: 读取 %s 越界：偏移 %d 需要 %d 字节，实际只剩 %d 字节（缓冲区共 %d 字节）"
                % (self.kind, what, offset, size, max(0, avail), len(self.data)))

    # -- 有符号 / 无符号整数（宽度 1/2/4/8）-------------------------------

    def _int(self, offset: int, size: int, signed: bool, what: str) -> int:
        self._need(offset, size, what)
        return int.from_bytes(self.data[offset:offset + size], "little", signed=signed)

    # -- 顺序读取（自动前进 pos）-----------------------------------------

    def u8(self, what: str = "u8") -> int:
        v = self._int(self.pos, 1, False, what)
        self.pos += 1
        return v

    def u16(self, what: str = "u16") -> int:
        v = self._int(self.pos, 2, False, what)
        self.pos += 2
        return v

    def i16(self, what: str = "i16") -> int:
        v = self._int(self.pos, 2, True, what)
        self.pos += 2
        return v

    def u32(self, what: str = "u32") -> int:
        v = self._int(self.pos, 4, False, what)
        self.pos += 4
        return v

    def i32(self, what: str = "i32") -> int:
        v = self._int(self.pos, 4, True, what)
        self.pos += 4
        return v

    def f32(self, what: str = "f32") -> float:
        """读一个 IEEE-754 单精度浮点。

        ⚠️⚠️ **非有限值一律归 0.0**（NaN / ±Inf）—— 这是踩出来的。

        ``.cam`` 里未初始化的字段常常是**全 1 的位模式**（``0xFFFFFFFF``），
        而它按 f32 解释恰好是 **NaN**。于是解析出一个"幽灵单位"：
        ``unit_id=0xFFFF0001``、``id_creator=0xFFFFFFFF``、``z=nan``。
        写入时 SQLite 把 NaN 存成 **NULL**，而 ``campaign_units.z`` 是
        ``NOT NULL`` ⟹ ``IntegrityError: NOT NULL constraint failed: campaign_units.z``
        ⟹ 整个存档入库失败。

        更麻烦的是**它把友好错误页也一起打掉了**：会话已经进入
        PendingRollback，而当时的异常处理里又拿这个会话去查战役列表 ——
        用户看到的是光秃秃的 500，而真正的原因（一个 NaN）一个字都没露。

        归 0.0 而不是保留 NaN：NaN 在数据库里没有合法表示（必变 NULL），
        在坐标语义里也等于"未知/未设置"。要区分"真的 0"与"字段未初始化"，
        看 ``unit_id``/``id_creator`` 那些哨兵值即可。
        """
        self._need(self.pos, 4, what)
        v = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return v if math.isfinite(v) else 0.0

    def f64(self, what: str = "f64") -> float:
        """同 :meth:`f32`：非有限值归 0.0。"""
        self._need(self.pos, 8, what)
        v = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        return v if math.isfinite(v) else 0.0

    def raw(self, size: int, what: str = "bytes") -> bytes:
        self._need(self.pos, size, what)
        v = self.data[self.pos:self.pos + size]
        self.pos += size
        return v

    def vu_id(self, what: str = "VU_ID") -> dict[str, int]:
        """``VU_ID { uint num_, creator_ }``（Core/Types.cs:3）。"""
        num = self.u32(what + ".num_")
        creator = self.u32(what + ".creator_")
        return {"num": num, "creator": creator}

    def seek(self, delta: int, what: str = "skip") -> None:
        """跳跃 ``delta`` 字节（可为负），并检查落点仍在缓冲区内。

        C# 里这些跳步是裸的 ``pos += n``（例如 UnitObjective.cs:45-47 的版本补丁），
        越界要等到**下一次**读取才炸，报错位置会指向错误的地方；这里立刻检查。
        """
        target = self.pos + delta
        if target < 0 or target > len(self.data):
            raise CamDataError(
                "%s: 跳过 %s 后越界：%d + %d = %d，缓冲区共 %d 字节"
                % (self.kind, what, self.pos, delta, target, len(self.data)))
        self.pos = target

    # -- 非前进式读取（C# 里用 pos+n 直取，例如 TeamStatus / SimpleTaskingMgr）--

    def at_u8(self, offset: int, what: str = "u8") -> int:
        return self._int(offset, 1, False, what)

    def at_u16(self, offset: int, what: str = "u16") -> int:
        return self._int(offset, 2, False, what)

    def at_i16(self, offset: int, what: str = "i16") -> int:
        return self._int(offset, 2, True, what)

    def at_u32(self, offset: int, what: str = "u32") -> int:
        return self._int(offset, 4, False, what)

    def at_vu_id(self, offset: int, what: str = "VU_ID") -> dict[str, int]:
        return {"num": self.at_u32(offset, what + ".num_"),
                "creator": self.at_u32(offset + 4, what + ".creator_")}

    # -- 定宽字符串 -------------------------------------------------------

    def string(self, size: int, what: str = "str") -> str:
        """定宽 ASCII 串；遇第一个 ``\\0`` 截断（C# ``R.Str``/``Clip``）。

        C# ``R.Str`` 用 ``Encoding.ASCII``：>0x7F 的字节会变成 ``'?'``。
        这里用 ``errors="replace"`` 保留可诊断性（等价的 ASCII 语义）。
        """
        return _clip(self.raw(size, what))


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _clip(buf: bytes) -> str:
    """定宽 ASCII，遇 ``'\\0'`` 截断。

    对应 C# 的 ``R.Str``（Core/R.cs）与 UnitObjective.cs:86 的
    ``TrimEnd('\\0')``；两者都是"在第一个 NUL 处截断"。
    """
    i = buf.find(b"\0")
    if i >= 0:
        buf = buf[:i]
    return buf.decode("ascii", "replace")


def _expand_with_count(raw: bytes) -> tuple[int, int, bytes]:
    """``[int32 compSz][int16 count][int32 uncompressedSz][data...]``。

    对应 C# ``Lzss.ExpandWithCount``（Core/Lzss.cs:91-99）。
    """
    from . import lzss  # 延迟导入：明文读取路径不依赖它

    if len(raw) < 10:
        raise CamDataError(".cam 内嵌文件: 10 字节长度头不足（只有 %d 字节）" % len(raw))
    return lzss.expand_with_count(raw)


def _expand_if_compressed(raw: bytes, kind: str) -> bytes:
    """按头部判断内嵌文件是否压缩：是则解压后返回，否则**原样**返回。

    ⚠️ 重要实测结论（决定了"解压责任"落在谁身上）
    --------------------------------------------
    CamReader 里只有这几种内嵌文件会解压：

    * ``.cmp`` —— ``CmpFile.Parse`` 调 ``Lzss.ExpandCmp``（CmpFile.cs:46）
    * ``.obd`` —— ``ObdFile.Parse`` 调 ``Lzss.ExpandWithCount``（ObdFile.cs:20）
    * ``.uni`` —— ``UniFile.Parse`` 调 ``Lzss.ExpandWithCount``（UniFile.cs:14）
    * ``.obj`` —— ``ObjFile.Parse`` 自己切头后调 ``Lzss.Decompress``（ObjFile.cs:24-33）

    而 ``.tea`` / ``.evt`` / ``.pol`` / ``.pst`` 是**直接把原始字节当明文读**的：
    ``TeaFile.Parse``（TeaFile.cs:81）从 ``raw[0]`` 起读队伍数，
    ``EvtFile.Parse``（EvtFile.cs:14）、``PolFile.Parse``（PolFile.cs:15）、
    ``PstFile.Parse``（PstFile.cs:20）同理，全都没有解压步骤。

    实测 ``Save-Day  3 02 00 46.cam``（version 109）也证实：``.tea`` 前两字节
    ``08 00`` = 8 个队伍、``.pol`` 首字节 ``FF`` = 队伍掩码、
    ``.evt`` 前两字节 ``16 00`` = 22 个事件 —— 都是明文，而且按明文布局
    都能**恰好消费整段缓冲区**（.tea 8898/8898、.pol 1603/1603、.evt 90/90）。

    判定策略（先形状、再试解压）
    ----------------------------
    先做**廉价的形状检查**（长度头字段取值范围是否自洽），不通过就按明文返回；
    通过之后**真的试解一次**，并要求同时满足：

    1. 解压过程不报错（不越界、不提前耗尽压缩流）；
    2. 解出的字节数**精确等于**头部申报的解压长度。

    两条都满足才认定"这确实是压缩文件"，否则退回明文。

    为什么要"试解压"这一步：形状检查会放行明文误判。实测 ``.evt`` 的明文头恰好是
    ``comp=22, count=0, u=524289``，形状检查会通过；只有"试解压后长度是否等于
    524289"才能把它挡回明文（真正的明文长度是 90 字节）。这是一次性成本。

    :param kind: 文件种类标签，仅用于报错/调试上下文。
    """
    # -- 形状 1：10 字节头 [i32 compSz][i16 count][i32 uSz]（ObdFile/UniFile 用）--
    if len(raw) >= 10:
        comp_sz = struct.unpack_from("<i", raw, 0)[0]
        count = struct.unpack_from("<h", raw, 4)[0]
        u_sz = struct.unpack_from("<i", raw, 6)[0]
        if count >= 0 and 1 <= comp_sz <= u_sz and 10 + comp_sz <= len(raw):
            body = _try_decompress(_expand_with_count, raw, u_sz)
            if body is not None:
                return body

    # -- 形状 2：8 字节头 [i32 compSz][i32 uSz]（CmpFile 用）----------------
    if len(raw) >= 8:
        comp_sz, u_sz = struct.unpack_from("<ii", raw, 0)
        if 1 <= comp_sz <= u_sz and 8 + comp_sz <= len(raw):
            body = _try_decompress(_expand_cmp, raw, u_sz)
            if body is not None:
                return body

    return raw


def _try_decompress(expand: Callable[[bytes], tuple[int, int, bytes]],
                    raw: bytes, expect: int) -> bytes | None:
    """试解压；只有"没报错 **且** 长度精确等于申报值"才返回结果，否则 ``None``。

    解压失败（明文被误判成压缩头）时**不抛异常** —— 这正是本函数存在的理由。
    """
    try:
        _, _, body = expand(raw)
    except Exception:
        # 明文误判成压缩头 → 解压必然失败，静默退回明文即可
        return None
    if len(body) != expect:
        # 解压"成功"但长度对不上 → 同样是明文误判（例如 .evt 的 comp=22/u=524289）
        return None
    return body


def _expand_cmp(raw: bytes) -> tuple[int, int, bytes]:
    """``[int32 compSz][int32 uncompressedSz][data...]``。

    对应 C# ``Lzss.ExpandCmp``（Core/Lzss.cs:102-110）。
    """
    from . import lzss  # 延迟导入：明文读取路径不依赖它

    if len(raw) < 8:
        raise CamDataError(".cam 内嵌文件: 8 字节长度头不足（只有 %d 字节）" % len(raw))
    return lzss.expand_cmp(raw)


def _cap(value: int, limit: int, what: str, kind: str, pos: int = -1) -> int:
    """对"按量循环"的计数做上限检查（畸形数据防护）。"""
    if value < 0:
        raise CamDataError("%s: %s 为负（%d）@偏移 %d" % (kind, what, value, pos))
    if value > limit:
        raise CamDataError(
            "%s: %s 数值异常（%d > 上限 %d）@偏移 %d —— 多半是错位或数据损坏"
            % (kind, what, value, limit, pos))
    return value


# --------------------------------------------------------------------------
# .tea —— 队伍 / 队伍状态 / 空情管理器
# --------------------------------------------------------------------------


def _read_team_status(r: _Reader) -> dict[str, int]:
    """``TeamStatus``：7×u16 + 2×u8 = 16 字节（TeaFile.cs:140-149）。"""
    return {
        "air_def_vehicles": r.u16("stats.airDefVehs"),
        "aircraft": r.u16("stats.aircraft"),
        "ground_vehicles": r.u16("stats.groundVehs"),
        "ships": r.u16("stats.ships"),
        "supply": r.u16("stats.supply"),
        "fuel": r.u16("stats.fuel"),
        "airbases": r.u16("stats.airbases"),
        "supply_level": r.u8("stats.supplyLvl"),
        "fuel_level": r.u8("stats.fuelLvl"),
    }


def _read_gnd_action(r: _Reader) -> dict[str, Any]:
    """``TeamGndAction``：25 字节（TeaFile.cs:9、151-161）。

    ``ActionTime``/``ActionTimeout`` 是 u32，其后两个 ``VU_ID``（各 8 字节），
    末尾 1 字节 ``ActionType``。C# 注释（TeaFile.cs:5-7）强调它和
    ``TeamAirAction`` **同宽 25 字节**（BMSUtils 反编译漏掉了 ``LastObjective``）。
    """
    return {
        "action_time": r.u32("gndAction.actionTime"),
        "action_timeout": r.u32("gndAction.actionTimeout"),
        "objective": r.vu_id("gndAction.objective"),
        "last_objective": r.vu_id("gndAction.lastObjective"),
        "action_type": r.u8("gndAction.actionType"),
    }


def _read_air_action(r: _Reader) -> dict[str, Any]:
    """``TeamAirAction``：25 字节（TeaFile.cs:10、163-173）。"""
    return {
        "start_time": r.u32("airAction.startTime"),
        "stop_time": r.u32("airAction.stopTime"),
        "objective": r.vu_id("airAction.objective"),
        "last_objective": r.vu_id("airAction.lastObjective"),
        "action_type": r.u8("airAction.actionType"),
    }


def _read_mission_request(r: _Reader, version: int) -> dict[str, Any]:
    """``MissionRequest``（TeaFile.cs:12-23、205-225）。

    版本门槛：``ver < 35`` 时 C# 直接 ``pos += 64`` 跳过整条记录、字段全为默认值
    （TeaFile.cs:208）。实测存档 ver=109，走完整分支。

    脚注（TeaFile.cs:213-223）：
    * ``m.Who=d[pos++]; m.Vs=d[pos++]; pos+=2;`` —— 两个字节之后**又跳 2 字节**，
      合计 4 字节（C# 结构体里 ``Who, Vs`` 之后没有对应字段，属对齐填充）。
    * 末尾 ``m.MinTo=d[pos++]; m.MaxTo=d[pos++]; pos+=3;`` —— 2 字节数据 + 3 字节
      填充，保证整条 = 76 字节。
    """
    if version < 35:
        r.seek(64, "missionRequest(ver<35 整条跳过)")
        return {
            "requester_id": {"num": 0, "creator": 0},
            "target_id": {"num": 0, "creator": 0},
            "secondary_id": {"num": 0, "creator": 0},
            "pak_id": {"num": 0, "creator": 0},
            "who": 0, "vs": 0, "tot": 0, "tx": 0, "ty": 0, "req_flags": 0,
            "caps": 0, "target_num": 0, "speed": 0, "match_strength": 0,
            "priority": 0, "tot_type": 0, "action_type": 0, "mission": 0,
            "aircraft": 0, "context": 0, "roe_check": 0, "delayed": 0,
            "start_block": 0, "final_block": 0, "slots": [0, 0, 0, 0],
            "min_to": 0, "max_to": 0, "skipped": True,
        }

    m: dict[str, Any] = {}
    m["requester_id"] = r.vu_id("missionRequest.requesterId")
    m["target_id"] = r.vu_id("missionRequest.targetId")
    m["secondary_id"] = r.vu_id("missionRequest.secondaryId")
    m["pak_id"] = r.vu_id("missionRequest.pakId")
    m["who"] = r.u8("missionRequest.who")
    m["vs"] = r.u8("missionRequest.vs")
    r.seek(2, "missionRequest.who/vs 后填充")          # TeaFile.cs:213
    m["tot"] = r.u32("missionRequest.tot")
    m["tx"] = r.i16("missionRequest.tx")
    m["ty"] = r.i16("missionRequest.ty")
    m["req_flags"] = r.u32("missionRequest.reqFlags")
    m["caps"] = r.i16("missionRequest.caps")
    m["target_num"] = r.i16("missionRequest.targetNum")
    m["speed"] = r.i16("missionRequest.speed")
    m["match_strength"] = r.i16("missionRequest.matchStrength")
    m["priority"] = r.i16("missionRequest.priority")
    m["tot_type"] = r.u8("missionRequest.totType")
    m["action_type"] = r.u8("missionRequest.actionType")
    m["mission"] = r.u8("missionRequest.mission")
    m["aircraft"] = r.u8("missionRequest.aircraft")
    m["context"] = r.u8("missionRequest.context")
    m["roe_check"] = r.u8("missionRequest.roeCheck")
    m["delayed"] = r.u8("missionRequest.delayed")
    m["start_block"] = r.u8("missionRequest.startBlock")
    m["final_block"] = r.u8("missionRequest.finalBlock")
    m["slots"] = list(r.raw(4, "missionRequest.slots"))
    m["min_to"] = r.u8("missionRequest.minTo")
    m["max_to"] = r.u8("missionRequest.maxTo")
    r.seek(3, "missionRequest 尾部填充")                # TeaFile.cs:223
    m["skipped"] = False
    return m


def _read_atm(r: _Reader, version: int) -> dict[str, Any]:
    """``AirTaskingMgr``（TeaFile.cs:27-38、175-203）。

    版本门槛（TeaFile.cs:184-189）：
    * ``ver >= 28`` 才读 ``AvgCAMissions`` + ``SampleCycles``；
    * 其中 ``AvgCAStrength`` 还要 ``ver >= 63`` 才存在。
    """
    a: dict[str, Any] = {}
    a["id"] = r.vu_id("atm.id")
    a["entity_type"] = r.u16("atm.entityType")
    a["manager_flags"] = r.i16("atm.managerFlags")
    a["owner"] = r.u8("atm.owner")
    a["flags"] = r.i16("atm.flags")
    # ver >= 28：这两个/三个字段才存在
    if version >= 28:
        # ver >= 63 才有 AvgCAStrength（TeaFile.cs:186）
        a["avg_ca_strength"] = r.i16("atm.avgCAStrength") if version >= 63 else 0
        a["avg_ca_missions"] = r.i16("atm.avgCAMissions")
        a["sample_cycles"] = r.u8("atm.sampleCycles")
    else:
        a["avg_ca_strength"] = 0
        a["avg_ca_missions"] = 0
        a["sample_cycles"] = 0

    num_bases = _cap(r.u8("atm.numBases"), MAX_AIRBASES, "ATM 机场数", ".tea", r.pos - 1)
    airbases = []
    for _ in range(num_bases):
        ab_id = r.vu_id("atm.airbase.id")
        schedule = list(r.raw(32, "atm.airbase.schedule"))
        airbases.append({"id": ab_id, "schedule": schedule})
    a["airbases"] = airbases

    a["cycle"] = r.u8("atm.cycle")
    num_reqs = _cap(r.i16("atm.numReqs"), MAX_MISSION_REQUESTS, "ATM 任务请求数",
                    ".tea", r.pos - 2)
    a["mission_requests"] = [_read_mission_request(r, version) for _ in range(num_reqs)]
    return a


def _read_simple_mgr(r: _Reader) -> dict[str, Any]:
    """``SimpleTaskingMgr``（地面/海军空情管理器）：15 字节（TeaFile.cs:40、227-237）。

    C# 用 ``pos+0..pos+14`` 直取后 ``pos += 15``；注意 ``Owner``(u8) 与
    ``Flags``(i16) 之间没有填充，``Flags`` 起始于 ``pos+13``。
    """
    base = r.pos
    m = {
        "id": r.at_vu_id(base, "simpleMgr.id"),
        "entity_type": r.at_u16(base + 8, "simpleMgr.entityType"),
        "manager_flags": r.at_i16(base + 10, "simpleMgr.managerFlags"),
        "owner": r.at_u8(base + 12, "simpleMgr.owner"),
        "flags": r.at_i16(base + 13, "simpleMgr.flags"),
    }
    r.pos = base + 15
    return m


def _read_tea_team(r: _Reader, version: int) -> dict[str, Any]:
    """``TeaTeam`` —— 748 字节定长记录（TeaFile.cs:42-68、98-138）。

    字段顺序严格照抄 ``ReadTeam``；只有关键字段标注了 C# 行号。
    ``version`` 在本函数里**没有**版本门槛（CamReader 传了 ver 却没用）。
    """
    t: dict[str, Any] = {}
    t["id"] = r.vu_id("team.id")
    t["entity_type"] = r.u16("team.entityType")
    t["who"] = r.u8("team.who")                # JSON 导出成 teams[i].id
    t["c_team"] = r.u8("team.cTeam")
    t["flags"] = r.i16("team.flags")
    t["member"] = list(r.raw(8, "team.member"))            # byte[8]
    t["stance"] = [r.i16("team.stance[%d]" % i) for i in range(8)]   # short[8]
    t["first_colonel"] = r.i16("team.firstColonel")
    t["first_commander"] = r.i16("team.firstCommander")
    t["first_wingman"] = r.i16("team.firstWingman")
    t["last_wingman"] = r.i16("team.lastWingman")
    t["experience"] = {
        "air": r.u8("team.airExp"),
        "air_def": r.u8("team.airDefExp"),
        "ground": r.u8("team.groundExp"),
        "naval": r.u8("team.navalExp"),
    }
    t["initiative"] = r.i16("team.initiative")
    t["supply_available"] = r.u16("team.supplyAvail")
    t["fuel_available"] = r.u16("team.fuelAvail")
    t["replacements_available"] = r.u16("team.replacementsAvail")
    t["player_rating"] = r.f32("team.playerRating")
    t["last_player_mission"] = r.u32("team.lastPlayerMission")
    t["current_stats"] = _read_team_status(r)
    t["start_stats"] = _read_team_status(r)
    t["reinforcement"] = r.i16("team.reinforcement")

    # BonusObjs[20] 是 20 个 VU_ID（各 8 字节），然后是 BonusTime[20]（各 u32）。
    # C# 分成两个循环（TeaFile.cs:123-124），即内存里"结构体数组"而非"逐元素配对"。
    bonus_objs = [r.vu_id("team.bonusObjs[%d]" % i) for i in range(20)]
    bonus_time = [r.u32("team.bonusTime[%d]" % i) for i in range(20)]
    t["bonus_objectives"] = bonus_objs
    t["bonus_time"] = bonus_time

    t["objective_type_priority"] = list(r.raw(36, "team.objTypePriority"))
    t["unit_type_priority"] = list(r.raw(20, "team.unitTypePriority"))
    t["mission_priority"] = list(r.raw(29, "team.missionPriority"))
    t["attack_time"] = r.u32("team.attackTime")
    t["offensive_loss"] = r.u8("team.offensiveLoss")
    t["max_vehicle"] = list(r.raw(20, "team.maxVehicle"))
    t["team_flag"] = r.u8("team.teamFlag")
    t["team_color"] = r.u8("team.teamColor")
    t["equipment"] = r.u8("team.equipment")
    t["name"] = r.string(20, "team.name")
    t["motto"] = r.string(200, "team.motto")
    t["ground_action"] = _read_gnd_action(r)
    t["def_air_action"] = _read_air_action(r)
    t["off_air_action"] = _read_air_action(r)
    return t


def read_tea(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.tea``（队伍 / 队伍状态 / 空情管理器）。

    C# 来源：``Parsers/TeaFile.cs``（``TeaFile.Parse``，TeaFile.cs:77-96）。

    实测存档（``Save-Day  3 02 00 46.cam``，ver=109）：``.tea`` 共 8898 字节、
    **明文**，头 2 字节 ``08 00`` = 8 个队伍。按 C# 顺序走满 8 个队伍 + 8 组
    (ATM/GTM/NTM) 后 ``pos`` 恰好等于 8898 —— 即 C# 的字段宽算**完全正确**，
    本模块原样照搬。

    结构：:

        [int16 numTeams]                       # >8 时截断为 8（TeaFile.cs:82）
        numTeams × (TeaTeam(748B), ATM, GTM(15B), NTM(15B))

    ``ATM`` 是**变长**的（含机场数 + 任务请求数），所以队伍记录之间**不是**等距；
    实测本存档各段长度依次为 802/802/2082/802/802/802/2002 字节。

    返回::

        {
          "version": int,
          "num_teams": int,          # 头部原始值经 8 上限截断后的值
          "num_teams_raw": int,      # 头部的原始 int16
          "teams": [ {...TeaTeam...} ],
          "atm": [ {...AirTaskingMgr...} ],
          "gtm": [ {...SimpleTaskingMgr...} ],
          "ntm": [ {...SimpleTaskingMgr...} ],
          "bytes_total": int,
          "bytes_consumed": int,
        }
    """
    data = _expand_if_compressed(raw, ".tea")
    r = _Reader(data, ".tea")
    num_raw = r.i16("numTeams")
    # C#：if (NumTeams > 8) NumTeams = 8;（TeaFile.cs:82）—— 只截上限，不查负数
    if num_raw < 0:
        raise CamDataError(".tea: 队伍数为负（%d）@偏移 0 —— 数据损坏" % num_raw)
    num_teams = min(num_raw, MAX_TEAMS)

    teams, atms, gtms, ntms = [], [], [], []
    for i in range(num_teams):
        teams.append(_read_tea_team(r, version))
        atms.append(_read_atm(r, version))
        gtms.append(_read_simple_mgr(r))
        ntms.append(_read_simple_mgr(r))

    return {
        "version": version,
        "num_teams": num_teams,
        "num_teams_raw": num_raw,
        "teams": teams,
        "atm": atms,
        "gtm": gtms,
        "ntm": ntms,
        "bytes_total": len(data),
        "bytes_consumed": r.pos,
    }


# --------------------------------------------------------------------------
# .evt —— 战役事件表
# --------------------------------------------------------------------------


def read_evt(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.evt``（战役事件列表）。

    C# 来源：``Parsers/EvtFile.cs``（``EvtFile.Parse``，EvtFile.cs:11-28）。

    ``version`` 参数**完全没用**（C# 的 ``Parse`` 只收 ``raw``）——保留它是为了
    与其它 ``read_*`` 统一签名。

    布局（EvtFile.cs:14-25）::

        [int16 n]
        n × ([int16 id][int16 flags])        # struct CampEvent，4 字节

    实测存档：90 字节、22 个事件，恰好 2 + 4×22 = 90。字段语义 C# 没给出 ——
    ``id`` 看起来是事件槽位下标（实测 0..21 连续），``flags`` 只有第 1 槽为
    ``0x0008``、其余为 0。
    """
    data = _expand_if_compressed(raw, ".evt")
    r = _Reader(data, ".evt")
    n = r.i16("numEvents")
    if n < 0:
        raise CamDataError(".evt: 事件数为负（%d）@偏移 0" % n)
    need = 2 + 4 * n
    if need > len(data):
        raise CamDataError(
            ".evt: 声明 %d 个事件需要 %d 字节，缓冲区只有 %d 字节（截断 %d 字节）"
            % (n, need, len(data), need - len(data)))
    events = []
    for i in range(n):
        events.append({
            "id": r.i16("events[%d].id" % i),
            "flags": r.i16("events[%d].flags" % i),
        })
    return {
        "version": version,
        "num_events": n,
        "events": events,
        "bytes_total": len(data),
        "bytes_consumed": r.pos,
    }


# --------------------------------------------------------------------------
# .pol —— 政治状态（首要目标优先级）
# --------------------------------------------------------------------------


def read_pol(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.pol``（队伍掩码 + 首要目标各队优先级/旗标）。

    C# 来源：``Parsers/PolFile.cs``（``PolFile.Parse``，PolFile.cs:12-34）。

    ``version`` 未使用（C# ``Parse`` 只收 ``raw``）。

    布局（PolFile.cs:15-31）::

        [uint8  teamMask]                      # bit t 置位 → 该目标有队伍 t 的条目
        [int16  n]                             # 首要目标数
        n × ([VU_ID id]                        # 8 字节
             Σ_{t=0..7, mask>>t&1} ([int16 priority][uint8 flags]))   # 每队 3 字节

    注意 ``Priority[t]`` / ``Flags[t]`` 是**定长 8 槽位数组**，掩码为 0 的槽位
    保持默认 0（PolFile.cs:24-26 只创建数组、不预填充，C# 默认值即 0）。

    实测存档：1603 字节、mask=0xFF、50 个目标，按上式正好消费 1603 字节
    （验证了 8 队全置位时每条 8+24=32 字节）。

    ``priority`` 实测取值含 ``-1``（推测是"未设置/无效"哨兵）与 0..62 的等级；
    C# 未说明语义。
    """
    data = _expand_if_compressed(raw, ".pol")
    r = _Reader(data, ".pol")
    team_mask = r.u8("teamMask")
    n = r.i16("numObjectives")
    if n < 0:
        raise CamDataError(".pol: 首要目标数为负（%d）@偏移 1" % n)
    # 最小可能长度：8 字节 id + 每个置位队伍 3 字节
    per_obj_min = 8 + 3 * bin(team_mask & 0xFF).count("1")
    need_min = 3 + per_obj_min * n
    if need_min > len(data):
        raise CamDataError(
            ".pol: mask=0x%02X 下声明 %d 个首要目标，最少需要 %d 字节，"
            "缓冲区只有 %d 字节（截断 %d 字节）"
            % (team_mask, n, need_min, len(data), need_min - len(data)))

    objectives = []
    for i in range(n):
        obj: dict[str, Any] = {
            "id": r.vu_id("objectives[%d].id" % i),
            # 定长 8 槽位数组，掩码为 0 的队保持 0（C# 数组默认值）
            "priority": [0] * 8,
            "flags": [0] * 8,
        }
        for t in range(8):
            if team_mask & (1 << t):
                obj["priority"][t] = r.i16("objectives[%d].priority[%d]" % (i, t))
                obj["flags"][t] = r.u8("objectives[%d].flags[%d]" % (i, t))
        objectives.append(obj)

    return {
        "version": version,
        "team_mask": team_mask,
        "num_objectives": n,
        "objectives": objectives,
        "bytes_total": len(data),
        "bytes_consumed": r.pos,
    }


# --------------------------------------------------------------------------
# .pst —— 持久化对象（世界坐标 + ID）
# --------------------------------------------------------------------------


def _read_persist_obj(r: _Reader) -> dict[str, Any]:
    """``PersistObj`` —— **24 字节**（BMS ``PackedPersistObj``）。

    逐字节布局（实测 849 条全部自洽）：:

        [0..3]  float32 X
        [4..7]  float32 Y
        [8..11] uint32  CreatorId
        [12..15] uint32 ObjId
        [16]    uint8   Index
        [17..19] 3 字节对齐填充（实测恒为 00 00 00）
        [20..21] int16  VisType
        [22..23] int16  Flags

    C# ``PstFile.cs:26-33`` 读的是 **26 字节**（``Index`` 后 ``pos += 4``），
    与真实记录不符，详见 :func:`read_pst`。
    """
    return {
        "x": r.f32("object.x"),
        "y": r.f32("object.y"),
        "creator_id": r.u32("object.creatorId"),
        "object_id": r.u32("object.objId"),
        "index": r.u8("object.index"),
        "padding": list(r.raw(3, "object.padding")),   # 1 字节 Index + 3 字节填充 = 4 字节槽
        "visibility_type": r.i16("object.visType"),
        "flags": r.i16("object.flags"),
    }


def read_pst(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.pst``（持久化对象：世界 x/y + 创建者/对象 ID + 可见性）。

    C# 来源：``Parsers/PstFile.cs``（``PstFile.Parse``，PstFile.cs:17-37）。

    ⚠️ **与 C# 的有意偏差（C# 在这里是错的）**
    ------------------------------------------
    C# 每条读 26 字节::

        X(4) Y(4) CreatorId(4) ObjId(4) Index(1) +3 padding(第 31 行 `pos += 4`)
        VisType(2) Flags(2)   →  26

    但实测存档 ``Save-Day  3 02 00 46.cam`` 的 ``.pst`` 是 **20380 字节、
    头声明 849 条**，而 ``4 + 849×24 = 20380`` **恰好整除**；按 26 字节算需要
    22078 字节，C# 会在**第 783 条**（偏移 20362 + 26 > 20380）抛
    ``IndexOutOfRangeException``，``Program.cs`` 的 catch 会让整个 CamReader
    以退出码 5 失败。也就是说 C# 的 ``.pst`` 读法**在实测存档上根本跑不通**
    —— ``campaign_state.json`` 里也确实没有任何 ``.pst`` 数据
    （``JsonExporter`` 从不导出 ``.pst``），所以没有任何 C# 侧参照可对拍。

    因此本模块用 **24 字节/条**：``Index`` 是 1 字节 + **3** 字节填充
    （C# 写的是 1 + 3 但 ``pos += 4`` 之后紧接着又读 ``VisType``，
    等于把 ``VisType`` 推到偏移 21 —— 真正的 ``VisType`` 在偏移 20）。
    字段顺序与 C# 完全一致，只是填充宽度改成正确的 3 字节。

    "24 字节"独立证据（不只是因为整除）：

    * 849 条全部按 24 字节解码自洽 —— 每条 ``index`` 都是 0、
      ``padding`` 恒为 ``00 00 00``、``flags`` 恒为 6、
      ``visType`` 取 128/129 中的一个；换成 26 字节则从第 1 条起就错位。
    * 坐标落在该剧场（Balkans）合理范围内且 x/y 成对合理：
      首条 (2773223.5, 1875955.25)，末条 (2712922.5, 2104439.25)。

    版本门槛：``version < 69`` 时 C# 直接返回空表（PstFile.cs:19）。

    返回::

        {"version", "present": bool, "num_objects", "objects": [...],
         "bytes_total", "bytes_consumed"}
      其中每个 object 为
        {"x", "y", "creator_id", "object_id", "index", "padding"[3],
         "visibility_type", "flags"}
    """
    data = _expand_if_compressed(raw, ".pst")
    r = _Reader(data, ".pst")
    # C#：if (version < 69) 返回空（PstFile.cs:19）
    if version < 69:
        return {
            "version": version,
            "present": False,
            "num_objects": 0,
            "objects": [],
            "bytes_total": len(data),
            "bytes_consumed": 0,
        }

    n = r.i32("numObjects")
    if n < 0:
        raise CamDataError(".pst: 对象数为负（%d）@偏移 0" % n)
    need = 4 + 24 * n
    if need > len(data):
        raise CamDataError(
            ".pst: 声明 %d 个持久化对象（每条 24 字节）需要 %d 字节，"
            "缓冲区只有 %d 字节（截断 %d 字节）"
            "—— 若这里报错而条数看起来正常，请对照 PstFile.cs 的 26 字节读法"
            % (n, need, len(data), need - len(data)))

    objects = [_read_persist_obj(r) for _ in range(n)]
    return {
        "version": version,
        "present": True,
        "num_objects": n,
        "objects": objects,
        "bytes_total": len(data),
        "bytes_consumed": r.pos,
    }


# --------------------------------------------------------------------------
# .obj / .obd / .uni 共用的目标结构
# --------------------------------------------------------------------------


def read_objective_record(d: bytes, pos: int, version: int,
                          kind: str = "objective") -> tuple[dict[str, Any], int]:
    """读**一条**目标记录（``CampaignBase`` + 目标专有字段），返回 ``(dict, 新偏移)``。

    供 ``.obj`` / ``.obd`` / ``.uni`` 三处共用 —— 三者的目标结构完全相同，
    C# 里也是同一对函数（``Units/UnitBase.cs`` 的 ``ReadCampaignBase`` +
    ``Units/UnitObjective.cs`` 的 ``ReadObjectiveFields``）。

    ⚠️ 调用方自己负责"记录之前是否还有前缀"：``.obj`` 每条前面有 2 字节
    int16 实体类型前缀（``ObjFile.cs:49``），``.uni`` 前面有记录类型字节 +
    int16 实体类型（见 ``Units/UniFile.cs``），``.obd`` 则是另一种结构
    （没有坐标/实体类型，见 :mod:`gfvfw.campaign.obd`）。本函数只读"目标本身"。

    :param d:       解压后的记录缓冲区。
    :param pos:     该条**目标数据**的起始偏移（不是整条记录的起始）。
    :param version: 战役版本；所有 ``ver`` 门槛照抄 C#，见 :func:`_read_objective`。
    :param kind:    报错标签（如 ``".obj"``），用于 :class:`CamDataError` 消息。
    :returns: ``(objective_dict, 该条之后的偏移)``。
    :raises CamDataError: 越界或计数异常。
    """
    r = _Reader(d, kind, pos)
    o = _read_objective(r, version)
    return o, r.pos


def _read_objective(r: _Reader, version: int) -> dict[str, Any]:
    """一条目标记录：``UnitBase.ReadCampaignBase`` + ``UnitObjective.ReadObjectiveFields``。

    来源：
    * ``Units/UnitBase.cs:55-69`` —— CampaignBase 部分
    * ``Units/UnitObjective.cs:33-91`` —— 目标专有部分
    """
    o: dict[str, Any] = {}
    # ── CampaignBase（UnitBase.cs:58-68）──────────────────────────────
    o["id"] = r.vu_id("objective.id")
    o["entity_type"] = r.u16("objective.entityType")
    o["x"] = r.i16("objective.x")
    o["y"] = r.i16("objective.y")
    # ver >= 70 才有 float z（UnitBase.cs:63）
    o["z"] = r.f32("objective.z") if version >= 70 else 0.0
    o["spot_time"] = r.u32("objective.spotTime")
    o["spotted"] = r.i16("objective.spotted")
    o["base_flags"] = r.i16("objective.baseFlags")
    o["owner"] = r.u8("objective.owner")
    o["camp_id"] = r.i16("objective.campId")

    # ── 目标专有（UnitObjective.cs:36-90）────────────────────────────
    o["last_repair"] = r.u32("objective.lastRepair")
    # ver > 1 才有 u32 ObjFlags，否则只有 u16（UnitObjective.cs:37-38）
    o["objectives_flags"] = r.u32("objective.objFlags") if version > 1 else r.u16("objective.objFlags16")
    o["supply"] = r.u8("objective.supply")
    o["fuel"] = r.u8("objective.fuel")
    o["losses"] = r.u8("objective.losses")

    # ver < 100 的版本补丁块（UnitObjective.cs:43-48）；实测 ver=109 不进入
    if version < 100:
        if version >= 86:
            r.seek(8, "objective ver86-99 补丁 A")
        elif version >= 84:
            r.seek(12, "objective ver84-85 补丁 A")
        elif version >= 83:
            r.seek(8, "objective ver83 补丁 A")

    num_statuses = _cap(r.u8("objective.numStatuses"), MAX_STATUSES,
                        "目标状态数", ".obj", r.pos - 1)
    o["num_statuses"] = num_statuses
    o["f_status"] = list(r.raw(num_statuses, "objective.fStatus"))
    o["priority"] = r.u8("objective.priority")
    o["name_id"] = r.i16("objective.nameId")
    o["parent"] = r.vu_id("objective.parent")
    o["first_owner"] = r.u8("objective.firstOwner")
    links = _cap(r.u8("objective.links"), MAX_LINKS, "目标连接数", ".obj", r.pos - 1)
    o["links"] = links
    # C# 直接把每条连接跳过 16 字节（UnitObjective.cs:62）
    r.seek(16 * links, "objective.links 数据（每条 16 字节）")

    # ver >= 20 才有雷达数据标志，>0 时再跟 8 个 float（UnitObjective.cs:65-74）
    if version >= 20:
        has_radar = r.u8("objective.hasRadarData")
        o["has_radar_data"] = has_radar
        if has_radar > 0:
            o["detect_ratio"] = [r.f32("objective.detectRatio[%d]" % i) for i in range(8)]
        else:
            o["detect_ratio"] = []
    else:
        o["has_radar_data"] = 0
        o["detect_ratio"] = []

    # ver >= 103：世界坐标双精度 3D + 单精度航向（UnitObjective.cs:76-83）
    if version >= 103:
        o["sim_x"] = r.f64("objective.simX")
        o["sim_y"] = r.f64("objective.simY")
        o["sim_z"] = r.f64("objective.simZ")
        o["sim_heading"] = r.f32("objective.simHeading")
    else:
        o["sim_x"] = 0.0
        o["sim_y"] = 0.0
        o["sim_z"] = 0.0
        o["sim_heading"] = 0.0

    # ver >= 106：80 字节 ASCII 战役名（UnitObjective.cs:84-89）
    o["camp_name"] = r.string(80, "objective.campName") if version >= 106 else ""

    # ver 86-99 的尾部补丁（UnitObjective.cs:90）。注意 C# 把它放在 CampName
    # **之后**，即 ver>=106 时这 24 字节永远不会被读到（106 已 >= 100）。
    if 86 <= version < 100:
        r.seek(24, "objective ver86-99 尾部补丁")

    return o


def read_obj(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.obj``（目标列表）。

    C# 来源：``Parsers/ObjFile.cs``（``ObjFile.Parse``，ObjFile.cs:17-79），
    依赖 ``Units/UnitBase.cs`` 与 ``Units/UnitObjective.cs``。

    头部（ObjFile.cs:19-23）::

        [0..1]   int16  numObjectives
        [2..5]   int32  uncompressedSize
        [6..9]   int32  （忽略）
        [10..]   LZSS 压缩数据（用 Lzss.Decompress(comp, uncompSize)）

    单条记录（ObjFile.cs:43-52）::

        [int16 实体类型前缀]                  # C# 只 pos += 2，不解释这个值
        CampaignBase + 目标字段               # 见 _read_objective

    ⚠️ **与 C# 的有意偏差**
    ----------------------
    C# 在 ``try/catch`` 里逐条读，一遇异常就 ``errors++`` 并 ``break``
    （ObjFile.cs:69-75）：**后面所有目标被静默丢弃**，errors 只进日志。
    本模块保留"读不动就停"的语义，但把

    * ``errors`` —— 字符串列表，写清第几条、偏移、原因；
    * ``complete`` —— 是否读满 ``numObjectives``；
    * ``records_read`` / ``bytes_consumed`` —— 实际读到哪

    都放进返回值，调用方能知道"数据被截断"而不会被静默的短列表骗到。

    ⚠️ ``.obj`` 在**本次测试的存档里不存在**：``Save-Day  3 02 00 46.cam`` 只有
    ``.cmp .obd .uni .tea .evt .plt .pst .pol .ver`` 9 个内嵌文件。
    CamReader 的 ``.obj`` 是从 ``.cmp`` 里 ``Scenario`` 推出的 **start save**
    （``Program.cs:129-151``）里读的，不是当前存档。因此本函数在本存档上
    无法用真实数据对拍（见验证脚本报告）。

    版本门槛：``ver >= 70`` 的 z、``ver > 1`` 的 ObjFlags、``ver < 100`` 的三个
    补丁块、``ver >= 20`` 的雷达、``ver >= 103`` 的 sim 坐标、``ver >= 106`` 的
    战役名，全部按 C# 逐条照抄（见 :func:`_read_objective`）。

    返回::

        {
          "version", "num_objectives", "uncompressed_size",
          "uncompressed_size_actual",     # LZSS 实际解出的字节数
          "records_read", "complete", "errors": [str, ...],
          "objectives":     {camp_id(int): {...}},   # 按 camp_id 索引（C# ByCampId）
          "by_camp_id":     {camp_id(int): {...}},   # 与 "objectives" 同一个对象（别名）
          "order":          [camp_id|None, ...],     # 记录出现顺序（camp_id<=0 记 None）
          "bytes_total", "bytes_consumed",
        }

    ``by_camp_id`` 是给上层取的**别名**（与 ``objectives`` 同一份 dict，不是拷贝）：
    并行产出的 :mod:`gfvfw.campaign.state` 用的是 ``obj.get("by_camp_id")``。
    两个键都存在，取哪个都一样。
    """
    if len(raw) < 10:
        raise CamDataError(".obj: 头部不足 10 字节（只有 %d 字节）" % len(raw))
    num_objectives = struct.unpack_from("<h", raw, 0)[0]
    uncomp_size = struct.unpack_from("<i", raw, 2)[0]

    out: dict[str, Any] = {
        "version": version,
        "num_objectives": num_objectives,
        "uncompressed_size": uncomp_size,
        "uncompressed_size_actual": 0,
        "records_read": 0,
        "complete": False,
        "errors": [],
        "objectives": {},
        "order": [],
        "bytes_total": len(raw),
        "bytes_consumed": 0,
    }

    # C#：uncompSz == 0 直接返回空（ObjFile.cs:29）；needs lzss 模块
    if uncomp_size <= 0:
        out["complete"] = True
        return out

    from . import lzss  # 延迟导入：只有 .obj 需要解压

    try:
        data = lzss.decompress(raw[10:], uncomp_size)
    except Exception as exc:
        # 解压流本身坏了/被截断：C# 的 Decompress 会抛 IndexOutOfRangeException，
        # 消息里没有 .obj 上下文。这里补上文件种类 + 申报/可用长度。
        raise CamDataError(
            ".obj: LZSS 解压失败（申报解压长度 %d 字节，压缩数据 %d 字节）：%s"
            % (uncomp_size, len(raw) - 10, exc)) from exc
    out["uncompressed_size_actual"] = len(data)
    out["bytes_total"] = len(data)
    r = _Reader(data, ".obj")

    # C# 循环条件：recIdx < numObjectives && pos < d.Length - 2（ObjFile.cs:43）
    # （末尾 -2 是因为每条记录开头要吃 2 字节类型前缀）
    errors: list[str] = out["errors"]
    while len(out["order"]) < num_objectives and r.pos < len(data) - 2:
        rec_idx = len(out["order"]) + 1
        start = r.pos
        try:
            r.seek(2, "obj record 的 int16 实体类型前缀")
            o = _read_objective(r, version)
        except CamDataError as exc:
            # 保留 C# 的"停止"语义，但把原因暴露出去（C# 只写日志）
            errors.append("第 %d 条记录（偏移 %d）解析失败：%s" % (rec_idx, start, exc))
            break

        camp_id = o["camp_id"]
        # C#：campId > 0 才入表（ObjFile.cs:55），键是 campId 而不是 id.num_
        if camp_id > 0:
            out["objectives"][camp_id] = o
            out["order"].append(camp_id)
        else:
            # 不满足入表条件时 C# 也不计入 ByCampId，但 recIdx 已经自增
            out["order"].append(None)

    # order 里可能含 None（campId <= 0 的记录），计数只算真正入表的
    out["records_read"] = len(out["order"])
    out["complete"] = (len(out["order"]) >= num_objectives)
    out["bytes_consumed"] = r.pos
    # 别名：与 "objectives" 指向同一份 dict（并行模块 state.py 用 by_camp_id）
    out["by_camp_id"] = out["objectives"]
    return out


# --------------------------------------------------------------------------
# .plt —— 无 C# 参考，明确不实现
# --------------------------------------------------------------------------


def read_plt(raw: bytes, version: int) -> dict[str, Any]:
    """``.plt``（飞行员记录）—— **本模块不实现**。

    C# 参考项目里**没有任何 .plt 解析器**：``reference/CamReader-0.1.0`` 与
    BMS 自带的 ``Tools/CamReader-0.1.0`` 下都没有 ``PltFile.cs``；
    ``Program.cs:2-4`` 列出的可解析文件是 ``.cmp .evt .obd .obj .pol .pst .tea .uni``，
    不含 ``.plt``。BMS 侧的 ``JsonToCam/Program.cs`` 与 ``README.md`` 反而明确写着
    ``.plt`` 是"未建模/原样拷回"的数据（README.md:132、165；
    JsonToCam/Program.cs:11、52、140、218）。

    也就是说 ``.plt`` 的**布局无从考证**，本模块不做任何猜测。
    实测该存档的 ``.plt`` 为 3373 字节、version 109，前几字节为
    ``20 03 | 02 00 | 08 00 | 02 00 | 01 5f | 02 00 | 02 62 | 02 00 | 03 61 ...``
    —— 能看到 ``[u16 800][u16 2][u16 8][u16 2]`` 之后出现
    ``[u16 x][u16 小值]`` 的重复模式，像是"某计数 + 定长记录数组"，
    但**没有任何一条能被证实**，故不写。

    需要 ``.plt`` 时请先找到权威格式来源（BMS 源码 ``campaign`` 部分或
    ``Pilot`` 类的序列化代码），再按本模块的报错风格补一个 ``read_plt``。

    :raises NotImplementedError: 总是抛出。
    """
    # 中文说明：C# 参考项目没有 .plt 解析器，格式未知，故不猜测实现。
    raise NotImplementedError(
        ".plt 无法解析：C# 参考项目（CamReader-0.1.0）不含任何 .plt 解析器，"
        "BMS 侧 JsonToCam 也明确把 .plt 列为「未建模、原样拷贝」的数据，"
        "其记录布局没有任何权威来源。（传入 %d 字节，version=%d）"
        % (len(raw), version))


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------

#: 扩展名 → 解析函数。``.plt`` 故意不在这里 —— 它在 C# 里没有参考实现，
#: 放进表里只会把 NotImplementedError 伪装成"支持"。
_READERS: dict[str, Callable[[bytes, int], dict[str, Any]]] = {
    ".tea": read_tea,
    ".obj": read_obj,
    ".evt": read_evt,
    ".pol": read_pol,
    ".pst": read_pst,
}


def _extension(name: str) -> str:
    """从内嵌文件名或扩展名取小写扩展名（含点）。

    ``read_any`` 同时接受 ``"foo.tea"``、``".tea"``、``"tea"`` 三种写法。
    """
    s = name.strip()
    i = s.rfind(".")
    ext = s[i:].lower() if i >= 0 else s.lower()
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def read_any(name: str, raw: bytes, version: int) -> dict[str, Any] | None:
    """按内嵌文件名/扩展名分发解析。

    :param name: 内嵌文件名（如 ``"Save-Day  3 02 00 46.tea"``）或扩展名
                 （``".tea"`` / ``"tea"`` 都行）。
    :param raw:  该内嵌文件从 ``.cam`` 里切出来的**原始字节**。
    :param version: 战役版本（内嵌 ``.ver`` 的内容）。
    :returns: 解析结果 dict；**不支持的扩展名返回 ``None``**。

    支持的扩展名：``.tea`` ``.obj`` ``.evt`` ``.pol`` ``.pst``。
    ``.plt`` 明确**不支持**（C# 无参考实现），因此返回 ``None`` 而不是抛
    ``NotImplementedError``；需要显式报错时请直接调 :func:`read_plt`。
    ``.obd`` 由 :mod:`gfvfw.campaign.obd` 负责；其它扩展名（``.cmp`` ``.uni``
    ``.ver``）由本包其它模块负责 —— 这里都返回 ``None``。

    ⚠️ 注意：``raw`` 必须是**该文件真实的原始字节**。传空字节（或任何明显不足
    的短缓冲）时，函数会抛 :class:`CamDataError` 而不是返回 ``None``
    —— 因为"扩展名认识但数据坏了"和"扩展名不认识"是两回事，
    静默返回 ``None`` 会把数据损坏伪装成分发失败。
    """
    fn = _READERS.get(_extension(name))
    if fn is None:
        return None
    return fn(raw, version)
