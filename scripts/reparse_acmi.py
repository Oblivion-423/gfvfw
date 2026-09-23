"""
修正历史 ACMI 记录的时间字段（重解析）。

为什么需要
----------
早期实现的 ``duration_seconds`` 取了文件内**末标记**，而 BMS 的 ACMI 时间戳
以剧本纪元（``ReferenceTime``）为基点、首个标记常常是个大数，于是录制时长被
严重高估（实测一份 4.5 秒的录制被报成 36004.7 秒 ≈ 10 小时）。同时
``recorded_start_at`` 被写成了 t=0 基准点本身，时间窗因此比真实录制更宽。

修复后老行仍然带着错误值，且 ``min_relative_seconds`` 为 NULL（无法从库里
反推 —— 必须重解析原始文件）。原始 ACMI 都在 ``var/storage`` 里，故可重算。

用法
----
    # 先看会改什么（默认只读，不写库）
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py

    # 确认后写入
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py --apply

    # 连已修过的行也一并重算
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py --apply --all
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from gfvfw.acmi_parser import parse_file  # noqa: E402
from gfvfw.config import settings  # noqa: E402
from gfvfw.db import SessionLocal  # noqa: E402
from gfvfw.models import AcmiFile, Mission  # noqa: E402
from gfvfw.services.ingest import AcmiIngestService  # noqa: E402


def resolve_stored(rec: AcmiFile) -> Path | None:
    """``stored_path`` 是**相对** ``storage_dir`` 的路径，不是绝对路径。"""
    if not rec.stored_path:
        return None
    p = Path(rec.stored_path)
    return p if p.is_absolute() else Path(settings.storage_dir) / p


def fmt_h(seconds) -> str:
    if not seconds:
        return "—"
    return "%.2f 小时" % (float(seconds) / 3600.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="重解析 ACMI，修正时长与时间窗")
    ap.add_argument("--apply", action="store_true",
                    help="真正写库；不给则只报告将要做的改动")
    ap.add_argument("--all", action="store_true",
                    help="连 min_relative_seconds 已存在的行也一起重算")
    args = ap.parse_args()

    with SessionLocal() as db:
        stmt = select(AcmiFile)
        if not args.all:
            stmt = stmt.where(AcmiFile.min_relative_seconds.is_(None))
        rows = list(db.scalars(stmt))

        if not rows:
            print("没有需要修正的记录。")
            return 0

        print("=" * 92)
        print("待修正 ACMI 记录 %d 条%s" % (len(rows), "" if args.apply else "（预览，未写库）"))
        print("=" * 92)
        print("%-34s %12s %12s %12s" % ("文件", "原时长", "新时长", "时间窗宽度"))
        print("-" * 92)

        fixed = 0
        touched_missions: set[str] = set()
        for rec in rows:
            path = resolve_stored(rec)
            if path is None or not path.exists():
                print("%-34s 跳过：原件不在 %s" % (rec.original_filename[:34],
                                                  rec.stored_path))
                continue
            try:
                info = parse_file(str(path))
            except Exception as exc:  # noqa: BLE001
                print("%-34s 跳过：重解析失败 %s"
                      % (rec.original_filename[:34], str(exc)[:40]))
                continue

            old_dur = rec.duration_seconds
            new_start = info.recording_start_utc
            new_end = info.recording_end_utc
            width = ((new_end - new_start).total_seconds()
                     if (new_start and new_end) else None)

            print("%-34s %12s %12s %12s"
                  % (rec.original_filename[:34], fmt_h(old_dur),
                     fmt_h(info.duration_seconds),
                     "%.0f 秒" % width if width is not None else "—"))

            if args.apply:
                rec.min_relative_seconds = info.min_relative_seconds
                rec.max_relative_seconds = info.max_relative_seconds
                rec.duration_seconds = info.duration_seconds
                rec.recorded_start_at = new_start
                rec.recorded_end_at = new_end
                if rec.mission_id:
                    touched_missions.add(rec.mission_id)
            fixed += 1

        if args.apply:
            db.flush()
            # 受影响的批次/任务也要重算起点终点与时长
            for fid in sorted(touched_missions):
                mission = db.get(Mission, fid)
                if mission is None:
                    continue
                files = list(db.scalars(
                    select(AcmiFile).where(AcmiFile.mission_id == fid)))
                starts = [f.recorded_start_at for f in files if f.recorded_start_at]
                ends = [f.recorded_end_at for f in files if f.recorded_end_at]
                if starts:
                    mission.started_at = min(starts)
                if ends:
                    mission.ended_at = max(ends)
                AcmiIngestService.recompute_mission(db, mission)
                print("  重算任务 %s：%s → %s（%s）"
                      % (mission.id[:8],
                         mission.started_at, mission.ended_at,
                         fmt_h(mission.duration_seconds)))
            db.commit()
            print("-" * 92)
            print("已修正 %d 条记录，重算 %d 个任务。"
                  % (fixed, len(touched_missions)))
        else:
            print("-" * 92)
            print("预览结束，%d 条将被修正。加 --apply 才会写库。" % fixed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
