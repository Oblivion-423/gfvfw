"""公开注册与入队申请（两步）。

联队口径（本轮修正）
--------------------
**注册 → 看公开内容 → 再提交入队申请 → 管理员提升为队员**

之前是"一张表同时完成注册与申请"，现在按联队要求拆成两步：

1. ``GET/POST /register`` —— **公开**。只填账号信息（用户名 / 密码 / 邮箱）。
   提交后立刻建立一个**游客**账号（``users.status='pending'``）并登录。
   游客已经能看各区块的**列表与汇总**（名册、飞行记录、战役、统计、资料目录）。
2. ``GET/POST /apply`` —— **需要登录**。填呼号意向 / 经历 / 意向 / 联系方式，
   落一条 :class:`~gfvfw.models.identity.Application`（状态 ``submitted``），
   等管理员在 ``/applications`` 提升为队员。

可见性规则（页面级）
--------------------
* **列表 / 汇总页** → 游客可看（``require_login``）
* **详情页** → 仅队员（``require_member``）

⚠️ 因此**列表页里内嵌的写操作 UI 必须一起挡住** ——
ACMI 工作台（上传/删除/认领/归并）是嵌在 `/log/campaign` 与 `/log/training`
里的，模板里用 ``principal.is_member`` 判过了。改这两类页面时别忘同步。

为什么注册后仍要"申请"这一步
----------------------------
注册只证明"有个能登录的账号"，**不证明是联队的人**。
入队申请是人工审核的入口，管理员据此建名册成员、给角色、激活账号 ——
那一步才是真正的授权动作（见 ``routers/applications.py``）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import Application, Member, User
from ...security import (
    SESSION_USER_KEY, hash_password, password_problem, verify_csrf,
)
from ...services.audit import record_audit
from ...services.naming import (
    callsign_owner, username_owner, username_shadows_callsign,
)
from ..deps import Principal, get_db, get_principal, require_login
from ..templating import render

log = logging.getLogger("gfvfw.web.apply")

router = APIRouter()

#: 同一个来源 IP 每天最多注册几个账号（需求 R7 的最简兜底）。
#:
#: ⚠️ 当前**没有 IP 维度限流**（只有账号维度的登录锁定），而注册入口是公开的，
#:    所以必须自己设一道闸。
MAX_REGISTRATIONS_PER_IP_PER_DAY = 5

MAX_CALLSIGN = 64
MAX_TEXT = 2000
MAX_CONTACT = 255

#: 申请状态 → (中文标签, 说明)
APPLICATION_STATUS_LABELS = {
    "submitted": ("已提交", "管理员还没看，请耐心等待。"),
    "screening": ("审核中", "管理员正在看你的申请。"),
    "approved": ("已通过", "管理员已通过，正在为你建立名册档案。"),
    "rejected": ("未通过", "很遗憾。可以联系管理员了解原因。"),
    "invited": ("已发出邀请", "请按管理员给的方式完成注册。"),
    "activated": ("已入队", "你已是队员，详情页与写操作全部开放。"),
}


def _q(text: str, maxlen: int = 200) -> str:
    from urllib.parse import quote
    return quote(text[:maxlen], safe="")


def _ip_hash(request: Request) -> str:
    """来源 IP 的哈希（**不存明文 IP**，需求 R7）。

    复用 :func:`gfvfw.security.privacy_hash` —— 与审计记录同一套哈希，
    这样"注册"与"审计"两条线能对上，又不落明文。
    """
    from ...security import privacy_hash
    from ...services.audit import client_ip

    return privacy_hash(client_ip(request)) or "unknown"


def _recent_registrations(db: Session, ip_hash: str) -> int:
    since = utcnow() - timedelta(days=1)
    return db.scalar(
        select(func.count()).select_from(User)
        .where(User.registration_ip_hash == ip_hash,
               User.created_at >= since)) or 0


def _callsign_taken(db: Session, callsign: str) -> bool:
    """呼号是否已被占用（名册成员或未撤销的申请）。

    ⚠️ 实现统一在 :mod:`gfvfw.services.naming` —— 别在这里再写一份。
    四个入口（注册 / 名册新建 / 隐藏的直开页 / CLI）共用同一套判定，
    否则迟早出现"某个入口放过了"的不一致。
    """
    from ...services.naming import callsign_owner
    return callsign_owner(db, callsign) is not None


# --------------------------------------------------------------------------
# 第一步：公开注册（只建游客账号）
# --------------------------------------------------------------------------

@router.get("/register")
def register_form(request: Request,
                  principal: Principal = Depends(get_principal)):
    """公开注册页。已登录的人不必再来，直接引导去申请。

    ⚠️ 这里用 ``get_principal``（只取身份、**不做任何要求**）而不是
    ``require_login`` —— 本页本身就必须对未登录用户开放。
    """
    if principal.is_authenticated:
        return RedirectResponse("/apply", status_code=303)
    # ⚠️ 阈值与密码下限**从常量传进模板**，不要写死在 HTML 里 ——
    #    写死的数字迟早和代码不一致，而这种不一致只有用户会碰到。
    from ...security import MIN_PASSWORD_LENGTH
    return render(request, "apply/register.html", {
        "did": request.query_params.get("did", ""),
        "max_per_day": MAX_REGISTRATIONS_PER_IP_PER_DAY,
        "min_password": MIN_PASSWORD_LENGTH,
    })


@router.post("/register")
def register_submit(request: Request,
                    username: str = Form(""),
                    email: str = Form(""),
                    password: str = Form(""),
                    confirm_password: str = Form(""),
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)

    # 已登录的人不该再注册一个账号 —— 他要么是游客（去提交申请），
    # 要么已是队员。放过去只会得到两个账号、两份困惑。
    if principal.is_authenticated:
        return RedirectResponse("/apply", status_code=303)

    username = username.strip()
    email = email.strip()

    def fail(message: str, status: int = 400):
        from ...security import MIN_PASSWORD_LENGTH
        return render(request, "apply/register.html", {
            "error": message,
            "form": {"username": username, "email": email},
            "max_per_day": MAX_REGISTRATIONS_PER_IP_PER_DAY,
            "min_password": MIN_PASSWORD_LENGTH,
        }, status_code=status)

    if not username:
        return fail("请填写用户名。")
    if len(username) > 64:
        return fail("用户名过长（最多 64 个字符）。")
    if " " in username:
        return fail("用户名不能含空格。")
    if not password:
        return fail("请设置密码。")
    if password != confirm_password:
        return fail("两次输入的密码不一致。")
    problem = password_problem(password)
    if problem:
        return fail(problem)
    if email and ("@" not in email or len(email) > 254):
        return fail("邮箱格式看起来不对（也可以留空）。")

    if username_owner(db, username) is not None:
        return fail("这个用户名已经被占用了，换一个吧。")

    # ★ 用户名不得与名册呼号相同。
    #
    # 这堵的是"游客看起来像现有成员"的漏洞：游客在界面上的显示名会回落成
    # 用户名（deps.Principal.display_name），所以一个用户名恰好等于
    # 某个成员呼号的游客，在名册与审批页里看起来就是那位成员。
    # 提升时的呼号校验会拦住他（不会真出现两个同名成员），但"看起来像"
    # 已经够让管理员看错人了。
    shadow = username_shadows_callsign(db, username)
    if shadow:
        return fail(shadow)

    ip_hash = _ip_hash(request)
    if _recent_registrations(db, ip_hash) >= MAX_REGISTRATIONS_PER_IP_PER_DAY:
        log.warning("注册触发每日上限 ip_hash=%s", ip_hash[:16])
        return fail("今天从这个网络注册的账号太多了，请明天再试。"
                    "如果是联队集体入队，请联系管理员。")

    try:
        user = User(username=username, password_hash=hash_password(password),
                    email=email or None, status="pending", member_id=None,
                    registration_ip_hash=ip_hash)
        db.add(user)
        db.flush()
        record_audit(db, user.id, "user.register", "users", user.id,
                     after={"username": username,
                            "status": user.status},
                     reason="公开注册（自动成为游客）", request=request)
        db.commit()
    except IntegrityError:
        db.rollback()
        log.warning("注册唯一约束冲突 username=%s", username)
        return fail("这个用户名刚刚被占用了，请换一个。")

    # 注册**不是**授权：账号是 pending（游客），没有任何权限点。
    # 但它已经能看各区块的列表与汇总，所以直接登录、少一次输密码。
    request.session[SESSION_USER_KEY] = user.id
    log.info("新游客注册：%s", username)
    return RedirectResponse("/apply?did=registered", status_code=303)


# --------------------------------------------------------------------------
# 第二步：入队申请（需要登录）
# --------------------------------------------------------------------------

@router.get("/apply")
def apply_form(request: Request,
               principal: Principal = Depends(require_login),
               db: Session = Depends(get_db)):
    """入队申请表 —— **需要先注册并登录**（联队要求"注册之后再提交申请"）。"""
    existing = db.scalar(
        select(Application)
        .where(Application.resulting_user_id == principal.user.id,
               Application.status.notin_(("rejected",)))
        .order_by(Application.created_at.desc()).limit(1))

    return render(request, "apply/form.html", {
        "did": request.query_params.get("did", ""),
        "existing": existing,
        "max_callsign": MAX_CALLSIGN,
        "max_text": MAX_TEXT,
        "status_labels": APPLICATION_STATUS_LABELS,
    })


@router.post("/apply")
def apply_submit(request: Request,
                 callsign: str = Form(""),
                 experience: str = Form(""),
                 intent: str = Form(""),
                 contact: str = Form(""),
                 csrf_token: str = Form(""),
                 principal: Principal = Depends(require_login),
                 db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)

    callsign = callsign.strip()
    experience = experience.strip()
    intent = intent.strip()
    contact = contact.strip()

    def fail(message: str, status: int = 400):
        existing = db.scalar(
            select(Application)
            .where(Application.resulting_user_id == principal.user.id)
            .order_by(Application.created_at.desc()).limit(1))
        return render(request, "apply/form.html", {
            "error": message,
            "existing": existing,
            "form": {"callsign": callsign, "experience": experience,
                     "intent": intent, "contact": contact},
            "max_callsign": MAX_CALLSIGN, "max_text": MAX_TEXT,
            "status_labels": APPLICATION_STATUS_LABELS,
        }, status_code=status)

    if not callsign:
        return fail("请填写想用的呼号。")
    if len(callsign) > MAX_CALLSIGN:
        return fail("呼号过长（最多 %d 个字符）。" % MAX_CALLSIGN)
    if len(experience) > MAX_TEXT or len(intent) > MAX_TEXT:
        return fail("文字内容过长（各最多 %d 个字符）。" % MAX_TEXT)
    if len(contact) > MAX_CONTACT:
        return fail("联系方式过长（最多 %d 个字符）。" % MAX_CONTACT)

    if _callsign_taken(db, callsign):
        return fail("呼号「%s」已被占用（或已有人申请）。"
                    "如果这就是你，请查看申请进度。" % callsign)

    # 已经提交过、还没被拒的，不重复提交（避免刷一屏申请）
    pending = db.scalar(
        select(Application)
        .where(Application.resulting_user_id == principal.user.id,
               Application.status.notin_(("rejected",)))
        .order_by(Application.created_at.desc()).limit(1))
    if pending is not None:
        return fail("你已经提交过入队申请（状态见下方），不必重复提交。"
                    "如需修改，请联系管理员。")

    try:
        app_row = Application(
            desired_callsign=callsign,
            experience=experience or None,
            intent=intent or None,
            contact=contact or None,
            status="submitted",
            resulting_user_id=principal.user.id,
            source_ip_hash=_ip_hash(request),
        )
        db.add(app_row)
        db.flush()
        record_audit(db, principal.user.id, "application.submit",
                     "applications", app_row.id,
                     after={"callsign": callsign},
                     reason="提交入队申请（游客 → 待提升）",
                     actor_role=principal.primary_role, request=request)
        db.commit()
    except IntegrityError:
        db.rollback()
        return fail("呼号刚刚被占用了，请换一个。")

    log.info("新入队申请：呼号=%s（账号 %s）", callsign, principal.user.username)
    return RedirectResponse("/apply/status?did=submitted", status_code=303)


# --------------------------------------------------------------------------
# 进度页
# --------------------------------------------------------------------------

@router.get("/apply/status")
def apply_status(request: Request,
                 principal: Principal = Depends(require_login),
                 db: Session = Depends(get_db)):
    """游客（或队员）自己的申请进度。

    ⚠️ 用 :func:`require_login` 而不是 ``require_member`` —— 这个页面
    正是给**游客**看的，加队员守卫等于把唯一能看到进度的页面锁上。
    """
    rows = list(db.scalars(
        select(Application)
        .where(Application.resulting_user_id == principal.user.id)
        .order_by(Application.created_at.desc())).all())

    return render(request, "apply/status.html", {
        "applications": rows,
        "latest": rows[0] if rows else None,
        "status_labels": APPLICATION_STATUS_LABELS,
        "did": request.query_params.get("did", ""),
    })
