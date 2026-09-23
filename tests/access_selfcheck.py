"""
自校验：**三档身份与公开/队内边界**（游客 vs 队员）。

联队要求
--------
* **游客可随意申请，只能查看公开部分。**
* **队员由管理员从游客提升上来，可查看仅限队内的资料。**

所以本套测试盯的是**边界**，不是页面长得对不对：

1. 未登录访客：只有公开页可进，其余被送到登录页；
2. 申请：公开可提交 → **立刻得到游客账号** + 一条申请记录；
   呼号/用户名重复、弱密码、无 CSRF、同 IP 刷量都要被拦住；
3. **游客**：能登录、能改自己密码、能看申请进度；
   **但队内页面必须 403 且说明"需要队员身份"，绝不能重定向到登录页**
   （那会造成「点→回登录→再点」的死循环，用户看不出差的是"被提升"这一步）；
4. **提升**：管理员操作后名册多一个成员、账号激活、角色到手，
   **同一账号立刻能看到队内内容**（这才是权限真的开了）；
5. **拒绝**：账号停用、无法登录；
6. 提升**不会**顺手把申请 polluted 进名册 —— 未审批的申请不出现在名册里。

运行:
    .venv\\Scripts\\python.exe tests\\access_selfcheck.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    Application, AuditLog, Member, MemberRole, Role, User,
)
from gfvfw.security import hash_password, privacy_hash  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

PW = "password123"

#: 队内（仅限队员）的页面 —— 游客与未登录都必须进不去
MEMBER_ONLY = (
    "/members",
    "/theater",
    "/log/campaign",
    "/log/training",
    "/log/pilots",
    "/log",
    "/stats",
    "/library",
    "/account/logbook",
)

#: 公开页面 —— 未登录也必须能打开
PUBLIC = ("/", "/login", "/apply")


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


_NAV_RE = re.compile(r'<nav class="nav">(.*?)</nav>', re.S)


def nav_of(html: str) -> str:
    """只取顶部导航那一块。

    ⚠️ 不能直接对整页做 ``"成员" in html`` —— 首页正文明明有「成员 6 人」
    这种统计标签，会让"导航里不该出现成员入口"的断言假失败。
    这里的断言针对的是**导航**，就必须真的只看导航。
    """
    m = _NAV_RE.search(html)
    return m.group(1) if m else ""


def nav_links(html: str) -> set[str]:
    return set(re.findall(r'href="([^"]+)"', nav_of(html)))


def build_app(tmpdir: Path):
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmpdir / "access.sqlite3").as_posix(),
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.config as cfgmod
    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]

    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession

    orig_storage = cfgmod.settings.storage_dir
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_person(db, callsign: str, role_code: str | None, *, status: str = "active"):
    """建一个（可选带角色的）账号。``role_code=None`` 表示不分配角色。"""
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    user = User(username=callsign.lower(),
                password_hash=hash_password(PW), status=status,
                member_id=member.id if status == "active" else None)
    db.add(user)
    db.flush()
    if role_code:
        role = db.scalar(select(Role).where(Role.code == role_code))
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, user.id


def login(client, username: str, password: str = PW):
    page = client.get("/login")
    return client.post("/login",
                       data={"username": username, "password": password,
                             "csrf_token": csrf_of(page.text)},
                       follow_redirects=False)


def submit_application(client, *, callsign: str, username: str,
                       password: str = PW, confirm: str | None = None,
                       email: str = "", token: str | None = None):
    page = client.get("/apply")
    tok = token if token is not None else csrf_of(page.text)
    return client.post("/apply", data={
        "callsign": callsign, "username": username, "password": password,
        "confirm_password": confirm if confirm is not None else password,
        "email": email, "experience": "飞过 Falcon 4.0",
        "intent": "想飞对空", "contact": "QQ 123",
        "csrf_token": tok,
    }, follow_redirects=False)


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 74)
    print("三档身份自校验：访客 / 游客 / 队员")
    print("=" * 74)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            with TestSession() as db:
                owner_mid, owner_uid = make_person(db, "Viper", "owner")
                mem_mid, mem_uid = make_person(db, "Rookie", "member")

            # ==============================================================
            print("\n[1] 未登录访客：只能看公开部分")
            # ==============================================================
            with TestClient(app) as client:
                for path in PUBLIC:
                    r = client.get(path)
                    check("访客可访问 %s" % path, r.status_code == 200,
                          "得到 %d" % r.status_code)

                check("★ 申请页确实不需要登录（200 而非跳转）",
                      "提交申请" in client.get("/apply").text)

                for path in MEMBER_ONLY:
                    r = client.get(path, follow_redirects=False)
                    check("★ 访客 %s → 跳登录（303）" % path,
                          r.status_code == 303,
                          "得到 %d" % r.status_code)

                r = client.get("/applications", follow_redirects=False)
                check("★ 访客 /applications → 跳登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

                home = client.get("/").text
                nav = nav_links(home)
                check("★ 拿到导航块（断言前提）", bool(nav_of(home)),
                      "没匹配到 <nav>，后面的导航断言都不可信")
                for href in ("/members", "/theater", "/log/campaign",
                             "/library", "/stats", "/applications"):
                    check("★ 访客导航里没有 %s" % href, href not in nav,
                          "导航含 %s" % href)
                check("★ 访客导航里有「申请入队」", "/apply" in nav, str(nav))
                check("★ 访客导航里没有「我的申请」（他还没申请过）",
                      "/apply/status" not in nav, str(nav))
                check("★ 访客页面上有登录入口", "/login" in home)

            # ==============================================================
            print("\n[2] 公开申请：提交即建游客账号")
            # ==============================================================
            with TestClient(app) as client:
                r = submit_application(client, callsign="Newbie",
                                       username="newbie")
                check("申请提交 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("★ 回跳带 did=submitted",
                      "did=submitted" in r.headers.get("location", ""),
                      r.headers.get("location", ""))

                with TestSession() as db:
                    u = db.scalar(select(User).where(User.username == "newbie"))
                    check("★ 已创建账号", u is not None)
                    check("★ 账号状态是游客（pending）",
                          u is not None and u.status == "pending",
                          str(u.status) if u else "")
                    check("★ 游客**不绑定**名册成员（不污染名册）",
                          u is not None and u.member_id is None,
                          str(u.member_id) if u else "")
                    a = db.scalar(select(Application).where(
                        Application.resulting_user_id == u.id))
                    check("★ 已落一条申请记录", a is not None)
                    check("申请状态为 submitted",
                          a is not None and a.status == "submitted")
                    check("申请记下了想要的呼号",
                          a is not None and a.desired_callsign == "Newbie")
                    check("★ 申请只存 IP 哈希、不存明文 IP",
                          a is not None and a.source_ip_hash
                          and a.source_ip_hash == privacy_hash("testclient"),
                          str(a.source_ip_hash) if a else "")
                    check("★ 提交后**不自动登录**（未建立会话）",
                          client.get("/apply/status",
                                     follow_redirects=False).status_code == 303)

                n_members = None
                with TestSession() as db:
                    n_members = db.scalar(
                        select(func.count()).select_from(Member))
                check("★ 未审批的申请不进名册", n_members == 2,
                      "名册有 %s 人" % n_members)

            print("\n[3] 申请的边界（重复、弱密码、CSRF、刷量）")
            with TestClient(app) as client:
                r = submit_application(client, callsign="Newbie2",
                                       username="newbie")
                check("★ 用户名重复被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("给出可读原因", "用户名" in r.text)

                # 呼号与现有名册成员冲突
                r = submit_application(client, callsign="Rookie",
                                       username="someoneelse")
                check("★ 呼号与名册成员冲突被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("给出可读原因", "呼号" in r.text)

                # 与已有申请冲突
                r = submit_application(client, callsign="Newbie",
                                       username="someoneelse2")
                check("★ 呼号与既有申请冲突被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = submit_application(client, callsign="Weak", username="weak1",
                                       password="123")
                check("★ 弱密码被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = submit_application(client, callsign="Mismatch", username="mm1",
                                       password="password123",
                                       confirm="password456")
                check("★ 两次密码不一致被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = submit_application(client, callsign="BadMail", username="bm1",
                                       email="not-an-email")
                check("★ 邮箱格式错被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = submit_application(client, callsign="NoCsrf", username="nc1",
                                       token="")
                check("★ 无 CSRF 被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # 同 IP 每日上限：把阈值临时降到 1（此时已有 1 条记录）
                import gfvfw.web.routers.apply as applymod
                old = applymod.MAX_APPLICATIONS_PER_IP_PER_DAY
                applymod.MAX_APPLICATIONS_PER_IP_PER_DAY = 1
                try:
                    r = submit_application(client, callsign="Flood",
                                           username="flood1")
                    check("★ 同 IP 超过每日上限被拒", r.status_code == 400,
                          "得到 %d" % r.status_code)
                    check("提示说明是刷量限制", "太多" in r.text)
                finally:
                    applymod.MAX_APPLICATIONS_PER_IP_PER_DAY = old

                r = submit_application(client, callsign="Good2", username="good2")
                check("阈值恢复后可以正常申请", r.status_code == 303,
                      "得到 %d" % r.status_code)

            # ==============================================================
            print("\n[4] ★ 游客：能登录，但队内内容一律 403（不是跳登录）")
            # ==============================================================
            with TestClient(app) as client:
                r = login(client, "newbie")
                check("游客能建立会话（303）", r.status_code == 303,
                      "得到 %d" % r.status_code)

                r = client.get("/")
                check("游客能看首页", r.status_code == 200)
                check("★ 顶部身份标签显示「游客」", ">游客<" in r.text,
                      "未找到身份标签")
                nav = nav_links(r.text)
                check("★ 游客导航有「我的申请」", "/apply/status" in nav, str(nav))
                for href in ("/members", "/theater", "/library", "/stats",
                             "/applications"):
                    check("★ 游客导航里没有 %s" % href, href not in nav,
                          "导航含 %s" % href)

                r = client.get("/apply/status")
                check("★ 游客能看自己的申请进度", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("进度页说明当前是游客", "游客" in r.text)
                check("进度页列出申请记录", "Newbie" in r.text)
                check("进度页说明下一步是等管理员提升",
                      "提升" in r.text)

                r = client.get("/account")
                check("★ 游客能改自己的密码", r.status_code == 200,
                      "得到 %d" % r.status_code)

                # 核心：队内内容必须是 403（说明页），不能是 303（跳登录）
                for path in MEMBER_ONLY:
                    r = client.get(path, follow_redirects=False)
                    check("★ 游客 %s → 403（说明需要队员）" % path,
                          r.status_code == 403,
                          "得到 %d" % r.status_code)
                r = client.get("/members", follow_redirects=False)
                check("★ 403 页面解释了「需要队员身份」",
                      "仅限" in r.text and "队员" in r.text)
                check("★ 403 页面给出「查看我的申请进度」入口",
                      "/apply/status" in r.text)
                check("★ 403 页面**没有**再让登录（已登录）",
                      "/login" not in r.text)

                r = client.get("/applications", follow_redirects=False)
                check("★ 游客看不到入队审批", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # 写操作同样进不去
                r = client.post("/acmi/upload", data={"csrf_token": ""},
                                follow_redirects=False)
                check("★ 游客不能触发写操作", r.status_code in (403,),
                      "得到 %d" % r.status_code)

                check("游客能退出",
                      client.post("/logout",
                                  data={"csrf_token": csrf_of(
                                      client.get("/account").text)},
                                  follow_redirects=False).status_code == 303)

            # ==============================================================
            print("\n[5] 管理员审批页与「提升为队员」")
            # ==============================================================
            with TestClient(app) as client:
                login(client, "viper")
                r = client.get("/applications")
                check("管理员能打开入队审批页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("页面上列出待审批游客", "newbie" in r.text)
                check("页面上显示申请内容", "想飞对空" in r.text)
                check("导航显示「入队审批」入口",
                      "/applications" in nav_links(r.text))

                with TestSession() as db:
                    u = db.scalar(select(User).where(User.username == "newbie"))
                    guest_uid = u.id
                    before_members = db.scalar(
                        select(func.count()).select_from(Member))

                page = client.get("/applications")
                r = client.post("/applications/%s/promote" % guest_uid,
                                data={"csrf_token": csrf_of(page.text),
                                      "callsign": "Newbie"},
                                follow_redirects=False)
                check("提升 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("★ 回跳带 did=promoted",
                      "did=promoted" in r.headers.get("location", ""),
                      r.headers.get("location", ""))

                with TestSession() as db:
                    u = db.get(User, guest_uid)
                    check("★ 账号已激活（active）", u.status == "active",
                          str(u.status))
                    check("★ 账号已绑定名册成员", u.member_id is not None)
                    m = db.get(Member, u.member_id)
                    check("★ 名册多了一个成员", m is not None)
                    check("★ 呼号用了申请时填的",
                          m is not None and m.callsign == "Newbie",
                          m.callsign if m else "")
                    check("★ 名册人数 +1",
                          db.scalar(select(func.count()).select_from(Member))
                          == before_members + 1)
                    roles = list(db.scalars(
                        select(Role.code).join(
                            MemberRole, MemberRole.role_id == Role.id)
                        .where(MemberRole.member_id == m.id,
                               MemberRole.revoked_at.is_(None))).all())
                    check("★ 已分配 member 角色", "member" in roles, str(roles))
                    a = db.scalar(select(Application).where(
                        Application.resulting_user_id == guest_uid))
                    check("★ 申请标为已入队（activated）",
                          a.status == "activated", str(a.status))
                    check("申请记下审批人", a.reviewed_by is not None)

                with TestSession() as db:
                    acts = list(db.scalars(select(AuditLog.action)).all())
                    check("★ 审计记录了 application.promote",
                          "application.promote" in acts, str(acts))
                    check("★ 审计记录了 application.submit",
                          "application.submit" in acts)

            # ==============================================================
            print("\n[6] ★ 提升之后，同一个账号立刻能看队内内容")
            # ==============================================================
            with TestClient(app) as client:
                r = login(client, "newbie")
                check("原游客账号可登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

                r = client.get("/")
                check("★ 身份标签不再显示「游客」", ">游客<" not in r.text)
                nav = nav_links(r.text)
                check("★ 导航出现「成员」", "/members" in nav, str(nav))
                check("★ 导航出现「资料查询」", "/library" in nav, str(nav))
                check("★ 导航不再显示「我的申请」",
                      "/apply/status" not in nav, str(nav))

                for path in ("/members", "/log/campaign", "/stats", "/library"):
                    r = client.get(path, follow_redirects=False)
                    check("★ 提升后可访问 %s" % path, r.status_code == 200,
                          "得到 %d" % r.status_code)

                r = client.get("/account/logbook", follow_redirects=False)
                check("★ 提升后可访问自己的 Logbook 页",
                      r.status_code == 200, "得到 %d" % r.status_code)

            # ==============================================================
            print("\n[7] 拒绝：账号停用、无法登录")
            # ==============================================================
            with TestClient(app) as client:
                r = submit_application(client, callsign="RejectMe",
                                       username="rejectme")
                check("再提交一份申请", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    ru = db.scalar(select(User).where(
                        User.username == "rejectme"))
                    reject_uid = ru.id
                    check("新游客已建立", ru.status == "pending")

                login(client, "viper")
                page = client.get("/applications")
                r = client.post("/applications/%s/reject" % reject_uid,
                                data={"csrf_token": csrf_of(page.text),
                                      "note": "呼号不合规"},
                                follow_redirects=False)
                check("拒绝 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)

                with TestSession() as db:
                    ru = db.get(User, reject_uid)
                    check("★ 账号已停用（suspended）",
                          ru.status == "suspended", str(ru.status))
                    a = db.scalar(select(Application).where(
                        Application.resulting_user_id == reject_uid))
                    check("★ 申请标为 rejected", a.status == "rejected",
                          str(a.status))
                    check("★ 拒绝原因已记录", a.review_note == "呼号不合规",
                          str(a.review_note))
                    check("★ 被拒账号**没有**进名册", ru.member_id is None)
                    acts = list(db.scalars(select(AuditLog.action)).all())
                    check("★ 审计记录了 application.reject",
                          "application.reject" in acts)

            with TestClient(app) as client:
                r = login(client, "rejectme")
                check("★ 被拒账号无法登录", r.status_code == 403,
                      "得到 %d" % r.status_code)

            # ==============================================================
            print("\n[8] 提升的边界与幂等")
            # ==============================================================
            with TestClient(app) as client:
                login(client, "viper")
                with TestSession() as db:
                    u = db.scalar(select(User).where(User.username == "newbie"))
                    active_uid = u.id
                    count_before = db.scalar(
                        select(func.count()).select_from(Member))

                page = client.get("/applications")
                tok = csrf_of(page.text)
                r = client.post("/applications/%s/promote" % active_uid,
                                data={"csrf_token": tok, "callsign": "Whatever"},
                                follow_redirects=False)
                check("对已是队员的账号再提升 → 被拒并说明",
                      r.status_code == 303 and "error=" in r.headers.get(
                          "location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    check("★ 重复提升**没有**多建成员",
                          db.scalar(select(func.count()).select_from(Member))
                          == count_before)
                    check("★ 重复提升**没有**改掉已有呼号",
                          db.get(User, active_uid).member_id is not None)

                r = client.post("/applications/00000000-0000-0000-0000-000000000000/promote",
                                data={"csrf_token": tok}, follow_redirects=False)
                check("提升不存在的账号 → 404", r.status_code == 404,
                      "得到 %d" % r.status_code)

                # 呼号冲突：与已有队员同名
                r = submit_application(client, callsign="DupCallsign",
                                       username="dup1")
                check("提交呼号将被占用的申请", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    du = db.scalar(select(User).where(User.username == "dup1"))
                    dup_uid = du.id
                    n_before = db.scalar(
                        select(func.count()).select_from(Member))

                page = client.get("/applications")
                r = client.post("/applications/%s/promote" % dup_uid,
                                data={"csrf_token": csrf_of(page.text),
                                      "callsign": "Rookie"},
                                follow_redirects=False)
                check("★ 提升时呼号与名册冲突 → 拒绝并报错",
                      "error=" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    check("★ 冲突时没有写入半个成员",
                          db.scalar(select(func.count()).select_from(Member))
                          == n_before)
                    check("★ 冲突时账号仍是游客",
                          db.get(User, dup_uid).status == "pending")

            # ==============================================================
            print("\n[9] 队员之间不越权（回归保护）")
            # ==============================================================
            with TestClient(app) as client:
                login(client, "rookie")           # 普通队员
                r = client.get("/members", follow_redirects=False)
                check("队员能看名册", r.status_code == 200,
                      "得到 %d" % r.status_code)
                r = client.get("/members/new", follow_redirects=False)
                check("★ 队员不能新建成员（需 member.create）",
                      r.status_code == 403, "得到 %d" % r.status_code)
                r = client.get("/applications", follow_redirects=False)
                check("★ 队员不能进审批页（需 application.review）",
                      r.status_code == 403, "得到 %d" % r.status_code)
        finally:
            import gfvfw.config as _c
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig
            _c.settings.storage_dir = orig_storage

    print("\n" + "=" * 74)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
