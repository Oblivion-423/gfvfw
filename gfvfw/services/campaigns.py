"""
战役统计服务。

口径与 ``services/stats.py`` 保持一致：
* 只统计 ``sorties``，因此 AI 与未认领不参与
* 时长存储为秒 → 展示为小时/分
* 航程存储为米 → 展示为海里（``METERS_PER_NM``）
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Campaign, Member, Mission, Sortie
from .stats import METERS_PER_NM


def list_campaigns(db: Session, include_finished: bool = True) -> list[dict]:
    """战役列表，含汇总（一次查询取回，避免 N+1）。"""
    return _list(db, include_finished=include_finished, deleted=False)


def list_deleted_campaigns(db: Session) -> list[dict]:
    """**已作废**的战役列表（同一个汇总口径）。

    与 :func:`list_campaigns` 唯一差别就是 ``deleted_at`` 的方向：
    前者只列活的，这个只列作废的 —— 供"恢复"用。
    没有这条路的话，"作废"在界面上就是不可逆的，而提示里写着"可恢复"。
    """
    return _list(db, include_finished=True, deleted=True)


def _list(db: Session, *, include_finished: bool, deleted: bool) -> list[dict]:
    stmt = select(Campaign).where(
        Campaign.deleted_at.is_not(None) if deleted else Campaign.deleted_at.is_(None))
    if not include_finished:
        stmt = stmt.where(Campaign.status != "finished")
    stmt = stmt.order_by(Campaign.sort_order, Campaign.started_at.desc())

    campaigns = list(db.scalars(stmt))
    if not campaigns:
        return []

    ids = [c.id for c in campaigns]
    rows = db.execute(
        select(Mission.campaign_id,
               func.count(func.distinct(Mission.id)),
               func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0),
               func.coalesce(func.sum(Sortie.distance_meters), 0),
               func.coalesce(func.sum(Sortie.deaths), 0),
               func.count(func.distinct(Sortie.member_id)))
        .join(Sortie, Sortie.mission_id == Mission.id, isouter=True)
        .where(Mission.campaign_id.in_(ids), Mission.deleted_at.is_(None),
               Sortie.deleted_at.is_(None) | Sortie.id.is_(None))
        .group_by(Mission.campaign_id)
    ).all()

    agg = {r[0]: r for r in rows}
    out = []
    for c in campaigns:
        r = agg.get(c.id)
        out.append({
            "campaign": c,
            "missions": int(r[1]) if r else 0,
            "sorties": int(r[2]) if r else 0,
            "flight_seconds": int(r[3]) if r else 0,
            "distance_nm": (int(r[4]) if r else 0) / METERS_PER_NM,
            "deaths": int(r[5]) if r else 0,
            "pilots": int(r[6]) if r else 0,
        })
    return out


def campaign_detail(db: Session, campaign: Campaign) -> dict:
    """单个战役的汇总 + 任务列表。"""
    missions = list(db.scalars(
        select(Mission)
        .where(Mission.campaign_id == campaign.id, Mission.deleted_at.is_(None))
        .order_by(Mission.started_at)))

    mission_rows = []
    for m in missions:
        r = db.execute(
            select(func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.flight_seconds), 0),
                   func.coalesce(func.sum(Sortie.distance_meters), 0),
                   func.coalesce(func.sum(Sortie.deaths), 0))
            .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))
        ).one()
        mission_rows.append({
            "mission": m,
            "sorties": int(r[0]),
            "flight_seconds": int(r[1]),
            "distance_nm": int(r[2]) / METERS_PER_NM,
            "deaths": int(r[3]),
        })

    totals = {
        "missions": len(missions),
        "sorties": sum(x["sorties"] for x in mission_rows),
        "flight_seconds": sum(x["flight_seconds"] for x in mission_rows),
        "distance_nm": sum(x["distance_nm"] for x in mission_rows),
        "deaths": sum(x["deaths"] for x in mission_rows),
    }

    # 参与成员（按时长排序）
    pilots = []
    if missions:
        mid_list = [m.id for m in missions]
        rows = db.execute(
            select(Member.id, Member.callsign, Member.status,
                   func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.flight_seconds), 0),
                   func.coalesce(func.sum(Sortie.distance_meters), 0))
            .join(Sortie, Sortie.member_id == Member.id)
            .where(Sortie.mission_id.in_(mid_list), Sortie.deleted_at.is_(None))
            .group_by(Member.id)
            .order_by(func.coalesce(func.sum(Sortie.flight_seconds), 0).desc())
        ).all()
        pilots = [{"member_id": r[0], "callsign": r[1], "status": r[2],
                   "sorties": r[3], "flight_seconds": int(r[4]),
                   "distance_nm": int(r[5]) / METERS_PER_NM} for r in rows]

    # 机型分布
    aircraft = []
    if missions:
        mid_list = [m.id for m in missions]
        rows = db.execute(
            select(Sortie.aircraft_raw_name, func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.flight_seconds), 0))
            .where(Sortie.mission_id.in_(mid_list), Sortie.deleted_at.is_(None))
            .group_by(Sortie.aircraft_raw_name)
            .order_by(func.count(Sortie.id).desc())
        ).all()
        aircraft = [{"raw": r[0] or "（未知）", "sorties": r[1],
                     "flight_seconds": int(r[2])} for r in rows]

    this = next((x for x in list_campaigns(db) if x["campaign"].id == campaign.id), None)

    return {"missions": mission_rows, "totals": totals, "pilots": pilots,
            "aircraft": aircraft, "summary": this}


def unassigned_missions(db: Session) -> list[Mission]:
    """尚未归入任何战役的任务（供"加入战役"选择）。"""
    return list(db.scalars(
        select(Mission)
        .where(Mission.campaign_id.is_(None), Mission.deleted_at.is_(None))
        .order_by(Mission.started_at.desc())))


def campaigns_for_select(db: Session) -> list[Campaign]:
    return list(db.scalars(
        select(Campaign).where(Campaign.deleted_at.is_(None))
        .order_by(Campaign.sort_order, Campaign.started_at.desc())))
