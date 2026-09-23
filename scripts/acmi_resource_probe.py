"""实测：解析大 ACMI 的耗时与内存占用（VPS 规格评估用）。

⚠️ 时间与内存**分两轮**测：
   ``tracemalloc`` 会给每次分配加钩子，实测能把解析拖慢数倍。
   混在一起测会得出一个既不是"真实耗时"也不是"真实内存"的假数字。

只读不写。

用法:
    .venv\\Scripts\\python.exe scripts\\acmi_resource_probe.py            # 全部样本
    .venv\\Scripts\\python.exe scripts\\acmi_resource_probe.py --time-only
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import parse_file  # noqa: E402

ACMI_DIR = Path(r"G:\BMS\backup\新建文件夹\Acmi")
LIMIT_MB = 256          # 与 settings.max_acmi_bytes 对应


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f GB" % n


def sample():
    files = sorted(ACMI_DIR.glob("*.acmi"), key=lambda p: p.stat().st_size)
    below = [p for p in files if p.stat().st_size / 1024 / 1024 <= LIMIT_MB]
    over = [p for p in files if p.stat().st_size / 1024 / 1024 > LIMIT_MB]
    # 关键样本：中等一个 + 最大的三个（最大者决定规格）
    pick = [below[len(below) // 2]] + below[-3:]
    seen = set()
    return [p for p in pick if not (p in seen or seen.add(p))], len(files), len(below), len(over)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--time-only", action="store_true",
                    help="只测耗时（不开 tracemalloc）")
    args = ap.parse_args()

    pick, n_all, n_below, n_over = sample()
    print("=" * 92, flush=True)
    print("ACMI 解析资源占用实测（VPS 规格评估用）", flush=True)
    print("=" * 92, flush=True)
    print("  文件总数 %d，其中 ≤%d MB 的 %d 个、超限 %d 个"
          % (n_all, LIMIT_MB, n_below, n_over), flush=True)
    print("  样本 %d 个（最大的决定 VPS 规格）" % len(pick), flush=True)

    print("\n[第 1 轮] 真实耗时（不开 tracemalloc）", flush=True)
    print("  %-40s %10s %9s %11s" % ("文件", "体积", "对象数", "耗时"), flush=True)
    print("  " + "-" * 74, flush=True)
    times = []
    for p in pick:
        mb = p.stat().st_size / 1024 / 1024
        t0 = time.perf_counter()
        try:
            info = parse_file(str(p))
        except Exception as exc:  # noqa: BLE001
            print("  %-40s %8.1f MB  失败 %s" % (p.name[:40], mb, str(exc)[:36]),
                  flush=True)
            continue
        dt = time.perf_counter() - t0
        times.append((p.name, mb, info.object_count, dt))
        print("  %-40s %8.1f MB %9d %10.1fs" % (p.name[:40], mb,
                                                info.object_count, dt), flush=True)

    if args.time_only or not times:
        _conclude(times, None)
        return 0

    print("\n[第 2 轮] 峰值内存（开 tracemalloc，只测最大的一个）", flush=True)
    biggest = max(times, key=lambda x: x[1])
    target = next(p for p in pick if p.name == biggest[0])
    tracemalloc.start()
    info = parse_file(str(target))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("  %-40s %8.1f MB  峰值 %s（Python 侧，C 层缓冲不计）"
          % (target.name[:40], biggest[1], human(peak)), flush=True)

    _conclude(times, peak)
    return 0


def _conclude(times, peak) -> None:
    print("\n" + "=" * 92, flush=True)
    if times:
        worst_t = max(t[3] for t in times)
        worst_mb = max(t[1] for t in times)
        print("  最坏：%.1f MB 的文件耗时 %.1f 秒（%.1f 分钟）"
              % (worst_mb, worst_t, worst_t / 60), flush=True)
    if peak:
        print("  峰值内存（Python 侧）：%s" % human(peak), flush=True)
    print("\n  对部署的结论：", flush=True)
    print("    * 反代读取超时必须远大于上面的耗时 ——", flush=True)
    print("      Caddy 默认不设超时（OK）；Nginx 默认 60s 会 504。", flush=True)
    print("    * 解析是**流式**的，内存不是瓶颈（实测峰值工作集仅数十 MB，", flush=True)
    print("      见 acmi_rss_probe.py）。真正要留余量的是**磁盘**：", flush=True)
    print("      上传时原件先落临时文件、再入库一份，峰值约 2 倍文件大小。", flush=True)
    print("    * 应用是单进程串行解析，一个人传大文件时其他人会等 ——", flush=True)
    print("      若这不可接受，需要引入后台任务队列（当前未做）。", flush=True)
    print("=" * 92, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
