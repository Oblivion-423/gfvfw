"""
轻量 schema 同步（开发期迁移）

⚠️ 为什么需要这个模块
--------------------
``Base.metadata.create_all()`` **只创建缺失的表，不会给已存在的表加列或索引**。

后果：开发中给模型加一个字段后，
  * 测试用全新建库 → 一切正常
  * 已有数据库 → 查询该字段直接 ``no such column``，页面 500

这是一类**只在真实环境暴露、测试完全看不见**的缺陷。本次就在 ACMI 上传页
首次访问时踩到了（``acmi_files.sortie_summaries_json`` 缺失）。

本模块的做法
------------
启动时对比「模型定义」与「数据库实际结构」，**只补**：

* 缺失的列（``ALTER TABLE ... ADD COLUMN``）
* 缺失的索引（``CREATE INDEX IF NOT EXISTS ...``）

并且：

* **绝不删除 / 改名 / 改类型 / 删索引** —— 那需要真正的迁移与数据搬迁
* 补列 / 补索引时记录 WARNING，提示应改用 Alembic
* 无法自动处理的情况（NOT NULL 无默认值、表达式索引）只报告，不强行执行
* ⚠️ **顺序依赖**：同一张表必须先补列、再补索引 ——
  新索引可能引用刚补的列，反过来会 ``no such column``

⚠️ **补列的默认值只认 ORM 层 ``default=``**（见 ``_sql_literal_for_default``）。
模型里必须同时写 ``default=""`` 和 ``server_default=text("''")``：
前者供本模块生成 DDL，后者供 ``create_all`` 建新表时使用。

一期采用此方案的理由：表结构刚定稿、尚无生产数据、单人维护。
**一旦上线有真实数据，必须切换到 Alembic 管理变更**（已在报告里提示）。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from ..db import Base

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认值提取
# ---------------------------------------------------------------------------

def _sql_literal_for_default(column) -> str | None:      # noqa: ANN001
    """为 ADD COLUMN 生成默认值子句（SQLite 要求加 NOT NULL 列必须带默认值）。

    ⚠️ 只识别 ORM 层的 ``default=``（``Column.default``）。
    若模型里写了 ``server_default=text("''")`` 却漏了 ``default=""``，
    本函数返回 None，补列被跳过并记 WARNING —— 两者必须同时写。
    """
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


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

def _index_column_names(index) -> list[str] | None:      # noqa: ANN001
    """从 Index 对象提取列名（按声明顺序）。

    只支持简单列索引；表达式索引（``lower(col)`` 等）返回 None，
    调用方应跳过并记 WARNING —— 跨方言重放表达式索引是危险的。
    """
    names: list[str] = []
    for expr in index.expressions:
        name = getattr(expr, "name", None)
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return names or None


def _render_create_index(table_name: str, index,                 # noqa: ANN001
                         columns: list[str]) -> str:
    """生成 ``CREATE [UNIQUE] INDEX IF NOT EXISTS ...``。

    手工拼 SQL 而非调 ``Index.create()``：后者不带 IF NOT EXISTS，且行为
    随方言/版本波动；手工拼一句话，可测、幂等、跨 SQLite/PG 通用。
    """
    unique = "UNIQUE " if index.unique else ""
    cols = ", ".join('"%s"' % c for c in columns)
    return (
        'CREATE %sINDEX IF NOT EXISTS "%s" ON "%s" (%s)'
        % (unique, index.name, table_name, cols)
    )


def _sync_indexes(engine: Engine, table_name: str,
                  table, applied: list[str]) -> None:        # noqa: ANN001
    """补 table 上缺失的索引。绝不 DROP 已存在但模型里没有的索引。"""
    # ⚠️ 每次新建 inspector：上一步可能刚 ALTER TABLE 加完列，
    # 复用旧 inspector 拿到的快照未必反映最新结构。
    fresh = inspect(engine)
    existing = {ix["name"] for ix in fresh.get_indexes(table_name)}

    for index in table.indexes:
        if index.name in existing:
            continue
        cols = _index_column_names(index)
        if cols is None:
            log.warning(
                "表 %s 的索引 %s 含表达式或无名列，schema_sync 不支持自动创建 —— "
                "请改用 Alembic 迁移", table_name, index.name)
            continue

        ddl = _render_create_index(table_name, index, cols)
        with engine.begin() as conn:
            conn.execute(text(ddl))
        applied.append(ddl)
        log.warning("自动补索引：%s（此为开发期权宜方案，上线后应改用 Alembic）",
                    ddl)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def sync_schema(engine: Engine) -> list[str]:
    """把模型新增的列与索引补到数据库上。返回执行的 DDL 清单（空 = 无需变更）。

    仅执行 ``ALTER TABLE ADD COLUMN`` 与 ``CREATE INDEX IF NOT EXISTS``；
    不做任何破坏性操作。

    ⚠️ 每张表内部顺序固定为「先补列、后补索引」：新索引可能引用刚补的列。
    """
    applied: list[str] = []
    inspector = inspect(engine)

    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue                                  # create_all 会建新表

        # ---- 1. 补列 ----
        existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue

            coltype = column.type.compile(dialect=engine.dialect)
            clause = "%s %s" % (column.name, coltype)

            if not column.nullable:
                literal = _sql_literal_for_default(column)
                if literal is None:
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

        # ---- 2. 补索引（必须在补列之后）----
        _sync_indexes(engine, table_name, table, applied)

    return applied


def ensure_schema_and_sync(engine: Engine) -> list[str]:
    """建表 + 补列 + 补索引。返回执行的 DDL 清单。"""
    from .. import models  # noqa: F401  确保模型注册
    Base.metadata.create_all(engine)
    return sync_schema(engine)