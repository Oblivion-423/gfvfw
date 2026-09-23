"""
``.uni`` 单位流解析验证（开发期探针，非产品代码）。

用最小的类表加载器（只读 Falcon4_CT.xml 的 8 个字段）驱动
:func:`gfvfw.campaign.units.read_units`，与 CamReader 产出的
``campaign_state.json`` 的实体计数对拍。

正式实现里类表由 :mod:`gfvfw.campaign.theater` 提供；本探针是为了在
theater 模块就绪前先验证 units.py 的字节布局是否正确。

用法:
    .venv\\Scripts\\python.exe scripts\\cam_units_probe.py
"""
from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.campaign.bundle import Bundle          # noqa: E402
from gfvfw.campaign.units import read_units       # noqa: E402

CT_XML = Path(r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\TerrData\Objects\Falcon4_CT.xml")
CAM = Path(r"G:\BMS\CAMreader\Save-Day  3 02 00 46.cam")
REF_JSON = Path(r"G:\BMS\Falcon BMS 4.38\Tools\CamReader-0.1.0\bin\Release\net472\campaign_state.json")

FIELDS = ("Domain", "Class", "Type", "SubType", "Specific", "Owner",
          "EntityType", "EntityIdx")


@dataclass
class CtEntry:
    num: int
    domain: int
    cls: int
    type: int
    subtype: int
    specific: int
    owner: int
    entity_type: int
    entity_idx: int


class MiniTheater:
    def __init__(self, entries: list[CtEntry]):
        self.entries = entries

    def ct_get(self, et_id: int):
        i = et_id - 100
        if 0 <= i < len(self.entries):
            return self.entries[i]
        return None


def load_ct(path: Path) -> list[CtEntry]:
    out: list[CtEntry] = []
    cur = None
    nums = []
    for ev, el in ET.iterparse(str(path), events=("start", "end")):
        if ev == "start" and el.tag == "CT":
            cur = {"num": int(el.get("Num") or 0)}
        elif ev == "end":
            if el.tag == "CT" and cur is not None:
                out.append(CtEntry(
                    num=cur["num"],
                    domain=cur.get("domain", 0), cls=cur.get("cls", 0),
                    type=cur.get("type", 0), subtype=cur.get("subtype", 0),
                    specific=cur.get("specific", 0), owner=cur.get("owner", 0),
                    entity_type=cur.get("entity_type", 0),
                    entity_idx=cur.get("entity_idx", -1)))
                nums.append(cur["num"])
                cur = None
                el.clear()
            elif cur is not None and el.tag in FIELDS:
                key = "cls" if el.tag == "Class" else (
                    "type" if el.tag == "Type" else (
                        "subtype" if el.tag == "SubType" else (
                            "specific" if el.tag == "Specific" else (
                                "owner" if el.tag == "Owner" else (
                                    "domain" if el.tag == "Domain" else (
                                        "entity_type" if el.tag == "EntityType"
                                        else "entity_idx"))))))
                try:
                    cur[key] = int((el.text or "0").strip())
                except ValueError:
                    pass
    # Num 是否等于文档序（C# 用 entityTypeId-100 直接索引）
    seq_ok = all(n == i for i, n in enumerate(nums))
    print("CT 条目数=%d，Num 与文档序一致=%s（前 5 个 Num=%s）"
          % (len(out), seq_ok, nums[:5]))
    return out


def main() -> int:
    t0 = time.time()
    entries = load_ct(CT_XML)
    print("类表加载耗时 %.2fs" % (time.time() - t0))
    print()

    b = Bundle.load(CAM)
    print("存档 %s  版本 %d" % (CAM.name, b.version))
    uni_raw = b.get_by_ext(".uni")
    print(".uni 原始 %d 字节" % len(uni_raw))

    t0 = time.time()
    res = read_units(uni_raw, b.version, MiniTheater(entries))
    dt = time.time() - t0
    print("解析耗时 %.2fs" % dt)
    print()
    print("申报记录数=%d  解出=%d  跳过=%d"
          % (res.declared_count, len(res.units), res.skipped))
    print("跳过明细: noEntry=%d noRouter=%d errors=%d"
          % (res.skipped_no_entry, res.skipped_no_router, res.skipped_error))
    print("消费字节 %d / %d （%.1f%%）"
          % (res.consumed, res.total, 100.0 * res.consumed / max(1, res.total)))
    print()
    print("按类型统计:")
    for k, v in sorted(res.kind_counts.items(), key=lambda x: -x[1]):
        print("    %-12s %d" % (k, v))
    if res.errors:
        print()
        print("错误/恢复记录（前 10 条）:")
        for e in res.errors[:10]:
            print("    ", e)

    print()
    print("=== 与 campaign_state.json 对拍 ===")
    # 注意：campaign_state.json 里的 objectives(6944) **不是** .uni 解出来的，
    # 而是剧场目标表（CampObjData.xml / .obj 增量）的产物。.uni 只含"当前存在
    # 的战役单位"：67 编队 + 67 飞行 + 47 中队 + 378 地面 + 10 海军 = 569 条，
    # 正好等于 .uni 申报的记录数。
    ref = json.load(open(REF_JSON, encoding="utf-8-sig"))
    checks = [
        ("flights", res.count_of("Flight"), len(ref.get("flights", []))),
        ("packages", res.count_of("Package"), len(ref.get("packages", []))),
        ("squadrons", res.count_of("Squadron"), len(ref.get("squadrons", []))),
        ("groundUnits", res.count_of("Battalion") + res.count_of("Brigade")
         + res.count_of("Division"), len(ref.get("groundUnits", []))),
        ("navalUnits", res.count_of("TaskForce"), len(ref.get("navalUnits", []))),
        ("合计", len(res.units), res.declared_count),
    ]
    ok = 0
    for name, got, want in checks:
        good = got == want
        ok += good
        print("    %-13s 本实现=%-6d 参考=%-6d %s"
              % (name, got, want, "OK" if good else "<<< 不符"))
    print("    %d/%d 一致" % (ok, len(checks)))

    # 抽一个 Flight 看字段是否合理
    fl = [u for u in res.units if u.unit_kind == "Flight"]
    if fl:
        u = fl[0]
        print()
        print("样例 Flight: id=%d et=%d owner=%d pos=(%d,%d) name_id=%d "
              "missions_id=%s wp=%d/%d loadouts=%s"
              % (u.id.num, u.entity_type_id, u.owner, u.x, u.y, u.name_id,
                 u.extra.get("mission"), u.current_wp, len(u.waypoints),
                 u.extra.get("loadouts")))
    if ok == len(checks):
        print("\n全部计数与参考一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
