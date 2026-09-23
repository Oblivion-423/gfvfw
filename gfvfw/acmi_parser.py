"""
GFVFW ACMI (Tacview) 解析器
===========================

把 Falcon BMS 产出的 Tacview ACMI 文件解析成结构化数据，供联队管理系统入库。

设计依据（均来自对 193 份真实 ACMI 的实测，非推测）
----------------------------------------------------
1. 容器：``.zip.acmi`` 实际是 **ZIP 归档**，内含 ``acmi.txt``；也存在裸文本 ACMI。
   本模块自动判别。
2. ⚠️ **``Pilot=`` 不是"人驾"判据（重要更正）**。
   实测同一飞行员名会出现在大量不同类型的飞机上，例如
   ``SZSZS`` 出现在 10238 条 ``F-15E-229`` 记录中，``Oblivion`` 还出现在
   ``MiG-31``（PRC）、``MiG-17PF``（U.S.）、``F/A-18E``（NATO）上。
   这显然不可能都是真人飞行。
   **``Pilot=`` 的语义是"该对象被指定了飞行员名"，AI 僚机同样带此字段。**
   因此本模块只把它作为**飞行员名来源**，并输出 ``objects_with_pilot_name``
   与 ``sorties`` 供上层按**名册**过滤；**是否人驾必须由上层判定**（见下）。
   无 ``Pilot=`` 的对象则确定是没有飞行员身份的 AI（``unnamed_ai_actors``）。
3. 增量差分：属性以「创建行 + 后续差分更新」出现，必须维护**对象状态机**。
4. 对象 ID 是 **16 进制**（``9`` 之后是 ``a``）。
5. 时间锚点：``文件名时间 = 录制结束（存档）时刻``，
   故 ``任务开始 t0 = 文件名时间 − 文件内最大时间戳 t_max``。
6. 不保留轨迹点：只累计**航程数值**（需求 §1.2 明确不做网页回放）。
7. 流式处理：实测最大文件 529 万行 / 108 MB，必须逐行处理，绝不整文件载入。

⚠️ 未解决：如何可靠区分"真人飞行"与"AI 僚机"
------------------------------------------------
``Pilot=`` 不足以判定。目前可用的辅助信号：
  - 飞行员名是否在**联队名册**中（``pilot_mappings`` 别名表）—— 主要手段
  - 阵营 / 机型是否属于联队执飞清单
  - 行为特征（真人通常有完整起降过程）
本模块不猜测，只如实输出原始字段，把判定权交给上层策略。

属性语义（实测确认）
--------------------
``T=lon|lat|alt|roll|pitch|yaw|u|v|heading``
    - 分量为空 → 该次差分未更新该分量，须沿用旧值
    - ``alt`` 为海拔（米）；实测地面单位约 0.2 m
    - ``u``/``v`` 为局部平面坐标（米，东/北），可直接用于距离累计
``Event=Shot|...``        武器发射
``Event=Destroyed|...``   对象被摧毁
``Event=Pilot|...``       飞行员状态
``Event=Message|...``     文本消息
``Event=LeftArea|...``    离开区域（与本系统无关）
``LongitudinalGForce=`` 等 过载（仅部分文件存在）
"""

from __future__ import annotations

import io
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "SortieRecord", "AcmiFileInfo", "AcmiParser", "parse_file",
    "normalize_aircraft", "parse_filename_time", "open_acmi_text",
    "DEFAULT_AIRCRAFT_ALIASES",
]

# --------------------------------------------------------------------------
# 常量与正则
# --------------------------------------------------------------------------

TS_RE = re.compile(r"^#([0-9.]+)")
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,]*)")
OBJ_ID_RE = re.compile(r"^[0-9A-Fa-f]+$")
FILENAME_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_-](\d{2})-(\d{2})-(\d{2})")
MACH_RE = re.compile(r"Mach=([0-9.]+)")

#: 海拔低于该值视为"在地面"。实测地面单位约 0.2 m，在空飞机最低 574 m。
GROUND_ALT_M = 5.0

#: 判定"速度为零"的阈值（用户口径：文件结束时速度为零即视为降落）。
#: 实测已降落飞机末值为 CAS=0.0 / Mach=0.000，在空飞机为 CAS>150 节，分离度极大。
ZERO_SPEED_CAS_KTS = 5.0
ZERO_SPEED_MACH = 0.02

#: 判定"确实升空过"的下限（用于区分"整场未起飞"与"起飞后降落"）。
AIRBORNE_CAS_KTS = 30.0
AIRBORNE_MACH = 0.10

#: 认定为"载人平台"的对象类型标记
PILOTED_TYPE_MARKERS = ("Air+FixedWing", "Air+RotaryWing")

#: 属性区起点正则 —— 匹配「从此处到行尾」的全部属性。
#:
#: ⚠️ 关键教训：T= 的值**本身可能含逗号**（实测 Air+FixedWing 的 9 分量形式为
#: ``lon,lat,alt,roll,pitch,yaw,u,v,heading``），因此不能简单地在第一个逗号处截断。
#:
#: 本正则**贪婪匹配**整个尾部属性序列 ``,KEY=VALUE``：
#:   ``KEY`` 以大写字母开头、后跟字母数字下划线，**且不能含空格**；
#:   值不得含逗号（本格式已确认）。
#: 结果是匹配到的第一段就是属性区起点，其之前即为完整的 T= 值。
#:
#: 实测反例（曾导致 Pilot 丢失）：``,Name=F-16C B52M HAF,Pilot=Oblivion,Type=...``
#: 必须整体归入属性区。此正则能正确做到。
_ATTR_TAIL_FULL_RE = re.compile(
    r"(?:,[A-Z][A-Za-z0-9_]*=[^,]*)+$"
)

#: 距离累计时的单步跳变上限（米）。超过则视为传送/重生，不计入航程。
MAX_STEP_M = 50000.0

EARTH_R_M = 6371008.8


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两点球面距离（米）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class SortieRecord:
    """一名飞行员的架次记录。

    起降判定采用**用户给定的联队口径**（非通用 ACMI 语义）：
      * 起飞 = 该飞行员名在本文件中出现 → 计 1 次
      * 降落 = 该飞行员单位在**文件结束时速度为零** → 计 1 次
    速度取 ``CAS``（校准空速，节）；``Mach`` 与海拔作为辅助证据一并保留。
    """

    raw_pilot_name: str = ""
    tactical_callsign: Optional[str] = None
    aircraft_raw_name: Optional[str] = None
    aircraft_standard_name: Optional[str] = None
    aircraft_known: bool = False
    coalition: Optional[str] = None
    object_id: Optional[str] = None

    first_seen_relative_s: Optional[float] = None
    last_seen_relative_s: Optional[float] = None

    #: **在空区间**（相对秒）。这是"日志时长"的依据 ——
    #: 从第一次有在空证据（速度）到最后一次有在空证据。
    #:
    #: ⚠️ 与"记录时长"（``AcmiFileInfo.duration_seconds``，即录制时间窗宽度）
    #: 是**两个不同的量**：录制窗含起飞前与降落后的时间，因此必然 ≥ 在空区间。
    #: 实测同一任务：记录时长 4418 s（1 小时 13 分），各人在空约 3970 s（1 小时 6 分）。
    #: 页面必须分别标注，不能互相校验。
    airborne_start_relative_s: Optional[float] = None
    airborne_end_relative_s: Optional[float] = None

    # -- 起降（用户口径） --
    takeoff_count: int = 1                 # 按定义出现即 1 次
    landing_count: int = 0
    end_cas_kts: Optional[float] = None    # 文件结束时 CAS
    end_mach: Optional[float] = None
    end_altitude_m: Optional[float] = None
    max_cas_kts: Optional[float] = None
    max_altitude_m: Optional[float] = None
    speed_sample_count: int = 0

    flight_seconds: float = 0.0
    distance_meters: float = 0.0
    distance_meters_uv: float = 0.0

    weapons_fired: int = 0
    #: ⚠️ 当前恒为 0：ACMI 的 ``Event=Destroyed`` 只标明"某对象被摧毁"，
    #: 归属到"谁的击杀"需要跨对象关联 initiator，一期未实现。
    #: 战损统计以 :attr:`deaths`（自己被摧毁）为准，不依赖跨文件配对（设计文档 §3.4）。
    kills: int = 0
    deaths: int = 0
    crashed: bool = False
    ejected: bool = False
    exceedance_count: int = 0
    max_g: Optional[float] = None
    max_mach: Optional[float] = None

    ground_collision: bool = False
    warnings: List[str] = field(default_factory=list)

    # -- 派生属性 ---------------------------------------------------------

    @property
    def landed(self) -> bool:
        """是否判定为已降落（文件结束时速度为零）。"""
        return self.landing_count > 0

    @property
    def took_off(self) -> bool:
        """是否确实升空过（有非零速度证据）。"""
        return (self.max_cas_kts is not None and self.max_cas_kts > AIRBORNE_CAS_KTS) or \
               (self.max_mach is not None and self.max_mach > AIRBORNE_MACH)

    @property
    def data_confidence(self) -> str:
        """对应 sorties.data_confidence（需求 §5.1）。"""
        if not self.speed_sample_count:
            return "estimated"
        if self.took_off and self.distance_meters > 0:
            return "exact"
        return "partial"


@dataclass
class AcmiFileInfo:
    """文件级元数据，对应 acmi_files 表。"""

    path: str
    container: str = "unknown"          # 'zip' | 'plain'
    inner_entry: Optional[str] = None
    file_bytes: int = 0
    file_version: Optional[str] = None
    data_recorder: Optional[str] = None
    data_source: Optional[str] = None
    reference_time: Optional[str] = None
    filename_time: Optional[datetime] = None
    #: 文件内**最小**的时间标记（秒）。BMS 的时间戳相对剧本纪元（``ReferenceTime``）
    #: 而非录制起点，因此首个标记常常是个大数（实测 36000.2 = 整整 10 小时）。
    min_relative_seconds: float = 0.0
    #: 文件内**最大**的时间标记（秒）。
    max_relative_seconds: float = 0.0
    timestamp_line_count: int = 0
    line_count: int = 0
    objects_with_pilot_name: int = 0
    unnamed_ai_actors: int = 0
    unknown_aircraft: List[str] = field(default_factory=list)
    sorties: List[SortieRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def object_count(self) -> int:
        """已识别的对象总数 = 带飞行员名者 + 无名 AI 载人平台。"""
        return self.objects_with_pilot_name + self.unnamed_ai_actors

    @property
    def duration_seconds(self) -> float:
        """**录制时长** = 末标记 − 首标记。

        ⚠️ 曾经写成 ``max_relative_seconds``（即末标记本身），这是错的：
        BMS 的 ACMI 时间戳以剧本纪元（``ReferenceTime``）为基点，**不是**以
        录制起点为基点，所以首个标记往往是个大数。实测一份文件从 36000.185
        录到 36004.675（真实 4.5 秒），旧算法报 36004.7 秒 ≈ 10 小时 —— 误差
        8000 倍，并连带把任务时长与"录制时间窗"一起撑大。
        """
        span = self.max_relative_seconds - self.min_relative_seconds
        return span if span > 0 else 0.0

    @property
    def time_origin_utc(self) -> Optional[datetime]:
        """时间戳的 **t=0 基准点**（剧本纪元）对应的 UTC 时刻。

        由文件名反推：``文件名时间 − 末标记``。注意它**不是**录制起点 ——
        录制起点是 :attr:`recording_start_utc`。
        """
        if self.filename_time is None:
            return None
        return self.filename_time - timedelta(seconds=self.max_relative_seconds)

    @property
    def recording_start_utc(self) -> Optional[datetime]:
        """**录制起点**的 UTC 时刻 = t=0 基准 + 首标记。"""
        return self.relative_to_utc(self.min_relative_seconds)

    @property
    def recording_end_utc(self) -> Optional[datetime]:
        """**录制终点**的 UTC 时刻 = t=0 基准 + 末标记（即文件名时间）。"""
        return self.relative_to_utc(self.max_relative_seconds)

    def relative_to_utc(self, rel_seconds: float) -> Optional[datetime]:
        start = self.time_origin_utc
        if start is None:
            return None
        return start + timedelta(seconds=rel_seconds)

    #: 兼容旧名。语义是"t=0 基准点"，**不是**任务开始时间，新代码请用
    #: :attr:`time_origin_utc` / :attr:`recording_start_utc`。
    @property
    def mission_start_utc(self) -> Optional[datetime]:
        return self.time_origin_utc

    def summary(self) -> dict:
        return {
            "file": os.path.basename(self.path),
            "container": self.container,
            "file_version": self.file_version,
            "data_recorder": self.data_recorder,
            "bytes": self.file_bytes,
            "lines": self.line_count,
            "timestamp_lines": self.timestamp_line_count,
            "objects_with_pilot_name": self.objects_with_pilot_name,
            "unnamed_ai_actors": self.unnamed_ai_actors,
            "unknown_aircraft": self.unknown_aircraft,
            "min_relative_seconds": round(self.min_relative_seconds, 1),
            "max_relative_seconds": round(self.max_relative_seconds, 1),
            "duration_seconds": round(self.duration_seconds, 1),
            "filename_time_utc": self.filename_time.isoformat() if self.filename_time else None,
            "time_origin_utc": (self.time_origin_utc.isoformat()
                                if self.time_origin_utc else None),
            "recording_start_utc": (self.recording_start_utc.isoformat()
                                    if self.recording_start_utc else None),
            "recording_end_utc": (self.recording_end_utc.isoformat()
                                  if self.recording_end_utc else None),
            "sorties": [s.__dict__ for s in self.sorties],
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
# 机型归一化
# --------------------------------------------------------------------------

#: ACMI 原始机型名 → (标准机型名, 是否联队收录)
DEFAULT_AIRCRAFT_ALIASES: Dict[str, Tuple[str, bool]] = {
    # ---- 人驾（★ 影响统计，必须精确） ----
    "F-15E-229":        ("F-15E", True),
    "F-16C B52M HAF":   ("F-16C Block 52M", True),
    "F-16CM-52":        ("F-16C Block 52", True),
    "F-16CM-40":        ("F-16C Block 40", True),
    # ---- AI（不进统计，仅归档以抑制"未知机型"告警） ----
    "F-16C-52 ROKAF":   ("F-16C Block 52", False),
    "F-16C-32 ROKAF":   ("F-16C Block 30", False),
    "F-16C-32 EAF":     ("F-16C Block 30", False),
    "F-16C-30 IAF":     ("F-16C Block 30", False),
    "F-16C B30 THK":    ("F-16C Block 30", False),
    "F-16C B30 HAF":    ("F-16C Block 30", False),
    "F-16C B40 THK":    ("F-16C Block 40", False),
    "F-16CM-50":        ("F-16C Block 50", False),
    "F-16C B50 HAF":    ("F-16C Block 50", False),
    "F-16C B50 THK":    ("F-16C Block 50", False),
    "F-16C B50+ THK":   ("F-16C Block 50+", False),
    "F-16C 50+ THK":    ("F-16C Block 50+", False),
    "F-16C B52+ HAF":   ("F-16C Block 52+", False),
    "F-16A-15 IAF":     ("F-16C Block 30", False),
    "F-16B-15 IAF":     ("F-16C Block 30", False),
    "F-15C":            ("F-15C", False),
    "F-15A IAF":        ("F-15A", False),
    "F-15K":            ("F-15E", False),
}


def normalize_aircraft(raw: Optional[str],
                       aliases: Optional[Dict[str, Tuple[str, bool]]] = None
                       ) -> Tuple[Optional[str], bool]:
    """返回 (标准机型名, 是否已收录)。未收录返回 (None, False)。"""
    if not raw:
        return None, False
    table = aliases if aliases is not None else DEFAULT_AIRCRAFT_ALIASES
    hit = table.get(raw.strip())
    return hit if hit else (None, False)


# --------------------------------------------------------------------------
# 流式读取
# --------------------------------------------------------------------------

def open_acmi_text(path: str) -> Tuple[Iterable[str], str, Optional[str]]:
    """返回 (行迭代器, 容器类型, 内部条目名)。

    调用方负责关闭返回的迭代器。ZIP 与裸文本自动判别。
    """
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        inner = names[0] if names else None
        for n in names:
            if n.lower().endswith(".txt"):
                inner = n
                break
        if inner is None:
            zf.close()
            raise ValueError("ZIP 内未找到可用条目")
        return io.TextIOWrapper(zf.open(inner), encoding="utf-8",
                               errors="replace"), "zip", inner
    return open(path, "r", encoding="utf-8", errors="replace"), "plain", None


def parse_filename_time(path: str) -> Optional[datetime]:
    """从文件名解析录制存档时刻（实测语义为 UTC）。"""
    m = FILENAME_TIME_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 对象滚动状态
# --------------------------------------------------------------------------

class _ObjState:
    """ACMI 对象的滚动状态 —— 增量差分的核心。"""

    __slots__ = (
        "oid", "type", "name", "pilot", "callsign", "coalition",
        "lon", "lat", "alt", "u", "v", "mach", "cas", "g",
        "max_cas", "max_alt",
        "prev_lon", "prev_lat", "prev_u", "prev_v",
        "first_seen", "last_seen", "last_ts",
        "flight", "airborne_start", "airborne_end",
        "takeoffs", "landings", "distance_geo", "distance_uv",
        "speed_samples",
        "shots", "destroyed", "crashed", "ejected", "exceed", "maxg", "maxmach",
        "counted",
    )

    def __init__(self, oid: str) -> None:
        self.oid = oid
        self.type = self.name = self.pilot = self.callsign = self.coalition = None
        self.lon = self.lat = self.alt = self.u = self.v = None
        self.mach = self.cas = self.g = None
        self.max_cas: Optional[float] = None
        self.max_alt: Optional[float] = None
        self.prev_lon = self.prev_lat = None
        self.prev_u = self.prev_v = None
        self.first_seen: Optional[float] = None
        self.last_seen: Optional[float] = None
        self.last_ts = 0.0
        self.flight = 0.0
        #: 在空区间（相对秒）—— 日志时长的依据，见 SortieRecord 的说明
        self.airborne_start: Optional[float] = None
        self.airborne_end: Optional[float] = None
        self.takeoffs = 0
        self.landings = 0
        self.distance_geo = 0.0
        self.distance_uv = 0.0
        self.speed_samples = 0
        self.shots = 0
        self.destroyed = False
        self.crashed = False
        self.ejected = False
        self.exceed = 0
        self.maxg: Optional[float] = None
        self.maxmach: Optional[float] = None
        self.counted = False

    @property
    def is_piloted(self) -> bool:
        return self.pilot is not None

    @property
    def is_piloted_platform(self) -> bool:
        return bool(self.type) and any(m in self.type for m in PILOTED_TYPE_MARKERS)


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------

class AcmiParser:
    """单个 ACMI 文件的解析器。"""

    def __init__(self,
                 aircraft_aliases: Optional[Dict[str, Tuple[str, bool]]] = None,
                 ground_alt_m: float = GROUND_ALT_M,
                 duration_alert_s: float = 12 * 3600.0,
                 collect_events: bool = False):
        self.aliases = (aircraft_aliases if aircraft_aliases is not None
                        else DEFAULT_AIRCRAFT_ALIASES)
        self.ground_alt_m = ground_alt_m
        self.duration_alert_s = duration_alert_s
        self.collect_events = collect_events

    # -- 主入口 -----------------------------------------------------------

    def parse(self, path: str) -> AcmiFileInfo:
        info = AcmiFileInfo(path=path)
        info.filename_time = parse_filename_time(path)
        if info.filename_time is None:
            info.warnings.append("文件名中未找到 YYYY-MM-DD_HH-MM-SS 形式的时间")
        try:
            info.file_bytes = os.path.getsize(path)
        except OSError:
            info.file_bytes = 0

        lines, container, inner = open_acmi_text(path)
        info.container = container
        info.inner_entry = inner

        objs: Dict[str, _ObjState] = {}
        unknown_aircraft: Dict[str, int] = {}
        cur_ts = 0.0
        saw_acmi_header = False

        try:
            for raw in lines:
                info.line_count += 1
                line = raw.rstrip("\r\n")
                if not line or line.startswith("//"):
                    continue

                # ---- 时间戳行 ----
                if line[0] == "#":
                    info.timestamp_line_count += 1
                    m = TS_RE.match(line)
                    if m:
                        try:
                            cur_ts = float(m.group(1))
                        except ValueError:
                            continue
                        if cur_ts > info.max_relative_seconds:
                            info.max_relative_seconds = cur_ts
                        # 首个标记直接落位，之后取真正的最小值。
                        # ⚠️ 不能只写 `if cur_ts < info.min_relative_seconds`：
                        #    min 的默认值是 0.0，而标记恒为正，那样它会永远停在 0，
                        #    时长就又退化成"末标记"了 —— 正是本 bug 的成因。
                        if (info.timestamp_line_count == 1
                                or cur_ts < info.min_relative_seconds):
                            info.min_relative_seconds = cur_ts
                    continue

                # ---- 头部全局行 ----
                if "," not in line:
                    if "=" in line:
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip()
                        if k == "FileType" and "acmi" in v.lower():
                            saw_acmi_header = True
                        elif k == "FileVersion":
                            info.file_version = v
                        elif k == "DataRecorder":
                            info.data_recorder = v
                        elif k == "DataSource":
                            info.data_source = v
                        elif k == "ReferenceTime":
                            info.reference_time = v
                    continue

                oid, rest = line.split(",", 1)
                if not OBJ_ID_RE.match(oid):
                    continue

                # ---- 分离 T= 分量与属性区 ----
                tparts: List[str] = []
                body = rest
                if rest.startswith("T="):
                    after = rest[2:]
                    m = _ATTR_TAIL_FULL_RE.search(after)
                    if m:
                        tstr, body = after[:m.start()], after[m.start() + 1:]
                    else:
                        tstr, body = after, ""
                    parts = tstr.split("|") if "|" in tstr else tstr.split(",")
                    # 逐分量 strip：9 分量形式里数值本身带前导空格
                    tparts = [p.strip() for p in parts]

                st = objs.get(oid)
                if st is None:
                    st = _ObjState(oid)
                    objs[oid] = st

                delta_t = cur_ts - st.last_ts

                # ---- 应用 T 分量（空值 = 未更新，沿用旧值） ----
                #
                # 分量语义（实测确认，9 分量形式）：
                #   [0]=经度 [1]=纬度 [2]=海拔(米) [3]=roll [4]=pitch [5]=yaw
                #   [6]=局部东向坐标u(米) [7]=局部北向坐标v(米) [8]=航向
                #
                # ⚠️ 注意：[2] 海拔实测**每个对象仅在生成时出现一次**，之后不再更新，
                #    因此不能依赖"高度跳变"来判定离地/接地。v1 暂无可靠起降信号，
                #    起降次数由位置是否离开地面起始点推断（见 _advance_piloted）。
                if tparts:
                    if len(tparts) > 0 and tparts[0]:
                        try:
                            st.lon = float(tparts[0])
                        except ValueError:
                            pass
                    if len(tparts) > 1 and tparts[1]:
                        try:
                            st.lat = float(tparts[1])
                        except ValueError:
                            pass
                    if len(tparts) > 2 and tparts[2]:
                        try:
                            st.alt = float(tparts[2])
                        except ValueError:
                            pass
                    if len(tparts) > 6 and tparts[6]:
                        try:
                            st.u = float(tparts[6])
                        except ValueError:
                            pass
                    if len(tparts) > 7 and tparts[7]:
                        try:
                            st.v = float(tparts[7])
                        except ValueError:
                            pass

                # ---- 属性（含事件） ----
                if body:
                    kvs = dict(KV_RE.findall(body))
                    if kvs:
                        had_pilot = st.pilot is not None
                        had_ident = st.name is not None or st.type is not None
                        self._apply_props(st, kvs, info, unknown_aircraft)

                        # 首次获得身份时计数
                        if not had_pilot and st.pilot is not None:
                            info.objects_with_pilot_name += 1
                        if not had_ident and (st.name or st.type):
                            if st.pilot is None and st.is_piloted_platform:
                                info.unnamed_ai_actors += 1

                # ---- 状态推进 ----
                if st.is_piloted:
                    if st.first_seen is None:
                        st.first_seen = cur_ts
                    st.last_seen = cur_ts
                    self._advance_piloted(st, cur_ts, delta_t)

                st.last_ts = cur_ts
        finally:
            closer = getattr(lines, "close", None)
            if closer:
                closer()

        # ---- 结算架次 ----
        for st in objs.values():
            if st.is_piloted:
                info.sorties.append(self._build_sortie(st, info))

        # ⚠️ 内容校验：必须是真正的 ACMI，不能静默接受垃圾数据。
        #    没有 ACMI 头部且找不到任何对象 → 明确报错（_store 会记为 failed）。
        if not saw_acmi_header and not objs:
            raise ValueError(
                "不是有效的 ACMI 文件：缺少 FileType=text/acmi/tacview 头部，"
                "且未解析到任何对象（容器=%s 条目=%s）"
                % (info.container, info.inner_entry))

        if info.objects_with_pilot_name and not info.sorties:
            info.warnings.append("检测到带 Pilot 名的对象但未生成架次，请检查属性区解析")

        for nm, cnt in sorted(unknown_aircraft.items(), key=lambda x: -x[1]):
            info.unknown_aircraft.append(nm)
            info.warnings.append(f"未收录机型（{cnt} 次）: {nm}")

        # ⚠️ 用**真实录制时长**（末−首）判阈值，不能用末标记本身。
        #    用末标记会让任何"首个标记不为 0"的 BMS 文件都误报超时。
        if info.duration_seconds > self.duration_alert_s:
            info.warnings.append(
                "录制时长 %.2f 小时超过 %.1f 小时阈值，建议核对时间基准"
                % (info.duration_seconds / 3600.0, self.duration_alert_s / 3600.0))

        return info

    # -- 属性应用 ---------------------------------------------------------

    @staticmethod
    def _apply_props(st: _ObjState, kvs: Dict[str, str],
                     info: AcmiFileInfo, unknown: Dict[str, int]) -> None:
        if "Type" in kvs:
            st.type = kvs["Type"] or None
        if "Name" in kvs:
            st.name = kvs["Name"] or None
        if "Pilot" in kvs:
            st.pilot = kvs["Pilot"] or None
        if "CallSign" in kvs:
            st.callsign = kvs["CallSign"] or None
        if "Coalition" in kvs:
            st.coalition = kvs["Coalition"] or None

        if "Mach" in kvs:
            try:
                v = float(kvs["Mach"])
                if st.maxmach is None or v > st.maxmach:
                    st.maxmach = v
            except ValueError:
                pass

        if "CAS" in kvs:
            try:
                v = float(kvs["CAS"])
                st.cas = v
                st.speed_samples += 1
                if st.max_cas is None or v > st.max_cas:
                    st.max_cas = v
            except ValueError:
                pass
        elif "IAS" in kvs:
            # CAS 缺失时退化为 IAS（同为节，量级相近）
            try:
                v = float(kvs["IAS"])
                st.cas = v
                st.speed_samples += 1
                if st.max_cas is None or v > st.max_cas:
                    st.max_cas = v
            except ValueError:
                pass

        if st.alt is not None and (st.max_alt is None or st.alt > st.max_alt):
            st.max_alt = st.alt

        g = None
        for key in ("LongitudinalGForce", "VerticalGForce", "LateralGForce"):
            if key in kvs:
                try:
                    gv = abs(float(kvs[key]))
                    g = gv if g is None else max(g, gv)
                except ValueError:
                    pass
        if g is not None:
            if st.maxg is None or g > st.maxg:
                st.maxg = g
            if g > 9.0:
                st.exceed += 1

        if "Event" in kvs:
            ev = kvs["Event"]
            kind = ev.split("|", 1)[0]
            if kind == "Shot" and st.is_piloted:
                st.shots += 1
            elif kind == "Destroyed":
                st.destroyed = True
                low = ev.lower()
                if "crash" in low or "ground" in low or "terrain" in low:
                    st.crashed = True
            elif kind == "Pilot" and st.is_piloted:
                low = ev.lower()
                if "eject" in low:
                    st.ejected = True

    # -- 状态推进：离地 / 接地 / 时长 / 距离 -------------------------------

    def _advance_piloted(self, st: _ObjState, cur_ts: float, delta_t: float) -> None:
        # --- 双路距离累计：u/v 平面坐标 与 经纬度 haversine，互为校验 ---
        if st.u is not None and st.v is not None:
            if st.prev_u is not None and st.prev_v is not None:
                step = math.hypot(st.u - st.prev_u, st.v - st.prev_v)
                if 0.0 < step < MAX_STEP_M:
                    st.distance_uv += step
            st.prev_u, st.prev_v = st.u, st.v

        if st.lon is not None and st.lat is not None:
            if st.prev_lon is not None and st.prev_lat is not None:
                step = haversine_m(st.prev_lon, st.prev_lat, st.lon, st.lat)
                if 0.0 < step < MAX_STEP_M:
                    st.distance_geo += step
            st.prev_lon, st.prev_lat = st.lon, st.lat

        # --- 飞行时长：有非零速度证据时累计 ---
        # 用户口径下"起飞"由出现即定，但时长仍需时间区间，故按速度证据累计。
        if delta_t > 0 and self._looks_airborne(st):
            st.flight += delta_t
            # 在空区间的起止。
            #
            # ⚠️ 起点取**上一个采样时刻**（``st.last_ts``）而不是当前时刻：
            #    上面把 ``delta_t``（上一个采样 → 当前采样）整段计入了飞行时长，
            #    因为飞机是在这段区间内的某一刻离地的。若起点取当前时刻，
            #    区间宽度会**小于**累计出来的飞行时长 ——
            #    进而出现"多人同飞任务的日志时长比其中单人的还短"这种自相矛盾。
            #    （自校验里正是这条断言抓住了该错误：合成样例
            #     flight_seconds=1500 而区间只有 1200。）
            if st.airborne_start is None:
                st.airborne_start = st.last_ts
            st.airborne_end = cur_ts

    @staticmethod
    def _looks_airborne(st: _ObjState) -> bool:
        """是否有"在空中"的速度证据。"""
        if st.cas is not None and st.cas > AIRBORNE_CAS_KTS:
            return True
        if st.mach is not None and st.mach > AIRBORNE_MACH:
            return True
        return False

    # -- 结算架次 ---------------------------------------------------------

    def _build_sortie(self, st: _ObjState, info: AcmiFileInfo) -> SortieRecord:
        std, known = normalize_aircraft(st.name, self.aliases)

        # ---- 降落判定（用户口径：文件结束时速度为零）----
        # 实测分离度极大：已降落 CAS=0.0/Mach=0.000，在空 CAS>150/Mach>0.5。
        zero_speed = False
        if st.cas is not None:
            zero_speed = st.cas <= ZERO_SPEED_CAS_KTS
        elif st.mach is not None:
            zero_speed = st.mach <= ZERO_SPEED_MACH
        landing = 1 if zero_speed else 0

        rec = SortieRecord(
            raw_pilot_name=st.pilot or "",
            tactical_callsign=st.callsign,
            aircraft_raw_name=st.name,
            aircraft_standard_name=std,
            aircraft_known=known,
            coalition=st.coalition,
            object_id=st.oid,
            first_seen_relative_s=st.first_seen,
            last_seen_relative_s=st.last_seen,
            airborne_start_relative_s=st.airborne_start,
            airborne_end_relative_s=st.airborne_end,
            takeoff_count=1,                 # 用户口径：出现即计 1 次
            landing_count=landing,
            end_cas_kts=st.cas,
            end_mach=st.mach,
            end_altitude_m=st.alt,
            max_cas_kts=st.max_cas,
            max_altitude_m=st.max_alt,
            speed_sample_count=st.speed_samples,
            flight_seconds=st.flight,
            distance_meters=st.distance_geo,
            distance_meters_uv=st.distance_uv,
            weapons_fired=st.shots,
            deaths=1 if st.destroyed else 0,
            crashed=st.crashed,
            ejected=st.ejected,
            exceedance_count=st.exceed,
            max_g=st.maxg,
            max_mach=st.maxmach,
            ground_collision=st.crashed,
        )

        # 未降落但最后时刻未接近文件末尾 → 可能中途退出/被击落/换机
        if not zero_speed and st.last_seen is not None:
            gap = info.max_relative_seconds - st.last_seen
            if gap > 300.0:
                rec.warnings.append(
                    "最后活动距文件结束 %.0f 秒，无法用末速判定降落（可能被击落或中途退出）"
                    % gap)

        # 速度证据不足
        if st.speed_samples == 0:
            rec.warnings.append("本文件未取到速度数据（CAS/IAS/Mach），起降判定不可靠")

        # 双路距离交叉校验
        if st.distance_geo > 0 and st.distance_uv > 0:
            lo, hi = sorted((st.distance_geo, st.distance_uv))
            if hi > 0 and (hi - lo) / hi > 0.10:
                rec.warnings.append(
                    "距离双路校验不一致：经纬度 %.0f m vs 平面坐标 %.0f m（偏差 %.0f%%）"
                    % (st.distance_geo, st.distance_uv, 100.0 * (hi - lo) / hi))
        if not known and st.name:
            rec.warnings.append("机型未收录: %s" % st.name)
        return rec

    # -- 单对象便捷入口（供测试） -----------------------------------------

    def parse_stream(self, lines: Iterable[str], path: str = "<stream>") -> AcmiFileInfo:
        """从字符串行序列解析，便于单元测试。

        ⚠️ ``path`` 同时用于文件名时间解析，因此临时文件会沿用 ``basename(path)``，
        否则 ``mission_start_utc`` 会因文件名不匹配而失效。
        """
        import tempfile
        base = os.path.basename(path) or "stream.acmi"
        tmpdir = tempfile.mkdtemp(prefix="acmi_stream_")
        tmp = os.path.join(tmpdir, base)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            info = self.parse(tmp)
            info.path = path
            return info
        finally:
            try:
                os.unlink(tmp)
                os.rmdir(tmpdir)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------

def parse_file(path: str, **kw) -> AcmiFileInfo:
    """解析单个 ACMI 文件。"""
    return AcmiParser(**kw).parse(path)
