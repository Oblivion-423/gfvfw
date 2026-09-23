"""统计：解析 .cam 到底需要 BMS 安装目录里的哪些文件、多大。

只读不写，不复制任何东西。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.campaign.maps import find_theater_maps  # noqa: E402
from gfvfw.campaign.theater import _find_dir, _find_file, theater_root  # noqa: E402
from gfvfw.config import settings  # noqa: E402


def sizeof(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return 0


def mb(n: int) -> str:
    return "%.1f MB" % (n / (1024 * 1024))


def main() -> int:
    inst = Path(settings.bms_install_path or "")
    print("=" * 74)
    print("BMS 安装目录：%s" % inst)
    print("存在：%s" % inst.is_dir())
    print("=" * 74)
    if not inst.is_dir():
        print("未配置或不存在，无法统计")
        return 1

    data = inst / "Data"
    objects = data / "TerrData" / "Objects"
    campaign = data / "Campaign"

    # ---- 必需：Objects 下的 6 张类表 ----
    print("\n[必需] TerrData/Objects 类表")
    total_required = 0
    for names in (("Falcon4_CT.xml", "FALCON4_CT.XML"),
                  ("Falcon4_UCD.xml", "FALCON4_UCD.XML"),
                  ("Falcon4_VCD.xml", "FALCON4_VCD.XML"),
                  ("Falcon4_WCD.xml", "FALCON4_WCD.XML"),
                  ("Falcon4_RCD.xml", "FALCON4_RCD.XML"),
                  ("Falcon4_FCD.xml", "FALCON4_FCD.XML")):
        p = _find_file(objects, *names)
        n = sizeof(p) if p else 0
        total_required += n
        print("  %-20s %10s  %s" % (names[0], mb(n) if p else "缺失",
                                    p.name if p else ""))

    # ---- 必需：Campaign 下 2 个 ----
    print("\n[必需] Campaign 目标清单与字符串表")
    for names in (("CampObjData.xml", "CampObjData.XML"),
                  ("strings.txt", "Strings.txt")):
        p = _find_file(campaign, *names)
        n = sizeof(p) if p else 0
        total_required += n
        print("  %-20s %10s  %s" % (names[0], mb(n) if p else "缺失",
                                    p.name if p else ""))

    # ---- 必需：ObjectiveRelatedData（目标类别链路） ----
    ocd = _find_dir(objects, "ObjectiveRelatedData")
    n_ocd = sizeof(ocd) if ocd else 0
    n_files = len(list(ocd.rglob("*"))) if ocd else 0
    total_required += n_ocd
    print("\n[必需] ObjectiveRelatedData（OCD → CtIdx → CT 的目标类别链路）")
    print("  %-20s %10s  %d 个条目" % ("ObjectiveRelatedData", mb(n_ocd), n_files))

    print("\n  ── 必需小计：%s ──" % mb(total_required))

    # ---- 可选：投影参数 ----
    print("\n[可选] TerrData/<剧场>/NewTerrain/Theater.txt（投影参数，Add-On 常缺）")
    for th in ("Korea", "Hellas", "Balkans", "Israel"):
        root = theater_root(inst, th)
        td = _find_addon_dir = None
        try:
            from gfvfw.campaign.theater import _find_addon_dir as fad
            td = fad(inst / "Data" / "TerrData", th)
        except Exception:  # noqa: BLE001
            pass
        p = _find_file(td / "NewTerrain", "Theater.txt") if td is not None else None
        print("  %-10s %s" % (th, ("%s  %s" % (mb(sizeof(p)), p)) if p else "缺失"))

    # ---- 可选：剧场底图 ----
    print("\n[可选] 剧场底图（不给也能用：SVG 仍然画目标点与单位，只是没有背景图）")
    map_total = 0
    for th in ("Korea", "Hellas", "Balkans", "Israel"):
        try:
            maps = find_theater_maps(theater_root(inst, th), None, th)
        except Exception as exc:  # noqa: BLE001
            print("  %-10s 扫描失败 %s" % (th, exc))
            continue
        n = sum(m.size_bytes for m in maps)
        map_total += n
        print("  %-10s %2d 张  %10s" % (th, len(maps), mb(n)))
        for m in maps[:3]:
            print("        %-46s %9s" % (Path(m.path).name, mb(m.size_bytes)))
    print("  ── 底图合计：%s ──" % mb(map_total))

    print("\n" + "=" * 74)
    print("最小数据集（必需）：%s" % mb(total_required))
    print("加上全部 4 个剧场底图：%s" % mb(total_required + map_total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
