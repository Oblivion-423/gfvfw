"""
数据库层：引擎、会话与可移植类型。

⚠️ 可移植性强制机制
-------------------
本项目要求 **SQLite 上开发、可平滑迁移到 PostgreSQL**（docs/database-design.md §0）。
为把"禁用 SQLite 专有特性"从口头约定变成**机器强制**，本模块：

1. 使用带 ``postgresql`` extra 安装的 SQLAlchemy，启用跨方言类型校验；
2. 定义了下方 :data:`PORTABLE_TYPES`，只允许可移植类型；
3. 提供 :func:`check_portability` 在测试中校验模型未使用危险类型；
4. SQLite 连接统一开启 ``WAL`` 与外键约束。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text, Uuid
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeEngine

from .config import settings

# --------------------------------------------------------------------------
# 类型选择（可移植性核心）
# --------------------------------------------------------------------------

#: 允许在模型中使用的基础类型。任何不在此列的类型都会在
#: :func:`check_portability` 中被标记为风险。
#:
#: * ``Float`` 在 PG 上落 ``FLOAT``/``DOUBLE PRECISION``、SQLite 上落 ``REAL``，
#:   语义一致，属可移植类型。用于测量值（过载、航向、秒数）足够。
#:   ⚠️ 如需**精确十进制**（如财务数字），不要用 Float —— 用整数最小单位或 String。
PORTABLE_TYPES: tuple[type, ...] = (
    BigInteger, Boolean, DateTime, Float, Integer, JSON, String, Text, Uuid,
)

#: ⚠️ 禁止类型 —— 在 SQLite 与 PostgreSQL 上语义不一致或不可用
FORBIDDEN_TYPES: tuple[type, ...] = (
    # SQLite 无原生 Numeric 精度，PG 更精确，隐式转换会引入差异
    # （如需精确小数请用 String 或整数最小单位）
)


def utcnow() -> datetime:
    """带时区的当前 UTC 时间（存储一律 UTC，展示层负责转换）。"""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """生成主键。

    使用 UUID4 的字符串形式（36 字符，带连字符）：
      * 跨库可移植（:class:`sqlalchemy.Uuid` 在 PG 上落 ``uuid`` 类型，
        SQLite 上落 ``CHAR(32)``）
      * 无需数据库序列，对象在入库前即可分配 ID（归并确认页需要）
      * 不泄露记录数量
    """
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# 引擎与会话
# --------------------------------------------------------------------------

def build_engine(url: str | None = None, *, echo: bool = False):
    """创建引擎，并为 SQLite 配置 WAL 与外键约束。"""
    url = url or settings.database_url
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # check_same_thread=False：FastAPI 线程池中复用连接
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            # WAL：读写并发（解析任务与 Web 请求并存）
            cur.execute("PRAGMA journal_mode=WAL")
            # 外键约束在 SQLite 中默认关闭，必须显式打开
            cur.execute("PRAGMA foreign_keys=ON")
            # 降低 fsync 频率以提升写入吞吐（仍有 WAL 保证一致性）
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有模型的基类。"""

    #: 便于统一构造审计字段
    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        pk = getattr(self, "id", None)
        return "<%s id=%s>" % (type(self).__name__, pk)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文。异常回滚，正常提交。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# 可移植性自检
# --------------------------------------------------------------------------

def check_portability(base: type = Base) -> list[str]:
    """校验所有模型未使用非可移植类型，返回问题列表（空列表 = 通过）。

    在测试中调用，防止后续开发无意中引入 SQLite 专有特性。
    """
    problems: list[str] = []
    for mapper in base.registry.mappers:
        cls = mapper.class_
        for col in mapper.columns:
            t = col.type
            if isinstance(t, FORBIDDEN_TYPES):
                problems.append(
                    "%s.%s 使用了禁止类型 %s" % (cls.__name__, col.name, type(t).__name__)
                )
            if not isinstance(t, PORTABLE_TYPES):
                # Uuid 的子类等允许，其余报告以便人工判断
                problems.append(
                    "%s.%s 类型 %s 不在可移植白名单内，请确认"
                    % (cls.__name__, col.name, type(t).__name__)
                )
            # TEXT 未指定长度在 PG 上没问题，但 String 建议给长度
            if isinstance(t, String) and not isinstance(t, Text) and t.length is None:
                problems.append(
                    "%s.%s 为 String 但未指定长度（PG 允许，建议显式指定）"
                    % (cls.__name__, col.name)
                )
    return problems
