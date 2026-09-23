"""
导航结构自校验。

覆盖：
  1. 一级菜单为：概览 | 成员 | 战役管理 | 飞行记录 | 资料查询
  2. 飞行记录子菜单三项：战役记录 / 训练记录 / 飞行员个人记录
     （需求："删去当前已有的任务记录" —— 子菜单不再含"全部任务"）
  3. 三个子页面均可访问，且按预期口径筛选
  4. 占位页（战役管理、资料查询）明确标注"尚未实现"，不是空白页
  5. 主导航不含冗余的顶级入口（原先的"任务记录/战役/飞行日志/统计"已下移）
  6. "ACMI 导入 / 飞行员认领 / 归并确认"仅从导航隐藏，页面与数据保留

运行:
    .venv\\Scripts\\python.exe tests\\nav_selfcheck.py
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


def nav_block(html: str) -> str:
    """截取导航区内 HTML，用于避免在整页里误判。"""
    m = re.search(r'<nav class="nav">(.*?)</nav>', html, re.S)
    return m.group(1) if m else ""


def make_app(tmpdir: Path):
    db_path = tmpdir / "nav.sqlite3"
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


def main() -> int:
    print("=" * 70)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig = make_app(tdp)
        try:
            with TestSession() as db:
                aid, auser = make_user(db, "Oblivion", "owner")
                f16 = db.scalar(select(AircraftType).where(
                    AircraftType.name == "F-16C Block 52"))
                base = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)

                camp = Campaign(name="测试战役", theater="Korea", status="active",
                                started_at=base, visibility="public")
                db.add(camp)
                db.flush()
                cid = camp.id          # 后面在会话外还要用（工作台宿主页）

                # 战役内任务
                mc = Mission(name="战役任务-01", campaign_id=camp.id, started_at=base,
                             mission_type="cap", acmi_completeness="complete")
                # 战役外训练任务
                mt = Mission(name="日常训练-01", started_at=base + timedelta(days=3),
                             mission_type="training", acmi_completeness="complete")
                # 战役内训练任务（用于验证"包含战役内训练"开关）
                mtc = Mission(name="战役内训练-01", campaign_id=camp.id,
                              started_at=base + timedelta(days=5),
                              mission_type="training", acmi_completeness="complete")
                db.add_all([mc, mt, mtc])
                db.flush()

                for m, secs in ((mc, 3600), (mt, 1800), (mtc, 2700)):
                    db.add(Sortie(mission_id=m.id, member_id=aid,
                                  raw_pilot_name="Oblivion",
                                  aircraft_type_id=f16.id,
                                  aircraft_raw_name="F-16CM-52",
                                  takeoff_at=m.started_at, flight_seconds=secs,
                                  distance_meters=185200, takeoff_count=1,
                                  landing_count=1))
                db.commit()

            print("\n[1] 一级菜单结构")
            with TestClient(app) as client:
                login(client, auser)
                html = client.get("/").text
                nav = nav_block(html)
                check("找到导航区", bool(nav))

                # 一级菜单项（排除子菜单内容）
                top_level = nav.split('<div class="nav-sub">')[0] + \
                    nav.split('</div>', 1)[-1] if 'nav-sub' in nav else nav
                check("一级含「概览」", ">概览<" in nav)
                check("一级含「成员」", ">成员<" in nav)
                check("一级含「战役管理」", ">战役管理<" in nav)
                check("一级含「飞行记录」", "飞行记录" in nav)
                check("一级含「资料查询」", ">资料查询<" in nav)

                print("\n[2] 飞行记录子菜单")
                sub = nav.split('<div class="nav-sub">')[1].split('</div>')[0] \
                    if 'nav-sub' in nav else ""
                check("子菜单存在", bool(sub))
                check("子菜单含「战役记录」", ">战役记录<" in sub)
                check("子菜单含「训练记录」", ">训练记录<" in sub)
                check("子菜单含「飞行员个人记录」", "飞行员个人记录" in sub)
                check("子菜单不含「全部任务」", "全部任务" not in sub,
                      "需求要求删去当前已有的任务记录")

                print("\n[3] 子页面可访问")
                for path in ("/log/campaign", "/log/training", "/log/pilots"):
                    r = client.get(path)
                    check("%s 可访问" % path, r.status_code == 200,
                          "得到 %d" % r.status_code)

                print("\n[4] 三个子页面的口径")
                # 战役记录：只含归入战役的任务
                r = client.get("/log/campaign")
                check("战役记录含战役内任务", "战役任务-01" in r.text)
                check("战役记录含战役内训练", "战役内训练-01" in r.text)
                check("战役记录不含战役外训练", "日常训练-01" not in r.text)
                # 任务维度的时长一律"只算一次"，标签也随之明确
                check("战役记录显示汇总（日志总时长）", "日志总时长" in r.text)
                check("战役记录注明按任务计", "多人飞同一任务只算一次" in r.text)
                check("战役记录用海里", "NM" in r.text)

                # 训练记录：默认只看战役外训练
                r = client.get("/log/training")
                check("训练记录含战役外训练", "日常训练-01" in r.text)
                check("训练记录默认不含战役内训练", "战役内训练-01" not in r.text)
                check("训练记录含开关", "战役内的训练" in r.text)

                r = client.get("/log/training?include_campaign=1")
                check("开启开关后含战役内训练", "战役内训练-01" in r.text)

                r = client.get("/log/training")
                check("训练记录不含非训练任务", "战役任务-01" not in r.text)

                # 飞行员个人记录：未选人时显示总表
                r = client.get("/log/pilots")
                check("个人记录页含总表", "个人成绩总表" in r.text)
                check("总表含 Oblivion", "Oblivion" in r.text)
                check("总表含明细链接", "/log/pilots?member_id=" in r.text)

                r = client.get("/log/pilots?member_id=%s" % aid)
                check("选定成员后显示记录", "的记录" in r.text)
                check("显示该成员三个任务",
                      all(n in r.text for n in ("战役任务-01", "日常训练-01",
                                                "战役内训练-01")))
                check("显示汇总卡片", "总飞行时长" in r.text)

            print("\n[5] 战役管理已实现；资料查询仍为占位页")
            with TestClient(app) as client:
                login(client, auser)
                # 战役管理现在是真功能（解析 BMS .cam），不再是占位页
                r = client.get("/theater")
                check("战役管理页可访问", r.status_code == 200)
                check("战役管理已不再标注未实现", "此功能尚未实现" not in r.text)
                check("战役管理说明解析 .cam 存档", ".cam" in r.text)
                check("战役管理有上报入口或说明",
                      "/theater/upload" in r.text or "上报战役存档" in r.text)

                # 资料查询仍是占位页，且明确标注
                r = client.get("/library")
                check("资料查询页可访问", r.status_code == 200)
                check("资料查询标注尚未实现", "此功能尚未实现" in r.text)
                check("资料查询说明不做回放", "网页轨迹回放" in r.text)
                check("资料查询指路到飞行记录",
                      "/log/campaign" in r.text and "/log/pilots" in r.text)

            print("\n[6] 主页功能导航表")
            with TestClient(app) as client:
                login(client, auser)
                html = client.get("/").text
                check("主页含功能导航表", "功能导航" in html)
                check("导航表标注战役管理待实现", "待实现" in html)
                check("导航表含资料查询行", "/library" in html)
                check("主页指引含飞行记录三项",
                      all(p in html for p in ("/log/campaign", "/log/training",
                                              "/log/pilots")))

            print("\n[7] 权限：普通成员可见只读菜单")
            with TestClient(app) as db_holder:
                pass
            with TestSession() as db:
                bid, buser = make_user(db, "Rookie", "member")
            with TestClient(app) as client:
                login(client, buser)
                nav = nav_block(client.get("/").text)
                check("普通成员可见五个一级菜单",
                      all(k in nav for k in ("概览", "成员", "战役管理",
                                             "飞行记录", "资料查询")))
                # ⚠️ 只在**导航区**内断言，避免把页面正文里的链接误判为菜单项
                check("普通成员子菜单含三项飞行记录",
                      all(k in nav for k in ("战役记录", "训练记录", "飞行员个人记录")))

            print("\n[8] ACMI 已并入业务页面（无独立页面、无独立导航项）")
            # 联队口径：不要单独的 ACMI 上传页。
            # 上传/认领/归并作为「ACMI 工作台」区块内嵌在三个业务页面里。
            with TestClient(app) as client:
                login(client, auser)
                nav = nav_block(client.get("/").text)
                check("导航区无 ACMI 导入", "ACMI 导入" not in nav)
                check("导航区无飞行员认领", "飞行员认领" not in nav)
                check("导航区无归并确认", "归并确认" not in nav)
                check("导航区无 /acmi 链接", "/acmi" not in nav)

                # 独立页面已删除；旧路径保留为 302，旧书签不能失效
                for path in ("/acmi", "/acmi/upload", "/acmi/claim", "/acmi/merge"):
                    r = client.get(path, follow_redirects=False)
                    loc = r.headers.get("location", "")
                    check("旧路径 %s → 302 到宿主页" % path,
                          r.status_code == 302 and loc.startswith("/log/campaign"),
                          "得到 %d %s" % (r.status_code, loc))

                # 工作台应当且仅当出现在这三个宿主页面
                for path in ("/log/campaign", "/log/training", "/theater/%s" % cid):
                    r = client.get(path, follow_redirects=False)
                    check("工作台出现在 %s" % path,
                          r.status_code == 200 and "acmiWorkbench" in r.text,
                          "status=%d" % r.status_code)

                # ⚠️ 必须同时断言 200：否则 404 页面也"不含工作台"，断言会假通过。
                for path in ("/", "/log", "/log/pilots", "/missions", "/stats",
                             "/library", "/theater", "/campaigns", "/members"):
                    r = client.get(path, follow_redirects=False)
                    check("%s 可访问且正文不含工作台" % path,
                          r.status_code == 200 and "acmiWorkbench" not in r.text,
                          "status=%d" % r.status_code)

            print("\n[9] 导航按身份分档（未登录 / 游客 / 队员）")
            # 联队口径：**列表/汇总页对游客开放，详情页与写操作仅队员**。
            # 所以五个业务区块对"已登录"一律显示 —— 菜单里藏起来会让游客
            # 以为系统是空的；限制由详情页的 403 说明页解释。
            with TestClient(app) as anon:
                nav = nav_block(anon.get("/").text)
                check("★ 未登录：导航有「注册」", "/register" in nav, nav[:200])
                check("★ 未登录：导航有「登录」", "/login" in nav)
                check("★ 未登录：导航**没有**业务区块（成员/飞行记录/统计/资料）",
                      not any(k in nav for k in ("/members", "/log/campaign",
                                                 "/stats", "/library")))
                # ⚠️ 未登录访客以前指向 /apply，而 /apply 现在需要登录 ——
                #    点进去只会被弹回登录页。必须指向 /register。
                check("★ 未登录：导航不指向 /apply（那个页面需要先登录）",
                      'href="/apply"' not in nav)

            with TestSession() as db:
                db.add(User(username="guestnav",
                            password_hash=hash_password("password123"),
                            status="pending", member_id=None))
                db.commit()
            with TestClient(app) as client:
                login(client, "guestnav")
                home = client.get("/").text
                nav = nav_block(home)
                check("★ 游客：导航有五个业务区块（列表页对他开放）",
                      all(k in nav for k in ("/members", "/theater",
                                             "/log/campaign", "/stats",
                                             "/library")), nav[:300])
                check("★ 游客：导航有「入队申请」", 'href="/apply"' in nav)
                check("★ 游客：导航有「我的申请」", "/apply/status" in nav)
                check("★ 游客：导航**没有**「入队审批」", "/applications" not in nav)
                check("★ 游客：身份标签显示「游客」", ">游客<" in home)
                check("★ 游客：导航**没有** /register（他已注册）",
                      "/register" not in nav)

            print("\n[10] 列表页对游客可见，但写操作 UI 必须消失")
            # ⚠️ 这是本档身份最容易漏的一条：/log/campaign 与 /log/training
            #    既是对游客开放的列表页，又内嵌了 ACMI 写操作工作台。
            #    模板若忘了按身份挡住工作台，页面能打开，但游客会看到
            #    "上传 ACMI" 的表单 —— 点了必然 403，属可见的误导。
            with TestClient(app) as client:
                login(client, "guestnav")
                for path in ("/log/campaign", "/log/training"):
                    r = client.get(path, follow_redirects=False)
                    check("游客可打开列表页 %s" % path, r.status_code == 200,
                          "status=%d" % r.status_code)
                    check("★ 但 %s 里没有 ACMI 工作台" % path,
                          "acmiWorkbench" not in r.text)
                    check("★ 但 %s 里没有上传表单" % path,
                          "/acmi/upload" not in r.text)
                # 详情页仍然是仅队员
                r = client.get("/theater/%s" % cid, follow_redirects=False)
                check("★ 游客打开战役详情 → 403（详情仅队员）",
                      r.status_code == 403, "status=%d" % r.status_code)
        finally:
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig

    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
