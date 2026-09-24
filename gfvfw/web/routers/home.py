"""首页路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AcmiFile, Member, Mission, Sortie
from ...services.stats import overview
from ..deps import Principal, get_db, get_principal
from ..templating import render

router = APIRouter()


@router.get("/")
def home(request: Request,
         principal: Principal = Depends(get_principal),
         db: Session = Depends(get_db)):

    def count(model) -> int:                                  # noqa: ANN001
        return db.scalar(select(func.count()).select_from(model)) or 0

    # 两个时长口径必须一起出现：只给「任务总时长」会低估联队飞行量，
    # 只给「飞行员累计」会让单个任务看起来比实际长 N 倍。见 services/stats.py。
    ov = overview(db)

    # ⚠️ 成员计数排除已作废（deleted_at 非空）——作废呼号不得进「系统状态」。
    #    作废只标 deleted_at、不改 status，所以 active 计数同样要过滤。
    stats = {
        "members_total": db.scalar(
            select(func.count()).select_from(Member)
            .where(Member.deleted_at.is_(None))) or 0,
        "members_active": db.scalar(
            select(func.count()).select_from(Member)
            .where(Member.deleted_at.is_(None), Member.status == "active")) or 0,
        "missions": count(Mission),
        "sorties": count(Sortie),
        "acmi_files": count(AcmiFile),
        "flight_seconds": ov["flight_seconds"],
        "pilot_flight_seconds": ov["pilot_flight_seconds"],
        "distance_meters": ov["distance_meters"],
    }
    return render(request, "home.html", {"stats": stats})
