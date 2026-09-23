"""首页路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AcmiFile, Member, Mission, Sortie
from ..deps import Principal, get_db, get_principal
from ..templating import render

router = APIRouter()


@router.get("/")
def home(request: Request,
         principal: Principal = Depends(get_principal),
         db: Session = Depends(get_db)):

    def count(model) -> int:                                  # noqa: ANN001
        return db.scalar(select(func.count()).select_from(model)) or 0

    stats = {
        "members_total": count(Member),
        "members_active": db.scalar(
            select(func.count()).select_from(Member).where(Member.status == "active")) or 0,
        "missions": count(Mission),
        "sorties": count(Sortie),
        "acmi_files": count(AcmiFile),
    }
    return render(request, "home.html", {"stats": stats})
