"""
战役管理路由。

``/campaigns``                    列表（``?deleted=1`` 显示已作废的）
``/campaigns/new``                新建
``/campaigns/{id}``               详情（含任务列表、参战成员、机型分布）
``/campaigns/{id}/edit``          编辑
``/campaigns/{id}/delete``        软删除（任务移出，存档保留）
``/campaigns/{id}/restore``       恢复（任务**不会**自动归回）
``/campaigns/{id}/assign``        把未归属的任务加入本战役
``/missions/{id}/campaign``       把单个任务归入/移出战役
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
from ...models import Campaign, CampaignSave, Mission
from ...permissions import CAMPAIGN_MANAGE
from ...security import verify_csrf
from ...services import campaigns as CS
from ...services.audit import record_audit
from ..deps import Principal, get_db, require, require_login, require_member
from ..templating import render

log = logging.getLogger("gfvfw.web.campaigns")

router = APIRouter(prefix="/campaigns")

STATUS_LABELS = {"planning": "筹备中", "active": "进行中", "finished": "已结束"}
STATUS_BADGE = {"planning": "warn", "active": "ok", "finished": ""}
VISIBILITY_LABELS = {"public": "公开", "members": "内部", "command": "指挥层"}

MISSION_TYPE_LABELS = {
    "training": "训练", "patrol": "巡逻", "cap": "战斗空中巡逻",
    "intercept": "截击", "escort": "护航", "strike": "对地打击",
    "sead": "压制敌防空", "cas": "近距空中支援", "recon": "侦察",
    "transport": "运输", "other": "其他",
}


def _ctx() -> dict:
    return {
        "status_labels": STATUS_LABELS,
        "status_badge": STATUS_BADGE,
        "visibility_labels": VISIBILITY_LABELS,
        "type_labels": MISSION_TYPE_LABELS,
    }


def _parse_day(value: str) -> Optional[datetime]:
    """``YYYY-MM-DD``（UTC+8 时区的日期）→ UTC 当天 00:00。"""
    if not value or not value.strip():
        return None
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _fmt_day(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------

@router.get("")
def campaign_list(request: Request,
                  deleted: str = "",
                  principal: Principal = Depends(require_login),
                  db: Session = Depends(get_db)):
    """战役列表。

    ``?deleted=1`` 显示**已作废**的战役（只给有管理权限的人看）——
    没有这个视图的话，"作废"就是一条没有回头路的操作，而页面上却写着"可恢复"。
    """
    show_deleted = deleted in ("1", "true", "yes") and principal.can(CAMPAIGN_MANAGE)
    return render(request, "campaigns/list.html", {
        **_ctx(),
        "campaigns": (CS.list_deleted_campaigns(db) if show_deleted
                      else CS.list_campaigns(db)),
        "show_deleted": show_deleted,
        "can_manage": principal.can(CAMPAIGN_MANAGE),
        "unassigned": len(CS.unassigned_missions(db)),
    })


# --------------------------------------------------------------------------
# 新建（须在 /{campaign_id} 之前注册）
# --------------------------------------------------------------------------

@router.get("/new")
def campaign_new_form(request: Request,
                      principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                      db: Session = Depends(get_db)):
    return render(request, "campaigns/form.html", {
        **_ctx(), "campaign": None,
        "form": {"status": "planning", "visibility": "public", "sort_order": 0},
    })


@router.post("/new")
def campaign_create(request: Request,
                    name: str = Form(...),
                    theater: str = Form(""),
                    status: str = Form("planning"),
                    started_at: str = Form(""),
                    ended_at: str = Form(""),
                    summary: str = Form(""),
                    visibility: str = Form("public"),
                    sort_order: int = Form(0),
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                    db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    form = {"name": name.strip(), "theater": theater.strip(), "status": status,
            "started_at": started_at, "ended_at": ended_at,
            "summary": summary, "visibility": visibility, "sort_order": sort_order}

    def fail(msg: str):
        return render(request, "campaigns/form.html", {
            **_ctx(), "campaign": None, "form": form, "error": msg}, status_code=400)

    if not form["name"]:
        return fail("战役名称不能为空。")
    if len(form["name"]) > 128:
        return fail("战役名称过长（上限 128 字符）。")
    if status not in STATUS_LABELS:
        return fail("状态取值不合法。")
    if visibility not in VISIBILITY_LABELS:
        return fail("可见性取值不合法。")

    dup = db.scalar(select(Campaign).where(Campaign.name == form["name"],
                                          Campaign.deleted_at.is_(None)))
    if dup is not None:
        return fail("同名战役已存在：%s" % form["name"])

    c = Campaign(name=form["name"], theater=form["theater"] or None,
                 status=status, started_at=_parse_day(started_at),
                 ended_at=_parse_day(ended_at),
                 summary=summary.strip() or None, visibility=visibility,
                 sort_order=sort_order)
    db.add(c)
    db.flush()
    record_audit(db, actor_user_id=principal.user.id, action="create",
                 target_table="campaigns", target_id=c.id,
                 after={"name": c.name, "status": c.status},
                 reason="新建战役", request=request)
    db.commit()
    return RedirectResponse("/campaigns/%s" % c.id, status_code=303)


# --------------------------------------------------------------------------
# 详情
# --------------------------------------------------------------------------

@router.get("/{campaign_id}")
def campaign_detail(campaign_id: str, request: Request,
                    principal: Principal = Depends(require_member),
                    db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")

    detail = CS.campaign_detail(db, c)
    return render(request, "campaigns/detail.html", {
        **_ctx(),
        "campaign": c,
        **detail,
        "can_manage": principal.can(CAMPAIGN_MANAGE),
        "unassigned": CS.unassigned_missions(db) if principal.can(CAMPAIGN_MANAGE) else [],
    })


# --------------------------------------------------------------------------
# 编辑
# --------------------------------------------------------------------------

@router.get("/{campaign_id}/edit")
def campaign_edit_form(campaign_id: str, request: Request,
                       principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                       db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")
    return render(request, "campaigns/form.html", {
        **_ctx(), "campaign": c,
        "form": {"name": c.name, "theater": c.theater or "", "status": c.status,
                 "started_at": _fmt_day(c.started_at), "ended_at": _fmt_day(c.ended_at),
                 "summary": c.summary or "", "visibility": c.visibility,
                 "sort_order": c.sort_order},
    })


@router.post("/{campaign_id}/edit")
def campaign_update(campaign_id: str, request: Request,
                    name: str = Form(...),
                    theater: str = Form(""),
                    status: str = Form("planning"),
                    started_at: str = Form(""),
                    ended_at: str = Form(""),
                    summary: str = Form(""),
                    visibility: str = Form("public"),
                    sort_order: int = Form(0),
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                    db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    c = db.get(Campaign, campaign_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")

    before = {"name": c.name, "status": c.status, "theater": c.theater}

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="战役名称不能为空")
    dup = db.scalar(select(Campaign).where(Campaign.name == name,
                                          Campaign.id != c.id,
                                          Campaign.deleted_at.is_(None)))
    if dup is not None:
        raise HTTPException(status_code=400, detail="同名战役已存在：%s" % name)

    c.name = name
    c.theater = theater.strip() or None
    c.status = status
    c.started_at = _parse_day(started_at)
    c.ended_at = _parse_day(ended_at)
    c.summary = summary.strip() or None
    c.visibility = visibility
    c.sort_order = sort_order

    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="campaigns", target_id=c.id, before=before,
                 after={"name": c.name, "status": c.status, "theater": c.theater},
                 reason="编辑战役", request=request)
    db.commit()
    return RedirectResponse("/campaigns/%s" % c.id, status_code=303)


# --------------------------------------------------------------------------
# 软删除
# --------------------------------------------------------------------------

@router.post("/{campaign_id}/delete")
def campaign_delete(campaign_id: str, request: Request,
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                    db: Session = Depends(get_db)):
    verify_csrf(request, csrf_token)

    c = db.get(Campaign, campaign_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")

    # 先把"会受影响的量"数出来，写进审计与提示 —— 用户点删除前应该知道
    # 这一下动了多少东西，事后也才说得清当时是什么状态。
    n_missions = db.scalar(
        select(func.count()).select_from(Mission)
        .where(Mission.campaign_id == c.id)) or 0
    n_saves = db.scalar(
        select(func.count()).select_from(CampaignSave)
        .where(CampaignSave.campaign_id == c.id)) or 0

    # ✅ 软删除（R11）；同时把任务移出战役，避免任务指向已作废战役
    try:
        db.query(Mission).filter(Mission.campaign_id == c.id).update(
            {Mission.campaign_id: None}, synchronize_session=False)

        c.deleted_at = utcnow()
        record_audit(db, actor_user_id=principal.user.id, action="delete",
                     target_table="campaigns", target_id=c.id,
                     before={"name": c.name, "theater": c.theater,
                             "missions": n_missions, "saves": n_saves},
                     after={"deleted": True, "missions_detached": n_missions},
                     reason="软删除战役（任务已移出；战役存档仍保留）",
                     request=request)
        db.commit()
    except Exception as exc:                            # noqa: BLE001
        # ⚠️ 先回滚再对外说话：不滚的话会话会留在 PendingRollback，
        #    后面任何查询都会抛 PendingRollbackError 把真因盖掉
        #    （这条是 .cam 上传 500 事件里学到的）。
        db.rollback()
        log.exception("作废战役失败 id=%s", campaign_id)
        raise HTTPException(status_code=500,
                            detail="作废战役失败：%s" % exc) from exc

    detail = ""
    if n_missions:
        detail += "，%d 个任务已移出为未归属" % n_missions
    if n_saves:
        detail += "，%d 份战役存档仍保留在该战役下" % n_saves
    return RedirectResponse(
        "/campaigns?message=" + quote(
            "已作废战役「%s」%s。记录仍在库里（不是真删），可在列表页「显示已作废」里恢复。"
            % (c.name, detail)),
        status_code=303)


@router.post("/{campaign_id}/restore")
def campaign_restore(campaign_id: str, request: Request,
                     csrf_token: str = Form(""),
                     principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                     db: Session = Depends(get_db)):
    """恢复被作废的战役。

    ⚠️ **不把任务自动归回来**。作废时任务被移出（``campaign_id=None``），
    但没有记录它们原本属于这个战役 —— 谁在这一段时间里被归到了别的战役、
    或者本来就是未归属的，事后分不清。所以恢复是"把战役放回列表"，
    要归回任务请在战役详情页用「归入任务」逐个（或批量）做。
    宁可让用户多点一次，也不要凭猜测挪数据。
    """
    verify_csrf(request, csrf_token)

    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="战役不存在")
    if c.deleted_at is None:
        raise HTTPException(status_code=400, detail="该战役没有被作废，无需恢复。")

    c.deleted_at = None
    record_audit(db, actor_user_id=principal.user.id, action="restore",
                 target_table="campaigns", target_id=c.id,
                 after={"name": c.name},
                 reason="恢复被作废的战役（任务不会自动归回）",
                 request=request)
    db.commit()

    return RedirectResponse(
        "/campaigns/%s?message=%s" % (
            c.id, quote("已恢复战役「%s」。作废时移出的任务需要你在本页用"
                        "「归入任务」重新归入。" % c.name)),
        status_code=303)


# --------------------------------------------------------------------------
# 任务归入战役
# --------------------------------------------------------------------------

@router.post("/{campaign_id}/assign")
def campaign_assign(campaign_id: str, request: Request,
                    mission_ids: list[str] = Form(default=[]),
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                    db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    c = db.get(Campaign, campaign_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")
    if not mission_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个任务")

    n = db.query(Mission).filter(Mission.id.in_(mission_ids)).update(
        {Mission.campaign_id: c.id}, synchronize_session=False)
    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="campaigns", target_id=c.id,
                 after={"assigned_missions": mission_ids},
                 reason="把任务归入战役", request=request)
    db.commit()
    return RedirectResponse("/campaigns/%s?message=已归入 %d 个任务" % (c.id, n),
                            status_code=303)


@router.post("/{campaign_id}/detach/{mission_id}")
def campaign_detach(campaign_id: str, mission_id: str, request: Request,
                    csrf_token: str = Form(""),
                    principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                    db: Session = Depends(get_db)):
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    m = db.get(Mission, mission_id)
    if m is None or m.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="该任务不属于此战役")
    m.campaign_id = None
    record_audit(db, actor_user_id=principal.user.id, action="update",
                 target_table="missions", target_id=m.id,
                 before={"campaign_id": campaign_id},
                 reason="把任务移出战役", request=request)
    db.commit()
    return RedirectResponse("/campaigns/%s?message=已移出该任务" % campaign_id,
                            status_code=303)
