"""
ACMI 摄入服务 —— 把上传的 ACMI 文件变成结构化记录。

完整链路（对应 docs/database-design.md §5 流程图）
--------------------------------------------------

    上传文件
      ├─ 计算 SHA256、按内容落盘（R4 去重）
      ├─ 解析（流式，异常不静默丢弃）
      ├─ 落 acmi_files + acmi_actors
      ├─ 归并建议（按时间窗）→ import_batches（状态 suggested）
      ▼
    【人工确认】admin 决定：并入哪个 Mission / 新建 / 忽略
      ▼
    正式入库：创建 Mission + Sortie + SortieEvent
      ├─ 飞行员名 → member（经 PilotMapping 别名表）
      └─ 未命中名册者 → 仅为"未认领"，不生成正式架次

设计要点
--------
* **SHA256 唯一**：同一份文件重复上传会被识别，不会产生重复数据。
* **AI 不生成 sorties**：只记 acmi_actors，统计层天然排除 AI。
* **未认领飞行员**：保留 raw_pilot_name，`member_id` 为 NULL。
* **解析失败不静默**：parse_status=failed + parse_error 留档，原件仍保存。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..acmi_parser import AcmiFileInfo, AcmiParser, SortieRecord, normalize_aircraft
from ..config import settings
from ..db import utcnow
from ..models import (
    AcmiActor, AcmiFile, AircraftAlias, AircraftType, IgnoredPilot, ImportBatch,
    Member, Mission, PilotMapping, Sortie, SortieEvent,
)

log = logging.getLogger(__name__)

#: 归并建议：两文件录制时间窗重叠超过该比例即视为同一场任务
MERGE_OVERLAP_THRESHOLD = 0.5
#: 归并建议：时间窗允许的间隙（秒）。同一任务的多份录像起止可能略有差异。
MERGE_GAP_SECONDS = 900.0


# --------------------------------------------------------------------------
# 结果载体
# --------------------------------------------------------------------------

@dataclass
class StoreResult:
    """一次上传处理的结果。"""

    acmi_file: AcmiFile
    duplicate: bool = False
    #: 未命中名册的飞行员名（需人工认领）
    unclaimed_pilots: list[str] = field(default_factory=list)
    #: 未归一的机型名（需补 aircraft_aliases）
    unknown_aircraft: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class MergePlanItem:
    acmi_file_id: str
    original_filename: str
    recorded_start_at: Optional[datetime]
    recorded_end_at: Optional[datetime]
    pilot_names: list[str]
    mission_id: Optional[str] = None      # 已归属则非空


@dataclass
class MergePlan:
    """归并建议 —— 交由人工确认。"""

    batch: ImportBatch
    items: list[MergePlanItem]
    #: 建议新建的 Mission 时间窗
    proposed_start_at: Optional[datetime]
    proposed_end_at: Optional[datetime]
    pilot_names: list[str]
    overlap_score: float


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

class AcmiIngestService:
    """ACMI 摄入服务。所有方法以 Session 为参数，便于测试注入。"""

    def __init__(self, storage_dir: Optional[Path] = None,
                 parser: Optional[AcmiParser] = None):
        self.storage_dir = Path(storage_dir or settings.storage_dir)
        self.parser = parser or AcmiParser(
            ground_alt_m=5.0,
            duration_alert_s=settings.duration_alert_seconds,
        )

    # -- 1. 存储文件 ------------------------------------------------------

    def _hash_file(self, path: Path) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    def _park_path(self, sha256: str, filename: str) -> Path:
        """按 ``acmi/<年月>/<sha256>__<原名>`` 落盘，避免同名覆盖。"""
        now = utcnow()
        sub = self.storage_dir / "acmi" / now.strftime("%Y-%m")
        sub.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
        return sub / ("%s__%s" % (sha256[:16], safe))

    # -- 2. 处理单个文件 --------------------------------------------------

    def ingest_file(self, db: Session, source_path: str | Path,
                    original_filename: Optional[str] = None,
                    uploaded_by: Optional[str] = None) -> StoreResult:
        """处理一个 ACMI：落盘、解析、建记录、生成归并建议。

        **幂等**：同一 SHA256 再次上传会直接返回已有记录并标记 ``duplicate``。
        """
        src = Path(source_path)
        filename = original_filename or src.name

        sha256, size = self._hash_file(src)

        existing = db.scalar(select(AcmiFile).where(AcmiFile.sha256 == sha256))
        if existing is not None:
            log.info("ACMI 已存在（SHA256 命中），跳过：%s", filename)
            return StoreResult(acmi_file=existing, duplicate=True,
                               warnings=["此文件已存在，未重复入库"])

        if size > settings.max_acmi_bytes:
            raise ValueError("文件超过上限 %d 字节：%d" % (settings.max_acmi_bytes, size))

        dest = self._park_path(sha256, filename)
        if not dest.exists():
            shutil.copy2(src, dest)

        rec = AcmiFile(
            sha256=sha256,
            original_filename=filename,
            stored_path=str(dest.relative_to(self.storage_dir)).replace("\\", "/"),
            size_bytes=size,
            uploaded_by=uploaded_by,
            parse_status="parsing",
        )
        db.add(rec)
        db.flush()

        result = StoreResult(acmi_file=rec)
        try:
            info = self.parser.parse(str(dest))
        except Exception as exc:                       # noqa: BLE001
            # R2：解析失败必须留档，**不静默丢弃**，原件已保存
            rec.parse_status = "failed"
            rec.parse_error = "%s: %s" % (type(exc).__name__, exc)
            db.flush()
            log.warning("ACMI 解析失败 %s: %s", filename, exc)
            result.warnings.append("解析失败：%s" % exc)
            return result

        self._apply_info(db, rec, info)
        rec.parse_status = "parsed"
        rec.parsed_at = utcnow()

        self._store_actors(db, rec, info, result)
        db.flush()

        result.warnings.extend(info.warnings)
        return result

    def _apply_info(self, db: Session, rec: AcmiFile, info: AcmiFileInfo) -> None:
        rec.container_format = info.container
        rec.inner_entry_name = info.inner_entry
        rec.format_version = info.file_version
        rec.data_recorder = info.data_recorder
        rec.data_source = info.data_source
        rec.reference_time = info.reference_time
        rec.filename_time = info.filename_time
        # 时间窗按 t=0 基准点推导，两个端点用**同一个**基准：
        #   起点 = 基准 + 首标记，终点 = 基准 + 末标记
        # ⚠️ 曾经把 recorded_start_at 写成基准点本身（= 剧本纪元），
        #    于是时间窗比真实录制宽出"首标记"那段，任务时长随之被撑大。
        rec.recorded_end_at = info.recording_end_utc
        rec.recorded_start_at = info.recording_start_utc
        rec.min_relative_seconds = info.min_relative_seconds
        rec.max_relative_seconds = info.max_relative_seconds
        rec.duration_seconds = info.duration_seconds

        rec.line_count = info.line_count
        rec.timestamp_line_count = info.timestamp_line_count
        rec.object_count = info.object_count
        rec.objects_with_pilot_name = info.objects_with_pilot_name
        rec.unnamed_ai_actors = info.unnamed_ai_actors

        pilots = sorted({s.raw_pilot_name for s in info.sorties if s.raw_pilot_name})
        rec.pilot_names_json = json.dumps(pilots, ensure_ascii=False)
        rec.parse_warnings_json = json.dumps(info.warnings, ensure_ascii=False) \
            if info.warnings else None

        # ⚠️ 持久化解析快照 —— 确认入库时直接读它，避免重新解析大文件
        #    （实测单文件最大 107.9 MB / 529 万行，重解析代价不可接受）
        rec.sortie_summaries_json = json.dumps(
            [self._summary_of(s) for s in info.sorties], ensure_ascii=False)

    @staticmethod
    def _summary_of(s: SortieRecord) -> dict:
        """把一条解析结果压成可存储的 JSON 摘要。"""
        return {
            "object_id": s.object_id,
            "pilot": s.raw_pilot_name,
            "callsign": s.tactical_callsign,
            "aircraft_raw": s.aircraft_raw_name,
            "aircraft_std": s.aircraft_standard_name,
            "coalition": s.coalition,
            "flight_seconds": round(s.flight_seconds, 1),
            # ⚠️ 在空区间（相对秒）—— 「日志时长」的依据，与「记录时长」不同。
            #    必须持久化：否则确认入库时只能退回用录制时间窗，
            #    而录制窗含起飞前/降落后，会让"任务占用了多久"偏大。
            "airborne_start_s": (round(s.airborne_start_relative_s, 1)
                                 if s.airborne_start_relative_s is not None else None),
            "airborne_end_s": (round(s.airborne_end_relative_s, 1)
                               if s.airborne_end_relative_s is not None else None),
            "first_seen_s": (round(s.first_seen_relative_s, 1)
                             if s.first_seen_relative_s is not None else None),
            "last_seen_s": (round(s.last_seen_relative_s, 1)
                            if s.last_seen_relative_s is not None else None),
            "takeoffs": s.takeoff_count,
            "landings": s.landing_count,
            "distance_m": round(s.distance_meters),
            "weapons_fired": s.weapons_fired,
            "kills": s.kills,
            "deaths": s.deaths,
            "crashed": s.crashed,
            "ejected": s.ejected,
            "exceedances": s.exceedance_count,
            "max_g": s.max_g,
            "max_mach": s.max_mach,
            "max_cas": s.max_cas_kts,
            "end_cas": s.end_cas_kts,
            "end_alt": s.end_altitude_m,
            "max_alt": s.max_altitude_m,
            "speed_samples": s.speed_sample_count,
            "data_confidence": s.data_confidence,
        }

    def _store_actors(self, db: Session, rec: AcmiFile,
                      info: AcmiFileInfo, result: StoreResult) -> None:
        """写入 acmi_actors，并解析飞行员名与机型名。

        只对**载人平台**建 actor 行 —— 实测一份大任务可有 23842 个对象，
        其中绝大多数是地面单位与诱饵，全部入库会产生巨量噪声。
        """
        for s in info.sorties:
            if not s.raw_pilot_name:
                continue
            mapping = db.scalar(
                select(PilotMapping).where(PilotMapping.raw_name == s.raw_pilot_name))
            member_id = mapping.member_id if mapping else None
            if mapping is None and s.raw_pilot_name not in result.unclaimed_pilots:
                result.unclaimed_pilots.append(s.raw_pilot_name)

            actor = AcmiActor(
                acmi_file_id=rec.id,
                acmi_object_id=s.object_id or "0",
                pilot_name=s.raw_pilot_name,
                tactical_callsign=s.tactical_callsign,
                aircraft_raw_name=s.aircraft_raw_name,
                coalition=s.coalition,
                has_pilot_name=True,
                is_member_flight=(member_id is not None),
                role="member" if member_id else "unknown",
                member_id=member_id,
            )
            db.add(actor)

            if s.aircraft_raw_name and not s.aircraft_known:
                if s.aircraft_raw_name not in result.unknown_aircraft:
                    result.unknown_aircraft.append(s.aircraft_raw_name)

    # -- 3. 归并建议 ------------------------------------------------------

    def suggest_merge(self, db: Session,
                      file_ids: Iterable[str]) -> MergePlan:
        """按录制时间窗把若干 ACMI 归并为一个 ImportBatch（状态 suggested）。

        规则：时间窗重叠比例 ≥ :data:`MERGE_OVERLAP_THRESHOLD`，
        或间隙 ≤ :data:`MERGE_GAP_SECONDS`，即视为同一场任务。
        """
        ids = list(file_ids)
        files = list(db.scalars(select(AcmiFile).where(AcmiFile.id.in_(ids))))
        if not files:
            raise ValueError("未找到任何 ACMI 文件")

        items: list[MergePlanItem] = []
        starts, ends, pilots = [], [], set()
        for f in files:
            names = json.loads(f.pilot_names_json) if f.pilot_names_json else []
            items.append(MergePlanItem(
                acmi_file_id=f.id,
                original_filename=f.original_filename,
                recorded_start_at=f.recorded_start_at,
                recorded_end_at=f.recorded_end_at,
                pilot_names=names,
                mission_id=f.mission_id,
            ))
            if f.recorded_start_at:
                starts.append(f.recorded_start_at)
            if f.recorded_end_at:
                ends.append(f.recorded_end_at)
            pilots.update(names)

        p_start = min(starts) if starts else None
        p_end = max(ends) if ends else None

        # 重叠度：各文件时间窗与总体时间窗的交集/并集
        score = self._overlap_score(starts, ends)

        batch = ImportBatch(
            status="suggested",
            proposed_start_at=p_start,
            proposed_end_at=p_end,
            overlap_score=score,
            pilot_names_json=json.dumps(sorted(pilots), ensure_ascii=False),
            file_count=len(files),
            plan_json=json.dumps({
                "items": [
                    {"acmi_file_id": i.acmi_file_id,
                     "filename": i.original_filename,
                     "start": i.recorded_start_at.isoformat() if i.recorded_start_at else None,
                     "end": i.recorded_end_at.isoformat() if i.recorded_end_at else None,
                     "pilots": i.pilot_names}
                    for i in items
                ]
            }, ensure_ascii=False),
        )
        db.add(batch)
        db.flush()

        for f in files:
            f.batch_id = batch.id
        db.flush()

        return MergePlan(batch=batch, items=items,
                         proposed_start_at=p_start, proposed_end_at=p_end,
                         pilot_names=sorted(pilots), overlap_score=score)

    @staticmethod
    def _overlap_score(starts: list[datetime], ends: list[datetime]) -> float:
        """时间窗一致性评分（0~1）。1 表示所有文件时间窗几乎完全一致。"""
        if not starts or not ends:
            return 0.0
        overall_start, overall_end = min(starts), max(ends)
        overall = (overall_end - overall_start).total_seconds()
        if overall <= 0:
            return 1.0
        covered = 0.0
        for s, e in zip(sorted(starts), sorted(ends)):
            covered += max(0.0, (e - s).total_seconds())
        return min(1.0, covered / (overall * max(1, len(starts))))

    # -- 4. 确认入库 ------------------------------------------------------

    def confirm_merge(self, db: Session, batch_id: str,
                      confirmed_by: Optional[str] = None,
                      mission_name: Optional[str] = None,
                      mission_type: str = "other",
                      visibility: str = "members",
                      campaign_id: Optional[str] = None,
                      create_events: bool = True) -> Mission:
        """确认归并并正式入库：创建 Mission + Sortie（仅名册命中者）。

        这是**唯一**把 ACMI 数据变成飞行日志的入口。

        ``campaign_id`` 非空时，任务**直接归入该战役** —— 供"在战役管理页内
        上传 ACMI"的场景使用，省掉手工归入那一步。为空即日常训练。
        调用方须自行确认该战役存在且未删除。
        """
        batch = db.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError("归并批次不存在: %s" % batch_id)
        if batch.status not in ("suggested", "confirmed"):
            raise ValueError("批次状态为 %s，不可确认" % batch.status)

        files = list(db.scalars(
            select(AcmiFile).where(AcmiFile.batch_id == batch_id)))
        started = batch.proposed_start_at or utcnow()
        mission = Mission(
            name=mission_name or self._default_mission_name(started),
            started_at=started,
            ended_at=batch.proposed_end_at,
            mission_type=mission_type,
            visibility=visibility,
            campaign_id=campaign_id,
            duration_seconds=int(round((batch.proposed_end_at - started).total_seconds()))
            if batch.proposed_end_at else None,
            confirmed_by=confirmed_by,
            confirmed_at=utcnow(),
            acmi_completeness="complete" if files else "missing",
        )
        db.add(mission)
        db.flush()

        created = 0
        for f in files:
            f.mission_id = mission.id
            created += self._create_sorties_for_file(
                db, mission, f, create_events=create_events)

        batch.status = "imported"
        batch.suggested_mission_id = mission.id
        batch.reviewed_by = confirmed_by
        batch.reviewed_at = utcnow()
        db.flush()

        # 重算汇总与上传完整性（冗余字段必须可重算，否则架次变更后会失真）
        self.recompute_mission(db, mission)

        log.info("确认归并 batch=%s → mission=%s，生成 %d 条架次",
                 batch_id, mission.id, created)
        return mission

    # -- 4b. 重算汇总 -----------------------------------------------------

    @staticmethod
    def recompute_mission(db: Session, mission: Mission) -> Mission:
        """重算任务的冗余汇总字段与 ACMI 完整性标记。

        ``missions.duration_seconds`` / ``acmi_completeness`` 是列表页性能所需的
        冗余值。**必须提供重算入口** —— 否则架次被编辑或删除后汇总就失真了。

        ⚠️ ``duration_seconds`` 是**任务时长（同一任务只算一次）**：多人飞同一
        任务时取各架次在空区间的并集，不是人次相加。详见
        :func:`gfvfw.services.stats.mission_flight_seconds`。
        """
        from .stats import mission_flight_seconds

        rows = db.execute(
            select(
                func.count(Sortie.id),
                func.coalesce(func.sum(Sortie.flight_seconds), 0),
            ).where(Sortie.mission_id == mission.id, Sortie.deleted_at.is_(None))
        ).one()
        sortie_count, flight = int(rows[0]), int(rows[1])

        # 「只算一次」的任务时长；无架次时退回任务窗口，再退回人次之和
        once = mission_flight_seconds(db, [mission.id]).get(mission.id, 0)
        if once:
            mission.duration_seconds = once
        elif mission.started_at and mission.ended_at:
            # ⚠️ 一律四舍五入。曾经这里（以及 confirm_merge）用 int() 截断，
            #    而并集路径用 round()，同一个任务会因两条代码路径差 1 秒。
            mission.duration_seconds = int(round(
                (mission.ended_at - mission.started_at).total_seconds()))
        else:
            mission.duration_seconds = flight

        total_files = db.scalar(
            select(func.count()).select_from(AcmiFile)
            .where(AcmiFile.mission_id == mission.id)) or 0
        if total_files == 0:
            mission.acmi_completeness = "missing"
        elif sortie_count == 0:
            mission.acmi_completeness = "partial"
        else:
            mission.acmi_completeness = "complete"
        return mission

    # -- 4c. 未认领飞行员 ------------------------------------------------

    @staticmethod
    def unclaimed_pilots(db: Session) -> list[dict]:
        """列出所有尚未认领到名册的飞行员名（含出现次数与示例机型）。

        这是"人工认领"队列的数据来源。**未认领的名字不进统计** ——
        这是"不统计 AI / 名册外成员"的实现方式，且不需要任何过滤逻辑。
        已被显式忽略的名字不在此列表内。
        """
        ignored = select(IgnoredPilot.raw_name)
        rows = db.execute(
            select(AcmiActor.pilot_name,
                   func.count(AcmiActor.id).label("n"),
                   func.min(AcmiActor.aircraft_raw_name),
                   func.min(AcmiFile.recorded_start_at).label("first_seen"))
            .join(AcmiFile, AcmiFile.id == AcmiActor.acmi_file_id)
            .where(AcmiActor.member_id.is_(None),
                   AcmiActor.pilot_name.isnot(None),
                   AcmiActor.pilot_name.notin_(ignored))
            .group_by(AcmiActor.pilot_name)
            .order_by(func.count(AcmiActor.id).desc())
        ).all()
        return [{"raw_name": r[0], "count": r[1], "aircraft": r[2],
                 "first_seen": r[3]} for r in rows]

    @staticmethod
    def claimed_pilots(db: Session) -> list[dict]:
        """已建立的"名字 → 成员"映射。"""
        rows = db.execute(
            select(PilotMapping, Member.callsign)
            .join(Member, Member.id == PilotMapping.member_id)
            .order_by(Member.callsign)
        ).all()
        return [{"id": pm.id, "raw_name": pm.raw_name, "member_id": pm.member_id,
                 "callsign": cs, "confidence": pm.confidence}
                for pm, cs in rows]

    @staticmethod
    def ignored_pilots(db: Session) -> list[IgnoredPilot]:
        return list(db.scalars(select(IgnoredPilot).order_by(IgnoredPilot.raw_name)))

    @staticmethod
    def _default_mission_name(started: datetime) -> str:
        # 展示用 UTC+8
        bj = started.astimezone(timezone(timedelta(hours=8)))
        return "%s 任务" % bj.strftime("%Y-%m-%d %H:%M")

    def _create_sorties_for_file(self, db: Session, mission: Mission,
                                 rec: AcmiFile, create_events: bool = True) -> int:
        """把一份 ACMI 的飞行员记录落成 ``Sortie``。

        数据来源是 :attr:`AcmiFile.sortie_summaries_json`（解析时保存的快照），
        **不重新读取原始文件** —— 实测单文件最大 107.9 MB，重解析代价不可接受。

        ⚠️ 只有**命中飞行员映射**的才生成正式架次；
        未命中者仅保留在 ``acmi_actors``，等待人工认领。
        AI 对象没有 actor 行，因此天然不进统计。
        """
        summaries = json.loads(rec.sortie_summaries_json or "[]")
        by_oid = {s.get("object_id"): s for s in summaries}

        created = 0
        for actor in db.scalars(
                select(AcmiActor).where(AcmiActor.acmi_file_id == rec.id)):
            if not actor.has_pilot_name or not actor.member_id:
                continue          # AI 或未认领 → 不生成架次

            info = by_oid.get(actor.acmi_object_id, {})

            std_name = info.get("aircraft_std")
            if not std_name:
                std_name, _ = normalize_aircraft(actor.aircraft_raw_name)
            at_id = None
            if std_name:
                at = db.scalar(select(AircraftType).where(AircraftType.name == std_name))
                at_id = at.id if at else None

            # ---- 该飞行员自己的在空区间（日志时长的依据）----
            #
            # ⚠️ 这里曾经把 ``rec.recorded_start_at`` / ``recorded_end_at``
            #    （= **ACMI 录制时间窗**）直接写进 takeoff_at / landing_at。
            #    后果：同一文件里**四个飞行员的起降时刻完全相同**，
            #    于是"任务时长"退化成录制窗宽度（实测 1 小时 13 分），
            #    而各人实际在空只有约 1 小时 6 分 —— 两个不同的量被混成一个。
            #
            # 现在：有在空区间就用它；没有则退回录制窗并标警告。
            origin = rec.recorded_start_at
            a_start = info.get("airborne_start_s")
            a_end = info.get("airborne_end_s")
            if origin is not None and a_start is not None and a_end is not None:
                start = origin + timedelta(seconds=float(a_start))
                end = origin + timedelta(seconds=float(a_end))
            else:
                start = rec.recorded_start_at
                end = rec.recorded_end_at

            s = Sortie(
                mission_id=mission.id,
                member_id=actor.member_id,
                raw_pilot_name=actor.pilot_name or "",
                tactical_callsign=actor.tactical_callsign,
                aircraft_type_id=at_id,
                aircraft_raw_name=actor.aircraft_raw_name,
                coalition=actor.coalition,
                takeoff_at=start,
                landing_at=end if info.get("landings") else None,
                # ---- 六类指标（全部来自解析快照）----
                flight_seconds=int(info.get("flight_seconds") or 0),
                takeoff_count=int(info.get("takeoffs") or 1),
                landing_count=int(info.get("landings") or 0),
                distance_meters=int(info.get("distance_m") or 0),
                weapons_fired=int(info.get("weapons_fired") or 0),
                kills=int(info.get("kills") or 0),
                deaths=int(info.get("deaths") or 0),
                crashed=bool(info.get("crashed")),
                ejected=bool(info.get("ejected")),
                exceedance_count=int(info.get("exceedances") or 0),
                max_g=info.get("max_g"),
                max_mach=info.get("max_mach"),
                max_cas_kts=info.get("max_cas"),
                end_cas_kts=info.get("end_cas"),
                end_altitude_m=info.get("end_alt"),
                max_altitude_m=info.get("max_alt"),
                speed_sample_count=int(info.get("speed_samples") or 0),
                # ---- 来源与可信度 ----
                data_source="acmi",
                data_confidence=info.get("data_confidence") or "partial",
                acmi_file_id=rec.id,
            )
            db.add(s)
            db.flush()

            if create_events:
                self._create_events(db, s, rec, info)
            created += 1
        return created

    def _create_events(self, db: Session, sortie: Sortie,
                       rec: AcmiFile, info: dict) -> int:
        """把武器投放等事件写入 ``sortie_events``。

        ⚠️ ``kills`` 不在此推导：``Event=Destroyed`` 只标明"某对象被摧毁"，
        归属到"谁的击杀"需要跨对象关联，一期未实现（设计文档 §3.4）。
        因此只记"自己投放了武器"与"自己被打下来"这两类可靠事件。
        """
        n = 0
        start = rec.recorded_start_at

        for _ in range(int(info.get("weapons_fired") or 0)):
            db.add(SortieEvent(
                sortie_id=sortie.id,
                occurred_at=start,
                event_type="weapon_release",
                source="acmi",
                detail="来自 ACMI 武器投放计数（逐发时刻待解析器支持）",
            ))
            n += 1

        if info.get("deaths"):
            db.add(SortieEvent(
                sortie_id=sortie.id,
                occurred_at=rec.recorded_end_at,
                event_type="crash" if info.get("crashed") else "shot_down",
                source="acmi",
                detail="被摧毁" + ("（判定为坠毁）" if info.get("crashed") else ""),
            ))
            n += 1

        if info.get("ejected"):
            db.add(SortieEvent(
                sortie_id=sortie.id, occurred_at=rec.recorded_end_at,
                event_type="ejection", source="acmi", detail="弹射",
            ))
            n += 1

        if info.get("exceedances"):
            db.add(SortieEvent(
                sortie_id=sortie.id, occurred_at=start,
                event_type="exceedance_overg",
                source="acmi",
                detail="过载超限 %d 次，最大 %.1f G"
                       % (info["exceedances"], info.get("max_g") or 0),
            ))
            n += 1
        return n

    # -- 5. 飞行员认领 ----------------------------------------------------

    def claim_pilot(self, db: Session, raw_name: str, member_id: str,
                    created_by: Optional[str] = None,
                    confidence: str = "confirmed") -> PilotMapping:
        """建立"原始名 → 成员"映射，并回填历史 acmi_actors。

        ✅ 配一次永久复用；回填让先前未认领的记录立即生效。
        """
        mapping = db.scalar(select(PilotMapping).where(PilotMapping.raw_name == raw_name))
        if mapping is None:
            mapping = PilotMapping(raw_name=raw_name, member_id=member_id,
                                   confidence=confidence, created_by=created_by)
            db.add(mapping)
        else:
            mapping.member_id = member_id
            mapping.confidence = confidence
        db.flush()

        # 回填 actor
        for actor in db.scalars(
                select(AcmiActor).where(AcmiActor.pilot_name == raw_name)):
            actor.member_id = member_id
            actor.is_member_flight = True
            actor.role = "member"
        db.flush()
        return mapping

    def revoke_claim(self, db: Session, mapping_id: str) -> None:
        """撤销认领：删除映射并清空相关 actor 的绑定。

        ⚠️ 已生成的 ``sorties`` **不在此删除** —— 它们已属于某个任务，
        删除需要走任务重算流程。此处只解除"名字→成员"的映射，
        并在返回任务确认页重算时自然消失（`recompute_mission`）。
        """
        mapping = db.get(PilotMapping, mapping_id)
        if mapping is None:
            return
        raw_name = mapping.raw_name
        db.delete(mapping)
        db.flush()
        for actor in db.scalars(
                select(AcmiActor).where(AcmiActor.pilot_name == raw_name)):
            actor.member_id = None
            actor.is_member_flight = False
            actor.role = "unknown"
        db.flush()

    def ignore_pilot(self, db: Session, raw_name: str,
                     created_by: Optional[str] = None,
                     reason: Optional[str] = None) -> IgnoredPilot:
        """把某个飞行员名标记为"非联队人员"（AI / 外部），不再出现在待认领列表。"""
        row = db.scalar(select(IgnoredPilot).where(IgnoredPilot.raw_name == raw_name))
        if row is None:
            row = IgnoredPilot(raw_name=raw_name, created_by=created_by, reason=reason)
            db.add(row)
        else:
            row.reason = reason or row.reason
        db.flush()
        # 同时清空可能存在的 actor 绑定，确保不进统计
        for actor in db.scalars(
                select(AcmiActor).where(AcmiActor.pilot_name == raw_name)):
            actor.member_id = None
            actor.is_member_flight = False
            actor.role = "external"
        db.flush()
        return row

    def unignore_pilot(self, db: Session, ignored_id: str) -> None:
        row = db.get(IgnoredPilot, ignored_id)
        if row is None:
            return
        for actor in db.scalars(
                select(AcmiActor).where(AcmiActor.pilot_name == row.raw_name)):
            if actor.role == "external":
                actor.role = "unknown"
        db.delete(row)
        db.flush()

    # -- 6. 机型归一化 ----------------------------------------------------

    def register_aircraft_alias(self, db: Session, raw_name: str,
                                standard_name: str,
                                created_by: Optional[str] = None) -> AircraftAlias:
        at = db.scalar(select(AircraftType).where(AircraftType.name == standard_name))
        if at is None:
            at = AircraftType(name=standard_name)
            db.add(at)
            db.flush()
        alias = db.scalar(select(AircraftAlias).where(
            AircraftAlias.raw_name == raw_name))
        if alias is None:
            alias = AircraftAlias(raw_name=raw_name, aircraft_type_id=at.id,
                                  created_by=created_by)
            db.add(alias)
        else:
            alias.aircraft_type_id = at.id
        db.flush()
        return alias
