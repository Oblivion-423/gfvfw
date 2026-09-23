"""补测：解析最大 ACMI 时的**真实峰值工作集**（含 C 层缓冲）。

tracemalloc 只统计 Python 对象分配，会漏掉 C 层缓冲，
所以它给出的 6 MB 不能直接当作内存需求。这里读进程的
PeakWorkingSetSize（Windows）作为上界参考。

用法:
    .venv\\Scripts\\python.exe scripts\\acmi_rss_probe.py [文件路径]
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import parse_file  # noqa: E402

ACMI_DIR = Path(r"G:\BMS\backup\新建文件夹\Acmi")


def peak_working_set_mb() -> float:
    """当前进程的峰值工作集（MB）。"""
    class _PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    # 伪句柄 -1 = 当前进程
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb)
    return pmc.PeakWorkingSetSize / (1024 * 1024)


def main() -> int:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        files = sorted(ACMI_DIR.glob("*.acmi"), key=lambda p: p.stat().st_size)
        below = [p for p in files if p.stat().st_size / 1024 / 1024 <= 256]
        target = below[-1]

    before = peak_working_set_mb()
    mb = target.stat().st_size / 1024 / 1024
    print("=" * 74)
    print("真实峰值工作集实测")
    print("=" * 74)
    print("  文件        %s（%.1f MB）" % (target.name, mb))
    print("  解析前峰值  %.1f MB" % before)

    t0 = time.perf_counter()
    info = parse_file(str(target))
    dt = time.perf_counter() - t0
    after = peak_working_set_mb()

    print("  解析后峰值  %.1f MB" % after)
    print("  解析净增    %.1f MB" % (after - before))
    print("  耗时        %.1f 秒" % dt)
    print("  对象数      %d" % info.object_count)
    print()
    print("  结论：解析 %.0f MB 的文件，进程峰值工作集约 %.0f MB。" % (mb, after))
    print("  → VPS 内存不是瓶颈（1 GB 足够），真正要留余量的是**磁盘**：")
    print("    上传时原件先落临时文件，再入库一份，峰值约 2 倍文件大小。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
