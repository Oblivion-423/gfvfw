"""
成员名册路由。

权限
----
* 名册列表 ``GET /members``：``require_login`` —— **游客也能看**
  （呼号、军衔、飞行时长等汇总列；这是联队明确要求的"公开部分"）
* 成员详情 ``GET /members/{id}``：``require_member`` —— 仅队员
  （含角色、档案可见性、Logbook 归档与管理入口）
* 增删改：``MEMBER_CREATE`` / ``MEMBER_EDIT`` / ``MEMBER_DELETE``
* 军衔变更：``MEMBER_EDIT_RANK``（敏感，单独一个权限点）
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import (
    Member, MemberQualification, MemberRole, Mission, Qualification, Rank,
    Role, Sortie,
)
from ...permissions import (
    LOGBOOK_UPLOAD_ANY, MEMBER_CREATE, MEMBER_DELETE, MEMBER_EDIT,
    MEMBER_EDIT_RANK,
)
from ...services import logbook as LB
from ...services.audit import record_audit
from ..deps import (
    Principal, get_db, require, require_login, require_member,
)
from ..templating import render

router = APIRouter(prefix="/members")

STATUS_LABELS = {
    "active": "现役",
    "reserve": "休整",
    "retired": "退役",
    "probation": "预备",
}
STATUS_BADGE = {
    "active": "ok",
    "reserve": "warn",
    "retired": "",
    "probation": "accent",
}
VISIBILITY_LABELS = {
    "public": "公开（任何人可见）",
    "members": "内部（仅成员可见）",
    "command": "指挥层（仅管理层可见）",
}
CONFIDENCE_LABELS = {
    "exact": "完整",
    "partial": "部分",
    "estimated": "估算",
}
CONFIDENCE_BADGE = {
    "exact": "ok",
    "partial": "warn",
    "estimated": "danger",
}
SOURCE_LABELS = {
    "acmi": "ACMI",
    "manual": "手工录入",
    "self_reported": "成员自填",
    "logbook": "Logbook",
}


def _common_context() -> dict:
    return {
        "status_labels": STATUS_LABELS,
        "status_badge": STATUS_BADGE,
        "visibility_labels": VISIBILITY_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "confidence_badge": CONFIDENCE_BADGE,
        "source_labels": SOURCE_LABELS,
    }


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------

@router.get("")
def member_list(request: Request,
                q: str = "",
                status: str = "",
                principal: Principal = Depends(require_login),
                db: Session = Depends(get_db)):

    stmt = select(Member).where(Member.deleted_at.is_(None))
    if q.strip():
        needle = "%%%s%%" % q.strip()
        # ⚠️ 只按呼号搜。队号（service_number）已从界面移除，见 member_create 的说明；
        #    按一个页面上根本看不见的字段做匹配，只会让搜索结果显得莫名其妙。
        stmt = stmt.where(Member.callsign.ilike(needle))
    if status.strip():
        stmt = stmt.where(Member.status == status.strip())
    stmt = stmt.order_by(Member.status, Member.callsign)

    members = list(db.scalars(stmt))

    # 架次与时长汇总（一次查询取回，避免 N+1）
    stats: dict[str, tuple[int, int]] = {}
    if members:
        ids = [m.id for m in members]
        rows = db.execute(
            select(Sortie.member_id,
                   func.count(Sortie.id),
                   func.coalesce(func.sum(Sortie.flight_seconds), 0))
            .where(Sortie.member_id.in_(ids), Sortie.deleted_at.is_(None))
            .group_by(Sortie.member_id)
        ).all()
        stats = {r[0]: (r[1], int(r[2])) for r in rows}

    rows = [{"member": m,
             "sortie_count": stats.get(m.id, (0, 0))[0],
             "flight_seconds": stats.get(m.id, (0, 0))[1]}
            for m in members]

    return render(request, "members/list.html", {
        **_common_context(),
        "members": rows,
        "total": len(rows),
        "q": q,
        "status_filter": status,
        "can_manage": principal.can(MEMBER_CREATE),
    })


# --------------------------------------------------------------------------
# 新建 / 编辑
#
# ⚠️ 路由注册顺序很重要：``/members/new`` 必须**先于** ``/members/{member_id}``
#    注册，否则 "new" 会被当成 member_id 匹配，导致新建成 404。
#    FastAPI 按注册顺序匹配，因此本段放在详情路由之前。
# --------------------------------------------------------------------------

@router.get("/new")
def member_new_form(request: Request,
                    principal: Principal = Depends(require(MEMBER_CREATE)),
                    db: Session = Depends(get_db)):
    return render(request, "members/form.html", {
        **_common_context(),
        "member": None,
        "form": {"status": "active", "visibility": "public", "joined_at": ""},
        "ranks": list(db.scalars(select(Rank).where(Rank.is_active.is_(True))
                                 .order_by(Rank.level))),
    })


# --------------------------------------------------------------------------
# 详情
# --------------------------------------------------------------------------

@router.get("/{member_id}")
def member_detail(member_id: str, request: Request,
                  principal: Principal = Depends(require_member),
                  db: Session = Depends(get_db)):

    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    # 架次汇总
    agg = db.execute(
        select(func.count(Sortie.id),
               func.coalesce(func.sum(Sortie.flight_seconds), 0),
               func.coalesce(func.sum(Sortie.distance_meters), 0),
               func.coalesce(func.sum(Sortie.takeoff_count), 0),
               func.coalesce(func.sum(Sortie.landing_count), 0),
               func.coalesce(func.sum(Sortie.weapons_fired), 0))
        .where(Sortie.member_id == member.id, Sortie.deleted_at.is_(None))
    ).one()

    sortie_rows = db.execute(
        select(Sortie, Mission.name)
        .join(Mission, Mission.id == Sortie.mission_id, isouter=True)
        .where(Sortie.member_id == member.id, Sortie.deleted_at.is_(None))
        .order_by(Sortie.takeoff_at.desc())
        .limit(50)
    ).all()

    from ...acmi_parser import normalize_aircraft
    missions = []
    for s, mission_name in sortie_rows:
        std, _ = normalize_aircraft(s.aircraft_raw_name)
        missions.append({
            "takeoff_at": s.takeoff_at,
            "mission_name": mission_name or "（未命名任务）",
            "aircraft": std,
            "aircraft_raw_name": s.aircraft_raw_name,
            "flight_seconds": s.flight_seconds,
            "distance_meters": s.distance_meters,
            "data_confidence": s.data_confidence,
        })

    quals = db.execute(
        select(Qualification.name, Qualification.category,
               MemberQualification.granted_at, MemberQualification.source)
        .join(MemberQualification,
              MemberQualification.qualification_id == Qualification.id)
        .where(MemberQualification.member_id == member.id,
               MemberQualification.revoked_at.is_(None))
        .order_by(Qualification.category, Qualification.name)
    ).all()

    role_codes = db.execute(
        select(Role.code).join(MemberRole, MemberRole.role_id == Role.id)
        .where(MemberRole.member_id == member.id, MemberRole.revoked_at.is_(None))
    ).scalars().all()

    stats = {
        "sortie_count": agg[0] or 0,
        "flight_seconds": int(agg[1] or 0),
        "distance_meters": int(agg[2] or 0),
        "takeoffs": int(agg[3] or 0),
        "landings": int(agg[4] or 0),
        "weapons_fired": int(agg[5] or 0),
        "missions": missions,
    }
    qualifications = [{"name": q[0], "category": q[1],
                       "granted_at": q[2], "source": q[3]} for q in quals]

    return render(request, "members/detail.html", {
        **_common_context(),
        "member": member,
        "stats": stats,
        "qualifications": qualifications,
        "roles": list(role_codes),
        "can_manage": principal.can(MEMBER_EDIT),
        # Logbook 归档（本页只显示摘要 + 入口，完整操作在专用页面）
        "logbook_count": len(LB.list_for_member(db, member.id)),
        "logbook_applied": LB.applied_for_member(db, member.id) is not None,
        "can_manage_logbook": principal.can(LOGBOOK_UPLOAD_ANY),
    })


# --------------------------------------------------------------------------
# 新建（POST）/ 编辑
# --------------------------------------------------------------------------

@router.post("/new")
def member_create(request: Request,
                  callsign: str = Form(...),
                  status: str = Form("active"),
                  rank_id: str = Form(""),
                  joined_at: str = Form(""),
                  left_at: str = Form(""),
                  visibility: str = Form("public"),
                  bio: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(MEMBER_CREATE)),
                  db: Session = Depends(get_db)):
    """新建成员。

    ⚠️ 队号（``service_number``）已**从界面移除**：联队不用它，而它此前
    只在新建/编辑表单里出现，详情页与列表页从不显示 —— 是个"只能写、看不见"
    的字段。数据库列保留（见 docs/database-design.md），已有取值原样留存，
    但接口不再接受该字段，也不做唯一性校验。
    """
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    callsign = callsign.strip()
    form = {"callsign": callsign,
            "status": status, "rank_id": rank_id, "joined_at": joined_at,
            "left_at": left_at, "visibility": visibility, "bio": bio}

    def fail(msg: str):
        return render(request, "members/form.html", {
            **_common_context(), "member": None, "form": form, "error": msg,
            "ranks": list(db.scalars(select(Rank).where(Rank.is_active.is_(True))
                                     .order_by(Rank.level))),
        }, status_code=400)

    if not callsign:
        return fail("呼号不能为空。")
    if len(callsign) > 64:
        return fail("呼号过长（上限 64 字符）。")

    exists = db.scalar(select(Member).where(Member.callsign == callsign))
    if exists is not None:
        return fail("呼号「%s」已被使用，呼号必须唯一。" % callsign)

    if status not in STATUS_LABELS:
        return fail("状态取值不合法。")
    if visibility not in VISIBILITY_LABELS:
        return fail("可见性取值不合法。")

    # 军衔变更需要单独权限
    if rank_id and not principal.can(MEMBER_EDIT_RANK):
        return fail("你没有设置军衔的权限。")

    m = Member(
        callsign=callsign,
        status=status,
        rank_id=rank_id or None,
        rank_source="manual",
        rank_updated_at=utcnow() if rank_id else None,
        rank_updated_by=principal.user.id if rank_id else None,
        joined_at=_parse_date(joined_at) or utcnow(),
        left_at=_parse_date(left_at),
        visibility=visibility,
        bio=bio.strip() or None,
    )
    db.add(m)
    db.flush()

    record_audit(db, actor_user_id=principal.user.id, action="create",
                 target_table="members", target_id=m.id,
                 after={"callsign": callsign, "status": status},
                 reason="新建成员", request=request)
    db.commit()

    return RedirectResponse("/members/%s" % m.id, status_code=303)


@router.get("/{member_id}/edit")
def member_edit_form(member_id: str, request: Request,
                     principal: Principal = Depends(require(MEMBER_EDIT)),
                     db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return render(request, "members/form.html", {
        **_common_context(),
        "member": member,
        "form": {
            "callsign": member.callsign,
            "status": member.status,
            "rank_id": member.rank_id or "",
            "joined_at": member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "",
            "left_at": member.left_at.strftime("%Y-%m-%d") if member.left_at else "",
            "visibility": member.visibility,
            "bio": member.bio or "",
        },
        "ranks": list(db.scalars(select(Rank).where(Rank.is_active.is_(True))
                                 .order_by(Rank.level))),
    })


@router.post("/{member_id}/edit")
def member_update(member_id: str, request: Request,
                  callsign: str = Form(...),
                  status: str = Form("active"),
                  rank_id: str = Form(""),
                  joined_at: str = Form(""),
                  left_at: str = Form(""),
                  visibility: str = Form("public"),
                  bio: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(MEMBER_EDIT)),
                  db: Session = Depends(get_db)):

    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    before = {"callsign": member.callsign, "status": member.status,
              "rank_id": member.rank_id, "visibility": member.visibility}

    callsign = callsign.strip()
    if not callsign:
        raise HTTPException(status_code=400, detail="呼号不能为空")

    dup = db.scalar(select(Member).where(Member.callsign == callsign,
                                        Member.id != member.id))
    if dup is not None:
        raise HTTPException(status_code=400,
                            detail="呼号「%s」已被使用" % callsign)

    # 军衔变更单独校验权限
    new_rank = rank_id or None
    if new_rank != member.rank_id:
        if not principal.can(MEMBER_EDIT_RANK):
            raise HTTPException(status_code=403, detail="你没有修改军衔的权限")
        member.rank_source = "manual"
        member.rank_updated_at = utcnow()
        member.rank_updated_by = principal.user.id

    member.callsign = callsign
    # ⚠️ 刻意**不**再写 member.service_number。
    #    表单已无该字段，若沿用 `= service_number.strip() or None`，
    #    每次编辑都会把库里已有的队号清成 NULL —— 静默数据丢失。
    #    队号现为只读的历史列，由数据库保留原值。
    member.status = status
    member.rank_id = new_rank
    member.joined_at = _parse_date(joined_at) or member.joined_at
    member.left_at = _parse_date(left_at)
    member.visibility = visibility
    member.bio = bio.strip() or None

    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="members", target_id=member.id,
                 before=before,
                 after={"callsign": member.callsign, "status": member.status,
                        "rank_id": member.rank_id, "visibility": member.visibility},
                 reason="编辑成员", request=request)
    db.commit()

    return RedirectResponse("/members/%s" % member.id, status_code=303)


# --------------------------------------------------------------------------
# 软删除
# --------------------------------------------------------------------------

@router.post("/{member_id}/delete")
def member_delete(member_id: str, request: Request,
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(MEMBER_DELETE)),
                  db: Session = Depends(get_db)):

    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    # ✅ 强制软删除（R11）：标记作废，可恢复，不真删
    member.deleted_at = utcnow()
    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="members", target_id=member.id,
                 before={"callsign": member.callsign},
                 reason="软删除成员", request=request)
    db.commit()

    return RedirectResponse("/members?message=已作废该成员记录（可恢复）", status_code=303)
