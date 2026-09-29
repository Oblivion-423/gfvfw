"""
飞行记录、统计与查询路由。

导航结构（一级菜单 → 子菜单）
------------------------------
* **飞行记录**（一级）
    * ``/log/campaign``          战役记录 · 一层 —— 全部战役（卡片汇总）
    * ``/log/campaign/{id}``     战役记录 · 二层 —— 该战役的任务与架次明细
    * ``/log/training``          训练记录 —— 训练类且不属战役的任务
    * ``/log/pilots``            飞行员个人记录 —— 按人汇总与明细
    * ``/missions``              全部任务（未归类的兜底入口）
    * ``/log``                   高级查询 —— 多条件组合
* ``/stats`` 统计总览
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Campaign, Member, Mission, Sortie
from ...services import stats as S
from ..deps import Principal, get_db, require_login
from ..templating import render
from .acmi import wizard_for_request
from .campaigns import STATUS_BADGE, STATUS_LABELS

router = APIRouter()

MISSION_TYPE_LABELS = {
    "training": "训练", "patrol": "巡逻", "cap": "战斗空中巡逻",
    "intercept": "截击", "escort": "护航", "strike": "对地打击",
    "sead": "压制敌防空", "cas": "近距空中支援", "recon": "侦察",
    "transport": "运输", "other": "其他",
}
CONFIDENCE_LABELS = {"exact": "完整", "partial": "部分", "estimated": "估算"}
CONFIDENCE_BADGE = {"exact": "ok", "partial": "warn", "estimated": "danger"}


@router.get("/stats")
def stats_page(request: Request,
               principal: Principal = Depends(require_login),
               db: Session = Depends(get_db)):
    return render(request, "stats/overview.html", {
        "ov": S.overview(db),
        "pilots": S.pilot_leaderboard(db),
        "aircraft": S.by_aircraft(db),
        "mission_types": S.by_mission_type(db),
        "months": S.monthly_trend(db),
        "quality": S.data_quality(db),
        "type_labels": MISSION_TYPE_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "confidence_badge": CONFIDENCE_BADGE,
    })


@router.get("/log")
def log_query(request: Request,
              callsign: str = "",
              member_id: str = "",
              aircraft_raw: str = "",
              aircraft_type_id: str = "",
              mission_type: str = "",
              campaign_id: str = "",
              date_from: str = "",
              date_to: str = "",
              confidence: str = "",
              include_unclaimed: str = "1",
              principal: Principal = Depends(require_login),
              db: Session = Depends(get_db)):

    f = S.SortieFilter(
        callsign=callsign,
        member_id=member_id,
        aircraft_raw=aircraft_raw,
        aircraft_type_id=aircraft_type_id,
        mission_type=mission_type,
        campaign_id=campaign_id,
        date_from=_parse_day(date_from, end=False),
        date_to=_parse_day(date_to, end=True),
        confidence=confidence,
        include_unclaimed=(include_unclaimed == "1"),
    )

    rows = S.query_sorties(db, f, limit=300)
    total = S.count_sorties(db, f)
    totals = S.filter_totals(db, f)

    return render(request, "stats/query.html", {
        "rows": rows,
        "total": total,
        "shown": len(rows),
        "totals": totals,
        "options": S.filter_options(db),
        "q": {
            "callsign": callsign, "member_id": member_id,
            "aircraft_raw": aircraft_raw, "aircraft_type_id": aircraft_type_id,
            "mission_type": mission_type, "campaign_id": campaign_id,
            "date_from": date_from, "date_to": date_to,
            "confidence": confidence, "include_unclaimed": include_unclaimed == "1",
        },
        "type_labels": MISSION_TYPE_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "confidence_badge": CONFIDENCE_BADGE,
        "filtered": not f.is_empty,
    })


def _parse_day(value: str, end: bool) -> datetime | None:
    """把 ``YYYY-MM-DD`` 解析为 UTC 边界。

    ⚠️ 用户输入的是 **UTC+8** 的日期，而库中存 UTC。
    因此起始 = UTC+8 当日 00:00 → UTC 前一日 16:00；
    结束 = UTC+8 次日 00:00 → UTC 当日 16:00（用 `<` 比较，天然含整天）。
    """
    if not value:
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    if end:
        d = d + timedelta(days=1)
    # UTC+8 当日 00:00 对应的 UTC 时刻
    return datetime.combine(d, time(0, 0), tzinfo=timezone.utc) - timedelta(hours=8)


# ==========================================================================
# 飞行记录 —— 三个子页面
# ==========================================================================

def _fmt_day(dt) -> str:                                    # noqa: ANN001
    return dt.strftime("%Y-%m-%d") if dt else ""


def _log_context() -> dict:
    return {
        "type_labels": MISSION_TYPE_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "confidence_badge": CONFIDENCE_BADGE,
    }


def _campaign_cards(db: Session, campaigns: list[Campaign]) -> list[dict]:
    """第一层战役卡片的汇总（任务数 / 架次 / 日志总时长 / 战损）。

    ⚠️ 任务维度的「日志总时长」沿用既定口径：**同一任务的在空区间并集
    只算一次**（:func:`S.mission_flight_seconds`），多人同飞不翻倍。
    全部聚合各发一次查询，卡片数量增长也不会造成 N+1。
    """
    ids = [c.id for c in campaigns]
    if not ids:
        return []
    agg = {cid: {"missions": 0, "sorties": 0, "flight_seconds": 0,
                 "distance_nm": 0.0, "deaths": 0} for cid in ids}

    missions = db.execute(
        select(Mission.id, Mission.campaign_id)
        .where(Mission.deleted_at.is_(None), Mission.campaign_id.in_(ids))
    ).all()
    once = S.mission_flight_seconds(db, [mid for mid, _cid in missions])
    for mid, cid in missions:
        agg[cid]["missions"] += 1
        agg[cid]["flight_seconds"] += once.get(mid, 0)

    sortie_agg = db.execute(
        select(Mission.campaign_id,
               func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.distance_meters), 0),
               func.coalesce(func.sum(Sortie.deaths), 0))
        .join(Sortie, Sortie.mission_id == Mission.id)
        .where(Sortie.deleted_at.is_(None), Mission.deleted_at.is_(None),
               Mission.campaign_id.in_(ids))
        .group_by(Mission.campaign_id)
    ).all()
    for cid, n_sorties, dist, deaths in sortie_agg:
        agg[cid]["sorties"] = int(n_sorties)
        agg[cid]["distance_nm"] = int(dist) / S.METERS_PER_NM
        agg[cid]["deaths"] = int(deaths)

    return [{"campaign": c, **agg[c.id]} for c in campaigns]


@router.get("/log/campaign")
def log_campaign(request: Request,
                 campaign_id: str = "",
                 date_from: str = "",
                 date_to: str = "",
                 principal: Principal = Depends(require_login),
                 db: Session = Depends(get_db)):
    """战役记录 —— **第一层：全部战役**。

    一层只列战役（卡片带任务数 / 架次 / 日志总时长汇总），
    点进 ``/log/campaign/{id}`` 才是该战役的任务与架次明细。

    旧版本页是"全部任务的平铺列表 + 战役下拉筛选"，``?campaign_id=…``
    的旧书签与工作台回跳链接一律 302 到对应战役的明细页（其余查询参数
    原样带走，``?acmi=`` 阶段与日期筛选都不会丢）。
    """
    if campaign_id:
        rest = [(k, v) for k, v in request.query_params.multi_items()
                if k != "campaign_id"]
        url = "/log/campaign/%s" % campaign_id
        if rest:
            url += "?" + urlencode(rest)
        return RedirectResponse(url, status_code=302)

    campaigns = list(db.scalars(
        select(Campaign).where(Campaign.deleted_at.is_(None))
        .order_by(Campaign.sort_order, Campaign.started_at.desc())))

    return render(request, "log/campaign.html", {
        **_log_context(),
        # 「ACMI 工作台」。第一层不针对某场战役，归并表单里的归属战役
        # 不锁定（选了哪场，上传后就落到那场战役的明细页继续流程）。
        **wizard_for_request(db, principal, request,
                             return_to="/log/campaign"),
        "cards": _campaign_cards(db, campaigns),
        "status_labels": STATUS_LABELS,
        "status_badge": STATUS_BADGE,
        "can_manage_campaign": principal.can("campaign.manage"),
    })


@router.get("/log/campaign/{campaign_id}")
def log_campaign_detail(campaign_id: str,
                        request: Request,
                        date_from: str = "",
                        date_to: str = "",
                        principal: Principal = Depends(require_login),
                        db: Session = Depends(get_db)):
    """战役记录 —— **第二层：一场战役的任务与架次明细**（战史视角）。"""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")

    stmt = (select(Mission)
            .where(Mission.deleted_at.is_(None),
                   Mission.campaign_id == campaign.id)
            .order_by(Mission.started_at.desc()))
    df, dt_to = _parse_day(date_from, False), _parse_day(date_to, True)
    if df:
        stmt = stmt.where(Mission.started_at >= df)
    if dt_to:
        stmt = stmt.where(Mission.started_at < dt_to)

    missions = list(db.scalars(stmt))
    totals = {"missions": len(missions), "sorties": 0, "flight_seconds": 0,
              "distance_nm": 0.0, "deaths": 0}
    # 任务时长口径：多人飞同一任务只算一次（各架次在空区间取并集）
    once = S.mission_flight_seconds(db, [m.id for m in missions])
    rows = []
    for m in missions:
        agg = db.execute(
            select(func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.distance_meters), 0),
                   func.coalesce(func.sum(Sortie.deaths), 0))
            .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))
        ).one()
        nm = int(agg[1]) / S.METERS_PER_NM
        rows.append({"mission": m, "sorties": int(agg[0]),
                     "flight_seconds": once.get(m.id, 0),
                     "distance_nm": nm, "deaths": int(agg[2])})
        totals["sorties"] += int(agg[0])
        totals["flight_seconds"] += once.get(m.id, 0)
        totals["distance_nm"] += nm
        totals["deaths"] += int(agg[2])

    return render(request, "log/campaign_detail.html", {
        **_log_context(),
        # 「ACMI 工作台」。战役**锁定**为当前战役：归并出的任务直接归入它，
        # 也保证上传完的东西立刻出现在本页的任务列表里。
        **wizard_for_request(db, principal, request,
                             return_to="/log/campaign/%s" % campaign.id,
                             campaign_id=campaign.id,
                             lock_campaign=True),
        "campaign": campaign,
        "missions": rows,
        "totals": totals,
        "status_labels": STATUS_LABELS,
        "status_badge": STATUS_BADGE,
        "q": {"date_from": date_from, "date_to": date_to},
        "can_manage_campaign": principal.can("campaign.manage"),
    })


@router.get("/log/training")
def log_training(request: Request,
                 date_from: str = "",
                 date_to: str = "",
                 include_campaign: str = "0",
                 principal: Principal = Depends(require_login),
                 db: Session = Depends(get_db)):
    """训练记录 —— 训练类任务。

    默认只看**未归入战役**的训练（战役内的训练属于战役战史，
    应到"战役记录"里看），可用开关改为全部训练。

    ⚠️ 本页不做"按人筛选" —— 那是 ``/log/pilots``（飞行员个人记录）的职责。
    把两种筛选混在一个页面会让"合计"含义变得含糊。
    """
    stmt = (select(Mission).where(Mission.deleted_at.is_(None),
                                  Mission.mission_type == "training")
            .order_by(Mission.started_at.desc()))
    if include_campaign != "1":
        stmt = stmt.where(Mission.campaign_id.is_(None))

    df, dt_to = _parse_day(date_from, False), _parse_day(date_to, True)
    if df:
        stmt = stmt.where(Mission.started_at >= df)
    if dt_to:
        stmt = stmt.where(Mission.started_at < dt_to)

    missions = list(db.scalars(stmt))

    rows = []
    totals = {"missions": 0, "sorties": 0, "flight_seconds": 0,
              "distance_nm": 0.0, "pilots": 0}
    seen_pilots: set[str] = set()
    # 任务时长口径：多人飞同一任务只算一次
    once = S.mission_flight_seconds(db, [m.id for m in missions])
    for m in missions:
        agg = db.execute(
            select(func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.flight_seconds), 0),
                   func.coalesce(func.sum(Sortie.distance_meters), 0))
            .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))
        ).one()
        names = db.execute(
            select(Member.callsign, Sortie.raw_pilot_name, Sortie.member_id)
            .join(Member, Member.id == Sortie.member_id, isouter=True)
            .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))
        ).all()
        callsigns = [(n[0] or n[1], n[2] is None) for n in names]
        seen_pilots |= {n[2] for n in names if n[2]}

        nm = int(agg[2]) / S.METERS_PER_NM
        rows.append({"mission": m, "sorties": int(agg[0]),
                     "flight_seconds": once.get(m.id, 0), "distance_nm": nm,
                     "callsigns": callsigns})
        totals["sorties"] += int(agg[0])
        totals["flight_seconds"] += once.get(m.id, 0)
        totals["distance_nm"] += nm
    totals["missions"] = len(rows)
    totals["pilots"] = len(seen_pilots)

    return render(request, "log/training.html", {
        **_log_context(),
        # 「ACMI 工作台」。本页只列 training 类型且默认只看未归战役的任务，
        # 所以这里**锁死**战役与类型 —— 否则上传完的任务会当场从本页消失。
        **wizard_for_request(db, principal, request,
                             return_to="/log/training",
                             lock_campaign=True,
                             lock_mission_type=True,
                             default_mission_type="training"),
        "rows": rows,
        "totals": totals,
        "q": {"date_from": date_from, "date_to": date_to,
              "include_campaign": include_campaign == "1"},
    })


@router.get("/log/pilots")
def log_pilots(request: Request,
               member_id: str = "",
               principal: Principal = Depends(require_login),
               db: Session = Depends(get_db)):
    """飞行员个人记录 —— 选择成员后看其全部架次明细。"""
    members = list(db.scalars(
        select(Member).where(Member.deleted_at.is_(None))
        .order_by(Member.callsign)))

    # 有架次记录的人（含未认领名字），供列表展示
    leaderboard = S.pilot_leaderboard(db)

    selected = None
    detail = None
    totals = None
    if member_id:
        selected = db.get(Member, member_id)
        if selected is None or selected.deleted_at is not None:
            raise HTTPException(status_code=404, detail="成员不存在")
        f = S.SortieFilter(member_id=member_id)
        detail = S.query_sorties(db, f, limit=500)
        totals = S.filter_totals(db, f)

    return render(request, "log/pilots.html", {
        **_log_context(),
        "members": members,
        "leaderboard": leaderboard,
        "unclaimed": S.data_quality(db)["unclaimed"],
        "selected": selected,
        "detail": detail,
        "totals": totals,
        "q": {"member_id": member_id},
    })
