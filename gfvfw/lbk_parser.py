"""
BMS Logbook（``.lbk``）读取器 —— **已完全解出格式**。

格式来源（全部为实测，非推测）
------------------------------
1. 官方读取器是 ``Tools\\Logbook Editor\\LogbookEditor.exe``（32 位 MinGW/Qt4）。
   用 ``objdump`` 反汇编后定位到文件读写与其包装函数
   （``scripts/pe_find_import_calls.py`` 负责定位 IAT/thunk 调用点）：

   * 保存：``0x40300c(buf, 0x58, buf, 0x174)`` → ``fwrite(buf, 1, 0x174, fp)``
     → ``0x40305c(buf, 0x58, buf, 0x174)``（写回前还原内存）
   * 载入：``fopen`` → ``fseek/ftell`` 取长度 → **长度必须恰好 0x174 = 372**
     → ``fread(buf, 1, 372, fp)`` → ``0x40305c`` 解码
     → 校验 ``*(uint32*)(buf + 0x170) == 0``，否则判为非法文件

2. ``0x40305c`` 的循环解出一个**差分异或**：

       P[i] = C[i] XOR key[i % K] XOR C[i-1],   C[-1] = 0x58

   其中 ``key`` 是 ``.data:0x418000`` 处的 C 字符串：
   ``"Falcon is your Master"``（21 字节，循环使用）。

   编码方向是 ``0x40300c``，形式对称但不相同：

       C[i] = C[i-1] XOR key[i % K] XOR P[i],   C[-1] = 0x58

   ⚠️ 两者都以**密文**的前一字节作链值，所以**这个变换不是自身的逆**，
   必须分别实现（见 :func:`decode` / :func:`encode`）。
   保存路径正是"先 ``0x40300c`` 编码写盘、再 ``0x40305c`` 解码还原内存"，
   一编一解抵消，这也是当初容易误判成"同一个函数"的原因。

3. 字段布局由反汇编中 ``call 0x402e3c``（取结构体指针）之后的
   ``[eax+0xNN]`` 访问枚举得出，见 ``scripts/lbk_layout_probe.py``。

⚠️ 已知边界
-----------
* 内存里的结构体**比文件大**：反汇编里存在对 ``[eax+0x174]`` 的读取，
  而文件只有 372 字节 —— 文件是结构体的前缀。
* 数值字段的**业务含义**（哪个是时长、哪个是击杀）部分是推断。
  本模块把"已确证"与"推断"分开标注：:data:`FIELDS` 里
  ``certain=True`` 的字段可直接使用；推断字段一律带上偏移串，
  便于与人眼在 LogbookEditor 里对照核验。

如何重建证据（``_re/`` 不入库，随时可重生成）
--------------------------------------------
本机的 REA 深挖提供方均不可用（Ghidra 未安装且**拒绝 32 位 PE**，Hopper 仅 macOS），
所以用 MinGW 自带的 ``objdump``：:

    # 1. 反汇编（约 1.5 MB）
    G:\\mingw64\\bin\\objdump.exe -d -M intel LogbookEditor.exe > _re/lbk/disasm.txt
    # 2. 取 .rdata / 字符串（军衔表、edtMedal* 控件名都在这）
    .venv\\Scripts\\python.exe scripts\\pe_strings.py
    .venv\\Scripts\\python.exe scripts\\pe_strings_addressed.py
    # 3. 定位文件读写与解码函数的调用点（跟着 IAT + thunk 走）
    .venv\\Scripts\\python.exe scripts\\pe_find_import_calls.py
    # 4. 枚举结构体字段偏移
    .venv\\Scripts\\python.exe scripts\\lbk_layout_probe.py
    # 5. 同一人 4 份存档的时间序列对拍（验证字段语义）
    .venv\\Scripts\\python.exe scripts\\lbk_timeseries_probe.py

关键地址（ImageBase ``0x400000``，节区 VMA 是 RVA、IAT 槽是 VMA —— 这个区别
曾经让我把文件读写函数找错）：``0x40300c`` 编码、``0x40305c`` 解码、
``0x418000`` 密钥字符串。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------
# 常量：全部来自对 LogbookEditor.exe 的实测
# --------------------------------------------------------------------------

#: 文件大小**必须**恰好是这个值，官方读取器会拒绝其它长度
FILE_SIZE = 0x174            # 372

#: 异或链的初始状态（保存/载入路径都写死了 0x58）
XOR_SEED = 0x58

#: ``.data:0x418000`` 处的密钥字符串
XOR_KEY = b"Falcon is your Master"

#: 文件末尾必须是 0 的那个双字（官方读取器的合法性校验）
SENTINEL_OFFSET = 0x170

#: 军衔下拉框的条目（``.rdata`` 里的字符串，按顺序）
RANKS = (
    "Second lieutenant",
    "First lieutenant",
    "Captain",
    "Major",
    "Lieutenant colonel",
    "Colonel",
    "Brigadier general",
)


class LbkError(ValueError):
    """无法解析的 ``.lbk``（对外可直接展示）。"""


#: 解析器版本。格式理解有更新时递增，便于判断历史存档要不要重解析。
PARSER_VERSION = "1"


# --------------------------------------------------------------------------
# 解码 / 编码
# --------------------------------------------------------------------------

def decode(data: bytes) -> bytes:
    """把 ``.lbk`` 密文解成 372 字节明文。

    ``P[i] = C[i] XOR key[i % K] XOR C[i-1]``，``C[-1] = 0x58``。
    """
    out = bytearray(len(data))
    prev = XOR_SEED
    for i, b in enumerate(data):
        out[i] = b ^ XOR_KEY[i % len(XOR_KEY)] ^ prev
        prev = b
    return bytes(out)


def encode(plain: bytes) -> bytes:
    """明文 → 密文（:func:`decode` 的逆）。

    ``C[i] = C[i-1] XOR key[i % K] XOR P[i]``，``C[-1] = 0x58``。

    ⚠️ **不是** :func:`decode`（也就是说这个变换**不是自身的逆**）。
    官方工具有两个不同的函数：``0x40300c`` 编码、``0x40305c`` 解码，
    两者都以**密文**的前一字节为链值：保存时先调 ``0x40300c`` 写盘，
    写完再调 ``0x40305c`` 把内存里的结构体还原 —— 一编一解，正好抵消。
    所以这里必须老老实实写成正向递推，不能偷懒复用 :func:`decode`。
    """
    out = bytearray(len(plain))
    prev = XOR_SEED
    for i, p in enumerate(plain):
        cur = p ^ XOR_KEY[i % len(XOR_KEY)] ^ prev
        out[i] = cur
        prev = cur
    return bytes(out)


# --------------------------------------------------------------------------
# 字段表
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSpec:
    """一个字段：偏移、类型、名称、是否已确证、显示名、说明。"""

    offset: int
    kind: str                   # str21 / str / u8 / u16 / u32 / f32
    name: str
    certain: bool
    note: str = ""
    label: str = ""             # 给人看的名字（页面用；空则回退到 name）


def _str_field(off: int, maxlen: int, name: str, certain: bool = True,
               note: str = "", label: str = "") -> FieldSpec:
    return FieldSpec(off, "str%d" % maxlen, name, certain, note, label)


#: 结构体字段表。
#:
#: 偏移与宽度来自反汇编枚举（``scripts/lbk_layout_probe.py``）；
#: 名称中 ``certain=False`` 的为**推断**，依据是官方工具的界面字段清单
#: （``.rdata`` 0x41d064 起）与数值随时间的单调性，需要人工核验。
FIELDS: tuple[FieldSpec, ...] = (
    # ---- 字符串区 ----
    #
    # 偏移由"解密后扫 NUL 结尾串"实测得出（4 个样本一致）：
    #   0x00 姓名 / 0x15 呼号 / 0x28 四字符文本 / 0x2d 日期 / 0x3a 中队
    # ⚠️ 长度给得比实测串长，读取时**以 NUL 为准**：
    #    早先把呼号长度写成 7，把 "Oblivion" 截成了 "Oblivio"。
    _str_field(0x00, 0x15, "name", note="飞行员姓名；官方默认模板里是 Joe Pilot",
               label="姓名"),
    _str_field(0x15, 0x13, "callsign", note="游戏内呼号；与 .lbk 文件名一致",
               label="呼号"),
    _str_field(0x28, 4, "text_28", False,
               "4 字符自由文本，只有同一人的两份存档里有值（ID5A）",
               label="4 字符文本"),
    _str_field(0x2d, 9, "date", note="MM/DD/YY，随存档日期变化", label="存档日期"),
    # ⚠️ 早先标为 certain=True，理由是"官方界面有中队输入框"。但实测 4 份样本里
    #    有 3 份该字段**与呼号完全相同**（Oblivion/Obsequies/SZSZS），
    #    只有官方默认模板是 "Default"。这更像"BMS 默认把呼号填进这个字段"，
    #    不足以断定它稳定表示中队 —— 所以降级为推断，只展示、不入库。
    _str_field(0x3a, 0x0d, "squadron", False,
               "疑似中队名。实测 3/4 样本与呼号相同，仅官方模板为 Default",
               label="中队（待核对）"),

    # ---- 浮点 ----
    FieldSpec(0x48, "f32", "flight_hours", True,
              "飞行小时数。三重佐证：① 界面字段 edtFlightHours 是双精度微调框，"
              "保存路径 fstp [eax+0x48]；② 同一人四份存档随日期**单调递增**"
              "（251.6 → 253.8 → 255.9 → 296.3）；③ 量级与老玩家相符",
              label="累计飞行小时"),
    FieldSpec(0x4c, "f32", "ace_factor", True,
              "官方界面字段 edtAceFactor，保存路径 fstp [eax+0x4c]；"
              "默认模板里恰为 1.0",
              label="Ace 系数"),

    # ---- 军衔 ----
    FieldSpec(0x50, "u32", "rank_index", True,
              "军衔下拉框下标 → RANKS。佐证：同一人在 2026-02-27 为 3、"
              "03-03 起为 4，与「期间晋升」一致",
              label="军衔下标"),

    # ---- 16 位计数区 ----
    FieldSpec(0x54, "u16", "counter_54", False),
    FieldSpec(0x56, "u16", "counter_56", False),
    FieldSpec(0x58, "u16", "counter_58", False),
    FieldSpec(0x5a, "u16", "counter_5a", False),
    FieldSpec(0x5c, "u16", "counter_5c", False),
    FieldSpec(0x5e, "u16", "counter_5e", False),
    FieldSpec(0x60, "u16", "counter_60", False),
    FieldSpec(0x62, "u16", "counter_62", False),
    FieldSpec(0x64, "u16", "counter_64", False),
    FieldSpec(0x66, "u16", "counter_66", False),
    FieldSpec(0x68, "u16", "counter_68", False),
    FieldSpec(0x6a, "u16", "counter_6a", False),
    FieldSpec(0x6c, "u32", "value_6c", False, "32 位，量级像累计量而非计数"),
    FieldSpec(0x70, "u32", "value_70", False, "32 位，量级像累计量而非计数"),
    FieldSpec(0x74, "u16", "counter_74", False),
    FieldSpec(0x76, "u16", "counter_76", False),
    FieldSpec(0x78, "u16", "counter_78", False),
    FieldSpec(0x7a, "u16", "counter_7a", False),
    FieldSpec(0x7c, "u16", "counter_7c", False),
    FieldSpec(0x7e, "u16", "counter_7e", False),
    FieldSpec(0x80, "u16", "counter_80", False),
    FieldSpec(0x82, "u16", "counter_82", False),
    FieldSpec(0x84, "u16", "counter_84", False),
    FieldSpec(0x86, "u16", "counter_86", False),
    FieldSpec(0x88, "u16", "counter_88", False),

    # ---- 勋章：官方界面有 6 个 "edtMedal*" 控件，正好 6 个字节 ----
    FieldSpec(0x8c, "u8", "medal_dist_fly_cross", False, "edtMedalDistFly"),
    FieldSpec(0x8d, "u8", "medal_longevity", False, "edtMedalLongevity"),
    FieldSpec(0x8e, "u8", "medal_air_medal", False, "edtMedalAirMedal"),
    FieldSpec(0x8f, "u8", "medal_korea_campaign", False, "edtMedalKoreaCampaign"),
    FieldSpec(0x90, "u8", "medal_air_force_cross", False, "edtMedalAirForce"),
    FieldSpec(0x91, "u8", "medal_silver_star", False, "edtMedalSilver"),
)

#: 只读的 32 位字段（官方工具里不作为输入框，但存在于文件中）。
#: 一并解析出来，便于核验与将来使用。
READONLY_U32 = (
    0x98, 0xa4, 0xa8, 0xbc, 0xc4, 0xcc, 0xd8, 0xdc, 0xe4, 0xe8, 0xf0, 0xf4,
    0x100, 0x10c, 0x118, 0x120, 0x124, 0x134, 0x140, 0x148, 0x150, 0x15c,
    0x160, 0x168,
)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def _read_cstr(buf: bytes, off: int, maxlen: int) -> str:
    raw = buf[off:off + maxlen]
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return raw.decode("latin-1", errors="replace").strip()


@dataclass
class LbkRecord:
    """一份 Logbook 的内容。``raw`` 保留全部 372 字节明文。"""

    raw: bytes
    fields: dict[str, Any] = field(default_factory=dict)
    uncertain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- 便捷访问（值缺失时给默认，避免调用方到处判空）--
    @property
    def name(self) -> str:
        return self.fields.get("name") or ""

    @property
    def callsign(self) -> str:
        return self.fields.get("callsign") or ""

    @property
    def squadron(self) -> str:
        return self.fields.get("squadron") or ""

    @property
    def flight_hours(self) -> Optional[float]:
        v = self.fields.get("flight_hours")
        return float(v) if v is not None else None

    @property
    def rank_code(self) -> Optional[str]:
        """把 ``rank_index`` 映射为 BMS 的英文军衔名（越界返回 None）。"""
        idx = self.fields.get("rank_index")
        if idx is None or not (0 <= int(idx) < len(RANKS)):
            return None
        return RANKS[int(idx)]

    @property
    def medals(self) -> dict[str, int]:
        return {k: v for k, v in self.fields.items()
                if k.startswith("medal_") and v}


def parse(data: bytes, *, strict: bool = True) -> LbkRecord:
    """解析 ``.lbk`` 字节流。

    ``strict=True`` 时执行官方读取器的两项校验（长度 372、哨兵为 0），
    任一项不过就抛 :class:`LbkError`。
    """
    if len(data) != FILE_SIZE:
        msg = ("Logbook 文件长度应为 %d 字节，实际 %d 字节"
               % (FILE_SIZE, len(data)))
        if strict:
            raise LbkError(msg)
        plain, warn = data, msg
    else:
        plain, warn = data, None

    buf = decode(plain)
    rec = LbkRecord(raw=buf)
    if warn:
        rec.warnings.append(warn)

    if len(buf) == FILE_SIZE:
        sentinel = struct.unpack_from("<I", buf, SENTINEL_OFFSET)[0]
        if sentinel != 0:
            msg = ("结尾校验字非 0（0x%08x）—— 可能不是 Logbook，"
                   "或文件已损坏" % sentinel)
            if strict:
                raise LbkError(msg)
            rec.warnings.append(msg)

    for spec in FIELDS:
        off = spec.offset
        if spec.kind.startswith("str"):
            n = int(spec.kind[3:])
            rec.fields[spec.name] = _read_cstr(buf, off, n)
        elif spec.kind == "u8":
            rec.fields[spec.name] = buf[off]
        elif spec.kind == "u16":
            rec.fields[spec.name] = struct.unpack_from("<H", buf, off)[0]
        elif spec.kind == "u32":
            rec.fields[spec.name] = struct.unpack_from("<I", buf, off)[0]
        elif spec.kind == "f32":
            rec.fields[spec.name] = struct.unpack_from("<f", buf, off)[0]
        else:                                       # pragma: no cover
            raise AssertionError("未知字段类型 %s" % spec.kind)
        if not spec.certain:
            rec.uncertain.append(spec.name)

    for off in READONLY_U32:
        rec.fields["raw_u32_%03x" % off] = struct.unpack_from("<I", buf, off)[0]
        rec.uncertain.append("raw_u32_%03x" % off)
    return rec


def parse_file(path, *, strict: bool = True) -> LbkRecord:
    with open(path, "rb") as fh:
        return parse(fh.read(), strict=strict)
