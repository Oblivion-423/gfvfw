"""
统计与查询服务。

口径说明（重要）
----------------
* **所有统计只基于 ``sorties``** —— AI 与未认领飞行员根本不生成架次，
  因此统计层**天然排除**它们，不需要任何过滤条件。
* 时长按**秒**存储，展示层转"小时分钟"。
* 航程按**米**存储，展示层转**海里**。
* **任务维度**的时长一律"同一任务只算一次"——各架次在空区间取并集，
  见 :func:`mission_flight_seconds`。**飞行员维度**的时长仍是各人架次之和
  （人次口径）。两者含义不同，页面上必须分别标清楚，否则会让人以为数据错了。
* 排行榜只统计 ``member_id`` 非空的架次（成员个人成绩）。
  未认领架次另行汇总，避免"数据看着对但没人认领"时被误当成队员成绩。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    AcmiFile, AircraftType, Campaign, Member, Mission, Sortie,
)

#: 1 海里 = 1852 米
METERS_PER_NM = 1852.0


# --------------------------------------------------------------------------
# 任务时长口径：**同一任务只算一次**
# --------------------------------------------------------------------------

def union_seconds(intervals) -> int:
    """若干时间区间取**并集**后的总秒数。

    这是"同一任务只算一次"的基本运算。多个飞行员同时飞同一任务时，
    各人的架次区间相互重叠；直接相加会把同一段时间重复计算
    （实测 4 人各飞约 1 小时 10 分，相加得 4 小时 24 分，而任务只历时 1 小时 13 分）。
    取并集后，每一段时间只计一次。

    相接（``s == cur_e``）也算重叠并合并 —— 否则连续飞行的两段会被
    算成两个区间，虽然总秒数相同，但语义上更清楚。
    """
    cleaned = sorted((s, e) for s, e in intervals if s and e and e > s)
    if not cleaned:
        return 0
    total = 0.0
    cur_s, cur_e = cleaned[0]
    for s, e in cleaned[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).total_seconds()
    return int(round(total))


def mission_flight_seconds(db: Session, mission_ids) -> dict:
    """``{mission_id: 任务**日志时长**秒数}``，**同一任务只算一次**。

    优先取各架次「在空区间」的并集；没有可用区间时退回任务窗口
    （``ended_at − started_at``），再退回该任务最长的单个架次。

    ⚠️ **这是"日志时长"，不是"记录时长"**，两者必须分别标注：

    | 量 | 来源 | 实测同一任务 |
    |---|---|---|
    | **日志时长**（本函数） | 各架次**在空区间**的并集 | 4032 s = **1 小时 7 分** |
    | **记录时长** | ACMI **录制时间窗**宽度（起飞前 + 在空 + 降落后） | 4418 s = **1 小时 13 分** |

    录制窗必然 ≥ 在空并集（差值就是起飞前与降落后的时间）。
    当初把两者混为一谈，导致页面上的"任务时长"其实是录制窗宽度。

    ⚠️ 刻意在**读取时**算，而不是只依赖 ``missions.duration_seconds`` 这个
    冗余列 —— 后者只在归并时（``recompute_mission``）刷新，架次被编辑或删除
    后就失真了。本函数每页只发一次查询，不随任务数增长。
    """
    ids = [i for i in dict.fromkeys(mission_ids) if i]
    if not ids:
        return {}

    windows = {m.id: (m.started_at, m.ended_at)
               for m in db.scalars(select(Mission).where(Mission.id.in_(ids)))}
    rows = db.execute(
        select(Sortie.mission_id, Sortie.takeoff_at, Sortie.landing_at,
               Sortie.flight_seconds)
        .where(Sortie.mission_id.in_(ids), Sortie.deleted_at.is_(None))
    ).all()

    spans: dict[str, list] = {}
    longest: dict[str, int] = {}
    for mid, takeoff, landing, secs in rows:
        if takeoff:
            # ⚠️ 未记录到降落时（飞行员未降落/坠毁，ingest 会把 landing_at 留空），
            #    用「起飞 + 在空时长」估一个终点。若直接跳过，该架次会整段从并集里
            #    消失 —— 两人同飞、其中一人没降落时，任务时长会被低估一半。
            end = landing
            if end is None and secs:
                end = takeoff + timedelta(seconds=int(secs))
            if end and end > takeoff:
                spans.setdefault(mid, []).append((takeoff, end))
        longest[mid] = max(longest.get(mid, 0), int(secs or 0))

    out: dict[str, int] = {}
    for mid in ids:
        secs = union_seconds(spans.get(mid, []))
        if not secs:
            start, end = windows.get(mid, (None, None))
            if start and end:
                secs = int((end - start).total_seconds())
        if not secs:
            secs = longest.get(mid, 0)
        out[mid] = secs
    return out


def mission_recording_seconds(db: Session, mission_ids) -> dict:
    """``{mission_id: 任务**记录时长**秒数}`` —— ACMI 录制时间窗的宽度。

    ⚠️ 与 :func:`mission_flight_seconds`（日志时长）是**两个不同的量**：

    * **记录时长** = 从开始录制到停止录制。含起飞前的地面时间与降落后的滑行，
      因此**必然 ≥** 该任务的日志时长。
    * **日志时长** = 各架次在空区间的并集。

    对多份文件归并出的任务，任务窗口（``started_at``/``ended_at``）本身就是
    各文件录制窗的并集，所以直接取窗口宽度即可。
    """
    ids = [i for i in dict.fromkeys(mission_ids) if i]
    if not ids:
        return {}
    out: dict[str, int] = {}
    for m in db.scalars(select(Mission).where(Mission.id.in_(ids))):
        if m.started_at and m.ended_at and m.ended_at > m.started_at:
            out[m.id] = int(round((m.ended_at - m.started_at).total_seconds()))
        else:
            out[m.id] = int(m.duration_seconds or 0)
    return out


# --------------------------------------------------------------------------
# 查询条件
# --------------------------------------------------------------------------

@dataclass
class SortieFilter:
    """飞行日志查询条件。全部可选，组合生效。"""

    callsign: str = ""              # 呼号片段
    member_id: str = ""             # 精确到某人
    aircraft_raw: str = ""          # ACMI 原始机型名片段
    aircraft_type_id: str = ""      # 精确到标准机型
    mission_type: str = ""          # 任务类型
    campaign_id: str = ""
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    confidence: str = ""            # exact / partial / estimated
    include_unclaimed: bool = True  # 是否包含未认领飞行员

    @property
    def is_empty(self) -> bool:
        return not any([
            self.callsign, self.member_id, self.aircraft_raw, self.aircraft_type_id,
            self.mission_type, self.campaign_id, self.date_from, self.date_to,
            self.confidence,
        ]) and self.include_unclaimed


def _base_query(f: SortieFilter) -> Select:
    stmt = (select(Sortie, Member.callsign, Mission.name, Mission.mission_type)
            .join(Mission, Mission.id == Sortie.mission_id)
            .join(Member, Member.id == Sortie.member_id, isouter=True)
            .where(Sortie.deleted_at.is_(None), Mission.deleted_at.is_(None)))

    if not f.include_unclaimed:
        stmt = stmt.where(Sortie.member_id.isnot(None))
    if f.member_id:
        stmt = stmt.where(Sortie.member_id == f.member_id)
    if f.callsign.strip():
        # 同时匹配「名册呼号」与「ACMI 原始名」：
        # 未认领的架次没有 member_id，只有 raw_pilot_name 可用，
        # 因此必须两者都匹配，否则搜不到未认领记录。
        needle = "%%%s%%" % f.callsign.strip()
        stmt = stmt.where(or_(Member.callsign.ilike(needle),
                             Sortie.raw_pilot_name.ilike(needle)))
    if f.aircraft_raw.strip():
        stmt = stmt.where(Sortie.aircraft_raw_name.ilike(
            "%%%s%%" % f.aircraft_raw.strip()))
    if f.aircraft_type_id:
        stmt = stmt.where(Sortie.aircraft_type_id == f.aircraft_type_id)
    if f.mission_type:
        stmt = stmt.where(Mission.mission_type == f.mission_type)
    if f.campaign_id:
        stmt = stmt.where(Mission.campaign_id == f.campaign_id)
    if f.date_from:
        stmt = stmt.where(Sortie.takeoff_at >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Sortie.takeoff_at < f.date_to)
    if f.confidence:
        stmt = stmt.where(Sortie.data_confidence == f.confidence)
    return stmt


def query_sorties(db: Session, f: SortieFilter,
                  limit: int = 200, offset: int = 0) -> list[dict]:
    """按条件查询架次明细。"""
    stmt = _base_query(f).order_by(Sortie.takeoff_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).all()
    out = []
    for s, callsign, mission_name, mtype in rows:
        out.append({
            "id": s.id, "mission_id": s.mission_id,
            "callsign": callsign or s.raw_pilot_name,
            "member_id": s.member_id,
            "unclaimed": s.member_id is None,
            "aircraft_raw": s.aircraft_raw_name,
            "coalition": s.coalition,
            "mission_name": mission_name,
            "mission_type": mtype,
            "takeoff_at": s.takeoff_at,
            "flight_seconds": s.flight_seconds,
            "distance_meters": s.distance_meters,
            "takeoff_count": s.takeoff_count,
            "landing_count": s.landing_count,
            "weapons_fired": s.weapons_fired,
            "deaths": s.deaths,
            "crashed": s.crashed,
            "exceedance_count": s.exceedance_count,
            "max_g": s.max_g,
            "data_confidence": s.data_confidence,
        })
    return out


def count_sorties(db: Session, f: SortieFilter) -> int:
    stmt = _base_query(f).with_only_columns(func.count(Sortie.id)).order_by(None)
    return db.scalar(stmt) or 0


def filter_totals(db: Session, f: SortieFilter) -> dict:
    """当前筛选条件下的合计。"""
    sub = _base_query(f).with_only_columns(
        Sortie.id, Sortie.flight_seconds, Sortie.distance_meters,
        Sortie.takeoff_count, Sortie.landing_count,
        Sortie.weapons_fired, Sortie.deaths,
    ).order_by(None).subquery()
    row = db.execute(
        select(
            func.count(sub.c.id),
            func.coalesce(func.sum(sub.c.flight_seconds), 0),
            func.coalesce(func.sum(sub.c.distance_meters), 0),
            func.coalesce(func.sum(sub.c.takeoff_count), 0),
            func.coalesce(func.sum(sub.c.landing_count), 0),
            func.coalesce(func.sum(sub.c.weapons_fired), 0),
            func.coalesce(func.sum(sub.c.deaths), 0),
        )
    ).one()
    return {
        "sorties": int(row[0]),
        "flight_seconds": int(row[1]),
        "distance_meters": int(row[2]),
        "distance_nm": int(row[2]) / METERS_PER_NM,
        "takeoffs": int(row[3]),
        "landings": int(row[4]),
        "weapons": int(row[5]),
        "deaths": int(row[6]),
    }


# --------------------------------------------------------------------------
# 概览统计
# --------------------------------------------------------------------------

def overview(db: Session) -> dict:
    """站点总览。"""
    def cnt(model) -> int:                                  # noqa: ANN001
        return db.scalar(select(func.count()).select_from(model)) or 0

    total = db.execute(
        select(func.coalesce(func.sum(Sortie.flight_seconds), 0),
               func.coalesce(func.sum(Sortie.distance_meters), 0),
               func.coalesce(func.sum(Sortie.weapons_fired), 0),
               func.coalesce(func.sum(Sortie.deaths), 0))
        .where(Sortie.deleted_at.is_(None))
    ).one()

    # ⚠️ 两个不同的时长口径，页面上必须分别标注：
    #   * ``flight_seconds``   —— **任务维度**：每个任务只算一次（区间并集）
    #   * ``pilot_flight_seconds`` —— **飞行员维度**：各人架次之和（人次）
    #     多人飞同一任务时后者必然大于前者，这不是数据错误。
    live_missions = list(db.scalars(
        select(Mission.id).where(Mission.deleted_at.is_(None))))
    flight_once = sum(mission_flight_seconds(db, live_missions).values())

    unclaimed = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.deleted_at.is_(None), Sortie.member_id.is_(None))) or 0

    # 近 30 天：以库中最新一份架次时间为基准，避免因无数据而空窗
    latest = db.scalar(select(func.max(Sortie.takeoff_at))
                       .where(Sortie.deleted_at.is_(None)))
    recent_flight = 0
    recent_missions: list[str] = []
    if latest:
        from datetime import timedelta
        since = latest - timedelta(days=30)
        recent_missions = list(db.scalars(
            select(Mission.id).where(Mission.deleted_at.is_(None),
                                     Mission.ended_at.is_not(None),
                                     Mission.ended_at >= since)))
        recent_flight = sum(
            mission_flight_seconds(db, recent_missions).values())

    failed_files = db.scalar(
        select(func.count()).select_from(AcmiFile)
        .where(AcmiFile.parse_status == "failed")) or 0

    return {
        "members_total": cnt(Member),
        "members_active": db.scalar(
            select(func.count()).select_from(Member)
            .where(Member.status == "active")) or 0,
        "missions": cnt(Mission),
        "sorties": cnt(Sortie),
        "acmi_files": cnt(AcmiFile),
        #: 任务维度：每个任务只算一次
        "flight_seconds": flight_once,
        #: 飞行员维度：各人架次之和（人次），必然 >= flight_seconds
        "pilot_flight_seconds": int(total[0]),
        "distance_meters": int(total[1]),
        "distance_nm": int(total[1]) / METERS_PER_NM,
        "weapons": int(total[2]),
        "deaths": int(total[3]),
        "unclaimed_sorties": unclaimed,
        "recent_30d_flight": recent_flight,
        "latest_sortie_at": latest,
        "failed_files": failed_files,
    }


def pilot_leaderboard(db: Session, limit: int = 100,
                      include_inactive: bool = True) -> list[dict]:
    """飞行员排行榜（按总飞行时长）。

    ⚠️ 只统计已认领到名册的架次 —— 未认领者没有"成绩归属方"，
    单独在 :func:`overview` 里计数提醒，不能混进排行榜。
    """
    stmt = (
        select(Member.callsign, Member.status, Member.id,
               func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0),
               func.coalesce(func.sum(Sortie.distance_meters), 0),
               func.coalesce(func.sum(Sortie.takeoff_count), 0),
               func.coalesce(func.sum(Sortie.landing_count), 0),
               func.coalesce(func.sum(Sortie.weapons_fired), 0),
               func.coalesce(func.sum(Sortie.deaths), 0),
               func.max(Sortie.takeoff_at))
        .join(Sortie, Sortie.member_id == Member.id)
        .where(Sortie.deleted_at.is_(None), Member.deleted_at.is_(None))
        .group_by(Member.id)
        .order_by(func.coalesce(func.sum(Sortie.flight_seconds), 0).desc())
        .limit(limit)
    )
    if not include_inactive:
        stmt = stmt.where(Member.status == "active")

    rows = db.execute(stmt).all()
    out = []
    for (callsign, status, mid, n, flight, dist, toff, land, wpn, deaths, last) in rows:
        out.append({
            "callsign": callsign, "status": status, "member_id": mid,
            "sorties": n,
            "flight_seconds": int(flight),
            "avg_flight_seconds": int(flight / n) if n else 0,
            "distance_meters": int(dist),
            "distance_nm": int(dist) / METERS_PER_NM,
            "takeoffs": int(toff), "landings": int(land),
            "weapons": int(wpn), "deaths": int(deaths),
            "last_sortie_at": last,
        })
    return out


def by_aircraft(db: Session) -> list[dict]:
    """机型分布。以 ACMI 原始机型名为准（标准名可能为空）。"""
    rows = db.execute(
        select(Sortie.aircraft_raw_name,
               func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0),
               func.coalesce(func.sum(Sortie.distance_meters), 0))
        .where(Sortie.deleted_at.is_(None))
        .group_by(Sortie.aircraft_raw_name)
        .order_by(func.coalesce(func.sum(Sortie.flight_seconds), 0).desc())
    ).all()

    from ..acmi_parser import normalize_aircraft
    out = []
    for raw, n, flight, dist in rows:
        std, known = normalize_aircraft(raw)
        out.append({
            "raw": raw or "（未知）",
            "standard": std,
            "known": known,
            "sorties": n,
            "flight_seconds": int(flight),
            "distance_nm": int(dist) / METERS_PER_NM,
        })
    return out


def by_mission_type(db: Session) -> list[dict]:
    rows = db.execute(
        select(Mission.mission_type,
               func.count(func.distinct(Mission.id)),
               func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0))
        .join(Sortie, Sortie.mission_id == Mission.id)
        .where(Sortie.deleted_at.is_(None), Mission.deleted_at.is_(None))
        .group_by(Mission.mission_type)
        .order_by(func.coalesce(func.sum(Sortie.flight_seconds), 0).desc())
    ).all()
    return [{"mission_type": r[0], "missions": r[1], "sorties": r[2],
             "flight_seconds": int(r[3])} for r in rows]


def monthly_trend(db: Session, months: int = 12) -> list[dict]:
    """按月汇总。

    ⚠️ 不用 ``strftime`` 做数据库端分月 —— 那是 SQLite 专有函数，
    违反本项目的可移植性强制规则（见 db.check_portability）。
    改为**取出时间段与时长后在 Python 分桶**：
    数据量（<100 人 × 数千架次）完全可接受，且 SQLite/PG 行为一致。

    分月按 **UTC+8** —— 队员感知的"这个月"是本地月份。
    """
    rows = db.execute(
        select(Sortie.takeoff_at, Sortie.flight_seconds, Sortie.distance_meters)
        .where(Sortie.deleted_at.is_(None), Sortie.takeoff_at.isnot(None))
    ).all()

    buckets: dict[str, dict] = {}
    for takeoff_at, flight, dist in rows:
        key = _beijing_month(takeoff_at)
        if not key:
            continue
        b = buckets.setdefault(key, {"month": key, "sorties": 0,
                                     "flight_seconds": 0, "distance_meters": 0})
        b["sorties"] += 1
        b["flight_seconds"] += int(flight or 0)
        b["distance_meters"] += int(dist or 0)

    out = sorted(buckets.values(), key=lambda b: b["month"], reverse=True)[:months]
    for b in out:
        b["distance_nm"] = b["distance_meters"] / METERS_PER_NM
    return out


def _beijing_month(dt: Optional[datetime]) -> Optional[str]:
    """取 UTC+8 时区下的 ``YYYY-MM``。"""
    if dt is None:
        return None
    if dt.tzinfo is None:                       # SQLite 会丢 tzinfo，按 UTC 处理
        from datetime import timezone as _tz
        dt = dt.replace(tzinfo=_tz.utc)
    from datetime import timedelta as _td, timezone as _tz2
    return dt.astimezone(_tz2(_td(hours=8))).strftime("%Y-%m")


def data_quality(db: Session) -> dict:
    """数据质量：可信度分布、未认领架次、异常告警。"""
    conf_rows = db.execute(
        select(Sortie.data_confidence, func.count(Sortie.id))
        .where(Sortie.deleted_at.is_(None))
        .group_by(Sortie.data_confidence)
    ).all()

    unclaimed = db.execute(
        select(Sortie.raw_pilot_name, func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0))
        .where(Sortie.deleted_at.is_(None), Sortie.member_id.is_(None))
        .group_by(Sortie.raw_pilot_name)
        .order_by(func.count(Sortie.id).desc())
    ).all()

    not_landed = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.deleted_at.is_(None), Sortie.takeoff_count > 0,
               Sortie.landing_count == 0)) or 0

    crashed = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.deleted_at.is_(None), Sortie.crashed.is_(True))) or 0

    exceed = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.deleted_at.is_(None), Sortie.exceedance_count > 0)) or 0

    return {
        "confidence": {r[0]: r[1] for r in conf_rows},
        "unclaimed": [{"raw_name": r[0], "sorties": r[1],
                       "flight_seconds": int(r[2])} for r in unclaimed],
        "not_landed": not_landed,
        "crashed": crashed,
        "exceedance": exceed,
    }


def filter_options(db: Session) -> dict:
    """给查询表单用的下拉选项。"""
    return {
        "members": list(db.scalars(
            select(Member).where(Member.deleted_at.is_(None))
            .order_by(Member.callsign))),
        "aircraft_types": list(db.scalars(
            select(AircraftType).where(AircraftType.is_active.is_(True))
            .order_by(AircraftType.sort_order, AircraftType.name))),
        "campaigns": list(db.scalars(
            select(Campaign).where(Campaign.deleted_at.is_(None))
            .order_by(Campaign.started_at.desc()))),
        "aircraft_raw_names": [r[0] for r in db.execute(
            select(func.distinct(Sortie.aircraft_raw_name))
            .where(Sortie.deleted_at.is_(None), Sortie.aircraft_raw_name.isnot(None))
            .order_by(Sortie.aircraft_raw_name)).all()],
    }
