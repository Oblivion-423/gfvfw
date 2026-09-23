"""临时自检脚本：验证 :mod:`gfvfw.campaign.theater` 对真实 BMS 数据的加载。

**仅供开发期使用**，不属于产品代码，可以随时删除。

用法::

    .venv\\Scripts\\python.exe scripts\\cam_theater_probe.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.campaign.theater import (  # noqa: E402
    OBJECTIVE_TYPE_NAMES, TheaterData, clear_cache, theater_root,
)

BMS = Path(r"G:\BMS\Falcon BMS 4.38")

#: SITREP 文档给出的目标类别名全表
SITREP_NAMES = [
    "Airbase", "Airstrip", "Army Base", "Headquarters", "Bridge",
    "SAM / AAA Site", "SAM Site (Dedicated)", "Radar Site", "Depot",
    "Refinery", "Factory", "Chemical Plant", "Nuclear Plant", "Port",
    "Power / Dam", "City", "Town", "Village", "Intersection", "Misc",
    "Special", "Nav Beacon", "Radio Tower", "Range", "Border",
    "Fortification", "Mountain Pass",
]


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    hr("0. 路径解析")
    for th in ("Korea", "Hellas"):
        print("  %-8s root = %s" % (th, theater_root(BMS, th)))
    print("  Korea  root exists:", theater_root(BMS, "Korea").is_dir())
    print("  Hellas root exists:", theater_root(BMS, "Hellas").is_dir())

    hr("1. CT 条数与前 3 条字段")
    for th in ("Korea", "Hellas"):
        t0 = time.perf_counter()
        td = TheaterData.load(BMS, th)
        dt = time.perf_counter() - t0
        print("  [%s] entries=%d  首载耗时 %.3f s" % (th, len(td.entries), dt))
        for e in td.entries[:3]:
            print("    num=%-5d domain=%d cls=%d type=%-3d subtype=%-3d "
                  "specific=%-3d owner=%d entity_type=%d entity_idx=%d"
                  % (e.num, e.domain, e.cls, e.type, e.subtype, e.specific,
                     e.owner, e.entity_type, e.entity_idx))
        print("    summary:", td.summary())

    hr("2. Hellas 全量加载耗时（本剧场）")
    clear_cache()
    times = []
    hellas = None
    for i in range(3):
        clear_cache()
        t0 = time.perf_counter()
        hellas = TheaterData.load(BMS, "Hellas")
        dt = time.perf_counter() - t0
        times.append(dt)
        print("    run %d: %.3f s  (缓存已清)" % (i + 1, dt))
    assert hellas is not None
    print("    最快 %.3f s / 最慢 %.3f s / 中位 %.3f s"
          % (min(times), max(times), sorted(times)[1]))
    t0 = time.perf_counter()
    hellas2 = TheaterData.load(BMS, "Hellas")
    print("    第 4 次（用缓存）: %.6f s, 同一对象=%s"
          % (time.perf_counter() - t0, hellas2 is hellas))

    hr("3. ct_get 边界行为")
    for eid in (101, 2600, 99, 10_000_000, -5):
        got = hellas.ct_get(eid)
        print("    ct_get(%-9d) -> %s" % (
            eid, "None" if got is None
            else "num=%d D=%d C=%d T=%d ET=%d EI=%d"
                 % (got.num, got.domain, got.cls, got.type,
                    got.entity_type, got.entity_idx)))

    hr("4. UCD / VCD / WCD / RCD / FCD / CampObj / strings 条数")
    for th in ("Korea", "Hellas"):
        td = TheaterData.load(BMS, th)
        s = td.summary()
        print("  [%s] ucd=%d vcd=%d wcd=%d rcd=%d fcd=%d campobj=%d strings=%d "
              "ocd映射=%d 目标模板=%d"
              % (th, s["ucd"], s["vcd"], s["wcd"], s["rcd"], s["fcd"],
                 s["camp_obj"], s["strings"], s["ocd_mappings"],
                 s["objective_templates"]))

    hr("5. sam_radii 抽样")
    radii = hellas.sam_radii
    print("    系统数: %d" % len(radii))
    for key in sorted(radii, key=lambda k: -radii[k]["long"]):
        r = radii[key]
        print("      %-10s long=%7.1f NM  medium=%7.1f NM  short=%6.1f NM"
              % (key, r["long"], r["medium"], r["short"]))
    print("    兜底项与 RCD 补充项应存在:",
          {k: k in radii for k in ("THAAD", "KN-06", "KM-SAM")})
    print("    未匹配到 RCD 的系统 long/medium 对照（Korea 有 KM-SAM）：")
    korea_r = TheaterData.load(BMS, "Korea").sam_radii
    for key in ("KM-SAM", "HAWK", "PATRIOT", "NIKE"):
        print("      Korea %-9s -> %s" % (key, korea_r.get(key)))
    print("    SA-12A/B 是否合并为 SA-12:", "SA-12" in radii,
          "| 未合并残留:", [k for k in radii if k.startswith("SA-12")])

    hr("6. 目标类别名核对（SITREP 名单 %d 项）" % len(SITREP_NAMES))
    all_names: set[str] = set()
    for th in ("Korea", "Hellas"):
        td = TheaterData.load(BMS, th)
        names = td.objective_type_names()
        all_names |= set(names)
        print("  [%s] CampObjData 实际产出的类别名 %d 种:" % (th, len(names)))
        for n in names:
            print("      %s" % n)
    print("\n  两剧场合并产出: %d 种" % len(all_names))
    hit = [n for n in SITREP_NAMES if n in all_names]
    miss = [n for n in SITREP_NAMES if n not in all_names]
    print("  SITREP 名单命中 %d/%d" % (len(hit), len(SITREP_NAMES)))
    print("  未产出:", miss if miss else "（无）")
    extra = sorted(all_names - set(SITREP_NAMES))
    print("  产出但不在名单里的:", extra if extra else "（无）")
    print("  名单本身有 %d 项，映射表 OBJECTIVE_TYPE_NAMES 有 %d 项"
          % (len(SITREP_NAMES), len(OBJECTIVE_TYPE_NAMES)))

    hr("7. objective_type_name(CtEntry) 抽样（走 D=3 C=4 目标模板）")
    td = hellas
    shown = 0
    for e in td.entries:
        if e.domain == 3 and e.cls == 4:
            print("    num=%-5d type=%-3d -> %s"
                  % (e.num, e.type, td.objective_type_name(e)))
            shown += 1
            if shown >= 8:
                break
    # 正样本：CampObjData 里的真实目标
    print("  -- 用真实 CampObjData 记录验证 --")
    for camp_id in list(td.camp_obj_data)[:8]:
        c = td.camp_obj(camp_id)
        assert c is not None
        print("    campId=%-6d %-28s ocd=%-5d -> %s"
              % (c.camp_id, c.name[:28], c.ocd_index,
                 td.objective_type_for_ocd(c.ocd_index)))

    hr("8. 编码 / 非 ASCII 校验")
    korea = TheaterData.load(BMS, "Korea")
    accents = [c.name for c in korea.camp_obj_data.values()
               if any(ord(ch) > 127 for ch in c.name)]
    print("  Korea 非 ASCII 目标名 %d 条，样例 %s" % (len(accents), accents[:5]))
    greek = [c.name for c in hellas.camp_obj_data.values()
             if any(ord(ch) > 127 for ch in c.name)]
    print("  Hellas 非 ASCII 目标名 %d 条，样例 %s" % (len(greek), greek[:5]))
    print("  是否出现替换字符 U+FFFD:",
          any("\ufffd" in c.name for c in korea.camp_obj_data.values())
          or any("\ufffd" in c.name for c in hellas.camp_obj_data.values()))
    print("  Korea strings[10..13]:", [korea.string(i) for i in range(10, 14)])
    print("  Hellas strings[10..13]:", [hellas.string(i) for i in range(10, 14)])

    hr("9. 缺失文件容错（不存在的剧场/安装目录）")
    empty = TheaterData.load(BMS, "NoSuchTheater", cache=False)
    print("  NoSuchTheater: entries=%d ucd=%d campobj=%d sam=%d missing=%s"
          % (len(empty.entries), len(empty.ucd), len(empty.camp_obj_data),
             len(empty.sam_radii), empty.missing))
    print("  ct_get(101) ->", empty.ct_get(101))
    print("  string(0) ->", repr(empty.string(0)))
    print("  sam_radii ->", empty.sam_radii)
    bogus = TheaterData.load(r"C:\definitely\not\here", "Korea", cache=False)
    print("  伪安装路径: entries=%d missing=%s" % (len(bogus.entries), bogus.missing))

    hr("10. 链式查询抽查")
    print("  aircraft_name(ct 100+125) =", td.aircraft_name(225))
    u = td.unit_def_by_entity_type(101)
    print("  unit_def_by_entity_type(101) =", None if u is None
          else "%s domain=%d role=%s" % (u.name, u.domain, u.main_role))
    v = td.vehicle_def_by_ct_idx(221)
    print("  vehicle_def_by_ct_idx(221) =", None if v is None
          else "%s nctr=%s rcs=%.2f" % (v.name, v.nctr, v.radar_cs))
    w = td.weapon_def(258)
    print("  weapon_def(258) =", None if w is None
          else "%s range=%.1f hit_air=%d guidance=%s"
               % (w.name, w.range, w.hit_air, w.guidance))
    f = td.feature_def(10)
    print("  feature_def(10) =", None if f is None
          else "%s hp=%d rt=%d" % (f.name, f.hit_points, f.repair_time))
    r = td.radar_def(8)
    print("  radar_def(8) =", None if r is None
          else "%s det=%.1f" % (r.name, r.detection_range))
    obj = td.camp_obj(4)
    print("  camp_obj(4) =", None if obj is None else
          "%s ocd=%d pos=(%.0f,%.0f)" % (obj.name, obj.ocd_index,
                                          obj.pos_x, obj.pos_y))
    entry = td.ct_get(225)
    assert entry is not None
    print("  ct 225 -> %s / %s" % (td.objective_type_name(entry),
                                   td.aircraft_name(225)))
    print("  theater_info (Hellas) =", td.theater_info)
    print("  theater_info (Korea)  =",
          TheaterData.load(BMS, "Korea").theater_info)
    print("  missing (Hellas) =", td.missing)

    print("\n全部检查完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
