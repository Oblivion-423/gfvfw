"""独立核对：ACMI 录制时长到底该是多少，解析器算出来是多少。

不依赖解析器内部逻辑 —— 自己把文本解开、逐行扫 `#` 时间标记。
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import parse_file  # noqa: E402

ACMI_DIR = Path(r"G:\BMS\backup\新建文件夹\Acmi")
TS = re.compile(r"^#([0-9.]+)")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".txt")),
                        zf.namelist()[0])
            return zf.read(name).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def scan(path: Path) -> dict:
    text = read_text(path)
    marks: list[float] = []
    n_lines = 0
    for line in text.splitlines():
        n_lines += 1
        if line.startswith("#"):
            m = TS.match(line)
            if m:
                marks.append(float(m.group(1)))
    gaps = [b - a for a, b in zip(marks, marks[1:])] if len(marks) > 1 else []
    return {
        "lines": n_lines,
        "marks": len(marks),
        "first": marks[0] if marks else None,
        "last": marks[-1] if marks else None,
        "monotonic": all(b >= a for a, b in zip(marks, marks[1:])),
        "max_gap": max(gaps) if gaps else None,
        "sorted": marks == sorted(marks),
    }


def main() -> int:
    files = sorted(ACMI_DIR.glob("*.acmi"), key=lambda p: p.stat().st_size)
    # 取样：小的、中的、大的各取几个，避免只看一种
    pick = files[:3] + files[len(files) // 2: len(files) // 2 + 3] + files[-3:]
    print("=" * 104)
    print("ACMI 录制时长核对：独立扫描 vs 解析器")
    print("=" * 104)
    print("%-34s %9s %9s %10s %10s %8s %10s" %
          ("文件", "首标记", "末标记", "扫描时长", "解析时长", "差", "旧算法偏差"))
    print("-" * 104)
    bad = 0
    for p in pick:
        s = scan(p)
        try:
            r = parse_file(str(p))
        except Exception as exc:  # noqa: BLE001
            print("%-40s 解析失败 %s" % (p.name[:40], str(exc)[:40]))
            continue
        span = None if s["first"] is None else s["last"] - s["first"]
        got = r.duration_seconds
        diff = None if span is None else got - span
        old_bias = None if span is None else s["last"] - span   # 旧算法错报的量
        flag = ""
        if s["first"] not in (None, 0.0):
            flag += " 首标记≠0"
        if diff is not None and abs(diff) > 0.05:
            flag += " ⚠时长不符"
            bad += 1
        print("%-34s %9.1f %9.1f %10.1f %10.1f %8.1f %10.1f%s"
              % (p.name[:34], s["first"] or 0, s["last"] or 0, span or 0,
                 got, diff or 0, old_bias or 0, flag))
    print("-" * 104)
    print("样本 %d 个，时长不符 %d 个" % (len(pick), bad))
    print("「旧算法偏差」= 末标记 − 真实时长，即修复前会多报的秒数")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
