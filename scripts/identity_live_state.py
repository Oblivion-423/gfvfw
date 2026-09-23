"""查看线上库的账号状态分布（只读）——用于确认三档身份迁移没有意外。"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
db = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "var" / "gfvfw.sqlite3")

c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print("users:")
for r in c.execute("select username, status, member_id from users"
                   " order by created_at"):
    print("  %-14s status=%-10s member=%s" % (
        r["username"], r["status"], (r["member_id"] or "—")[:8]))

print("\n按状态统计:")
for r in c.execute("select status, count(*) n from users group by status"):
    print("  %-12s %d" % (r["status"], r["n"]))

print("\nmembers: %d" % c.execute(
    "select count(*) from members where deleted_at is null").fetchone()[0])
print("applications: %d" % c.execute(
    "select count(*) from applications").fetchone()[0])

# 三档身份的判定要一致：pending 的账号不应该绑定名册
bad = list(c.execute(
    "select username, status from users"
    " where status <> 'active' and member_id is not null"))
print("\n⚠️ 非 active 却绑定了名册的账号: %s" % (bad or "无"))
