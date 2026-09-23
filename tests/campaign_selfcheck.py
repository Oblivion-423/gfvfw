"""
战役管理自校验。

覆盖：
  1. 战役 CRUD 与唯一名约束
  2. 任务归入 / 移出战役
  3. 战役汇总口径（任务数/架次/时长/航程/参战人数/战损）
  4. 软删除战役时任务被移出（不丢数据）
  5. 权限（CAMPAIGN_MANAGE）

运行:
    .venv\\Scripts\\python.exe tests\\campaign_selfcheck.py
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
    AircraftType, Campaign, Member, MemberRole, Mission, Role, Sortie, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services import campaigns as CS  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

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


def make_app(tmpdir: Path):
    db_path = tmpdir / "camp.sqlite3"
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


def test_service_layer() -> None:
    print("\n[1] 战役汇总口径（服务层）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig = make_app(tdp)
        try:
            with TestSession() as db:
                aid, auser = make_user(db, "Oblivion", "owner")
                bid, buser = make_user(db, "Rookie", "member")
                f16 = db.scalar(select(AircraftType).where(
                    AircraftType.name == "F-16C Block 52"))

                base = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
                camp = Campaign(name="2026 春季战役", theater="Korea",
                                status="active", started_at=base,
                                visibility="public", sort_order=1)
                db.add(camp)
                db.flush()

                # 战役内 2 个任务
                m1 = Mission(name="春季-01", campaign_id=camp.id, started_at=base,
                             mission_type="cap", acmi_completeness="complete")
                m2 = Mission(name="春季-02", campaign_id=camp.id,
                             started_at=base + timedelta(days=7),
                             mission_type="strike", acmi_completeness="complete")
                # 战役外 1 个任务
                m3 = Mission(name="日常训练", started_at=base + timedelta(days=14),
                             mission_type="training", acmi_completeness="complete")
                db.add_all([m1, m2, m3])
                db.flush()

                # m1: 两人各 1 架次
                db.add(Sortie(mission_id=m1.id, member_id=aid, raw_pilot_name="Oblivion",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base, flight_seconds=3600,
                              distance_meters=185200, takeoff_count=1, landing_count=1))
                db.add(Sortie(mission_id=m1.id, member_id=bid, raw_pilot_name="Rookie",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base, flight_seconds=1800,
                              distance_meters=92600, takeoff_count=1, landing_count=1))
                # m2: Oblivion 战损
                db.add(Sortie(mission_id=m2.id, member_id=aid, raw_pilot_name="Oblivion",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base + timedelta(days=7),
                              flight_seconds=5400, distance_meters=277800,
                              takeoff_count=1, landing_count=0, deaths=1, crashed=True))
                # m3: 战役外
                db.add(Sortie(mission_id=m3.id, member_id=bid, raw_pilot_name="Rookie",
                              aircraft_type_id=f16.id, aircraft_raw_name="F-16CM-52",
                              takeoff_at=base + timedelta(days=14),
                              flight_seconds=1200, distance_meters=37040,
                              takeoff_count=1, landing_count=1))
                db.commit()

            with TestSession() as db:
                rows = CS.list_campaigns(db)
                check("战役列表 1 条", len(rows) == 1, "得到 %d" % len(rows))
                r = rows[0]
                check("任务数 = 2（不含战役外）", r["missions"] == 2,
                      "得到 %d" % r["missions"])
                check("架次 = 3", r["sorties"] == 3, "得到 %d" % r["sorties"])
                check("总时长 = 10800 秒", r["flight_seconds"] == 10800,
                      "得到 %d" % r["flight_seconds"])
                check("参战成员 = 2", r["pilots"] == 2, "得到 %d" % r["pilots"])
                check("战损 = 1", r["deaths"] == 1, "得到 %d" % r["deaths"])
                expect_nm = (185200 + 92600 + 277800) / 1852.0
                check("总航程换算海里正确",
                      abs(r["distance_nm"] - expect_nm) < 0.01,
                      "得到 %.2f 期望 %.2f" % (r["distance_nm"], expect_nm))

                camp = db.scalar(select(Campaign))
                d = CS.campaign_detail(db, camp)
                check("详情任务列表 2 条", len(d["missions"]) == 2)
                check("详情合计架次 3", d["totals"]["sorties"] == 3)
                check("参战成员按时长排序",
                      d["pilots"][0]["callsign"] == "Oblivion",
                      "得到 %s" % [p["callsign"] for p in d["pilots"]])
                check("Oblivion 战役内时长 = 9000",
                      d["pilots"][0]["flight_seconds"] == 9000,
                      "得到 %d" % d["pilots"][0]["flight_seconds"])
                check("机型分布 1 种", len(d["aircraft"]) == 1,
                      "得到 %s" % d["aircraft"])
                check("未归属任务 = 1", len(CS.unassigned_missions(db)) == 1,
                      "得到 %d" % len(CS.unassigned_missions(db)))

            print("\n[2] 页面与权限")
            with TestClient(app) as client:
                r = client.get("/campaigns", follow_redirects=False)
                check("匿名访问战役 → 跳登录", r.status_code == 303)

            with TestClient(app) as client:
                login(client, auser)
                r = client.get("/campaigns")
                check("战役列表可访问", r.status_code == 200)
                check("列出战役名", "2026 春季战役" in r.text)
                check("显示战区", "Korea" in r.text)
                check("显示总时长（小时分）", "小时" in r.text)
                check("显示航程（NM）", "NM" in r.text)
                check("显示未归属任务提示",
                      "尚未归入战役" in r.text or "未归属" in r.text)

                r = client.get("/campaigns/new")
                check("新建表单可访问", r.status_code == 200)
                check("表单含三种状态",
                      all(s in r.text for s in ("筹备中", "进行中", "已结束")))
                token = csrf_of(r.text)

                # 重名拒绝
                r = client.post("/campaigns/new", data={
                    "name": "2026 春季战役", "status": "planning",
                    "visibility": "public", "sort_order": 0, "csrf_token": token,
                })
                check("重名战役被拒", r.status_code == 400, "得到 %d" % r.status_code)
                check("重名给出提示", "同名战役已存在" in r.text)

                # 正常创建
                r = client.post("/campaigns/new", data={
                    "name": "2026 夏季战役", "theater": "Balkans",
                    "status": "planning", "started_at": "2026-07-01",
                    "summary": "夏季攻势", "visibility": "public",
                    "sort_order": 2, "csrf_token": csrf_of(client.get('/campaigns/new').text),
                }, follow_redirects=False)
                check("创建战役成功（303）", r.status_code == 303,
                      "得到 %d" % r.status_code)
                loc = r.headers.get("location", "")
                with TestSession() as db:
                    c2 = db.scalar(select(Campaign).where(
                        Campaign.name == "2026 夏季战役"))
                    check("新战役已入库", c2 is not None)
                    check("战区已保存", c2 and c2.theater == "Balkans")
                    check("开始日期已保存",
                          c2 and c2.started_at and c2.started_at.strftime("%Y-%m-%d") == "2026-07-01",
                          "得到 %s" % (c2.started_at if c2 else None))
                    c2_id = c2.id

                # 编辑
                r = client.get("/campaigns/%s/edit" % c2_id)
                check("编辑表单可访问", r.status_code == 200)
                r = client.post("/campaigns/%s/edit" % c2_id, data={
                    "name": "2026 夏季战役", "theater": "Balkans",
                    "status": "active", "started_at": "2026-07-01",
                    "ended_at": "", "summary": "改过了",
                    "visibility": "members", "sort_order": 2,
                    "csrf_token": csrf_of(client.get('/campaigns/%s/edit' % c2_id).text),
                }, follow_redirects=False)
                check("编辑战役成功", r.status_code == 303)
                with TestSession() as db:
                    c2 = db.get(Campaign, c2_id)
                    check("状态已更新为进行中", c2.status == "active")
                    check("可见性已更新", c2.visibility == "members")
                    check("简介已更新", c2.summary == "改过了")

            print("\n[3] 任务归入 / 移出")
            with TestClient(app) as client:
                login(client, auser)
                with TestSession() as db:
                    camp = db.scalar(select(Campaign).where(
                        Campaign.name == "2026 春季战役"))
                    m3 = db.scalar(select(Mission).where(Mission.name == "日常训练"))
                    camp_id, m3_id = camp.id, m3.id

                # 通过战役页归入
                r = client.get("/campaigns/%s" % camp_id)
                check("战役详情含归入区块", "把任务归入本战役" in r.text)
                check("归入区块列出未归属任务", "日常训练" in r.text)
                token = csrf_of(r.text)
                r = client.post("/campaigns/%s/assign" % camp_id,
                                data={"mission_ids": [m3_id], "csrf_token": token},
                                follow_redirects=False)
                check("归入成功（303）", r.status_code == 303)
                with TestSession() as db:
                    m3 = db.get(Mission, m3_id)
                    check("任务已归属该战役", m3.campaign_id == camp_id)
                    rows = CS.list_campaigns(db)
                    r0 = next(x for x in rows if x["campaign"].id == camp_id)
                    check("汇总已更新（3 任务 / 4 架次）",
                          r0["missions"] == 3 and r0["sorties"] == 4,
                          "得到 %d 任务 %d 架次" % (r0["missions"], r0["sorties"]))
                    check("时长已更新 = 12000",
                          r0["flight_seconds"] == 12000,
                          "得到 %d" % r0["flight_seconds"])

                # 通过任务页移出
                r = client.get("/missions/%s" % m3_id)
                check("任务详情含战役选择器", "所属战役" in r.text)
                token = csrf_of(r.text)
                r = client.post("/missions/%s/campaign" % m3_id,
                                data={"campaign_id": "", "csrf_token": token},
                                follow_redirects=False)
                check("移出战役成功", r.status_code == 303)
                with TestSession() as db:
                    m3 = db.get(Mission, m3_id)
                    check("任务已移出（campaign_id 为空）", m3.campaign_id is None)
                    check("任务本身未被删除", m3.deleted_at is None)

                # 通过战役页移出
                client.post("/campaigns/%s/assign" % camp_id,
                            data={"mission_ids": [m3_id],
                                  "csrf_token": csrf_of(client.get('/campaigns/%s' % camp_id).text)},
                            follow_redirects=False)
                r = client.get("/campaigns/%s" % camp_id)
                token = csrf_of(r.text)
                r = client.post("/campaigns/%s/detach/%s" % (camp_id, m3_id),
                                data={"csrf_token": token}, follow_redirects=False)
                check("从战役页移出成功", r.status_code == 303)
                with TestSession() as db:
                    check("任务已移出", db.get(Mission, m3_id).campaign_id is None)

            print("\n[4] 权限")
            with TestClient(app) as client:
                login(client, buser)
                r = client.get("/campaigns")
                check("普通成员可查看战役列表", r.status_code == 200)
                check("普通成员看不到新建按钮", "新建战役" not in r.text)
                r = client.get("/campaigns/new", follow_redirects=False)
                check("普通成员无新建权限（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    camp = db.scalar(select(Campaign).where(
                        Campaign.name == "2026 春季战役"))
                    camp_id = camp.id
                r = client.get("/campaigns/%s" % camp_id)
                check("普通成员可看战役详情", r.status_code == 200)
                check("普通成员看不到归入区块",
                      "把任务归入本战役" not in r.text)

            print("\n[5] 软删除战役（任务移出，不丢数据）")
            with TestClient(app) as client:
                login(client, auser)
                with TestSession() as db:
                    camp = db.scalar(select(Campaign).where(
                        Campaign.name == "2026 夏季战役"))
                    camp_id = camp.id
                r = client.get("/campaigns/%s/edit" % camp_id)
                token = csrf_of(r.text)
                r = client.post("/campaigns/%s/delete" % camp_id,
                                data={"csrf_token": token}, follow_redirects=False)
                check("删除返回 303", r.status_code == 303, "得到 %d" % r.status_code)
                with TestSession() as db:
                    c = db.get(Campaign, camp_id)
                    check("战役记录仍存在（软删除）", c is not None)
                    check("deleted_at 已设置", c and c.deleted_at is not None)
                r = client.get("/campaigns")
                check("软删除后不在列表", "2026 夏季战役" not in r.text)

            # 有任务的战役被删除 → 任务应被移出而不是级联删除
            with TestClient(app) as client:
                login(client, auser)
                with TestSession() as db:
                    camp = db.scalar(select(Campaign).where(
                        Campaign.name == "2026 春季战役"))
                    camp_id = camp.id
                    mission_ids = [m.id for m in db.scalars(
                        select(Mission).where(Mission.campaign_id == camp_id))]
                    check("删除前该战役有任务", len(mission_ids) >= 1,
                          "得到 %d" % len(mission_ids))
                r = client.post("/campaigns/%s/delete" % camp_id,
                                data={"csrf_token": csrf_of(
                                    client.get('/campaigns/%s/edit' % camp_id).text)},
                                follow_redirects=False)
                check("删除含任务的战役成功", r.status_code == 303)
                with TestSession() as db:
                    for mid in mission_ids:
                        m = db.get(Mission, mid)
                        check("任务 %s 未被删除" % mid[:8],
                              m is not None and m.deleted_at is None)
                        check("任务 %s 已移出战役" % mid[:8],
                              m is not None and m.campaign_id is None)
                    from gfvfw.models import Sortie as S2
                    from sqlalchemy import func
                    n = db.scalar(select(func.count()).select_from(S2))
                    check("架次未受影响（4 条）", n == 4, "得到 %d" % n)
        finally:
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig


def main() -> int:
    print("=" * 70)
    test_service_layer()
    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
