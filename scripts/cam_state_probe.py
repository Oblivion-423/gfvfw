"""
战役态势组装验证（开发期探针，非产品代码）。

把 :func:`gfvfw.campaign.state.build_state` 的产物与 CamReader 产出的
``campaign_state.json`` 逐项对拍。

用法:
    .venv\\Scripts\\python.exe scripts\\cam_state_probe.py
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.campaign.bundle import Bundle              # noqa: E402
from gfvfw.campaign.cmpfile import read_cmp           # noqa: E402
from gfvfw.campaign.state import build_state          # noqa: E402
from gfvfw.campaign.theater import TheaterData        # noqa: E402

BMS = Path(r"G:\BMS\Falcon BMS 4.38")
CAM = Path(r"G:\BMS\CAMreader\Save-Day  3 02 00 46.cam")
REF = Path(r"G:\BMS\Falcon BMS 4.38\Tools\CamReader-0.1.0\bin\Release\net472\campaign_state.json")

OK = [0]
BAD = [0]


def ck(name: str, got, want, *, tol: float | None = None) -> None:
    if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        good = abs(got - want) <= tol
    else:
        good = got == want
    if good:
        OK[0] += 1
        print("    OK    %-30s %s" % (name, _s(got)))
    else:
        BAD[0] += 1
        print("    DIFF  %-30s 本实现=%s  参考=%s" % (name, _s(got), _s(want)))


def _s(v, n: int = 34) -> str:
    r = repr(v)
    return r if len(r) <= n else r[:n - 1] + "…"


def main() -> int:
    t0 = time.time()
    b = Bundle.load(CAM)
    c = read_cmp(b.get_by_ext(".cmp"), b.version)
    theater_name = c.theater_name
    print("存档 %s  版本 %d  剧场 %r" % (CAM.name, b.version, theater_name))
    print()

    t1 = time.time()
    th = TheaterData.load(BMS, theater_name)
    print("剧场数据加载 %.2fs  %s" % (time.time() - t1, th.summary() if hasattr(th, "summary") else ""))
    print()

    t2 = time.time()
    st = build_state(CAM, th)
    print("态势组装 %.2fs" % (time.time() - t2))
    print()
    print("各内嵌文件解出情况:", st.sections)
    print("单位流计数:", st.unit_counts)
    if st.warnings:
        print()
        print("告警 %d 条:" % len(st.warnings))
        for w in st.warnings[:12]:
            print("    ", w)
    print()

    ref = json.load(open(REF, encoding="utf-8-sig"))
    m = ref["meta"]

    print("=" * 78)
    print("[1] meta")
    print("=" * 78)
    ck("version", st.cam_version, m["version"])
    ck("theater", st.theater, m["theater"])
    ck("scenario", st.scenario, m["scenario"])
    ck("saveName", st.save_name, m["saveName"])
    ck("campaignTimeMs", st.campaign_time_ms, m["campaignTimeMs"])
    ck("campaignTime", st.campaign_time_label, m["campaignTime"])
    ck("currentDay", st.campaign_day, m["currentDay"])
    ck("dayZero", st.day_zero, m["dayZero"])
    ck("activeTeams", st.active_teams, m["activeTeams"])
    ck("situation", st.situation, m["situation"])
    ck("tempo", st.tempo, m["tempo"])
    ck("endgameResult", st.endgame_result, m["endgameResult"])
    ck("bullseye.x", st.bullseye_east, m["bullseye"]["x"])
    ck("bullseye.y", st.bullseye_north, m["bullseye"]["y"])
    ck("te.victoryPts", st.te_victory_pts, m["te"]["victoryPts"])
    ck("te.numTeams", st.te_num_teams, m["te"]["numTeams"])
    ck("forceRatios.ground", st.ground_ratio, m["forceRatios"]["ground"])
    ck("forceRatios.air", st.air_ratio, m["forceRatios"]["air"])

    print()
    print("=" * 78)
    print("[2] teams")
    print("=" * 78)
    ck("team count", len(st.teams), len(ref["teams"]))
    for i, rt in enumerate(ref["teams"][:8]):
        if i >= len(st.teams):
            break
        t = st.teams[i]
        ck("t%d name" % i, t.name, rt["name"])
        ck("t%d active" % i, t.active, rt["active"])
        ck("t%d flag" % i, t.flag, rt["flag"])
        ck("t%d color" % i, t.color, rt["color"])
        ck("t%d equipment" % i, t.equipment, rt["equipment"])
        ck("t%d initiative" % i, t.initiative, rt["initiative"])
        ck("t%d reinforcement" % i, t.reinforcement, rt["reinforcement"])
        ck("t%d playerRating" % i, t.player_rating, rt["playerRating"], tol=1e-4)
        ck("t%d offensiveLoss" % i, t.offensive_loss, rt["offensiveLoss"])
        ck("t%d attackTime" % i, t.attack_time, rt["attackTime"])
        for k, dst in (("air", "exp_air"), ("airDef", "exp_air_def"),
                       ("ground", "exp_ground"), ("naval", "exp_naval")):
            ck("t%d exp.%s" % (i, k), getattr(t, dst), rt["experience"][k])
        for k, dst in (("supply", "supply"), ("fuel", "fuel"),
                       ("replacements", "replacements")):
            ck("t%d res.%s" % (i, k), getattr(t, dst), rt["resources"][k])
        for k, dst in (("aircraft", "st_aircraft"), ("airDef", "st_air_def"),
                       ("ground", "st_ground"), ("ships", "st_ships"),
                       ("bases", "st_bases"), ("supplyLvl", "supply_lvl"),
                       ("fuelLvl", "fuel_lvl")):
            ck("t%d str.%s" % (i, k), getattr(t, dst), rt["strength"][k])
        for k, dst in (("aircraft", "start_aircraft"), ("airDef", "start_air_def"),
                       ("ground", "start_ground"), ("ships", "start_ships"),
                       ("bases", "start_bases")):
            ck("t%d start.%s" % (i, k), getattr(t, dst), rt["startStrength"][k])
        ck("t%d stances" % i, list(t.stances),
           [s["value"] for s in rt["stances"]])

    print()
    print("=" * 78)
    print("[3] 实体计数")
    print("=" * 78)
    ck("squadrons", len(st.squadrons), len(ref["squadrons"]))
    ck("packages", st.count_of("Package"), len(ref["packages"]))
    ck("flights", st.count_of("Flight"), len(ref["flights"]))
    ck("groundUnits", st.count_of("Battalion") + st.count_of("Brigade")
       + st.count_of("Division"), len(ref["groundUnits"]))
    ck("navalUnits", st.count_of("TaskForce"), len(ref["navalUnits"]))
    ck("objectives", len(st.objectives), len(ref["objectives"]))
    ck("events", len(st.events), len(ref["events"]))

    print()
    print("=" * 78)
    print("[4] 目标点：类型分布与占有")
    print("=" * 78)
    mine = collections.Counter(o.type_name for o in st.objectives)
    theirs = collections.Counter(o["typeName"] for o in ref["objectives"])
    allt = sorted(set(mine) | set(theirs), key=lambda k: -(theirs.get(k, 0)))
    same = 0
    for t in allt[:16]:
        a, bb = mine.get(t, 0), theirs.get(t, 0)
        flag = "OK  " if a == bb else "DIFF"
        same += (a == bb)
        print("    %s  %-22s 本实现=%-6d 参考=%-6d" % (flag, t, a, bb))
    print("    —— %d/%d 个类型计数一致" % (same, len(allt[:16])))
    OK[0] += same
    BAD[0] += len(allt[:16]) - same

    mine_own = collections.Counter(o.team_id for o in st.objectives)
    their_own = collections.Counter(o["teamId"] for o in ref["objectives"])
    print()
    ck("占有分布(-1 无主)", mine_own.get(-1, 0), their_own.get(-1, 0))
    for tid in sorted(set(their_own) - {-1}):
        ck("占有分布(team %d)" % tid, mine_own.get(tid, 0), their_own.get(tid, 0))

    print()
    print("=" * 78)
    print("[5] 抽样比对（位置）")
    print("=" * 78)
    my_obj = {o.camp_id: o for o in st.objectives}
    n_ok = 0
    for ro in ref["objectives"][:1] + [x for x in ref["objectives"] if x["teamId"] != -1][:6]:
        mo = my_obj.get(ro["campId"])
        if mo is None:
            print("    DIFF  campId=%d 本实现缺失" % ro["campId"])
            BAD[0] += 1
            continue
        de = abs((mo.east or 0) - ro["pos"]["x"])
        dn = abs((mo.north or 0) - ro["pos"]["y"])
        if de < 0.01 and dn < 0.01:
            n_ok += 1
            print("    OK    campId=%-6d %-24s pos=(%.2f,%.2f) owner=%d/%d src=%s"
                  % (ro["campId"], ro["name"][:24], mo.east, mo.north,
                     mo.team_id, ro["teamId"], mo.source))
        else:
            print("    DIFF  campId=%-6d %-24s 本实现=(%.2f,%.2f) 参考=(%.2f,%.2f)"
                  % (ro["campId"], ro["name"][:24], mo.east, mo.north,
                     ro["pos"]["x"], ro["pos"]["y"]))
        OK[0] += n_ok
        BAD[0] += 0

    my_fl = {}
    for u in st.units:
        if u.unit_kind == "Flight":
            my_fl[u.unit_id] = u
    hit = 0
    for rf in ref["flights"][:8]:
        mf = my_fl.get(rf["id"])
        if mf is None:
            print("    DIFF  flight %d 本实现缺失" % rf["id"])
            continue
        good = (mf.east == rf["pos"]["x"] and mf.north == rf["pos"]["y"]
                and mf.team_id == rf["teamId"])
        hit += good
        print("    %s  flight %-6d 本实现=(%s,%s,%s,%s) 参考=(%s,%s,%s,%s)"
              % ("OK  " if good else "DIFF", rf["id"],
                 mf.east, mf.north, mf.mission_code, mf.team_id,
                 rf["pos"]["x"], rf["pos"]["y"], rf["missionCode"], rf["teamId"]))
    OK[0] += hit
    BAD[0] += max(0, 8 - hit)

    print()
    print("=" * 78)
    print("[6] 事件")
    print("=" * 78)
    for i, re_ in enumerate(ref["events"][:3]):
        if i >= len(st.events):
            break
        me = st.events[i]
        good = (me.team_id == re_["teamId"] and me.east == re_["pos"]["x"]
                and me.north == re_["pos"]["y"] and me.text == re_["text"]
                and me.at_ms == re_["timeMs"])
        ck("event %d" % i, good, True)
        if not good:
            print("         本实现: t=%d team=%d (%d,%d) %r"
                  % (me.at_ms, me.team_id, me.east, me.north, me.text[:50]))
            print("         参考  : t=%d team=%d (%d,%d) %r"
                  % (re_["timeMs"], re_["teamId"], re_["pos"]["x"], re_["pos"]["y"],
                     re_["text"][:50]))

    print()
    print("=" * 78)
    print("断言 %d 项一致，%d 项不符    总耗时 %.2fs"
          % (OK[0], BAD[0], time.time() - t0))
    print("=" * 78)
    return 1 if BAD[0] else 0


if __name__ == "__main__":
    sys.exit(main())
