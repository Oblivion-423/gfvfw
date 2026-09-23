"""排查：谁改的 admin 密码？（只读）

用审计里的 ``user_agent_hash`` 指纹区分客户端 ——
``privacy_hash`` 是确定性的，所以可以对本机已知 UA 重算并比对。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gfvfw.config import settings  # noqa: E402
from gfvfw.security import privacy_hash  # noqa: E402

CANDIDATES = {
    "本项目的实况探针": "gfvfw-live-edit-check/1.0",
    "常见浏览器(Chrome/Win)": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def main() -> int:
    p = pathlib.Path(settings.database_url.replace("sqlite+pysqlite:///", ""))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row

    rows = list(con.execute(
        "SELECT occurred_at, action, user_agent_hash, ip_hash, reason "
        "FROM audit_log WHERE action LIKE 'user.password%' "
        "ORDER BY occurred_at"))

    print("=== 密码相关审计 ===")
    for r in rows:
        print("  %s | %-28s | ua=%s ip=%s"
              % (r["occurred_at"], r["action"],
                 (r["user_agent_hash"] or "-")[:16], (r["ip_hash"] or "-")[:16]))

    print("\n=== 已知客户端 UA 指纹 ===")
    prints = {}
    for label, ua in CANDIDATES.items():
        h = privacy_hash(ua)
        prints[label] = h
        print("  %-24s %s…" % (label, h[:16]))

    print("\n=== 归属判定 ===")
    for r in rows:
        who = "(未知客户端)"
        for label, h in prints.items():
            if r["user_agent_hash"] == h:
                who = label
                break
        print("  %s | %-28s | %s" % (r["occurred_at"], r["action"], who))

    # 所有不同的 UA 指纹（能看到共有几个客户端动过这个账号）
    print("\n=== 该账号上出现过的全部 UA 指纹 ===")
    for r in con.execute(
            "SELECT DISTINCT user_agent_hash, COUNT(*) n FROM audit_log "
            "WHERE actor_user_id = (SELECT id FROM users WHERE username='admin') "
            "GROUP BY user_agent_hash ORDER BY n DESC"):
        ua = r["user_agent_hash"] or "(空)"
        label = next((k for k, v in prints.items() if v == r["user_agent_hash"]),
                     "未知")
        print("  %s…  出现 %d 次   → %s" % (ua[:20], r["n"], label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
