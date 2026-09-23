"""
统计与查询自校验。

覆盖：
  1. 单位换算（时长 → 小时分钟；航程 → 海里）
  2. 统计口径（未认领不进排行榜、只统计已认领）
  3. 多条件组合筛选
  4. 权限（log.view 门槛）

运行:
    .venv\\Scripts\\python.exe tests\\stats_selfcheck.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AircraftType, Member, MemberRole, Mission, Role, Sortie, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services import stats as S  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402
from gfvfw.web.templating import (  # noqa: E402
    METERS_PER_NM, filter_distance, filter_duration,
)

FAILURES: list[str] = []
CHECKS = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def csrf_of(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def test_units() -> None:
    print("\n[1] 单位换算")
    # 时长：小时/分
    check("3661 秒 → 1小时1分", filter_duration(3661) == "1小时1分",
          "得到 %s" % filter_duration(3661))
    check("3600 秒 → 1小时0分", filter_duration(3600) == "1小时0分",
          "得到 %s" % filter_duration(3600))
    check("2700 秒 → 45分", filter_duration(2700) == "45分",
          "得到 %s" % filter_duration(2700))
    check("45 秒 → 45秒", filter_duration(45) == "45秒",
          "得到 %s" % filter_duration(45))
    check("0 → 占位符", filter_duration(0) == "—")
    check("None → 占位符", filter_duration(None) == "—")
    check("119 秒 → 1分59秒（不丢秒）", filter_duration(119) == "1分59秒",
          "得到 %s" % filter_duration(119))
    check("239 秒 → 3分59秒（不丢秒）", filter_duration(239) == "3分59秒",
          "得到 %s" % filter_duration(239))
    check("3661 秒 → 1小时1分（有小时则省略秒）",
          filter_duration(3661) == "1小时1分",
          "得到 %s" % filter_duration(3661))

    # 航程：海里
    check("1 海里常量 = 1852 m", METERS_PER_NM == 1852.0)
    check("1852 m → 1.00 NM", filter_distance(1852) == "1.00 NM",
          "得到 %s" % filter_distance(1852))
    check("18520 m → 10.0 NM", filter_distance(18520) == "10.0 NM",
          "得到 %s" % filter_distance(18520))
    check("185200 m → 100 NM", filter_distance(185200) == "100 NM",
          "得到 %s" % filter_distance(185200))
    check("不再出现 km", "km" not in filter_distance(500000))
    check("不再出现 m 单位", not filter_distance(500000).endswith(" m"))


def test_union_seconds() -> None:
    """任务时长口径：多人飞同一任务**只算一次**（区间并集）。"""
    print("\n[1b] 任务时长并集（同一任务只算一次）")
    from datetime import datetime, timedelta, timezone

    from gfvfw.services.stats import union_seconds

    t = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    h = lambda n: t + timedelta(hours=n)          # noqa: E731

    check("空输入 → 0", union_seconds([]) == 0)
    check("单个区间 = 自身", union_seconds([(h(0), h(1))]) == 3600)
    # ★ 核心：4 人同飞同一小时，只算 1 小时（人次相加会是 4 小时）
    same = [(h(0), h(1))] * 4
    check("4 个完全重叠区间 → 只算 1 小时", union_seconds(same) == 3600,
          "得到 %d" % union_seconds(same))
    check("部分重叠 → 合并", union_seconds([(h(0), h(1)), (h(0.5), h(2))]) == 7200,
          "得到 %d" % union_seconds([(h(0), h(1)), (h(0.5), h(2))]))
    check("首尾相接 → 合并", union_seconds([(h(0), h(1)), (h(1), h(2))]) == 7200)
    check("中间有空档 → 不相加空档",
          union_seconds([(h(0), h(1)), (h(3), h(4))]) == 7200,
          "得到 %d" % union_seconds([(h(0), h(1)), (h(3), h(4))]))
    check("乱序输入结果一致",
          union_seconds([(h(3), h(4)), (h(0), h(1))]) == 7200)
    check("被完全包含的区间不重复计",
          union_seconds([(h(0), h(3)), (h(1), h(2))]) == 10800)
    check("起止相等（零长度）忽略", union_seconds([(h(0), h(0))]) == 0)
    check("结束早于开始（脏数据）忽略",
          union_seconds([(h(2), h(1))]) == 0)
    check("None 端点忽略", union_seconds([(None, h(1)), (h(0), None)]) == 0)


def make_app(tmpdir: Path):
    db_path = tmpdir / "stats.sqlite3"
    engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]
    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = TestSession
    depsmod.SessionLocal = TestSession
    appmod.SessionLocal = TestSession

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig


def make_user(db, callsign: str, role_code: str):
    username = callsign.lower()
    m = Member(callsign=callsign, status="active")
    db.add(m)
    db.flush()
    db.add(User(username=username, password_hash=hash_password("password123"),
                status="active", member_id=m.id))
    role = db.scalar(select(Role).where(Role.code == role_code))
    db.add(MemberRole(member_id=m.id, role_id=role.id))
    db.commit()
    return m.id, username


def login(client: TestClient, username: str) -> bool:
    page = client.get("/login")
    r = client.post("/login", data={"username": username, "password": "password123",
                                    "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    return r.status_code == 303


def test_stats_and_query() -> None:
    print("\n[2] 统计口径与查询")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig = make_app(tdp)
        try:
            with TestSession() as db:
                aid, auser = make_user(db, "Oblivion", "owner")
                bid, buser = make_user(db, "Rookie", "member")
                f16 = db.scalar(select(AircraftType).where(
                    AircraftType.name == "F-16C Block 52"))

                base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)

                # 任务 1：训练，两人各 1 架次
                m1 = Mission(name="训练任务", started_at=base, ended_at=base + timedelta(hours=2),
                             mission_type="training", acmi_completeness="complete")
                db.add(m1)
                db.flush()
                db.add(Sortie(mission_id=m1.id, member_id=aid, raw_pilot_name="Oblivion",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base, flight_seconds=5400, distance_meters=185200,
                              takeoff_count=1, landing_count=1, weapons_fired=2,
                              data_confidence="exact"))
                db.add(Sortie(mission_id=m1.id, member_id=bid, raw_pilot_name="Rookie",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base, flight_seconds=3600, distance_meters=92600,
                              takeoff_count=1, landing_count=0, data_confidence="exact"))

                # 任务 2：对地，Oblivion 再飞一次（另一机型）
                m2 = Mission(name="打击任务", started_at=base + timedelta(days=32),
                             mission_type="strike", acmi_completeness="partial")
                db.add(m2)
                db.flush()
                f15 = db.scalar(select(AircraftType).where(AircraftType.name == "F-15E"))
                db.add(Sortie(mission_id=m2.id, member_id=aid, raw_pilot_name="Oblivion",
                              aircraft_type_id=f15.id, aircraft_raw_name="F-15E-229",
                              takeoff_at=base + timedelta(days=32),
                              flight_seconds=7200, distance_meters=277800,
                              takeoff_count=1, landing_count=1, deaths=1, crashed=True,
                              data_confidence="exact"))

                # 未认领架次（Ghost）
                db.add(Sortie(mission_id=m2.id, member_id=None, raw_pilot_name="Ghost",
                              aircraft_raw_name="MiG-21MF",
                              takeoff_at=base + timedelta(days=32),
                              flight_seconds=1800, distance_meters=33336,
                              takeoff_count=1, data_confidence="partial"))
                db.commit()

            # ---- 服务层口径 ----
            with TestSession() as db:
                ov = S.overview(db)
                check("总架次 = 4", ov["sorties"] == 4, "得到 %d" % ov["sorties"])
                # ★ 任务维度：每个任务只算一次。
                #   本用例两份任务的架次都没记 landing_at，故按「起飞 + 在空时长」
                #   估终点后取并集：
                #     m1：两人同一时刻起飞（5400 / 3600）→ 并集 = 5400
                #     m2：两人同一时刻起飞（7200 / 1800）→ 并集 = 7200
                #   合计 12600 —— 而不是人次相加的 18000。
                check("任务总时长 = 任务口径（每任务只算一次）",
                      ov["flight_seconds"] == 12600,
                      "得到 %d" % ov["flight_seconds"])
                # ★ 飞行员维度：人次之和，必然 >= 任务口径
                check("飞行员累计时长 = 人次之和",
                      ov["pilot_flight_seconds"] == 18000,
                      "得到 %d" % ov["pilot_flight_seconds"])
                check("人次口径 >= 任务口径",
                      ov["pilot_flight_seconds"] >= ov["flight_seconds"])
                check("未认领架次数 = 1", ov["unclaimed_sorties"] == 1,
                      "得到 %d" % ov["unclaimed_sorties"])
                total_m = 185200 + 92600 + 277800 + 33336
                check("总航程换算海里正确",
                      abs(ov["distance_nm"] - total_m / 1852.0) < 0.01,
                      "得到 %.2f" % ov["distance_nm"])

                lb = S.pilot_leaderboard(db)
                names = {p["callsign"]: p for p in lb}
                check("排行榜只含已认领成员（2 人）", len(lb) == 2,
                      "得到 %s" % list(names))
                check("Ghost 不在排行榜", "Ghost" not in names)
                check("Oblivion 时长 = 12600 秒",
                      names["Oblivion"]["flight_seconds"] == 12600,
                      "得到 %d" % names["Oblivion"]["flight_seconds"])
                check("Oblivion 架次 = 2", names["Oblivion"]["sorties"] == 2)
                check("Oblivion 平均时长 = 6300 秒",
                      names["Oblivion"]["avg_flight_seconds"] == 6300,
                      "得到 %d" % names["Oblivion"]["avg_flight_seconds"])
                check("Oblivion 排第一", lb[0]["callsign"] == "Oblivion")
                check("Rookie 战损 = 0", names["Rookie"]["deaths"] == 0)
                check("Oblivion 战损 = 1", names["Oblivion"]["deaths"] == 1)

                ac = S.by_aircraft(db)
                check("机型分布 3 种", len(ac) == 3, "得到 %d" % len(ac))
                check("未归一化机型被标记",
                      any(not a["known"] for a in ac),
                      "得到 %s" % [(a["raw"], a["known"]) for a in ac])

                mt = S.by_mission_type(db)
                check("任务类型分 2 类", len(mt) == 2, "得到 %d" % len(mt))

                months = S.monthly_trend(db)
                check("月度趋势分 2 个月", len(months) == 2, "得到 %s" % months)
                check("月度趋势按时间倒序",
                      months[0]["month"] > months[1]["month"],
                      "得到 %s" % [m["month"] for m in months])

                q = S.data_quality(db)
                check("可信度分布正确",
                      q["confidence"].get("exact") == 3
                      and q["confidence"].get("partial") == 1,
                      "得到 %s" % q["confidence"])
                check("未判定降落 = 2（含 Ghost 那条）", q["not_landed"] == 2,
                      "得到 %d" % q["not_landed"])
                check("坠毁 = 1", q["crashed"] == 1)
                check("未认领明细只有 Ghost",
                      len(q["unclaimed"]) == 1 and q["unclaimed"][0]["raw_name"] == "Ghost")

            # ---- 筛选 ----
            with TestSession() as db:
                f = S.SortieFilter(member_id=aid)
                check("按成员筛选 = 2 条", S.count_sorties(db, f) == 2)

                f = S.SortieFilter(mission_type="training")
                check("按任务类型筛选 = 2 条", S.count_sorties(db, f) == 2)

                f = S.SortieFilter(aircraft_raw="F-15")
                check("按 ACMI 机型片段筛选 = 1 条", S.count_sorties(db, f) == 1)

                f = S.SortieFilter(include_unclaimed=False)
                check("排除未认领 = 3 条", S.count_sorties(db, f) == 3)

                f = S.SortieFilter(callsign="Ghost")
                check("按未认领名字搜索能命中 = 1 条",
                      S.count_sorties(db, f) == 1,
                      "得到 %d" % S.count_sorties(db, f))

                f = S.SortieFilter(callsign="Obl")
                check("按呼号片段搜索 = 2 条", S.count_sorties(db, f) == 2,
                      "得到 %d" % S.count_sorties(db, f))

                # 组合
                f = S.SortieFilter(callsign="Obl", mission_type="strike")
                check("组合筛选（呼号+类型）= 1 条", S.count_sorties(db, f) == 1,
                      "得到 %d" % S.count_sorties(db, f))

                f = S.SortieFilter(member_id=aid)
                t = S.filter_totals(db, f)
                check("筛选合计时长 = 12600", t["flight_seconds"] == 12600,
                      "得到 %d" % t["flight_seconds"])
                check("筛选合计航程含海里值",
                      abs(t["distance_nm"] - (185200 + 277800) / 1852.0) < 0.01)

                f = S.SortieFilter(confidence="partial")
                check("按可信度筛选 = 1 条", S.count_sorties(db, f) == 1)

            # ---- HTTP 页面 ----
            print("\n[3] 页面渲染与权限")
            with TestClient(app) as client:
                r = client.get("/stats", follow_redirects=False)
                check("匿名访问统计 → 跳登录", r.status_code == 303)
                r = client.get("/log", follow_redirects=False)
                check("匿名访问日志 → 跳登录", r.status_code == 303)

            with TestClient(app) as client:
                login(client, auser)
                r = client.get("/stats")
                check("统计页可访问", r.status_code == 200, "得到 %d" % r.status_code)
                check("显示总架次 4", "总架次" in r.text)
                check("排行榜含 Oblivion", "Oblivion" in r.text)
                # ⚠️ Ghost 会合法地出现在「未认领的架次」区块，
                #    因此不能断言整页不含 Ghost —— 只断言它不在排行榜表格里。
                lb_html = r.text.split("飞行员排行")[1].split("机型分布")[0] \
                    if "飞行员排行" in r.text else ""
                check("排行榜表格内不含 Ghost", "Ghost" not in lb_html,
                      "排行榜段含 Ghost")
                check("显示未认领提醒", "尚未认领" in r.text or "未认领" in r.text)
                check("时长用小时分（含「小时」）", "小时" in r.text)
                check("航程用海里（含 NM）", "NM" in r.text)
                check("机型分布含待归一化标记", "待归一化" in r.text)

                # 概览页（/）也必须同时给出两个时长口径。
                # 只给一个会误导：只见「任务总时长」会低估联队飞行量，
                # 只见「飞行员累计」会让单个任务看起来比实际长 N 倍。
                r = client.get("/")
                check("概览页可访问", r.status_code == 200, "得到 %d" % r.status_code)
                check("概览页显示「日志总时长」", "日志总时长" in r.text)
                check("★ 概览页说明「记录时长」是第三个量", "记录时长" in r.text)
                check("概览页显示「飞行员累计时长」", "飞行员累计时长" in r.text)
                check("概览页说明「只算一次」", "只算一次" in r.text)
                check("概览页显示总航程", "总航程" in r.text)
                # 概览页不得再声称「ACMI 入口已移除」—— 工作台已内嵌到三个宿主页。
                check("概览页文案未过期（不再说入口已移除）",
                      "已临时从导航移除" not in r.text,
                      "概览页仍在声称 ACMI 入口被移除")
                check("概览页指向内嵌工作台的宿主页",
                      "/log/campaign" in r.text and "/log/training" in r.text)

                r = client.get("/log")
                check("日志页可访问", r.status_code == 200)
                check("日志页显示合计", "合计" in r.text)
                check("日志页含筛选表单", "筛选条件" in r.text)
                check("日志页显示未认领徽标", "未认领" in r.text)

                r = client.get("/log?member_id=%s" % aid)
                check("按成员筛选页可访问", r.status_code == 200)
                check("筛选结果含匹配数", "匹配" in r.text)

                r = client.get("/log?callsign=Obl&mission_type=strike")
                check("组合筛选页可访问", r.status_code == 200)

                r = client.get("/log?include_unclaimed=0")
                check("排除未认领页可访问", r.status_code == 200)
                check("排除后不显示 Ghost", "Ghost" not in r.text)

            # 普通成员也能看日志与统计（log.view 门槛低）
            with TestClient(app) as client:
                login(client, buser)
                check("普通成员可看统计", client.get("/stats").status_code == 200)
                check("普通成员可看日志", client.get("/log").status_code == 200)
        finally:
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig


def main() -> int:
    print("=" * 70)
    test_units()
    test_union_seconds()
    test_stats_and_query()
    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
