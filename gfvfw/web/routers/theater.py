"""战役管理路由：BMS ``.cam`` 存档上报与战场态势展示。

菜单里的「战役管理」指向本模块。它解析的是 **Falcon BMS 的战役存档**
（``.cam``），呈现联队当前所在战场的态势：队伍兵力对比、目标点归属、
空中编队与飞行、地面与海军单位、情报事件、以及跨存档的战役进程。

与「飞行记录 → 战役记录」的区别：后者是本联队的**战争史**（谁在哪个任务里
飞了什么），本模块是 BMS **战场本身**的态势。两者互补，不是一回事。
"""
from __future__ import annotations

import json
import logging
import tempfile
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...campaign.coords import GRID_SIZE, Projection
from ...campaign.maps import (
    MAP_DIR_SETTING_HINT, discover_theater_maps, pick_default_map)
from ...campaign.state import stance_name
from ...config import settings
from ...models.campaign_state import (
    CampaignEvent, CampaignObjective, CampaignSave,
    CampaignTeamState, CampaignUnit,
)
from ...models.flight import Campaign
from ...permissions import CAMPAIGN_MANAGE, CAMPAIGN_UPLOAD, CAMPAIGN_VIEW
from ...services import campaign as svc
from ...services.audit import record_audit
from ..deps import Principal, get_db, require, require_login
from ..templating import render
from .acmi import wizard_for_request

log = logging.getLogger(__name__)

router = APIRouter()

#: 单位种类 → 中文名（页面显示用）
UNIT_KIND_CN = {
    "Flight": "飞行", "Package": "编队", "Squadron": "中队",
    "Battalion": "营", "Brigade": "旅", "Division": "师",
    "TaskForce": "特混舰队",
}

#: 地图图层默认可见的单位
MAP_KINDS = ("Flight", "Package", "Battalion", "Brigade", "TaskForce", "Squadron")

#: 默认显示在地图上的"有意义"目标点类型。
#: 全量 6944 个目标点里 Village(2860) 与 Range(2621) 占了大头，全画上去只是噪声；
#: 默认只画基地、城市、工业、防空等真正影响战局的点，需要时可切到全部。
SIG_OBJECTIVE_TYPES = (
    "Airbase", "Airstrip", "Army Base", "Headquarters", "Port",
    "Depot", "Refinery", "Factory", "Chemical Plant", "Nuclear Plant",
    "Power / Dam", "Bridge", "SAM / AAA Site", "SAM Site (Dedicated)",
    "Radar Site", "Nav Beacon", "Radio Tower", "Fortification",
    "Mountain Pass", "City", "Border",
)

#: 1 海里 = 1.852 km = 1.852 网格格（网格 1 格 = 1 km）
NM_TO_GRID = 1.852


def _load_campaign(db: Session, campaign_id: str) -> Campaign:
    camp = db.get(Campaign, campaign_id)
    if camp is None or camp.deleted_at is not None:
        raise HTTPException(status_code=404, detail="战役不存在")
    return camp


def _latest_or_404(db: Session, campaign_id: str) -> CampaignSave:
    sv = svc.latest_save(db, campaign_id)
    if sv is None:
        raise HTTPException(status_code=404, detail="该战役还没有可用的存档")
    return sv


# ==========================================================================
# 删除一份上传的存档
# ==========================================================================

@router.post("/theater/{campaign_id}/saves/{save_id}/delete")
def save_delete(campaign_id: str, save_id: str, request: Request,
                reason: str = Form(""),
                csrf_token: str = Form(""),
                principal: Principal = Depends(require(CAMPAIGN_MANAGE)),
                db: Session = Depends(get_db)):
    """删掉传错的一份 ``.cam`` 存档（连带它的队伍/目标点/单位/事件明细）。

    * 子表有 ``ON DELETE CASCADE``，删存档会一并删明细（有测试覆盖）。
    * 删掉**最新**那份之后，战役总览会自动落到上一份存档上
      （``latest_save`` 按战役时刻取最新），战局显示随之后退 —— 这是预期行为。
    * 磁盘原件一并删除（``GFVFW_KEEP_CAM_FILES`` 只影响上传时是否留原件，
      删除动作总是清理，避免留下无主文件）。
    """
    from ...security import verify_csrf
    verify_csrf(request, csrf_token)

    camp = _load_campaign(db, campaign_id)
    sv = db.get(CampaignSave, save_id)
    if sv is None or sv.campaign_id != camp.id:
        raise HTTPException(status_code=404, detail="存档不存在或不属于该战役")

    n_obj = db.scalar(select(func.count()).select_from(CampaignObjective)
                      .where(CampaignObjective.save_id == sv.id)) or 0
    n_unit = db.scalar(select(func.count()).select_from(CampaignUnit)
                       .where(CampaignUnit.save_id == sv.id)) or 0

    latest = svc.latest_save(db, camp.id)
    was_latest = latest is not None and latest.id == sv.id

    fname = sv.original_filename
    stored = sv.stored_path
    sha = sv.sha256
    sv_id = sv.id

    record_audit(db, actor_user_id=principal.user.id, action="delete",
                 target_table="campaign_saves", target_id=sv_id,
                 before={"original_filename": fname, "sha256": sha,
                         "theater": sv.theater, "scenario": sv.scenario,
                         "campaign_time": sv.campaign_time_label,
                         "objectives": n_obj, "units": n_unit},
                 reason=reason.strip() or "删除上传的战役存档", request=request)
    db.delete(sv)
    db.commit()

    removed = False
    if stored:
        p = Path(stored)
        if not p.is_absolute():
            p = Path(settings.storage_dir) / p
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError as exc:
            log.warning("存档原件删除失败 %s：%s", p, exc)

    log.info("存档 %s（%s）已删除：%d 目标点 / %d 单位；磁盘原件%s",
             fname, sv_id[:8], n_obj, n_unit, "已删" if removed else "未找到")
    tail = "（含 %d 目标点、%d 单位）" % (n_obj, n_unit)
    if was_latest:
        tail += "，战局已回退到上一份存档"
    return RedirectResponse(
        "/theater/%s/saves?saved=1&message=存档已删除%s" % (camp.id, tail),
        status_code=303)


def _nav_ctx(db: Session, campaign: Campaign,
             principal: Principal | None = None) -> dict:
    """所有战役子页面共用的上下文。

    ⚠️ ``can_upload`` / ``can_manage`` / ``can_delete`` **必须在这里给全**。
        它们以前只在 ``theater_index`` 里设置，而 ``theater/detail.html`` 也用
        了 ``can_upload`` —— 于是详情页上那两个"去上报一份 / 再上报一份存档"
        按钮**永远是隐藏的**（Jinja 里未定义变量为假），有权限的人根本看不到。
        "写好了却点不到"和没做一样，所以统一在这里按权限算好，各子页共用。
    """
    ctx = {
        "campaign": campaign,
        "saves": svc.saves_for(db, campaign.id),
        "latest": svc.latest_save(db, campaign.id),
    }
    if principal is not None:
        ctx.update({
            "can_upload": principal.can(CAMPAIGN_UPLOAD),
            "can_manage": principal.can(CAMPAIGN_MANAGE),
            # 与 /campaigns 的作废/恢复同一个权限点、同一个 handler
            "can_delete": principal.can(CAMPAIGN_MANAGE),
        })
    return ctx


# ==========================================================================
# 首页：战役列表
# ==========================================================================

@router.get("/theater")
def theater_index(request: Request, deleted: str = "",
                  db: Session = Depends(get_db),
                  principal: Principal = Depends(require_login)):
    """战役管理首页：所有带存档的战役 + 最新态势摘要。

    ⚠️ 列表页 —— 游客可看（"只开列表，不开详情"）。
    各战役的**详情**（态势图 / 基地 / 兵力 / 目标点 / 时间线）仍是
    ``require(CAMPAIGN_VIEW)``，即仅队员。
    写操作入口由模板里的 ``can_upload`` / ``can_manage`` / ``can_delete``
    自行隐藏，游客这几项都是 ``False``（没有任何权限点）。

    ``?deleted=1`` 列出**已作废**的战役（仅 ``campaign.manage``）——
    ⚠️ 恢复入口必须放在**这一页**：联队口中的"战役管理"就是 ``/theater``
        （顶栏那一项指的就是它），而作废/恢复的 handler 挂在 ``/campaigns`` 下。
        只把按钮放在 ``/campaigns`` 的话，用户在自己天天用的页面上根本看不到
        —— 本轮的反馈"战役管理中仍然不能删除战役"就是这么来的。
    """
    can_manage = principal.can(CAMPAIGN_MANAGE)
    show_deleted = deleted in ("1", "true", "yes") and can_manage

    stmt = select(Campaign).where(
        Campaign.deleted_at.is_not(None) if show_deleted
        else Campaign.deleted_at.is_(None))
    campaigns = list(db.scalars(stmt.order_by(Campaign.created_at)))

    rows = []
    for camp in campaigns:
        rows.append(svc.campaign_overview(db, camp))

    orphans = db.scalar(
        select(func.count()).select_from(CampaignSave)
        .where(CampaignSave.campaign_id.is_(None))) or 0

    return render(request, "theater/index.html", {
        "rows": rows,
        "orphans": orphans,
        "show_deleted": show_deleted,
        "deleted_count": (db.scalar(
            select(func.count()).select_from(Campaign)
            .where(Campaign.deleted_at.is_not(None))) or 0),
        "bms_path": settings.bms_install_path,
        "bms_ok": bool(settings.bms_install_path)
        and Path(settings.bms_install_path).is_dir(),
        "can_upload": principal.can(CAMPAIGN_UPLOAD),
        "can_manage": can_manage,
        "can_delete": can_manage,
        "total_saves": db.scalar(select(func.count()).select_from(CampaignSave)) or 0,
    })


# ==========================================================================
# 上报
# ==========================================================================

@router.get("/theater/upload")
def upload_form(request: Request, db: Session = Depends(get_db),
                principal: Principal = Depends(require(CAMPAIGN_UPLOAD))):
    return render(request, "theater/upload.html", {
        "campaigns": list(db.scalars(
            select(Campaign).where(Campaign.deleted_at.is_(None))
            .order_by(Campaign.created_at))),
        "bms_path": settings.bms_install_path,
        "max_mb": settings.max_cam_bytes // (1024 * 1024),
    })


@router.post("/theater/upload")
async def upload_submit(request: Request,
                        file: UploadFile = File(...),
                        campaign_id: str = Form(""),
                        db: Session = Depends(get_db),
                        principal: Principal = Depends(require(CAMPAIGN_UPLOAD))):

    name = (file.filename or "").strip()
    if not name.lower().endswith(".cam"):
        return render(request, "theater/upload.html", {
            "error": "只接受 .cam 战役存档（BMS 的 Campaigns 目录里那些文件）",
            "campaigns": list(db.scalars(select(Campaign).where(
                Campaign.deleted_at.is_(None)))),
            "bms_path": settings.bms_install_path,
            "max_mb": settings.max_cam_bytes // (1024 * 1024),
        }, status_code=400)

    # 先落到临时文件再交给服务（服务自己按 SHA256 落到 storage）
    data = await file.read()
    if len(data) > settings.max_cam_bytes:
        raise HTTPException(status_code=413, detail="存档文件过大")

    def fail(message: str, status: int = 400):
        """把失败渲染回上传页（带上当前配置，便于自助排查）。

        ⚠️ 必须**先回滚**再往下走。
            入库过程中抛出的异常（例如 campaign_units.z 的 NOT NULL 失败）
            会让 Session 进入 "PendingRollbackError" 状态：此后任何一次
            查询/刷新都会直接抛 PendingRollbackError，覆盖掉真正的异常。
            下面的 render 要查 Campaign 列表，所以不先回滚的话，
            用户看到的就不是这条 fail() 的可读提示，而是又一个 500 ——
            我们把真正的错误信息亲手弄丢了。
            回滚本身再套一层 try：回滚失败也不能遮蔽原始异常。
        """
        try:
            db.rollback()
        except Exception:                               # noqa: BLE001
            log.exception("回滚会话失败（原始错误：%s）", message)
        return render(request, "theater/upload.html", {
            "error": message,
            "campaigns": list(db.scalars(select(Campaign).where(
                Campaign.deleted_at.is_(None)))),
            "bms_path": settings.bms_install_path,
            "max_mb": settings.max_cam_bytes // (1024 * 1024),
        }, status_code=status)

    # ⚠️ 临时文件的创建与写入**也要**包起来。
    #    它们在原来的 try 之外，而这是最典型的"只在服务器上炸"的操作：
    #    服务跑在 systemd 沙箱里（ProtectSystem=strict / PrivateTmp），
    #    TMPDIR 不可写、磁盘满了、配额用尽 —— 任何一种都会抛出 OSError，
    #    结果是用户看到光秃秃的 500，而真正的原因（写不进 /tmp）一个字都没露。
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="gfvfw-cam-"))
        tmp = tmpdir / Path(name).name
        tmp.write_bytes(data)
    except OSError as exc:
        log.warning("战役存档临时文件写入失败：%s", exc)
        return fail("服务器无法写入临时文件（%s）。"
                    "请检查磁盘空间与临时目录权限，或把这个错误告诉管理员。"
                    % exc, status=500)

    try:
        svc_ = svc.CampaignService()
        result = svc_.ingest(db, tmp, original_filename=Path(name).name,
                            uploaded_by=principal.user.id if principal.user else None,
                            campaign_id=campaign_id or None)
    except FileNotFoundError as exc:
        # 没配 BMS 安装目录 —— 这是部署问题，给出可执行的提示
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("战役存档上报失败：%s", exc)
        return fail("解析失败：%s" % exc)
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmpdir.rmdir()
        except OSError:
            pass

    # ⚠️ 审计与提交也在原来的 try 之外。走到这里说明解析已经成功、
    #    文件已经归档到磁盘 —— 此时再抛 500，用户看到的是"上传失败"，
    #    但磁盘上其实多了一个文件、库里可能已经多了一条存档记录。
    #    所以这里失败要**如实说出这种半成品状态**，而不是留给用户去猜。
    try:
        record_audit(db, actor_user_id=principal.user.id if principal.user else None,
                     action="campaign.upload", target_table="campaign_saves",
                     target_id=result.save.id,
                     after={"file": name, "duplicate": result.duplicate,
                            "status": result.save.parse_status,
                            "objective_changes": result.objective_changes},
                     reason="上报 BMS 战役存档", request=request)
        db.commit()
    except Exception as exc:                            # noqa: BLE001
        db.rollback()
        log.exception("战役存档已解析但入库失败 file=%s：%s", name, exc)
        return fail("存档已解析，但写入数据库失败：%s。"
                    "原始文件已归档到 storage 目录，%s"
                    % (exc, "可能已产生一条未完成的存档记录，请到战役页核对。"),
                    status=500)
    if result.save.campaign_id:
        return RedirectResponse(
            "/theater/%s?saved=1" % result.save.campaign_id, status_code=303)
    return RedirectResponse("/theater", status_code=303)


# ==========================================================================
# 战役态势总览
# ==========================================================================

@router.get("/theater/{campaign_id}")
def theater_detail(campaign_id: str, request: Request,
                   db: Session = Depends(get_db),
                   principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)
    ov = svc.campaign_overview(db, camp)
    ctx = _nav_ctx(db, camp, principal)
    ctx.update({
        "ov": ov,
        "teams": ov.teams,
        "stance_name": stance_name,
        "saved": request.query_params.get("saved") == "1",
        "active_teams": [t for t in ov.teams if t.active],
        "unit_kind_cn": UNIT_KIND_CN,
    })
    # 「ACMI 工作台」：战役固定为当前存档，归并出的任务**直接归入本战役**。
    ctx.update(wizard_for_request(db, principal, request,
                                  return_to="/theater/%s" % camp.id,
                                  campaign_id=camp.id,
                                  lock_campaign=True))
    if ov.save is not None:
        ctx["events"] = list(db.scalars(
            select(CampaignEvent).where(CampaignEvent.save_id == ov.save.id)
            .order_by(CampaignEvent.at_campaign_time_ms.desc())))
        ctx["warnings"] = json.loads(ov.save.parse_warnings_json or "[]")
        # 「战区地图」面板：战役管理里也要能直接看到战区底图。
        # 只放**一张**（7~15 MB）；列表页每行一张会把首屏拖成几十 MB。
        ctx.update(_map_ctx(request, ov.save))
    return render(request, "theater/detail.html", ctx)


# ==========================================================================
# 地图
# ==========================================================================

@router.get("/theater/{campaign_id}/map")
def theater_map(campaign_id: str, request: Request,
                db: Session = Depends(get_db),
                principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    """战场态势图：目标点 + 单位 + SAM 威胁环 + 靶心，纯网格坐标绘制。"""
    camp = _load_campaign(db, campaign_id)
    sv = _latest_or_404(db, campaign_id)

    kind = request.query_params.get("kind", "Flight")
    layer = request.query_params.get("layer", "key")   # key | all
    #: 可选：把全部 SAM 阵地都按某一型号的真实射程画成统一威胁环。
    #: 因为存档里**不记录每个 SAM 阵地是哪一型系统**（目标点只有
    #: "SAM / AAA Site" 这一个类型名），所以逐点画不同半径是做不到的；
    #: 这个参数提供的是一个**透明的假设视图**，不是实测结果。
    sam_assume = (request.query_params.get("sam") or "").strip()
    objectives = list(db.scalars(
        select(CampaignObjective).where(CampaignObjective.save_id == sv.id)
        .where(CampaignObjective.grid_x.is_not(None))))
    units = list(db.scalars(
        select(CampaignUnit).where(CampaignUnit.save_id == sv.id)
        .where(CampaignUnit.grid_x.is_not(None))))
    teams = list(db.scalars(
        select(CampaignTeamState).where(CampaignTeamState.save_id == sv.id)
        .order_by(CampaignTeamState.team_id)))

    # 目标点按类型分组计数（图层控制与图例用）
    type_counts = Counter(o.type_name or "Unknown" for o in objectives)
    kinds = Counter(u.unit_kind for u in units)
    shown = objectives if layer == "all" else [
        o for o in objectives if (o.type_name or "") in SIG_OBJECTIVE_TYPES]

    # 队伍配色（BMS 的 color 是 0..7 的槽位）
    palette = ["#4f8cff", "#ff5c5c", "#3fbf7f", "#ffcc4d",
               "#a86bff", "#ff8a3d", "#38c7c7", "#c0c0c0"]

    sam_threat = _sam_threat_for(db, sv)
    tctx = _theater_context(sv)

    ctx = {
        "campaign": camp, "save": sv,
        "latest": sv,
        "objectives": shown, "all_count": len(objectives),
        "units": units, "teams": teams,
        "type_counts": sorted(type_counts.items(), key=lambda x: -x[1]),
        "kinds": kinds, "kind": kind, "layer": layer,
        "grid": GRID_SIZE,
        "palette": palette,
        "bullseye": (sv.bullseye_x, sv.bullseye_y),
        "bullseye_latlon": _latlon(tctx.get("proj_obj"), sv.bullseye_x, sv.bullseye_y),
        "unit_kind_cn": UNIT_KIND_CN,
        "sam_threat": sam_threat,
        "sam_assume": sam_assume,
        "sam_assume_radius": sam_threat.get(sam_assume),
        "nm_to_grid": NM_TO_GRID,
        "theater_root": tctx.get("theater_root"),
        "projection": tctx.get("projection"),
        "grid_center_latlon": tctx.get("grid_center_latlon"),
    }
    # 剧场地图底图（与战役管理详情页共用同一套选择逻辑）
    ctx.update(_map_ctx(request, sv))
    return render(request, "theater/map.html", ctx)


def _pick_map(request: Request, sv: CampaignSave):
    """按 ``?map=`` 选出要用的剧场底图。

    返回 ``(disc, maps, sel_map, sel_idx, default_map, off)``。

    抽出来是因为**两个页面**都要用：态势图（``/map``）与
    战役管理详情页的「战区地图」面板。各写一份必然慢慢不一致。

    * ``?map=none``  → 显式关掉底图（``off=True``）
    * ``?map=<i>``   → 取第 i 张；越界或非数字则当作"没指定"
    * 没指定         → 取默认（``default_map``，即**体积最小的 4K**）

    地图按**体积升序**列出：底图是页面首次加载的主要成本（Hellas 16K 有
    768 MB），不该拿最大那张当默认值。
    """
    disc = _maps_discovery(sv)
    maps = disc.maps
    default_map = pick_default_map(maps)
    sel_map = None
    param = request.query_params.get("map")
    if param is not None:
        if param == "none":
            sel_map = None                      # 显式关掉底图
        else:
            try:
                i = int(param)
                if 0 <= i < len(maps):
                    sel_map = maps[i]
            except ValueError:
                pass
    elif default_map is not None:
        sel_map = default_map
    sel_idx = maps.index(sel_map) if sel_map in maps else -1
    return disc, maps, sel_map, sel_idx, default_map, param == "none"


def _map_ctx(request: Request, sv: CampaignSave) -> dict:
    """底图相关的模板上下文（态势图与战役管理共用）。

    ``default_map`` 一并传出去，是为了让模板能把"当前这张"与"默认那张"
    区分开（`pick_default_map` 只算一次，不在两处各算一遍）。
    """
    disc, maps, sel_map, sel_idx, default_map, off = _pick_map(request, sv)
    return {
        "maps": maps,
        "map_disc": disc,
        "map_dir_setting": MAP_DIR_SETTING_HINT,
        "map_dir_value": str(settings.bms_map_dir) if settings.bms_map_dir else None,
        "sel_map": sel_map,
        "sel_map_idx": sel_idx,
        "default_map": default_map,
        "maps_off": off,
    }


def _file_etag(st) -> str:
    """按（修改时间, 大小）生成 ETag。文件不变则 ETag 不变。"""
    return '"%x-%x"' % (int(st.st_mtime), st.st_size)


def _is_not_modified(request: Request, st) -> bool:
    """判断条件请求是否命中缓存（该返回 304）。

    ⚠️ Starlette 的 ``FileResponse`` 只会**设置** ETag/Last-Modified，
    **不做** 304 判定 —— 那是 ``StaticFiles`` 才有的行为。地图底图有 7~48 MB，
    缓存过期后靠 304 省掉整份重传，所以这里自己判。

    按 HTTP 语义：``If-None-Match`` 一旦出现就以它为准，不再回退看
    ``If-Modified-Since``（否则不匹配时会被错误地判成 304）。
    """
    etag = _file_etag(st)
    inm = request.headers.get("if-none-match")
    if inm:
        for cand in inm.split(","):
            c = cand.strip()
            if c == "*" or c.removeprefix("W/") == etag:
                return True
        return False
    ims = request.headers.get("if-modified-since")
    if ims:
        try:
            since = parsedate_to_datetime(ims)
        except (TypeError, ValueError):
            return False
        if since is not None:
            return int(st.st_mtime) <= int(since.timestamp())
    return False


@router.get("/theater/{campaign_id}/map/image/{idx}")
def theater_map_image(campaign_id: str, idx: int, request: Request,
                      db: Session = Depends(get_db),
                      principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    """提供剧场地图底图。

    只接受**列表下标**（服务端解析成实际文件），因此无法用它读任意路径 ——
    这是刻意的：地图文件在 BMS 安装目录里，不能让 URL 直接指文件系统。
    """
    # 战役不存在就 404（否则会拿着一个野 id 去解析地图下标）
    _load_campaign(db, campaign_id)
    sv = _latest_or_404(db, campaign_id)
    maps = _maps_for(sv)
    if not maps:
        raise HTTPException(status_code=404, detail="该剧场没有可用的地图图片")
    if idx < 0 or idx >= len(maps):
        raise HTTPException(status_code=404, detail="地图序号超出范围")
    m = maps[idx]
    try:
        st = m.path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="地图文件已不存在")
    cache = {"Cache-Control": "public, max-age=604800",
             "ETag": _file_etag(st)}
    if _is_not_modified(request, st):
        return Response(status_code=304, headers=cache)
    return FileResponse(m.path, media_type="image/png", headers=cache)


def _sam_threat_for(db: Session, sv: CampaignSave) -> dict[str, float]:
    """SAM 威胁半径（海里）。来自剧场武器表，按存档剧场缓存加载。

    取 ``sam_radii[name]["long"]`` —— 即该型系统武器射程的**最大值**，
    是 WCD 里真实存在的量；``short`` 那一档是推断值（见 theater.py 的常量说明），
    本页面不使用。
    """
    try:
        th = svc.theater_data(sv.theater or "", None)
        out = {}
        for k, v in (th.sam_radii or {}).items():
            nm = None
            if isinstance(v, dict):
                nm = v.get("long") or v.get("engagementRangeNm")
            else:
                nm = v
            if nm:
                out[k] = float(nm)
        return out
    except Exception:  # noqa: BLE001
        return {}


def _theater_context(sv: CampaignSave) -> dict:
    """剧场的坐标系信息：解析到的根目录、投影、经纬度换算。

    ⚠️ 剧场根目录会显示给管理员看：``load(install, "Hellas")`` 可能解析到
    ``Data\\Add-On Hellas`` 而不是 ``Data\\Add-On Hellas 2026``（BMS 的
    ``Add-On <剧场>`` 命名里带了年份，``.cmp`` 里只存 "Hellas"，无法自动区分）。
    两张表本机实测字节相同，但让管理员能看见选中的是哪一个更稳妥。
    """
    out: dict[str, Any] = {"theater_root": None, "projection": None,
                           "grid_center_latlon": None}
    try:
        th = svc.theater_data(sv.theater or "", None)
        out["theater_root"] = str(getattr(th, "root", "") or "") or None
        info = getattr(th, "theater_info", None)
        proj_str = getattr(info, "projection_string", None) if info else None
        if proj_str:
            proj = Projection.from_proj_string(proj_str)
            out["projection"] = proj_str
            out["proj_obj"] = proj
            out["grid_center_latlon"] = proj.grid_to_latlon(
                GRID_SIZE / 2, GRID_SIZE / 2)
    except Exception:  # noqa: BLE001
        pass
    return out


def _maps_for(sv: CampaignSave) -> list:
    """该存档所在剧场可用的全图列表（体积升序，小图在前）。"""
    return _maps_discovery(sv).maps


def _maps_discovery(sv: CampaignSave):
    """地图发现结果（含**搜索过程**，供页面自我诊断）。

    ⚠️ 自备目录（``GFVFW_BMS_MAP_DIR``）**必须**在剧场数据缺失时也能用。
        它曾经被写在 ``try`` 里面：只要 ``theater_data()`` 抛异常（服务器上
        没配剧场数据就是这种情况），整个函数直接返回空列表 —— 连联队自己
        放好的地图也一起丢掉。而"没配剧场数据"恰恰是最需要自备图的场景：
        那种情况下 `_MAP_SUBDIRS` 一个都扫不到。
        现在把两件事分开：剧场数据失败只记一条 problem，自备目录照扫。
    """
    try:
        th = svc.theater_data(sv.theater or "", None)
        root = getattr(th, "root", None)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取剧场数据失败，只能依赖自备地图目录：%s", exc)
        disc = discover_theater_maps(None, settings.bms_map_dir,
                                     theater=sv.theater or "")
        disc.problems.append("读取剧场数据失败（%s）—— 只能使用联队自备地图目录。"
                             % exc)
        return disc
    disc = discover_theater_maps(root, settings.bms_map_dir,
                                 theater=sv.theater or "")
    if not disc.maps and not disc.custom_dir:
        disc.problems.append(
            "没有配置联队自备地图目录，而剧场数据目录里也没有可用底图。")
    return disc


def _latlon(proj, east, north) -> Optional[tuple[float, float]]:
    """网格 → 经纬度；失败返回 ``None``（页面不能因此崩掉）。"""
    if proj is None or east is None or north is None:
        return None
    try:
        return proj.grid_to_latlon(float(east), float(north))
    except Exception:  # noqa: BLE001
        return None


# ==========================================================================
# 空中态势
# ==========================================================================

@router.get("/theater/{campaign_id}/air")
def theater_air(campaign_id: str, request: Request, db: Session = Depends(get_db),
                principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)
    sv = _latest_or_404(db, campaign_id)

    squadrons = list(db.scalars(
        select(CampaignUnit).where(CampaignUnit.save_id == sv.id,
                                   CampaignUnit.unit_kind == "Squadron")
        .order_by(CampaignUnit.team_id, CampaignUnit.name)))
    packages = list(db.scalars(
        select(CampaignUnit).where(CampaignUnit.save_id == sv.id,
                                   CampaignUnit.unit_kind == "Package")
        .order_by(CampaignUnit.team_id, CampaignUnit.unit_id)))
    flights = list(db.scalars(
        select(CampaignUnit).where(CampaignUnit.save_id == sv.id,
                                   CampaignUnit.unit_kind == "Flight")
        .order_by(CampaignUnit.team_id, CampaignUnit.unit_id)))

    return render(request, "theater/air.html", {
        **_nav_ctx(db, camp, principal), "save": sv,
        "squadrons": [_sq_row(u) for u in squadrons],
        "packages": [_pkg_row(u) for u in packages], "flights": flights,
        "aircraft_counts": Counter(f.aircraft_type or "未识别" for f in flights).most_common(),
        "mission_counts": Counter(f.mission_name or "未识别" for f in flights).most_common(),
        "team_names": {t.team_id: t.name for t in db.scalars(
            select(CampaignTeamState).where(CampaignTeamState.save_id == sv.id))},
    })


def _pkg_row(u: CampaignUnit) -> dict:
    """编队行 + ``extra_json`` → 模板用的 dict。"""
    e = json.loads(u.extra_json or "{}")
    return {
        "unit": u,
        "flights": e.get("flights"),
        "is_final": e.get("is_final"),
        "takeoff_ms": e.get("takeoff"),
        "requests": e.get("requests"),
        "responses": e.get("responses"),
        "interceptor": e.get("interceptor", {}).get("num") if isinstance(e.get("interceptor"), dict) else None,
        "tanker": e.get("tanker", {}).get("num") if isinstance(e.get("tanker"), dict) else None,
        "awacs": e.get("awacs", {}).get("num") if isinstance(e.get("awacs"), dict) else None,
        "ingress": (e.get("iax"), e.get("iay")),
        "egress": (e.get("eax"), e.get("eay")),
        "target": (e.get("tpx"), e.get("tpy")),
    }


def _sq_row(u: CampaignUnit) -> dict:
    """把中队行 + ``extra_json`` 摊平成模板好用的 dict。"""
    e = json.loads(u.extra_json or "{}")
    return {
        "unit": u, "leader": u.name or "（无名）",
        "airbase": e.get("airbase_name") or "—",
        "strength": e.get("strength_pct"),
        "specialty": e.get("specialty"),
        "missions": e.get("missions_flown"),
        "aa": e.get("aa_kills"), "ag": e.get("ag_kills"),
        "as": e.get("as_kills"), "an": e.get("an_kills"),
        "losses": e.get("total_losses"), "pilot_losses": e.get("pilot_losses"),
        "camp_id": e.get("camp_id"),
    }


# ==========================================================================
# 地面与海军
# ==========================================================================

@router.get("/theater/{campaign_id}/ground")
def theater_ground(campaign_id: str, request: Request, db: Session = Depends(get_db),
                   principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)
    sv = _latest_or_404(db, campaign_id)

    ground = list(db.scalars(
        select(CampaignUnit).where(
            CampaignUnit.save_id == sv.id,
            CampaignUnit.unit_kind.in_(("Battalion", "Brigade", "Division")))
        .order_by(CampaignUnit.unit_kind, CampaignUnit.team_id,
                  CampaignUnit.unit_id)))
    naval = list(db.scalars(
        select(CampaignUnit).where(CampaignUnit.save_id == sv.id,
                                   CampaignUnit.unit_kind == "TaskForce")
        .order_by(CampaignUnit.team_id, CampaignUnit.unit_id)))

    return render(request, "theater/ground.html", {
        **_nav_ctx(db, camp, principal), "save": sv,
        "ground": ground, "naval": naval,
        "ground_by_kind": Counter(u.unit_kind for u in ground).most_common(),
        "team_names": {t.team_id: t.name for t in db.scalars(
            select(CampaignTeamState).where(CampaignTeamState.save_id == sv.id))},
        "unit_kind_cn": UNIT_KIND_CN,
    })


# ==========================================================================
# 目标点
# ==========================================================================

@router.get("/theater/{campaign_id}/objectives")
def theater_objectives(campaign_id: str, request: Request,
                       db: Session = Depends(get_db),
                       principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)
    sv = _latest_or_404(db, campaign_id)

    tname = request.query_params.get("type") or ""
    owner = request.query_params.get("owner") or ""

    stmt = select(CampaignObjective).where(CampaignObjective.save_id == sv.id)
    if tname:
        stmt = stmt.where(CampaignObjective.type_name == tname)
    if owner != "":
        try:
            stmt = stmt.where(CampaignObjective.team_id == int(owner))
        except ValueError:
            pass
    objs = list(db.scalars(stmt.order_by(CampaignObjective.type_name,
                                         CampaignObjective.name)))

    counts = dict(db.execute(
        select(CampaignObjective.type_name, func.count())
        .where(CampaignObjective.save_id == sv.id)
        .group_by(CampaignObjective.type_name)).all())
    owned = dict(db.execute(
        select(CampaignObjective.team_id, func.count())
        .where(CampaignObjective.save_id == sv.id)
        .group_by(CampaignObjective.team_id)).all())

    return render(request, "theater/objectives.html", {
        **_nav_ctx(db, camp, principal), "save": sv,
        "objs": objs,
        "type_counts": sorted(counts.items(), key=lambda x: -x[1]),
        "owned_counts": sorted(owned.items()),
        "sel_type": tname, "sel_owner": owner,
        "team_names": {t.team_id: t.name for t in db.scalars(
            select(CampaignTeamState).where(CampaignTeamState.save_id == sv.id))},
    })


# ==========================================================================
# 战役进程 / 胜负评估
# ==========================================================================

@router.get("/theater/{campaign_id}/timeline")
def theater_timeline(campaign_id: str, request: Request, db: Session = Depends(get_db),
                     principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)

    saves = svc.saves_for(db, camp.id)
    changes = svc.objective_changes(db, camp.id, limit=300)

    # 每份存档的目标点归属快照（用于折线）
    series: dict[int, list[tuple[str, int]]] = defaultdict(list)
    team_names: dict[int, str] = {}
    for sv in saves:
        try:
            owned = json.loads(sv.owned_by_team_json or "{}")
        except ValueError:
            owned = {}
        for k, v in owned.items():
            series[int(k)].append((sv.campaign_time_label or "", v))
    if saves:
        for ts in db.scalars(select(CampaignTeamState).where(
                CampaignTeamState.save_id.in_([s.id for s in saves]))):
            team_names.setdefault(ts.team_id, ts.name)

    # 易手统计：谁从谁手里拿走了多少
    grabs = Counter()
    for c in changes:
        grabs[(c.to_team, c.from_team)] += 1

    return render(request, "theater/timeline.html", {
        **_nav_ctx(db, camp, principal),
        "saves": saves,
        "changes": changes,
        "series": {k: v for k, v in sorted(series.items())},
        "team_names": team_names,
        "grabs": grabs.most_common(12),
        "stance_name": stance_name,
        "palette": ["#4f8cff", "#ff5c5c", "#3fbf7f", "#ffcc4d",
                    "#a86bff", "#ff8a3d", "#38c7c7", "#c0c0c0"],
    })


# ==========================================================================
# 存档列表
# ==========================================================================

@router.get("/theater/{campaign_id}/saves")
def theater_saves(campaign_id: str, request: Request, db: Session = Depends(get_db),
                  principal: Principal = Depends(require(CAMPAIGN_VIEW))):
    camp = _load_campaign(db, campaign_id)
    return render(request, "theater/saves.html", {
        **_nav_ctx(db, camp, principal),
        "all_saves": list(db.scalars(
            select(CampaignSave).where(CampaignSave.campaign_id == camp.id)
            .order_by(CampaignSave.campaign_time_ms))),
    })
