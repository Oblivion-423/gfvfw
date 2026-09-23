"""
开发探针（非产品代码）：ACMI 工作台三条入口的端到端验证。

要证明的是三件事，且都用**真实 ACMI**跑：

1. 战役管理 · 战役详情上传 → 归并出的任务**自动归入该战役**；
2. 飞行记录 · 战役记录（筛到某战役）上传 → 归入该战役；
3. 飞行记录 · 训练记录上传 → 记为**日常训练**（training 且不归入战役），
   从而必然出现在训练记录列表里。

全程在临时库里跑，不触碰生产 ``var/`` 数据。

运行:
    .venv\\Scripts\\python.exe scripts\\acmi_campaign_e2e_probe.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AcmiActor, AcmiFile, Campaign, Member, MemberRole, Mission, Role, Sortie, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

ACMI_DIR = Path(r"G:\BMS\backup\新建文件夹\Acmi")
SIZE_LO, SIZE_HI = 8.0, 14.0          # 这个区间的文件内容较完整，且解析够快
MAX_CANDIDATES = 6

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
FAILS: list[str] = []

#: 从归并表单里把字段"像浏览器那样"抠出来 ——
#: 否则探针会绕过页面上的锁定（如训练记录页把任务类型锁成 training），
#: 于是测出来的行为与用户实际点按钮的行为不一致。
_MERGE_FORM_RE = re.compile(
    r'<form[^>]*action="/acmi/merge".*?</form>', re.S)
_INPUT_RE = re.compile(r'<input[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"[^>]*>')
_SELECT_RE = re.compile(r'<select[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>', re.S)
_OPTION_RE = re.compile(r'<option[^>]*\bvalue="([^"]*)"([^>]*)>')


def csrf_of(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def merge_form_fields(html: str) -> dict:
    """解析归并表单的隐藏字段与下拉框选中值（模拟浏览器提交的内容）。"""
    blob = _MERGE_FORM_RE.search(html)
    if not blob:
        return {}
    form = blob.group(0)
    fields: dict[str, str] = {}
    for name, value in _INPUT_RE.findall(form):
        if name == "file_ids":
            continue
        fields[name] = value
    for name, body in _SELECT_RE.findall(form):
        opts = _OPTION_RE.findall(body)
        chosen = None
        for value, attrs in opts:
            if "selected" in attrs:
                chosen = value
                break
        if chosen is None and opts:
            chosen = opts[0][0]
        if chosen is not None:
            fields[name] = chosen
    return fields


def step(n, label, detail=""):
    print("\n[%s] %s" % (n, label))
    if detail:
        for line in detail.splitlines():
            print("      " + line)


def check(label, cond, detail=""):
    print("      %s  %s %s" % ("PASS" if cond else "FAIL", label, detail))
    if not cond:
        FAILS.append("%s %s" % (label, detail))


def build_app(tmpdir: Path):
    db_path = tmpdir / "probe.sqlite3"
    engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.config as cfgmod
    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod

    appmod = sys.modules["gfvfw.web.app"]
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession
    # storage 也要重定向，否则真实 ACMI 会落进产品存储目录
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)

    with TestSession() as db:
        seed(db)
    return appmod.app, TestSession


def login(client, username):
    page = client.get("/login")
    r = client.post("/login",
                    data={"username": username, "password": "password123",
                          "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    return r.status_code == 303


def candidates():
    out = []
    for p in ACMI_DIR.glob("*.acmi"):
        try:
            mb = p.stat().st_size / (1024 * 1024)
        except OSError:
            continue
        if SIZE_LO <= mb <= SIZE_HI:
            out.append((mb, p))
    out.sort(key=lambda t: -t[0])
    return out[:MAX_CANDIDATES]


def run_flow(client, TestSession, src: Path, host_url: str, campaign_id: str,
             tag: str):
    """在 host_url 上走完 上传 → 认领 → 归并，返回新建的 Mission（或 None）。"""
    step(tag, "宿主页面 %s" % host_url)

    # --- 上传：先打开宿主页拿 csrf，再 POST（工作台就在这一页上） ---
    page = client.get(host_url)
    check("宿主页可访问", page.status_code == 200, "HTTP %s" % page.status_code)
    check("页面含 ACMI 工作台", "acmiWorkbench" in page.text)

    raw = src.read_bytes()
    r = client.post("/acmi/upload",
                    data={"csrf_token": csrf_of(page.text),
                          "return_to": host_url.split("?")[0],
                          "campaign_id": campaign_id},
                    files=[("files", (src.name, raw, "application/octet-stream"))],
                    follow_redirects=False)
    loc = r.headers.get("location") or ""
    print("      上传  -> HTTP %s  Location=%s" % (r.status_code, loc))
    if r.status_code != 303 or "acmi=claim" not in loc:
        check("上传后跳到认领阶段", False, loc)
        return None
    check("上传后跳到认领阶段", True)

    # --- 认领：把**这份文件里的**飞行员名逐个绑到同一成员 ---
    cp = client.get(loc)
    check("认领阶段可渲染", cp.status_code == 200, "HTTP %s" % cp.status_code)
    with TestSession() as db:
        f = db.scalars(select(AcmiFile)
                       .order_by(AcmiFile.created_at.desc())).first()
        names = sorted(json.loads(f.pilot_names_json) if f.pilot_names_json else [])
        member = db.scalar(select(Member).order_by(Member.callsign))
        mid = member.id if member else None
    if not names or not mid:
        check("该文件含飞行员名", False, "names=%d mid=%s" % (len(names), mid))
        return None
    check("该文件含飞行员名", True, "共 %d 个，如 %s"
          % (len(names), "、".join(names[:3])))
    ok = 0
    for nm in names:
        rr = client.post("/acmi/claim",
                         data={"csrf_token": csrf_of(cp.text), "raw_name": nm,
                               "member_id": mid,
                               "return_to": host_url.split("?")[0],
                               "campaign_id": campaign_id},
                         follow_redirects=False)
        ok += (rr.status_code == 303)
        cp = client.get(loc)          # 令牌可能随会话变化，逐次取新的
    print("      认领  -> %d/%d 个名字已绑定" % (ok, len(names)))
    check("全部认领被接受", ok == len(names), "%d/%d" % (ok, len(names)))

    # --- 归并：字段从页面里取，等价于用户点"确认归并并创建任务" ---
    mp = client.get("%s?acmi=merge%s" % (host_url.split("?")[0],
                                         "&campaign_id=%s" % campaign_id if campaign_id else ""))
    check("归并阶段可渲染", mp.status_code == 200, "HTTP %s" % mp.status_code)
    fields = merge_form_fields(mp.text)
    if not fields:
        check("归并表单可解析", False)
        return None
    check("归并表单可解析", True, "字段=%s" % sorted(fields))
    with TestSession() as db:
        fids = [f.id for f in db.scalars(
            select(AcmiFile).where(AcmiFile.mission_id.is_(None))).all()]
    r = client.post("/acmi/merge",
                    data={**fields, "file_ids": fids,
                          "mission_name": "探针 · %s" % tag},
                    follow_redirects=False)
    print("      归并  -> HTTP %s  Location=%s" % (r.status_code,
                                                  r.headers.get("location")))
    print("      提交字段: mission_type=%r campaign_id=%r"
          % (fields.get("mission_type"), fields.get("campaign_id")))
    if r.status_code != 303:
        check("归并被接受", False, r.text[:200].replace("\n", " "))
        return None
    check("归并被接受", True)

    with TestSession() as db:
        mission = db.scalars(select(Mission)
                             .where(Mission.name == "探针 · %s" % tag)).first()
        if mission is None:
            check("生成了任务", False)
            return None
        n_sorties = len(db.scalars(
            select(Sortie).where(Sortie.mission_id == mission.id)).all())
    check("生成了任务", True, "id=%s" % mission.id[:8])
    print("      任务 %s  type=%s  campaign_id=%s  sorties=%d"
          % (mission.id[:8], mission.mission_type, mission.campaign_id, n_sorties))
    return mission


def main() -> int:
    print("=" * 72)
    print("ACMI 工作台 · 三条入口端到端探针（真实 ACMI）")
    print("=" * 72)

    cands = candidates()
    if len(cands) < 3:
        print("!! 候选真实 ACMI 不足 3 个（%d）" % len(cands))
        return 1
    for mb, p in cands:
        print("   候选 %7.1f MB  %s" % (mb, p.name))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        app, TestSession = build_app(Path(td))
        with TestClient(app) as client:
            with TestSession() as db:
                m = Member(callsign="Oblivion", status="active")
                db.add(m)
                db.flush()
                db.add(User(username="oblivion",
                            password_hash=hash_password("password123"),
                            status="active", member_id=m.id))
                role = db.scalar(select(Role).where(Role.code == "owner"))
                db.add(MemberRole(member_id=m.id, role_id=role.id))
                camp = Campaign(name="探针战役", status="active")
                db.add(camp)
                db.commit()
                cid = camp.id
            assert login(client, "oblivion"), "登录失败"

            # --- 入口 A：战役管理 · 战役详情 → 自动归入该战役 ---
            mA = run_flow(client, TestSession, cands[0][1],
                          "/theater/%s" % cid, cid, "A·战役详情")
            if mA:
                check("A 归入当前战役", mA.campaign_id == cid,
                      "campaign_id=%s 期望 %s" % (mA.campaign_id, cid))

            # --- 入口 B：飞行记录 · 战役记录（筛到该战役） ---
            mB = run_flow(client, TestSession, cands[1][1],
                          "/log/campaign?campaign_id=%s" % cid, cid, "B·战役记录")
            if mB:
                check("B 归入选中的战役", mB.campaign_id == cid,
                      "campaign_id=%s 期望 %s" % (mB.campaign_id, cid))
                # 战役记录页应能列出它
                lp = client.get("/log/campaign?campaign_id=%s" % cid)
                check("B 出现在战役记录页", "探针 · B·战役记录" in lp.text)

            # --- 入口 C：飞行记录 · 训练记录 → 日常训练 ---
            mC = run_flow(client, TestSession, cands[2][1],
                          "/log/training", "", "C·训练记录")
            if mC:
                check("C 不归入任何战役", mC.campaign_id is None,
                      "campaign_id=%r" % mC.campaign_id)
                check("C 类型固定为 training", mC.mission_type == "training",
                      "mission_type=%s" % mC.mission_type)
                tp = client.get("/log/training")
                check("C 出现在训练记录页", "探针 · C·训练记录" in tp.text)

            print("\n" + "=" * 72)
            if FAILS:
                print("失败 %d 项：" % len(FAILS))
                for f in FAILS:
                    print("  - " + f)
            else:
                print("三条入口全部通过。")
            print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
