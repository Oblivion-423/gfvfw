"""上线前预检（Pre-flight）：在**全新数据库 + 生产式配置**下走一遍首启流程。

它回答的是"照 DEPLOY.md 装到服务器上，能不能起来"这个具体问题，
而不是重跑功能测试（那是 pytest 的事）。检查项：

1. **可移植性**：`check_portability()` 不报错（无 strftime / JSON1 / AUTOINCREMENT…）
2. **建表 + 播种**：空库能建出全部表、角色/军衔/机型播下去
3. **首启**：`create-admin` 等价路径能建出第一个管理员
4. **生产式配置生效**：`GFVFW_HTTPS_ONLY=true` → 会话 Cookie 带 `Secure`
5. **三档身份在空库上也成立**：访客进不去队内、公开申请能提交、
   提升后立刻拿到队内权限（空库没有历史数据可依赖）
6. **备份可用**：`deploy/backup.py` 能在有数据后产出一份可校验的包
7. **静态资源与模板**：所有模板都能编译（缺变量/语法错会在启动后第一次渲染才炸）

用法::

    .venv\\Scripts\\python.exe scripts\\preflight.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
CHECKS = [0]
PW = "Preflight-Admin-2026"
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  " + detail))
    if not cond:
        FAILURES.append("%s %s" % (name, detail))


def main() -> int:
    print("=" * 74)
    print("上线前预检（全新库 + 生产式配置）")
    print("=" * 74)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        db = tdp / "gfvfw.sqlite3"
        storage = tdp / "storage"
        backups = tdp / "backups"
        for d in (storage, backups):
            d.mkdir(parents=True, exist_ok=True)

        # ⚠️ 必须在 import gfvfw.* 之前设好 —— config 在导入时读环境变量
        os.environ["GFVFW_DATABASE_URL"] = "sqlite+pysqlite:///%s" % db.as_posix()
        os.environ["GFVFW_STORAGE_DIR"] = str(storage)
        os.environ["GFVFW_BACKUP_DIR"] = str(backups)
        os.environ["GFVFW_SECRET_KEY"] = "preflight-secret-key-0123456789"
        os.environ["GFVFW_HTTPS_ONLY"] = "true"
        os.environ["GFVFW_TRUSTED_PROXY_IPS"] = "127.0.0.1,::1"
        os.environ["GFVFW_SITE_NAME"] = "矛隼虚拟飞行联队"

        # ---------------------------------------------------------------
        print("\n[1] 可移植性（PORTABLE_TYPES 白名单）")
        # ---------------------------------------------------------------
        from gfvfw.db import check_portability
        problems = check_portability()
        check("★ check_portability() 无告警", not problems, str(problems[:3]))

        print("\n[2] 空库建表 + 播种")
        from gfvfw.db import Base, SessionLocal, engine
        from gfvfw.services.bootstrap import ensure_schema, seed

        ensure_schema(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as s:
            created = seed(s)
        check("★ 播种完成（角色/权限/军衔/机型）", bool(created), str(created))

        import sqlite3
        c = sqlite3.connect(str(db))
        tables = [r[0] for r in c.execute(
            "select name from sqlite_master where type='table'")]
        c.close()
        check("★ 表数量符合预期（含 member_awards / applications）",
              len([t for t in tables if not t.startswith("sqlite_")]) >= 35,
              "只有 %d 张表" % len(tables))
        check("★ member_awards 已建（Logbook 勋章）", "member_awards" in tables)
        check("★ applications 已建（入队申请）", "applications" in tables)

        # ---------------------------------------------------------------
        print("\n[3] 首个管理员（create-admin 的等价路径）")
        # ---------------------------------------------------------------
        from gfvfw.security import hash_password
        from gfvfw.models import Application, Member, MemberRole, Role, User
        from sqlalchemy import func, select
        with SessionLocal() as s:
            m = Member(callsign="Viper", status="active")
            s.add(m)
            s.flush()
            u = User(username="admin",
                     password_hash=hash_password(PW), status="active",
                     member_id=m.id)
            s.add(u)
            s.flush()
            role = s.scalar(select(Role).where(Role.code == "owner"))
            s.add(MemberRole(member_id=m.id, role_id=role.id))
            s.commit()
            check("★ 管理员账号已建立且绑定名册", u.member_id == m.id)
            check("★ owner 角色存在", role is not None)

        # ---------------------------------------------------------------
        print("\n[4] 生产式配置生效（Cookie 必须带 Secure）")
        # ---------------------------------------------------------------
        from fastapi.testclient import TestClient
        from gfvfw.config import settings
        from gfvfw.web.app import create_app

        check("★ GFVFW_HTTPS_ONLY 已读到", settings.https_only is True)
        app = create_app()

        # ⚠️⚠️ 这里必须用 **https** 的 base_url。
        #    GFVFW_HTTPS_ONLY=true 会把会话 Cookie 标成 `Secure`，
        #    于是走 http:// 的客户端**不会把它带回来** —— 表现为
        #    "登录成功但下一个请求又是匿名的"，CSRF 校验随之 403。
        #    这正是生产环境必须上 HTTPS 的原因，也是本地用 http 冒烟
        #    会莫名失败的原因。预检要按生产的真实协议跑。
        with TestClient(app) as http_client:
            r = http_client.get("/login")
            cookies = "; ".join(
                v for k, v in r.headers.items() if k.lower() == "set-cookie")
            check("★ 会话 Cookie 含 Secure 属性",
                  "secure" in cookies.lower(), cookies or "（没有 Set-Cookie）")
            # 反面确认：http 下确实带不回会话（这就是必须 HTTPS 的原因）
            page = http_client.get("/login")
            tok = _CSRF_RE.search(page.text).group(1)
            http_client.post("/login", data={"username": "admin",
                                            "password": PW,
                                            "csrf_token": tok},
                             follow_redirects=False)
            rr = http_client.get("/account", follow_redirects=False)
            check("★ http:// 下会话确实带不回（故生产必须 HTTPS）",
                  rr.status_code == 303, "得到 %d" % rr.status_code)

        with TestClient(app, base_url="https://gfvfw.test") as client:
            # -----------------------------------------------------------
            print("\n[5] 空库上的三档身份（按生产的 https 协议）")
            # -----------------------------------------------------------
            for path in ("/", "/register", "/login"):
                check("访客可访问 %s" % path,
                      client.get(path).status_code == 200)
            # ⚠️ /apply 现在**需要登录**（注册与申请已拆成两步），
            #    所以它出现在"被拦"这一组，不再是公开页。
            for path in ("/apply", "/members", "/library", "/stats",
                         "/applications"):
                rr = client.get(path, follow_redirects=False)
                check("★ 访客 %s 被拦（303）" % path, rr.status_code == 303,
                      "得到 %d" % rr.status_code)

            # 第一步：公开注册 → 游客账号（**不建申请**）
            page = client.get("/register")
            tok = _CSRF_RE.search(page.text).group(1)
            r = client.post("/register", data={
                "username": "newbie", "email": "",
                "password": "newbie-password-1",
                "confirm_password": "newbie-password-1",
                "csrf_token": tok,
            }, follow_redirects=False)
            check("★ 空库上公开注册可提交", r.status_code == 303,
                  "得到 %d" % r.status_code)

            with SessionLocal() as s:
                guest = s.scalar(select(User).where(User.username == "newbie"))
                check("★ 注册即得游客账号（pending）",
                      guest is not None and guest.status == "pending")
                check("★ 注册**不建**入队申请（注册 ≠ 申请）",
                      s.scalar(select(func.count()).select_from(Application)
                               .where(Application.resulting_user_id
                                      == guest.id)) == 0)

            # 第二步：游客提交入队申请（需登录；注册已自动登录）
            page = client.get("/apply")
            check("★ 注册后自动登录，可直接打开入队申请页",
                  page.status_code == 200, "得到 %d" % page.status_code)
            r = client.post("/apply", data={
                "callsign": "Newbie", "experience": "", "intent": "",
                "contact": "", "csrf_token": _CSRF_RE.search(page.text).group(1),
            }, follow_redirects=False)
            check("★ 游客可提交入队申请", r.status_code == 303,
                  "得到 %d" % r.status_code)

            # 游客：列表页全开，详情页 403
            for path in ("/members", "/theater", "/log/campaign",
                         "/log/training", "/log/pilots", "/log",
                         "/stats", "/library"):
                rr = client.get(path, follow_redirects=False)
                check("★ 游客可看列表页 %s" % path, rr.status_code == 200,
                      "得到 %d" % rr.status_code)
            rr = client.get("/members/00000000-0000-0000-0000-000000000000",
                            follow_redirects=False)
            check("★ 游客详情页 → 403（而不是跳登录）",
                  rr.status_code == 403, "得到 %d" % rr.status_code)
            check("★ 403 说明了需要队员身份", "队员" in rr.text)
            check("★ 403 说明游客能看列表与汇总", "列表与汇总" in rr.text)

        # 提升为队员
        with TestClient(app, base_url="https://gfvfw.test") as client:
            page = client.get("/login")
            client.post("/login", data={"username": "admin", "password": PW,
                                       "csrf_token": _CSRF_RE.search(
                                           page.text).group(1)},
                        follow_redirects=False)
            with SessionLocal() as s:
                guest = s.scalar(select(User).where(User.username == "newbie"))
                guest_id = guest.id
            page = client.get("/applications")
            check("★ 管理员能打开入队审批页", page.status_code == 200,
                  "得到 %d" % page.status_code)
            r = client.post("/applications/%s/promote" % guest_id,
                            data={"csrf_token": _CSRF_RE.search(
                                page.text).group(1), "callsign": "Newbie"},
                            follow_redirects=False)
            check("★ 提升为队员成功", r.status_code == 303,
                  "得到 %d" % r.status_code)

        with TestClient(app, base_url="https://gfvfw.test") as client:
            page = client.get("/login")
            client.post("/login", data={"username": "newbie",
                                       "password": "newbie-password-1",
                                       "csrf_token": _CSRF_RE.search(
                                           page.text).group(1)},
                        follow_redirects=False)
            for path in ("/members", "/library", "/stats", "/log/campaign"):
                rr = client.get(path, follow_redirects=False)
                check("★ 提升后 %s 可访问" % path, rr.status_code == 200,
                      "得到 %d" % rr.status_code)
            # 详情页此时应当**放行**（404 = 守卫过了但数据不存在）
            rr = client.get("/campaigns/00000000-0000-0000-0000-000000000000",
                            follow_redirects=False)
            check("★ 提升后详情页守卫放行（404 而非 403）",
                  rr.status_code == 404, "得到 %d" % rr.status_code)

        # ---------------------------------------------------------------
        print("\n[6] 备份可用（VACUUM INTO + 打包）")
        # ---------------------------------------------------------------
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, str(ROOT / "deploy" / "backup.py"), "--verify",
             "--dest", str(backups)],
            capture_output=True, text=True, env=env, cwd=str(ROOT),
            # ⚠️ Windows 默认按 GBK 解码子进程输出，而子进程打的是中文 UTF-8，
            #    会抛 UnicodeDecodeError（在读取线程里，表现为一段莫名其妙的堆栈）。
            encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        check("★ backup.py 成功退出（含完整性校验）", r.returncode == 0,
              out[-400:])
        made = list(backups.glob("gfvfw-backup-*.tar.gz"))
        check("★ 备份包已产出", bool(made), str(out[-200:]))
        if made:
            check("★ 备份包非空", made[0].stat().st_size > 1000,
                  "%d 字节" % made[0].stat().st_size)

        # ---------------------------------------------------------------
        print("\n[7] 模板全部可编译")
        # ---------------------------------------------------------------
        from gfvfw.web.templating import templates
        bad: list[str] = []
        count = 0
        for path in sorted((ROOT / "gfvfw" / "web" / "templates").rglob("*.html")):
            count += 1
            try:
                templates.env.get_template(
                    str(path.relative_to(ROOT / "gfvfw" / "web" / "templates"))
                    .replace("\\", "/"))
            except Exception as exc:                          # noqa: BLE001
                bad.append("%s: %s" % (path.name, exc))
        check("★ 全部模板可编译（%d 个）" % count, not bad, "; ".join(bad[:3]))

    print("\n" + "=" * 74)
    print("预检断言 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
