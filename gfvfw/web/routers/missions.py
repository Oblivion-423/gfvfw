"""任务路由：任务列表、详情，以及任务/架次的**事后编辑与删除**。

为什么需要编辑
--------------
上线后一定会发生：ACMI 传错、归并归错、任务名打错、时长明显不合理。
需求里明确要求这条路（`docs/requirements.md` L97/L282/L326/L489）：
成员可编辑自己的架次、指挥层可修正任何记录、修正要留痕（R5）。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import AcmiFile, Campaign, Member, Mission, Sortie, SortieEvent
from ...permissions import (
    CAMPAIGN_MANAGE, LOG_DELETE, LOG_EDIT_ANY, LOG_EDIT_OWN, LOG_VIEW,
)
from ...services import campaigns as CS
from ...services.audit import record_audit
from ..deps import Principal, get_db, require
from ..forms import FieldError, local_input_value, parse_local_datetime
from ..templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/missions")

MISSION_TYPE_LABELS = {
    "training": "训练", "patrol": "巡逻", "cap": "战斗空中巡逻",
    "intercept": "截击", "escort": "护航", "strike": "对地打击",
    "sead": "压制敌防空", "cas": "近距空中支援", "recon": "侦察",
    "transport": "运输", "other": "其他",
}
CONFIDENCE_LABELS = {"exact": "完整", "partial": "部分", "estimated": "估算"}
CONFIDENCE_BADGE = {"exact": "ok", "partial": "warn", "estimated": "danger"}
EVENT_LABELS = {
    "takeoff": "起飞", "landing": "降落", "weapon_release": "武器投放",
    "hit": "命中", "kill": "击落", "shot_down": "被击落", "crash": "坠毁",
    "ejection": "弹射", "exceedance_overspeed": "超速",
    "exceedance_overg": "过载超限", "exceedance_terrain": "撞地风险",
    "other": "其他",
}
#: 任务可见性（与 members 的可见性取值一致）
VISIBILITY_LABELS = {"public": "公开", "members": "内部", "command": "指挥层"}


@router.get("")
def mission_list(request: Request,
                 principal: Principal = Depends(require(LOG_VIEW)),
                 db: Session = Depends(get_db)):

    rows = db.execute(
        select(Mission, func.count(Sortie.id))
        .join(Sortie, Sortie.mission_id == Mission.id, isouter=True)
        .where(Mission.deleted_at.is_(None))
        .group_by(Mission.id)
        .order_by(Mission.started_at.desc())
        .limit(200)
    ).all()

    missions = [{"mission": m, "sortie_count": n} for m, n in rows]

    # 战役名映射（一次查回，避免 N+1）
    campaign_names: dict[str, str] = {}
    cids = {m.campaign_id for m, _ in rows if m.campaign_id}
    if cids:
        campaign_names = {c.id: c.name for c in db.scalars(
            select(Campaign).where(Campaign.id.in_(cids)))}

    return render(request, "missions/list.html", {
        "missions": missions,
        "campaign_names": campaign_names,
        "type_labels": MISSION_TYPE_LABELS,
        "can_edit": principal.can(LOG_EDIT_ANY),
        "can_delete": principal.can(LOG_DELETE),
    })


# ==========================================================================
# 任务编辑 / 删除
# ==========================================================================

def _load_mission(db: Session, mission_id: str) -> Mission:
    m = db.get(Mission, mission_id)
    if m is None or m.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return m


@router.get("/{mission_id}/edit")
def mission_edit_form(mission_id: str, request: Request,
                      principal: Principal = Depends(require(LOG_EDIT_ANY)),
                      db: Session = Depends(get_db)):
    m = _load_mission(db, mission_id)
    n_sorties = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))) or 0
    return render(request, "missions/form.html", {
        "mission": m,
        "sortie_count": n_sorties,
        "form": {
            "name": m.name or "",
            "mission_number": m.mission_number or "",
            "mission_type": m.mission_type or "other",
            "visibility": m.visibility or "members",
            "base": m.base or "",
            "outcome": m.outcome or "",
            # ⚠️ 表单显示的是 **UTC+8**；存储始终 UTC
            "started_at": local_input_value(m.started_at),
            "ended_at": local_input_value(m.ended_at),
            "brief": m.brief or "",
            "debrief": m.debrief or "",
        },
        "type_labels": MISSION_TYPE_LABELS,
        "visibility_labels": VISIBILITY_LABELS,
        "error": None,
    })


@router.post("/{mission_id}/edit")
def mission_update(mission_id: str, request: Request,
                   name: str = Form(""),
                   mission_number: str = Form(""),
                   mission_type: str = Form("other"),
                   visibility: str = Form("members"),
                   base: str = Form(""),
                   outcome: str = Form(""),
                   started_at: str = Form(""),
                   ended_at: str = Form(""),
                   brief: str = Form(""),
                   debrief: str = Form(""),
                   csrf_token: str = Form(""),
                   principal: Principal = Depends(require(LOG_EDIT_ANY)),
                   db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    m = _load_mission(db, mission_id)
    if mission_type not in MISSION_TYPE_LABELS:
        raise HTTPException(status_code=400, detail="任务类型取值不合法")
    if visibility not in VISIBILITY_LABELS:
        raise HTTPException(status_code=400, detail="可见性取值不合法")

    try:
        new_start = parse_local_datetime(started_at, "任务开始时间")
        new_end = parse_local_datetime(ended_at, "任务结束时间")
    except FieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if new_start and new_end and new_end < new_start:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")

    before = {"name": m.name, "mission_type": m.mission_type,
              "visibility": m.visibility, "started_at": str(m.started_at),
              "ended_at": str(m.ended_at)}
    m.name = name.strip() or m.name
    m.mission_number = mission_number.strip() or None
    m.mission_type = mission_type
    m.visibility = visibility
    m.base = base.strip() or None
    m.outcome = outcome.strip() or None
    m.started_at = new_start
    m.ended_at = new_end
    m.brief = brief.strip() or None
    m.debrief = debrief.strip() or None
    db.flush()

    # 时间被改过 → 冗余时长必须重算（否则列表页显示旧值）
    from ...services.ingest import AcmiIngestService
    AcmiIngestService.recompute_mission(db, m)

    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="missions", target_id=m.id,
                 before=before,
                 after={"name": m.name, "mission_type": m.mission_type,
                        "visibility": m.visibility,
                        "started_at": str(m.started_at),
                        "ended_at": str(m.ended_at)},
                 reason="人工修正任务", request=request)
    db.commit()
    return RedirectResponse("/missions/%s?message=任务已更新" % m.id,
                            status_code=303)


@router.get("/{mission_id}/delete")
def mission_delete_confirm(mission_id: str, request: Request,
                           principal: Principal = Depends(require(LOG_DELETE)),
                           db: Session = Depends(get_db)):
    """删除前的确认页 —— 要列清"会连带影响什么"。"""
    m = _load_mission(db, mission_id)
    n_sorties = db.scalar(
        select(func.count()).select_from(Sortie)
        .where(Sortie.mission_id == m.id, Sortie.deleted_at.is_(None))) or 0
    files = list(db.scalars(
        select(AcmiFile).where(AcmiFile.mission_id == m.id)))
    return render(request, "missions/delete.html", {
        "mission": m,
        "sortie_count": n_sorties,
        "files": files,
    })


@router.post("/{mission_id}/delete")
def mission_delete(mission_id: str, request: Request,
                   reason: str = Form(""),
                   csrf_token: str = Form(""),
                   principal: Principal = Depends(require(LOG_DELETE)),
                   db: Session = Depends(get_db)):
    """软删除任务，并**撤销这次归并**。

    连带处理（否则会留下半截数据）：
    * 该任务的架次一并软删除 —— 否则它们会继续出现在查询与排行榜里；
    * 该任务的 ACMI 文件**拆回待归并** —— 删任务多半就是因为归并归错了，
      拆回去才能重新归并；这也正是"改归属"的实现方式。
    """
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    m = _load_mission(db, mission_id)
    now = utcnow()

    sorties = list(db.scalars(
        select(Sortie).where(Sortie.mission_id == m.id,
                             Sortie.deleted_at.is_(None))))
    for s in sorties:
        s.deleted_at = now

    files = list(db.scalars(
        select(AcmiFile).where(AcmiFile.mission_id == m.id)))
    for f in files:
        f.mission_id = None
        # 批次也一并清掉：该批次已被"撤销归并"，留着会显示成"待确认"
        f.batch_id = None

    m.deleted_at = now
    db.flush()

    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="missions", target_id=m.id,
                 before={"name": m.name, "mission_type": m.mission_type},
                 after={"soft_deleted": True, "sorties": len(sorties),
                        "files_detached": len(files)},
                 reason=reason.strip() or "删除任务（撤销归并）", request=request)
    db.commit()

    log.info("任务 %s 已软删除：连带 %d 架次、拆回 %d 份 ACMI",
             m.id, len(sorties), len(files))
    msg = "任务已删除；%d 个架次已一并删除，%d 份 ACMI 已拆回待归并" % (
        len(sorties), len(files))
    return RedirectResponse("/missions?message=%s" % msg, status_code=303)


@router.post("/{mission_id}/campaign")
def mission_set_campaign(mission_id: str, request: Request,
                         campaign_id: str = Form(""),
                         csrf_token: str = Form(""),
                         principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                         db: Session = Depends(get_db)):
    """把任务归入某个战役，或移出（``campaign_id`` 留空）。"""
    from ...security import verify_csrf
    from ...services.audit import record_audit
    verify_csrf(request, csrf_token)

    m = db.get(Mission, mission_id)
    if m is None or m.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")

    before = m.campaign_id
    if campaign_id:
        c = db.get(Campaign, campaign_id)
        if c is None or c.deleted_at is not None:
            raise HTTPException(status_code=400, detail="目标战役不存在")
        m.campaign_id = c.id
    else:
        m.campaign_id = None

    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="missions", target_id=m.id,
                 before={"campaign_id": before}, after={"campaign_id": m.campaign_id},
                 reason="调整任务所属战役", request=request)
    db.commit()

    if m.campaign_id:
        return RedirectResponse("/campaigns/%s?message=任务已归入" % m.campaign_id,
                                status_code=303)
    return RedirectResponse("/missions/%s?message=已移出战役" % m.id, status_code=303)


@router.get("/{mission_id}")
def mission_detail(mission_id: str, request: Request,
                   principal: Principal = Depends(require(LOG_VIEW)),
                   db: Session = Depends(get_db)):

    mission = db.get(Mission, mission_id)
    if mission is None or mission.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")

    sortie_rows = db.execute(
        select(Sortie, Member.callsign)
        .join(Member, Member.id == Sortie.member_id, isouter=True)
        .where(Sortie.mission_id == mission.id, Sortie.deleted_at.is_(None))
        .order_by(Sortie.flight_seconds.desc())
    ).all()

    from ...acmi_parser import normalize_aircraft
    me = principal.member.id if principal.member else None
    can_edit_any = principal.can(LOG_EDIT_ANY)
    can_edit_own = principal.can(LOG_EDIT_OWN)
    sorties = []
    agg = {"flight": 0, "distance": 0, "weapons": 0, "deaths": 0,
           "takeoffs": 0, "landings": 0}
    for s, callsign in sortie_rows:
        std, _ = normalize_aircraft(s.aircraft_raw_name)
        sorties.append({
            "id": s.id,
            "callsign": callsign or s.raw_pilot_name,
            "member_id": s.member_id,
            "unclaimed": s.member_id is None,
            "aircraft": std or s.aircraft_raw_name or "—",
            "coalesce": s.coalition,
            "flight_seconds": s.flight_seconds,
            "distance_meters": s.distance_meters,
            "takeoff_count": s.takeoff_count,
            "landing_count": s.landing_count,
            "weapons_fired": s.weapons_fired,
            "deaths": s.deaths,
            "crashed": s.crashed,
            "exceedance_count": s.exceedance_count,
            "max_g": s.max_g,
            "end_cas_kts": s.end_cas_kts,
            "data_confidence": s.data_confidence,
            "data_source": s.data_source,
            # 自己人的架次看 log.edit.own，别人的看 log.edit.any
            "editable": can_edit_any or (can_edit_own and me is not None
                                         and s.member_id == me),
        })
        agg["flight"] += s.flight_seconds or 0
        agg["distance"] += s.distance_meters or 0
        agg["weapons"] += s.weapons_fired or 0
        agg["deaths"] += s.deaths or 0
        agg["takeoffs"] += s.takeoff_count or 0
        agg["landings"] += s.landing_count or 0

    # ⚠️ 这里有两个**不同的量**，标签必须分开（pages 上也写明了口径）：

    #    ① 日志时长：各架次「在空区间」的并集 —— 多人同飞只算一次。
    #       上面的 agg["flight"] 是**人次相加**（飞行员累计），不能当任务时长用。
    #    ② 记录时长：ACMI 录制时间窗的宽度（含起飞前/降落后），必然 ≥ 日志时长。
    #       它来自文件录制窗，与"飞了多久"不是一回事。
    from ...services.stats import mission_flight_seconds, mission_recording_seconds
    agg["flight_once"] = mission_flight_seconds(db, [mission.id]).get(mission.id, 0)
    agg["recording_seconds"] = mission_recording_seconds(
        db, [mission.id]).get(mission.id, 0)

    files = list(db.scalars(
        select(AcmiFile).where(AcmiFile.mission_id == mission.id)
        .order_by(AcmiFile.recorded_start_at)))

    events = db.execute(
        select(SortieEvent, Member.callsign)
        .join(Sortie, Sortie.id == SortieEvent.sortie_id)
        .join(Member, Member.id == Sortie.member_id, isouter=True)
        .where(Sortie.mission_id == mission.id)
        .order_by(SortieEvent.occurred_at)
        .limit(200)
    ).all()

    campaign = db.get(Campaign, mission.campaign_id) if mission.campaign_id else None

    return render(request, "missions/detail.html", {
        "mission": mission,
        "campaign": campaign,
        "campaigns": CS.campaigns_for_select(db) if principal.can(CAMPAIGN_MANAGE) else [],
        "can_manage_campaign": principal.can(CAMPAIGN_MANAGE),
        "sorties": sorties,
        "files": files,
        "events": [{"event": e, "callsign": cs} for e, cs in events],
        "agg": agg,
        "type_labels": MISSION_TYPE_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "confidence_badge": CONFIDENCE_BADGE,
        "event_labels": EVENT_LABELS,
    })
