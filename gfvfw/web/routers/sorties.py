"""
架次路由：编辑、软删除、**手动补录**。

权限口径（对齐需求 §4.1 与 `permissions.py` 里早已定义好的权限点）
--------------------------------------------------------------
| 动作 | 权限 | owner | commander | instructor | member |
|---|---|---|---|---|---|
| 编辑**自己**的架次 | `LOG_EDIT_OWN` | ✓ | ✓ | ✓ | ✓ |
| 编辑**任何**架次 | `LOG_EDIT_ANY` | ✓ | ✓ | ✓ | — |
| 删除架次 | `LOG_DELETE` | ✓ | ✓ | — | — |
| 手动补录架次 | `LOG_APPROVE` | ✓ | ✓ | ✓ | — |

⚠️ 这些权限点此前**定义了却从未被引用**（`LOG_EDIT_OWN`/`LOG_EDIT_ANY`/
`LOG_APPROVE`/`LOG_DELETE` 在别处出现 0 次），需求里要求的人工修正能力
（`requirements.md` L97/L326/L489）因此一直缺失。本模块把它补上。

为什么要"手动补录"
------------------
联队通常各自录制、各自上传，必然有人忘记录制或文件丢了（需求 R10）。
补录的架次 `data_source='manual'`、`data_confidence='estimated'`，
在页面上明确标记，避免与 ACMI 解析结果混淆。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...acmi_parser import normalize_aircraft
from ...db import utcnow
from ...models import AircraftType, Member, Mission, Sortie
from ...permissions import (
    LOG_APPROVE, LOG_DELETE, LOG_EDIT_ANY, LOG_EDIT_OWN, LOG_VIEW,
)
from ...services.audit import record_audit
from ..deps import Principal, get_db, require
from ..forms import (
    FieldError, local_input_value, parse_float, parse_int, parse_local_datetime,
)
from ..templating import render

log = logging.getLogger(__name__)
router = APIRouter()

#: 能被人工修正的数值字段（字段名 → 中文名），用于审计前后对比
EDITABLE_NUMERIC = {
    "flight_seconds": "飞行时长（秒）",
    "distance_meters": "航程（米）",
    "takeoff_count": "起飞次数",
    "landing_count": "降落次数",
    "weapons_fired": "武器投放",
    "deaths": "战损",
    "kills": "击落",
    "exceedance_count": "超限次数",
}


def _load_sortie(db: Session, sortie_id: str) -> Sortie:
    s = db.get(Sortie, sortie_id)
    if s is None or s.deleted_at is not None:
        raise HTTPException(status_code=404, detail="架次不存在")
    return s


def _may_edit(principal: Principal, sortie: Sortie) -> bool:
    """自己人的架次看 ``LOG_EDIT_OWN``，别人的看 ``LOG_EDIT_ANY``。"""
    if principal.can(LOG_EDIT_ANY):
        return True
    if not principal.can(LOG_EDIT_OWN):
        return False
    me = principal.member.id if principal.member else None
    return me is not None and sortie.member_id == me


def _require_edit(principal: Principal, sortie: Sortie) -> None:
    if not _may_edit(principal, sortie):
        raise HTTPException(
            status_code=403,
            detail="只能编辑自己的架次；修改他人架次需要 log.edit.any 权限")


# ==========================================================================
# 编辑
# ==========================================================================

@router.get("/sorties/{sortie_id}/edit")
def sortie_edit_form(sortie_id: str, request: Request,
                     principal: Principal = Depends(require(LOG_VIEW)),
                     db: Session = Depends(get_db)):
    s = _load_sortie(db, sortie_id)
    _require_edit(principal, s)
    mission = db.get(Mission, s.mission_id)

    members = list(db.scalars(
        select(Member).where(Member.deleted_at.is_(None))
        .order_by(Member.callsign))) if principal.can(LOG_EDIT_ANY) else []

    return render(request, "sorties/form.html", {
        "sortie": s,
        "mission": mission,
        "members": members,
        "can_reassign": principal.can(LOG_EDIT_ANY),
        "form": {
            "member_id": s.member_id or "",
            "aircraft_raw_name": s.aircraft_raw_name or "",
            "flight_seconds": s.flight_seconds or 0,
            "distance_meters": s.distance_meters or 0,
            "takeoff_count": s.takeoff_count or 0,
            "landing_count": s.landing_count or 0,
            "weapons_fired": s.weapons_fired or 0,
            "deaths": s.deaths or 0,
            "kills": s.kills or 0,
            "exceedance_count": s.exceedance_count or 0,
            "takeoff_at": local_input_value(s.takeoff_at),
            "landing_at": local_input_value(s.landing_at),
            "edit_note": s.edit_note or "",
            "manual": s.data_source == "manual",
        },
    })


def _secs_from_hhmm(hours: str, minutes: str, seconds: str) -> int | None:
    """把"时/分"两个输入盒合成秒。三者全空则返回 None（表示未填）。"""
    if not any((hours.strip(), minutes.strip(), seconds.strip())):
        return None
    h = parse_int(hours, "小时", default=0, minimum=0, maximum=1000)
    m = parse_int(minutes, "分钟", default=0, minimum=0, maximum=59)
    ss = parse_int(seconds, "秒", default=0, minimum=0, maximum=59)
    return h * 3600 + m * 60 + ss


@router.post("/sorties/{sortie_id}/edit")
def sortie_update(sortie_id: str, request: Request,
                  member_id: str = Form(""),
                  aircraft_raw_name: str = Form(""),
                  hours: str = Form(""), minutes: str = Form(""),
                  seconds: str = Form(""),
                  distance_nm: str = Form(""),
                  takeoff_count: str = Form("0"),
                  landing_count: str = Form("0"),
                  weapons_fired: str = Form("0"),
                  deaths: str = Form("0"),
                  kills: str = Form("0"),
                  exceedance_count: str = Form("0"),
                  takeoff_at: str = Form(""),
                  landing_at: str = Form(""),
                  edit_note: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(LOG_VIEW)),
                  db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    s = _load_sortie(db, sortie_id)
    _require_edit(principal, s)

    # ---- 归属人（"改归属"）----
    new_member: str | None = s.member_id
    if principal.can(LOG_EDIT_ANY):
        if not member_id.strip():
            new_member = None
        else:
            m = db.get(Member, member_id.strip())
            if m is None or m.deleted_at is not None:
                raise HTTPException(status_code=400, detail="目标成员不存在")
            new_member = m.id

    try:
        secs = _secs_from_hhmm(hours, minutes, seconds)
        # 航程按海里填、按米存（联队口径）
        nm = parse_float(distance_nm, "航程（海里）", minimum=0)
        new_to = parse_local_datetime(takeoff_at, "起飞时间")
        new_ld = parse_local_datetime(landing_at, "降落时间")
        counts = {
            "takeoff_count": parse_int(takeoff_count, "起飞次数",
                                       minimum=0, maximum=100),
            "landing_count": parse_int(landing_count, "降落次数",
                                       minimum=0, maximum=100),
            "weapons_fired": parse_int(weapons_fired, "武器投放",
                                       minimum=0, maximum=10000),
            "deaths": parse_int(deaths, "战损", minimum=0, maximum=100),
            "kills": parse_int(kills, "击落", minimum=0, maximum=1000),
            "exceedance_count": parse_int(exceedance_count, "超限次数",
                                          minimum=0, maximum=10000),
        }
    except FieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if new_to and new_ld and new_ld < new_to:
        raise HTTPException(status_code=400, detail="降落时间不能早于起飞时间")

    before = {
        "member_id": s.member_id,
        "aircraft_raw_name": s.aircraft_raw_name,
        "flight_seconds": s.flight_seconds,
        "distance_meters": s.distance_meters,
        **{k: getattr(s, k) for k in ("takeoff_count", "landing_count",
                                      "weapons_fired", "deaths", "kills",
                                      "exceedance_count")},
    }

    if principal.can(LOG_EDIT_ANY):
        s.member_id = new_member

    raw = aircraft_raw_name.strip()
    if raw and raw != (s.aircraft_raw_name or ""):
        s.aircraft_raw_name = raw
        std, _known = normalize_aircraft(raw)
        # 归一化命中就绑定机型；否则清空（避免留下错误的旧绑定）
        at = db.scalar(select(AircraftType).where(AircraftType.name == std)) \
            if std else None
        s.aircraft_type_id = at.id if at is not None else None

    if secs is not None:
        s.flight_seconds = secs
    if nm is not None:
        s.distance_meters = int(round(nm * 1852))
    for key, value in counts.items():
        setattr(s, key, value)
    if new_to is not None:
        s.takeoff_at = new_to
    if new_ld is not None:
        s.landing_at = new_ld

    # ⚠️ 人工改过的架次不能再标记为"完整解析"—— 可信度必须降级，
    #    否则统计里会把它当成 ACMI 原始结果。
    if s.data_source != "manual":
        s.data_confidence = "estimated"
    s.edited_by = principal.user.id
    s.edit_note = edit_note.strip() or s.edit_note

    db.flush()
    from ...services.ingest import AcmiIngestService
    mission = db.get(Mission, s.mission_id)
    if mission is not None:
        AcmiIngestService.recompute_mission(db, mission)

    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="sorties", target_id=s.id,
                 before=before,
                 after={"member_id": s.member_id,
                        "aircraft_raw_name": s.aircraft_raw_name,
                        "flight_seconds": s.flight_seconds,
                        "distance_meters": s.distance_meters,
                        **{k: getattr(s, k) for k in counts}},
                 reason=edit_note.strip() or "人工修正架次", request=request)
    db.commit()

    log.info("架次 %s 被 %s 人工修正", s.id, principal.display_name)
    return RedirectResponse("/missions/%s?message=架次已更新" % s.mission_id,
                            status_code=303)


@router.post("/sorties/{sortie_id}/delete")
def sortie_delete(sortie_id: str, request: Request,
                  reason: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(LOG_DELETE)),
                  db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    s = _load_sortie(db, sortie_id)
    mission_id = s.mission_id
    s.deleted_at = utcnow()
    db.flush()

    from ...services.ingest import AcmiIngestService
    mission = db.get(Mission, mission_id)
    if mission is not None:
        AcmiIngestService.recompute_mission(db, mission)

    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="sorties", target_id=s.id,
                 before={"raw_pilot_name": s.raw_pilot_name,
                         "flight_seconds": s.flight_seconds},
                 after={"soft_deleted": True},
                 reason=reason.strip() or "删除架次", request=request)
    db.commit()
    return RedirectResponse("/missions/%s?message=架次已删除" % mission_id,
                            status_code=303)


# ==========================================================================
# 手动补录
# ==========================================================================

@router.get("/missions/{mission_id}/sorties/new")
def sortie_new_form(mission_id: str, request: Request,
                    principal: Principal = Depends(require(LOG_APPROVE)),
                    db: Session = Depends(get_db)):
    m = db.get(Mission, mission_id)
    if m is None or m.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return render(request, "sorties/form.html", {
        "sortie": None,
        "mission": m,
        "members": list(db.scalars(
            select(Member).where(Member.deleted_at.is_(None))
            .order_by(Member.callsign))),
        "can_reassign": True,
        "form": {
            "member_id": "", "aircraft_raw_name": "",
            "flight_seconds": 0, "distance_meters": 0,
            "takeoff_count": 1, "landing_count": 1,
            "weapons_fired": 0, "deaths": 0, "kills": 0, "exceedance_count": 0,
            "takeoff_at": local_input_value(m.started_at),
            "landing_at": local_input_value(m.ended_at),
            "edit_note": "", "manual": True,
        },
    })


@router.post("/missions/{mission_id}/sorties/new")
def sortie_create(mission_id: str, request: Request,
                  member_id: str = Form(""),
                  raw_pilot_name: str = Form(""),
                  aircraft_raw_name: str = Form(""),
                  hours: str = Form(""), minutes: str = Form(""),
                  seconds: str = Form(""),
                  distance_nm: str = Form(""),
                  takeoff_count: str = Form("1"),
                  landing_count: str = Form("1"),
                  weapons_fired: str = Form("0"),
                  deaths: str = Form("0"),
                  kills: str = Form("0"),
                  exceedance_count: str = Form("0"),
                  takeoff_at: str = Form(""),
                  landing_at: str = Form(""),
                  edit_note: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(LOG_APPROVE)),
                  db: Session = Depends(get_db)):
    """手动补录一条架次（`data_source='manual'`）。"""
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    m = db.get(Mission, mission_id)
    if m is None or m.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")

    member = None
    if member_id.strip():
        member = db.get(Member, member_id.strip())
        if member is None or member.deleted_at is not None:
            raise HTTPException(status_code=400, detail="目标成员不存在")

    # 未选成员时必须有原始名字，否则无从归属
    raw = (member.callsign if member is not None
           else raw_pilot_name.strip())
    if not raw:
        raise HTTPException(status_code=400,
                            detail="必须选择成员或填写 ACMI 中的名字")

    try:
        secs = _secs_from_hhmm(hours, minutes, seconds) or 0
        nm = parse_float(distance_nm, "航程（海里）", minimum=0)
        new_to = parse_local_datetime(takeoff_at, "起飞时间")
        new_ld = parse_local_datetime(landing_at, "降落时间")
        counts = {
            "takeoff_count": parse_int(takeoff_count, "起飞次数", minimum=0, maximum=100),
            "landing_count": parse_int(landing_count, "降落次数", minimum=0, maximum=100),
            "weapons_fired": parse_int(weapons_fired, "武器投放", minimum=0, maximum=10000),
            "deaths": parse_int(deaths, "战损", minimum=0, maximum=100),
            "kills": parse_int(kills, "击落", minimum=0, maximum=1000),
            "exceedance_count": parse_int(exceedance_count, "超限次数", minimum=0, maximum=10000),
        }
    except FieldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if new_to and new_ld and new_ld < new_to:
        raise HTTPException(status_code=400, detail="降落时间不能早于起飞时间")

    at = None
    std = None
    if aircraft_raw_name.strip():
        std, _k = normalize_aircraft(aircraft_raw_name.strip())
        if std:
            at = db.scalar(select(AircraftType).where(AircraftType.name == std))

    s = Sortie(
        mission_id=m.id,
        member_id=member.id if member is not None else None,
        raw_pilot_name=raw,
        aircraft_type_id=at.id if at is not None else None,
        aircraft_raw_name=aircraft_raw_name.strip() or None,
        takeoff_at=new_to,
        landing_at=new_ld,
        flight_seconds=secs,
        distance_meters=int(round((nm or 0) * 1852)),
        data_source="manual",
        # 手录没有 ACMI 佐证，可信度只能是"估算"
        data_confidence="estimated",
        edited_by=principal.user.id,
        edit_note=edit_note.strip() or "手动补录",
        **counts,
    )
    db.add(s)
    db.flush()

    from ...services.ingest import AcmiIngestService
    AcmiIngestService.recompute_mission(db, m)

    record_audit(db, actor_user_id=principal.user.id, action="create",
                 target_table="sorties", target_id=s.id,
                 after={"mission_id": m.id, "raw_pilot_name": raw,
                        "flight_seconds": secs, "manual": True},
                 reason=edit_note.strip() or "手动补录架次", request=request)
    db.commit()

    log.info("手动补录架次 %s（任务 %s）", s.id, m.id)
    return RedirectResponse("/missions/%s?message=已补录 1 条架次（标记为手动）" % m.id,
                            status_code=303)
