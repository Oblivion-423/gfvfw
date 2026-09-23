"""隐藏的「直接开队员」页 —— ``GET/POST /enroll``。

联队口径
--------
正常流程是 **注册 → 入队申请 → 管理员提升**（见 :mod:`gfvfw.web.routers.apply`
与 :mod:`gfvfw.web.routers.applications`）。但有时需要跳过前两步直接给人开号：

* 老朋友、线下已经确认过身份的人；
* 之前只用 ``/members/new``（或 ``cli create-member --callsign``）
  **登记了名册却没开账号**的成员 —— 这一页可以挑他的呼号直接补一个账号；
* 联队集体入队，一个个走申请太慢。

两种模式
--------
1. **挂到名册已有成员**：从下拉里挑一个**还没有登录账号**的成员，给他开号并绑定。
2. **全新成员**：同时建名册成员 + 账号。

两种模式建出来的账号都是 ``status='active'``（**立即是队员**）、绑定名册、
分配 ``member`` 角色 —— 与「提升为队员」的效果完全一致（同一个角色、同一个
账号状态、同一条审计），只是少了"先注册再申请"这一段。

⚠️⚠️ 这一页**不在任何导航里**，只能靠地址进入
----------------------------------------------
``/enroll`` 不出现在顶栏、首页功能表、任何页面的链接里。但要**说清楚**：
「不在导航」**不是**安全措施 —— 地址固定在代码里，读过源码的人都知道。
真正的防线是下面这条权限判定：

    需要 ``application.review``（指挥 / owner）

之所以用这个权限点而不是 ``member.create``：本页的效果是"把一个人变成队员"，
与 ``/applications`` 的「提升为队员」**完全等价**，所以就该用同一个权限点。
其余身份一律拿不到：未登录访客被送到登录页，游客得到 403 说明页，
普通队员与教官得到 403「权限不足」。

**不要**把这一页加进导航 —— 它是运维入口，不是给全队用的功能。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models import Member, MemberRole, Role, User
from ...permissions import APPLICATION_REVIEW
from ...security import hash_password, password_problem, verify_csrf
from ...services.audit import record_audit
from ...services.naming import (
    callsign_owner, callsign_shadows_username, has_account,
    members_without_account, username_owner, username_shadows_callsign,
)
from ..deps import Principal, get_db, require
from ..templating import render

log = logging.getLogger("gfvfw.web.enroll")

router = APIRouter()

#: 本页固定分配的角色。**刻意不给选择** ——「直接注册为队员」就是 ``member``；
#: 要授指挥/教官请用 ``gfvfw.cli grant-role``（那是 ``system.role.assign``
#: 的事，owner 专属）。在网页上摆一个"选角色"的下拉，等于把提权做成一次误点。
ENROLL_ROLE = "member"

MODE_EXISTING = "existing"      # 挂到名册已有成员上
MODE_NEW = "new"                # 同时建名册成员

CALLSIGN_MAX = 64
USERNAME_MAX = 64


def _ctx(db: Session, **extra) -> dict:
    """本页共用的上下文。"""
    ctx = {
        "candidates": members_without_account(db),
        "role_code": ENROLL_ROLE,
        "callsign_max": CALLSIGN_MAX,
        "username_max": USERNAME_MAX,
        "mode_existing": MODE_EXISTING,
        "mode_new": MODE_NEW,
    }
    ctx.update(extra)
    return ctx


def _redirect(**params: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse("/enroll?" + urlencode(params), status_code=303)


def _member_role(db: Session) -> Role:
    """取 ``member`` 角色。缺失说明基础数据没播种 —— 明确报错。"""
    role = db.scalar(select(Role).where(Role.code == ENROLL_ROLE))
    if role is None:
        raise HTTPException(
            status_code=500,
            detail="基础数据缺失：找不到 %s 角色，请检查是否执行过 seed"
                   % ENROLL_ROLE)
    return role


@router.get("/enroll")
def enroll_form(request: Request,
                principal: Principal = Depends(require(APPLICATION_REVIEW)),
                db: Session = Depends(get_db)):
    """隐藏的开号页（需要 ``application.review``）。"""
    return render(request, "enroll/index.html", _ctx(
        db,
        did=request.query_params.get("did", ""),
        created_username=request.query_params.get("username", ""),
        created_callsign=request.query_params.get("callsign", ""),
        created_mode=request.query_params.get("mode", ""),
    ))


@router.post("/enroll")
def enroll_submit(request: Request,
                  mode: str = Form(MODE_NEW),
                  member_id: str = Form(""),
                  callsign: str = Form(""),
                  username: str = Form(""),
                  password: str = Form(""),
                  confirm_password: str = Form(""),
                  csrf_token: str = Form(""),
                  principal: Principal = Depends(require(APPLICATION_REVIEW)),
                  db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)

    if mode not in (MODE_EXISTING, MODE_NEW):
        mode = MODE_NEW
    callsign = callsign.strip()
    username = username.strip()

    def fail(message: str, status: int = 400):
        return render(request, "enroll/index.html", _ctx(
            db, error=message, mode=mode, member_id=member_id,
            form={"callsign": callsign, "username": username},
        ), status_code=status)

    # ---- 1) 表单本身 ----
    if not username:
        return fail("请填登录名。")
    if len(username) > USERNAME_MAX:
        return fail("登录名过长（最多 %d 个字符）。" % USERNAME_MAX)
    if " " in username:
        return fail("登录名不能含空格。")
    if not password:
        return fail("请设置密码。")
    if password != confirm_password:
        return fail("两次输入的密码不一致。")
    problem = password_problem(password)
    if problem:
        return fail(problem)

    # ---- 2) 名字冲突（两个命名空间都要查）----
    if username_owner(db, username) is not None:
        return fail("登录名「%s」已被占用，换一个。" % username)
    shadow = username_shadows_callsign(db, username)
    if shadow:
        return fail(shadow + "。")

    member: Member | None = None

    if mode == MODE_EXISTING:
        # ---- 3a) 挂到名册已有成员 ----
        if not member_id:
            return fail("请从下拉里选择一位名册成员。")
        member = db.get(Member, member_id)
        if member is None or member.deleted_at is not None:
            return fail("选中的名册成员不存在（可能已被删除）。")
        # ⚠️ 必须在这里**重新**校验，不能只信 GET 时渲染出的下拉列表：
        #    两次请求之间别人可能刚给他开过号 —— 那就变成一个成员两个账号。
        if has_account(db, member.id):
            return fail("成员「%s」已经有登录账号了。"
                        "要改密码请用「账号与密码」，或用 "
                        "gfvfw.cli set-password --username <登录名>。"
                        % member.callsign)
        callsign = member.callsign
    else:
        # ---- 3b) 同时建名册成员 ----
        if not callsign:
            return fail("请填呼号。")
        if len(callsign) > CALLSIGN_MAX:
            return fail("呼号过长（最多 %d 个字符）。" % CALLSIGN_MAX)
        problem = callsign_owner(db, callsign)
        if problem:
            return fail(problem + "。")
        problem = callsign_shadows_username(db, callsign, ignore_username=username)
        if problem:
            return fail(problem + "。")

    # ---- 4) 落库：一个事务里的成员 + 账号 + 角色 + 审计 ----
    role = _member_role(db)          # 缺失直接 500，别写到一半才发现

    try:
        if member is None:
            member = Member(callsign=callsign, status="active")
            db.add(member)
            db.flush()

        user = User(username=username, password_hash=hash_password(password),
                    status="active", member_id=member.id)
        db.add(user)
        db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))

        record_audit(db, principal.user.id, "member.enroll", "users", user.id,
                     after={"callsign": member.callsign, "username": username,
                            "role": ENROLL_ROLE, "account_status": "active",
                            "mode": mode},
                     reason="隐藏页直接开通队员账号（跳过注册与申请）",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except IntegrityError:
        # 并发下两个管理员同时开号 / 名字刚被占用 —— 如实报告，不假装成功
        db.rollback()
        log.warning("直接开号失败（唯一约束）username=%s callsign=%s",
                    username, callsign)
        return fail("开号失败：登录名或呼号刚刚被占用了，请刷新后重试。"
                    "（事务已回滚，不会留下半个成员。）")

    log.info("直接开通队员账号：%s（呼号 %s，模式 %s，操作者 %s）",
             username, member.callsign, mode, principal.display_name)
    return _redirect(did="created", mode=mode, username=quote(username, safe=""),
                     callsign=quote(member.callsign, safe=""))
