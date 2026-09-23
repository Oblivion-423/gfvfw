"""
BMS .cam 战役存档格式探针（开发期验证用，非产品代码）。

目的：在把 CamReader（C#）移植成 Python 之前，先验证三件事：
  1. .cam 是"目录 + 内嵌文件"的简单容器（CamReader/Core/Bundle.cs）
  2. 内嵌文件是 LZSS(12位窗口/4位长度) 压缩（CamReader/Core/Lzss.cs）
  3. .cmp 头部字段能按版本号正确解出，并与 CamReader 产出的
     campaign_state.json 对齐

用法:
    .venv\\Scripts\\python.exe scripts\\cam_probe.py <file.cam> [--json <campaign_state.json>]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

WINDOW_SIZE = 4096


# --------------------------------------------------------------------------
# 1. LZSS 解压
# --------------------------------------------------------------------------

def lzss_decompress(data: bytes, out_size: int, *, tail_fix: bool = True) -> bytes:
    """CamReader Lzss.Decompress 的 Python 移植。

    ``tail_fix``：CamReader（C#）在最后一段匹配越界时写的是
        ``size = 0; matchLength = size - 1;``   → matchLength = -1，尾部被截断
    而原始 lzss.cpp 的顺序是
        ``matchLength = size - 1; size = 0;``   → 正好输出剩余 size 字节
    后者才是正确的。默认用修正版，``--buggy-tail`` 可复现 C# 行为做对比。
    """
    window = bytearray(WINDOW_SIZE)          # 全零初始化
    out = bytearray(out_size)

    in_pos = 0
    out_pos = 0
    cur = 1                                   # current_position 从 1 开始
    size = out_size

    flag_byte = data[in_pos]
    in_pos += 1
    flag_mask = 1

    while size > 0:
        reload = False
        if flag_mask == 0x100:
            flag_byte = data[in_pos]
            flag_mask = 1
            reload = True
        flag_mask <<= 1
        bit = (flag_byte & (flag_mask >> 1)) != 0

        if bit:
            if reload:
                in_pos += 1
            c = data[in_pos]
            in_pos += 1
            out[out_pos] = c
            out_pos += 1
            size -= 1
            window[cur] = c
            cur = (cur + 1) & (WINDOW_SIZE - 1)
        else:
            if reload:
                in_pos += 1
            b0 = data[in_pos]
            in_pos += 1
            b1 = data[in_pos]
            in_pos += 1
            match_pos = b1 | ((b0 & 0x0F) << 8)
            match_len = (b0 >> 4) + 1

            if match_len < size:
                size -= match_len + 1
            else:
                if tail_fix:
                    match_len = size - 1
                    size = 0
                else:
                    size = 0
                    match_len = size - 1     # C# 原样：-1，尾部截断

            for i in range(match_len + 1):
                c = window[(match_pos + i) & (WINDOW_SIZE - 1)]
                out[out_pos] = c
                out_pos += 1
                window[cur] = c
                cur = (cur + 1) & (WINDOW_SIZE - 1)

    return bytes(out)


# --------------------------------------------------------------------------
# 2. .cam 容器
# --------------------------------------------------------------------------

class Embedded:
    __slots__ = ("name", "offset", "size")

    def __init__(self, name: str, offset: int, size: int):
        self.name, self.offset, self.size = name, offset, size


def load_bundle(path: Path):
    raw = path.read_bytes()
    dir_off = struct.unpack_from("<I", raw, 0)[0]
    n = struct.unpack_from("<I", raw, dir_off)[0]
    cur = dir_off + 4
    files = []
    for _ in range(n):
        nl = raw[cur]
        cur += 1
        name = raw[cur:cur + nl].decode("ascii", "replace")
        cur += nl
        off, sz = struct.unpack_from("<II", raw, cur)
        cur += 8
        files.append(Embedded(name, off, sz))
    version = 72
    for f in files:
        if f.name.lower().endswith(".ver"):
            txt = raw[f.offset:f.offset + f.size].decode("ascii", "replace").strip("\0 \t\r\n")
            try:
                version = int(txt)
            except ValueError:
                pass
    return raw, files, version


def expand_cmp(raw: bytes) -> tuple[int, int, bytes]:
    """[int32 compSz][int32 uncompressedSz][data...]"""
    comp_sz = struct.unpack_from("<i", raw, 0)[0]
    u_sz = struct.unpack_from("<i", raw, 4)[0]
    comp = raw[8:]
    return comp_sz, u_sz, lzss_decompress(comp, u_sz)


def expand_with_count(raw: bytes) -> tuple[int, int, bytes]:
    """[int32 compSz][int16 count][int32 uncompressedSz][data...]"""
    count = struct.unpack_from("<h", raw, 4)[0]
    u_sz = struct.unpack_from("<i", raw, 6)[0]
    comp = raw[10:]
    return count, u_sz, lzss_decompress(comp, u_sz)


# --------------------------------------------------------------------------
# 3. .cmp 头部解码
# --------------------------------------------------------------------------

class Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def i16(self):
        v = struct.unpack_from("<h", self.d, self.p)[0]; self.p += 2; return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4; return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]; self.p += 4; return v

    def bytes(self, n):
        v = self.d[self.p:self.p + n]; self.p += n; return v

    def str(self, n):
        return clip(self.bytes(n).decode("ascii", "replace"))

    def skip(self, n):
        self.p += n


def clip(s: str) -> str:
    i = s.find("\0")
    return s[:i] if i >= 0 else s


def decode_cmp(d: bytes, ver: int) -> dict:
    r = Reader(d)
    out = {"version": ver}

    out["currentTime"] = r.u32() or 1
    if ver >= 48:
        out["teStartTime"] = r.u32()
        out["teTimeLimit"] = r.u32()
        out["teVictoryPts"] = r.i32() if ver >= 49 else 0
    else:
        out["teStartTime"] = out["currentTime"]
        out["teTimeLimit"] = out["currentTime"] + 18000000

    if ver >= 52:
        out["teType"] = r.i32()
        out["teNumTeams"] = r.i32()
        out["teAircraft"] = [r.i32() for _ in range(8)]
        out["teF16s"] = [r.i32() for _ in range(8)]
        out["teTeam"] = r.i32()
        out["teTeamPts"] = [r.i32() for _ in range(8)]
        out["teFlags"] = r.i32()
        teams = []
        for _ in range(8):
            teams.append({
                "flag": r.u8(), "color": r.u8(),
                "name": r.str(20), "motto": r.str(200),
            })
        out["teams"] = teams

    if ver >= 19:
        out["lastMajorEvent"] = r.u32()
    out["lastResupply"] = r.u32()
    out["lastRepair"] = r.u32()
    out["lastReinforce"] = r.u32()

    out["timeStamp"] = r.i16()
    out["group"] = r.i16()
    out["groundRatio"] = r.i16()
    out["airRatio"] = r.i16()
    out["airDefRatio"] = r.i16()
    out["navalRatio"] = r.i16()
    out["brief"] = r.i16()
    out["theaterSizeX"] = r.i16()
    out["theaterSizeY"] = r.i16()

    out["currentDay"] = r.u8()
    out["activeTeams"] = r.u8()
    out["dayZero"] = r.u8()
    out["endgameResult"] = r.u8()
    out["situation"] = r.u8()
    out["enemyAirExp"] = r.u8()
    out["enemyADExp"] = r.u8()
    out["bullseyeName"] = r.u8()
    out["bullseyeX"] = r.i16()
    out["bullseyeY"] = r.i16()

    out["theaterName"] = r.str(40)
    out["scenario"] = r.str(40)
    out["saveFile"] = r.str(40)
    out["uiName"] = r.str(40)

    out["playerSquadId"] = {"num": r.u32(), "creator": r.u32()}

    n_recent = r.i16()
    out["numRecentEvents"] = n_recent
    ev = []
    for _ in range(max(0, n_recent)):
        ev.append(read_event(r))
    out["recentEvents"] = ev

    n_prio = r.i16()
    out["numPriorityEvents"] = n_prio
    ev2 = []
    for _ in range(max(0, n_prio)):
        ev2.append(read_event(r))
    out["priorityEvents"] = ev2

    out["campMapSize"] = r.i16()
    if out["campMapSize"] > 0:
        out["campMap"] = r.bytes(out["campMapSize"])

    out["lastIndexNum"] = r.i16()
    n_sq = r.i16()
    out["numSquadrons"] = n_sq
    name_len = 80 if ver >= 102 else 40
    squads = []
    for _ in range(max(0, n_sq)):
        s = {}
        s["x"] = r.f32()
        s["y"] = r.f32()
        s["id"] = {"num": r.u32(), "creator": r.u32()}
        s["descIdx"] = r.i16()
        s["nameId"] = r.i16()
        if ver >= 42:
            s["icon"] = r.i16()
            s["path"] = r.i16()
        s["specialty"] = r.u8()
        s["strength"] = r.u8()
        s["country"] = r.u8()
        s["airbase"] = r.str(name_len)
        r.skip(1)
        if ver >= 102:
            s["flags"] = r.i32()
            s["campId"] = r.i16()
            s["texSet"] = r.i16()
            s["squadName"] = r.str(name_len)
        squads.append(s)
    out["squadrons"] = squads

    out["bytesConsumed"] = r.p
    out["bytesTotal"] = len(d)
    if ver >= 31 and r.p < len(d):
        out["tempo"] = r.u8(); r.p += 0
    if ver >= 43 and r.p + 12 <= len(d):
        out["creatorIP"] = r.u32()
        out["creationTime"] = r.u32()
        out["creationRand"] = r.u32()
    if ver >= 110 and r.p + 4 <= len(d):
        out["campPeriodStart"] = r.i16()
        out["campPeriodEnd"] = r.i16()
    out["bytesConsumedFinal"] = r.p
    return out


def read_event(r: Reader) -> dict:
    e = {"x": r.i16(), "y": r.i16(), "time": r.u32(),
         "flags": r.u8(), "team": r.u8()}
    r.skip(2); r.skip(4); r.skip(4)
    ln = r.i16()
    e["text"] = r.str(ln) if ln > 0 else ""
    return e


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cam")
    ap.add_argument("--json", default=None, help="CamReader 的 campaign_state.json 用于对拍")
    ap.add_argument("--buggy-tail", action="store_true")
    ap.add_argument("--dump-raw", default=None)
    args = ap.parse_args()

    global lzss_decompress
    if args.buggy_tail:
        _orig = lzss_decompress
        lzss_decompress = lambda d, n: _orig(d, n, tail_fix=False)

    p = Path(args.cam)
    raw, files, version = load_bundle(p)
    print("文件        : %s  (%d 字节)" % (p.name, len(raw)))
    print("bundle 版本 : %d" % version)
    print("内嵌文件 (%d):" % len(files))
    for f in files:
        print("    %-12s off=%-8d size=%d" % (f.name, f.offset, f.size))

    cmp_raw = None
    for f in files:
        if f.name.lower().endswith(".cmp"):
            cmp_raw = raw[f.offset:f.offset + f.size]
    if cmp_raw is None:
        print("!! bundle 内没有 .cmp")
        return 2

    comp_sz, u_sz, d = expand_cmp(cmp_raw)
    print()
    print(".cmp 压缩=%s 解压申报=%s 实得=%d" % (f"{comp_sz:,}", f"{u_sz:,}", len(d)))
    print("解压后首 64 字节: %s" % d[:64].hex(" "))

    if args.dump_raw:
        Path(args.dump_raw).write_bytes(d)
        print("已写出原始 .cmp → %s" % args.dump_raw)

    info = decode_cmp(d, version)
    print()
    print("=== .cmp 头部解码 ===")
    for k in ("currentTime", "currentDay", "dayZero", "activeTeams", "endgameResult",
              "situation", "tempo", "theaterName", "scenario", "saveFile", "uiName",
              "bullseyeX", "bullseyeY", "theaterSizeX", "theaterSizeY",
              "groundRatio", "airRatio", "airDefRatio", "navalRatio",
              "playerSquadId", "numRecentEvents", "numPriorityEvents",
              "numSquadrons", "bytesConsumedFinal", "bytesTotal"):
        if k in info:
            print("    %-20s %s" % (k, info[k]))
    if info.get("teams"):
        print("    队伍:")
        for i, t in enumerate(info["teams"]):
            if t["name"] or t["motto"]:
                print("        [%d] flag=%d color=%d name=%r motto=%r"
                      % (i, t["flag"], t["color"], t["name"][:28], t["motto"][:40]))
    if info.get("squadrons"):
        print("    中队前 6:")
        for s in info["squadrons"][:6]:
            print("        %-22s x=%.1f y=%.1f str=%d cty=%d spc=%d"
                  % (s.get("squadName") or s.get("airbase"), s["x"], s["y"],
                     s["strength"], s["country"], s["specialty"]))

    if args.json:
        ref = json.load(open(args.json, encoding="utf-8-sig"))
        m = ref["meta"]
        print()
        print("=== 与 campaign_state.json 对拍 ===")
        checks = [
            ("version", version, m.get("version")),
            ("theater", info.get("theaterName"), m.get("theater")),
            ("scenario", info.get("scenario"), m.get("scenario")),
            ("saveName", info.get("saveFile"), m.get("saveName")),
            ("currentDay", info.get("currentDay"), m.get("currentDay")),
            ("dayZero", info.get("dayZero"), m.get("dayZero")),
            ("activeTeams", info.get("activeTeams"), m.get("activeTeams")),
            ("situation", info.get("situation"), m.get("situation")),
            ("tempo", info.get("tempo"), m.get("tempo")),
            ("endgameResult", info.get("endgameResult"), m.get("endgameResult")),
            ("campaignTimeMs", info.get("currentTime"), m.get("campaignTimeMs")),
            ("numSquadrons", info.get("numSquadrons"), len(ref.get("squadrons", []))),
        ]
        ok = 0
        for name, got, want in checks:
            good = (got == want)
            ok += good
            print("    %-16s %-28s %-28s %s"
                  % (name, repr(got)[:28], repr(want)[:28], "OK" if good else "<<< 不符"))
        print("    %d/%d 一致" % (ok, len(checks)))
        if m.get("bullseye"):
            print("    bullseye json=%s  cmp=(%s,%s)"
                  % (m["bullseye"], info.get("bullseyeX"), info.get("bullseyeY")))
        print("    forceRatios json=%s  cmp ground/air/ad/naval=%s/%s/%s/%s"
              % (m.get("forceRatios"), info.get("groundRatio"), info.get("airRatio"),
                 info.get("airDefRatio"), info.get("navalRatio")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
