"""
Web 层自校验：骨架、认证、权限、成员名册 CRUD。

⚠️ 关键设计：测试用**独立的 SQLite 文件库**并覆盖 ``gfvfw.db.SessionLocal``，
   而不是内存库 —— 因为中间件通过 ``SessionLocal`` 取会话，
   必须让应用与被测代码共用同一个引擎，否则出现"测试写了但应用读不到"。

运行:
    .venv\\Scripts\\python.exe tests\\web_selfcheck.py
"""
from __future__ import annotations

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
from gfvfw.models import Member, MemberRole, Role, User  # noqa: E402
from gfvfw.permissions import (  # noqa: E402
    MEMBER_CREATE, MEMBER_EDIT_RANK, MEMBER_VIEW, ROLE_DEFINITIONS,
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


def build_app(tmpdir: Path):
    """建立使用独立文件库的应用实例。"""
    db_path = tmpdir / "test.sqlite3"
    engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # ⚠️ 覆盖应用的会话工厂，让中间件与测试共用同一引擎。
    #    注意：`gfvfw.web` 包把 `app` 名字导出为 FastAPI 实例，
    #    因此 `import gfvfw.web.app as appmod` 拿到的是实例而非模块，
    #    必须从 sys.modules 取真正的模块对象。
    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]

    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = TestSession
    depsmod.SessionLocal = TestSession
    appmod.SessionLocal = TestSession

    with TestSession() as db:
        seed(db)

    app = appmod.create_app()
    return app, TestSession, orig


def make_user(db, TestSession, callsign: str, role_code: str,
              username: str | None = None, status: str = "active") -> tuple[str, str]:
    """建成员 + 账号 + 角色，返回 (member_id, username)。"""
    username = username or callsign.lower()
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    db.add(User(username=username, password_hash=hash_password("password123"),
                status=status, member_id=member.id))
    role = db.scalar(select(Role).where(Role.code == role_code))
    assert role is not None, "角色未播种: %s" % role_code
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, username


def login(client: TestClient, username: str, password: str = "password123") -> bool:
    page = client.get("/login")
    token = csrf_of(page.text)
    r = client.post("/login", data={"username": username, "password": password,
                                    "csrf_token": token},
                    follow_redirects=False)
    return r.status_code == 303


def test_schema_drift_is_repaired() -> None:
    """⚠️ 回归测试：已有数据库缺列时，启动必须自动补列。

    背景：``Base.metadata.create_all()`` **只建缺失的表，不给已存在的表加列**。
    开发中给模型加字段后，测试用全新建库一切正常，
    而**已有的真实数据库**会直接 ``no such column`` 导致 500。

    本测试模拟该场景：先建一个"旧结构"的库（人为删掉某列），
    再跑 ``ensure_schema``，验证列被补回且不丢数据。
    """
    print("\n[11] schema 漂移自愈（回归测试：create_all 不会加列）")
    import sqlite3
    from gfvfw.services.bootstrap import ensure_schema

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "drift.sqlite3"
        engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix())

        # 1) 建一个"旧结构"：先建全表，再删掉目标列，并塞一行数据
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE acmi_files RENAME TO acmi_files_old")
            conn.exec_driver_sql("""
                CREATE TABLE acmi_files (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    sha256 VARCHAR(64) NOT NULL,
                    original_filename VARCHAR(255) NOT NULL,
                    stored_path VARCHAR(512) NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    parse_status VARCHAR(16) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )""")
            conn.exec_driver_sql("DROP TABLE acmi_files_old")
            conn.exec_driver_sql("""
                INSERT INTO acmi_files
                    (id, sha256, original_filename, stored_path, size_bytes,
                     parse_status, created_at, updated_at)
                VALUES ('x','deadbeef','old.zip.acmi','acmi/x',123,'parsed',
                        '2026-01-01 00:00:00','2026-01-01 00:00:00')""")

        # 2) 确认列确实缺失
        with engine.connect() as conn:
            cols = {r[1] for r in conn.exec_driver_sql(
                "PRAGMA table_info(acmi_files)").fetchall()}
        check("模拟旧库：sortie_summaries_json 缺失",
              "sortie_summaries_json" not in cols)

        # 3) 启动时同步
        applied = ensure_schema(engine)
        check("自动补齐了缺失列", len(applied) >= 1, "得到 %s" % applied)

        with engine.connect() as conn:
            cols = {r[1] for r in conn.exec_driver_sql(
                "PRAGMA table_info(acmi_files)").fetchall()}
            rows = conn.exec_driver_sql(
                "SELECT id, sha256, sortie_summaries_json FROM acmi_files").fetchall()
        check("列已补回", "sortie_summaries_json" in cols)
        check("原有数据未丢失", len(rows) == 1 and rows[0][1] == "deadbeef",
              "得到 %s" % rows)

        # 4) 幂等：再跑一次不应有任何变更
        again = ensure_schema(engine)
        check("重复同步无变更（幂等）", again == [], "得到 %s" % again)
        engine.dispose()


def test_deployment_security() -> None:
    """上线相关的两处安全行为（都是曾经写错的地方）。"""
    print("\n[12] 部署安全：会话 Cookie 的 Secure 与 X-Forwarded-For 信任")
    import gfvfw.config as cfg
    from gfvfw.services.audit import _client_ip

    # ---- X-Forwarded-For 只在直连对端是可信代理时才采信 ----
    class _Peer:
        def __init__(self, host: str) -> None:
            self.host = host

    class _Req:
        def __init__(self, peer: str | None, xff: str | None) -> None:
            self.client = _Peer(peer) if peer else None
            self.headers = {"x-forwarded-for": xff} if xff else {}

    saved = cfg.settings.trusted_proxy_ips
    try:
        cfg.settings.trusted_proxy_ips = "127.0.0.1,::1"

        check("可信代理转发 → 取 XFF",
              _client_ip(_Req("127.0.0.1", "203.0.113.9")) == "203.0.113.9")
        check("可信代理 + 多段 → 取第一段",
              _client_ip(_Req("127.0.0.1", "203.0.113.9, 10.0.0.1"))
              == "203.0.113.9")
        # ★ 关键：不可信来源伪造的 XFF 必须被忽略
        check("★ 不可信来源伪造 XFF → 忽略，用真实对端",
              _client_ip(_Req("198.51.100.7", "1.2.3.4")) == "198.51.100.7",
              "得到 %r" % _client_ip(_Req("198.51.100.7", "1.2.3.4")))
        check("无可信代理且无 XFF → 用对端",
              _client_ip(_Req("198.51.100.7", None)) == "198.51.100.7")
        check("无 client 时返回 None", _client_ip(None) is None)
        check("不可信来源且无 XFF → 用对端",
              _client_ip(_Req("203.0.113.1", None)) == "203.0.113.1")
    finally:
        cfg.settings.trusted_proxy_ips = saved

    # ---- 会话 Cookie 的 Secure 属性由 GFVFW_HTTPS_ONLY 控制 ----
    import tempfile as _tf
    saved_https = cfg.settings.https_only
    try:
        for flag, expect_secure in ((True, True), (False, False)):
            cfg.settings.https_only = flag
            with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                app, TestSession = _build_plain_app(Path(td))
                with TestClient(app) as client:
                    # GET /login 会写 csrf 进会话 → 必然回一个 Set-Cookie
                    page = client.get("/login")
                    check("https_only=%s 登录页可渲染" % flag,
                          page.status_code == 200, "得到 %d" % page.status_code)
                    blob = " ".join(page.headers.get_list("set-cookie"))
                    check("会话 Cookie 存在", "gfvfw_session" in blob, blob[:110])
                    has = "secure" in blob.lower()
                    check("https_only=%s → Set-Cookie %s Secure"
                          % (flag, "含" if expect_secure else "不含"),
                          has == expect_secure, blob[:110])
    finally:
        cfg.settings.https_only = saved_https


def _build_plain_app(tmpdir: Path):
    """建一个最小可用的 app（只用于观察 Set-Cookie）。

    ⚠️ 必须播种 —— 不播种的话 ``ranks`` 等基础表是空的，
    登录页会直接 500，那样拿到的响应根本没有 Set-Cookie，
    断言会"通过"在错误的前提上。
    """
    from gfvfw.services.bootstrap import seed

    db_path = tmpdir / "https.sqlite3"
    engine = create_engine("sqlite+pysqlite:///%s" % db_path.as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession


def main() -> int:
    print("=" * 70)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig = build_app(tdp)
        try:
            with TestSession() as db:
                admin_mid, admin_user = make_user(db, TestSession, "Oblivion", "owner")
                member_mid, member_user = make_user(db, TestSession, "Rookie", "member")
                cmd_mid, cmd_user = make_user(db, TestSession, "Chief", "commander")

            print("\n[1] 骨架与匿名访问")
            with TestClient(app) as client:
                r = client.get("/")
                check("首页可访问（匿名）", r.status_code == 200, "得到 %d" % r.status_code)
                check("首页显示站名", "矛隼虚拟飞行联队" in r.text)
                check("首页空状态提示存在", "不导入历史文件" in r.text)
                check("静态样式可访问", client.get("/static/app.css").status_code == 200)

                r = client.get("/login")
                check("登录页可访问", r.status_code == 200)
                check("登录页含 CSRF 令牌", bool(csrf_of(r.text)))

                print("\n[2] 匿名访问受限页面 → 跳登录")
                r = client.get("/members", follow_redirects=False)
                check("名册需登录（303 → /login）",
                      r.status_code == 303 and "/login" in r.headers.get("location", ""),
                      "得到 %d %s" % (r.status_code, r.headers.get("location")))
                check("跳转带回 next 参数",
                      "next=/members" in r.headers.get("location", ""),
                      "得到 %s" % r.headers.get("location"))

            print("\n[3] 登录：错误密码 / 正确密码")
            with TestClient(app) as client:
                page = client.get("/login")
                token = csrf_of(page.text)
                r = client.post("/login", data={"username": admin_user,
                                               "password": "wrong",
                                               "csrf_token": token})
                check("错误密码被拒", r.status_code == 401, "得到 %d" % r.status_code)
                check("错误提示不泄露用户是否存在",
                      "用户名或密码不正确" in r.text)

                check("正确密码可登录", login(client, admin_user))
                r = client.get("/")
                check("登录后顶栏显示呼号", "Oblivion" in r.text)

            print("\n[4] CSRF 防护")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.post("/members/new", data={
                    "callsign": "NoCsrf", "status": "active", "visibility": "public",
                    "csrf_token": "bogus",
                })
                check("伪造 CSRF 令牌被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            print("\n[5] 成员名册 CRUD（管理员）")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/members")
                check("名册页可访问", r.status_code == 200)
                check("名册含已播种成员", "Oblivion" in r.text and "Rookie" in r.text)

                # 名册搜索只按呼号（队号已从界面移除，不再参与匹配）
                r = client.get("/members?q=Obliv")
                check("按呼号片段能搜到", r.status_code == 200 and "Oblivion" in r.text)
                check("搜索命中时排除不匹配者", "Rookie" not in r.text,
                      "队号已不参与搜索，Rookie 不该被 Obliv 命中")

                r = client.get("/members/new")
                token = csrf_of(r.text)
                check("新建表单含军衔下拉", "少尉" in r.text and "准将" in r.text)
                check("新建表单含全部状态",
                      all(s in r.text for s in ("现役", "休整", "退役", "预备")))
                # 联队口径：表单不要示例型占位提示，也不要队号字段
                check("新建表单无占位提示语", "placeholder=" not in r.text)
                check("新建表单无队号字段", "队号" not in r.text)

                # ⚠️ 表单已无队号，但即使有人手工 POST 该字段也必须被忽略
                r = client.post("/members/new", data={
                    "callsign": "Viper", "service_number": "GF-002",
                    "status": "active", "rank_id": "", "joined_at": "2026-01-15",
                    "visibility": "public", "bio": "测试成员", "csrf_token": token,
                }, follow_redirects=False)
                check("创建成员成功（303）", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    created = db.scalar(select(Member).where(Member.callsign == "Viper"))
                    check("成员已入库", created is not None)
                    check("POST 里的队号被忽略", created is not None
                          and created.service_number is None,
                          "得到 %r" % (created.service_number if created else None))
                    check("入队日期已保存", created is not None
                          and created.joined_at.strftime("%Y-%m-%d") == "2026-01-15")
                    new_id = created.id if created else ""
                    # 直接往库里塞一个历史队号，用于验证"编辑不会清空它"
                    created.service_number = "GF-LEGACY"
                    db.commit()

                r = client.get("/members/%s" % new_id)
                check("详情页可访问", r.status_code == 200)
                check("详情页显示呼号", "Viper" in r.text)
                check("详情页显示空架次提示", "暂无架次记录" in r.text)

                # 重名拒绝
                r = client.get("/members/new")
                token = csrf_of(r.text)
                r = client.post("/members/new", data={
                    "callsign": "Viper", "status": "active", "visibility": "public",
                    "csrf_token": token,
                })
                check("重复呼号被拒", r.status_code == 400, "得到 %d" % r.status_code)
                check("重复呼号给出提示", "已被使用" in r.text)

                # 编辑
                r = client.get("/members/%s/edit" % new_id)
                token = csrf_of(r.text)
                r = client.post("/members/%s/edit" % new_id, data={
                    "callsign": "Viper", "service_number": "GF-002",
                    "status": "reserve", "rank_id": "", "joined_at": "2026-01-15",
                    "left_at": "", "visibility": "members", "bio": "改过了",
                    "csrf_token": token,
                }, follow_redirects=False)
                check("编辑成员成功", r.status_code == 303, "得到 %d" % r.status_code)
                with TestSession() as db:
                    m = db.get(Member, new_id)
                    check("状态已更新", m.status == "reserve", "得到 %s" % m.status)
                    check("可见性已更新", m.visibility == "members")
                    # ★ 回归防线：编辑表单没有队号字段，绝不能因此把历史值清空。
                    #   曾经的写法是 `member.service_number = service_number.strip() or None`，
                    #   表单移除该字段后每次编辑都会静默清空它。
                    check("编辑不会清空历史队号",
                          m.service_number == "GF-LEGACY",
                          "得到 %r" % m.service_number)

            print("\n[6] 权限分级")
            with TestClient(app) as client:
                login(client, member_user)
                r = client.get("/members")
                check("普通成员可查看名册", r.status_code == 200)
                check("普通成员看不到新增按钮", "新增成员" not in r.text)

                r = client.get("/members/new", follow_redirects=False)
                check("普通成员无新建权限（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                check("403 页面说明所需权限", "member.create" in r.text)

            with TestClient(app) as client:
                login(client, cmd_user)
                r = client.get("/members/new")
                check("联队指挥可新建成员", r.status_code == 200)

            print("\n[7] 未激活账号只读（游客档）")
            with TestSession() as db:
                pend_mid, pend_user = make_user(db, TestSession, "Pending",
                                               "member", status="pending")
            with TestClient(app) as client:
                page = client.get("/login")
                token = csrf_of(page.text)
                r = client.post("/login", data={"username": pend_user,
                                               "password": "password123",
                                               "csrf_token": token},
                                follow_redirects=False)
                check("待审批账号可登录（有提示）", r.status_code == 303,
                      "得到 %d" % r.status_code)
                r = client.get("/members", follow_redirects=False)
                # ⚠️ 这里曾经断言 303（跳登录）。但游客**已经登录**，
                #    重定向会造成「点→回登录→再点」的死循环，用户看不出
                #    自己差的是"被提升为队员"这一步。现在改为 403 说明页。
                check("★ 待审批账号访问名册 → 403（说明需要队员身份）",
                      r.status_code == 403, "得到 %d" % r.status_code)
                check("★ 403 页面给出申请进度入口", "/apply/status" in r.text)
                r = client.get("/")
                check("★ 待审批账号首页显示「游客」身份", "游客" in r.text)

            print("\n[8] 软删除（R11）")
            with TestClient(app) as client:
                login(client, admin_user)
                r = client.get("/members/%s" % new_id)
                token = csrf_of(r.text)
                r = client.post("/members/%s/delete" % new_id,
                                data={"csrf_token": token},
                                follow_redirects=False)
                check("删除返回 303", r.status_code == 303, "得到 %d" % r.status_code)
                with TestSession() as db:
                    m = db.get(Member, new_id)
                    check("记录仍存在（软删除）", m is not None)
                    check("deleted_at 已设置", m is not None and m.deleted_at is not None)
                r = client.get("/members")
                check("软删除后不在列表中", ">Viper<" not in r.text)

            print("\n[9] 审计日志（R11）")
            with TestSession() as db:
                from gfvfw.models import AuditLog
                logs = list(db.scalars(select(AuditLog)))
                check("产生了审计记录", len(logs) >= 2, "得到 %d" % len(logs))
                actions = {l.action for l in logs}
                check("记录了 create 与 delete",
                      {"create", "delete"} <= actions, "得到 %s" % actions)
                check("审计不含明文 IP 字段",
                      all((l.ip_hash is None) or len(l.ip_hash) == 64 for l in logs))

            print("\n[10] 登录锁定")
            with TestSession() as db:
                lock_mid, lock_user = make_user(db, TestSession, "Locky", "member")
            with TestClient(app) as client:
                for i in range(5):
                    page = client.get("/login")
                    client.post("/login", data={"username": lock_user,
                                                "password": "bad%d" % i,
                                                "csrf_token": csrf_of(page.text)})
                page = client.get("/login")
                r = client.post("/login", data={"username": lock_user,
                                               "password": "password123",
                                               "csrf_token": csrf_of(page.text)})
                check("连续失败后账号被锁定", r.status_code == 429,
                      "得到 %d" % r.status_code)
        finally:
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig

    test_schema_drift_is_repaired()
    test_deployment_security()

    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
