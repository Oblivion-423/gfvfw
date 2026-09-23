"""
Web 层自校验：ACMI 上传 / 飞行员认领 / 归并确认 / 任务详情。

这是本项目最关键的一条链路 ——
`上传 → 解析 → 认领 → 归并 → 生成架次 → 出现在统计与任务页`。

运行:
    .venv\\Scripts\\python.exe tests\\acmi_web_selfcheck.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AcmiActor, AcmiFile, AuditLog, Campaign, IgnoredPilot, Member, MemberRole,
    Mission, PilotMapping, Role, Sortie, SortieEvent, User,
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


# --------------------------------------------------------------------------
# 合成 ACMI（物理自洽：u/v 与经纬度描述同一条航线）
# --------------------------------------------------------------------------

def synthetic_acmi(pilots, aircraft="F-16CM-52", duration_s=1800) -> bytes:
    lines = [
        "FileType=text/acmi/tacview\n",
        "FileVersion=2.1\n",
        "0,DataRecorder=Falcon BMS 4.38.1\n",
        "0,ReferenceTime=2024-8-16T00:00:00Z\n",
    ]
    for i, p in enumerate(pilots):
        lines.append(
            "%x,T=126.50|36.10|7.0|0|0|0|400000|250000|90,"
            "CallSign=Viper6%d,Coalition=ROK,CAS=0,Name=%s,Pilot=%s,"
            "Type=Air+FixedWing\n" % (9 + i, i, aircraft, p))
    # 一个无名 AI 僚机（无 Pilot=）
    lines.append(
        "f,T=126.70|36.30|7.0|0|0|0|410000|251000|90,"
        "Coalition=ROK,CAS=0,Name=F-16C-52 ROKAF,Type=Air+FixedWing\n")

    # 每 300 秒一个采样点，向北推进 0.01°（约 1112 m）同时 v 前进 1112
    steps = max(1, duration_s // 300)
    for k in range(1, steps + 1):
        t = k * 300.0
        lines.append("#%.1f\n" % t)
        for i in range(len(pilots)):
            lat = 36.10 + 0.01 * k
            v = 250000 + 1112 * k
            cas = 0 if k == steps else 300
            mach = 0 if k == steps else 0.8
            lines.append(
                "%x,T=126.50|%.4f|6000|||0|400000|%d|,CAS=%s,Mach=%s\n"
                % (9 + i, lat, v, cas, mach))
        # 中途投放武器一次
        if k == steps // 2:
            lines.append(
                "%x,T=126.50|%.4f|6000|||0|400000|%d|,Event=Shot|AIM-120C\n"
                % (9, 36.10 + 0.01 * k, 250000 + 1112 * k))
    return "".join(lines).encode("utf-8")


def write_zip_acmi(dirpath: Path, name: str, body: bytes) -> Path:
    p = dirpath / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("acmi.txt", body)
    p.write_bytes(buf.getvalue())
    return p


def build_app(tmpdir: Path):
    db_path = tmpdir / "test.sqlite3"
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

    # ⚠️ 生产 ``var/storage`` 必须一并重定向。
    #    上传路由是逐请求构造 ``AcmiIngestService()``，它读的是
    #    ``settings.storage_dir``，所以只换 DB 不够——测试的 ACMI 会真的
    #    落到产品存储目录里，留下无主残留文件（曾经积了 26 个）。
    import gfvfw.config as cfgmod
    orig_storage = cfgmod.settings.storage_dir
    test_storage = tmpdir / "storage"
    test_storage.mkdir(parents=True, exist_ok=True)
    cfgmod.settings.storage_dir = test_storage

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_user(db, callsign: str, role_code: str, username: str | None = None):
    username = username or callsign.lower()
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    db.add(User(username=username, password_hash=hash_password("password123"),
                status="active", member_id=member.id))
    role = db.scalar(select(Role).where(Role.code == role_code))
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, username


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
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            with TestSession() as db:
                admin_mid, admin_user = make_user(db, "Oblivion", "owner")
                member_mid, member_user = make_user(db, "Rookie", "member")

            # 准备 ACMI 源文件
            src_dir = tdp / "src"
            src_dir.mkdir()
            f1 = write_zip_acmi(src_dir, "2026-06-01_10-00-00.zip.acmi",
                                synthetic_acmi(["Oblivion", "Rookie"]))
            f2 = write_zip_acmi(src_dir, "2026-06-02_10-00-00.zip.acmi",
                                synthetic_acmi(["Ghost"]))
            # 第三份独立文件：用于测"归并即归入战役"，不能复用 f2（会判重）
            f3 = write_zip_acmi(src_dir, "2026-06-03_10-00-00.zip.acmi",
                                synthetic_acmi(["Oblivion"]))

            print("\n[1] 入口与权限（ACMI 已内嵌进业务页面，无独立页面）")
            with TestClient(app) as client:
                # 旧的独立页面路径必须仍在，且 302 到宿主页面（旧书签不能失效）
                for legacy, expect in (("/acmi", "/log/campaign"),
                                       ("/acmi/upload", "/log/campaign"),
                                       ("/acmi/claim", "/log/campaign"),
                                       ("/acmi/merge", "/log/campaign")):
                    r = client.get(legacy, follow_redirects=False)
                    loc = r.headers.get("location", "")
                    check("%s 302 到宿主页" % legacy,
                          r.status_code == 302 and loc.startswith(expect),
                          "得到 %d %s" % (r.status_code, loc))

                r = client.get("/log/campaign", follow_redirects=False)
                check("匿名访问宿主页 → 跳登录",
                      r.status_code == 303 and "/login" in r.headers.get("location", ""),
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, member_user)
                r = client.get("/log/campaign")
                check("普通成员可访问战役记录页", r.status_code == 200)
                check("页面内嵌 ACMI 工作台", "acmiWorkbench" in r.text)
                check("成员能看到上传表单", 'action="/acmi/upload"' in r.text)
                check("成员看不到归并表单", 'action="/acmi/merge"' not in r.text)
                r = client.get("/log/campaign?acmi=claim")
                check("无认领权限时工作台给出说明",
                      "没有认领飞行员名的权限" in r.text)
                r = client.get("/log/training")
                check("训练记录页也内嵌工作台",
                      r.status_code == 200 and "acmiWorkbench" in r.text)

                # ⚠️ 真正的边界在 POST 上：无权限者直接构造请求也必须被挡住。
                #    把权限检查写成 require(principal, X) 这种"调用而非依赖"是
                #    这个项目曾经犯过的真实漏洞，所以这里专门测 POST。
                r = client.post("/acmi/merge",
                                data={"csrf_token": "x", "file_ids": []},
                                follow_redirects=False)
                check("成员伪造归并 POST → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/acmi/claim",
                                data={"csrf_token": "x", "raw_name": "Nope",
                                      "member_id": member_mid},
                                follow_redirects=False)
                check("成员伪造认领 POST → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/acmi/upload",
                                data={"csrf_token": "x"}, follow_redirects=False)
                check("成员伪造上传 POST（无 CSRF）→ 403", r.status_code == 403,
                      "得到 %d" % r.status_code)

            print("\n[2] 上传与解析（管理员，走战役记录页内的工作台）")
            with TestClient(app) as client:
                login(client, admin_user)

                r = client.get("/log/campaign?acmi=upload")
                token = csrf_of(r.text)
                check("上传阶段含历史文件警示", "不导入历史文件" in r.text)
                check("上传阶段含流程步骤",
                      "上传文件" in r.text and "归并确认" in r.text)

                with open(f1, "rb") as fh:
                    r = client.post("/acmi/upload",
                                    files={"files": (f1.name, fh, "application/zip")},
                                    data={"csrf_token": token,
                                          "return_to": "/log/campaign",
                                          "campaign_id": ""},
                                    follow_redirects=False)
                check("上传后 303 跳到认领阶段",
                      r.status_code == 303 and "acmi=claim" in r.headers.get("location", ""),
                      "得到 %d %s" % (r.status_code, r.headers.get("location")))
                check("回执标注已解析",
                      "did=uploaded" in r.headers.get("location", ""),
                      r.headers.get("location", ""))

                with TestSession() as db:
                    rec = db.scalar(select(AcmiFile))
                    check("acmi_files 已入库", rec is not None)
                    check("parse_status=parsed", rec.parse_status == "parsed",
                          "得到 %s / %s" % (rec.parse_status, rec.parse_error))
                    check("保存了解析快照", bool(rec.sortie_summaries_json))
                    check("识别 2 个带名对象",
                          rec.objects_with_pilot_name == 2,
                          "得到 %s" % rec.objects_with_pilot_name)
                    check("识别 1 个无名 AI", rec.unnamed_ai_actors == 1,
                          "得到 %s" % rec.unnamed_ai_actors)
                    actors = list(db.scalars(select(AcmiActor)))
                    check("AI 不建 actor 行", len(actors) == 2, "得到 %d" % len(actors))
                    f1_id = rec.id

                cp = client.get(r.headers["location"])
                check("认领阶段列出新上传的飞行员名",
                      "Oblivion" in cp.text and "Rookie" in cp.text)
                check("认领阶段提示未认领", "未认领" in cp.text)
                up = client.get("/log/campaign?acmi=upload")
                check("上传阶段的最近归档显示解析状态", "已解析" in up.text)

                # 去重：重复上传不得新增记录，且必须给出明确回执
                with open(f1, "rb") as fh:
                    r = client.post("/acmi/upload",
                                    files={"files": (f1.name, fh, "application/zip")},
                                    data={"csrf_token": csrf_of(up.text),
                                          "return_to": "/log/campaign",
                                          "campaign_id": ""},
                                    follow_redirects=False)
                loc = r.headers.get("location", "")
                check("重复上传给出 duplicate 回执", "did=duplicate" in loc,
                      "得到 %s" % loc)
                with TestSession() as db:
                    n = db.scalar(select(func.count()).select_from(AcmiFile))
                    check("未产生重复记录", n == 1, "得到 %d" % n)
                dup = client.get(loc)
                check("重复上传的说明文字可见",
                      "此前已经上传过" in dup.text)

            print("\n[3] 飞行员认领（工作台认领阶段）")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/log/campaign?acmi=claim")
                check("认领阶段可访问", r.status_code == 200)
                check("列出待认领名字",
                      "Oblivion" in r.text and "Rookie" in r.text)
                token = csrf_of(r.text)

                r = client.post("/acmi/claim",
                                data={"raw_name": "Ghost", "member_id": member_mid,
                                      "csrf_token": token, "return_to": "/log/campaign"},
                                follow_redirects=False)
                check("认领操作成功", r.status_code == 303, "得到 %d" % r.status_code)
                check("认领后回到认领阶段并带回执",
                      "acmi=claim" in r.headers.get("location", "")
                      and "did=claimed" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    pm = db.scalar(select(PilotMapping).where(
                        PilotMapping.raw_name == "Ghost"))
                    check("映射已建立", pm is not None and pm.member_id == member_mid)
                    bound = db.scalar(select(func.count()).select_from(AcmiActor)
                                      .where(AcmiActor.member_id.isnot(None)))
                    check("actor 已回填绑定", bound == 0, "得到 %d（此名字尚未有文件）" % bound)

                # 忽略
                r = client.post("/acmi/ignore",
                                data={"raw_name": "Oblivion",
                                      "csrf_token": csrf_of(client.get('/log/campaign?acmi=claim').text),
                                      "return_to": "/log/campaign"},
                                follow_redirects=False)
                check("忽略操作成功", r.status_code == 303)
                with TestSession() as db:
                    ig = db.scalar(select(IgnoredPilot).where(
                        IgnoredPilot.raw_name == "Oblivion"))
                    check("忽略名单已记录", ig is not None)
                r = client.get("/log/campaign?acmi=claim")
                check("忽略后不再出现在待认领列表",
                      "已忽略" in r.text)

                # 恢复
                with TestSession() as db:
                    ig = db.scalar(select(IgnoredPilot))
                    ig_id = ig.id
                r = client.post("/acmi/claim/%s/unignore" % ig_id,
                                data={"csrf_token": csrf_of(client.get('/log/campaign?acmi=claim').text),
                                      "return_to": "/log/campaign"},
                                follow_redirects=False)
                check("恢复待认领成功", r.status_code == 303)
                r = client.get("/log/campaign?acmi=claim")
                check("恢复后重新出现在待认领列表", "Oblivion" in r.text)

                # 正式认领两位飞行员
                token = csrf_of(r.text)
                client.post("/acmi/claim",
                            data={"raw_name": "Oblivion", "member_id": admin_mid,
                                  "csrf_token": token}, follow_redirects=False)
                client.post("/acmi/claim",
                            data={"raw_name": "Rookie", "member_id": member_mid,
                                  "csrf_token": csrf_of(client.get('/log/campaign?acmi=claim').text)},
                            follow_redirects=False)
                with TestSession() as db:
                    bound = db.scalar(select(func.count()).select_from(AcmiActor)
                                      .where(AcmiActor.member_id.isnot(None)))
                    check("两个 actor 均已绑定", bound == 2, "得到 %d" % bound)

            print("\n[4] 归并确认（工作台归并阶段）")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/log/campaign?acmi=merge")
                check("归并阶段可访问", r.status_code == 200)
                check("列出待归并文件", f1.name in r.text)
                check("已认领名字标绿（badge ok）", 'badge ok">Oblivion' in r.text)
                check("归并阶段含确认按钮", "确认归并并创建任务" in r.text)
                check("战役记录页的战役被锁定（无第二个下拉）",
                      'id="acmiCampaign"' not in r.text)
                check("训练记录页把类型锁成 training",
                      'name="mission_type" value="training"'
                      in client.get("/log/training?acmi=merge").text)

                token = csrf_of(r.text)
                r = client.post("/acmi/merge",
                                data={"file_ids": [f1_id], "mission_name": "测试任务",
                                      "mission_type": "training",
                                      "visibility": "members", "csrf_token": token},
                                follow_redirects=False)
                check("归并确认成功（303）", r.status_code == 303,
                      "得到 %d %s" % (r.status_code, r.text[:200]))
                loc = r.headers.get("location", "")
                check("跳转到任务详情", "/missions/" in loc, "得到 %s" % loc)

            print("\n[5] 生成结果核验")
            with TestSession() as db:
                mission = db.scalar(select(Mission))
                check("任务已创建", mission is not None)
                check("任务名称正确", mission and mission.name == "测试任务",
                      "得到 %s" % (mission.name if mission else None))
                check("任务类型正确", mission and mission.mission_type == "training")
                check("汇总已重算", mission and mission.duration_seconds is not None)

                # ★ 任务时长口径：**多人飞同一任务只算一次**。
                #   本用例是 2 人同飞一份 ACMI，两人区间完全相同；
                #   任务的时长必须等于"并集"（≈单人时长），而不是两人相加。
                _ms = list(db.scalars(select(Sortie)))
                _person_sum = sum(int(s.flight_seconds or 0) for s in _ms)
                from gfvfw.services.stats import mission_flight_seconds
                _once = mission_flight_seconds(db, [mission.id])[mission.id]
                check("日志时长 == 读取时算得的一次值",
                      mission.duration_seconds == _once,
                      "存值 %s 算得 %s" % (mission.duration_seconds, _once))
                check("日志时长远小于人次之和（只算一次）",
                      mission.duration_seconds < _person_sum,
                      "任务 %s 人次和 %s" % (mission.duration_seconds, _person_sum))
                check("日志时长 ≈ 单人时长（两人同飞不翻倍）",
                      _person_sum and abs(mission.duration_seconds
                                          - _person_sum / len(_ms)) < 120,
                      "任务 %s 单人均值 %s"
                      % (mission.duration_seconds, _person_sum // max(1, len(_ms))))

                sorties = list(db.scalars(select(Sortie)))
                check("生成 2 条架次（AI 不计）", len(sorties) == 2,
                      "得到 %d" % len(sorties))
                for s in sorties:
                    check("架次[%s] 时长 > 0" % s.raw_pilot_name,
                          s.flight_seconds > 0, "得到 %d" % s.flight_seconds)
                    check("架次[%s] 航程 > 0" % s.raw_pilot_name,
                          s.distance_meters > 0, "得到 %d" % s.distance_meters)
                    check("架次[%s] 起飞=1" % s.raw_pilot_name,
                          s.takeoff_count == 1)
                    check("架次[%s] 已降落" % s.raw_pilot_name,
                          s.landing_count == 1, "得到 %d" % s.landing_count)
                    check("架次[%s] 机型已归一化" % s.raw_pilot_name,
                          s.aircraft_type_id is not None)
                    check("架次[%s] 可信度=完整" % s.raw_pilot_name,
                          s.data_confidence == "exact",
                          "得到 %s" % s.data_confidence)

                ev = list(db.scalars(select(SortieEvent)))
                check("生成了事件流水", len(ev) >= 1, "得到 %d" % len(ev))
                check("含武器投放事件",
                      any(e.event_type == "weapon_release" for e in ev))

                check("产生了审计记录",
                      (db.scalar(select(func.count()).select_from(AuditLog)) or 0) > 0)

            print("\n[6] 任务页面")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/missions")
                check("任务列表可访问", r.status_code == 200)
                check("列出测试任务", "测试任务" in r.text)

                r = client.get(loc)
                check("任务详情可访问", r.status_code == 200)
                check("详情显示参战架次", "参战架次" in r.text)
                # 任务维度与飞行员维度是两个口径，标签必须都在
                check("详情显示日志时长（只算一次）", "日志时长" in r.text)
                check("★ 详情同时显示记录时长（录制窗）", "记录时长" in r.text)
                check("★ 详情说明三个时长不要互相校验", "不要互相校验" in r.text)
                check("详情显示飞行员累计（人次口径）", "飞行员累计" in r.text)
                check("详情列出飞行员", "Oblivion" in r.text)
                check("详情含归档文件区", "归档文件" in r.text)
                check("详情说明不做回放", "本地 Tacview" in r.text)
                check("详情含事件流水", "武器投放" in r.text)

            print("\n[7] 统计进入个人档案")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/members/%s" % admin_mid)
                check("成员档案显示架次", "架次" in r.text)
                check("档案不再显示空状态", "暂无架次记录" not in r.text)
                check("档案含机型", "F-16C" in r.text)

            print("\n[8] 无认领者时不生成架次")
            with TestClient(app) as client:
                login(client, admin_user)
                token = csrf_of(client.get("/log/campaign?acmi=upload").text)
                with open(f2, "rb") as fh:
                    client.post("/acmi/upload",
                                files={"files": (f2.name, fh, "application/zip")},
                                data={"csrf_token": token, "return_to": "/log/campaign"})
                with TestSession() as db:
                    # Ghost 已认领（member_mid），故应能生成架次
                    rec2 = db.scalar(select(AcmiFile).where(
                        AcmiFile.original_filename == f2.name))
                    check("第二个文件已解析", rec2 is not None
                          and rec2.parse_status == "parsed")
                    rec2_id = rec2.id
                token = csrf_of(client.get("/log/campaign?acmi=merge").text)
                r = client.post("/acmi/merge",
                                data={"file_ids": [rec2_id], "mission_type": "other",
                                      "visibility": "members", "csrf_token": token},
                                follow_redirects=False)
                check("第二任务创建成功", r.status_code == 303)
                with TestSession() as db:
                    total = db.scalar(select(func.count()).select_from(Sortie))
                    check("总架次变为 3", total == 3, "得到 %d" % total)
                    m2 = db.scalar(select(Mission).order_by(
                        Mission.created_at.desc()))
                    s2 = db.scalar(select(Sortie).where(Sortie.mission_id == m2.id))
                    check("Ghost 的架次绑定到 Rookie 成员",
                          s2 is not None and s2.member_id == member_mid,
                          "得到 %s" % (s2.member_id if s2 else None))

            print("\n[9] 归并即归入战役（三处入口的归属语义）")
            with TestSession() as db:
                camp = Campaign(name="自检战役", status="active")
                db.add(camp)
                db.commit()
                cid = camp.id

            with TestClient(app) as client:
                login(client, admin_user)
                up = client.get("/log/campaign?acmi=upload")
                with open(f3, "rb") as fh:
                    r = client.post("/acmi/upload",
                                    files={"files": (f3.name, fh, "application/zip")},
                                    data={"csrf_token": csrf_of(up.text),
                                          "return_to": "/log/campaign",
                                          "campaign_id": cid},
                                    follow_redirects=False)
                check("带战役上下文的上传被接受", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    rec3 = db.scalar(select(AcmiFile).where(
                        AcmiFile.original_filename == f3.name))
                    rec3_id = rec3.id if rec3 else None
                check("第三个文件已入库待归并", rec3_id is not None)

                mp = client.get("/log/campaign?acmi=merge&campaign_id=%s" % cid)
                check("工作台记住选定战役",
                      'name="campaign_id" value="%s"' % cid in mp.text)

                r = client.post("/acmi/merge",
                                data={"file_ids": [rec3_id],
                                      "mission_name": "归入战役的任务",
                                      "mission_type": "other",
                                      "visibility": "members",
                                      "campaign_id": cid,
                                      "csrf_token": csrf_of(mp.text)},
                                follow_redirects=False)
                check("带战役的归并被接受", r.status_code == 303,
                      "得到 %d %s" % (r.status_code, r.text[:160]))
                check("回执说明已归入战役",
                      "任务已创建并归入战役" in unquote(r.headers.get("location", "")),
                      r.headers.get("location", ""))

                with TestSession() as db:
                    m3 = db.scalar(select(Mission).where(
                        Mission.name == "归入战役的任务"))
                    check("任务已创建", m3 is not None)
                    check("★ 任务自动归入该战役", m3 and m3.campaign_id == cid,
                          "campaign_id=%s 期望 %s" % (m3.campaign_id if m3 else None, cid))
                    check("审计记录了战役归属",
                          any(a.after_json and cid in a.after_json
                              for a in db.scalars(select(AuditLog))))

                # 战役详情页里也应看到它
                r = client.get("/theater/%s" % cid)
                check("战役详情页可访问", r.status_code == 200)
                check("战役详情页内嵌 ACMI 工作台", "acmiWorkbench" in r.text)

                # 不存在的战役必须被拒，而不是写进库
                r = client.post("/acmi/merge",
                                data={"file_ids": [rec3_id], "campaign_id": "no-such",
                                      "csrf_token": csrf_of(mp.text)},
                                follow_redirects=False)
                check("归入不存在的战役 → 400", r.status_code == 400,
                      "得到 %d" % r.status_code)

                # return_to 白名单：外部地址不得被用作跳转目标（开放重定向）
                r = client.post("/acmi/claim",
                                data={"raw_name": "Ghost", "member_id": member_mid,
                                      "csrf_token": csrf_of(client.get(
                                          "/log/campaign?acmi=claim").text),
                                      "return_to": "//evil.example.com/x"},
                                follow_redirects=False)
                check("return_to 拒绝协议相对外链",
                      r.headers.get("location", "").startswith("/log/campaign"),
                      "得到 %s" % r.headers.get("location"))
                r = client.post("/acmi/claim",
                                data={"raw_name": "Ghost", "member_id": member_mid,
                                      "csrf_token": csrf_of(client.get(
                                          "/log/campaign?acmi=claim").text),
                                      "return_to": "https://evil.example.com/"},
                                follow_redirects=False)
                check("return_to 拒绝绝对外链",
                      r.headers.get("location", "").startswith("/log/campaign"),
                      "得到 %s" % r.headers.get("location"))
        finally:
            import gfvfw.config as _c
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig
            _c.settings.storage_dir = orig_storage

    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
