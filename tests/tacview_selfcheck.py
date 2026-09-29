"""Web 层自校验：Tacview XML 导出上传 → 战斗分析页 → 删除。

链路：任务详情页上传「Export Flight Log」XML → sha256 去重 →
分析器（``gfvfw.tacview_analyzer``，内置自 TacviewLogAnalyzer）产出
击杀链 / 武器效能 / 空战分组 / 飞行结局 → ``/tacview/{id}`` 展示。

与 ``.acmi`` 摄入（``acmi_web_selfcheck``）平行的另一条数据通路：
XML 事件**不产生架次**，只做战斗分析。

运行:
    .venv\\Scripts\\python.exe tests\\tacview_selfcheck.py
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
    AcmiFile, AuditLog, Campaign, Member, MemberRole, Mission, Role,
    TacviewXmlFile, USER_STATUSES, User,
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
# 合成 Tacview XML（Export Flight Log 结构，事件覆盖分析器全部分支）
# --------------------------------------------------------------------------

def _obj(tag: str, oid: int, otype: str, name: str, coalition: str = "",
         pilot: str = "") -> str:
    parts = [f'<{tag} ID="{oid}">', f"<Type>{otype}</Type>",
             f"<Name>{name}</Name>"]
    if coalition:
        parts.append(f"<Coalition>{coalition}</Coalition>")
    if pilot:
        parts.append(f"<Pilot>{pilot}</Pilot>")
    parts.append(f"</{tag}>")
    return "".join(parts)


def synthetic_tacview_xml() -> str:
    """两人任务的完整事件链：

    * Oblivion：AIM-9M 击落 MiG-29（确定性链）；第二发未命中；
      AGM-65 被敌方 SAM 拦截（interception 分支）；起飞后降落。
    * Viper：GBU-12（Occurrences=2，启发式/连投分支）一次投放
      摧毁两个地面目标（溅射 +1）；起飞后被击落。
    """
    ev: list[str] = []

    def event(time: str, primary: str, action: str,
              secondary: str = "", locked: str = "", parent: str = "",
              occurrences: int = 0) -> None:
        occ = f"<Occurrences>{occurrences}</Occurrences>" if occurrences else ""
        body = f"<Time>{time}</Time><Location><Longitude>5.97</Longitude>" \
               f"<Latitude>53.18</Latitude><Altitude>100</Altitude></Location>" \
               f"{primary}<Action>{action}</Action>{locked}{secondary}{parent}{occ}"
        ev.append(f"<Event>{body}</Event>")

    # ---- 进场与起飞 ----
    event("100", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasEnteredTheArea")
    event("105", _obj("PrimaryObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"), "HasEnteredTheArea")
    event("110", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasTakenOff")
    event("115", _obj("PrimaryObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"), "HasTakenOff")

    # ---- Oblivion：AIM-9M 命中并击落 MiG-29（确定性：weapon_id=200）----
    event("120", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasFired",
          secondary=_obj("SecondaryObject", 200, "Missile", "AIM-9M Sidewinder",
                         "Blue"),
          locked=_obj("LockedObject", 300, "Aircraft", "MiG-29 Fulcrum", "Red"))
    event("150", _obj("PrimaryObject", 300, "Aircraft", "MiG-29 Fulcrum", "Red"),
          "HasBeenHitBy",
          secondary=_obj("SecondaryObject", 200, "Missile",
                         "AIM-9M Sidewinder", "Blue"),
          parent=_obj("ParentObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"))
    event("160", _obj("PrimaryObject", 300, "Aircraft", "MiG-29 Fulcrum", "Red"),
          "HasBeenDestroyed",
          secondary=_obj("SecondaryObject", 100, "Aircraft", "F-16CM-52",
                         "Blue", "Oblivion"))

    # ---- Oblivion：第二发未命中（无命中/击毁事件 → miss）----
    event("180", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasFired",
          secondary=_obj("SecondaryObject", 201, "Missile",
                         "AIM-9M Sidewinder", "Blue"),
          locked=_obj("LockedObject", 301, "Aircraft", "MiG-29 Fulcrum", "Red"))

    # ---- Oblivion：AGM-65 被敌方 SAM 拦截（A-G 武器拦截分支）----
    event("200", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasFired",
          secondary=_obj("SecondaryObject", 202, "Missile", "AGM-65 Maverick",
                         "Blue"),
          locked=_obj("LockedObject", 310, "Vehicle", "SA-6 Launcher", "Red"))
    event("220", _obj("PrimaryObject", 400, "Missile", "SA-13 Gopher", "Red"),
          "HasBeenHitBy",
          secondary=_obj("SecondaryObject", 202, "Missile", "AGM-65 Maverick",
                         "Blue"),
          parent=_obj("ParentObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"))

    # ---- Oblivion 降落 ----
    event("500", _obj("PrimaryObject", 100, "Aircraft", "F-16CM-52",
                      "Blue", "Oblivion"), "HasLanded")

    # ---- Viper：GBU-12 连投（Occurrences=2），一次投放两个地面目标 ----
    event("240", _obj("PrimaryObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"), "HasFired",
          secondary=_obj("SecondaryObject", 210, "Bomb", "GBU-12 Paveway II",
                         "Blue"),
          locked=_obj("LockedObject", 310, "Vehicle", "SA-6 Launcher", "Red"),
          occurrences=2)
    event("300", _obj("PrimaryObject", 310, "Vehicle", "SA-6 Launcher", "Red"),
          "HasBeenHitBy",
          secondary=_obj("SecondaryObject", 210, "Bomb",
                         "GBU-12 Paveway II", "Blue"),
          parent=_obj("ParentObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"))
    event("310", _obj("PrimaryObject", 310, "Vehicle", "SA-6 Launcher", "Red"),
          "HasBeenDestroyed",
          secondary=_obj("SecondaryObject", 101, "Aircraft", "F-16CM-52",
                         "Blue", "Viper"))
    event("305", _obj("PrimaryObject", 311, "Vehicle", "SA-6 TEL", "Red"),
          "HasBeenHitBy",
          secondary=_obj("SecondaryObject", 210, "Bomb",
                         "GBU-12 Paveway II", "Blue"),
          parent=_obj("ParentObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"))
    event("315", _obj("PrimaryObject", 311, "Vehicle", "SA-6 TEL", "Red"),
          "HasBeenDestroyed",
          secondary=_obj("SecondaryObject", 101, "Aircraft", "F-16CM-52",
                         "Blue", "Viper"))

    # ---- Viper 起飞后被击落（HasLanded 之外的结局）----
    event("400", _obj("PrimaryObject", 101, "Aircraft", "F-16CM-52",
                      "Blue", "Viper"), "HasBeenDestroyed",
          secondary=_obj("SecondaryObject", 500, "Aircraft", "MiG-29 Fulcrum",
                         "Red", "Bandit"))

    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        '<TacviewDebriefing Version="1.2.6">\n'
        "<FlightRecording><Source>Falcon 4.0</Source>"
        "<Recorder>Falcon BMS 4.38</Recorder></FlightRecording>\n"
        "<Mission><Title>selfcheck</Title>"
        "<MissionTime>2026-09-01T00:00:00Z</MissionTime>"
        "<Duration>500</Duration><MainAircraftID>100</MainAircraftID></Mission>\n"
        "<Events>\n" + "\n".join(ev) + "\n</Events>\n"
        "</TacviewDebriefing>\n"
    )


def synthetic_acmi() -> str:
    """合成一份**裸 ACMI 录像**（BMS 4.38 口径：无任何战斗事件属性，
    只有轨迹与 CAS）—— 验证直传 .acmi 时转换器的推断链：

    * Oblivion（对象2）10s 在地面 → 20s 离地（CAS=150）→ 110s 接地（CAS=10）
    * 40s 发射 AIM-9M（对象4，首现位置≈载机）→ 50s 在 MiG-29（对象3）旁起爆
      （爆炸对象5）→ MiG-29 50s 后不再更新 → 被击杀
    * 80s 再射一枚 AIM-9M（对象6）→ 90s 飞到无人区起爆（爆炸对象7）→ 脱靶
    """
    ev: list[str] = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.1",
        "0,DataRecorder=Falcon BMS 4.38",
        "0,DataSource=Falcon 4.0",
        "0,ReferenceTime=2026-9-1T00:00:00Z",
        "#10",
        "2,T=26.0|40.0|100|0|0|0|0|0|90,"
        "Type=Air+FixedWing,Name=F-16CM-52,Pilot=Oblivion,Coalition=Blue,CAS=0",
        "#20",
        "2,T=26.0005|40.0005|200|0|0|0|50|90|90,CAS=150",
        "#30",
        "3,T=26.05|40.06|3000|0|0|0|5000|6000|270,"
        "Type=Air+FixedWing,Name=MiG-29 Fulcrum,Coalition=Red,CAS=450",
        "#40",
        "2,T=26.001|40.001|800|0|0|0|110|180|90,CAS=420",
        "4,T=26.0012|40.0012|800|0|0|0|115|185|90,"
        "Type=Weapon+Missile,Name=AIM-9M Sidewinder,Coalition=Blue,LockedTarget=3",
        "#50",
        "4,T=26.0497|40.0597|2990|0|0|0|4900|5900|90,CAS=900",
        "3,T=26.05|40.06|3000|0|0|0|5000|6000|270,CAS=450",
        "5,T=26.0497|40.0597|2990|0|0|0|4900|5900|90,"
        "Type=Misc+Explosion+Medium,Name=Explosion,Coalition=Blue",
        "#70",
        "2,T=26.01|40.01|500|0|0|0|100|200|90,CAS=400",
        "#80",
        "2,T=26.01|40.01|500|0|0|0|100|200|90,CAS=400",
        "6,T=26.0105|40.0105|500|0|0|0|102|202|90,"
        "Type=Weapon+Missile,Name=AIM-9M Sidewinder,Coalition=Blue",
        "#90",
        "6,T=26.2|40.2|2900|0|0|0|19000|20000|95,CAS=900",
        "#95",
        "7,T=26.2|40.2|2900|0|0|0|19000|20000|95,"
        "Type=Misc+Explosion+Medium,Name=Explosion,Coalition=Blue",
        "#110",
        "2,T=26.0102|40.0102|120|0|0|0|20|40|90,CAS=10",
    ]
    return "\n".join(ev) + "\n"


def build_app(tmpdir: Path):
    db_path = tmpdir / "tacview.sqlite3"
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

    import gfvfw.config as cfgmod
    orig_storage = cfgmod.settings.storage_dir
    test_storage = tmpdir / "storage"
    test_storage.mkdir(parents=True, exist_ok=True)
    cfgmod.settings.storage_dir = test_storage

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_user(db, callsign: str, role_code: str):
    username = callsign.lower()
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
            xml_bytes = synthetic_tacview_xml().encode("utf-8")

            with TestSession() as db:
                admin_mid, admin_user = make_user(db, "Oblivion", "owner")
                member_mid, member_user = make_user(db, "Rookie", "member")
                # 真游客：注册了账号但未被提升为队员（status=pending，可登录
                # 但拿不到权限点 —— 见 identity.USER_STATUSES）
                db.add(User(username="guestuser",
                            password_hash=hash_password("password123"),
                            status="pending"))
                db.commit()
                base = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
                camp = Campaign(name="分析战役", status="active")
                db.add(camp)
                db.flush()
                mission = Mission(name="分析任务-01", campaign_id=camp.id,
                                  started_at=base, mission_type="cap")
                db.add(mission)
                db.commit()
                mid = mission.id

            print("\n[1] 权限边界")
            with TestClient(app) as client:
                # 匿名 → 跳登录；游客 → 403 说明页（分析页与写操作仅队员）
                r = client.post("/tacview/upload", data={},
                                follow_redirects=False)
                check("匿名 POST 上传 → 跳登录",
                      r.status_code == 303 and "/login" in r.headers.get("location", ""),
                      "得到 %d" % r.status_code)
                r = client.get("/tacview/none", follow_redirects=False)
                check("匿名看分析页 → 跳登录",
                      r.status_code == 303 and "/login" in r.headers.get("location", ""),
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, member_user)
                r = client.post("/tacview/upload",
                                data={"csrf_token": "x", "mission_id": mid},
                                follow_redirects=False)
                check("游客伪造上传 POST → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
            with TestClient(app) as client:
                login(client, "guestuser")
                r = client.get("/tacview/none", follow_redirects=False)
                check("游客看分析页 → 403 说明页（不跳登录）",
                      r.status_code == 403, "得到 %d" % r.status_code)
                r = client.get("/missions/%s" % mid, follow_redirects=False)
                check("游客看任务详情 → 403（分析入口随任务仅队员）",
                      r.status_code == 403, "得到 %d" % r.status_code)

            print("\n[2] 上传与解析（管理员，走任务详情页入口）")
            with TestClient(app) as client:
                login(client, admin_user)
                page = client.get("/missions/%s" % mid)
                check("任务详情页含 Tacview 区块", "Tacview 战斗分析" in page.text)

                r = client.post("/tacview/upload",
                                files={"file": ("mission.xml", xml_bytes,
                                                "application/xml")},
                                data={"csrf_token": csrf_of(page.text),
                                      "mission_id": mid},
                                follow_redirects=False)
                loc = r.headers.get("location", "")
                check("上传 303 回任务详情页", r.status_code == 303
                      and loc.startswith("/missions/%s" % mid),
                      "得到 %d %s" % (r.status_code, loc))
                check("回执 did=uploaded", "did=uploaded" in loc, loc)

                with TestSession() as db:
                    rec = db.scalar(select(TacviewXmlFile))
                    check("tacview_xml_files 已入库", rec is not None)
                    check("挂在任务上", rec.mission_id == mid)
                    check("解析成功（vm_json 非空）", bool(rec.vm_json),
                          rec.parse_error or "")
                    check("事件计数", rec.event_count == 17,
                          "得到 %d" % rec.event_count)
                    check("两名飞行员", rec.human_pilots == 2,
                          "得到 %d" % rec.human_pilots)
                    # 射击：AIM-9×2 + AGM-65×1 + GBU-12（Occurrences=2）= 5
                    check("总射击 = 5", rec.total_shots == 5,
                          "得到 %d" % rec.total_shots)
                    # 命中按「成功的射击」计：MiG-29（1 发）+ GBU 同一发
                    # 打掉两个目标（去重后 1 发）；被拦截的 AGM-65 不算命中
                    check("总命中 = 2", rec.total_hits == 2,
                          "得到 %d" % rec.total_hits)
                    # 击杀同样按「造成击杀的射击」去重：AIM-9 1 发 +
                    # GBU 1 发（双杀但同一发）= 2
                    check("总击杀 = 2", rec.total_kills == 2,
                          "得到 %d" % rec.total_kills)
                    # 未中：AIM-9 第二发（脱靶）+ 被拦截的 AGM-65 = 2
                    check("总未中 = 2", rec.total_misses == 2,
                          "得到 %d" % rec.total_misses)
                    check("结局：1 降落 + 1 被击落",
                          rec.landed_pilots == 1 and rec.ejected_or_shot_pilots == 1,
                          "得到 %d/%d" % (rec.landed_pilots, rec.ejected_or_shot_pilots))
                    rec_id = rec.id

                page = client.get(loc)
                check("任务详情页列出归档文件并内嵌分析",
                      "mission.xml" in page.text
                      and "完整分析" in page.text
                      and "空空 射/中/杀" in page.text)

                print("\n[3] 战斗分析页")
                r = client.get("/tacview/%s" % rec_id)
                check("分析页可访问", r.status_code == 200,
                      "得到 %d" % r.status_code)
                for text in ("战斗分析", "全体武器效能", "Oblivion", "Viper",
                             "AIM-9M Sidewinder", "MiG-29 Fulcrum", "GBU-12",
                             "空空", "空地", "已降落", "被击落", "被拦截"):
                    check("分析页含「%s」" % text, text in r.text)
                # GBU-12 带 weapon_id，两发命中都走确定性关联（非启发式）
                check("全部链均为确定性关联（无启发式徽标）",
                      "启发式" not in r.text)
                # 拦截分支：AGM-65 的目标显示为拦截弹而非原目标
                check("AGM-65 链显示拦截弹名", "SA-13 Gopher" in r.text)
                # 误击：Viper 被红方击落不算误击；本例无误击事件
                check("无误击徽标", "误击" not in r.text)

                r = client.get("/tacview/%s/download" % rec_id)
                check("原件可下载", r.status_code == 200
                      and b"TacviewDebriefing" in r.content,
                      "得到 %d" % r.status_code)

            print("\n[4] 去重（同内容重复上传不重复入库）")
            with TestClient(app) as client:
                login(client, admin_user)
                page = client.get("/missions/%s" % mid)
                r = client.post("/tacview/upload",
                                files={"file": ("再次上传.xml", xml_bytes,
                                                "application/xml")},
                                data={"csrf_token": csrf_of(page.text),
                                      "mission_id": mid},
                                follow_redirects=False)
                check("重复上传 did=duplicate",
                      "did=duplicate" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    n = db.scalar(select(func.count()).select_from(TacviewXmlFile))
                    check("未产生重复记录", n == 1, "得到 %d" % n)

            print("\n[5] 解析失败照常归档")
            with TestClient(app) as client:
                login(client, admin_user)
                page = client.get("/missions/%s" % mid)
                r = client.post("/tacview/upload",
                                files={"file": ("坏文件.xml", b"this is not xml",
                                                "application/xml")},
                                data={"csrf_token": csrf_of(page.text),
                                      "mission_id": mid},
                                follow_redirects=False)
                check("坏文件 did=failed",
                      "did=failed" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    bad = db.scalar(select(TacviewXmlFile).where(
                        TacviewXmlFile.parse_error.isnot(None)))
                    check("坏文件已归档且记录原因",
                          bad is not None and bool(bad.parse_error),
                          (bad.parse_error or "")[:80] if bad else "无记录")
                    bad_id = bad.id if bad else ""
                page = client.get("/missions/%s" % mid)
                check("任务页标注解析失败", "解析失败" in page.text)
                r = client.get("/tacview/%s" % bad_id)
                check("分析页解释失败原因", r.status_code == 200
                      and "解析失败" in r.text and "Export Flight Log" in r.text)

            print("\n[7] 直接上传 .acmi（自动转换战斗事件）")
            with TestClient(app) as client:
                login(client, admin_user)
                page = client.get("/missions/%s" % mid)
                r = client.post(
                    "/tacview/upload",
                    files={"file": ("2026-09-01_flight.acmi",
                                    synthetic_acmi().encode("utf-8"),
                                    "application/octet-stream")},
                    data={"csrf_token": csrf_of(page.text), "mission_id": mid},
                    follow_redirects=False)
                loc = r.headers.get("location", "")
                check("ACMI 直传 303 且 did=uploaded",
                      r.status_code == 303 and "did=uploaded" in loc,
                      "得到 %d %s" % (r.status_code, loc))
                with TestSession() as db:
                    rec = db.scalar(select(TacviewXmlFile).where(
                        TacviewXmlFile.source_format == "acmi",
                        TacviewXmlFile.original_filename
                        == "2026-09-01_flight.acmi"))
                    check("记录标记 source_format=acmi",
                          rec is not None and rec.source_format == "acmi",
                          "得到 %s" % (rec.source_format if rec else None))
                    check("存档的是转换产物（.converted.xml）",
                          rec.stored_path.endswith(".converted.xml"),
                          rec.stored_path)
                    check("转换后解析成功", bool(rec.vm_json),
                          rec.parse_error or "")
                    check("推断：1 名飞行员", rec.human_pilots == 1,
                          "得到 %d" % rec.human_pilots)
                    check("推断：射击 2 / 命中 1 / 击杀 1",
                          (rec.total_shots, rec.total_hits, rec.total_kills) == (2, 1, 1),
                          "得到 %d/%d/%d" % (rec.total_shots, rec.total_hits, rec.total_kills))
                    check("推断：脱靶 1", rec.total_misses == 1,
                          "得到 %d" % rec.total_misses)
                    check("推断：起飞并降落（1/0）",
                          rec.landed_pilots == 1 and rec.ejected_or_shot_pilots == 0,
                          "得到 %d/%d" % (rec.landed_pilots, rec.ejected_or_shot_pilots))
                    acmi_rec_id = rec.id
                page = client.get("/tacview/%s" % acmi_rec_id)
                check("ACMI 分析页可访问", page.status_code == 200,
                      "得到 %d" % page.status_code)
                for text in ("由 .acmi 自动转换", "MiG-29 Fulcrum", "AIM-9M",
                             "空空", "已降落"):
                    check("ACMI 分析页含「%s」" % text, text in page.text)
                # 两发都是空空 → 空地拆分应为 0/0/0（"空地"标签本身恒在）
                check("ACMI 射击全部判为空空（空地拆分 0/0/0）",
                      "0/0/0" in page.text)

            print("\n[8] 重新解析与删除（自己的文件 / 他人的要 any 权限）")
            with TestClient(app) as client:
                login(client, member_user)
                # member 没有 acmi.upload.any：不能动管理员上传的文件
                r = client.post("/tacview/%s/delete" % rec_id,
                                data={"csrf_token": csrf_of(
                                    client.get("/missions/%s" % mid).text)},
                                follow_redirects=False)
                check("他人文件删除 → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/tacview/%s/reparse" % rec_id,
                                data={"csrf_token": csrf_of(
                                    client.get("/missions/%s" % mid).text)},
                                follow_redirects=False)
                check("他人文件重解析 → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # member 自己传一份再删（自己即可删，不需要 any）
                page = client.get("/missions/%s" % mid)
                r = client.post("/tacview/upload",
                                files={"file": ("rookie.xml", xml_bytes,
                                                "application/xml")},
                                data={"csrf_token": csrf_of(page.text),
                                      "mission_id": mid},
                                follow_redirects=False)
                # 内容与 admin 那份相同 → 命中去重，但 mission_id 已设置，
                # 落点仍是 duplicate；此时 rec.uploaded_by 仍是 admin。
                check("同内容第二次上传仍是 duplicate",
                      "did=duplicate" in r.headers.get("location", ""),
                      r.headers.get("location", ""))

            with TestClient(app) as client:
                login(client, admin_user)
                with TestSession() as db:
                    rec = db.get(TacviewXmlFile, rec_id)
                    stored = rec.stored_path
                page = client.get("/tacview/%s" % rec_id)
                token = csrf_of(page.text)
                r = client.post("/tacview/%s/reparse" % rec_id,
                                data={"csrf_token": token},
                                follow_redirects=False)
                loc = r.headers.get("location", "")
                check("重解析后回分析页", "did=reparsed" in loc
                      or "/tacview/%s" % rec_id in loc,
                      loc or str(r.status_code))

                r = client.post("/tacview/%s/delete" % rec_id,
                                data={"csrf_token": csrf_of(
                                    client.get("/missions/%s" % mid).text)},
                                follow_redirects=False)
                check("删除 303 回任务页",
                      r.status_code == 303
                      and "did=deleted" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    check("行已硬删", db.get(TacviewXmlFile, rec_id) is None)
                    from gfvfw.config import settings as cfg
                    p = cfg.storage_dir / stored
                    check("磁盘原件一并删除", not p.exists(), str(p))

            print("\n[9] 工作台上传 .acmi 即分析（归并后自动挂到任务）")
            with TestClient(app) as client:
                login(client, admin_user)
                # --- 经 ACMI 工作台上传（内容与 [7] 不同，换一个坐标基准） ---
                up = client.get("/log/campaign?acmi=upload")
                acmi_bytes = synthetic_acmi().replace("26.", "27.").encode("utf-8")
                r = client.post(
                    "/acmi/upload",
                    files={"files": ("2026-09-02_flight.acmi", acmi_bytes,
                                     "application/octet-stream")},
                    data={"csrf_token": csrf_of(up.text),
                          "return_to": "/log/campaign", "campaign_id": ""},
                    follow_redirects=False)
                loc = r.headers.get("location", "")
                check("工作台上传 .acmi → 303 进认领阶段",
                      r.status_code == 303 and "acmi=claim" in loc,
                      "得到 %d %s" % (r.status_code, loc))
                with TestSession() as db:
                    af = db.scalar(select(AcmiFile).where(
                        AcmiFile.original_filename == "2026-09-02_flight.acmi"))
                    check("ACMI 已入库且解析成功",
                          af is not None and af.parse_status == "parsed",
                          af.parse_status if af else "无记录")
                    tv = db.scalar(select(TacviewXmlFile).where(
                        TacviewXmlFile.sha256 == af.sha256))
                    check("★ 上传即生成战斗分析（无需单独上传）",
                          tv is not None and bool(tv.vm_json),
                          tv.parse_error if tv else "无分析记录")
                    check("分析此时还未挂任务（归并后才有归属）",
                          tv is not None and tv.mission_id is None,
                          tv.mission_id if tv else "-")
                    af_id, tv_id = af.id, tv.id

                # --- 归并确认 → 分析自动挂到新任务 ---
                mp = client.get("/log/campaign?acmi=merge")
                token = csrf_of(mp.text)
                r = client.post("/acmi/merge",
                                data={"file_ids": [af_id],
                                      "mission_name": "工作台分析任务",
                                      "mission_type": "other",
                                      "visibility": "public",
                                      "csrf_token": token},
                                follow_redirects=False)
                loc = r.headers.get("location", "")
                check("归并确认 303 跳任务详情", r.status_code == 303
                      and "/missions/" in loc, "得到 %d %s" % (r.status_code, loc))
                with TestSession() as db:
                    tv = db.get(TacviewXmlFile, tv_id)
                    check("★ 归并后分析自动挂到任务",
                          tv.mission_id is not None
                          and tv.mission_id in loc,
                          "mission_id=%s loc=%s" % (tv.mission_id, loc))
                    new_mid = tv.mission_id
                page = client.get("/missions/%s" % new_mid)
                check("任务详情页内嵌战斗分析",
                      "Tacview 战斗分析" in page.text
                      and "由 .acmi 自动转换，推断口径" in page.text)
                # 分析里的飞行时长（事件口径 90s → 00:01:30）与架次口径同现
                check("内嵌分析含飞行员统计表",
                      "空空 射/中/杀" in page.text and "00:01:30" in page.text)
                check("任务页不再有独立上传表单",
                      'action="/tacview/upload"' not in page.text)

            # 审计记录单独核对（避免作用域混淆）
            with TestSession() as db:
                n = db.scalar(select(func.count()).select_from(AuditLog))
                check("上传/删除均留痕（审计 ≥ 3 条）", (n or 0) >= 3,
                      "得到 %d" % (n or 0))

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
