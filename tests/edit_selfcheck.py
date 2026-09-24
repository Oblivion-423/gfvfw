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
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AcmiFile, AuditLog, Campaign, Member, MemberRole, Mission, Role, Sortie,
    User,
)
from gfvfw.models.campaign_state import CampaignSave  # noqa: E402
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

            # ---------------------------------------------------------------
            print("\n[10] ★ 删除战役（软删除 + 恢复）")
            # ---------------------------------------------------------------
            # ⚠️ 这个 handler 早就在（campaigns.py），但**界面上只有编辑页底部**
            #    有一个按钮，详情页和列表页都没有 —— 所以"删除战役"实际上找不到。
            #    本轮把它放到详情页，并补上恢复路径（否则"可恢复"是一句空话）。
            with TestSession() as db:
                camp = Campaign(name="要被作废的战役", theater="Hellas",
                                status="active", visibility="public")
                db.add(camp)
                db.flush()
                m_in = Mission(name="属于该战役的任务", started_at=BASE,
                               ended_at=BASE + timedelta(hours=1),
                               mission_type="cap", campaign_id=camp.id,
                               acmi_completeness="complete")
                db.add(m_in)
                # 附一份战役存档：这样提示里应当出现"存档仍保留"，
                # 顺带覆盖"作废时把受影响的数量如实报出来"这段逻辑。
                db.add(CampaignSave(campaign_id=camp.id, sha256="c" * 64,
                                    original_filename="x.cam",
                                    stored_path="x.cam", size_bytes=1,
                                    theater="Hellas", parse_status="parsed"))
                db.commit()
                camp_id, m_in_id = camp.id, m_in.id

            with TestClient(app) as client:
                login(client, "rookie")           # 普通队员
                r = client.get("/campaigns/%s" % camp_id)
                check("普通队员看得到战役详情", r.status_code == 200)
                check("★ 普通队员看不到「作废此战役」按钮",
                      "作废此战役" not in r.text)

            with TestClient(app) as client:
                login(client, "instructor")       # 教官**有** campaign.manage
                r = client.get("/campaigns/%s" % camp_id)
                check("★ 教官能看到「作废此战役」按钮（有 campaign.manage）",
                      "作废此战役" in r.text)
                check("★ 按钮在**详情页**（这是本轮补的入口）",
                      '/campaigns/%s/delete' % camp_id in r.text)

            with TestClient(app) as client:
                login(client, "viper")            # commander
                r = client.get("/campaigns/%s" % camp_id)
                check("指挥也能看到作废按钮", "作废此战役" in r.text)
                tok = csrf_of(r.text)
                r = client.post("/campaigns/%s/delete" % camp_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 作废战役 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                # ⚠️ 跳转里的 message 是 **URL 编码**过的中文，断言前必须解码 ——
                #    否则"提示里含某某"这类断言永远失败，而提示其实是对的。
                _loc = unquote(r.headers.get("location", ""))
                check("提示写明任务被移出与存档保留",
                      "移出" in _loc and "存档" in _loc, _loc[:160])

            with TestSession() as db:
                c2 = db.get(Campaign, camp_id)
                check("★ 是软删除（战役行还在库里）", c2 is not None
                      and c2.deleted_at is not None)
                m2r = db.get(Mission, m_in_id)
                check("★ 任务没被删，只是移出战役（撤销时不会丢数据）",
                      m2r is not None and m2r.campaign_id is None
                      and m2r.deleted_at is None)
                check("审计记录了 campaigns 删除",
                      db.scalar(select(func.count()).select_from(AuditLog)
                                .where(AuditLog.target_table == "campaigns",
                                       AuditLog.action == "delete")) >= 1)

            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/campaigns", follow_redirects=False)
                check("★ 已作废的战役不再出现在列表里",
                      "要被作废的战役" not in r.text)
                r = client.get("/campaigns?deleted=1")
                check("★ 有「显示已作废」视图（恢复入口）",
                      r.status_code == 200 and "要被作废的战役" in r.text)
                check("★ 已作废视图给出「恢复」按钮",
                      "/campaigns/%s/restore" % camp_id in r.text)
                r = client.get("/campaigns/%s" % camp_id, follow_redirects=False)
                check("已作废战役的详情页打不开（404）", r.status_code == 404,
                      "得到 %d" % r.status_code)

                tok = csrf_of(client.get("/campaigns?deleted=1").text)
                r = client.post("/campaigns/%s/restore" % camp_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 恢复战役 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)

            with TestSession() as db:
                c3 = db.get(Campaign, camp_id)
                check("★ 恢复后 deleted_at 清空", c3 is not None
                      and c3.deleted_at is None)
                m3 = db.get(Mission, m_in_id)
                check("★ 恢复**不会**把任务自动归回（避免猜测性挪数据）",
                      m3 is not None and m3.campaign_id is None)

            # ---------------------------------------------------------------
            print("\n[11] ★ 删除名册成员：必须同时撤掉访问权")
            # ---------------------------------------------------------------
            # 这条是**真实存在过的安全洞**：member_delete 只把 members 行标作废，
            # 而 deps.load_principal 不看 deleted_at ⟹ 那个账号照样加载到该成员、
            # 照样取到未撤销的角色 ⟹ **照样拥有全部权限**，导航里还显示已删的呼号。
            with TestSession() as db:
                doomed = Member(callsign="Doomed", status="active")
                db.add(doomed)
                db.flush()
                db.add(User(username="doomed",
                            password_hash=hash_password("password123"),
                            status="active", member_id=doomed.id))
                role = db.scalar(select(Role).where(Role.code == "commander"))
                db.add(MemberRole(member_id=doomed.id, role_id=role.id))
                db.commit()
                doomed_mid, doomed_uid = doomed.id, db.scalar(
                    select(User.id).where(User.username == "doomed"))

            with TestClient(app) as client:
                check("被作废前：doomed 能登录", login(client, "doomed"))
                check("被作废前：doomed 有指挥权限（能进入队审批）",
                      client.get("/applications").status_code == 200)
                check("被作废前：doomed 能改别人（名册编辑入口在）",
                      "编辑" in client.get("/members/%s" % mem_mid).text)

                r = client.get("/members/%s" % doomed_mid)
                check("★ 成员详情页显示绑定的登录账号",
                      "登录账号" in r.text and "doomed" in r.text)
                check("★ 详情页给出「解绑该账号」按钮",
                      "/members/%s/unbind" % doomed_mid in r.text)
                check("★ 详情页给出「作废此成员」按钮",
                      "/members/%s/delete" % doomed_mid in r.text)

            with TestClient(app) as client:
                login(client, "rookie")           # 普通队员：无 member.delete
                r = client.get("/members/%s" % doomed_mid)
                check("★ 普通队员看不到作废/解绑按钮（无 member.delete）",
                      "作废此成员" not in r.text and "解绑该账号" not in r.text)
                r = client.post("/members/%s/delete" % doomed_mid,
                                data={"csrf_token": csrf_of(r.text)},
                                follow_redirects=False)
                check("★ 普通队员直接 POST 删除 → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/members/%s/unbind" % doomed_mid,
                                data={"csrf_token": csrf_of(r.text)},
                                follow_redirects=False)
                check("★ 普通队员直接 POST 解绑 → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "viper")            # commander
                tok = csrf_of(client.get("/members/%s" % doomed_mid).text)
                r = client.post("/members/%s/delete" % doomed_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("指挥作废成员 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("提示说明账号被一并停用",
                      "停用" in unquote(r.headers.get("location", "")),
                      unquote(r.headers.get("location", ""))[:160])

            with TestSession() as db:
                dm = db.get(Member, doomed_mid)
                du = db.get(User, doomed_uid)
                check("★ 是软删除（成员行还在库里）", dm is not None
                      and dm.deleted_at is not None)
                check("★ 绑定的登录账号被一并停用（不再能登录）",
                      du is not None and du.status == "suspended",
                      "得到 %r" % (du.status if du else None))
                check("审计同时记了成员与账号",
                      db.scalar(select(func.count()).select_from(AuditLog)
                                .where(AuditLog.target_table == "members",
                                       AuditLog.action == "delete",
                                       AuditLog.target_id == doomed_mid)) >= 1)

            with TestClient(app) as client:
                r = client.post("/login",
                                data={"username": "doomed",
                                      "password": "password123",
                                      "csrf_token": csrf_of(client.get("/login").text)},
                                follow_redirects=False)
                check("★ 被作废后该账号无法登录", r.status_code != 303,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/members")
                check("★ 已作废的成员不在名册里", "Doomed" not in r.text)
                r = client.get("/members?deleted=1")
                check("★ 名册有「显示已作废」视图", "Doomed" in r.text)
                check("★ 给出「恢复」按钮",
                      "/members/%s/restore" % doomed_mid in r.text)
                tok = csrf_of(r.text)
                r = client.post("/members/%s/restore" % doomed_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("恢复成员 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)

            with TestSession() as db:
                dm2 = db.get(Member, doomed_mid)
                du2 = db.get(User, doomed_uid)
                check("★ 恢复后成员回到名册", dm2 is not None
                      and dm2.deleted_at is None)
                check("★ 恢复后其账号也恢复为队员（否则名册里有个登录不了的鬼）",
                      du2 is not None and du2.status == "active")

            # ---------------------------------------------------------------
            print("\n[11b] ★ 绕过路由直接软删成员时，账号也不得再拥有权限")
            # ---------------------------------------------------------------
            # 为什么单独测这一条：上面的 [11] 走的是**路由**，而路由自己会把账号
            # 停用（suspended）—— 于是 `load_principal` 里那道"软删成员当成没有成员"
            # 的过滤根本轮不到被验证。但只要有人用 SQL / CLI / 将来的新代码路径
            # 仅把 members.deleted_at 标上，那个账号就会是 active 且仍绑着一个
            # 已作废的成员 —— 那正是权限泄漏的入口。这里就模拟那种情况。
            with TestSession() as db:
                ghost = Member(callsign="GhostMember", status="active")
                db.add(ghost)
                db.flush()
                db.add(User(username="ghostacct",
                            password_hash=hash_password("password123"),
                            status="active", member_id=ghost.id))
                role = db.scalar(select(Role).where(Role.code == "commander"))
                db.add(MemberRole(member_id=ghost.id, role_id=role.id))
                db.commit()
                ghost_mid, ghost_uid = ghost.id, db.scalar(
                    select(User.id).where(User.username == "ghostacct"))

            with TestClient(app) as client:
                check("（前置）ghostacct 有指挥权限",
                      login(client, "ghostacct")
                      and client.get("/applications").status_code == 200)

            with TestSession() as db:
                # 只标作废，**不动账号** —— 模拟绕过路由的删除
                db.get(Member, ghost_mid).deleted_at = datetime(
                    2026, 6, 1, tzinfo=timezone.utc)
                db.commit()
                check("（前置）账号仍是 active 且仍绑着那个成员",
                      db.get(User, ghost_uid).status == "active"
                      and db.get(User, ghost_uid).member_id == ghost_mid)

            with TestClient(app) as client:
                check("★ 仅软删成员后：账号拿不到任何权限点（审批页 403）",
                      login(client, "ghostacct")
                      and client.get("/applications",
                                     follow_redirects=False).status_code == 403,
                      "得到 %d" % client.get(
                          "/applications", follow_redirects=False).status_code)
                r = client.get("/", follow_redirects=False)
                check("★ 导航里不再显示那个已作废的呼号",
                      "GhostMember" not in r.text, r.text[:200])

            # ---------------------------------------------------------------
            print("\n[12] ★ 账号与成员解绑")
            # ---------------------------------------------------------------
            with TestClient(app) as client:
                # 先确认解绑前：绑着的账号有指挥权限
                check("解绑前 doomed 能登录", login(client, "doomed"))
                check("解绑前 doomed 有指挥权限",
                      client.get("/applications").status_code == 200)

            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/members/%s" % doomed_mid)
                tok = csrf_of(r.text)
                r = client.post("/members/%s/unbind" % doomed_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 解绑 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)

            with TestSession() as db:
                du3 = db.get(User, doomed_uid)
                dm3 = db.get(Member, doomed_mid)
                check("★ 账号已与成员断开（member_id 为空）",
                      du3 is not None and du3.member_id is None)
                check("★ 解绑后账号降为游客（pending），不再是队员",
                      du3 is not None and du3.status == "pending",
                      "得到 %r" % (du3.status if du3 else None))
                check("★ 名册成员本身保留（只拆链接，不删人）",
                      dm3 is not None and dm3.deleted_at is None)
                check("★ 角色仍挂在成员上（拆开重绑后权限会自动回来）",
                      db.scalar(select(func.count()).select_from(MemberRole)
                                .where(MemberRole.member_id == doomed_mid,
                                       MemberRole.revoked_at.is_(None))) >= 1)
                check("审计记录了 member.unbind",
                      db.scalar(select(func.count()).select_from(AuditLog)
                                .where(AuditLog.action == "member.unbind",
                                       AuditLog.target_id == doomed_uid)) >= 1)

            with TestClient(app) as client:
                check("★ 解绑后账号仍能登录（是游客，不是被停用）",
                      login(client, "doomed"))
                check("★ 解绑后不再是队员：进不了队内详情页（403）",
                      client.get("/members/%s" % mem_mid,
                                 follow_redirects=False).status_code == 403,
                      "得到 %d" % client.get(
                          "/members/%s" % mem_mid,
                          follow_redirects=False).status_code)
                check("★ 解绑后拿不到任何权限点（入队审批 403）",
                      client.get("/applications",
                                 follow_redirects=False).status_code == 403)
                check("★ 但公开的列表页仍然能看（游客档位）",
                      client.get("/members", follow_redirects=False).status_code
                      == 200)
                r = client.post("/members/%s/delete" % doomed_mid,
                                data={"csrf_token": csrf_of(r.text)},
                                follow_redirects=False)
                check("★ 游客直接 POST 删除成员 → 403",
                      r.status_code == 403, "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/members/%s" % doomed_mid)
                check("★ 解绑后详情页提示「未绑定」",
                      "未绑定" in r.text)
                tok = csrf_of(r.text)
                r = client.post("/members/%s/unbind" % doomed_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 重复解绑 → 400（没有账号可解）",
                      r.status_code == 400, "得到 %d" % r.status_code)

            # 自我删除防护：别把最后一个管理员自己删掉
            with TestClient(app) as client:
                login(client, "viper")
                r = client.post("/members/%s/delete" % cmd_mid,
                                data={"csrf_token": csrf_of(
                                    client.get("/members/%s" % cmd_mid).text)},
                                follow_redirects=False)
                check("★ 不能作废自己所在的成员记录（400）",
                      r.status_code == 400, "得到 %d" % r.status_code)
                check("提示让用另一个管理员账号",
                      "另一个管理员" in r.text, r.text[:200])

            # 自锁防护：也不能把自己（当前登录账号）解绑掉
            with TestClient(app) as client:
                login(client, "viper")
                r = client.post("/members/%s/unbind" % cmd_mid,
                                data={"csrf_token": csrf_of(
                                    client.get("/members/%s" % cmd_mid).text)},
                                follow_redirects=False)
                check("★ 不能给自己解绑（400，否则立刻自锁）",
                      r.status_code == 400, "得到 %d" % r.status_code)
                check("提示让用另一个管理员账号或先授 owner",
                      "另一个管理员" in r.text or "owner" in r.text,
                      r.text[:200])

            # ---------------------------------------------------------------
            print("\n[13] ★ 别把最后一个能登录的 owner 删掉 / 解绑")
            # ---------------------------------------------------------------
            # 这不是权限判定，是防止**不可逆的运维事故**：界面上没有 owner 之后
            # 再没有人能授权（连"提升别人"都做不到），只能上服务器用 CLI 救。
            with TestSession() as db:
                n_owners = db.scalar(
                    select(func.count(func.distinct(MemberRole.member_id)))
                    .select_from(MemberRole)
                    .join(Member, Member.id == MemberRole.member_id)
                    .join(User, User.member_id == Member.id)
                    .join(Role, Role.id == MemberRole.role_id)
                    .where(Role.code == "owner",
                           MemberRole.revoked_at.is_(None),
                           Member.deleted_at.is_(None),
                           User.status == "active"))
                check("（前置）当前只有 1 个能登录的 owner", n_owners == 1,
                      "得到 %s" % n_owners)

            with TestClient(app) as client:
                login(client, "viper")             # commander，有 member.delete
                tok = csrf_of(client.get("/members/%s" % owner_mid).text)
                r = client.post("/members/%s/delete" % owner_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 作废最后一个 owner → 400（否则界面再也授权不了）",
                      r.status_code == 400, "得到 %d" % r.status_code)
                check("提示让人先给别的账号授 owner",
                      "owner" in r.text, r.text[:200])

                r = client.post("/members/%s/unbind" % owner_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 解绑最后一个 owner 的账号 → 400",
                      r.status_code == 400, "得到 %d" % r.status_code)

            # 正面控制：另有一个能登录的 owner 之后，就不该再被拦
            # （guard 不能变成"owner 一律不许动"）
            with TestSession() as db:
                owner_role = db.scalar(select(Role).where(Role.code == "owner"))
                db.add(MemberRole(member_id=ins_mid, role_id=owner_role.id))
                db.commit()

            with TestClient(app) as client:
                login(client, "viper")
                tok = csrf_of(client.get("/members/%s" % owner_mid).text)
                r = client.post("/members/%s/unbind" % owner_mid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 另有 owner 时解绑不再被拦（303）",
                      r.status_code == 303, "得到 %d" % r.status_code)
                with TestSession() as db:
                    ob_u = db.get(User, owner_uid)
                    check("★ 确实解绑了（账号降为游客）",
                          ob_u is not None and ob_u.member_id is None
                          and ob_u.status == "pending")
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
