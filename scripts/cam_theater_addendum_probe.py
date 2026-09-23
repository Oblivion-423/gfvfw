"""临时自检：JsonExporter.cs 所需的追加接口 + Hellas 目标类别分布。

**仅供开发期使用**，可随时删除。

用法::

    .venv\\Scripts\\python.exe scripts\\cam_theater_addendum_probe.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.campaign.theater import (  # noqa: E402
    CALLSIGN_BASE, MISSION_NAME_BASE, TheaterData,
)

BMS = Path(r"G:\BMS\Falcon BMS 4.38")

#: 用户给的参考分布（来自 campaign_state.json 的 objectives 数组）
REFERENCE = {
    "Village": 2860, "Range": 2621, "Town": 823, "City": 110, "Port": 78,
    "Army Base": 77, "Factory": 73, "Power / Dam": 50, "Airbase": 49,
    "Airstrip": 40, "Radar Site": 40, "SAM / AAA Site": 36, "Depot": 21,
    "Misc": 19, "Bridge": 15,
}


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:
    addon2026 = TheaterData.load(BMS, "Add-On Hellas 2026", cache=False)
    hellas = TheaterData.load(BMS, "Hellas", cache=False)

    hr("1. CampObjData 条目数")
    for label, td in (("Add-On Hellas 2026", addon2026), ("Add-On Hellas", hellas)):
        objs = td.all_camp_objs()
        print("  [%s] root=%s" % (label, td.root))
        print("    all_camp_objs() = %d 条；camp_obj_data = %d 条；"
              "camp_id 范围 %d..%d"
              % (len(objs), len(td.camp_obj_data),
                 min(o.camp_id for o in objs), max(o.camp_id for o in objs)))
        first = objs[0]
        print("    首条: camp_id=%d name=%r ocd_index=%d heading=%.3f "
              "raw pos=(%.3f, %.3f, %.3f)"
              % (first.camp_id, first.name, first.ocd_index, first.heading,
                 first.position_x, first.position_y, first.position_z))
    print("  all_camp_objs 顺序 = 文件次序:",
          [o.name for o in addon2026.all_camp_objs()[:3]])

    hr("2. 原始坐标严格性（不得换算）")
    # 与 XML 文本逐字节对拍
    import re
    xml = (addon2026.root / "Campaign" / "CampObjData.XML").read_text(
        encoding="utf-8-sig")
    blocks = re.findall(r"<CampObj CampId=\"(\d+)\">(.*?)</CampObj>", xml, re.S)
    ok = 0
    for camp_id, body in blocks[:200]:
        x = float(re.search(r"<PositionX>([^<]+)</PositionX>", body).group(1))
        y = float(re.search(r"<PositionY>([^<]+)</PositionY>", body).group(1))
        o = addon2026.camp_obj(int(camp_id))
        assert o is not None
        if o.position_x == x and o.position_y == y and o.pos_x == x and o.pos_y == y:
            ok += 1
    print("  前 200 条 position_x/y 与 XML 文本逐值相等: %d/200" % ok)
    print("  样例: camp_id=%d pos_x=%.3f (alias position_x=%.3f) —— 未做任何缩放"
          % (addon2026.all_camp_objs()[0].camp_id,
             addon2026.all_camp_objs()[0].pos_x,
             addon2026.all_camp_objs()[0].position_x))
    print("  网格换算（仅演示，本模块不做）: %.3f / %.3f"
          % (addon2026.all_camp_objs()[0].pos_x / 3280.84,
             addon2026.all_camp_objs()[0].pos_y / 3280.84))

    hr("3. ocd_type_name / objective_type_name 一致性")
    bad = 0
    for o in addon2026.all_camp_objs():
        a = addon2026.ocd_type_name(o.ocd_index)
        b = addon2026.objective_type_for_ocd(o.ocd_index)
        if a != b:
            bad += 1
    print("  两方法对全部 %d 条结果不一致数: %d" % (len(addon2026.camp_obj_data), bad))
    print("  OcdIndex 范围: %d..%d"
          % (min(o.ocd_index for o in addon2026.camp_obj_data.values()),
             max(o.ocd_index for o in addon2026.camp_obj_data.values())))
    for ocd in (16, 33, 656, 657, 868):
        ct_idx = addon2026._ocd_to_ct.get(ocd)
        print("    OcdIndex=%-4d -> CtIdx=%-5s -> CT.(D,C,T)=%s -> %s"
              % (ocd, ct_idx,
                 None if ct_idx is None else
                 (addon2026.entries[ct_idx].domain, addon2026.entries[ct_idx].cls,
                  addon2026.entries[ct_idx].type),
                 addon2026.ocd_type_name(ocd)))
    # objective_type_name(CtEntry) 与 ocd_type_name(OcdIndex) 对同一条目标是否一致
    mism = 0
    checked = 0
    for o in addon2026.all_camp_objs():
        ct_idx = addon2026._ocd_to_ct.get(o.ocd_index)
        if ct_idx is None:
            continue
        by_ct = addon2026.objective_type_name(addon2026.entries[ct_idx])
        checked += 1
        if by_ct != addon2026.ocd_type_name(o.ocd_index):
            mism += 1
    print("  对同一条目标：objective_type_name(CtEntry) 与 ocd_type_name(OcdIndex)"
          " 抽查 %d 条，不一致 %d 条" % (checked, mism))

    hr("4. Hellas 2026 目标类别名分布（全部 %d 条）"
       % len(addon2026.camp_obj_data))
    dist = Counter(addon2026.ocd_type_name(o.ocd_index) for o in addon2026.all_camp_objs())
    total = sum(dist.values())
    print("  %-24s %6s %8s %8s %6s" % ("类别名", "本模块", "参考", "差值", "占比"))
    for name, cnt in dist.most_common():
        ref = REFERENCE.get(name)
        diff = "" if ref is None else ("%+d" % (cnt - ref))
        print("  %-24s %6d %8s %8s %5.1f%%"
              % (name, cnt, "-" if ref is None else ref, diff or "-",
                 100.0 * cnt / total))
    print("  %-24s %6d %8d %8s" % ("合计", total, sum(REFERENCE.values()), ""))
    print("\n  参考里有、本模块未产出的类别:",
          sorted(set(REFERENCE) - set(dist)) or "（无）")
    print("  本模块产出、参考里没有的类别:",
          sorted(set(dist) - set(REFERENCE)) or "（无）")
    diff_names = {n: dist.get(n, 0) - v for n, v in REFERENCE.items()
                  if dist.get(n, 0) != v}
    print("  与参考不一致的类别:", diff_names or "（全部分毫不差）")

    hr("5. 参考分布覆盖率")
    covered = sum(1 for n in REFERENCE if n in dist)
    print("  参考列出的 %d 个类别中，本模块产出 %d 个" % (len(REFERENCE), covered))

    hr("6. get_callsign（StringsTable.GetCallsign）")
    print("  CALLSIGN_BASE =", CALLSIGN_BASE)
    for cid, num in ((0, 1), (5, 3), (12, 4), (0, 0), (250, 2)):
        print("    get_callsign(%-3d, %d) = %r"
              % (cid, num, hellas.get_callsign(cid, num)))
    # 与 strings.txt 索引直接对照
    for cid in (0, 5, 12):
        print("    strings[%d] = %r  -> 呼号 %r"
              % (CALLSIGN_BASE + cid, hellas.string(CALLSIGN_BASE + cid),
                 hellas.get_callsign(cid, 1)))

    hr("7. 任务名 string(300 + code)")
    for code in (0, 1, 2, 5, 17, 40):
        print("    string(300+%-2d) = string(%-3d) = %r"
              % (code, MISSION_NAME_BASE + code, hellas.string(MISSION_NAME_BASE + code)))

    hr("8. aircraft_entry（VcdTable.GetAircraftEntry）")
    print("  %-8s %-9s %-16s %-6s %-7s %-8s %-6s %-6s %-6s %-5s %-6s %-5s %-5s"
          % ("et", "kind", "Name", "Nctr", "MaxSpd", "CruiseA", "FuelW",
             "Crew", "SvcS", "SvcE", "RadIdx", "RCS", "RngA"))
    v = hellas.aircraft_entry(6570)
    assert v is not None
    for et in (6570, 6585, 505, 827, 2600, 225):
        entry = hellas.ct_get(et)
        ucd = hellas.unit_def_by_entity_type(et) if entry else None
        e = hellas.aircraft_entry(et, entry, ucd)
        if e is None:
            print("  %-8d %-9s -> None" % (et, entry.entity_type_name if entry else "-"))
            continue
        print("  %-8d %-9s %-16s %-6s %-7d %-8d %-6d %-6d %-6d %-6d %-6d %-6d %-5d %-6d"
              % (et, entry.entity_type_name, e.Name, e.Nctr, e.MaxSpeed,
                 e.CruiseAlt, e.FuelWeight, e.NumberOfCrew, e.InServiceStart,
                 e.InServiceEnd, e.RadarIdx, e.RadarCs, e.RngAir, e.MaxAlt))
    print("  同一对象上 HitAir=%d" % v.HitAir)
    # 三条签名路径的结果应完全一致
    for et in (6570, 6585, 505):
        entry = hellas.ct_get(et)
        ucd = hellas.unit_def_by_entity_type(et) if entry else None
        a = hellas.aircraft_entry(et)
        b = hellas.aircraft_entry(et, entry)
        c = hellas.aircraft_entry(et, entry, ucd)
        d = hellas.aircraft_entry(et, None, ucd)
        same = len({id(x) for x in (a, b, c, d)}) == 1
        print("    et=%-6d 四种调用方式同一对象: %s (%s)"
              % (et, same, a.Name if a else None))
    # 不可解析的情况
    print("  不可解析: et=99 ->", hellas.aircraft_entry(99))
    print("             et=2600 ->", hellas.aircraft_entry(2600))

    hr("9. 空剧场容错")
    empty = TheaterData.load(BMS, "NoSuchTheater", cache=False)
    print("  all_camp_objs() =", empty.all_camp_objs())
    print("  ocd_type_name(16) =", repr(empty.ocd_type_name(16)))
    print("  get_callsign(0, 1) =", repr(empty.get_callsign(0, 1)))
    print("  string(300) =", repr(empty.string(300)))
    print("  aircraft_entry(6570) =", empty.aircraft_entry(6570))

    print("\n全部检查完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
