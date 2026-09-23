"""核清 ACMI 的时间基准与真实录制时长。

对每个文件打印：
  文件名时间 / ReferenceTime / 首个时间标记 / 末个时间标记 / 真实时长
以及三种可能的锚点算法各自推出的"录制起始时刻"，看哪个自洽。
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import parse_filename_time  # noqa: E402

ACMI_DIR = Path(r"G:\BMS\backup\新建文件夹\Acmi")
TS = re.compile(r"^#([0-9.]+)")
REF = re.compile(r"ReferenceTime=([0-9T:\-Z]+)")


def info(path: Path) -> dict:
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".txt")),
                        zf.namelist()[0])
            text = zf.read(name).decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    ref = None
    marks: list[float] = []
    for line in text.splitlines():
        if ref is None:
            m = REF.search(line)
            if m:
                try:
                    ref = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    pass
        if line.startswith("#"):
            m2 = TS.match(line)
            if m2:
                marks.append(float(m2.group(1)))
    return {"ref": ref, "first": marks[0] if marks else None,
            "last": marks[-1] if marks else None, "n": len(marks)}


def main() -> int:
    files = sorted(ACMI_DIR.glob("*.acmi"), key=lambda p: p.stat().st_size)
    pick = files[:2] + files[len(files) // 2 - 1: len(files) // 2 + 1] + files[-2:]
    print("=" * 110)
    print("%-34s %-17s %-17s %9s %9s %9s %11s" %
          ("文件", "文件名时间", "ReferenceTime", "首标记", "末标记", "真实时长", "文件名-末"))
    print("-" * 110)
    agree = 0
    for p in pick:
        d = info(p)
        ft = parse_filename_time(str(p.name))
        ref = d["ref"]
        span = (d["last"] - d["first"]) if d["first"] is not None else None
        anchor = (ft - timedelta(seconds=d["last"])) if (ft and d["last"]) else None
        same = (ref is not None and anchor is not None
                and abs((ref - anchor).total_seconds()) < 60)
        agree += same
        print("%-34s %-17s %-17s %9.1f %9.1f %9.1f %11s %s" %
              (p.name[:34],
               ft.strftime("%Y-%m-%d %H:%M") if ft else "-",
               ref.strftime("%Y-%m-%d %H:%M") if ref else "(无)",
               d["first"] or 0, d["last"] or 0, span or 0,
               anchor.strftime("%m-%d %H:%M") if anchor else "-",
               "✔一致" if same else ""))
    print("-" * 110)
    print("样本 %d 个；「文件名时间 − 末标记」与 ReferenceTime 一致的有 %d 个"
          % (len(pick), agree))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
