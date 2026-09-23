"""端到端校验：**真实线上数据**下的 Logbook 自动解析页面。

与 ``tests/logbook_selfcheck.py`` 的区别
--------------------------------------
自检用合成数据、临时库；本探针用**线上库的快照 + 线上归档的原件**，
验证"真实文件 → 真实名册 → 真实页面"这条链路真的通了。

做法（不碰线上库）：
1. ``VACUUM INTO`` 出一份快照（直接复制 .sqlite3 会漏掉 WAL，见 DEPLOY.md §12）；
2. 在**快照**里把管理员密码设成已知值（线上密码由用户自己管，探针不知道）；
3. 用 ``TestClient`` 在进程内渲染页面，断言页面内容。

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_live_e2e_probe.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ⚠️⚠️ 只从 gfvfw.sqlite_snapshot 导入 —— 它**刻意不导入 gfvfw.config**。
#    这里绝不能写 `from gfvfw.db import snapshot_sqlite`：
#    gfvfw.db 会带出 config，而 config 在**导入时**就把 settings 绑到
#    （尚未被环境变量改写的）**线上库**上，于是这个探针会去读写真实数据。
#    真发生过：这样改过一次，探针的登录尝试打到线上库，
#    把真实管理员账号连败 5 次锁掉了。
from gfvfw.sqlite_snapshot import assert_isolated_snapshot, snapshot_sqlite  # noqa: E402

LIVE_DB = ROOT / "var" / "gfvfw.sqlite3"
LIVE_STORAGE = ROOT / "var" / "storage"
PROBE_PW = "Probe-Only-Password-2026"

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else " " + detail))
    if not cond:
        FAILURES.append("%s %s" % (name, detail))


def snapshot(dest: Path) -> None:
    """一致性快照 —— 与生产备份走同一条路径（见 gfvfw/db.py::snapshot_sqlite）。

    ⚠️ 以前这里自己写 ``VACUUM INTO``，于是"本地探针能跑"证明不了线上备份能用
       （服务器 SQLite 3.26 没有这个语法）。
    """
    if dest.exists():
        dest.unlink()
    snapshot_sqlite(LIVE_DB, dest)


def main() -> int:
    if not LIVE_DB.exists():
        print("找不到线上库：%s" % LIVE_DB)
        return 2

    print("=" * 74)
    print("Logbook 自动解析端到端探针（线上数据快照）")
    print("=" * 74)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        snap = Path(td) / "snapshot.sqlite3"
        snapshot(snap)

        # ⚠️ 必须在 import gfvfw.config **之前**设好环境变量，
        #    否则 settings 会读到线上库。
        os.environ["GFVFW_DATABASE_URL"] = "sqlite+pysqlite:///%s" % snap.as_posix()
        os.environ["GFVFW_STORAGE_DIR"] = str(LIVE_STORAGE)
        os.environ["GFVFW_SECRET_KEY"] = "probe-only-secret-key"

        from fastapi.testclient import TestClient

        from gfvfw.security import hash_password
        from gfvfw.web.app import create_app

        # ★ 安全闸：确认配置真的指向快照而不是线上库。
        #   必须在任何写操作/登录之前调用 —— 见 gfvfw/sqlite_snapshot.py。
        assert_isolated_snapshot(snap)

        # 在快照里放一个已知密码的管理员
        c = sqlite3.connect(str(snap))
        try:
            c.execute("update users set password_hash=?, status='active'"
                      " where username='admin'", (hash_password(PROBE_PW),))
            c.commit()
            admin = c.execute("select id, username, member_id from users"
                              " where username='admin'").fetchone()
        finally:
            c.close()
        check("快照里有 admin 账号", admin is not None)

        # 找出持有 Logbook 归档的成员
        c = sqlite3.connect(str(snap))
        c.row_factory = sqlite3.Row
        try:
            rec = c.execute("select * from logbook_files"
                            " order by created_at desc limit 1").fetchone()
            check("线上库有 Logbook 归档", rec is not None)
            if rec is None:
                return 1
            member = c.execute("select * from members where id=?",
                               (rec["member_id"],)).fetchone()
            awards = list(c.execute(
                "select code, level from member_awards where member_id=?",
                (rec["member_id"],)))
        finally:
            c.close()

        callsign = member["callsign"]
        print("\n  目标成员：%s   归档：%s" % (callsign, rec["original_filename"]))
        print("  库内累计=%s 秒  架次=%s  勋章=%d 枚"
              % (member["logbook_hours_seconds"], member["logbook_sorties"],
                 len(awards)))

        print("\n[1] 归档记录本身已被解析")
        check("★ parser_version 已写入", bool(rec["parser_version"]),
              str(rec["parser_version"]))
        check("★ parsed_at 已写入", bool(rec["parsed_at"]))
        check("★ parsed_json 已写入", bool(rec["parsed_json"]))
        check("★ 没有 parse_error", not rec["parse_error"],
              str(rec["parse_error"]))

        print("\n[2] 名册已按文件自动填入")
        check("★ 累计飞行时长已填入",
              (member["logbook_hours_seconds"] or 0) > 0,
              str(member["logbook_hours_seconds"]))
        check("★ 累计架次已填入", (member["logbook_sorties"] or 0) > 0,
              str(member["logbook_sorties"]))
        check("★ 军衔已填入", bool(member["rank_id"]), str(member["rank_id"]))
        check("★ 勋章已写入", len(awards) > 0, str(awards))

        app = create_app()
        with TestClient(app) as client:
            page = client.get("/login")
            tok = _CSRF_RE.search(page.text)
            r = client.post("/login",
                            data={"username": "admin", "password": PROBE_PW,
                                  "csrf_token": tok.group(1) if tok else ""},
                            follow_redirects=False)
            check("管理员登录成功", r.status_code == 303,
                  "status=%d" % r.status_code)

            print("\n[3] 成员 Logbook 页（真实数据渲染）")
            r = client.get("/members/%s/logbook" % member["id"])
            html = r.text
            check("页面 200", r.status_code == 200, "status=%d" % r.status_code)

            hours = (member["logbook_hours_seconds"] or 0) / 3600.0
            check("★ 页面显示文件名", rec["original_filename"] in html)
            check("★ 页面显示解析出的飞行小时（%.2f）" % hours,
                  ("%.2f" % hours) in html, "期望 %.2f" % hours)
            check("★ 页面显示解析出的架次（%s）" % member["logbook_sorties"],
                  str(member["logbook_sorties"]) in html)
            check("★ 页面显示解析器版本", ("v%s" % rec["parser_version"]) in html)
            check("★ 显示数据来源于哪份归档", "数据来自归档" in html)
            check("★ 页面展示勋章", "勋章" in html)
            check("★ 未确认的数值折叠且标注含义未确认",
                  "含义未确认" in html and "<details" in html)
            check("★ 页面没有手填输入框",
                  'name="hours"' not in html and 'name="sorties"' not in html)
            check("★ 页面说明上传即自动解析", "上传即自动解析" in html)
            check("★ 页面说明不需要审核", "不需要审核" in html)
            check("★ 三个时长分别标注",
                  all(k in html for k in ("Logbook 累计时长", "日志时长", "记录时长")))

            print("\n[4] 名册页面（军衔/时长是否随之显示）")
            r = client.get("/members/%s" % member["id"])
            check("成员详情页 200", r.status_code == 200,
                  "status=%d" % r.status_code)
            check("★ 成员详情页显示 logbook 时长", "%.2f" % hours in r.text
                  or "Logbook" in r.text)
            r = client.get("/members")
            check("名册列表 200", r.status_code == 200,
                  "status=%d" % r.status_code)
            check("★ 名册列表出现该成员", callsign in r.text)

            print("\n[5] 名册总览/统计页不炸")
            for path in ("/", "/stats", "/log/campaign", "/log/training"):
                r = client.get(path, follow_redirects=False)
                check("GET %s 可渲染" % path, r.status_code in (200, 303),
                      "status=%d" % r.status_code)

    print("\n" + "=" * 74)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
