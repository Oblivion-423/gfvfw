"""
站点初始化：建表与基础数据播种。

为什么需要"播种"
----------------
军衔 7 级、机型 9 型、角色 5 个、飞行员别名表都是**已确认的初始数据**
（docs/database-design.md §7.1 / §7.2 / 权限模型），
不应每次部署靠人手输入。此模块把已确认的初始数据写成代码。

**幂等**：可重复调用，已存在的记录不会重复插入，也不会覆盖人工修改。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AircraftAlias, AircraftType, Rank, Role, RolePermission
from ..permissions import ROLE_DEFINITIONS

log = logging.getLogger(__name__)


#: 军衔 7 级，level 对齐 BMS Logbook 序号（文档 §7.1）
RANKS = [
    (1, "少尉", "Second Lieutenant", "2Lt"),
    (2, "中尉", "First Lieutenant", "1Lt"),
    (3, "上尉", "Captain", "Capt"),
    (4, "少校", "Major", "Maj"),
    (5, "中校", "Lieutenant Colonel", "Lt Col"),
    (6, "上校", "Colonel", "Col"),
    (7, "准将", "Brigadier General", "Brig Gen"),
]

#: 联队执飞机型 9 型（文档 §7.2）
AIRCRAFT_TYPES = [
    ("F-16C Block 30", "F-16", 10),
    ("F-16C Block 40", "F-16", 20),
    ("F-16C Block 50", "F-16", 30),
    ("F-16C Block 52", "F-16", 40),
    ("F-16C Block 52+", "F-16", 50),
    ("F-16C Block 52M", "F-16", 60),
    ("F-15A", "F-15", 70),
    ("F-15C", "F-15", 80),
    ("F-15E", "F-15", 90),
]

#: 机型别名种子数据 —— 实测 193 份 ACMI 提取（文档 §7.2）
#: 格式 (ACMI 原始名, 标准机型, 是否被飞行员实际驾驶)
AIRCRAFT_ALIASES = [
    # ---- 人驾（影响统计，必须精确）----
    ("F-15E-229", "F-15E", True),
    ("F-15E-220", "F-15E", True),
    ("F-16C B52M HAF", "F-16C Block 52M", True),
    ("F-16CM-52", "F-16C Block 52", True),
    ("F-16CM-40", "F-16C Block 40", True),
    ("F-16DM-52", "F-16C Block 52", True),
    ("F-15C", "F-15C", True),
    # ---- AI / 盟军（归档用，抑制"未知机型"告警）----
    ("F-16C-52 ROKAF", "F-16C Block 52", False),
    ("F-16C-32 ROKAF", "F-16C Block 30", False),
    ("F-16C-32 EAF", "F-16C Block 30", False),
    ("F-16C-30 IAF", "F-16C Block 30", False),
    ("F-16C B30 THK", "F-16C Block 30", False),
    ("F-16C B30 HAF", "F-16C Block 30", False),
    ("F-16C B40 THK", "F-16C Block 40", False),
    ("F-16CM-50", "F-16C Block 50", False),
    ("F-16C B50 HAF", "F-16C Block 50", False),
    ("F-16C B50 THK", "F-16C Block 50", False),
    ("F-16C B50+ THK", "F-16C Block 50+", False),
    ("F-16C 50+ THK", "F-16C Block 50+", False),
    ("F-16C B52+ HAF", "F-16C Block 52+", False),
    ("F-16A-15 IAF", "F-16C Block 30", False),
    ("F-16B-15 IAF", "F-16C Block 30", False),
    ("F-15A IAF", "F-15A", False),
    ("F-15K", "F-15E", False),
    ("F-16C-52 ROKAF ", "F-16C Block 52", False),
]


def seed(db: Session) -> dict[str, int]:
    """播种基础数据。返回各表新建记录数。"""
    created = {"ranks": 0, "aircraft_types": 0, "aircraft_aliases": 0,
               "roles": 0, "role_permissions": 0}

    # --- 军衔 ---
    for level, name, name_en, abbrev in RANKS:
        row = db.scalar(select(Rank).where(Rank.level == level))
        if row is None:
            db.add(Rank(level=level, name=name, name_en=name_en,
                        abbrev=abbrev, tier="officer"))
            created["ranks"] += 1

    # --- 机型 ---
    for name, family, order in AIRCRAFT_TYPES:
        row = db.scalar(select(AircraftType).where(AircraftType.name == name))
        if row is None:
            db.add(AircraftType(name=name, family=family, sort_order=order))
            created["aircraft_types"] += 1

    db.flush()

    # --- 机型别名 ---
    for raw, standard, _human in AIRCRAFT_ALIASES:
        raw = raw.strip()
        if not raw:
            continue
        exists = db.scalar(select(AircraftAlias).where(AircraftAlias.raw_name == raw))
        if exists is not None:
            continue
        at = db.scalar(select(AircraftType).where(AircraftType.name == standard))
        if at is None:
            at = AircraftType(name=standard)
            db.add(at)
            db.flush()
        db.add(AircraftAlias(raw_name=raw, aircraft_type_id=at.id))
        created["aircraft_aliases"] += 1

    # --- 角色与权限点 ---
    for code, (name, level, perms) in ROLE_DEFINITIONS.items():
        role = db.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, name=name, level=level, is_builtin=True)
            db.add(role)
            db.flush()
            created["roles"] += 1
        for p in sorted(perms):
            exists = db.scalar(select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission == p))
            if exists is None:
                db.add(RolePermission(role_id=role.id, permission=p))
                created["role_permissions"] += 1

    db.commit()
    log.info("基础数据播种完成：%s", created)
    return created


def ensure_schema(engine) -> list[str]:                     # noqa: ANN001
    """建表 + 补齐缺失列（幂等）。返回执行的补列语句列表。

    ⚠️ 只用 ``create_all`` **不够**：它不会给已存在的表加列。
    因此额外调用 :func:`gfvfw.services.schema_sync.sync_schema`，
    否则开发中新增字段后，已有数据库会直接查询失败。

    一期不引入 Alembic 的原因：表结构刚定稿、无生产数据、单人维护。
    **上线有真实数据后必须切换 Alembic**（见 schema_sync 模块说明）。
    """
    from .schema_sync import ensure_schema_and_sync
    applied = ensure_schema_and_sync(engine)
    if applied:
        log.warning("启动时补齐了 %d 个缺失列，请确认是否为预期的模型变更", len(applied))
    return applied
