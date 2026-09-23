"""公开入队申请与游客进度页。

需求
----
联队要求：**游客可随意申请，只能查看公开部分**；队员由管理员从游客提升上来。
所以这里做两件事：

1. ``GET/POST /apply`` —— **未登录即可访问**的公开申请表。提交后
   **立即创建一个「游客」账号**（``users.status = 'pending'``）并落一条
   :class:`~gfvfw.models.identity.Application`。游客当场就能登录，
   但拿不到任何权限点（见 :mod:`gfvfw.web.deps` 的三档身份说明）。
2. ``GET /apply/status`` —— 游客自己的进度页。这是他的"首页"：
   告诉他当前是什么身份、能看到什么、还差哪一步、以及被拒/待审的原因。

为什么申请即建账号（而不是"批准后才发邀请码"）
--------------------------------------------
联队选了"申请即建账号"。好处是游客随时能登录看进度，不必等邮件；
代价是**开放注册**，所以必须自己兜住防刷 —— 见 :data:`MAX_APPLICATIONS_PER_IP_PER_DAY`。

⚠️ 安全边界（这里**不做**什么）
------------------------------
* 申请**不创建名册成员**。``Member`` 是队员名册，只有管理员提升时才建 ——
  否则名册会被未审批的申请污染。
* 申请**不授予任何角色**。``users.status='pending'`` 在 :func:`load_principal`
  里会走白名单分支，权限点必然为空。
* ``Application`` 里**不存明文密码**；密码只进 ``users.password_hash``（argon2id）。
* ``source_ip_hash`` 存哈希不存明文 IP（需求 R7）。
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
from ...security import hash_password, password_problem, verify_csrf
from ...services.audit import record_audit
from ..deps import Principal, get_db, require_login
from ..templating import render

log = logging.getLogger("gfvfw.web.apply")

router = APIRouter()

#: 同一个来源 IP 每天最多提交几份申请（需求 R7 的最简兜底）。
#:
#: ⚠️ 当前**没有 IP 维度限流**（只有账号维度的登录锁定），而申请入口是公开的，
#:    所以必须自己设一道闸。联队规模下 5 份/天足够正常使用，
#:    又能拦住脚本批量注册。
MAX_APPLICATIONS_PER_IP_PER_DAY = 5

#: 申请表单字段长度上限（超长输入直接判非法，避免塞爆数据库）。
MAX_CALLSIGN = 64
MAX_TEXT = 2000
MAX_CONTACT = 255


def _ip_hash(request: Request) -> str:
    """来源 IP 的哈希（**不存明文 IP**，需求 R7）。

    复用 :func:`gfvfw.security.privacy_hash` —— 与审计记录同一套哈希，
    这样"申请"与"审计"两条线能对上，又不落明文。
    """
    from ...security import privacy_hash
    from ...services.audit import client_ip

    return privacy_hash(client_ip(request)) or "unknown"


def _recent_from_ip(db: Session, ip_hash: str) -> int:
    since = utcnow() - timedelta(days=1)
    return db.scalar(
        select(func.count()).select_from(Application)
        .where(Application.source_ip_hash == ip_hash,
               Application.created_at >= since)) or 0


def _callsign_taken(db: Session, callsign: str) -> bool:
    """呼号是否已被占用（名册成员或未撤销的申请）。"""
    hit = db.scalar(
        select(Member.id).where(func.lower(Member.callsign) == callsign.lower(),
                                Member.deleted_at.is_(None)).limit(1))
    if hit:
        return True
    return db.scalar(
        select(Application.id)
        .where(func.lower(Application.desired_callsign) == callsign.lower(),
               Application.status.notin_(("rejected",))).limit(1)) is not None


# --------------------------------------------------------------------------
# 申请表
# --------------------------------------------------------------------------

@router.get("/apply")
def apply_form(request: Request,
               db: Session = Depends(get_db)):
    """公开入队申请表。**明确不加任何守卫** —— 这是对外招新的入口。"""
    return render(request, "apply/form.html", {
        "did": request.query_params.get("did", ""),
        "max_callsign": MAX_CALLSIGN,
        "max_text": MAX_TEXT,
    })


@router.post("/apply")
def apply_submit(request: Request,
                 callsign: str = Form(""),
                 email: str = Form(""),
                 username: str = Form(""),
                 password: str = Form(""),
                 confirm_password: str = Form(""),
                 experience: str = Form(""),
                 intent: str = Form(""),
                 contact: str = Form(""),
                 csrf_token: str = Form(""),
                 db: Session = Depends(get_db)):

    verify_csrf(request, csrf_token)

    callsign = callsign.strip()
    email = email.strip()
    username = username.strip()
    experience = experience.strip()
    intent = intent.strip()
    contact = contact.strip()

    def fail(message: str):
        # 回填已填内容，别让人重打一遍；密码不回填
        return render(request, "apply/form.html", {
            "error": message,
            "form": {"callsign": callsign, "email": email,
                     "username": username, "experience": experience,
                     "intent": intent, "contact": contact},
            "max_callsign": MAX_CALLSIGN, "max_text": MAX_TEXT,
        }, status_code=400)

    # ---- 字段校验 ----
    if not callsign:
        return fail("请填写想要的呼号。")
    if len(callsign) > MAX_CALLSIGN:
        return fail("呼号过长（最多 %d 个字符）。" % MAX_CALLSIGN)
    if not username:
        return fail("请设置登录用户名。")
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
    if len(experience) > MAX_TEXT or len(intent) > MAX_TEXT:
        return fail("文字内容过长（各最多 %d 个字符）。" % MAX_TEXT)
    if len(contact) > MAX_CONTACT:
        return fail("联系方式过长（最多 %d 个字符）。" % MAX_CONTACT)
    if email and ("@" not in email or len(email) > 254):
        return fail("邮箱格式看起来不对（也可以留空）。")

    # ---- 唯一性 ----
    if db.scalar(select(User.id).where(
            func.lower(User.username) == username.lower()).limit(1)):
        return fail("这个用户名已经被占用了，换一个吧。")
    if _callsign_taken(db, callsign):
        return fail("呼号「%s」已被占用（或已有人申请）。"
                    "如果这就是你，请直接登录查看进度。" % callsign)

    # ---- 防刷：同一 IP 每天限几份 ----
    ip_hash = _ip_hash(request)
    if _recent_from_ip(db, ip_hash) >= MAX_APPLICATIONS_PER_IP_PER_DAY:
        log.warning("入队申请触发每日上限 ip_hash=%s", ip_hash[:16])
        return fail("今天从这个网络提交的申请太多了，请明天再试。"
                    "如果是联队集体报名，请联系管理员。")

    # ---- 落库：申请 + 游客账号（同一事务）----
    try:
        user = User(username=username, password_hash=hash_password(password),
                    email=email or None, status="pending", member_id=None)
        db.add(user)
        db.flush()

        app_row = Application(
            desired_callsign=callsign,
            experience=experience or None,
            intent=intent or None,
            contact=contact or None,
            status="submitted",
            resulting_user_id=user.id,
            source_ip_hash=ip_hash,
        )
        db.add(app_row)
        db.flush()

        record_audit(db, user.id, "application.submit", "applications",
                     app_row.id,
                     after={"callsign": callsign, "username": username},
                     reason="公开入队申请（自动创建游客账号）",
                     request=request)
        db.commit()
    except IntegrityError:
        # 并发下的兜底：唯一约束挡住就如实说，不要抛 500
        db.rollback()
        log.warning("入队申请唯一约束冲突 username=%s callsign=%s",
                    username, callsign)
        return fail("这个用户名或呼号刚刚被占用了，请换一个。")

    log.info("新入队申请：呼号=%s 用户名=%s", callsign, username)
    # 刻意**不**自动登录：让申请人用刚设的密码走一次登录，
    # 既确认他记住了密码，也避免"注册即得会话"这种不必要的状态。
    return RedirectResponse("/apply?did=submitted", status_code=303)


# --------------------------------------------------------------------------
# 游客进度页
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

    # 队员也允许打开（比如回看自己当初的申请），页面上会说明已是队员
    return render(request, "apply/status.html", {
        "applications": rows,
        "latest": rows[0] if rows else None,
        "is_member": principal.is_member,
        "status_labels": APPLICATION_STATUS_LABELS,
    })


#: 申请状态 → 中文说明（页面直接展示，避免前端各写一份）
APPLICATION_STATUS_LABELS = {
    "submitted": ("已提交", "管理员还没看，请耐心等待。"),
    "screening": ("审核中", "管理员正在看你的申请。"),
    "approved": ("已通过", "管理员已通过，正在为你建立名册档案。"),
    "rejected": ("未通过", "很遗憾。可以联系管理员了解原因。"),
    "invited": ("已发出邀请", "请按管理员给的方式完成注册。"),
    "activated": ("已入队", "你已是队员，队内内容全部开放。"),
}
