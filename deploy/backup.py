#!/usr/bin/env python3
"""
GFVFW 备份：数据库 + 上传目录一起打包（需求 R9）。

为什么不能直接 ``cp`` 数据库文件
--------------------------------
SQLite 跑在 WAL 模式下，直接复制 ``.sqlite3`` 会得到一个**撕裂的快照**：
已经提交但还在 ``-wal`` 里的事务不在主文件里，而 ``-wal`` 又不一定同时被复制。
本脚本用 ``VACUUM INTO`` 取一致性快照（SQLite 3.27+，Python 3.10 自带 ≥3.37），
它在事务中生成一个完整、可直接打开的数据库副本，不需要停服务。

用法
----
    # 立即备份到默认目录（settings.backup_dir）
    .venv/bin/python deploy/backup.py

    # 指定输出目录 + 保留天数
    .venv/bin/python deploy/backup.py --dest /srv/gfvfw/var/backups --keep-days 30

    # 打包前校验快照 + 打包后列出内容
    .venv/bin/python deploy/backup.py --verify

    # 不压缩（上传目录里多是已压过的 zip，压缩几乎无收益却耗 CPU）
    .venv/bin/python deploy/backup.py --no-compress

定时执行（systemd timer 或 cron 均可，见 deploy/DEPLOY.md）。

⚠️ 备份与数据放在同一块盘只能防"误删/写坏"，防不了磁盘故障。
   请按需求 R9 定期把备份**下载到本地**。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gfvfw.config import settings  # noqa: E402


def db_file_path() -> Path | None:
    """从 ``database_url`` 解出 SQLite 文件路径。非 SQLite 返回 None。"""
    if not settings.is_sqlite:
        return None
    raw = settings.database_url.split("///")[-1]
    return Path(raw) if raw and raw != ":memory:" else None


def snapshot_db(src: Path, dest: Path, *, verify: bool) -> int:
    """用 ``VACUUM INTO`` 取一致性快照，返回字节数。

    ⚠️ ``VACUUM INTO`` 要求目标文件**不存在**；已存在会报错（这是好事，
    避免悄悄覆盖上一份备份）。
    """
    if not src.exists():
        raise FileNotFoundError("数据库文件不存在：%s" % src)
    if dest.exists():
        raise FileExistsError("快照目标已存在：%s" % dest)

    # 只读方式打开源库；VACUUM INTO 自己会处理 WAL 里的已提交事务
    con = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()

    if verify:
        # 单独打开快照做完整性检查 —— 备份不能用"文件存在"来证明有效
        chk = sqlite3.connect(dest)
        try:
            row = chk.execute("PRAGMA integrity_check").fetchone()
            status = row[0] if row else "?"
            if status != "ok":
                raise RuntimeError("快照完整性检查未通过：%s" % status)
            # 顺带确认关键表在
            tables = {r[0] for r in chk.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("members", "users", "sorties", "missions"):
                if t not in tables:
                    raise RuntimeError("快照缺少表：%s" % t)
        finally:
            chk.close()
    return dest.stat().st_size


def add_tree(tf: tarfile.TarFile, root: Path, arcname: str) -> tuple[int, int]:
    """把目录树加进归档，返回 (文件数, 字节数)。"""
    if not root.exists():
        return (0, 0)
    n = 0
    total = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        tf.add(p, arcname="%s/%s" % (arcname, p.relative_to(root).as_posix()))
        n += 1
        total += p.stat().st_size
    return (n, total)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % int(n)
        n /= 1024.0
    return "%.1f TB" % n


def prune(dest: Path, keep_days: int) -> list[Path]:
    """删掉超过保留期的备份（只认本脚本自己的命名）。"""
    if keep_days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = []
    for p in sorted(dest.glob("gfvfw-backup-*.tar*")):
        try:
            stamp = p.name.split("gfvfw-backup-")[1].split(".")[0]
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc)
        except (IndexError, ValueError):
            continue          # 不是本脚本产出的文件，不动
        if when < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="GFVFW 备份（数据库 + 上传目录）")
    ap.add_argument("--dest", default=None,
                    help="备份输出目录，默认取 GFVFW_BACKUP_DIR")
    ap.add_argument("--keep-days", type=int, default=None,
                    help="保留天数，默认取 GFVFW_BACKUP_KEEP_DAYS")
    ap.add_argument("--no-compress", action="store_true",
                    help="只打包不压缩（上传目录里多是已压缩的 zip，压缩收益很低）")
    ap.add_argument("--verify", action="store_true",
                    help="对数据库快照做 integrity_check（更慢，更可信）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告将要备份什么，不写任何文件")
    args = ap.parse_args()

    dest = Path(args.dest or settings.backup_dir)
    keep_days = settings.backup_keep_days if args.keep_days is None else args.keep_days
    storage = Path(settings.storage_dir)
    src_db = db_file_path()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = ".tar" if args.no_compress else ".tar.gz"
    out = dest / ("gfvfw-backup-%s%s" % (stamp, suffix))

    print("=" * 70)
    print("GFVFW 备份")
    print("=" * 70)
    print("  数据库      %s%s" % (src_db or "(非 SQLite，跳过)", ""))
    print("  上传目录    %s" % storage)
    print("  输出        %s" % out)
    print("  保留        %d 天" % keep_days)

    if args.dry_run:
        if src_db and src_db.exists():
            print("  数据库大小  %s" % human(src_db.stat().st_size))
        print("\n（--dry-run：未写入任何文件）")
        return 0

    if src_db is None:
        print("\n!! 当前数据库不是 SQLite，本脚本只支持 SQLite 备份", file=sys.stderr)
        return 2

    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gfvfw-backup-") as td:
        snap = Path(td) / "gfvfw.sqlite3"
        size = snapshot_db(src_db, snap, verify=args.verify)
        print("\n  [1/3] 数据库快照 %s%s"
              % (human(size), "（integrity_check 通过）" if args.verify else ""))

        mode = "w" if args.no_compress else "w:gz"
        with tarfile.open(out, mode) as tf:
            tf.add(snap, arcname="gfvfw.sqlite3")
            n_files, n_bytes = add_tree(tf, storage, "storage")
        print("  [2/3] 上传目录 %d 个文件、%s" % (n_files, human(n_bytes)))
        print("  [3/3] 归档 %s" % human(out.stat().st_size))

    removed = prune(dest, keep_days)
    if removed:
        print("\n  清理超期备份 %d 份：" % len(removed))
        for p in removed:
            print("    - %s" % p.name)

    # 列出当前所有备份，便于人一眼看出保留策略是否按预期工作
    rest = sorted(dest.glob("gfvfw-backup-*.tar*"))
    print("\n  当前备份 %d 份：" % len(rest))
    for p in rest[-5:]:
        print("    %s  %s" % (p.name, human(p.stat().st_size)))
    if len(rest) > 5:
        print("    …（只列最近 5 份）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
