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
* 账号解绑 ``POST /members/{id}/unbind``：``MEMBER_DELETE``
  —— 拆开"登录账号"与"名册成员"的绑定，账号降为游客（详见函数 docstring）
* 作废 / 恢复 ``POST /members/{id}/delete|restore``：``MEMBER_DELETE``
  —— 软删除；作废时**同时停用绑定的登录账号**（否则删了成员却没删掉访问权）
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import (
    Member, MemberQualification, MemberRole, Mission, Qualification, Rank,
    Role, Sortie, User,
)
from ...models.identity import USER_STATUS_LABELS
from ...permissions import (
    LOGBOOK_UPLOAD_ANY, MEMBER_CREATE, MEMBER_DELETE, MEMBER_EDIT,
    MEMBER_EDIT_RANK,
)
from ...security import verify_csrf
from ...services import logbook as LB
from ...services.audit import record_audit
from ...services.naming import callsign_owner
from ..deps import (
    Principal, get_db, require, require_login, require_member,
)
from ..templating import render

log = logging.getLogger("gfvfw.web.members")

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

#: **登录账号**状态的徽章色（与上面的名册状态无关）。
#: ``active``=队员可登录；``pending``=游客；``suspended``=已停用、不允许登录。
ACCOUNT_STATUS_BADGE = {
    "active": "ok",
    "pending": "warn",
    "suspended": "danger",
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
        # 登录账号状态（与名册状态是两码事：一个账号可以是 active 而名册成员
        # 状态是 reserve，反之亦然）
        "account_status_labels": USER_STATUS_LABELS,
        "account_status_badge": ACCOUNT_STATUS_BADGE,
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
                deleted: str = "",
                principal: Principal = Depends(require_login),
                db: Session = Depends(get_db)):
    """名册列表。

    ``?deleted=1`` 显示**已作废**的成员（只给有 MEMBER_DELETE 的人看）——
    没有这个视图的话，"作废"在界面上就是不可逆的，而提示里写着"可恢复"。
    """
    show_deleted = deleted in ("1", "true", "yes") and principal.can(MEMBER_DELETE)

    stmt = select(Member).where(
        Member.deleted_at.is_not(None) if show_deleted else Member.deleted_at.is_(None))
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
        "show_deleted": show_deleted,
        "can_manage": principal.can(MEMBER_CREATE),
        "can_delete": principal.can(MEMBER_DELETE),
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
        # 登录账号（本页要显示它，并给出「解绑」入口）
        "account": _bound_account(db, member.id),
        # 作废/解绑是同一个权限点（都能撤掉一个人的访问权）
        "can_delete": principal.can(MEMBER_DELETE),
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

    # 呼号唯一 —— 走公共服务（不区分大小写、排除已软删、也看未撤销的申请）。
    # ⚠️ 以前这里是 `Member.callsign == callsign`（**区分大小写**），
    #    于是 "viper" 能绕过 "Viper" 的唯一性，而 ACMI 归并是按呼号认人的。
    problem = callsign_owner(db, callsign)
    if problem:
        return fail(problem + "。")

    # 这里**曾经**还禁止"呼号与某个已有登录名相同"（跨命名空间）。
    # 已按联队口径取消：用呼号当登录名是常规做法。详见
    # gfvfw.services.naming 的模块 docstring。
    # 呼号自身的唯一性（上面那条）**不变** —— 名册重名会让 ACMI 归并认错人。

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
# 账号绑定：解绑 / 查看
# --------------------------------------------------------------------------

def _bound_account(db: Session, member_id: str):
    """该成员当前绑定的登录账号（没有则 ``None``）。

    ⚠️ 一对一：一个成员最多一个账号。数据库层面靠 ``users.member_id`` 上的
       唯一约束保证（``member_id`` 可空但非空时唯一）。
    """
    return db.scalar(select(User).where(User.member_id == member_id).limit(1))


def _owner_lockout(db: Session, member: Member, account: Optional[User]) -> Optional[str]:
    """这次操作会不会让系统**一个能用的 owner 都不剩**？是就返回可读原因。

    这不是权限判定，是**防止不可逆的运维事故**：把最后一个 owner 删掉或解绑之后，
    界面上再没有任何人能授权（连"提升别人"都做不到），只能上服务器用
    ``gfvfw.cli grant-role`` 救回来。

    只在目标**确实是一个能用的 owner**时才拦 —— 判定条件是
    「挂着未撤销的 owner 角色」**且**「绑着一个 status=active 的账号」。
    光有 owner 角色但没有账号（或账号已停用）的成员，删掉它不会让任何人失去
    登录能力，不该被拦。
    """
    if account is None or account.status != "active":
        return None
    owner = db.scalar(select(Role).where(Role.code == "owner"))
    if owner is None:
        return None
    holds = db.scalar(
        select(MemberRole.id)
        .where(MemberRole.member_id == member.id,
               MemberRole.role_id == owner.id,
               MemberRole.revoked_at.is_(None))
        .limit(1))
    if holds is None:
        return None

    others = db.scalar(
        select(func.count(func.distinct(MemberRole.member_id)))
        .select_from(MemberRole)
        .join(Member, Member.id == MemberRole.member_id)
        .join(User, User.member_id == Member.id)
        .where(MemberRole.role_id == owner.id,
               MemberRole.revoked_at.is_(None),
               Member.id != member.id,
               Member.deleted_at.is_(None),
               User.status == "active"))
    if others:
        return None
    return ("「%s」是最后一个**能登录的 owner** —— 动它之后界面上再没有人能授权"
            "（只剩服务器上的 gfvfw.cli 可用）。请先给另一个账号授予 owner 角色。"
            % member.callsign)


@router.post("/{member_id}/unbind")
def member_unbind_account(member_id: str, request: Request,
                          csrf_token: str = Form(""),
                          principal: Principal = Depends(require(MEMBER_DELETE)),
                          db: Session = Depends(get_db)):
    """把登录账号从名册成员上**解绑**。

    为什么需要它：绑错了账号（把 A 的账号绑到了 B 上）、或者某个人不该再以
    队员身份登录，都需要一条"只拆链接、不删任何数据"的路子。

    解绑后的账号是**游客**（``status='pending'``）：仍能登录、仍能看各区块的
    列表与汇总，但**不再有任何队内权限**。为什么不保留 ``active``：

    * ``Principal.is_member`` 只看 ``users.status``，不看有没有成员行 ——
      留着 ``active`` 的话，这个账号在 ``require_member`` 眼里**仍然是队员**，
      能进所有详情页，只是恰好没权限点。那是个自相矛盾的半成品状态；
    * ``load_principal`` 还有一条"已激活但没角色 → 兜底给 member"的逻辑
      （防止"激活了却什么都看不到"）。不改成 pending 的话，解绑反而会
      **给它 member 的全部权限** —— 解绑等于没解。

    ⚠️ **不动该成员的角色分配**。角色属于**名册成员**（``member_roles``
    挂在 ``member_id`` 上），是这个人的队内身份，不是某个登录账号的属性。
    所以：绑错账号 → 解绑 → 再绑正确的账号，指挥官权限会正确地跟着成员回来；
    反之，如果解绑就把角色清掉，一次误操作就得重新授一遍权。
    """
    verify_csrf(request, csrf_token)

    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    account = _bound_account(db, member.id)
    if account is None:
        raise HTTPException(
            status_code=400,
            detail="该成员没有绑定登录账号，无需解绑。")

    # 自锁防护：解绑自己的账号 = 立刻把自己的权限全下掉（下一个请求就 403 了）
    if principal.user is not None and account.id == principal.user.id:
        raise HTTPException(
            status_code=400,
            detail="不能用当前登录的账号给自己解绑 —— 那样你会立刻失去权限。"
                   "请用另一个管理员账号操作，或先给别的账号授予 owner 角色。")
    lockout = _owner_lockout(db, member, account)
    if lockout:
        raise HTTPException(status_code=400, detail=lockout)

    before = {"username": account.username, "status": account.status,
              "member_id": account.member_id}
    account.member_id = None
    account.status = "pending"

    record_audit(db, actor_user_id=principal.user.id,
                 action="member.unbind", target_table="users",
                 target_id=account.id,
                 before=before,
                 after={"username": account.username, "status": account.status,
                        "member_id": None, "callsign": member.callsign},
                 reason="把登录账号从名册成员上解绑（账号降为游客）",
                 request=request)
    db.commit()
    log.info("解绑账号 %s ← 成员 %s（操作者 %s）",
             account.username, member.callsign, principal.display_name)

    return RedirectResponse(
        "/members/%s?message=%s" % (
            member.id,
            quote("已把登录账号「%s」从该成员上解绑；"
                  "该账号现在是游客（能登录，但没有队内权限）。"
                  "成员的角色分配保持不变。" % account.username)),
        status_code=303)


# --------------------------------------------------------------------------
# 软删除 / 恢复
# --------------------------------------------------------------------------

@router.post("/{member_id}/delete")
def member_delete(member_id: str, request: Request,
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(MEMBER_DELETE)),
                  db: Session = Depends(get_db)):
    """作废名册成员（软删除），**并停用其绑定的登录账号**。"""
    verify_csrf(request, csrf_token)

    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=404, detail="成员不存在")

    # 自我删除防护：别把最后一个 owner 自己删掉，那是不可逆的运维事故。
    me = principal.member
    if me is not None and me.id == member.id:
        raise HTTPException(
            status_code=400,
            detail="不能作废你自己所在的成员记录 —— 请先用另一个管理员账号操作。")

    account = _bound_account(db, member.id)
    lockout = _owner_lockout(db, member, account)
    if lockout:
        raise HTTPException(status_code=400, detail=lockout)

    # ⚠️ **必须同时处理绑定账号**。
    #    只把 members 行标作废的话，那个人照样能登录、照样是队员
    #    （deps.load_principal 的说明里写了这个曾经真实存在过的口子）。
    #    这里是第二道：把账号停用（suspended ⇒ 不允许登录）。
    account_before = None
    if account is not None:
        account_before = {"username": account.username, "status": account.status}
        account.status = "suspended"

    # ✅ 强制软删除（R11）：标记作废，可恢复，不真删
    member.deleted_at = utcnow()
    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="members", target_id=member.id,
                 before={"callsign": member.callsign, "account": account_before},
                 after={"deleted": True,
                        "account_status": account.status if account else None},
                 reason=("软删除成员（同时停用其登录账号）" if account is not None
                         else "软删除成员（该成员没有登录账号）"),
                 request=request)
    db.commit()
    log.info("作废成员 %s（账号 %s，操作者 %s）", member.callsign,
             account.username if account else "无", principal.display_name)

    msg = "已作废成员「%s」。记录仍在库里（不是真删）。" % member.callsign
    if account is not None:
        msg += "其登录账号「%s」已一并停用，无法再登录。" % account.username
    return RedirectResponse("/members?message=" + quote(msg), status_code=303)


@router.post("/{member_id}/restore")
def member_restore(member_id: str, request: Request,
                   csrf_token: str = Form(""),
                   principal: Principal = Depends(require(MEMBER_DELETE)),
                   db: Session = Depends(get_db)):
    """恢复被作废的成员（并把它被停用的登录账号恢复为队员）。"""
    verify_csrf(request, csrf_token)

    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if member.deleted_at is None:
        raise HTTPException(status_code=400, detail="该成员没有被作废，无需恢复。")

    account = _bound_account(db, member.id)
    restored_account = None
    if account is not None and account.status == "suspended":
        # ⚠️ 只在**确实是停用**时恢复成 active，且动作在按钮文案里写明了。
        #    被停用 + 还绑在成员上的账号，来源只有"作废成员"这一条
        #    （拒绝申请停用的是还没绑定成员的游客账号）。
        account.status = "active"
        restored_account = account.username

    member.deleted_at = None
    record_audit(db, actor_user_id=principal.user.id, action="restore",
                 target_table="members", target_id=member.id,
                 after={"callsign": member.callsign,
                        "account_reactivated": restored_account},
                 reason="恢复被作废的成员", request=request)
    db.commit()

    msg = "已恢复成员「%s」。" % member.callsign
    if restored_account:
        msg += "其登录账号「%s」已恢复为队员。" % restored_account
    return RedirectResponse("/members/%s?message=%s"
                            % (member.id, quote(msg)), status_code=303)
