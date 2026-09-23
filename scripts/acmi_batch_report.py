"""
全量 ACMI 批量分析 —— **仅供开发期质量校验，不是产品功能**。

⚠️ 重要：联队已确认**不统计现有的历史 ACMI 文件**（需求 Q3/Q6）。
本脚本因此**不参与线上业务流程**，只用于：
  * 开发期验证解析器在全量样本上的健壮性（能否 100% 解析、有无崩溃）
  * 发现未归一化的机型名，补进 `aircraft_aliases`
  * 校准判定阈值（如起降速度阈值、时间基准是否成立）

线上只处理**成员新上传的** ACMI 文件。

用法:
    python scripts/acmi_batch_report.py <ACMI目录> [--json 输出.json]

输出内容:
  1. 解析总览（文件数、行数、耗时、失败）
  2. 飞行员统计（仅用于校验解析正确性，非产品统计口径）
  3. 机型分布（含未归一的原始名）
  4. 时间范围与异常（验证时间换算是否合理）
  5. 数据质量告警汇总
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import parse_file  # noqa: E402

#: 时间合理性判据：起飞时间换算成 UTC+8 后，落在该区间之外即视为可疑
PLAUSIBLE_START_HOUR = 6
PLAUSIBLE_END_HOUR = 26      # 允许跨零点到次日 2 点


def fmt_h(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return "%dh%02dm" % (h, m)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("acmi_dir")
    ap.add_argument("--json", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.acmi_dir, "*.acmi")))
    if args.limit:
        files = files[:args.limit]
    print("发现 %d 个 ACMI 文件\n" % len(files))

    pilot = defaultdict(lambda: {
        "sorties": 0, "flight": 0.0, "landed": 0, "dist": 0.0,
        "aircraft": Counter(), "coalitions": Counter(), "files": [],
    })
    unknown_aircraft = Counter()
    warnings = Counter()
    failed = []
    total_lines = 0
    total_bytes = 0
    suspicious_times = []
    mission_dates = []
    parse_errors = 0
    t0 = time.time()

    for i, path in enumerate(files, 1):
        name = os.path.basename(path)
        try:
            info = parse_file(path)
        except Exception as exc:                     # noqa: BLE001
            failed.append((name, str(exc)))
            parse_errors += 1
            continue

        total_lines += info.line_count
        total_bytes += info.file_bytes

        # ⚠️ 用 recording_start_utc（录制起点），不是 mission_start_utc ——
        #    后者是 t=0 基准点（剧本纪元），战役存档里可以比实际飞行早好几天，
        #    拿它做"起降时刻是否合理"的判断会满屏假报警。
        if info.recording_start_utc:
            mission_dates.append(info.recording_start_utc)
            # 展示时区固定为 UTC+8（存储始终是 UTC）
            h8 = (info.recording_start_utc.hour + 8) % 24
            if not (PLAUSIBLE_START_HOUR <= h8 <= PLAUSIBLE_END_HOUR % 24):
                suspicious_times.append((name, info.recording_start_utc.isoformat(),
                                         "%02d:00 UTC+8" % h8))

        for s in info.sorties:
            p = pilot[s.raw_pilot_name]
            p["sorties"] += 1
            p["flight"] += s.flight_seconds
            p["landed"] += s.landing_count
            p["dist"] += s.distance_meters
            p["aircraft"][s.aircraft_standard_name or ("未归一:" + (s.aircraft_raw_name or "?"))] += 1
            p["coalitions"][s.coalition or "-"] += 1
            p["files"].append(name)
            if s.aircraft_standard_name is None and s.aircraft_raw_name:
                unknown_aircraft[s.aircraft_raw_name] += 1
            for w in s.warnings:
                warnings[w.split("：")[0].split(":")[0]] += 1

        for w in info.warnings:
            key = w.split("（")[0].split("(")[0]
            warnings[key] += 1

        if i % 25 == 0 or i == len(files):
            print("  已处理 %d/%d ..." % (i, len(files)))

    elapsed = time.time() - t0

    print("\n" + "=" * 78)
    print("一、解析总览")
    print("=" * 78)
    print("  文件数        : %d（失败 %d）" % (len(files), parse_errors))
    print("  总行数        : %d" % total_lines)
    print("  总大小        : %.1f MB" % (total_bytes / 1048576.0))
    print("  总耗时        : %.1f 秒" % elapsed)
    print("  平均每文件    : %.2f 秒" % (elapsed / max(1, len(files))))
    if mission_dates:
        mission_dates.sort()
        print("  任务时间范围  : %s  ~  %s (UTC)"
              % (mission_dates[0].strftime("%Y-%m-%d %H:%M"),
                 mission_dates[-1].strftime("%Y-%m-%d %H:%M")))

    print("\n" + "=" * 78)
    print("二、飞行员真实飞行统计（按总时长降序）")
    print("=" * 78)
    print("  %-12s %5s %10s %9s %11s %7s  %s"
          % ("飞行员", "架次", "总时长", "平均时长", "总航程", "降落率", "机型"))
    rows = sorted(pilot.items(), key=lambda kv: -kv[1]["flight"])
    for nm, d in rows:
        avg = d["flight"] / d["sorties"] if d["sorties"] else 0
        lr = 100.0 * d["landed"] / d["sorties"] if d["sorties"] else 0
        ac = ",".join(a for a, _ in d["aircraft"].most_common(3))
        print("  %-12s %5d %10s %9s %10.0fkm %6.0f%%  %s"
              % (nm, d["sorties"], fmt_h(d["flight"]), fmt_h(avg),
                 d["dist"] / 1000.0, lr, ac))

    tot_sorties = sum(d["sorties"] for d in pilot.values())
    tot_flight = sum(d["flight"] for d in pilot.values())
    tot_landed = sum(d["landed"] for d in pilot.values())
    tot_dist = sum(d["dist"] for d in pilot.values())
    print("  " + "-" * 74)
    print("  %-12s %5d %10s %9s %10.0fkm %6.0f%%"
          % ("合计", tot_sorties, fmt_h(tot_flight),
             fmt_h(tot_flight / max(1, tot_sorties)),
             tot_dist / 1000.0,
             100.0 * tot_landed / max(1, tot_sorties)))

    print("\n" + "=" * 78)
    print("三、机型分布")
    print("=" * 78)
    ac_all = Counter()
    for d in pilot.values():
        ac_all.update(d["aircraft"])
    for a, c in ac_all.most_common(30):
        print("  %6d  %s" % (c, a))

    if unknown_aircraft:
        print("\n  ⚠️ 未归一化机型（需补 aircraft_aliases）:")
        for a, c in unknown_aircraft.most_common(20):
            print("      %5d  %s" % (c, a))

    print("\n" + "=" * 78)
    print("四、数据质量")
    print("=" * 78)
    if suspicious_times:
        print("  ⚠️ 时间换算可疑（起飞不在合理时段）%d 条，前 10 条:" % len(suspicious_times))
        for n, iso, note in suspicious_times[:10]:
            print("      %-34s %s  → %s" % (n, iso, note))
    else:
        print("  ✅ 全部任务开始时间换算后均落在合理时段（UTC+8 6:00~2:00）")

    if warnings:
        print("\n  告警汇总:")
        for w, c in warnings.most_common(15):
            print("      %6d  %s" % (c, w))

    if failed:
        print("\n  ❌ 解析失败文件:")
        for n, e in failed[:10]:
            print("      %-34s %s" % (n, e))

    if args.json:
        out = {
            "files": len(files),
            "failed": len(failed),
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "elapsed_seconds": elapsed,
            "pilots": {nm: {"sorties": d["sorties"],
                            "flight_seconds": d["flight"],
                            "landed": d["landed"],
                            "distance_meters": d["dist"],
                            "aircraft": dict(d["aircraft"]),
                            "coalitions": dict(d["coalitions"])}
                       for nm, d in pilot.items()},
            "unknown_aircraft": dict(unknown_aircraft),
            "suspicious_times": suspicious_times,
            "warnings": dict(warnings),
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("\n已写出 JSON: %s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
