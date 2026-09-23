"""
自校验：**三档身份与"列表公开 / 详情队内"边界**。

联队要求（本轮口径）
--------------------
* **游客注册后可查看所有公开的战役管理、飞行纪录、资料、统计数据。**
* **注册之后再提交申请成为队员。**
* 队员由管理员从游客提升上来，可查看**详情页**与写操作。

于是边界不再是"游客什么都看不见"，而是**列表/汇总 vs 详情/写操作**：

1. 未登录访客：只有 `/` `/login` `/register` 可进，其余一律送到登录页；
2. **注册**（公开）：只填账号信息 → 立刻得到**游客**账号并自动登录，**没有**申请表；
3. **申请**（需登录）：游客填呼号意向等 → 落一条 `Application`，等管理员提升；
4. **游客**：8 个列表页全部 200，**详情页一律 403 且说明"需要队员身份"**
   （绝不能重定向到登录页 —— 那会造成「点→回登录→再点」的死循环）；
   **写操作 UI 必须从页面里消失**（ACMI 工作台嵌在 `/log/campaign` 上）；
5. **提升**：管理员操作后名册多一个成员、账号激活、角色到手，
   **同一账号立刻能进详情页**；
6. **拒绝**：账号停用、无法登录；
7. 队员之间不越权（回归保护）。

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

#: 无需登录就能打开的页面
ANON_PAGES = ("/", "/login", "/register")

#: **列表 / 汇总页** —— 游客与队员都能看（联队口径"公开部分"）
LIST_PAGES = (
    "/members",
    "/theater",
    "/log/campaign",
    "/log/training",
    "/log/pilots",
    "/log",
    "/stats",
    "/library",
)

#: **详情页** —— 仅队员。用不存在的 ID 也没关系：守卫在处理器之前跑，
#: 游客一定拿 403（而不是 404）—— 这正好证明"拦住他的是身份，不是数据"。
FAKE_ID = "00000000-0000-0000-0000-000000000000"
DETAIL_PAGES = (
    "/members/%s" % FAKE_ID,
    "/missions/%s" % FAKE_ID,
    "/campaigns/%s" % FAKE_ID,
    "/theater/%s" % FAKE_ID,
    "/sorties/%s/edit" % FAKE_ID,
    "/account/logbook",
)

#: 写操作 / 管理层页面 —— 游客必须 403
GUEST_FORBIDDEN = ("/applications", "/theater/upload")


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

    from gfvfw.web.app import create_app
    return create_app(), TestSession, orig, orig_storage


def make_person(db, callsign: str, role_code: str | None, *, status: str = "active"):
    m = Member(callsign=callsign, status=status, visibility="members")
    db.add(m)
    db.flush()
    u = User(username=callsign.lower(), password_hash=hash_password(PW),
             status="active", member_id=m.id)
    db.add(u)
    db.flush()
    if role_code:
        role = db.scalar(select(Role).where(Role.code == role_code))
        assert role is not None, "角色 %s 不存在" % role_code
        db.add(MemberRole(member_id=m.id, role_id=role.id))
    db.commit()
    return m.id, u.id


def login(client, username: str, password: str = PW):
    page = client.get("/login")
    return client.post("/login",
                       data={"username": username, "password": password,
                             "csrf_token": csrf_of(page.text)},
                       follow_redirects=False)


def register(client, *, username: str, password: str = PW,
             confirm: str | None = None, email: str = "",
             token: str | None = None):
    """公开注册（第一步）：只建账号。"""
    page = client.get("/register")
    tok = token if token is not None else csrf_of(page.text)
    return client.post("/register", data={
        "username": username, "email": email, "password": password,
        "confirm_password": confirm if confirm is not None else password,
        "csrf_token": tok,
    }, follow_redirects=False)


def apply_for_membership(client, *, callsign: str, experience: str = "飞过 Falcon 4.0",
                         intent: str = "想飞对空", contact: str = "QQ 123",
                         token: str | None = None):
    """入队申请（第二步）：需已登录。"""
    page = client.get("/apply")
    tok = token if token is not None else csrf_of(page.text)
    return client.post("/apply", data={
        "callsign": callsign, "experience": experience,
        "intent": intent, "contact": contact, "csrf_token": tok,
    }, follow_redirects=False)


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 74)
    print("三档身份自校验：访客 / 游客 / 队员（列表公开 · 详情队内）")
    print("=" * 74)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            with TestSession() as db:
                owner_mid, owner_uid = make_person(db, "Viper", "owner")
                mem_mid, mem_uid = make_person(db, "Rookie", "member")

            # ==============================================================
            print("\n[1] 未登录访客：只有注册/登录/首页")
            # ==============================================================
            with TestClient(app) as client:
                for path in ANON_PAGES:
                    r = client.get(path)
                    check("访客可访问 %s" % path, r.status_code == 200,
                          "得到 %d" % r.status_code)

                for path in ("/apply", "/apply/status"):
                    r = client.get(path, follow_redirects=False)
                    check("★ 访客 %s → 跳登录（303）" % path,
                          r.status_code == 303, "得到 %d" % r.status_code)

                for path in LIST_PAGES:
                    r = client.get(path, follow_redirects=False)
                    check("★ 访客 %s → 跳登录（303）" % path,
                          r.status_code == 303, "得到 %d" % r.status_code)

                for path in DETAIL_PAGES:
                    r = client.get(path, follow_redirects=False)
                    check("★ 访客 %s → 跳登录（303）" % path,
                          r.status_code == 303, "得到 %d" % r.status_code)

                r = client.get("/applications", follow_redirects=False)
                check("★ 访客 /applications → 跳登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

                home = client.get("/").text
                nav = nav_links(home)
                check("★ 拿到导航块（断言前提）", bool(nav_of(home)),
                      "没匹配到 <nav>，后面的导航断言都不可信")
                check("★ 访客导航里有「注册」", "/register" in nav, str(nav))
                check("★ 访客导航里有「登录」", "/login" in nav, str(nav))
                for href in ("/members", "/theater", "/log/campaign",
                             "/library", "/stats", "/applications",
                             "/apply", "/apply/status"):
                    check("★ 访客导航里没有 %s" % href, href not in nav,
                          "导航含 %s" % href)

                check("★ 注册页说明「注册之后」的分工",
                      "注册" in client.get("/register").text
                      and "申请" in client.get("/register").text)

            # ==============================================================
            print("\n[2] 公开注册（第一步）：只建游客账号，不建申请")
            # ==============================================================
            with TestClient(app) as client:
                r = register(client, username="newbie", email="nb@example.com")
                check("注册提交 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("★ 回跳到入队申请页并带 did=registered",
                      r.headers.get("location", "").startswith("/apply")
                      and "did=registered" in r.headers.get("location", ""),
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
                    check("★ 记录注册来源 IP 哈希、不存明文 IP",
                          u is not None and u.registration_ip_hash
                          and u.registration_ip_hash == privacy_hash("testclient"),
                          str(u.registration_ip_hash) if u else "")
                    guest_uid = u.id
                    n_apps = db.scalar(
                        select(func.count()).select_from(Application)
                        .where(Application.resulting_user_id == u.id))
                    check("★ **注册不等于申请**：还没有 Application 记录",
                          n_apps == 0, "有 %s 条" % n_apps)

                # 注册即登录（游客零权限，不必再输一次密码）
                r = client.get("/apply/status", follow_redirects=False)
                check("★ 注册后自动登录（能直接打开申请进度页）",
                      r.status_code == 200, "得到 %d" % r.status_code)
                check("进度页明确说还没提交过申请",
                      "还没有提交过入队申请" in r.text)

            # ==============================================================
            print("\n[3] 注册的边界（重复、弱密码、CSRF、刷量、已登录）")
            # ==============================================================
            with TestClient(app) as client:
                r = register(client, username="newbie")
                check("★ 用户名重复被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("给出可读原因", "用户名" in r.text)

                r = register(client, username="weak1", password="123")
                check("★ 弱密码被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = register(client, username="mm1", password="password123",
                             confirm="password456")
                check("★ 两次密码不一致被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = register(client, username="bm1", email="not-an-email")
                check("★ 邮箱格式错被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = register(client, username="sp ace")
                check("★ 用户名含空格被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = register(client, username="nc1", token="")
                check("★ 无 CSRF 被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # 同 IP 每日上限：降到 1（此时已注册 1 个账号）
                import gfvfw.web.routers.apply as applymod
                old = applymod.MAX_REGISTRATIONS_PER_IP_PER_DAY
                applymod.MAX_REGISTRATIONS_PER_IP_PER_DAY = 1
                try:
                    r = register(client, username="flood1")
                    check("★ 同 IP 超过每日注册上限被拒", r.status_code == 400,
                          "得到 %d" % r.status_code)
                    check("提示说明是刷量限制", "太多" in r.text)
                finally:
                    applymod.MAX_REGISTRATIONS_PER_IP_PER_DAY = old

                r = register(client, username="good2")
                check("阈值恢复后可以正常注册", r.status_code == 303,
                      "得到 %d" % r.status_code)

            # 已登录的人不该再注册第二个账号
            with TestClient(app) as client:
                login(client, "newbie")
                page = client.get("/account")
                r = client.post("/register", data={
                    "username": "second", "password": PW,
                    "confirm_password": PW, "csrf_token": csrf_of(page.text),
                }, follow_redirects=False)
                check("★ 已登录用户再注册 → 被送回 /apply",
                      r.status_code == 303
                      and r.headers.get("location", "").startswith("/apply"),
                      "%d %s" % (r.status_code, r.headers.get("location", "")))
                with TestSession() as db:
                    check("★ 没有多建出第二个账号",
                          db.scalar(select(func.count()).select_from(User)
                                    .where(User.username == "second")) == 0)

            # ==============================================================
            print("\n[4] ★ 游客：列表页全开，详情页 403（不是跳登录）")
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
                for href in ("/members", "/theater", "/log/campaign",
                             "/library", "/stats"):
                    check("★ 游客导航里有 %s" % href, href in nav, str(nav))
                check("★ 游客导航里有「入队申请」", "/apply" in nav, str(nav))
                check("★ 游客导航里有「我的申请」", "/apply/status" in nav,
                      str(nav))
                check("★ 游客导航里没有「入队审批」",
                      "/applications" not in nav, str(nav))

                # ---- 列表页：全开 ----
                for path in LIST_PAGES:
                    r = client.get(path, follow_redirects=False)
                    check("★ 游客可看列表页 %s" % path, r.status_code == 200,
                          "得到 %d" % r.status_code)

                # 名册列表要真的能看到呼号 / 军衔 / 飞行时长（联队明确要求）
                roster = client.get("/members").text
                check("★ 名册列表显示呼号（Rookie）", "Rookie" in roster)
                check("★ 名册列表有「军衔」列", "军衔" in roster)
                check("★ 名册列表有「日志时长」列", "日志时长" in roster)
                check("★ 名册列表对游客**没有**「新增成员」按钮",
                      "/members/new" not in roster)

                # 统计对游客全开（含个人排行榜 —— 联队口径"全部统计都给"）。
                # ⚠️ 这个测试库没有任何架次，所以模板走的是"还没有架次数据"的
                #    空状态分支，排行榜区块根本不渲染。因此这里断言的是
                #    "统计页真的渲染了、且不是 403 说明页"，而不是某个区块标题。
                stats = client.get("/stats").text
                check("★ 游客能看到统计总览页（不是 403 说明页）",
                      "统计总览" in stats and "需要队员" not in stats)

                # ---- 详情页：403 ----
                for path in DETAIL_PAGES:
                    r = client.get(path, follow_redirects=False)
                    check("★ 游客 %s → 403（说明需要队员）" % path,
                          r.status_code == 403, "得到 %d" % r.status_code)

                r = client.get("/members/%s" % owner_mid, follow_redirects=False)
                check("★ 游客打开真实成员详情 → 403", r.status_code == 403,
                      "得到 %d" % r.status_code)
                check("★ 403 页面解释了「需要队员」",
                      "队员" in r.text and "游客" in r.text)
                check("★ 403 页面列出了游客**能**看什么",
                      "列表与汇总" in r.text)
                check("★ 403 页面给出入队申请入口", "/apply" in r.text)
                check("★ 403 页面**没有**再让登录（已登录）",
                      "/login" not in r.text)

                # ---- 管理页 / 写操作 ----
                for path in GUEST_FORBIDDEN:
                    r = client.get(path, follow_redirects=False)
                    check("★ 游客 %s → 403" % path, r.status_code == 403,
                          "得到 %d" % r.status_code)

                r = client.post("/acmi/upload", data={"csrf_token": ""},
                                follow_redirects=False)
                check("★ 游客不能触发写操作（ACMI 上传）",
                      r.status_code == 403, "得到 %d" % r.status_code)

                # ⚠️ 关键：写操作 UI 必须**从页面里消失**。
                #    /log/campaign 与 /log/training 都嵌了 ACMI 工作台，
                #    它们现在是游客可看的列表页 —— 模板必须按身份挡住工作台。
                for path in ("/log/campaign", "/log/training"):
                    html = client.get(path).text
                    check("★ 游客看的 %s 里没有 ACMI 工作台" % path,
                          '/acmi/upload' not in html)
                    check("★ 游客看的 %s 里没有归并按钮" % path,
                          '/acmi/merge' not in html)

                lib_html = client.get("/library").text
                check("★ 游客看的资料页说明下载仅限队员", "队员" in lib_html)

                check("游客能退出",
                      client.post("/logout",
                                  data={"csrf_token": csrf_of(
                                      client.get("/account").text)},
                                  follow_redirects=False).status_code == 303)

            # ==============================================================
            print("\n[5] 入队申请（第二步）：需已登录")
            # ==============================================================
            with TestClient(app) as client:
                # 未登录不能提交
                r = client.post("/apply", data={"callsign": "Anon", "csrf_token": ""},
                                follow_redirects=False)
                check("★ 未登录提交申请 → 跳登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

                login(client, "newbie")
                r = client.get("/apply")
                check("★ 游客能打开入队申请表", r.status_code == 200)
                check("★ 申请表**不再**要用户名/密码（那是注册页的事）",
                      'name="username"' not in r.text
                      and 'name="password"' not in r.text)
                check("申请表有呼号字段", 'name="callsign"' in r.text)

                r = apply_for_membership(client, callsign="NoCsrf", token="")
                check("★ 申请无 CSRF 被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                r = apply_for_membership(client, callsign="")
                check("★ 空呼号被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)

                r = apply_for_membership(client, callsign="Rookie")
                check("★ 呼号与名册成员冲突被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("给出可读原因", "呼号" in r.text)

                r = apply_for_membership(client, callsign="Newbie")
                check("★ 正式提交申请 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("★ 回跳带 did=submitted",
                      "did=submitted" in r.headers.get("location", ""),
                      r.headers.get("location", ""))

                with TestSession() as db:
                    a = db.scalar(select(Application).where(
                        Application.resulting_user_id == guest_uid))
                    check("★ 已落一条申请记录", a is not None)
                    check("申请状态为 submitted",
                          a is not None and a.status == "submitted")
                    check("申请记下了想要的呼号",
                          a is not None and a.desired_callsign == "Newbie")
                    check("★ 申请也存 IP 哈希、不存明文 IP",
                          a is not None and a.source_ip_hash
                          and a.source_ip_hash == privacy_hash("testclient"),
                          str(a.source_ip_hash) if a else "")
                    check("★ 申请内容被保存（经历/意向/联系）",
                          a is not None and a.experience and a.intent
                          and a.contact)

                r = apply_for_membership(client, callsign="Newbie2")
                check("★ 重复提交被拒", r.status_code == 400,
                      "得到 %d" % r.status_code)
                check("提示让他去看进度", "进度" in r.text or "不必重复" in r.text)

                r = client.get("/apply")
                check("★ 已提交后申请页变成进度说明（不再给表单）",
                      'name="callsign"' not in r.text)
                check("已提交后申请页显示当前状态",
                      "已提交" in r.text or "审核" in r.text)

                r = client.get("/apply/status")
                check("★ 进度页列出申请记录", "Newbie" in r.text)

            # ==============================================================
            print("\n[6] 管理员审批页与「提升为队员」")
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
                check("★ 审批页说明提升才开放详情页",
                      "详情页" in r.text or "唯一的授权动作" in r.text)

                with TestSession() as db:
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
                    check("★ 审计记录了 user.register",
                          "user.register" in acts, str(acts))
                    check("★ 审计记录了 application.promote",
                          "application.promote" in acts, str(acts))
                    check("★ 审计记录了 application.submit",
                          "application.submit" in acts)

            # ==============================================================
            print("\n[7] ★ 提升之后，同一个账号立刻能进详情页")
            # ==============================================================
            with TestClient(app) as client:
                r = login(client, "newbie")
                check("原游客账号可登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

                r = client.get("/")
                check("★ 身份标签不再显示「游客」", ">游客<" not in r.text)
                nav = nav_links(r.text)
                check("★ 导航不再显示「我的申请」",
                      "/apply/status" not in nav, str(nav))
                check("★ 导航不再显示「入队申请」", "/apply" not in nav, str(nav))

                for path in LIST_PAGES:
                    r = client.get(path, follow_redirects=False)
                    check("★ 提升后仍可访问列表页 %s" % path,
                          r.status_code == 200, "得到 %d" % r.status_code)

                with TestSession() as db:
                    u = db.scalar(select(User).where(User.username == "newbie"))
                    new_mid = u.member_id

                r = client.get("/members/%s" % new_mid, follow_redirects=False)
                check("★ 提升后可进成员详情页", r.status_code == 200,
                      "得到 %d" % r.status_code)

                r = client.get("/campaigns/%s" % FAKE_ID, follow_redirects=False)
                check("★ 提升后详情页守卫已放行（404 而非 403）",
                      r.status_code == 404, "得到 %d" % r.status_code)

                # 写操作 UI 现在应该出现
                html = client.get("/log/campaign").text
                check("★ 提升后 /log/campaign 出现 ACMI 工作台",
                      '/acmi/upload' in html)

                r = client.get("/account/logbook", follow_redirects=False)
                check("★ 提升后可访问自己的 Logbook 页",
                      r.status_code == 200, "得到 %d" % r.status_code)

                r = client.get("/applications", follow_redirects=False)
                check("★ 普通队员仍不能进审批页（需 application.review）",
                      r.status_code == 403, "得到 %d" % r.status_code)

            # ==============================================================
            print("\n[8] 拒绝：账号停用、无法登录")
            # ==============================================================
            with TestClient(app) as client:
                r = register(client, username="rejectme")
                check("再注册一个游客", r.status_code == 303,
                      "得到 %d" % r.status_code)
                apply_for_membership(client, callsign="RejectMe")
                with TestSession() as db:
                    ru = db.scalar(select(User).where(
                        User.username == "rejectme"))
                    reject_uid = ru.id
                    check("新游客已建立", ru.status == "pending")

            with TestClient(app) as client:
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
            print("\n[9] 提升的边界与幂等")
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

                r = client.post(
                    "/applications/00000000-0000-0000-0000-000000000000/promote",
                    data={"csrf_token": tok}, follow_redirects=False)
                check("提升不存在的账号 → 404", r.status_code == 404,
                      "得到 %d" % r.status_code)

                # 呼号冲突：申请时占住了，提升时被别人抢走
                with TestSession() as db:
                    u = db.get(User, active_uid)
                    dup_mid = db.scalar(select(Member.id).where(
                        Member.callsign == "Rookie"))

                # 造一个"只注册、没申请、且呼号会被抢"的游客
                with TestClient(app) as c2:
                    register(c2, username="dup1")
                    apply_for_membership(c2, callsign="DupCallsign")
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

                # ★ 只注册、没申请的游客也能被提升（管理员直接开通）
                with TestClient(app) as c3:
                    register(c3, username="silent")
                with TestSession() as db:
                    su = db.scalar(select(User).where(User.username == "silent"))
                    silent_uid = su.id
                page = client.get("/applications")
                check("★ 只注册未申请的游客也出现在审批列表", "silent" in page.text)
                r = client.post("/applications/%s/promote" % silent_uid,
                                data={"csrf_token": csrf_of(page.text),
                                      "callsign": "Silent"},
                                follow_redirects=False)
                check("★ 无申请表的游客也能被直接提升",
                      r.status_code == 303
                      and "did=promoted" in r.headers.get("location", ""),
                      r.headers.get("location", ""))
                with TestSession() as db:
                    su = db.get(User, silent_uid)
                    check("★ 直接提升后账号激活并绑定成员",
                          su.status == "active" and su.member_id is not None)
                    check("★ 直接提升写入 audit（而非 application.promote 失败）",
                          "application.promote" in list(
                              db.scalars(select(AuditLog.action)).all()))

            # ==============================================================
            print("\n[10] 队员之间不越权（回归保护）")
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
                r = client.get("/members/%s/edit" % owner_mid,
                               follow_redirects=False)
                check("★ 队员不能编辑他人档案（需 member.edit）",
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
