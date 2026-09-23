"""
探查：能否用「文件结束时速度为零」判定降落。

用户给出的判据：
  起飞 = 该文件包含该飞行员（出现即算一次）
  降落 = 该单位在文件结束时速度为零

本脚本验证 ACMI 中速度信息是否可用：
  - CAS / IAS / Mach 的更新频率
  - 文件结束时这些值能否取到
  - 数值分布（是否存在大量 0 值）
  - 位置速度（由 u/v 或 lon/lat 差分）作为兜底
"""
import io
import math
import os
import re
import sys
import zipfile
from collections import defaultdict

TAIL_RE = re.compile(r"(?:,[A-Z][A-Za-z0-9_]*=[^,]*)+$")
TS_RE = re.compile(r"^#([0-9.]+)")
ID_RE = re.compile(r"^[0-9A-Fa-f]+$")


def open_stream(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":
        zf = zipfile.ZipFile(path)
        return io.TextIOWrapper(zf.open("acmi.txt"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def main():
    paths = sys.argv[1:]
    for path in paths:
        print("=" * 78)
        print(os.path.basename(path), "(%.1f MB)" % (os.path.getsize(path) / 1048576.0))
        # oid -> last known values
        state = {}
        pilots = {}
        cur = 0.0
        with open_stream(path) as fh:
            for raw in fh:
                line = raw.rstrip("\r\n")
                if not line or line.startswith("//"):
                    continue
                if line[0] == "#":
                    m = TS_RE.match(line)
                    if m:
                        try:
                            cur = float(m.group(1))
                        except ValueError:
                            pass
                    continue
                if "," not in line:
                    continue
                oid, rest = line.split(",", 1)
                if not ID_RE.match(oid):
                    continue
                body = rest
                tparts = []
                if rest.startswith("T="):
                    after = rest[2:]
                    m = TAIL_RE.search(after)
                    if m:
                        tstr, body = after[:m.start()], after[m.start() + 1:]
                    else:
                        tstr, body = after, ""
                    tparts = [p.strip() for p in (tstr.split("|") if "|" in tstr else tstr.split(","))]
                st = state.setdefault(oid, {"cas": None, "ias": None, "mach": None,
                                            "alt": None, "u": None, "v": None,
                                            "lon": None, "lat": None, "ts": None,
                                            "cas_n": 0, "mach_n": 0, "uv_n": 0})
                if tparts:
                    if len(tparts) > 0 and tparts[0]:
                        try: st["lon"] = float(tparts[0])
                        except ValueError: pass
                    if len(tparts) > 1 and tparts[1]:
                        try: st["lat"] = float(tparts[1])
                        except ValueError: pass
                    if len(tparts) > 2 and tparts[2]:
                        try: st["alt"] = float(tparts[2])
                        except ValueError: pass
                    if len(tparts) > 6 and tparts[6]:
                        try:
                            st["u"] = float(tparts[6]); st["uv_n"] += 1
                        except ValueError: pass
                    if len(tparts) > 7 and tparts[7]:
                        try: st["v"] = float(tparts[7])
                        except ValueError: pass
                if body:
                    for k, dst in (("CAS", "cas"), ("IAS", "ias"), ("Mach", "mach")):
                        m = re.search(k + r"=([0-9.]+)", body)
                        if m:
                            try:
                                st[dst] = float(m.group(1))
                                if k == "CAS": st["cas_n"] += 1
                                if k == "Mach": st["mach_n"] += 1
                            except ValueError:
                                pass
                    m = re.search(r"Pilot=([^,]+)", body)
                    if m:
                        pilots[oid] = m.group(1).strip()
                if oid in pilots:
                    st["ts"] = cur

        print("  %-12s %-9s %-8s %-8s %-8s %-8s %-6s %-6s" %
              ("pilot", "CAS_last", "Mach_l", "CAS#", "Mach#", "ts_last", "alt", "uv#"))
        for oid, nm in sorted(pilots.items(), key=lambda x: x[1]):
            st = state.get(oid, {})
            print("  %-12s %-9s %-8s %-8d %-8d %-8.0f %-6s %-6d" % (
                nm,
                "%.1f" % st["cas"] if st.get("cas") is not None else "-",
                "%.3f" % st["mach"] if st.get("mach") is not None else "-",
                st.get("cas_n", 0), st.get("mach_n", 0),
                st.get("ts") or 0.0,
                "%.1f" % st["alt"] if st.get("alt") is not None else "-",
                st.get("uv_n", 0)))
        print()


if __name__ == "__main__":
    main()
