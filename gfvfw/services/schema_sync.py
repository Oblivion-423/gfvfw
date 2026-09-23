"""
轻量 schema 同步（开发期迁移）

⚠️ 为什么需要这个模块
--------------------
``Base.metadata.create_all()`` **只创建缺失的表，不会给已存在的表加列**。

后果：开发中给模型加一个字段后，
  * 测试用全新建库 → 一切正常
  * 已有数据库 → 查询该字段直接 ``no such column``，页面 500

这是一类**只在真实环境暴露、测试完全看不见**的缺陷。本次就在 ACMI 上传页
首次访问时踩到了（``acmi_files.sortie_summaries_json`` 缺失）。

本模块的做法
------------
启动时对比「模型定义」与「数据库实际列」，**只补缺失的列**
（``ALTER TABLE ... ADD COLUMN``），并且：

* **绝不删除/改名/改类型** —— 那需要真正的迁移与数据搬迁
* 补列时记录 WARNING，提示应改用 Alembic
* 无法自动处理的情况（如 NOT NULL 且无默认值）只报告，不强行执行

一期采用此方案的理由：表结构刚定稿、尚无生产数据、单人维护。
**一旦上线有真实数据，必须切换到 Alembic 管理变更**（已在报告里提示）。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

from ..db import Base

log = logging.getLogger(__name__)


def _sql_literal_for_default(column) -> str | None:      # noqa: ANN001
    """为 ADD COLUMN 生成默认值子句（SQLite 要求加 NOT NULL 列必须带默认值）。"""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return "'%s'" % escaped
    return None


def sync_schema(engine: Engine) -> list[str]:
    """把模型新增的列补到数据库上。返回执行的变更列表（空 = 无需变更）。

    仅执行 ``ALTER TABLE ADD COLUMN``；不做任何破坏性操作。
    """
    applied: list[str] = []
    inspector = inspect(engine)

    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue                                  # create_all 会建新表
        existing = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing:
                continue

            coltype = column.type.compile(dialect=engine.dialect)
            clause = "%s %s" % (column.name, coltype)

            if not column.nullable:
                literal = _sql_literal_for_default(column)
                if literal is None:
                    # 无法安全补列：只报告，不强行执行（避免破坏已有数据）
                    log.warning(
                        "表 %s 缺少非空列 %s 且无默认值，无法自动补列 —— "
                        "请改用 Alembic 迁移并手工处理数据",
                        table_name, column.name)
                    continue
                clause += " NOT NULL DEFAULT %s" % literal

            stmt = "ALTER TABLE %s ADD COLUMN %s" % (table_name, clause)
            with engine.begin() as conn:
                conn.execute(text(stmt))
            applied.append(stmt)
            log.warning("自动补列：%s（此为开发期权宜方案，上线后应改用 Alembic）",
                        stmt)

    return applied


def ensure_schema_and_sync(engine: Engine) -> list[str]:
    """建表 + 补列。返回补列清单。"""
    from .. import models  # noqa: F401  确保模型注册
    Base.metadata.create_all(engine)
    return sync_schema(engine)
