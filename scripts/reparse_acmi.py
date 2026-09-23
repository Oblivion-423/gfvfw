"""
修正历史 ACMI 记录的时间字段（重解析）。

为什么需要
----------
**问题一：录制时长被严重高估。**
早期实现的 ``duration_seconds`` 取了文件内**末标记**，而 BMS 的 ACMI 时间戳
以剧本纪元（``ReferenceTime``）为基点、首个标记常常是个大数，于是录制时长被
严重高估（实测一份 4.5 秒的录制被报成 36004.7 秒 ≈ 10 小时）。同时
``recorded_start_at`` 被写成了 t=0 基准点本身，时间窗因此比真实录制更宽。

**问题二：架次时间写的是"录制窗"，不是各人自己的飞行区间。**
早期把 ``recorded_start_at`` / ``recorded_end_at`` 直接写进
``sorties.takeoff_at`` / ``landing_at``，于是**同一文件里四个飞行员的起降时刻完全相同**，
"日志时长"因此退化成了"记录时长"（实测 1 小时 13 分 vs 各人约 1 小时 6 分）。
摘要 JSON 里也没有在空区间字段，无法从库内反推。

修复后老行仍带着错误值（``min_relative_seconds`` 为 NULL 也无法反推），
必须重解析原始文件。原始 ACMI 都在 ``var/storage`` 里，故可重算。

用法
----
    # 先看会改什么（默认只读，不写库）
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py

    # 确认后写入
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py --apply

    # 连已修过的行也一并重算（**本次时间语义变更必须加 --all**，
    # 因为老行的 min_relative_seconds 已经不为 NULL 了）
    .venv\\Scripts\\python.exe scripts\\reparse_acmi.py --apply --all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from gfvfw.acmi_parser import parse_file  # noqa: E402
from gfvfw.config import settings  # noqa: E402
from gfvfw.db import SessionLocal  # noqa: E402
from gfvfw.models import AcmiFile, Mission  # noqa: E402
from gfvfw.services.ingest import AcmiIngestService  # noqa: E402
from gfvfw.services.stats import mission_flight_seconds, union_seconds  # noqa: E402


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


def backfill_sorties(db, rec, info) -> int:
    """用重解析结果修正该文件的架次时间。

    修两件事：

    1. ``sortie_summaries_json`` **补上在空区间字段** ——
       早期版本没存 ``airborne_start_s`` / ``airborne_end_s``，
       于是确认入库时只能退回用**录制时间窗**当起降时刻。
    2. ``sorties.takeoff_at`` / ``landing_at`` 改成**该飞行员自己的**
       在空起点/终点。原先同一文件里所有人的起降时刻完全相同
       （都等于录制窗），使"日志时长"退化成了"记录时长"。

    返回修好的架次数。
    """
    from sqlalchemy import select as _select

    from gfvfw.models import Sortie

    # -- 1. 摘要 JSON 补字段 --
    try:
        summaries = json.loads(rec.sortie_summaries_json or "[]")
    except ValueError:
        summaries = []

    by_oid = {s.object_id: s for s in info.sorties if s.object_id}
    changed_json = False
    for item in summaries:
        s = by_oid.get(item.get("object_id"))
        if s is None:
            continue
        if item.get("airborne_start_s") is None and s.airborne_start_relative_s is not None:
            item["airborne_start_s"] = round(s.airborne_start_relative_s, 1)
            changed_json = True
        if item.get("airborne_end_s") is None and s.airborne_end_relative_s is not None:
            item["airborne_end_s"] = round(s.airborne_end_relative_s, 1)
            changed_json = True
    if changed_json:
        rec.sortie_summaries_json = json.dumps(summaries, ensure_ascii=False)

    # -- 2. 架次时间改成各人自己的在空区间 --
    origin = info.recording_start_utc - timedelta(seconds=info.min_relative_seconds)
    fixed = 0
    for row in db.scalars(_select(Sortie).where(Sortie.acmi_file_id == rec.id)):
        item = next((x for x in summaries
                     if x.get("object_id") == _oid_of(row, by_oid)), None)
        if item is None:
            continue
        a, b = item.get("airborne_start_s"), item.get("airborne_end_s")
        if a is None or b is None:
            continue
        new_start = origin + timedelta(seconds=float(a))
        new_end = origin + timedelta(seconds=float(b))
        if row.takeoff_at != new_start or (row.landing_at and row.landing_at != new_end):
            row.takeoff_at = new_start
            if row.landing_at is not None:
                row.landing_at = new_end
            fixed += 1
    return fixed


def _oid_of(row, by_oid) -> str | None:
    """把架次行对上摘要条目：优先用 acmi_actors 的原始名，退回名字匹配。"""
    for oid, s in by_oid.items():
        if s.raw_pilot_name == row.raw_pilot_name:
            return oid
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="重解析 ACMI，修正时长、时间窗与架次在空区间")
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

        print("=" * 100)
        print("待修正 ACMI 记录 %d 条%s" % (len(rows), "" if args.apply else "（预览，未写库）"))
        print("=" * 100)
        print("%-32s %11s %11s %11s %8s"
              % ("文件", "原记录时长", "新记录时长", "日志时长", "架次数"))
        print("-" * 100)

        fixed = 0
        touched_missions: set[str] = set()
        for rec in rows:
            path = resolve_stored(rec)
            if path is None or not path.exists():
                print("%-32s 跳过：原件不在 %s" % (rec.original_filename[:32],
                                                  rec.stored_path))
                continue
            try:
                info = parse_file(str(path))
            except Exception as exc:  # noqa: BLE001
                print("%-32s 跳过：重解析失败 %s"
                      % (rec.original_filename[:32], str(exc)[:40]))
                continue

            old_dur = rec.duration_seconds
            new_start = info.recording_start_utc
            new_end = info.recording_end_utc
            width = ((new_end - new_start).total_seconds()
                     if (new_start and new_end) else None)

            # 日志时长 = 各架次在空区间的并集（只算一次）
            origin = new_start - timedelta(seconds=info.min_relative_seconds)
            ivs = [(origin + timedelta(seconds=s.airborne_start_relative_s),
                    origin + timedelta(seconds=s.airborne_end_relative_s))
                   for s in info.sorties
                   if s.airborne_start_relative_s is not None
                   and s.airborne_end_relative_s is not None]
            log_secs = union_seconds(ivs)

            print("%-32s %11s %11s %11s %8d"
                  % (rec.original_filename[:32], fmt_h(old_dur),
                     fmt_h(info.duration_seconds), fmt_h(log_secs),
                     len(info.sorties)))

            if args.apply:
                rec.min_relative_seconds = info.min_relative_seconds
                rec.max_relative_seconds = info.max_relative_seconds
                rec.duration_seconds = info.duration_seconds
                rec.recorded_start_at = new_start
                rec.recorded_end_at = new_end
                n = backfill_sorties(db, rec, info)
                print("      架次时间已按各人在空区间修正：%d 条" % n)
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
                print("  重算任务 %s：%s → %s"
                      % (mission.id[:8], mission.started_at, mission.ended_at))
                print("      记录时长 %s / 日志时长 %s"
                      % (fmt_h(mission.duration_seconds),
                         fmt_h(mission_flight_seconds(db, [mission.id])
                               .get(mission.id, 0))))
            db.commit()
            print("-" * 100)
            print("已修正 %d 条记录，重算 %d 个任务。"
                  % (fixed, len(touched_missions)))
        else:
            print("-" * 100)
            print("预览结束，%d 条将被修正。加 --apply 才会写库。" % fixed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
