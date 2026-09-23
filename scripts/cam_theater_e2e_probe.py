"""临时自检：用真实 .cam 走通 ``TheaterData`` + ``units.read_units`` 全链路。

**仅供开发期使用**，可随时删除。

用法::

    .venv\\Scripts\\python.exe scripts\\cam_theater_e2e_probe.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.campaign.bundle import Bundle  # noqa: E402
from gfvfw.campaign.theater import TheaterData  # noqa: E402
from gfvfw.campaign.units import read_units  # noqa: E402

BMS = r"G:\BMS\Falcon BMS 4.38"
CAM = Path(BMS) / "Data" / "Add-On Hellas 2026" / "Campaign" / "Save-Day  3 02 00 46.cam"


def main() -> int:
    t0 = time.perf_counter()
    theater = TheaterData.load(BMS, "Hellas")
    print("TheaterData.load: %.3f s  root=%s" % (time.perf_counter() - t0, theater.root))

    bundle = Bundle.load(CAM)
    raw = bundle.get_by_ext(".uni")
    if raw is None:
        print("存档里没有 .uni")
        return 1
    print("存档 %s 版本 %d，.uni %d 字节" % (CAM.name, bundle.version, len(raw)))

    t0 = time.perf_counter()
    res = read_units(raw, bundle.version, theater)
    print("read_units: %.3f s  申报 %d 解出 %d 跳过 %d 消费 %d/%d (%.1f%%)"
          % (time.perf_counter() - t0, res.declared_count, len(res.units),
             res.skipped, res.consumed, res.total,
             100.0 * res.consumed / max(1, res.total)))
    print("类型统计:", dict(res.kind_counts))
    for err in res.errors[:5]:
        print("  err:", err)

    # 用剧场表把几条单位翻译成人能读的名字
    named = 0
    for u in res.units:
        name = theater.aircraft_name(u.entity_type_id)
        if name:
            named += 1
    print("能经 CT→UCD→VCD 解出载具名的记录数: %d" % named)
    for u in res.units[:6]:
        entry = theater.ct_get(u.entity_type_id)
        print("   et=%-5d kind=%-11s D=%s C=%s T=%s name=%s"
              % (u.entity_type_id, u.unit_kind,
                 entry.domain if entry else "-", entry.cls if entry else "-",
                 entry.type if entry else "-",
                 theater.aircraft_name(u.entity_type_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
