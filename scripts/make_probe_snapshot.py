"""为 live_edit_check 造一份带已知管理密码的线上库快照（放在 var/probe/ 下）。"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gfvfw.security import hash_password  # noqa: E402

out_dir = ROOT / "var" / "probe"
out_dir.mkdir(parents=True, exist_ok=True)
snap = out_dir / "snapshot.sqlite3"
if snap.exists():
    snap.unlink()

src = sqlite3.connect(str(ROOT / "var" / "gfvfw.sqlite3"))
try:
    src.execute("VACUUM INTO ?", (str(snap),))
finally:
    src.close()

pw = sys.argv[1] if len(sys.argv) > 1 else "Probe-PW-2026"
c = sqlite3.connect(str(snap))
try:
    c.execute("update users set password_hash=?, status='active'"
              " where username='admin'", (hash_password(pw),))
    c.commit()
    rows = c.execute("select username, status from users").fetchall()
finally:
    c.close()

print("snapshot:", snap)
print("users:", rows)
print("password:", pw)
