"""BMS 剧场数据表（Theater data tables）加载器。

移植自 ``CamReader/Theater/*.cs``：
``ClassTable.cs`` ``UcdTable.cs`` ``VcdTable.cs`` ``WcdTable.cs``
``FcdTable.cs`` ``CampObjTable.cs`` ``OcdTypeTable.cs`` ``TheaterInfo.cs``，
路径规则取自 ``CamReader/Core/Settings.cs``（第 74–94 行的派生路径）。

剧场目录
--------
``Settings.cs`` 的规则是：剧场名为 ``Korea`` 时用 ``<install>\\Data``，
否则用 ``<install>\\Data\\Add-On <剧场名>``。所有表都在这两个根下：

===================  ==================================================
文件                 含义
===================  ==================================================
``TerrData/Objects/Falcon4_CT.xml``    类表（Class Table），100 多列
``TerrData/Objects/Falcon4_UCD.xml``   单位定义（Unit Class Data）
``TerrData/Objects/Falcon4_VCD.xml``   载具定义（Vehicle Class Data）
``TerrData/Objects/Falcon4_WCD.xml``   武器定义（Weapon Class Data）
``TerrData/Objects/Falcon4_RCD.xml``   雷达定义（Radar Class Data）
``TerrData/Objects/Falcon4_FCD.xml``   特征定义（Feature Class Data）
``TerrData/Objects/ObjectiveRelatedData/OCD_%05d/OCD_%05d.XML``
                                       目标公共数据，提供 ``CtIdx`` 指针
``Campaign/CampObjData.xml``           战役目标清单（名字/坐标/OcdIndex）
``Campaign/strings.txt``               索引 → 文本（制表符分隔）
``TerrData/<剧场>/NewTerrain/Theater.txt``  投影参数（可能不存在）
===================  ==================================================

⚠️ 实测到的两个与 C# 源码不一致之处（已在本模块中兼容）
--------------------------------------------------------
1. **大小写不固定**。Korea 是 ``Falcon4_CT.xml`` / ``CampObjData.XML`` /
   ``Strings.txt``，Hellas 是 ``FALCON4_CT.XML`` / ``FALCON4_UCD.XML`` /
   ``falcon4_rcd.xml``。``Settings.cs`` 用固定拼写拼路径，靠 Windows 大小写
   不敏感才能跑通；Linux 上会全部找不到。本模块统一做大小写不敏感查找。
2. **``Add-On Hellas 2026`` 没有 ``Campaign/Strings.txt`` 与
   ``NewTerrain/Theater.txt``**，这两项缺失是常态，不是错误。

编码
----
CT/UCD/VCD/WCD/FCD/RCD/OCD 均为无 BOM 的 UTF-8（文件头即声明
``encoding="utf-8"``）；Hellas 的 ``CampObjData.XML`` **带 UTF-8 BOM**。
因此所有文本都用 ``encoding="utf-8-sig"`` 读取（有 BOM 自动剥离，无 BOM
等价于 utf-8），XML 则交给 :mod:`xml.etree.ElementTree` 从二进制流自行判编码
与剥 BOM，避免"声明与实际字节不符"时抛异常。

性能
----
``Falcon4_CT.xml`` 有 8–11 MB、约 5000–7000 条 ``<CT>``，每条 40 余个子元素。
全部七张表用 :func:`xml.etree.ElementTree.iterparse` 流式读取：每个子元素
处理完立即 ``elem.clear()``，闭合块随父元素一起释放，内存与耗时都与文件大小
线性相关（实测 Hellas 全量加载约 1 秒）。**不使用** ``ET.parse``（整树驻留，
CT 会占几百 MB）。

给战役状态导出（``Output/JsonExporter.cs``）用的追加接口
--------------------------------------------------------
* :meth:`TheaterData.string` —— ``strings.txt`` 按索引；任务名就是
  ``string(MISSION_NAME_BASE + mission_code)`` 即 ``string(300 + code)``
* :meth:`TheaterData.get_callsign` —— 飞行呼号（``"Jedi 5"``），索引基准 2000
* :meth:`TheaterData.aircraft_entry` —— ``VcdTable.GetAircraftEntry`` 的完整复刻
* :meth:`TheaterData.all_camp_objs` / :meth:`TheaterData.camp_obj` —— 原始坐标
  （**英尺，绝不换算**）
* :meth:`TheaterData.ocd_type_name` —— ``OcdIndex`` → 目标类别名（喂 ``typeName``）
"""

from __future__ import annotations

import codecs
import io
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.etree import ElementTree as ET

__all__ = [
    "CtEntry", "UcdEntry", "VcdEntry", "WcdEntry", "RcdLethality", "FcdEntry",
    "CampObjEntry", "TheaterInfo", "TheaterData",
    "load_theater", "clear_cache", "objective_type_name_by_type",
    "main_role_name", "guidance_name", "damage_type_name",
    "CT_ENTITY_TYPE_NAMES", "OBJECTIVE_TYPE_NAMES", "SAM_PREFIXES",
    "SAM_ALIASES", "SAM_FALLBACKS", "SAM_THREAT_SYSTEMS",
    "MISSION_NAME_BASE", "CALLSIGN_BASE",
    "DOM_AIR", "DOM_LAND", "DOM_SEA", "DOM_SUB",
]

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量 —— 逐条对应 C# 源码中的字面量表
# --------------------------------------------------------------------------

#: CT 表的 ``Domain`` 取值（ClassTable.cs 第 10 行注释）
DOM_AIR, DOM_LAND, DOM_SEA, DOM_SUB = 2, 3, 4, 7

#: ``sam_radii`` 里 ``short`` 环占 ``long`` 环的比例。
#:
#: ⚠️ **这是本模块的推断值，不是从 BMS 数据里读出来的。** 逐列核对过
#: ``Falcon4_WCD.xml``（只有单一 ``Range`` 列）、``Falcon4_RCD.xml``（探测距离
#: 与杀伤率，无射程上下限）、``Falcon4_UCD.xml``（``Rng_Air`` 是"交战距离"，
#: 与 WCD 并发存在且更小，含义不明确，未采用）之后确认：**BMS 数据表里没有
#: 近界（最小交战距离）这一列**。任务要求的 ``long/medium/short`` 三层结构
#: 在数据层只支持两层，第三层只能用约定比例补出（0.5 = 半射程），
#: 并在此显式标注，便于日后换成真实参数或直接忽略 ``short``。
SAM_SHORT_FRACTION = 0.5

#: ``CtEntry.entity_type`` 的语义（ClassTable.cs 第 16 行注释）
CT_ENTITY_TYPE_NAMES: dict[int, str] = {
    1: "Feature",
    2: "CampaignBase",
    3: "Objective",
    4: "Unit(air)",
    5: "Unit(ground)",
    6: "weapon",
}

#: 目标类别号 → 名称。逐字照搬 OcdTypeTable.cs 第 13–42 行的 ``TypeNames``。
#: 缺号（12/23/24/26/27…）在 BMS 里没有定义，故不列出。
OBJECTIVE_TYPE_NAMES: dict[int, str] = {
    0: "SAM Site (Dedicated)",
    1: "Airbase",
    2: "Airstrip",
    3: "Army Base",
    4: "Range",
    5: "Border",
    6: "Bridge",
    7: "Chemical Plant",
    8: "City",
    9: "Headquarters",
    10: "Depot",
    11: "Factory",
    13: "Fortification",
    14: "Misc",
    15: "Intersection",
    16: "Nav Beacon",
    17: "Nuclear Plant",
    18: "Mountain Pass",
    19: "Port",
    20: "Power / Dam",
    21: "Radar Site",
    22: "Radio Tower",
    25: "Refinery",
    28: "Town",
    29: "Village",
    30: "Special",
    31: "SAM / AAA Site",
}

#: 任务名（mission name）在 ``strings.txt`` 里的起始索引。
#: JsonExporter.cs 第 446 行按 ``300 + missionCode`` 取名，故 ``string(300 + code)``
#: 即可（``300`` 是 BMS 的固定约定）。
MISSION_NAME_BASE = 300

#: 调用号（callsign）在 ``strings.txt`` 里的起始索引。
#: StringsTable.cs 第 17 行 ``const int CallsignBase = 2000``：
#: 存档里字节 ``CallsignId`` 是相对 2000 的偏移（``0`` → 索引 ``2000``）。
CALLSIGN_BASE = 2000

#: WCD 武器名前缀 → 目标名前缀（WcdTable.cs 第 55–60 行 ``SamAliases``）
SAM_ALIASES: dict[str, str] = {
    "MIM-23": "HAWK",
    "MIM-104": "PATRIOT",
    "MIM-14": "NIKE",
}

#: 兜底射程（WcdTable.cs 第 62–66 行 ``SamFallbacks``）
SAM_FALLBACKS: dict[str, float] = {
    "THAAD": 120.0,
    "KN-06": 60.0,
}

#: 认作防空系统的武器名前缀（WcdTable.cs 第 68–76 行 ``SamPrefixes``）
SAM_PREFIXES: frozenset[str] = frozenset({
    "SA-2", "SA-3", "SA-4", "SA-5", "SA-6", "SA-7", "SA-8", "SA-9",
    "SA-10", "SA-11", "SA-12A", "SA-12B", "SA-13", "SA-14", "SA-15",
    "SA-16", "SA-17", "SA-18", "SA-19", "SA-20",
    "SA-N-3", "SA-N-6", "SA-N-7", "SA-N-9",
    "MIM-23", "MIM-104", "MIM-14",
    "Crotale",
})

#: 判据：WCD 里 ``Hit_Air > 0`` 且 ``Range > 0`` 的武器才计入射程统计
#: （WcdTable.cs 第 143 行）。**注意 ``SA-12A``/``SA-12B`` 合并成 ``SA-12``。**
_SAM_COLLAPSE: dict[str, str] = {"SA-12A": "SA-12", "SA-12B": "SA-12"}

#: RCD 名字前缀 → 目标名（WcdTable.cs 第 172–175 行 ``rcdAliases``）
_RCD_ALIASES: dict[str, str] = {"KSAM": "KM-SAM"}

#: 有已知威胁环的防空系统全集（WCD 的 SAM_PREFIXES + RCD 补充的 KM-SAM +
#: 兜底项）。用于给每个系统生成 ``sam_radii`` 条目。
SAM_THREAT_SYSTEMS: frozenset[str] = frozenset(
    {_SAM_COLLAPSE.get(p, p) for p in SAM_PREFIXES}
    | {SAM_ALIASES.get(p, p) for p in SAM_PREFIXES}
    | set(_RCD_ALIASES.values())
    | set(SAM_FALLBACKS)
)

#: WCD 里被识别为"专用防空目标"的 WCD 名字前缀 → 目标名。``SA-2`` 这类
#: 短程系统在 WCD 的 ``Name`` 里写作 ``"SA-2 Missile"``，取首空格前的 token。
_TOKEN_SPLIT = re.compile(r"\s+")

# --------------------------------------------------------------------------
# 编码与文件辅助
# --------------------------------------------------------------------------

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

_XML_DECL = re.compile(rb"<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9_.\-]+)[\"']")


def detect_encoding(path: Path) -> str:
    """嗅探文本文件编码。

    顺序：BOM → XML 声明里的 ``encoding=`` → UTF-8 严格试解码 →
    最后回退 ``cp1252``。返回可直接交给 ``str.encode``/``open`` 的名字。

    :param path: 待嗅探文件。
    """
    with open(path, "rb") as fh:
        head = fh.read(4096)
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc
    m = _XML_DECL.search(head)
    if m:
        try:
            return m.group(1).decode("ascii")
        except UnicodeDecodeError:  # pragma: no cover - 声明本身非 ASCII
            pass
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def read_text(path: Path) -> str | None:
    """读取文本文件，自动处理 BOM 与声明的编码。读不到返回 ``None``。

    与 C# 的差异：``StringsTable.cs`` 固定用 ``Encoding.UTF8`` 打开，
    遇到真·非 UTF-8 文件会整片变问号；这里按实际编码读取。
    """
    if not path.is_file():
        return None
    enc = detect_encoding(path)
    try:
        return path.read_text(encoding=enc, errors="replace")
    except (OSError, LookupError) as exc:
        _log.warning("剧场表读取失败 %s：%s", path, exc)
        return None


def _find_file(directory: Path, *names: str) -> Path | None:
    """在目录中做**大小写不敏感**的文件查找。

    ``Settings.cs`` 用固定拼写拼路径，只靠 Windows 大小写不敏感才成立；
    Hellas 的 ``FALCON4_CT.XML`` 与 ``falcon4_rcd.xml`` 就是反例。
    先按原名试，再退回一次目录扫描（大小写折叠比较）。

    :param directory: 目录。
    :param names: 候选名（含扩展名），按优先级。
    """
    if not directory.is_dir():
        return None
    for name in names:
        p = directory / name
        if p.is_file():
            return p
    wanted = {n.lower() for n in names}
    try:
        for entry in os.scandir(directory):
            if entry.is_file() and entry.name.lower() in wanted:
                return Path(entry.path)
    except OSError as exc:  # pragma: no cover - 权限/IO 异常
        _log.warning("目录扫描失败 %s：%s", directory, exc)
    return None


def _find_dir(directory: Path, name: str) -> Path | None:
    """在目录中做大小写不敏感的**子目录**查找（同上）。"""
    p = directory / name
    if p.is_dir():
        return p
    try:
        low = name.lower()
        for entry in os.scandir(directory):
            if entry.is_dir() and entry.name.lower() == low:
                return Path(entry.path)
    except OSError:  # pragma: no cover
        return None
    return None


def _find_addon_root(install: Path, theater: str) -> Path | None:
    """在 ``<install>/Data`` 下找 ``Add-On <theater>`` 目录。

    ``Settings.cs`` 拼的是 ``"Add-On " + theater``，即**目录名必须正好**是
    ``Add-On Hellas``。实测本机目录名可能是 ``Add-On Hellas 2026``（带年份），
    C# 那种拼法会直接找不到、静默退化成 Korea 的表。这里的匹配规则：

    1. ``Data/<theater>`` 本身是目录（调用方直接给了完整目录名）；
    2. ``Data/Add-On <theater>`` 存在（与 C# 完全一致，优先）；
    3. 去掉空格、忽略大小写后，目录名形如 ``addon<key>`` 且 ``<key>`` 以
       剧场名开头；多个候选取**名字最短**的（``Add-On Hellas`` 优先于
       ``Add-On Hellas 2026``）。

    :param install: BMS 安装根目录。
    :param theater: 剧场名。可只给前缀（``"Hellas"``），也可给完整目录名
        （``"Add-On Hellas 2026"``，此时前缀里的 ``Add-On`` 会被剥掉）。
    """
    data = install / "Data"
    for candidate in (theater, theater[len("Add-On "):] if theater.lower().startswith("add-on ") else None):
        if candidate:
            direct = data / candidate
            if direct.is_dir():
                return direct
    exact = data / ("Add-On %s" % theater)
    if exact.is_dir():
        return exact
    key = theater.replace(" ", "").lower()
    key = key[5:] if key.startswith("addon") else key
    if not key:
        return None
    candidates: list[str] = []
    try:
        for entry in os.scandir(data):
            if not entry.is_dir():
                continue
            nm = entry.name.replace(" ", "").lower()
            if nm.startswith("addon") and nm[5:].startswith(key):
                candidates.append(entry.name)
    except OSError:  # pragma: no cover
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda s: (len(s), s))
    return data / candidates[0]


def _find_addon_dir(base: Path, theater: str) -> Path | None:
    """在某个目录下按剧场名找子目录（大小写不敏感、允许前缀匹配）。

    用于 ``TerrData/<剧场名>/NewTerrain/Theater.txt`` 这类路径：Korea 是
    ``TerrData/Korea/NewTerrain``，Add-On 剧场里可能叫别的名字。同样剥掉
    ``Add-On `` 前缀后再比对（调用方可能传的是完整目录名）。

    :param base: 上级目录，如 ``<root>/TerrData``。
    :param theater: 剧场名。
    """
    candidates = [theater]
    if theater.lower().startswith("add-on "):
        candidates.append(theater[len("Add-On "):])
    for name in candidates:
        direct = _find_dir(base, name)
        if direct is not None:
            return direct
    if not base.is_dir():
        return None
    keys = {c.replace(" ", "").lower() for c in candidates if c}
    try:
        for entry in os.scandir(base):
            if not entry.is_dir():
                continue
            nm = entry.name.replace(" ", "").lower()
            nm = nm[5:] if nm.startswith("addon") else nm
            if any(nm.startswith(k) for k in keys):
                return Path(entry.path)
    except OSError:  # pragma: no cover
        return None
    return None


# --------------------------------------------------------------------------
# 数值解析 —— 复刻 C# 的 "TryParse 失败保持默认值" 语义
# --------------------------------------------------------------------------

def _int(text: str | None, default: int = 0) -> int:
    """``int.TryParse`` 等价物：失败返回 ``default``（**不抛异常**）。"""
    if text is None:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default


def _float(text: str | None, default: float = 0.0) -> float:
    """``float.TryParse(..., InvariantCulture)`` 等价物。"""
    if text is None:
        return default
    try:
        return float(text.strip())
    except ValueError:
        return default


def _local(tag: str) -> str:
    """剥掉命名空间前缀：``{ns}Name`` → ``Name``。"""
    i = tag.rfind("}")
    return tag[i + 1:] if i >= 0 else tag


#: XML 1.0 ``Char`` 之外的字符：C0 控制符、DEL、U+FFFD（解码器的替换符）。
#: 注意 ``\t`` ``\n`` ``\r`` 是合法的，必须保留。
_BAD_XML_CHARS_RX = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")


def sanitize_text(text: str) -> str:
    """把已解码文本里 XML 1.0 不允许的字符换成空格。

    只替换非法字符，不删除 —— ``ALP\\xa0310-G`` 变成 ``ALP 310-G`` 而不是
    ``ALP310-G``，避免把相邻单词粘在一起。

    :param text: 已解码的文本片段。
    """
    return _BAD_XML_CHARS_RX.sub(" ", text)


class _SanitizingStream:
    """把二进制流包一层：边读边把 XML 非法字符替换成空格，供 expat 使用。

    为什么要包一层而不是整文件读进内存：``Falcon4_CT.xml`` 有 8–11 MB，
    流式处理时峰值内存只与**单块**相关。做法是在 UTF-8 增量解码器之上替换：
    ``errors="replace"`` 把孤立高位字节解码成 ``U+FFFD``，再把 ``U+FFFD`` 与
    其余非法控制符一并换成空格 —— **长度守恒**（1 字符换 1 字符），
    因此 ``tell()`` 与字节偏移仍能对上，expat 报错的行列号依旧可用。

    实测 Hellas 的 ``falcon4_rcd.xml`` 第 2626 行有 ``ALP\\xa0310-G``
    （裸 Latin-1 不换行空格）：C# 的 ``XmlReader`` 在这里会抛
    "not well-formed"，而 ``LoadRcd`` 没有 try/catch，CamReader 会直接崩。
    本模块靠这一层把整张 RCD 表救回来。
    """

    __slots__ = ("_raw", "_decoder", "_buf", "_eof")

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._eof = False

    def read(self, size: int = -1) -> str:
        while True:
            if size is None or size < 0:
                if not self._eof:
                    self._buf += self._decoder.decode(self._raw.read() or b"", True)
                    self._eof = True
                out, self._buf = self._buf, ""
                return sanitize_text(out)
            if len(self._buf) >= size or self._eof:
                out, self._buf = self._buf[:size], self._buf[size:]
                return sanitize_text(out)
            chunk = self._raw.read(1 << 20)
            if not chunk:
                self._buf += self._decoder.decode(b"", True)
                self._eof = True
            else:
                self._buf += self._decoder.decode(chunk, False)


def iter_blocks(
    stream: Any, tag: str,
) -> Iterator[tuple[dict[str, str], dict[str, str]]]:
    """流式遍历 XML，逐个产出 ``(块属性, {子元素名: 文本})``。

    用 :func:`xml.etree.ElementTree.iterparse` + ``elem.clear()``，
    峰值内存只与**单个块**相关，与文件总大小无关（CT 有 8–11 MB）。
    字节流先过 :class:`_SanitizingStream` 以容忍厂商写脏的文件。

    属性表整份返回（不只是 ``Num``）：不同表的"主键"属性名不一样 ——
    ``CT/UCD/VCD/WCD/RCD/FCD`` 是 ``Num``，``CampObjData`` 是 ``CampId``。

    :param stream: 已打开的**二进制**文件对象。也可直接传 ``bytes``。
    :param tag: 块元素的标签名，如 ``"CT"``；其子元素文本按名收集。
    """
    source = stream if callable(getattr(stream, "read", None)) \
        else io.BytesIO(stream)
    for event, elem in ET.iterparse(_SanitizingStream(source),
                                    events=("start", "end")):
        if event == "start":
            continue
        if elem.tag != tag:
            continue
        fields: dict[str, str] = {}
        for child in elem:
            text = child.text
            fields[_local(child.tag)] = text if text is not None else ""
        attrs = dict(elem.attrib)
        elem.clear()
        yield attrs, fields


def _safe(loader: Callable[..., Any], path: Path | None, *,
          label: str) -> Any:
    """调用一个表加载器；文件缺失或 XML 损坏都只记日志并返回空表。

    C# 各 ``Load()`` 里对"文件不存在"的处理并不统一（``CampObjTable`` 直接
    静默返回空表，``UcdTable`` 打日志），对 XML 损坏则只有 ``CampObjTable``
    做了 ``try/catch``。本模块统一：**任何表加载失败都不抛异常**。
    """
    try:
        return loader(path)
    except (ET.ParseError, OSError, UnicodeDecodeError, ValueError) as exc:
        _log.warning("剧场表 %s 加载失败（%s）：%s", label, path, exc)
        return None


# --------------------------------------------------------------------------
# 记录类型
# --------------------------------------------------------------------------

@dataclass
class CtEntry:
    """``Falcon4_CT.xml`` 的一条 ``<CT>`` 块（ClassTable.cs ``CtEntry``）。

    ``Falcon4_CT.xml`` 是 BMS 的**类表**：每种可出现在存档里的实体都有一条
    ``<CT Num="n">``，其中 ``Domain/Class/Type/SubType/Specific/Owner`` 六个
    byte 就是 ``.uni`` 流开头那个 ``uint16 entityTypeId`` 指向的"类标识"。

    每块有 40 余个子元素，本类只保留 C# ``CtEntry`` 用到的 8 个 + ``Id``。
    """

    num: int = 0
    domain: int = 0
    #: C# 字段名是 ``Class``，Python 里改名为 ``cls``（``class`` 是关键字）
    cls: int = 0
    type: int = 0
    subtype: int = 0
    specific: int = 0
    owner: int = 0
    entity_type: int = 0
    #: ``Falcon4_UCD.xml`` 的下标；``-1`` 表示该实体没有单位定义。
    #: （ClassTable.cs 第 17 行注释写成 "index into UCD table"，
    #: 实测是 UCD 的 ``Num``，而 UCD 的 ``Num`` 与文件内次序一致。）
    entity_idx: int = -1

    @property
    def entity_type_name(self) -> str:
        """VU 基类名，如 ``"Objective"``；未知值返回 ``"EntityType7"``。"""
        return CT_ENTITY_TYPE_NAMES.get(self.entity_type, "EntityType%d" % self.entity_type)

    @property
    def domain_name(self) -> str:
        """域名字：``Air`` / ``Land`` / ``Sea`` / ``Sub``。"""
        return {DOM_AIR: "Air", DOM_LAND: "Land", DOM_SEA: "Sea",
                DOM_SUB: "Sub"}.get(self.domain, "Domain%d" % self.domain)


@dataclass
class UcdEntry:
    """``Falcon4_UCD.xml`` 的一条 ``<UCD>`` 块（UcdTable.cs ``UcdEntry``）。

    单位定义：地面营/旅/师、舰艇编队的编制与探测/交战参数。
    """

    num: int = -1
    name: str = ""
    #: CT 下标（UCD 自己的 ``CtIdx`` 字段，保留原始列便于排查）
    ct_idx: int = 0
    #: ``VehicleCtIdx_0``（VcdTable 用它把 UCD 接到 VCD 的载具名）
    vehicle_ct_idx0: int = 0
    #: 全部 ``VehicleCtIdx_0..15`` 中 ``> 0`` 的值（UcdTable.cs 第 78–84 行）
    vehicle_ct_idx_all: list[int] = field(default_factory=list)
    #: 2=Air 3=Land 4=Sea；由 CT 回填（UcdTable.cs ``GetEntryByEntityTypeId``）
    domain: int = 0
    move_type: int = 0
    move_speed: int = 0      # kph
    max_range: int = 0       # km
    fuel: int = 0
    fuel_rate: int = 0
    main_role: int = 0
    radar_vehicle: bool = False
    squadron_stores_idx: int = 0
    unit_icon: int = 0
    #: 交战距离（海里），按目标类型分列
    rng_air: int = 0
    rng_no_move: int = 0
    rng_foot: int = 0
    rng_wheeled: int = 0
    rng_tracked: int = 0
    rng_naval: int = 0
    #: 命中概率（百分比），按目标类型分列
    hit_air: int = 0
    hit_no_move: int = 0
    hit_foot: int = 0
    #: 探测距离（海里）
    det_air: float = 0.0
    det_no_move: float = 0.0
    det_foot: float = 0.0


@dataclass
class VcdEntry:
    """``Falcon4_VCD.xml`` 的一条 ``<VCD>`` 块（VcdTable.cs ``VcdEntry``）。

    载具（飞机/车辆/舰船）型号定义。``ct_idx`` 是 CT 下标，
    UCD 的 ``VehicleCtIdx_n`` 就是拿它去 VCD 里查名字。
    """

    num: int = -1
    name: str = ""
    ct_idx: int = -1
    nctr: str = ""           # RWR 识别串，如 "F16"
    max_speed: int = 0       # kph
    max_alt: int = 0         # 百英尺
    min_alt: int = 0
    cruise_alt: int = 0
    max_weight: int = 0      # 磅
    empty_weight: int = 0
    fuel_weight: int = 0
    fuel_rate: int = 0
    radar_cs: float = 0.0    # 雷达反射截面
    radar_idx: int = 0       # → RCD
    hit_points: int = 0
    number_of_crew: int = 0
    in_service_start: int = 0
    in_service_end: int = 0
    hit_air: int = 0
    rng_air: int = 0
    det_air: float = 0.0

    # ── C# 字段名别名 ────────────────────────────────────────────────
    # JsonExporter.cs 直接打印 ``VcdEntry`` 的字段；为免调用方再写一层映射，
    # 这里按 VcdTable.cs 的原字段名（首字母大写）提供只读属性。
    # 全部是 Python 侧原始值，**不做任何单位换算**。

    @property
    def Name(self) -> str:          # noqa: N802 - 刻意沿用 C# 字段名
        """VCD ``Name``（机型名，如 ``"F-16C B40 THK"``）。"""
        return self.name

    @property
    def Nctr(self) -> str:          # noqa: N802
        """VCD ``NCTR``（RWR 识别串，如 ``"F16"``）。"""
        return self.nctr

    @property
    def MaxSpeed(self) -> int:      # noqa: N802
        """VCD ``MaxSpeed``（kph）。"""
        return self.max_speed

    @property
    def MaxAlt(self) -> int:        # noqa: N802
        """VCD ``MaxAlt``（单位：百英尺，原始值）。"""
        return self.max_alt

    @property
    def CruiseAlt(self) -> int:     # noqa: N802
        """VCD ``CruiseAlt``（单位：百英尺，原始值）。"""
        return self.cruise_alt

    @property
    def FuelWeight(self) -> int:    # noqa: N802
        """VCD ``FuelWeight``（磅）。"""
        return self.fuel_weight

    @property
    def NumberOfCrew(self) -> int:  # noqa: N802
        """VCD ``NumberOfCrew``。"""
        return self.number_of_crew

    @property
    def InServiceStart(self) -> int:  # noqa: N802
        """VCD ``InServiceStart``（年份）。"""
        return self.in_service_start

    @property
    def InServiceEnd(self) -> int:  # noqa: N802
        """VCD ``InServiceEnd``（年份）。"""
        return self.in_service_end

    @property
    def RadarIdx(self) -> int:      # noqa: N802
        """VCD ``RadarIdx`` → RCD 的 ``Num``。"""
        return self.radar_idx

    @property
    def RadarCs(self) -> float:     # noqa: N802
        """VCD ``RadarCs``（雷达反射截面，原始值）。"""
        return self.radar_cs

    @property
    def HitAir(self) -> int:        # noqa: N802
        """VCD ``Hit_Air``（对空命中概率）。"""
        return self.hit_air

    @property
    def RngAir(self) -> int:        # noqa: N802
        """VCD ``Rng_Air``（对空交战距离，海里）。"""
        return self.rng_air


@dataclass
class WcdEntry:
    """``Falcon4_WCD.xml`` 的一条 ``<WCD>`` 块（WcdTable.cs ``WcdEntry``）。"""

    num: int = -1
    name: str = ""
    ct_idx: int = 0
    #: 最大射程（海里）—— ``sam_radii`` 的 "long" 环就取自它
    range: float = 0.0
    damage_type: int = 0
    guidance: int = 0        # 位掩码，见 :func:`guidance_name`
    blast_radius: int = 0
    strength: int = 0
    rariety: int = 0
    weight: int = 0
    hit_air: int = 0
    hit_no_move: int = 0
    flags: int = 0
    in_service_start: int = 0
    in_service_end: int = 0
    sim_weapon_data_idx: int = 0


@dataclass
class RcdLethality:
    """``Falcon4_RCD.xml`` 的一条 ``<RCD>`` 块（WcdTable.cs ``RcdLethality``）。

    雷达的探测与杀伤参数。
    """

    num: int = -1
    name: str = ""
    #: 探测距离（海里）—— ``VehicleCtIdx_0``/``RadarIdx`` 指向这里
    detection_range: float = 0.0
    high_alt_lethality: float = 0.0
    low_alt_lethality: float = 0.0
    jamming_penalty: float = 0.0
    look_down_penalty: float = 0.0
    notch_penalty: float = 0.0
    notch_speed: float = 0.0
    chaff_chance: float = 0.0
    beam_width: float = 0.0
    scan_width: float = 0.0
    rwr_symbol: int = 0
    flags: int = 0


@dataclass
class FcdEntry:
    """``Falcon4_FCD.xml`` 的一条 ``<FCD>`` 块（FcdTable.cs ``FcdEntry``）。

    特征/建筑定义（桥梁、工厂、雷达站……）的耐久与修复参数。
    """

    num: int = -1
    name: str = ""
    hit_points: int = 0
    repair_time: int = 0     # 分钟
    radar_idx: int = 0       # → RCD
    flags: int = 0
    display_priority: int = 0


@dataclass
class CampObjEntry:
    """``Campaign/CampObjData.xml`` 的一条 ``<CampObj>``（CampObjTable.cs）。

    战役目标清单。``ocd_index`` 指到 ``OCD_<n>/OCD_<n>.XML``，后者再给
    ``CtIdx`` 指回 ``Falcon4_CT.xml`` —— 这是解析目标类别的完整链路。
    """

    camp_id: int = 0
    name: str = ""
    ocd_index: int = 0
    heading: float = 0.0
    #: ⚠️ ``pos_x/pos_y/pos_z`` **单位是英尺，与 XML 里 ``<PositionX/Y/Z>`` 的
    #: 数值完全一致，本模块绝不换算。** 转成战役网格（1 网格 ≈ 3280.84 英尺）
    #: 由独立的坐标模块负责 —— X/Y 轴命名与地图视角是反的，混在一起极易搞错。
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0

    # JsonExporter.cs 打印的是 ``PositionX/Y/Z``；这里的同义属性取值与
    # ``pos_x/pos_y/pos_z`` **完全相同**（原始英尺，只读）。
    @property
    def position_x(self) -> float:
        """``<PositionX>`` 原始值（英尺，未换算）。"""
        return self.pos_x

    @property
    def position_y(self) -> float:
        """``<PositionY>`` 原始值（英尺，未换算）。"""
        return self.pos_y

    @property
    def position_z(self) -> float:
        """``<PositionZ>`` 原始值（英尺，未换算）。"""
        return self.pos_z


@dataclass
class TheaterInfo:
    """``NewTerrain/Theater.txt`` 的投影参数（TheaterInfo.cs）。

    文件格式是 ``键 = 值`` 的文本行，只认 5 个键。Hellas 等 Add-On 剧场
    **没有**这个文件，此时 :attr:`TheaterData.theater_info` 为 ``None``。
    """

    theater_name: str = ""
    size_km: float = 0.0
    center_lat: float = 0.0
    center_lon: float = 0.0
    projection_string: str = ""


# --------------------------------------------------------------------------
# 名称解码小工具（WcdTable.cs 的两个静态解码器 + UcdTable.cs 的角色表）
# --------------------------------------------------------------------------

def guidance_name(g: int) -> str:
    """WCD ``Guidance`` 位掩码 → 可读串（WcdTable.cs ``GuidanceName``）。

    :param g: 位掩码：1=INS 2=GPS 4=IIR 8=IR 16=SARH 32=ARH 64=TOO
        128=TV 256=LASER 512=ARM。全 0 返回 ``"UNGUIDED"``。
    """
    parts: list[str] = []
    for bit, name in ((1, "INS"), (2, "GPS"), (4, "IIR"), (8, "IR"),
                      (16, "SARH"), (32, "ARH"), (64, "TOO"),
                      (128, "TV"), (256, "LASER"), (512, "ARM")):
        if g & bit:
            parts.append(name)
    return "+".join(parts) if parts else "UNGUIDED"


def damage_type_name(d: int) -> str:
    """WCD ``DamageType`` → 名称（WcdTable.cs ``DamageTypeName``）。"""
    return {
        0: "None", 1: "Penetration", 2: "HighExplosive", 3: "Heave",
        4: "Incendiary", 5: "Proximity", 6: "Kinetic", 7: "Hydrostatic",
        8: "Chemical", 9: "Nuclear",
    }.get(d, "Type%d" % d)


def main_role_name(role: int, domain: int) -> str:
    """UCD ``MainRole`` → 名称（UcdTable.cs ``MainRoleName``）。

    ⚠️ ``MainRole`` 的整数含义**按域区分**：Air 的 3 是 "Attack"，
    Land 的 3 是 "AirAssault/Airborne"。调用时必须同时给出 CT 的 ``Domain``。

    :param role: UCD 的 ``MainRole`` 值。
    :param domain: CT 的 ``Domain``（2=Air 3=Land 4=Sea 7=Sub）。
    """
    if domain == DOM_AIR:
        return {0: "Fighter", 1: "Fighter", 2: "AirCav/Airlift/AttackHeli",
                3: "Attack", 4: "Attack", 5: "Bomber", 6: "ECM",
                7: "Attack", 8: "ASW", 9: "Fighter", 10: "Recon",
                11: "Airlift", 12: "AWACS", 13: "JSTAR",
                14: "Tanker"}.get(role, "Air_Role%d" % role)
    if domain == DOM_LAND:
        return {0: "HQ/Supply", 1: "Armor", 2: "Amphibian/Marine/SpecOps",
                3: "AirAssault/Airborne", 4: "Infantry", 5: "AirDefense",
                6: "Artillery/Missile", 7: "Engineer",
                8: "Cavalry"}.get(role, "Land_Role%d" % role)
    if domain == DOM_SEA:
        return {0: "NavalGroup"}.get(role, "Sea_Role%d" % role)
    if domain == DOM_SUB:
        return "Submarine"
    return "Domain%d_Role%d" % (domain, role)


def objective_type_name_by_type(type_id: int) -> str:
    """目标类别号 → 名称（OcdTypeTable.cs ``GetTypeName``）。

    未定义号返回 ``"Type<n>"``（照搬 C#，便于发现表外取值）。

    :param type_id: ``CtEntry.type``（又称 objective type）。
    """
    return OBJECTIVE_TYPE_NAMES.get(type_id, "Type%d" % type_id)


# --------------------------------------------------------------------------
# 各表加载器（每个都对应一个 C# 类的 Load()）
# --------------------------------------------------------------------------

#: CT 表里本模块保留的字段 → CtEntry 属性
_CT_FIELDS: dict[str, str] = {
    "Domain": "domain", "Class": "cls", "Type": "type",
    "SubType": "subtype", "Specific": "specific", "Owner": "owner",
    "EntityType": "entity_type",
}
#: 需要 ``int.TryParse`` 容错的整型字段
_UCD_INT_FIELDS: dict[str, str] = {
    "MoveType": "move_type", "MoveSpeed": "move_speed",
    "MaxRange": "max_range", "Fuel": "fuel", "FuelRate": "fuel_rate",
    "MainRole": "main_role", "SquadronStoresIdx": "squadron_stores_idx",
    "UnitIcon": "unit_icon", "Rng_Air": "rng_air", "Rng_NoMove": "rng_no_move",
    "Rng_Foot": "rng_foot", "Rng_Wheeled": "rng_wheeled",
    "Rng_Tracked": "rng_tracked", "Rng_Naval": "rng_naval",
    "Hit_Air": "hit_air", "Hit_NoMove": "hit_no_move", "Hit_Foot": "hit_foot",
}
_UCD_FLOAT_FIELDS: dict[str, str] = {
    "Det_Air": "det_air", "Det_NoMove": "det_no_move", "Det_Foot": "det_foot",
}
_VCD_INT_FIELDS: dict[str, str] = {
    "CtIdx": "ct_idx", "MaxSpeed": "max_speed", "MaxAlt": "max_alt",
    "MinAlt": "min_alt", "CruiseAlt": "cruise_alt", "MaxWeight": "max_weight",
    "EmptyWeight": "empty_weight", "FuelWeight": "fuel_weight",
    "FuelRate": "fuel_rate", "RadarIdx": "radar_idx",
    "HitPoints": "hit_points", "NumberOfCrew": "number_of_crew",
    "InServiceStart": "in_service_start", "InServiceEnd": "in_service_end",
    "Hit_Air": "hit_air", "Rng_Air": "rng_air",
}
_WCD_INT_FIELDS: dict[str, str] = {
    "CtIdx": "ct_idx", "DamageType": "damage_type", "Guidance": "guidance",
    "BlastRadius": "blast_radius", "Strength": "strength",
    "Rariety": "rariety", "Weight": "weight", "Hit_Air": "hit_air",
    "Hit_NoMove": "hit_no_move", "Flags": "flags",
    "InServiceStart": "in_service_start", "InServiceEnd": "in_service_end",
    "SimWeaponDataIdx": "sim_weapon_data_idx",
}
_RCD_FLOAT_FIELDS: dict[str, str] = {
    "DetectionRange": "detection_range",
    "HighAltLethality": "high_alt_lethality",
    "LowAltLethality": "low_alt_lethality",
    "JammingPenalty": "jamming_penalty",
    "LookDownPenalty": "look_down_penalty", "NotchPenalty": "notch_penalty",
    "NotchSpeed": "notch_speed", "ChaffChance": "chaff_chance",
    "BeamWidth": "beam_width", "ScanWidth": "scan_width",
}


def _apply(entry: Any, fields: dict[str, str], ints: dict[str, str] | None = None,
           floats: dict[str, str] | None = None,
           strings: dict[str, str] | None = None) -> None:
    """把一次 ``iter_blocks`` 的结果写进 dataclass（缺列保持默认值）。"""
    if strings:
        for tag, attr in strings.items():
            if tag in fields:
                setattr(entry, attr, fields[tag].strip())
    if ints:
        for tag, attr in ints.items():
            if tag in fields:
                setattr(entry, attr, _int(fields[tag]))
    if floats:
        for tag, attr in floats.items():
            if tag in fields:
                setattr(entry, attr, _float(fields[tag]))


def load_class_table(path: Path | None) -> list[CtEntry]:
    """读 ``Falcon4_CT.xml`` → :class:`CtEntry` 列表（ClassTable.cs ``Load``）。

    返回的是**按文件次序**的列表，下标即 ``Num``（实测 CT 的 ``Num`` 从 0
    连续递增，Korea 0..5262、Hellas 0..6999）；``ClassTable.Get`` 正是拿
    ``entityTypeId - 100`` 当下标用的，所以两者等价。

    :param path: 文件路径；``None`` 或不存在时返回空列表。
    """
    if path is None:
        return []
    out: list[CtEntry] = []
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "CT"):
            e = CtEntry(num=_int(attrs.get("Num"), len(out)), entity_idx=-1)
            _apply(e, fields, ints=_CT_FIELDS)
            if "EntityIdx" in fields:
                e.entity_idx = _int(fields["EntityIdx"], -1)
            out.append(e)
    return out


def load_ucd_table(path: Path | None) -> dict[int, UcdEntry]:
    """读 ``Falcon4_UCD.xml`` → ``{Num: UcdEntry}``（UcdTable.cs ``Load``）。

    ``Num < 0`` 的块被跳过（C# 第 72/119 行的 ``cur.Num >= 0`` 判据）。
    """
    out: dict[int, UcdEntry] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "UCD"):
            num = _int(attrs.get("Num"), -1)
            if num < 0:
                continue
            e = UcdEntry(num=num)
            e.name = fields.get("Name", "").strip()
            _apply(e, fields, ints=_UCD_INT_FIELDS, floats=_UCD_FLOAT_FIELDS)
            e.ct_idx = _int(fields.get("CtIdx"))
            # VehicleCtIdx_0..15：只看 > 0 的（UcdTable.cs 第 80–84 行）
            v0 = 0
            for tag, val in fields.items():
                if not tag.startswith("VehicleCtIdx_"):
                    continue
                v = _int(val)
                if v > 0:
                    e.vehicle_ct_idx_all.append(v)
                if tag == "VehicleCtIdx_0":
                    v0 = v
            e.vehicle_ct_idx0 = v0
            # RadarVehicle 是字符串比较 "1"，不是数值比较（第 95 行）
            e.radar_vehicle = fields.get("RadarVehicle", "").strip() == "1"
            out[num] = e
    return out


def load_vcd_table(path: Path | None) -> dict[int, VcdEntry]:
    """读 ``Falcon4_VCD.xml`` → ``{Num: VcdEntry}``（VcdTable.cs ``Load``）。

    VCD 同时有 ``Num`` 与 ``CtIdx`` 两个键；本函数只按 ``Num`` 返回，
    ``CtIdx`` 索引由 :class:`TheaterData` 在装配时另建。
    """
    out: dict[int, VcdEntry] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "VCD"):
            num = _int(attrs.get("Num"), -1)
            if num < 0:
                continue
            e = VcdEntry(num=num)
            e.name = fields.get("Name", "").strip()
            e.nctr = fields.get("NCTR", "").strip()
            _apply(e, fields, ints=_VCD_INT_FIELDS,
                   floats={"Det_Air": "det_air", "RadarCs": "radar_cs"})
            out[num] = e
    return out


def load_wcd_table(path: Path | None) -> dict[int, WcdEntry]:
    """读 ``Falcon4_WCD.xml`` → ``{Num: WcdEntry}``（WcdTable.cs ``LoadWcd``）。"""
    out: dict[int, WcdEntry] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "WCD"):
            num = _int(attrs.get("Num"), -1)
            if num < 0:
                continue
            e = WcdEntry(num=num)
            e.name = fields.get("Name", "").strip()
            _apply(e, fields, ints=_WCD_INT_FIELDS,
                   floats={"Range": "range"})
            out[num] = e
    return out


def load_rcd_table(path: Path | None) -> dict[int, RcdLethality]:
    """读 ``Falcon4_RCD.xml`` → ``{Num: RcdLethality}``（WcdTable.cs ``LoadRcd``）。"""
    out: dict[int, RcdLethality] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "RCD"):
            num = _int(attrs.get("Num"), -1)
            if num < 0:
                continue
            e = RcdLethality(num=num)
            e.name = fields.get("Name", "").strip()
            _apply(e, fields, ints={"RwrSymbol": "rwr_symbol", "Flags": "flags"},
                   floats=_RCD_FLOAT_FIELDS)
            out[num] = e
    return out


def load_fcd_table(path: Path | None) -> dict[int, FcdEntry]:
    """读 ``Falcon4_FCD.xml`` → ``{Num: FcdEntry}``（FcdTable.cs ``Load``）。"""
    out: dict[int, FcdEntry] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "FCD"):
            num = _int(attrs.get("Num"), -1)
            if num < 0:
                continue
            e = FcdEntry(num=num)
            e.name = fields.get("Name", "").strip()
            _apply(e, fields, ints={"HitPoints": "hit_points",
                                    "RepairTime": "repair_time",
                                    "RadarIdx": "radar_idx",
                                    "Flags": "flags",
                                    "DisplayPriority": "display_priority"})
            out[num] = e
    return out


def load_camp_obj_table(path: Path | None) -> dict[int, CampObjEntry]:
    """读 ``Campaign/CampObjData.xml`` → ``{CampId: CampObjEntry}``。

    移植自 CampObjTable.cs ``Load``。注意 ``CampName`` 里可能是**非 ASCII**
    （实测 Korea 的 ``T‘osan-up Bridge`` 用了 U+2018 单引号），故按实际编码读。
    """
    out: dict[int, CampObjEntry] = {}
    if path is None:
        return out
    with open(path, "rb") as fh:
        for attrs, fields in iter_blocks(fh, "CampObj"):
            # 注意：CampObj 的主键属性名是 CampId，不是 Num
            e = CampObjEntry(camp_id=_int(attrs.get("CampId"), 0))
            e.name = fields.get("CampName", "").strip()
            e.ocd_index = _int(fields.get("OcdIndex"))
            e.heading = _float(fields.get("Heading"))
            e.pos_x = _float(fields.get("PositionX"))
            e.pos_y = _float(fields.get("PositionY"))
            e.pos_z = _float(fields.get("PositionZ"))
            out[e.camp_id] = e
    return out


def load_strings_table(path: Path | None) -> dict[int, str]:
    """读 ``Campaign/strings.txt`` → ``{索引: 文本}``（StringsTable.cs ``Load``）。

    格式为制表符分隔的 ``索引\\t文本``；没有制表符或索引非数字的行跳过。
    调用号（callsign）索引基准是 2000（``CallsignId 0`` → 索引 2000）。
    """
    out: dict[int, str] = {}
    if path is None:
        return out
    text = read_text(path)
    if text is None:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        tab = line.find("\t")
        if tab <= 0:
            continue
        try:
            idx = int(line[:tab].strip())
        except ValueError:
            continue
        out[idx] = line[tab + 1:].strip()
    return out


def load_theater_info(path: Path | None) -> TheaterInfo | None:
    """读 ``NewTerrain/Theater.txt``（TheaterInfo.cs ``Load``）。

    只认 5 个键：``Theater name`` / ``Theater size in KM`` /
    ``Center latitude`` / ``Center longitude`` / ``Projection string``。
    文件不存在返回 ``None``（Hellas 等 Add-On 剧场就没有该文件）。
    """
    text = read_text(path) if path is not None else None
    if text is None:
        return None
    info = TheaterInfo()
    for raw in text.splitlines():
        line = raw.strip()
        eq = line.find("=")
        if eq < 0:
            continue
        key = line[:eq].strip()
        val = line[eq + 1:].strip()
        if key == "Theater name":
            info.theater_name = val
        elif key == "Theater size in KM":
            info.size_km = _float(val)
        elif key == "Center latitude":
            info.center_lat = _float(val)
        elif key == "Center longitude":
            info.center_lon = _float(val)
        elif key == "Projection string":
            info.projection_string = val
    return info


def load_ocd_index(
    ocd_folder: Path | None, ct_entries: list[CtEntry],
) -> tuple[dict[int, int], dict[int, int], dict[int, list[int]]]:
    """扫描 ``ObjectiveRelatedData`` 下的 ``OCD_%05d`` 目录。

    移植自 OcdTypeTable.cs ``Load``，返回三张表：

    * ``ocd_to_ct``  —— ``OCD 号 → CtIdx``，CtIdx 是 ``Falcon4_CT.xml`` 的下标；
    * ``ct_to_type`` —— ``CT 下标 → 目标类别号``，只收 ``Domain==3 and Class==4``
      的 CT 记录（即 BMS 里的"目标模板"）；
    * ``ct_to_ocds`` —— 反向索引 ``CT 下标 → [OCD 号…]``（本模块新增，供
      :meth:`TheaterData.objective_type_name` 从 CT 记录反查类别）。

    ``OCD_<n>/OCD_<n>.XML`` 里只有根节点 ``<OCD Num="0">`` 的 ``<CtIdx>``
    有效（子节点 ``<OCD Num="1..">`` 是子目标），故只取第一个块。

    :param ocd_folder: ``TerrData/Objects/ObjectiveRelatedData`` 目录。
    :param ct_entries: 已加载的类表（用于建 ``ct_to_type``）。
    """
    ocd_to_ct: dict[int, int] = {}
    ct_to_type: dict[int, int] = {}
    ct_to_ocds: dict[int, list[int]] = {}

    for e in ct_entries:
        if e.domain == DOM_LAND and e.cls == 4:
            ct_to_type[e.num] = e.type

    if ocd_folder is None or not ocd_folder.is_dir():
        return ocd_to_ct, ct_to_type, ct_to_ocds

    try:
        names = [d.name for d in os.scandir(ocd_folder) if d.is_dir()]
    except OSError as exc:  # pragma: no cover
        _log.warning("OCD 目录列举失败 %s：%s", ocd_folder, exc)
        return ocd_to_ct, ct_to_type, ct_to_ocds

    for name in names:
        if not name.startswith("OCD_"):
            continue
        try:
            ocd_num = int(name[4:])
        except ValueError:
            continue
        xml = _find_file(ocd_folder / name, "OCD_%05d.XML" % ocd_num,
                         "OCD_%d.XML" % ocd_num)
        if xml is None:
            continue
        try:
            with open(xml, "rb") as fh:
                root = ET.parse(_SanitizingStream(fh)).getroot()
        except (ET.ParseError, OSError) as exc:
            _log.warning("OCD 文件解析失败 %s：%s", xml, exc)
            continue
        block = root.find("OCD")
        if block is None:
            continue
        ct_el = block.find("CtIdx")
        if ct_el is None or ct_el.text is None:
            continue
        try:
            ct_idx = int(ct_el.text.strip())
        except ValueError:
            continue
        ocd_to_ct[ocd_num] = ct_idx
        ct_to_ocds.setdefault(ct_idx, []).append(ocd_num)

    return ocd_to_ct, ct_to_type, ct_to_ocds


def _resolve_sam_prefix(wcd_name: str) -> str | None:
    """WCD 武器名 → 防空系统名（WcdTable.cs ``ResolveSamPrefix``）。

    取名字第一个空格前的 token（``"SA-2 Missile"`` → ``"SA-2"``），
    在 :data:`SAM_PREFIXES` 里才认；``SA-12A/B`` 合并为 ``SA-12``，
    再经 :data:`SAM_ALIASES` 映射（``MIM-23`` → ``HAWK`` 等）。
    """
    if not wcd_name:
        return None
    token = _TOKEN_SPLIT.split(wcd_name.strip())[0]
    if token not in SAM_PREFIXES:
        return None
    key = _SAM_COLLAPSE.get(token, token)
    return SAM_ALIASES.get(key, key)


def build_sam_radii(
    weapons: dict[int, WcdEntry], radars: dict[int, RcdLethality],
) -> tuple[dict[str, dict[str, float]], dict[str, RcdLethality]]:
    """按 WcdTable.cs 的算法推导防空系统威胁环。

    WCD 侧（``LoadWcd`` 第 142–159 行）：对每条 ``Hit_Air > 0`` 且
    ``Range > 0`` 的武器，解析出防空系统名，取**该系统所有武器的最大射程**。

    RCD 侧（``LoadRcd`` 第 216–262 行）：名字以 ``KSAM`` 开头的雷达把
    ``DetectionRange`` 补给 ``KM-SAM``（仅在 WCD 没给过时）；随后对每个尚无
    威胁数据的系统做一次**best-effort 模糊匹配**（RCD 名包含系统名，或系统名
    包含 RCD 名的首个词）。

    最后应用 :data:`SAM_FALLBACKS` 兜底（``ApplyFallbacks``）。

    ⚠️ C# 里 ``SamRadii`` 是 ``string → float``（一个系统一个射程）。
    本函数的返回值是**每个系统一个小字典**，键固定为
    ``long`` / ``medium`` / ``short``：

    * ``long``   = WCD 交战射程（或 RCD 补的值、或兜底值），海里 —— 实测值；
    * ``medium`` = 该系统名字匹配到的 RCD ``DetectionRange``，海里 —— 实测值；
      匹配不到为 ``0.0``；
    * ``short``  = ``long × SAM_SHORT_FRACTION`` —— **推断值**，见该常量的说明。

    :return: ``(sam_radii, sam_threat)``；``sam_threat`` 是系统名 → RCD 记录。
    """
    raw: dict[str, float] = {}
    for w in weapons.values():
        if not w.name or w.hit_air <= 0 or w.range <= 0.0:
            continue
        prefix = _resolve_sam_prefix(w.name)
        if prefix is None:
            continue
        if w.range > raw.get(prefix, 0.0):
            raw[prefix] = w.range

    sam_radii: dict[str, float] = dict(raw)
    sam_threat: dict[str, RcdLethality] = {}

    for r in radars.values():
        if not r.name or r.detection_range <= 0.0:
            continue
        for alias_from, alias_to in _RCD_ALIASES.items():
            if r.name.upper().startswith(alias_from.upper()):
                sam_radii.setdefault(alias_to, r.detection_range)
                sam_threat.setdefault(alias_to, r)
                break

    for key in list(sam_radii):
        if key in sam_threat:
            continue
        for r in radars.values():
            if not r.name:
                continue
            first_word = _TOKEN_SPLIT.split(r.name.strip())[0]
            if key.upper() in r.name.upper() or (
                    first_word and first_word.upper() in key.upper()):
                sam_threat[key] = r
                break

    for key, value in SAM_FALLBACKS.items():
        sam_radii.setdefault(key, value)

    # KM-SAM 这类"只有 RCD 数据、没有 WCD 武器"的系统也要出现在结果里
    for key in sam_threat:
        sam_radii.setdefault(key, 0.0)

    out: dict[str, dict[str, float]] = {}
    for key, long_nm in sam_radii.items():
        radar = sam_threat.get(key)
        medium = radar.detection_range if radar is not None else 0.0
        # short 是推断值：BMS 表里没有近界列，见 SAM_SHORT_FRACTION 说明
        short = long_nm * SAM_SHORT_FRACTION if long_nm > 0.0 else 0.0
        out[key] = {"long": float(long_nm), "medium": float(medium),
                    "short": float(short)}
    return out, sam_threat


# --------------------------------------------------------------------------
# 总装
# --------------------------------------------------------------------------

#: ``(install_path.resolve(), theater.lower())`` → TheaterData
_CACHE: dict[tuple[str, str], "TheaterData"] = {}


def clear_cache() -> None:
    """清空 :meth:`TheaterData.load` 的进程内缓存。"""
    _CACHE.clear()


class TheaterData:
    """一个剧场（``Korea`` 或某个 Add-On）的全套数据表。

    典型用法::

        th = TheaterData.load(r"G:\\BMS\\Falcon BMS 4.38", "Hellas")
        entry = th.ct_get(101)                  # .uni 流里的 entityTypeId
        if entry is not None and entry.entity_idx >= 0:
            weapon = th.unit_def(entry.entity_idx)
        obj = th.camp_obj(4)
        if obj is not None:
            print(th.objective_type_for_ocd(obj.ocd_index), th.string(10))

    所有表都在构造时一次性读完并常驻内存（Korea 合计约 20 MB 文本，
    解析后约几 MB 对象），因此 :meth:`load` 默认按
    ``(install_path, 已解析剧场根目录)`` 缓存，同一进程内重复调用零成本，
    且 ``"Hellas"`` 与 ``"Add-On Hellas 2026"`` 这类别名会命中同一条缓存。
    """

    __slots__ = (
        "install_path", "theater", "root", "entries", "theater_info",
        "ucd", "vcd", "wcd", "rcd", "fcd", "camp_obj_data", "strings",
        "_ucd_by_ct", "_vcd_by_ct", "_ocd_to_ct", "_ct_to_type", "_ct_to_ocds",
        "_ocd_to_camp", "_sam_radii", "_sam_threat", "_missing",
    )

    def __init__(
        self,
        install_path: Path,
        theater: str,
        root: Path,
        *,
        entries: list[CtEntry],
        ucd: dict[int, UcdEntry],
        vcd: dict[int, VcdEntry],
        wcd: dict[int, WcdEntry],
        rcd: dict[int, RcdLethality],
        fcd: dict[int, FcdEntry],
        camp_obj: dict[int, CampObjEntry],
        strings: dict[int, str],
        theater_info: TheaterInfo | None,
        ocd_to_ct: dict[int, int],
        ct_to_type: dict[int, int],
        ct_to_ocds: dict[int, list[int]],
        sam_radii: dict[str, dict[str, float]],
        sam_threat: dict[str, RcdLethality],
        missing: list[str],
    ) -> None:
        self.install_path = install_path
        self.theater = theater
        #: 已解析的剧场根目录（``<install>\\Data`` 或 ``<install>\\Data\\Add-On …``）
        self.root = root
        self.entries = entries
        self.theater_info = theater_info
        self.ucd = ucd
        self.vcd = vcd
        self.wcd = wcd
        self.rcd = rcd
        self.fcd = fcd
        self.camp_obj_data = camp_obj
        self.strings = strings
        self._ucd_by_ct = {e.ct_idx: e for e in ucd.values()}
        self._vcd_by_ct = {e.ct_idx: e for e in vcd.values() if e.ct_idx >= 0}
        self._ocd_to_ct = ocd_to_ct
        self._ct_to_type = ct_to_type
        self._ct_to_ocds = ct_to_ocds
        # CampObjData 侧的 OcdIndex 反查集合（供 objective_type_name 兜底）
        self._ocd_to_camp: set[int] = {c.ocd_index for c in camp_obj.values()}
        self._sam_radii = sam_radii
        self._sam_threat = sam_threat
        #: 缺失的 XML 标签（部署可能没有全套 Add-On，缺失是正常状态）
        self._missing = missing

    # ── 构造 ────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls, install_path: str | Path, theater: str, *, cache: bool = True,
    ) -> "TheaterData":
        """加载一个剧场的全部数据表。

        路径规则照搬 Settings.cs 第 76–94 行：``theater == "Korea"``（默认）
        用 ``<install>\\Data``，否则在 ``<install>\\Data`` 下找
        ``Add-On <theater>``（见 :func:`theater_root`，兼容目录名带年份后缀）。

        任何单表缺失或损坏都只记 warning 并留空，**不抛异常**。

        :param install_path: BMS 安装根目录（含 ``Data`` 的那一级）。
        :param theater: 剧场名，如 ``"Korea"`` / ``"Hellas"``；空串按 Korea。
            也可以直接给 Add-On 目录名（``"Add-On Hellas 2026"``）。
        :param cache: 为 ``True`` 时按 ``(install_path, 已解析剧场根目录)``
            缓存结果，同进程内重复调用直接返回同一对象。
        """
        install = Path(install_path).expanduser()
        try:
            install = install.resolve()
        except OSError:  # pragma: no cover - 路径不可解析时保留原值
            pass
        name = (theater or "").strip() or "Korea"
        root = theater_root(install, name)
        # 缓存键用**已解析的根目录**：这样 "Hellas" 与 "Add-On Hellas 2026"
        # 这类别名会命中同一条缓存（实测本机目录名带年份后缀）。
        key = (str(install), str(root).lower() if root.is_dir() else name.lower())

        if cache:
            hit = _CACHE.get(key)
            if hit is not None:
                return hit

        obj = cls._build(install, name, root)
        if cache:
            _CACHE[key] = obj
        return obj

    @classmethod
    def _build(cls, install: Path, theater: str, root: Path) -> "TheaterData":
        """真正干活的那个：定位文件 → 逐表加载 → 装配。"""
        objects = root / "TerrData" / "Objects"
        campaign = root / "Campaign"
        missing: list[str] = []

        def need(directory: Path, *names: str, label: str) -> Path | None:
            p = _find_file(directory, *names)
            if p is None:
                missing.append(label)
            return p

        ct_path = need(objects, "Falcon4_CT.xml", "FALCON4_CT.XML", label="CT")
        entries = _safe(load_class_table, ct_path, label="CT") or []

        ucd_path = need(objects, "Falcon4_UCD.xml", "FALCON4_UCD.XML", label="UCD")
        ucd = _safe(load_ucd_table, ucd_path, label="UCD") or {}

        vcd_path = need(objects, "Falcon4_VCD.xml", "FALCON4_VCD.XML", label="VCD")
        vcd = _safe(load_vcd_table, vcd_path, label="VCD") or {}

        wcd_path = need(objects, "Falcon4_WCD.xml", "FALCON4_WCD.XML", label="WCD")
        wcd = _safe(load_wcd_table, wcd_path, label="WCD") or {}

        rcd_path = need(objects, "Falcon4_RCD.xml", "FALCON4_RCD.XML", label="RCD")
        rcd = _safe(load_rcd_table, rcd_path, label="RCD") or {}

        fcd_path = need(objects, "Falcon4_FCD.xml", "FALCON4_FCD.XML", label="FCD")
        fcd = _safe(load_fcd_table, fcd_path, label="FCD") or {}

        camp_path = need(campaign, "CampObjData.xml", "CampObjData.XML",
                         label="CampObjData")
        camp_obj = _safe(load_camp_obj_table, camp_path, label="CampObjData") or {}

        strings_path = need(campaign, "strings.txt", "Strings.txt", label="strings")
        strings = _safe(load_strings_table, strings_path, label="strings") or {}

        # Theater.txt 在 <root>/TerrData/<剧场名>/NewTerrain/ 下；Add-On 常缺。
        # Hellas 的 Add-On 目录里连 TerrData/Hellas 都没有，缺失是正常状态。
        terr_dir = _find_addon_dir(root / "TerrData", theater)
        info_path = _find_file(terr_dir / "NewTerrain", "Theater.txt") \
            if terr_dir is not None else None
        if info_path is None:
            missing.append("Theater.txt")
        theater_info = _safe(load_theater_info, info_path, label="Theater.txt")

        ocd_folder = _find_dir(objects, "ObjectiveRelatedData")
        if ocd_folder is None:
            missing.append("ObjectiveRelatedData")
        ocd_to_ct, ct_to_type, ct_to_ocds = load_ocd_index(ocd_folder, entries)

        sam_radii, sam_threat = build_sam_radii(wcd, rcd)

        return cls(
            install, theater, root, entries=entries, ucd=ucd, vcd=vcd, wcd=wcd,
            rcd=rcd, fcd=fcd, camp_obj=camp_obj, strings=strings,
            theater_info=theater_info, ocd_to_ct=ocd_to_ct,
            ct_to_type=ct_to_type, ct_to_ocds=ct_to_ocds,
            sam_radii=sam_radii, sam_threat=sam_threat, missing=missing,
        )

    # ── 类表查询 ────────────────────────────────────────────────────

    def ct_get(self, entity_type_id: int) -> CtEntry | None:
        """``.uni`` 流的 ``entityTypeId`` → :class:`CtEntry`。

        移植自 ClassTable.cs ``Get``：``entityTypeId - 100`` 即 ``entries``
        下标（``.uni`` 里写的 entity type id = CT 的 ``Num`` + 100），
        越界返回 ``None``。
        """
        idx = entity_type_id - 100
        if idx < 0 or idx >= len(self.entries):
            return None
        return self.entries[idx]

    # ── 各表按索引查询 ──────────────────────────────────────────────

    def unit_def(self, idx: int) -> UcdEntry | None:
        """``Falcon4_UCD.xml`` 按 ``Num`` 查单位定义；不存在返回 ``None``。"""
        return self.ucd.get(idx)

    def vehicle_def(self, idx: int) -> VcdEntry | None:
        """``Falcon4_VCD.xml`` 按 ``Num`` 查载具定义；不存在返回 ``None``。"""
        return self.vcd.get(idx)

    def weapon_def(self, idx: int) -> WcdEntry | None:
        """``Falcon4_WCD.xml`` 按 ``Num`` 查武器定义；不存在返回 ``None``。"""
        return self.wcd.get(idx)

    def radar_def(self, idx: int) -> RcdLethality | None:
        """``Falcon4_RCD.xml`` 按 ``Num`` 查雷达定义；不存在返回 ``None``。"""
        return self.rcd.get(idx)

    def feature_def(self, idx: int) -> FcdEntry | None:
        """``Falcon4_FCD.xml`` 按 ``Num`` 查特征定义；不存在返回 ``None``。"""
        return self.fcd.get(idx)

    def camp_obj(self, idx: int) -> CampObjEntry | None:
        """``CampObjData.xml`` 按 ``CampId`` 查目标；不存在返回 ``None``。

        返回的是**原始记录**：``pos_x/pos_y/pos_z``（别名 ``position_x/...``）
        就是 XML 里的 ``<PositionX/Y/Z>``，**单位英尺，不做任何换算**。
        完整字典见 :attr:`camp_obj_data`，全部条目见 :meth:`all_camp_objs`。
        """
        return self.camp_obj_data.get(idx)

    def all_camp_objs(self) -> list[CampObjEntry]:
        """``CampObjData.xml`` 的全部目标（``CampObjTable.GetAll``）。

        顺序为**文件出现次序**（``dict`` 保持插入序，Python 3.7+ 保证），
        与 C# 遍历 ``_byId.Values`` 的次序一致。

        ⚠️ ``pos_x/pos_y/pos_z`` 是 XML 原始值（**英尺**），本方法**不换算**；
        转战役网格请用独立的坐标模块（X/Y 轴与地图视角相反，极易搞错）。
        """
        return list(self.camp_obj_data.values())

    def string(self, idx: int) -> str:
        """``strings.txt`` 按索引取文本；不存在返回空串。

        （C# ``StringsTable.Get`` 默认返回 ``null``；本方法签名要求 ``str``，
        故用空串表示缺失，调用方可用 ``if not s`` 判断。）

        任务名就是 ``string(MISSION_NAME_BASE + mission_code)``，即
        ``string(300 + code)``（JsonExporter.cs 第 446 行的约定）。
        """
        return self.strings.get(idx, "")

    def get_callsign(self, callsign_id: int, callsign_num: int) -> str:
        """飞行呼号，如 ``"Jedi 5"``（StringsTable.cs ``GetCallsign``）。

        存档里的 ``callsignId`` 字节是**相对 :data:`CALLSIGN_BASE`(2000) 的偏移**，
        ``callsign_num`` 是机号（1/2/3/4…）。查不到时按 C# 原样回退成
        ``"#<id>/<num>"``（例如 ``"#7/3"``），而不是抛异常或返回空串 ——
        保留这个格式便于一眼看出是缺表而不是数据为空。

        :param callsign_id: 存档里的呼号 ID（0 基，0 → ``strings.txt`` 索引 2000）。
        :param callsign_num: 机号。
        """
        name = self.strings.get(CALLSIGN_BASE + callsign_id)
        if name:
            return "%s %d" % (name, callsign_num)
        return "#%d/%d" % (callsign_id, callsign_num)

    # ── 派生查询（C# 里散落在 UcdTable/VcdTable 的链式解析）──────────

    def unit_def_by_entity_type(self, entity_type_id: int) -> UcdEntry | None:
        """``entityTypeId`` → UCD（UcdTable.cs ``GetEntryByEntityTypeId``）。

        链路：``entityTypeId - 100`` → CT → ``CT.EntityIdx`` → UCD ``Num``。
        顺带把 CT 的 ``Domain`` 回填到 UCD 记录（C# 第 174 行就是这么干的）。
        """
        ct = self.ct_get(entity_type_id)
        if ct is None or ct.entity_idx < 0:
            return None
        e = self.ucd.get(ct.entity_idx)
        if e is not None:
            e.domain = ct.domain
        return e

    def unit_def_by_ct_idx(self, ct_idx: int) -> UcdEntry | None:
        """CT 下标（== CT 的 ``Num``）→ UCD 记录。"""
        return self._ucd_by_ct.get(ct_idx)

    def vehicle_def_by_ct_idx(self, ct_idx: int) -> VcdEntry | None:
        """VCD 的 ``CtIdx`` → VCD 记录（VcdTable.cs ``GetByCtIdx``）。"""
        return self._vcd_by_ct.get(ct_idx)

    def vehicle_names(self, ucd_entry: UcdEntry | None) -> list[str]:
        """UCD → 该单位所有载具名（UcdTable.cs ``GetVehicleNames``）。

        遍历 ``VehicleCtIdx_0..15`` 里所有非 0 值，经 ``VCD.CtIdx`` 取名，
        按槽位顺序去重（大小写不敏感），空名跳过。
        """
        result: list[str] = []
        if ucd_entry is None:
            return result
        seen: set[str] = set()
        for ct_idx in ucd_entry.vehicle_ct_idx_all:
            v = self._vcd_by_ct.get(ct_idx)
            if v is None or not v.name:
                continue
            low = v.name.lower()
            if low not in seen:
                seen.add(low)
                result.append(v.name)
        return result

    def aircraft_name(self, entity_type_id: int) -> str | None:
        """``entityTypeId`` → 机型名（VcdTable.cs ``GetAircraftName``）。

        链路：CT → ``EntityIdx`` → UCD → ``VehicleCtIdx_0`` → ``VCD.CtIdx``。
        不需要完整记录时用这个方法；需要各列参数请用 :meth:`aircraft_entry`。
        """
        v = self.aircraft_entry(entity_type_id)
        return v.name if v is not None else None

    def aircraft_entry(
        self, entity_type_id: int, ct_entry: CtEntry | None = None,
        ucd_entry: UcdEntry | None = None,
    ) -> VcdEntry | None:
        """``.uni`` 记录的 ``entityTypeId`` → VCD 载具记录。

        忠实复刻 VcdTable.cs ``GetAircraftEntry``（第 119–127 行）::

            if (ct == null || ucd == null) return null;
            var ctEntry = ct.Get(entityTypeId);
            if (ctEntry == null || ctEntry.EntityIdx < 0) return null;
            int vct0 = ucd.GetVehicleCtIdx0(ctEntry.EntityIdx);
            if (vct0 <= 0) return null;
            return GetByCtIdx(vct0);

        即：``entityTypeId`` 只用来查 CT；UCD 是**靠 CT 的 ``EntityIdx`` 自己
        查出来的**（``ucd.GetVehicleCtIdx0(ctEntry.EntityIdx)``），并不是直接
        拿传进来的 UCD 对象 —— ``ucd`` 参数在 C# 里只起"非空校验"的作用。
        本方法照此实现，``ucd`` 传 ``None`` 时会自行解析，行为更宽松但结果相同。

        :param entity_type_id: ``.uni`` 记录里的 entity type ID。
        :param ct_entry: 可选，调用方已查好的 CT 记录（省一次查找）；
            传了就与 C# 一样要求它非空，为 ``None`` 时才自行 :meth:`ct_get`。
        :param ucd_entry: 可选，调用方已查好的 UCD 记录。**注意**：按 C# 语义，
            只要它非空，实际取 ``VehicleCtIdx_0`` 用的仍是"由 entityTypeId →
            CT.EntityIdx → UCD"链出来的那条记录，而不是传入对象本身。
        :return: :class:`VcdEntry`，无法解析时返回 ``None``（导出侧写 ``null``）。
        """
        ct = ct_entry if ct_entry is not None else self.ct_get(entity_type_id)
        if ct is None or ct.entity_idx < 0:
            return None
        if ucd_entry is None:
            ucd = self.ucd.get(ct.entity_idx)
        else:
            # 与 C# 一致：非空校验用传入对象，实际取值仍走 CT.EntityIdx → UCD
            ucd = self.ucd.get(ct.entity_idx, ucd_entry)
        if ucd is None or ucd.vehicle_ct_idx0 <= 0:
            return None
        return self._vcd_by_ct.get(ucd.vehicle_ct_idx0)

    # ── 目标类别 ────────────────────────────────────────────────────

    def objective_type(self, ocd_index: int) -> int:
        """``CampObj.OcdIndex`` → 目标类别号（OcdTypeTable.cs ``GetType``）。

        链路：``OCD 号 → CtIdx``（来自 ``OCD_%05d.XML`` 的 ``<CtIdx>``）
        ``→ CT.type``。任一步缺失返回 ``-1``。
        """
        ct_idx = self._ocd_to_ct.get(ocd_index)
        if ct_idx is None:
            return -1
        return self._ct_to_type.get(ct_idx, -1)

    def objective_type_for_ocd(self, ocd_index: int) -> str:
        """``OcdIndex`` → 目标类别名（如 ``"Airbase"``）；未知返回 ``"Type-1"``。

        ``"Type-1"`` 是 OcdTypeTable.cs ``GetTypeName(-1)`` 的原样行为，
        保留它是为了能一眼看出"这条链断了"，而不是静默变成空串。

        （:meth:`ocd_type_name` 是本方法的别名。）
        """
        return objective_type_name_by_type(self.objective_type(ocd_index))

    def ocd_type_name(self, ocd_index: int) -> str:
        """``CampObjData`` 的 ``OcdIndex`` → 目标类别名（喂给 ``typeName``）。

        与 :meth:`objective_type_for_ocd` 完全相同，只是名字更贴调用点。

        **OcdIndex 为什么不是数组下标**（这是要点）：OcdTypeTable 走的是两级
        字典映射，不是数组索引：

        1. ``OCD_%05d/OCD_%05d.XML`` 里根节点 ``<OCD Num="0">`` 的 ``<CtIdx>``
           给出**类表下标**。实测 Korea 的 ``OcdIndex`` 是 ``0…1145``（1146 个
           目录一一对应），而 Hellas 的 ``OcdIndex`` 最大只到 ``868`` ——
           所以 ``656``/``657`` 这类值就是"第 656 号 OCD 目录"，只是目录总数
           随剧场变化，绝不能拿它当 ``OBJECTIVE_TYPE_NAMES`` 的下标；
        2. ``CtIdx`` 指向 ``Falcon4_CT.xml`` 的记录，只有 ``Domain=3 Class=4``
           （目标模板，Korea 1145 条 / Hellas 867 条）才登记进
           ``ctIdx → Type``；该 ``Type`` 才是 ``OBJECTIVE_TYPE_NAMES`` 的键。

        实测对照：``OcdIndex=656``（Hellas "<Ataturk Airport"）→ ``CtIdx=201``
        → CT ``Domain=3 Class=4 Type=1`` → ``"Airbase"``。

        查不到时返回 ``"Type-1"``（= ``GetTypeName(-1)``），故意保留这个可辨识
        的字符串，便于与"真的没有类别"区分开。

        :param ocd_index: ``CampObj.OcdIndex``（原始值，不换算）。
        """
        return objective_type_name_by_type(self.objective_type(ocd_index))

    def objective_type_name(self, ct_entry: CtEntry) -> str:
        """目标类别名，如 ``'Airbase'`` / ``'SAM / AAA Site'``。

        ⚠️ 与 C# 的差异（签名要求）：C# 的入口是 ``OcdTypeTable.GetType(ocdIndex)``，
        即**从 OCD 号出发**；本方法拿到的却是 :class:`CtEntry`，而 CtEntry 里
        没有 OcdIndex 字段。故这里按"CT 下标"反查，依次尝试三条路径：

        1. 该 CT 自身就在 ``ct_to_type`` 里（即本身就是 ``Domain=3 Class=4``
           的目标模板）—— 直接取它的 ``Type``。实测 Korea 的 1145 条
           ``D=3 C=4`` 记录覆盖了全部 27 个类别号，正常情况在这里就返回。
        2. 用 ``ct_to_ocds`` 反查该 CT 对应的 OCD 号，再走 ``OcdIndex →
           CtIdx → CT.Type`` 的 C# 链路。
        3. 兜底：在 ``CampObjData`` 里找第一条 ``OcdIndex`` 指到该 CT 的目标，
           取其 ``OcdIndex`` 再走链路（等价于"CampObjData 侧反查"）。

        全部走不通时返回 ``"Type-1"``（= ``GetTypeName(-1)``），
        便于一眼看出链路断了，而不是静默变空串。

        :param ct_entry: :meth:`ct_get` 返回的记录。
        """
        t = self._ct_to_type.get(ct_entry.num)
        if t is not None:
            return objective_type_name_by_type(t)
        for ocd_num in self._ct_to_ocds.get(ct_entry.num, ()):
            t = self.objective_type(ocd_num)
            if t >= 0:
                return objective_type_name_by_type(t)
        # 兜底路径：只在 CampObjData 实际引用过的 OCD 号里找
        for ocd_num in self._ocd_to_camp:
            if self._ocd_to_ct.get(ocd_num) != ct_entry.num:
                continue
            t = self.objective_type(ocd_num)
            if t >= 0:
                return objective_type_name_by_type(t)
        return objective_type_name_by_type(-1)

    def objective_type_names(self) -> list[str]:
        """本剧场 ``CampObjData`` 实际用到的全部目标类别名（去重、保序）。

        枚举每个 ``CampObj.OcdIndex`` 走完整 ``OcdIndex → CtIdx → CT.Type``
        链路，用来核对类别名表覆盖度。
        """
        seen: list[str] = []
        cache: dict[int, str] = {}
        for e in self.camp_obj_data.values():
            name = cache.get(e.ocd_index)
            if name is None:
                name = self.objective_type_for_ocd(e.ocd_index)
                cache[e.ocd_index] = name
            if name not in seen:
                seen.append(name)
        return seen

    # ── 防空射程 ────────────────────────────────────────────────────

    @property
    def sam_radii(self) -> dict[str, dict[str, float]]:
        """防空系统 → 三层威胁环半径（海里）。

        形如 ``{'SA-2': {'long': 30.0, 'medium': 0.0, 'short': 15.0}, ...}``。
        构造方式见 :func:`build_sam_radii`：``long`` 来自 ``Falcon4_WCD.xml``
        的武器射程（并经 ``Falcon4_RCD.xml`` 与兜底表补充），``medium`` 来自
        RCD 探测距离，``short`` 是按 :data:`SAM_SHORT_FRACTION` 折算的推断值。
        """
        return self._sam_radii

    @property
    def sam_threat(self) -> dict[str, RcdLethality]:
        """防空系统 → 匹配到的 RCD 记录（best-effort，可能错配）。

        C# ``WcdTable.SamThreat`` 自认这套名字匹配"不保证正确相关"，
        故仅作参考，不参与导出。
        """
        return self._sam_threat

    # ── 自省 ────────────────────────────────────────────────────────

    @property
    def missing(self) -> list[str]:
        """加载时未能找到的表名（``CT``/``UCD``/``Theater.txt``…）。

        部署缺 Add-On 时这是正常状态，调用方据此决定是否降级功能。
        """
        return list(self._missing)

    def summary(self) -> dict[str, Any]:
        """各表条数概览，供日志与自检使用。"""
        return {
            "theater": self.theater,
            "root": str(self.root),
            "ct_entries": len(self.entries),
            "ucd": len(self.ucd),
            "vcd": len(self.vcd),
            "wcd": len(self.wcd),
            "rcd": len(self.rcd),
            "fcd": len(self.fcd),
            "camp_obj": len(self.camp_obj_data),
            "strings": len(self.strings),
            "ocd_mappings": len(self._ocd_to_ct),
            "objective_templates": len(self._ct_to_type),
            "sam_systems": len(self._sam_radii),
            "missing": list(self._missing),
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<TheaterData %s ct=%d ucd=%d vcd=%d>" % (
            self.theater, len(self.entries), len(self.ucd), len(self.vcd))


def theater_root(install_path: str | Path, theater: str) -> Path:
    """按 Settings.cs 的规则算剧场根目录。

    ``theater`` 为空或等于 ``Korea``（大小写不敏感）→ ``<install>\\Data``；
    否则在 ``<install>\\Data`` 下找 ``Add-On <theater>`` 目录
    （见 :func:`_find_addon_root`，容忍目录名带年份后缀之类的差异）；
    实在找不到时按 C# 的拼法返回 ``<install>\\Data\\Add-On <theater>``，
    便于错误信息里看出期望路径。

    :param install_path: BMS 安装根目录。
    :param theater: 剧场名。
    """
    base = Path(install_path)
    if not theater or theater.strip().lower() == "korea":
        return base / "Data"
    name = theater.strip()
    found = _find_addon_root(base, name)
    return found if found is not None else base / "Data" / ("Add-On %s" % name)


def load_theater(
    install_path: str | Path, theater: str = "Korea", *, cache: bool = True,
) -> TheaterData:
    """便捷函数，等价于 :meth:`TheaterData.load`。"""
    return TheaterData.load(install_path, theater, cache=cache)
