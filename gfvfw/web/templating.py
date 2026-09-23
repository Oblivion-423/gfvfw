"""
模板环境：Jinja2 配置与自定义过滤器。

设计要点
--------
* **时间一律从 UTC 转 UTC+8 展示**。过滤器集中在这里，
  避免每个模板各写各的转换逻辑。
* 单位换算（海里）也集中在此，见 :data:`METERS_PER_NM`。
* 模板渲染时注入 ``principal`` 与 ``csrf_token`` ——
  所有页面都能用到，不必每个路由手动传递。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..permissions import ROLE_DEFINITIONS

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: 展示时区（UTC+8）。存储始终是 UTC。
DISPLAY_TZ = timezone(timedelta(hours=8))
DISPLAY_TZ_LABEL = "UTC+8"


# --------------------------------------------------------------------------
# 过滤器
# --------------------------------------------------------------------------

def to_display_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """UTC → 展示时区。SQLite 会丢 tzinfo，这里补上 UTC 再转换。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


def filter_datetime(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    d = to_display_tz(dt)
    return d.strftime(fmt) if d else "—"


def filter_date(dt: Optional[datetime]) -> str:
    return filter_datetime(dt, "%Y-%m-%d")


def filter_time(dt: Optional[datetime]) -> str:
    return filter_datetime(dt, "%H:%M")


def filter_filesize(num: Optional[int]) -> str:
    if num is None:
        return "—"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % int(n)
        n /= 1024.0
    return "%.1f TB" % n


def filter_duration(seconds: Optional[float]) -> str:
    """秒 → ``1小时23分`` / ``45分`` / ``3分59秒`` / ``30秒``（联队口径：小时分钟）。

    ⚠️ 秒数**非零时保留**，不做截断。
    早期实现只在"完全没有分钟"时才显示秒，导致 119 秒显示成"1分"、
    239 秒显示成"3分" —— **静默丢掉 59 秒**。短架次的精度因此失真。
    """
    if not seconds:
        return "—"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        # 有小时则秒可省略（小时级时长里秒无意义）
        return "%d小时%d分" % (h, m)
    if m and sec:
        return "%d分%d秒" % (m, sec)
    if m:
        return "%d分" % m
    return "%d秒" % sec


#: 1 海里 = 1852 米（国际标准）。
#: ⚠️ 唯一定义点 —— 从统计服务导入，避免两处各写一份而日后不一致。
from ..services.stats import METERS_PER_NM  # noqa: E402


def filter_distance(meters: Optional[float]) -> str:
    """米 → 海里（联队口径）。

    ⚠️ 航程一律以**海里**展示 —— 这是航空领域的标准单位，
    与 BMS 内的 HUD/简报一致，避免队员在两种单位间换算。
    """
    if meters is None:
        return "—"
    nm = float(meters) / METERS_PER_NM
    if nm >= 100:
        return "%.0f NM" % nm
    if nm >= 10:
        return "%.1f NM" % nm
    return "%.2f NM" % nm


def filter_distance_nm(meters: Optional[float]) -> str:
    """只要数字（不含单位），便于在表格里对齐。"""
    if meters is None:
        return "—"
    return "%.0f" % (float(meters) / METERS_PER_NM)


def filter_role_name(code: Optional[str]) -> str:
    if not code:
        return "—"
    defn = ROLE_DEFINITIONS.get(code)
    return defn[0] if defn else code


def filter_callsign(value: Optional[str]) -> str:
    """呼号脱敏展示（需求 Q7：不展示任何真实身份信息）。

    呼号本身可公开，此处仅做空白清理与占位。
    """
    if not value:
        return "未认领"
    return value


def filter_unknown(value: Any, placeholder: str = "—") -> str:
    if value is None or value == "":
        return placeholder
    return str(value)


def filter_fromjson(value: Any, default: Any = None) -> Any:
    """把 JSON 字符串解析成对象；解析失败或为空时返回 ``default``。

    用于把库里存的 ``*_json`` 文本字段（例如战役存档的
    ``owned_by_team_json``）在模板里直接当结构体用。
    """
    if value is None or value == "":
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if default is None else default


for name, fn in (
    ("datetime", filter_datetime),
    ("date", filter_date),
    ("time", filter_time),
    ("filesize", filter_filesize),
    # ⚠️ 不要用 "duration"：Jinja2 内置了同名过滤器（语义不同），
    #    覆盖它会让模板里 |duration 的行为变得难以预测。
    ("dur", filter_duration),
    ("distance", filter_distance),
    ("nm", filter_distance_nm),
    ("role_name", filter_role_name),
    ("callsign", filter_callsign),
    ("or_dash", filter_unknown),
    ("fromjson", filter_fromjson),
):
    templates.env.filters[name] = fn

templates.env.globals["DISPLAY_TZ_LABEL"] = DISPLAY_TZ_LABEL
templates.env.globals["SITE_NAME"] = settings.site_name
templates.env.globals["SITE_NAME_EN"] = settings.site_name_en
templates.env.globals["SITE_ABBR"] = settings.site_abbr


#: 查询串提示的最大长度。见 :func:`_flash_from_query`。
_FLASH_MAX = 200


def _flash_from_query(request: Request) -> dict:
    """从查询串取一次性提示（``?message=`` / ``?error=`` / ``?warning=``）。

    ⚠️ 这是**反射**行为 —— URL 里的文本会显示在页面上，所以做了三重限制：
    只认这三个 key、长度截断、剔除尖括号（模板本就自动转义，这里是第二道）。
    各路由用 ``RedirectResponse("...?message=已归入 3 个任务")`` 传回执，
    在此之前这些回执其实**从未显示过** —— ``message`` 一直没人回填。
    """
    out: dict[str, str] = {}
    for key in ("message", "error", "warning"):
        raw = request.query_params.get(key)
        if not raw:
            continue
        v = raw.replace("<", "").replace(">", "").strip()[:_FLASH_MAX]
        if v:
            out[key] = v
    return out


def render(request: Request, template: str, context: Optional[dict] = None,
           status_code: int = 200):
    """渲染模板，自动注入通用上下文。

    ``principal`` 与 ``csrf_token`` 由中间件写入 ``request.state``，
    因此每个路由都不必重复传参。
    """
    from .deps import ANONYMOUS
    from ..security import get_csrf_token

    ctx: dict[str, Any] = {
        "request": request,
        "principal": getattr(request.state, "principal", None) or ANONYMOUS,
        "csrf_token": get_csrf_token(request),
        "current_path": request.url.path,
    }
    # 查询串提示先放，显式传参优先于它
    ctx.update(_flash_from_query(request))
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)
