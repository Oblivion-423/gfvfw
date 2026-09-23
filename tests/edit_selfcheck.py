"""
自校验：**上线后的人工修正能力**。

覆盖需求里早就写明、但一直没实现的那条路
（`docs/requirements.md` L97 / L282 / L326 / L489）：
成员可编辑自己的架次、指挥层可修正任何记录、删任务要能"撤销归并"、
补录要能兜底（R10）、修正要留痕（R5）。

权限点是 `permissions.py` 里**早就定义好**的：
`LOG_EDIT_OWN` / `LOG_EDIT_ANY` / `LOG_DELETE` / `LOG_APPROVE`
（此前在别处被引用 0 次）。

运行:
    .venv\\Scripts\\python.exe tests\\edit_selfcheck.py
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
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AcmiFile, AuditLog, Member, MemberRole, Mission, Role, Sortie, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


def csrf_of(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def build_app(tmpdir: Path):
    db_path = tmpdir / "edit.sqlite3"
    engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.config as cfgmod
    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]

    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession

    # 上传/删除都要写 storage，必须重定向，否则会污染真实存储目录
    orig_storage = cfgmod.settings.storage_dir
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_user(db, callsign: str, role_code: str):
    """建成员 + 账号 + 角色，返回 ``(member_id, user_id)``。

    ⚠️ 两者不同：``edited_by`` 这类留痕字段是 FK 到 **users**，
    而架次归属是 FK 到 **members**。混用会写出永远失败的断言。
    """
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    user = User(username=callsign.lower(),
                password_hash=hash_password("password123"),
                status="active", member_id=member.id)
    db.add(user)
    role = db.scalar(select(Role).where(Role.code == role_code))
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, user.id


def login(client, username) -> bool:
    page = client.get("/login")
    r = client.post("/login",
                    data={"username": username, "password": "password123",
                          "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    return r.status_code == 303


# 时间基准固定，避免依赖当前时间
BASE = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def main() -> int:
    print("=" * 72)
    print("人工修正能力自校验（任务/架次/ACMI/存档）")
    print("=" * 72)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            with TestSession() as db:
                owner_mid, owner_uid = make_user(db, "Oblivion", "owner")
                cmd_mid, cmd_uid = make_user(db, "Viper", "commander")
                mem_mid, mem_uid = make_user(db, "Rookie", "member")
                ins_mid, ins_uid = make_user(db, "Instructor", "instructor")

                # 任务 + 2 条架次（一条属于 Rookie，一条属于 Oblivion）+ 1 份 ACMI
                m = Mission(name="待修正任务", started_at=BASE,
                            ended_at=BASE + timedelta(hours=1),
                            mission_type="cap", visibility="members",
                            acmi_completeness="complete")
                db.add(m)
                db.flush()
                s_rookie = Sortie(mission_id=m.id, member_id=mem_mid,
                                  raw_pilot_name="Rookie",
                                  aircraft_raw_name="F-16CM-52",
                                  takeoff_at=BASE, landing_at=BASE + timedelta(hours=1),
                                  flight_seconds=3600, distance_meters=185200,
                                  takeoff_count=1, landing_count=1,
                                  data_source="acmi", data_confidence="exact")
                s_other = Sortie(mission_id=m.id, member_id=owner_mid,
                                 raw_pilot_name="Oblivion",
                                 takeoff_at=BASE, landing_at=BASE + timedelta(hours=1),
                                 flight_seconds=3600, distance_meters=185200,
                                 takeoff_count=1, landing_count=1,
                                 data_source="acmi", data_confidence="exact")
                db.add_all([s_rookie, s_other])
                af = AcmiFile(sha256="a" * 64, original_filename="x.zip.acmi",
                              stored_path="acmi/test/x.zip.acmi", size_bytes=1024,
                              parse_status="parsed", mission_id=m.id,
                              batch_id=None, min_relative_seconds=0.0,
                              max_relative_seconds=3600.0, duration_seconds=3600.0)
                db.add(af)
                db.commit()
                m_id, s_rookie_id, s_other_id, af_id = m.id, s_rookie.id, s_other.id, af.id
                # 建一个真实存在的"原件"用于验证磁盘删除
                disk = tdp / "storage" / "acmi" / "test"
                disk.mkdir(parents=True)
                disk_file = disk / "x.zip.acmi"
                disk_file.write_bytes(b"PK\x03\x04fake")

            # ---------------------------------------------------------------
            print("\n[1] 权限边界：谁能改什么")
            with TestClient(app) as client:
                login(client, "rookie")          # member：只能改自己的架次
                r = client.get("/sorties/%s/edit" % s_rookie_id)
                check("成员可打开自己的架次编辑页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                r = client.get("/sorties/%s/edit" % s_other_id)
                check("★ 成员打不开他人的架次", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.get("/missions/%s/edit" % m_id)
                check("★ 成员不能编辑任务", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.get("/missions/%s/delete" % m_id)
                check("★ 成员不能删除任务", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.get("/missions/%s/sorties/new" % m_id)
                check("★ 成员不能补录架次", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "viper")           # commander：全都能改
                for path, label in (
                        ("/missions/%s/edit" % m_id, "编辑任务"),
                        ("/missions/%s/delete" % m_id, "删除任务确认"),
                        ("/missions/%s/sorties/new" % m_id, "补录架次"),
                        ("/sorties/%s/edit" % s_other_id, "编辑他人架次")):
                    r = client.get(path)
                    check("指挥可打开%s" % label, r.status_code == 200,
                          "得到 %d" % r.status_code)

            # ---------------------------------------------------------------
            print("\n[2] 任务编辑")
            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.get("/missions/%s/edit" % m_id)
                check("编辑页回填 UTC+8 时间",
                      'value="2026-05-01T20:00"' in r.text,
                      "未找到 20:00（UTC 12:00 = UTC+8 20:00）")
                r = client.post("/missions/%s/edit" % m_id, data={
                    "csrf_token": csrf_of(r.text),
                    "name": "改过名的任务", "mission_type": "strike",
                    "visibility": "command",
                    "started_at": "2026-05-01T20:00",
                    "ended_at": "2026-05-01T22:30",     # 2.5 小时
                    "base": "Kunsan", "outcome": "success",
                    "brief": "简报", "debrief": "总结",
                    "mission_number": "M-001",
                }, follow_redirects=False)
                check("任务编辑提交成功", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    mm = db.get(Mission, m_id)
                    check("名称已改", mm.name == "改过名的任务", mm.name)
                    check("类型已改", mm.mission_type == "strike")
                    check("可见性已改", mm.visibility == "command")
                    check("编号已改", mm.mission_number == "M-001")
                    check("★ 结束时间按 UTC 存（UTC+8 22:30 → UTC 14:30）",
                          mm.ended_at.strftime("%H:%M") == "14:30",
                          "得到 %s" % mm.ended_at)
                    # ⚠️ 有意如此：任务时长按**各架次的在空区间**算（只算一次），
                    #    改时间窗不会改变它 —— 两条架次都是 1 小时，故仍为 3600。
                    #    时间窗只是元数据/筛选依据。页面已明说这一点。
                    check("★ 任务时长仍按架次区间 = 3600（改时间窗不影响）",
                          mm.duration_seconds == 3600,
                          "得到 %s" % mm.duration_seconds)
                    check("时间窗本身已改到 2.5 小时",
                          int((mm.ended_at - mm.started_at).total_seconds()) == 9000,
                          "得到 %s" % (mm.ended_at - mm.started_at))

                # 结束早于开始必须被拒
                r = client.post("/missions/%s/edit" % m_id, data={
                    "csrf_token": csrf_of(client.get("/missions/%s/edit" % m_id).text),
                    "name": "x", "mission_type": "other", "visibility": "members",
                    "started_at": "2026-05-02T10:00", "ended_at": "2026-05-02T09:00",
                }, follow_redirects=False)
                check("结束早于开始被拒（400）", r.status_code == 400,
                      "得到 %d" % r.status_code)

            # ---------------------------------------------------------------
            print("\n[3] 架次编辑：改归属、改数值、可信度降级、留痕")
            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.get("/sorties/%s/edit" % s_rookie_id)
                check("架次编辑页可打开", r.status_code == 200)
                r = client.post("/sorties/%s/edit" % s_rookie_id, data={
                    "csrf_token": csrf_of(r.text),
                    "member_id": str(cmd_mid),      # 改归属到 Viper
                    "aircraft_raw_name": "F-15E-229",
                    "hours": "0", "minutes": "45", "seconds": "30",
                    "distance_nm": "120.5",
                    "takeoff_count": "2", "landing_count": "1",
                    "weapons_fired": "3", "deaths": "0", "kills": "2",
                    "exceedance_count": "1",
                    "takeoff_at": "", "landing_at": "",
                    "edit_note": "ACMI 归并时归错人了",
                }, follow_redirects=False)
                check("架次编辑提交成功", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    s = db.get(Sortie, s_rookie_id)
                    check("★ 归属已改到 Viper", s.member_id == cmd_mid,
                          "得到 %s" % s.member_id)
                    check("时长按 45分30秒 = 2730 秒", s.flight_seconds == 2730,
                          "得到 %s" % s.flight_seconds)
                    check("★ 航程按海里填、按米存（120.5 NM → 223166 m）",
                          s.distance_meters == int(round(120.5 * 1852)),
                          "得到 %s" % s.distance_meters)
                    check("机型已归一化绑定 F-15E",
                          s.aircraft_type_id is not None)
                    check("起飞次数已改", s.takeoff_count == 2)
                    check("★ 可信度降级为估算（不再是 ACMI 原始结果）",
                          s.data_confidence == "estimated", s.data_confidence)
                    check("★ 修正人已留痕（R5）", s.edited_by == owner_uid,
                          "得到 %s 期望 %s" % (s.edited_by, owner_uid))
                    check("修正说明已存", "归错人" in (s.edit_note or ""))
                    check("产生了 sorties 审计记录",
                          db.scalar(select(func.count()).select_from(AuditLog)
                                    .where(AuditLog.target_table == "sorties",
                                           AuditLog.action == "update")) == 1)

                # 成员改自己的架次应成功；改他人的已被 [1] 拒绝
                login(client, "rookie")
                r = client.get("/sorties/%s/edit" % s_rookie_id)
                check("架次改归属后，原主人已无权编辑", r.status_code == 403,
                      "得到 %d" % r.status_code)

            # ---------------------------------------------------------------
            print("\n[4] 手动补录架次（R10 兜底）")
            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/missions/%s/sorties/new" % m_id)
                check("补录页可打开", r.status_code == 200)
                r = client.post("/missions/%s/sorties/new" % m_id, data={
                    "csrf_token": csrf_of(r.text),
                    "member_id": str(mem_mid),
                    "raw_pilot_name": "",
                    "aircraft_raw_name": "F-16CM-52",
                    "hours": "1", "minutes": "0", "seconds": "0",
                    "distance_nm": "200",
                    "takeoff_count": "1", "landing_count": "1",
                    "weapons_fired": "0", "deaths": "0", "kills": "0",
                    "exceedance_count": "0",
                    "edit_note": "ACMI 文件丢了",
                }, follow_redirects=False)
                check("补录提交成功", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    manual = db.scalar(select(Sortie).where(
                        Sortie.mission_id == m_id, Sortie.data_source == "manual"))
                    check("★ 补录的架次标记为 manual", manual is not None)
                    check("★ 可信度标记为估算",
                          manual is not None and manual.data_confidence == "estimated",
                          manual.data_confidence if manual else None)
                    check("归属到选定成员",
                          manual is not None and manual.member_id == mem_mid)
                    check("时长正确", manual is not None
                          and manual.flight_seconds == 3600)
                    check("补录留痕", manual is not None
                          and manual.edited_by is not None)

                # 既没选成员也没填名字 → 必须被拒
                r = client.post("/missions/%s/sorties/new" % m_id, data={
                    "csrf_token": csrf_of(client.get(
                        "/missions/%s/sorties/new" % m_id).text),
                    "member_id": "", "raw_pilot_name": "",
                    "hours": "1", "minutes": "0", "seconds": "0",
                }, follow_redirects=False)
                check("★ 无成员又无名字被拒（400）", r.status_code == 400,
                      "得到 %d" % r.status_code)

            # ---------------------------------------------------------------
            print("\n[5] 架次软删除")
            with TestSession() as db:
                before = db.scalar(select(func.count()).select_from(Sortie)
                                   .where(Sortie.mission_id == m_id,
                                          Sortie.deleted_at.is_(None)))
            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.post("/sorties/%s/delete" % s_other_id, data={
                    "csrf_token": csrf_of(client.get("/missions/%s" % m_id).text),
                    "reason": "重复记录",
                }, follow_redirects=False)
                check("架次删除提交成功", r.status_code == 303,
                      "得到 %d" % r.status_code)
            with TestSession() as db:
                s = db.get(Sortie, s_other_id)
                check("★ 是软删除（记录仍在库里）", s is not None and s.deleted_at is not None)
                after = db.scalar(select(func.count()).select_from(Sortie)
                                  .where(Sortie.mission_id == m_id,
                                         Sortie.deleted_at.is_(None)))
                check("活动架次数减 1", after == before - 1,
                      "%d → %d" % (before, after))

            # ---------------------------------------------------------------
            print("\n[6] 已归并的 ACMI 不能直接删（必须先撤归并）")
            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.post("/acmi/%s/delete" % af_id, data={
                    "csrf_token": csrf_of(client.get("/log/campaign?acmi=upload").text),
                    "return_to": "/log/campaign",
                }, follow_redirects=False)
                check("★ 已归并的文件删除被拒（400）", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("拒绝理由指明要先删任务",
                      "删除任务" in r.text, r.text[:120])
                with TestSession() as db:
                    check("文件仍在库里", db.get(AcmiFile, af_id) is not None)
                check("磁盘原件未被删", disk_file.exists())

            # ---------------------------------------------------------------
            print("\n[7] 删除任务 = 撤销归并（架次软删 + ACMI 拆回待归并）")
            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.post("/missions/%s/delete" % m_id, data={
                    "csrf_token": csrf_of(client.get("/missions/%s/delete" % m_id).text),
                    "reason": "归并归错了",
                }, follow_redirects=False)
                check("任务删除提交成功", r.status_code == 303,
                      "得到 %d" % r.status_code)
            with TestSession() as db:
                mm = db.get(Mission, m_id)
                check("★ 任务是软删除", mm is not None and mm.deleted_at is not None)
                left = db.scalar(select(func.count()).select_from(Sortie)
                                 .where(Sortie.mission_id == m_id,
                                        Sortie.deleted_at.is_(None)))
                check("★ 其架次一并软删除", left == 0, "还剩 %d" % left)
                af2 = db.get(AcmiFile, af_id)
                check("★ ACMI 已拆回待归并（mission_id 清空）",
                      af2.mission_id is None, "得到 %s" % af2.mission_id)
                check("★ 批次也清掉（否则显示成待确认）", af2.batch_id is None)
                check("任务不在列表中",
                      db.scalar(select(func.count()).select_from(Mission)
                                .where(Mission.deleted_at.is_(None))) == 0)

            # ---------------------------------------------------------------
            print("\n[8] 拆回之后就能删了（行 + 磁盘原件）")
            with TestClient(app) as client:
                login(client, "oblivion")
                # 拆回后应出现在归并候选里
                mp = client.get("/log/campaign?acmi=merge")
                check("拆回的文件重新出现在待归并列表",
                      "x.zip.acmi" in mp.text)
                r = client.post("/acmi/%s/delete" % af_id, data={
                    "csrf_token": csrf_of(
                        client.get("/log/campaign?acmi=upload").text),
                    "return_to": "/log/campaign",
                    "reason": "传错了",
                }, follow_redirects=False)
                check("删除成功（303）", r.status_code == 303,
                      "得到 %d" % r.status_code)
            with TestSession() as db:
                check("★ 文件行已删除（硬删，以便同名文件能重传）",
                      db.get(AcmiFile, af_id) is None)
                check("产生了 acmi_files 删除审计",
                      db.scalar(select(func.count()).select_from(AuditLog)
                                .where(AuditLog.target_table == "acmi_files",
                                       AuditLog.action == "delete")) >= 1)
            check("★ 磁盘原件已删除", not disk_file.exists())

            # ---------------------------------------------------------------
            print("\n[9] 任务的架次列表按权限给出编辑入口")
            with TestSession() as db:
                m2 = Mission(name="权限观察任务", started_at=BASE,
                             ended_at=BASE + timedelta(hours=1),
                             mission_type="other", acmi_completeness="complete")
                db.add(m2)
                db.flush()
                db.add(Sortie(mission_id=m2.id, member_id=mem_mid,
                              raw_pilot_name="Rookie", flight_seconds=60,
                              distance_meters=1000, takeoff_count=1,
                              landing_count=1, data_source="acmi",
                              data_confidence="exact"))
                db.commit()
                m2_id = m2.id

            with TestClient(app) as client:
                login(client, "rookie")
                r = client.get("/missions/%s" % m2_id)
                check("成员在自己的架次行看到编辑链接",
                      "/sorties/" in r.text and ">编辑<" in r.text)
                check("成员看不到补录架次按钮", "补录架次" not in r.text)
                check("成员看不到编辑任务按钮", "编辑任务" not in r.text)

            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/missions/%s" % m2_id)
                check("指挥看到补录架次按钮", "补录架次" in r.text)
                check("指挥看到编辑任务按钮", "编辑任务" in r.text)
                # ⚠️ commander **有** log.delete（见 permissions.py），所以能看到；
                #    没有 log.delete 的是 instructor —— 下一段用 instructor 验证边界。
                check("指挥看到删除任务按钮（commander 有 log.delete）",
                      "删除任务" in r.text)

            with TestClient(app) as client:
                login(client, "instructor")
                r = client.get("/missions/%s" % m2_id)
                check("★ 教官能编辑任务（有 log.edit.any）", "编辑任务" in r.text)
                check("★ 教官看不到删除任务（无 log.delete）", "删除任务" not in r.text)
                r = client.get("/missions/%s/delete" % m2_id)
                check("★ 教官打不开删除确认页（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "oblivion")
                r = client.get("/missions/%s" % m2_id)
                check("owner 看到删除任务按钮", "删除任务" in r.text)
        finally:
            import gfvfw.config as _c
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig
            _c.settings.storage_dir = orig_storage

    print("\n" + "=" * 72)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
