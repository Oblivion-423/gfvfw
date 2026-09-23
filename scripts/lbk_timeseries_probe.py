"""用**同一飞行员不同时间的 Logbook 存档**来判定各字段的含义。

思路（纯经验、可复现）
----------------------
``G:\\BMS`` 下同一个呼号有多份不同日期的 ``.lbk``（备份与当前存档）。
解密后把同一人的多个时间点排成一列：

* **单调不减**的字段 → 累计计数（架次、击杀、时长…）
* **上升后回落** → 当前值/连击数/评分
* **不变** → 与飞行无关的静态信息或未用字段
* **字符串** → 直接可读，无需推断

比"猜偏移"可靠得多：不依赖对反汇编的理解，只看数据本身的行为。

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_timeseries_probe.py
"""
from __future__ import annotations

import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gfvfw import lbk_parser as LB  # noqa: E402

ROOTS = (
    pathlib.Path(r"G:\BMS\Falcon BMS 4.38\User\Config"),
    pathlib.Path(r"G:\BMS\backup\Config"),
    pathlib.Path(r"G:\BMS\backup\新建文件夹"),
    pathlib.Path(r"G:\BMS\backup\新建文件夹\Config"),
)


def collect() -> dict[str, list[tuple[str, pathlib.Path, LB.LbkRecord]]]:
    out: dict[str, list] = defaultdict(list)
    seen: set[bytes] = set()
    for root in ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.lbk"):
            data = p.read_bytes()
            if data in seen:
                continue
            seen.add(data)
            mtime = p.stat().st_mtime
            import datetime
            stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            try:
                rec = LB.parse(data)
            except LB.LbkError as exc:
                print("跳过 %s：%s" % (p.name, exc))
                continue
            out[p.stem].append((stamp, p, rec))
    for k in out:
        out[k].sort(key=lambda t: t[0])
    return out


def main() -> int:
    groups = collect()
    print("呼号 %d 个，样本 %d 份" % (len(groups), sum(len(v) for v in groups.values())))
    print()

    for callsign, rows in sorted(groups.items()):
        print("=" * 96)
        print("%s —— %d 份存档：%s"
              % (callsign, len(rows), ", ".join(s for s, _, _ in rows)))
        print("=" * 96)
        for stamp, path, rec in rows:
            print("  %s  文件内姓名=%-12s 呼号=%-10s 中队=%-12s 日期=%s 军衔=%s"
                  % (stamp, rec.name or "-", rec.callsign or "-",
                     rec.squadron or "-", rec.fields.get("date") or "-",
                     rec.rank_code or "-"))
            print("        飞行小时=%-10s Ace=%s"
                  % (rec.flight_hours, rec.fields.get("ace_factor")))
        if len(rows) > 1:
            print("  --- 逐字段随时间的变化 ---")
            keys = [k for k in rows[0][2].fields if not k.startswith("name")
                    and not k.startswith("callsign")]
            for k in keys:
                vals = [r.fields.get(k) for _, _, r in rows]
                if len(set(map(str, vals))) == 1:
                    continue
                mono = all(
                    (isinstance(vals[i], (int, float)) and isinstance(vals[i+1], (int, float))
                     and vals[i+1] >= vals[i])
                    for i in range(len(vals) - 1))
                tag = "单调不减(计数?)" if mono else "有回落"
                print("    %-24s %-46s %s" % (k, str(vals), tag))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
