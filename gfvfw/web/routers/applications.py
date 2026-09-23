"""管理员审批：把**游客提升为队员**（需求 §4.7 入队流水线）。

为什么以「账号」而不是「申请」为主体
------------------------------------
联队要的是"管理员把游客提升上来"。游客的载体是**账号**（``users``，
``status='pending'``）—— 申请记录只是他当初填的表。

按账号做审批有三个好处：

1. 管理员**手工建的**游客账号（没走申请表）也能被提升，不会漏掉；
2. 一个账号可能有多份申请（重复提交），提升哪一份不会搞错人；
3. 提升的语义只有一个：**让这个账号成为队员**。

提升做四件事（同一事务）
------------------------
1. 建（或复用）名册成员 :class:`~gfvfw.models.identity.Member`；
2. 给成员分配 ``member`` 角色；
3. ``users.member_id`` 指向该成员、``users.status = 'active'``；
4. 关联的 :class:`~gfvfw.models.identity.Application` 标为 ``activated``。

⚠️ 第 3 步是**权限的开关**：``load_principal`` 用白名单判定，
只有 ``active`` 才拿得到权限点。所以"提升"不是加一个标记，
而是真的把队内内容开放给他。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import Application, Member, MemberRole, Role, User
from ...permissions import APPLICATION_REVIEW
from ...security import verify_csrf
from ...services.audit import record_audit
from ..deps import Principal, get_db, require
from ..templating import render

log = logging.getLogger("gfvfw.web.applications")

router = APIRouter()


def _member_role(db: Session) -> Role:
    """取 ``member`` 角色。缺失说明基础数据没播种 —— 明确报错。"""
    role = db.scalar(select(Role).where(Role.code == "member"))
    if role is None:
        raise HTTPException(
            status_code=500,
            detail="基础数据缺失：找不到 member 角色，请检查是否执行过 seed")
    return role


# --------------------------------------------------------------------------
# 待审批列表
# --------------------------------------------------------------------------

@router.get("/applications")
def index(request: Request,
          principal: Principal = Depends(require(APPLICATION_REVIEW)),
          db: Session = Depends(get_db)):
    """待审批的游客账号列表（含他们提交的申请内容）。"""
    # 游客 = 已登录但未激活的账号。这里按账号列，保证手工建的游客也出现。
    pending_users = list(db.scalars(
        select(User).where(User.status == "pending")
        .order_by(User.created_at)).all())

    # 每个游客带上他最新的一份申请（没有则为 None）
    apps_by_user: dict[str, Application] = {}
    if pending_users:
        ids = [u.id for u in pending_users]
        for row in db.scalars(
                select(Application)
                .where(Application.resulting_user_id.in_(ids))
                .order_by(Application.created_at.desc())).all():
            apps_by_user.setdefault(row.resulting_user_id, row)

    rows = [{"user": u, "application": apps_by_user.get(u.id)}
            for u in pending_users]

    # 已处理（最近若干条），便于回看谁被提升/拒绝了
    # ⚠️ 不用 NULLS LAST —— SQLite 只有 3.30+ 才支持，而本项目强制可移植性
    #    （services/schema_sync.py::check_portability）。按创建时间倒序足够。
    recent = list(db.scalars(
        select(Application)
        .where(Application.status.in_(("activated", "rejected")))
        .order_by(Application.created_at.desc())
        .limit(20)).all())

    active_count = db.scalar(
        select(func.count()).select_from(User)
        .where(User.status == "active")) or 0

    return render(request, "applications/index.html", {
        "rows": rows,
        "recent": recent,
        "active_count": active_count,
        "did": request.query_params.get("did", ""),
        "error": request.query_params.get("error", ""),
    })


# --------------------------------------------------------------------------
# 提升为队员
# --------------------------------------------------------------------------

@router.post("/applications/{user_id}/promote")
def promote(user_id: str, request: Request,
            callsign: str = Form(""),
            csrf_token: str = Form(""),
            principal: Principal = Depends(require(APPLICATION_REVIEW)),
            db: Session = Depends(get_db)):
    """**把游客提升为队员** —— 建名册、给角色、激活账号。"""
    verify_csrf(request, csrf_token)

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    app_row = db.scalar(
        select(Application)
        .where(Application.resulting_user_id == user.id)
        .order_by(Application.created_at.desc()).limit(1))

    # 呼号：优先用管理员在表单里填的（默认已填申请表里的），否则申请里的
    desired = (callsign or "").strip() \
        or (app_row.desired_callsign if app_row else "") \
        or user.username

    def fail(message: str):
        return RedirectResponse(
            "/applications?error=%s" % _q(message), status_code=303)

    if user.status == "active":
        return fail("账号 %s 已经是队员了。" % user.username)
    if user.status == "suspended":
        return fail("账号 %s 已被停用，请先恢复状态再提升。" % user.username)
    if len(desired) > 64:
        return fail("呼号过长。")

    # 呼号唯一性：名册里不能有重名（重名会让 ACMI 归并认错人）
    clash = db.scalar(
        select(Member).where(func.lower(Member.callsign) == desired.lower(),
                             Member.deleted_at.is_(None)).limit(1))
    if clash is not None:
        return fail("呼号「%s」已被名册成员占用，请换一个。" % desired)

    role = _member_role(db)

    before = {"username": user.username, "status": user.status,
              "member_id": user.member_id}

    try:
        member = Member(callsign=desired, status="active")
        db.add(member)
        db.flush()

        db.add(MemberRole(member_id=member.id, role_id=role.id))

        user.member_id = member.id
        user.status = "active"

        if app_row is not None:
            app_row.status = "activated"
            app_row.reviewed_by = principal.user.id
            app_row.reviewed_at = utcnow()
            app_row.review_note = "管理员提升为队员（呼号 %s）" % desired

        record_audit(db, principal.user.id, "application.promote", "users",
                     user.id,
                     before=before,
                     after={"username": user.username, "status": user.status,
                            "member_id": member.id, "callsign": desired},
                     reason="把游客提升为队员",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except IntegrityError:
        # 并发下两个管理员同时提升 / 同名成员刚被建 —— 如实报告
        db.rollback()
        log.warning("提升游客失败（唯一约束）user=%s callsign=%s",
                    user.username, desired)
        return fail("提升失败：呼号「%s」或账号绑定刚刚被占用，请刷新后重试。"
                    % desired)

    log.info("游客已提升为队员：%s → 呼号 %s（操作者 %s）",
             user.username, desired, principal.display_name)
    return RedirectResponse("/applications?did=promoted", status_code=303)


# --------------------------------------------------------------------------
# 拒绝
# --------------------------------------------------------------------------

@router.post("/applications/{user_id}/reject")
def reject(user_id: str, request: Request,
           note: str = Form(""),
           csrf_token: str = Form(""),
           principal: Principal = Depends(require(APPLICATION_REVIEW)),
           db: Session = Depends(get_db)):
    """拒绝申请：账号转为**停用**（``suspended`` 不允许登录）。

    ⚠️ 刻意**不删账号** —— 删了就没有"这个人申请过"的痕迹，
    而且同一 IP 可以换个名字继续刷。停用 + 留档更便于事后追查。
    """
    verify_csrf(request, csrf_token)

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if user.status == "active":
        return RedirectResponse(
            "/applications?error=%s" % _q("该账号已是队员，不能用这里拒绝。"),
            status_code=303)

    note = (note or "").strip()[:1000]
    before = {"username": user.username, "status": user.status}

    user.status = "suspended"
    rows = list(db.scalars(
        select(Application)
        .where(Application.resulting_user_id == user.id,
               Application.status.notin_(("activated",)))).all())
    for row in rows:
        row.status = "rejected"
        row.reviewed_by = principal.user.id
        row.reviewed_at = utcnow()
        row.review_note = note or None

    record_audit(db, principal.user.id, "application.reject", "users", user.id,
                 before=before, after={"username": user.username,
                                       "status": user.status},
                 reason=("拒绝入队申请：%s" % note) if note else "拒绝入队申请",
                 actor_role=principal.primary_role, request=request)
    db.commit()

    log.info("入队申请被拒绝：%s（操作者 %s）", user.username,
             principal.display_name)
    return RedirectResponse("/applications?did=rejected", status_code=303)


def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text[:200], safe="")
