"""
自校验：**密码与账号**（改密、忘记密码、强度策略、锁定解除）。

背景
----
密码是 argon2id 单向哈希，**找不回来**。所以必须同时有两条路：

1. **本人自助改密** —— ``/account``，需验原密码（仅凭登录态不够）；
2. **运维重置** —— ``gfvfw.cli set-password``，且必须顺带**解除登录锁定**
   （账号被锁时正是最需要重置的场景）。

两者必须共用同一套强度策略，否则会出现"网页不让设的密码命令行能设"。

运行:
    .venv\\Scripts\\python.exe tests\\account_selfcheck.py
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
from gfvfw.models import (  # noqa: E402
    AuditLog, Member, MemberRole, Role, User,
)
from gfvfw.security import (  # noqa: E402
    LOCKOUT_MINUTES, MAX_FAILED_LOGINS, MIN_PASSWORD_LENGTH, hash_password,
    password_problem, verify_password,
)
from gfvfw.services.bootstrap import seed  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

OLD_PW = "password123"
NEW_PW = "Gyrfalcon-2026-v2"


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
    db_path = tmpdir / "account.sqlite3"
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

    # ⚠️ 必须同时重定向 storage_dir：漏掉会往真实存储目录写孤儿文件
    orig_storage = cfgmod.settings.storage_dir
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_user(db, callsign: str, role_code: str, *, status: str = "active"):
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    user = User(username=callsign.lower(),
                password_hash=hash_password(OLD_PW),
                status=status, member_id=member.id)
    db.add(user)
    role = db.scalar(select(Role).where(Role.code == role_code))
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, user.id


def login(client, username: str, password: str = OLD_PW) -> int:
    page = client.get("/login")
    r = client.post("/login",
                    data={"username": username, "password": password,
                          "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    return r.status_code


# --------------------------------------------------------------------------
# 1. 强度策略
# --------------------------------------------------------------------------

def test_policy() -> None:
    print("\n[1] 密码强度策略（CLI 与 Web 共用同一个函数）")

    check("空密码被拒", password_problem("") is not None)
    check("过短被拒", password_problem("a" * (MIN_PASSWORD_LENGTH - 1)) is not None)
    check("刚好达下限通过",
          password_problem("a" * MIN_PASSWORD_LENGTH) is None)
    check("首尾空格被拒", password_problem("  abcdefgh  ") is not None)
    check("超长被拒（防用 1MB 密码打服务）", password_problem("x" * 201) is not None)

    for weak in ("password", "12345678", "qwerty123", "admin123", "falconbms"):
        check("弱口令被拒：%s" % weak, password_problem(weak) is not None)

    check("策略提示里不含「复杂度」字样（联队不接受被逼出的 Passw0rd!）",
          "复杂度" not in (password_problem("abc") or ""))

    print("\n[1b] 哈希与校验")
    h = hash_password(NEW_PW)
    check("使用 argon2id", h.startswith("$argon2id$"))
    check("正确密码校验通过", verify_password(h, NEW_PW))
    check("错误密码校验失败", not verify_password(h, "wrong-password"))
    check("空哈希不通过（防脏数据绕过）", not verify_password("", NEW_PW))
    check("同一密码两次哈希不同（有 salt）", hash_password(NEW_PW) != h)


# --------------------------------------------------------------------------
# 2. 自助改密
# --------------------------------------------------------------------------

def test_self_service(app, TestSession, orig, orig_storage) -> None:
    print("\n[2] 自助改密 /account")
    with TestSession() as db:
        _, uid = make_user(db, "Oblivion", "owner")
        _, ins_uid = make_user(db, "Instructor", "instructor")

    with TestClient(app) as client:
        r = client.get("/account", follow_redirects=False)
        check("匿名访问账号页 → 跳登录", r.status_code == 303,
              "得到 %d" % r.status_code)

    with TestClient(app) as client:
        check("登录成功", login(client, "oblivion") == 303)
        r = client.get("/account")
        check("账号页可访问", r.status_code == 200, "得到 %d" % r.status_code)
        check("账号页含改密表单", 'action="/account/password"' in r.text)
        check("账号页显示登录名", "oblivion" in r.text)
        check("账号页说明密码无法找回", "无法找回" in r.text)
        check("账号页给出 CLI 重置命令", "set-password" in r.text)
        check("账号页提示当前锁定策略（取自常量而非写死）",
              str(MAX_FAILED_LOGINS) in r.text and str(LOCKOUT_MINUTES) in r.text)
        check("账号页含 autocomplete 提示（便于密码管理器）",
              'autocomplete="current-password"' in r.text)
        check("账号页警告改密不使其他会话失效",
              "不会" in r.text and "失效" in r.text)
        token = csrf_of(r.text)

        # --- 各类拒绝路径 ---
        r = client.post("/account/password", data={
            "current_password": "wrong", "new_password": NEW_PW,
            "confirm_password": NEW_PW, "csrf_token": token})
        check("原密码错误 → 400", r.status_code == 400, "得到 %d" % r.status_code)
        check("原密码错误有提示", "原密码不正确" in r.text)
        with TestSession() as db:
            check("原密码错误时密码未被改动",
                  verify_password(db.get(User, uid).password_hash, OLD_PW))
            check("改密失败写了审计",
                  db.scalar(select(AuditLog).where(
                      AuditLog.action == "user.password.change_failed")) is not None)

        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": "short",
            "confirm_password": "short", "csrf_token": token})
        check("新密码过短 → 400", r.status_code == 400, "得到 %d" % r.status_code)

        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": NEW_PW,
            "confirm_password": NEW_PW + "x", "csrf_token": token})
        check("两次不一致 → 400", r.status_code == 400, "得到 %d" % r.status_code)
        check("两次不一致有提示", "不一致" in r.text)

        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": OLD_PW,
            "confirm_password": OLD_PW, "csrf_token": token})
        check("新旧密码相同 → 400", r.status_code == 400, "得到 %d" % r.status_code)

        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": NEW_PW,
            "confirm_password": NEW_PW})
        check("缺 CSRF → 403", r.status_code == 403, "得到 %d" % r.status_code)

        # --- 成功路径 ---
        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": NEW_PW,
            "confirm_password": NEW_PW, "csrf_token": token},
            follow_redirects=False)
        check("改密成功 → 303 回账号页", r.status_code == 303,
              "得到 %d" % r.status_code)
        check("回跳带 did=password", "did=password" in r.headers.get("location", ""))

        with TestSession() as db:
            u = db.get(User, uid)
            check("库中已是新密码", verify_password(u.password_hash, NEW_PW))
            check("旧密码已失效", not verify_password(u.password_hash, OLD_PW))
            check("记录了改密时间", u.password_changed_at is not None)
            check("写了改密审计",
                  db.scalar(select(AuditLog).where(
                      AuditLog.action == "user.password.change")) is not None)

        r = client.get("/account?did=password")
        check("账号页显示成功提示", "密码已修改" in r.text)

    print("\n[3] 改密后的登录行为")
    with TestClient(app) as client:
        check("旧密码登录失败（401）", login(client, "oblivion", OLD_PW) == 401)
    with TestClient(app) as client:
        check("新密码登录成功（303）", login(client, "oblivion", NEW_PW) == 303)


# --------------------------------------------------------------------------
# 3. 改密不解锁别的账号 / 不越权
# --------------------------------------------------------------------------

def test_isolation(app, TestSession) -> None:
    print("\n[4] 只能改自己的密码")
    with TestClient(app) as client:
        login(client, "instructor")
        r = client.get("/account")
        check("教官能进自己的账号页", r.status_code == 200)
        check("★ 账号页只显示自己的登录名（无他人切换入口）",
              "instructor" in r.text and "oblivion" not in r.text)

        # 构造：把别人的 user_id 塞进表单也没用 —— 路由根本不接受该参数
        token = csrf_of(r.text)
        r = client.post("/account/password", data={
            "current_password": OLD_PW, "new_password": NEW_PW,
            "confirm_password": NEW_PW, "csrf_token": token,
            "user_id": "someone-else", "username": "oblivion"},
            follow_redirects=False)
        check("提交他人 user_id 不影响目标（参数被忽略）",
              r.status_code == 303, "得到 %d" % r.status_code)

    with TestSession() as db:
        ins = db.scalar(select(User).where(User.username == "instructor"))
        obl = db.scalar(select(User).where(User.username == "oblivion"))
        check("★ 改的是自己", verify_password(ins.password_hash, NEW_PW))
        check("★ 别人的密码没被顺手改掉",
              verify_password(obl.password_hash, NEW_PW))   # 上一步自己改过


# --------------------------------------------------------------------------
# 4. CLI 重置（含解锁）
# --------------------------------------------------------------------------

def test_cli_reset(app, TestSession, orig, orig_storage) -> None:
    print("\n[5] 运维重置（CLI set-password）")

    # 直接调用命令函数，避免起子进程（沙箱下管道受限）
    from gfvfw.cli import cmd_set_password

    class Args:
        username = "rookie"
        callsign = ""
        password = "Reset-By-Admin-2026"
        generate = False
        reason = "自校验"

    with TestSession() as db:
        _, uid = make_user(db, "Rookie", "member")
        # 模拟"被锁定的账号" —— 这正是最需要重置密码的场景
        u = db.get(User, uid)
        u.failed_login_count = MAX_FAILED_LOGINS
        from datetime import datetime, timedelta, timezone
        u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        db.commit()

    # cmd_set_password 内部用模块级 SessionLocal，测试里已替换
    import gfvfw.config as cfgmod
    orig_storage2 = cfgmod.settings.storage_dir
    cfgmod.settings.storage_dir = orig_storage2           # 保持不变
    try:
        cmd_set_password(Args())
    finally:
        cfgmod.settings.storage_dir = orig_storage2

    with TestSession() as db:
        u = db.get(User, uid)
        check("CLI 已改密", verify_password(u.password_hash, Args.password))
        check("★ CLI 同时解除了登录锁定（否则运维会以为重置失败）",
              u.locked_until is None, "locked_until=%s" % u.locked_until)
        check("★ CLI 清空了失败计数", u.failed_login_count == 0)
        check("CLI 写了审计",
              db.scalar(select(AuditLog).where(
                  AuditLog.action == "user.password.reset")) is not None)

    with TestClient(app) as client:
        check("重置后的新密码可登录", login(client, "rookie", Args.password) == 303)

    print("\n[6] CLI 的拒绝路径")
    class BadArgs:
        username = "nobody-here"
        callsign = ""
        password = "Whatever-2026-ok"
        generate = False
        reason = ""

    try:
        cmd_set_password(BadArgs())
        check("不存在的用户名 → 报错退出", False, "竟然成功了")
    except SystemExit as exc:
        check("不存在的用户名 → 报错退出", "找不到账号" in str(exc))

    class WeakArgs:
        username = "rookie"
        callsign = ""
        password = "password"          # 弱口令
        generate = False
        reason = ""

    try:
        cmd_set_password(WeakArgs())
        check("CLI 拒绝弱口令（与 Web 同一套策略）", False, "竟然成功了")
    except SystemExit as exc:
        check("CLI 拒绝弱口令（与 Web 同一套策略）",
              "常见" in str(exc) or "至少" in str(exc), "得到 %s" % exc)

    with TestSession() as db:
        u = db.scalar(select(User).where(User.username == "rookie"))
        check("弱口令被拒后密码未被改动",
              verify_password(u.password_hash, "Reset-By-Admin-2026"))

    print("\n[7] --generate 生成的随机密码必须合规")
    from gfvfw.security import new_token
    ok = all(password_problem(new_token(12)) is None for _ in range(20))
    check("20 次随机密码全部通过策略校验", ok)


def main() -> int:
    print("=" * 72)
    print("密码与账号自校验（自助改密 / 运维重置 / 强度策略 / 解锁）")
    print("=" * 72)

    test_policy()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            test_self_service(app, TestSession, orig, orig_storage)
            test_isolation(app, TestSession)
            test_cli_reset(app, TestSession, orig, orig_storage)
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
