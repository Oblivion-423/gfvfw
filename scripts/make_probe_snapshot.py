"""为 live_edit_check 造一份带已知管理密码的线上库快照（放在 var/probe/ 下）。

⚠️ 快照走 `gfvfw.sqlite_snapshot.snapshot_sqlite`，与生产备份**同一条路径** ——
   以前这里各写一份 `VACUUM INTO`，结果是"本地探针能跑、线上备份跑不了"
   （服务器 SQLite 3.26 没有 `VACUUM INTO`）。

⚠️ 从 `gfvfw.sqlite_snapshot` 导入而不是 `gfvfw.db`：后者会带出 `gfvfw.config`，
   而 config 在**导入时**就把 settings 绑到线上库上。本脚本反正就是拿线上库
   做快照（源路径显式传入），所以不受影响；但保持一致能避免以后误改。

用法::

    .venv\\Scripts\\python.exe scripts\\make_probe_snapshot.py [明文密码]

⚠️ 密码**只写进快照**（var/probe/ 下，不进 git），线上库的密码一个字节都不动。
   同时会把锁定状态清干净 —— 否则线上库若因连败被锁，快照里也是锁的，
   探针会以一个看起来莫名其妙的 429 失败。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gfvfw.security import hash_password  # noqa: E402
from gfvfw.sqlite_snapshot import snapshot_sqlite  # noqa: E402

out_dir = ROOT / "var" / "probe"
out_dir.mkdir(parents=True, exist_ok=True)
snap = out_dir / "snapshot.sqlite3"
if snap.exists():
    snap.unlink()

snapshot_sqlite(ROOT / "var" / "gfvfw.sqlite3", snap)

pw = sys.argv[1] if len(sys.argv) > 1 else "Probe-PW-2026"
c = sqlite3.connect(str(snap))
try:
    # ⚠️ 连锁定状态一起清掉。线上库若处于锁定/连败状态，快照会继承它，
    #    探针就会以一个看不出原因的 429 失败（真踩过）。
    c.execute("update users set password_hash=?, status='active',"
              " failed_login_count=0, locked_until=NULL"
              " where username='admin'", (hash_password(pw),))
    c.commit()
    rows = c.execute("select username, status from users").fetchall()
finally:
    c.close()

print("snapshot:", snap)
print("users:", rows)
print("password:", pw)
