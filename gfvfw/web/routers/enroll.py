"""「直接开队员」页 —— ``GET/POST /enroll``。

联队口径（**本页公开，不需要任何权限**）
----------------------------------------
正常流程是 **注册 → 入队申请 → 管理员提升**（见 :mod:`gfvfw.web.routers.apply`
与 :mod:`gfvfw.web.routers.applications`）。但联队要求有一条"一句话就进队"的
路子：把链接发给本人，他自己建号、**建完立刻是队员**。所以：

* 本页**不需要登录、不需要任何权限点**（与 ``/register`` 同级公开）；
* 登录名**可以与名册里的呼号相同** —— 联队本来就用呼号当登录名。
  两个命名空间不再互相禁止，详见 :mod:`gfvfw.services.naming`。

⚠️⚠️ 公开意味着**链接本身就是凭证**
-----------------------------------
这一点必须说透，别自欺欺人：

* 这个页面**能一步造出 ``status='active'`` 的队员账号** —— 拿到链接的人
  不需要任何审批就获得队内全部内容的可见性与写操作权限；
* 它**不在任何导航里**（不在顶栏、首页、任何页面的链接里），但"不在导航"
  **不是**安全措施：地址固定在代码里，读过源码的人都知道；
* 所以：**链接泄漏 = 一个队员名额泄漏**。要发给谁、发到哪个群，请自己拿捏。
  这也正是它与"需要 ``application.review``"的区别 —— 后者泄漏链接无害。

要收回公开（恢复"需要指挥/owner 权限"），把下面这行写进 ``/etc/gfvfw/env``
并重启即可，**不需要改代码**：

    GFVFW_ENROLL_OPEN=false

两种模式
--------
1. **挂到名册已有成员**：从下拉里挑一个**还没有登录账号**的成员，给他开号并绑定。
2. **全新成员**：同时建名册成员 + 账号。

两种模式建出来的账号都是 ``status='active'``（**立即是队员**）、绑定名册、
分配 ``member`` 角色 —— 与「提升为队员」的效果完全一致（同一个角色、同一个
账号状态、同一条审计），只是少了"先注册再申请"这一段。

**不会自动登录**：开完号必须自己去 ``/login`` 登录。刻意如此 —— 管理员常
用这一页替别人开号，若自动登录就会把管理员自己的会话换成新账号的。

**不要**把这一页加进导航。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import settings
from ...db import utcnow
from ...models import Member, MemberRole, Role, User
from ...permissions import APPLICATION_REVIEW
from ...security import hash_password, password_problem, verify_csrf
from ...services.audit import record_audit
from ...services.naming import (
    callsign_owner, has_account, members_without_account, username_owner,
)
from ..deps import Principal, get_db, get_principal, require
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

#: 关闭公开时用的守卫（``GFVFW_ENROLL_OPEN=false``）。
#: 直接复用统一的权限守卫，拿到与其它受限页面完全一致的 401/403 行为。
_gated = require(APPLICATION_REVIEW)


def enroll_access(principal: Principal = Depends(get_principal)) -> Principal:
    """本页的准入判定。

    默认（``enroll_open=True``）**谁都能进**，包括未登录访客 —— 这是联队口径。
    置 ``GFVFW_ENROLL_OPEN=false`` 后恢复为"需要 ``application.review``"。

    ⚠️ 注意这里**不要求登录**：公开时 ``principal`` 可能是匿名的，
       所以下面凡是记审计/日志的地方都要容忍 ``principal.user is None``。
    """
    if settings.enroll_open:
        return principal
    return _gated(principal)


def _ip_hash(request: Request) -> str:
    """来源 IP 的哈希（**不存明文 IP**，与 ``/register`` 同一套）。"""
    from ...security import privacy_hash
    from ...services.audit import client_ip

    return privacy_hash(client_ip(request)) or "unknown"


def _recent_enrollments(db: Session, ip_hash: str) -> int:
    """这个来源最近 24 小时经本页开出了几个账号。

    ⚠️ 数的是 ``users`` 表里带同一 ``registration_ip_hash`` 的行 ——
       与 ``/register`` 共用同一个计数器列（都是"从这儿开了个账号"）。
       所以两个入口共享同一份每日额度，不会被"换个入口接着刷"绕过。
    """
    since = utcnow() - timedelta(days=1)
    return db.scalar(
        select(func.count()).select_from(User)
        .where(User.registration_ip_hash == ip_hash,
               User.created_at >= since)) or 0


def _ctx(db: Session, **extra) -> dict:
    """本页共用的上下文。"""
    ctx = {
        "candidates": members_without_account(db),
        "role_code": ENROLL_ROLE,
        "callsign_max": CALLSIGN_MAX,
        "username_max": USERNAME_MAX,
        "mode_existing": MODE_EXISTING,
        "mode_new": MODE_NEW,
        "enroll_open": settings.enroll_open,
        "max_per_day": settings.max_enroll_per_ip_per_day,
        "logged_in_as": None,
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
                principal: Principal = Depends(enroll_access),
                db: Session = Depends(get_db)):
    """开号页。默认**公开**（见本模块 docstring）。"""
    return render(request, "enroll/index.html", _ctx(
        db,
        did=request.query_params.get("did", ""),
        created_username=request.query_params.get("username", ""),
        created_callsign=request.query_params.get("callsign", ""),
        created_mode=request.query_params.get("mode", ""),
        logged_in_as=(principal.display_name if principal.is_authenticated
                      else None),
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
                  principal: Principal = Depends(enroll_access),
                  db: Session = Depends(get_db)):

    # ⚠️ 公开页面上的 CSRF 检查**照旧保留**。公开 ≠ 可以省掉 CSRF ——
    #    没有它，任何第三方页面都能替访客往这里提交表单。
    verify_csrf(request, csrf_token)

    if mode not in (MODE_EXISTING, MODE_NEW):
        mode = MODE_NEW
    callsign = callsign.strip()
    username = username.strip()

    def fail(message: str, status: int = 400):
        return render(request, "enroll/index.html", _ctx(
            db, error=message, mode=mode, member_id=member_id,
            form={"callsign": callsign, "username": username},
            logged_in_as=(principal.display_name if principal.is_authenticated
                          else None),
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

    # ---- 2) 按来源 IP 限流 ----
    #    公开页面 + "建出来的就是队员" = 一条能批量刷的通路。这道闸**不是**
    #    权限判定（联队要求本页不需要权限），只挡住单个来源无限刷。
    ip_hash = _ip_hash(request)
    used = _recent_enrollments(db, ip_hash)
    if used >= settings.max_enroll_per_ip_per_day:
        log.warning("开号被限流：ip_hash=%s 24 小时内已开 %d 个", ip_hash, used)
        return fail("同一个网络今天已经开过 %d 个账号了（上限 %d 个）。"
                    "如果是联队集体入队，请让一部分人明天再来，"
                    "或联系管理员用手工方式开号。"
                    % (used, settings.max_enroll_per_ip_per_day), status=429)

    # ---- 3) 登录名唯一（**只查登录名这一侧**）----
    #    ⚠️ 联队口径：登录名**可以**与名册呼号相同（用呼号当登录名是常规做法）。
    #       所以这里**不再**做"两个命名空间不得同名"的检查。
    #       重名换不来任何权限：导航栏始终带身份标签（游客/队员），
    #       ACMI 认领需要 ACMI_CLAIM_PILOT（仅队员），呼号唯一性照旧拦重名成员。
    if username_owner(db, username) is not None:
        return fail("登录名「%s」已被占用，换一个。" % username)

    member: Member | None = None

    if mode == MODE_EXISTING:
        # ---- 4a) 挂到名册已有成员 ----
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
        # ---- 4b) 同时建名册成员 ----
        if not callsign:
            return fail("请填呼号。")
        if len(callsign) > CALLSIGN_MAX:
            return fail("呼号过长（最多 %d 个字符）。" % CALLSIGN_MAX)
        # 呼号唯一性**照旧**：名册重名会让 ACMI 归并认错人。
        problem = callsign_owner(db, callsign)
        if problem:
            return fail(problem + "。")

    # ---- 5) 落库：一个事务里的成员 + 账号 + 角色 + 审计 ----
    role = _member_role(db)          # 缺失直接 500，别写到一半才发现

    try:
        if member is None:
            member = Member(callsign=callsign, status="active")
            db.add(member)
            db.flush()

        user = User(username=username, password_hash=hash_password(password),
                    status="active", member_id=member.id,
                    registration_ip_hash=ip_hash)
        db.add(user)
        db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))

        record_audit(db,
                     actor_user_id=(principal.user.id
                                    if principal.user is not None else None),
                     action="member.enroll", target_table="users",
                     target_id=user.id,
                     after={"callsign": member.callsign, "username": username,
                            "role": ENROLL_ROLE, "account_status": "active",
                            "mode": mode, "open": settings.enroll_open,
                            "identical_name": bool(
                                username.lower() == member.callsign.lower())},
                     reason=("公开页直接开通队员账号（跳过注册与申请）"
                             if settings.enroll_open else
                             "受限页直接开通队员账号（跳过注册与申请）"),
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except IntegrityError:
        # 并发下两个人同时开号 / 名字刚被占用 —— 如实报告，不假装成功
        db.rollback()
        log.warning("直接开号失败（唯一约束）username=%s callsign=%s",
                    username, callsign)
        return fail("开号失败：登录名或呼号刚刚被占用了，请刷新后重试。"
                    "（事务已回滚，不会留下半个成员。）")

    log.info("直接开通队员账号：%s（呼号 %s，模式 %s，来源 %s）",
             username, member.callsign, mode,
             principal.display_name if principal.is_authenticated else "匿名访客")
    return _redirect(did="created", mode=mode, username=quote(username, safe=""),
                     callsign=quote(member.callsign, safe=""))
