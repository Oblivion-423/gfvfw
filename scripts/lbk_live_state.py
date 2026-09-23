"""查看线上库里的 Logbook 相关状态（只读）。"""
import sqlite3
import sys

c = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "var/gfvfw.sqlite3")
c.row_factory = sqlite3.Row

tables = {r[0] for r in c.execute(
    "select name from sqlite_master where type='table'")}

n = c.execute("select count(*) from logbook_files").fetchone()[0]
print("logbook_files: %d" % n)
for r in c.execute("select * from logbook_files"):
    print("  id=%s member=%s file=%s size=%s sha=%s" % (
        r["id"][:8], r["member_id"][:8], r["original_filename"],
        r["size_bytes"], (r["sha256"] or "")[:16]))
    print("    parser_version=%s parse_error=%s parsed_at=%s confirmed_at=%s" % (
        r["parser_version"], r["parse_error"], r["parsed_at"], r["confirmed_at"]))

print("\nmembers:")
for r in c.execute("select id, callsign, rank_id, logbook_hours_seconds,"
                   " logbook_sorties, logbook_updated_at, logbook_updated_by"
                   " from members order by callsign"):
    if r["deleted_at"] if "deleted_at" in r.keys() else False:
        continue
    print("  %-12s rank=%s hours=%s sorties=%s updated=%s" % (
        r["callsign"], (r["rank_id"] or "")[:8], r["logbook_hours_seconds"],
        r["logbook_sorties"], r["logbook_updated_at"]))

if "member_awards" in tables:
    rows = list(c.execute("select member_id, code, name, level, source"
                          " from member_awards order by member_id, code"))
    print("\nmember_awards: %d" % len(rows))
    for r in rows:
        print("  member=%s %-20s %-28s level=%s src=%s" % (
            r["member_id"][:8], r["code"], r["name"], r["level"], r["source"]))
else:
    print("\nmember_awards: 表不存在（需要 schema_sync 建表）")

print("\nranks:")
for r in c.execute("select level, name, name_en from ranks order by level"):
    print("  %d %s / %s" % (r["level"], r["name"], r["name_en"]))
