"""library 自检：Document 模型形状 + schema_sync 的补列 / 补索引行为。

覆盖范围
--------
  §1  Document 模型形状
        - 新增列 folder / original_filename 存在且为 NOT NULL
        - 两列都同时带 ORM default 与 server_default
          （这是 schema_sync 能成功补列的前提）
        - 5 个索引在 __table_args__ 里全部声明
        - sha256 不带 UNIQUE 约束
        - 老的列没有被误改（title / category / stored_path / ...）

  §2  _sql_literal_for_default 辅助函数
        - 字符串 / 整数 / 布尔 / None 的处理
        - 单引号转义

  §3  schema_sync 补列 / 补索引
        - 空库：create_all 建表后，5 个索引全部存在
        - 旧库：手工建一张只有 ix_documents_sha256 的 documents 表，
          跑 sync_schema 后新索引补齐、老索引不动、DDL 顺序正确
          （ALTER 必须先于 CREATE INDEX）
        - 幂等：连跑两次，第二次无变更
        - 只加不删：手工加的额外索引不被删
        - 复合索引的列顺序被保留
        - NOT NULL 无 default 的列被跳过并记 WARNING

运行
----
    python tests/library_selfcheck.py
"""
from __future__ import annotations

import shutil          # ← 新增
import logging
import sys
import tempfile
from io import StringIO
from pathlib import Path

# 允许从项目根目录直接运行（与其它 selfcheck 一致）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import Column, String, Table, create_engine, event, text
from sqlalchemy.engine import Engine

from gfvfw.db import Base, SessionLocal
from gfvfw.models.site import Document
from gfvfw.services import schema_sync


# ============================================================================
# 断言基础设施
# ============================================================================

_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _assert(cond: bool, msg: str) -> None:
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
    else:
        _FAILED += 1
        _FAILURES.append(msg)
        print("  ✗ %s" % msg)


def _section(title: str) -> None:
    print("\n=== %s ===" % title)


# ============================================================================
# 基础设施
# ============================================================================

# ★ Windows 修复：SQLite 文件句柄未释放时，TemporaryDirectory 清理会
#   抛 PermissionError: [WinError 32] 另一个程序正在使用此文件。
#   所有 engine 必须登记，在清理前统一 dispose。
_ENGINES: list[Engine] = []


def _make_engine(db_file: Path) -> Engine:
    """新建一个临时 SQLite 文件引擎，打开外键约束（与生产一致）。"""
    engine = create_engine("sqlite:///%s" % db_file, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):                          # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _ENGINES.append(engine)
    return engine


def _dispose_all_engines() -> None:
    """释放所有登记的 engine 及其连接池。必须在删临时目录之前调用。"""
    for e in _ENGINES:
        try:
            e.dispose()
        except Exception:                              # noqa: BLE001
            pass                                       # dispose 失败不阻断清理
    _ENGINES.clear()


def _list_indexes(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            'SELECT name FROM sqlite_master WHERE type="index" AND tbl_name=?',
            (table,),
        ).fetchall()
    return {r[0] for r in rows}


def _list_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql('PRAGMA table_info("%s")' % table).fetchall()
    return {r[1] for r in rows}


# ============================================================================
# §1  Document 模型形状
# ============================================================================

def check_document_new_columns_exist() -> None:
    cols = {c.name for c in Document.__table__.columns}
    _assert("folder" in cols, "Document 缺 folder 列")
    _assert("original_filename" in cols, "Document 缺 original_filename 列")


def check_document_new_columns_not_null() -> None:
    for name in ("folder", "original_filename"):
        col = Document.__table__.columns[name]
        _assert(col.nullable is False, "%s 必须 NOT NULL" % name)


def check_document_new_columns_have_defaults() -> None:
    """★ 关键：default 与 server_default 必须同时存在。

    server_default 供 create_all 建新表用；
    ORM default 供 schema_sync._sql_literal_for_default 生成
    ALTER TABLE 的 DEFAULT 子句用 —— 缺了它，生产库补列会被静默跳过。
    """
    for name in ("folder", "original_filename"):
        col = Document.__table__.columns[name]
        _assert(col.server_default is not None,
                "%s 必须带 server_default（create_all 用）" % name)
        _assert(col.default is not None,
                "%s 必须带 ORM default（schema_sync 补列用）" % name)


def check_document_indexes_declared() -> None:
    declared = {ix.name for ix in Document.__table__.indexes}
    expected = {
        "ix_documents_folder_name",
        "ix_documents_folder_created",
        "ix_documents_sha256",
        "ix_documents_category_created",
        "ix_documents_visibility",
    }
    missing = expected - declared
    _assert(not missing, "Document 缺少索引声明：%s" % missing)


def check_sha256_not_unique() -> None:
    col = Document.__table__.columns["sha256"]
    _assert(col.unique is not True,
            "sha256 不应有 UNIQUE 约束（会阻碍软删除后重传）")


def check_document_legacy_columns_intact() -> None:
    """新增列不应破坏老列。"""
    required = {
        "id", "title", "category", "description", "stored_path",
        "sha256", "size_bytes", "mime_type", "version",
        "aircraft_type_id", "visibility", "download_count", "uploaded_by",
        "created_at", "updated_at", "deleted_at",
    }
    actual = {c.name for c in Document.__table__.columns}
    missing = required - actual
    _assert(not missing, "Document 丢失老列：%s" % missing)


# ============================================================================
# §2  _sql_literal_for_default
# ============================================================================

def check_sql_literal_for_default() -> None:
    fn = schema_sync._sql_literal_for_default

    c1 = Column("a", String(10), default="hello")
    _assert(fn(c1) == "'hello'", "字符串 default 应生成 'hello'")

    c2 = Column("b", String(10), default=42)
    _assert(fn(c2) == "42", "整数 default 应生成 42")

    c3 = Column("c", String(10), default=True)
    _assert(fn(c3) == "1", "布尔 True 应生成 1")

    c4 = Column("d", String(10), default=False)
    _assert(fn(c4) == "0", "布尔 False 应生成 0")

    c5 = Column("e", String(10))
    _assert(fn(c5) is None, "无 default 应返回 None")

    c6 = Column("f", String(10), default="a'b")
    _assert(fn(c6) == "'a''b'", "单引号应被转义为 ''")


# ============================================================================
# §3  schema_sync：补列 / 补索引
# ============================================================================

_LEGACY_DOCUMENTS_DDL = """
CREATE TABLE documents (
    title VARCHAR(200) NOT NULL,
    category VARCHAR(16) NOT NULL,
    description TEXT,
    stored_path VARCHAR(512) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime_type VARCHAR(128),
    version VARCHAR(32),
    aircraft_type_id VARCHAR(36),
    visibility VARCHAR(16) NOT NULL,
    download_count INTEGER NOT NULL,
    uploaded_by VARCHAR(36),
    id VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME,
    PRIMARY KEY (id)
)
"""
_LEGACY_SHA_INDEX = "CREATE INDEX ix_documents_sha256 ON documents (sha256)"


def check_sync_indexes_on_empty_db(db_file: Path) -> None:
    """空库：ensure_schema_and_sync 建表后，5 个索引全部存在。"""
    engine = _make_engine(db_file)
    schema_sync.ensure_schema_and_sync(engine)

    declared = {ix.name for ix in Document.__table__.indexes}
    existing = _list_indexes(engine, "documents")
    missing = declared - existing
    _assert(not missing, "空库建表后索引缺失：%s" % missing)


def check_sync_on_legacy_table(db_file: Path) -> None:
    """★ 模拟生产：手工建一张只有 ix_documents_sha256 的老表，
    跑 sync_schema 后新索引补齐、老索引不动、DDL 顺序正确。"""
    engine = _make_engine(db_file)
    with engine.begin() as conn:
        conn.exec_driver_sql(_LEGACY_DOCUMENTS_DDL)
        conn.exec_driver_sql(_LEGACY_SHA_INDEX)

    # 前置条件：老表缺新列、缺新索引
    cols_before = _list_columns(engine, "documents")
    _assert("folder" not in cols_before, "前置：老表不应有 folder 列")
    _assert("original_filename" not in cols_before,
            "前置：老表不应有 original_filename 列")

    applied = schema_sync.sync_schema(engine)

    # 新列被补上
    cols_after = _list_columns(engine, "documents")
    _assert("folder" in cols_after, "sync_schema 未补 folder 列")
    _assert("original_filename" in cols_after,
            "sync_schema 未补 original_filename 列")

    # 新索引被补上
    declared = {ix.name for ix in Document.__table__.indexes}
    existing = _list_indexes(engine, "documents")
    missing = declared - existing
    _assert(not missing, "sync_schema 未补索引：%s" % missing)

    # 老索引未被删
    _assert("ix_documents_sha256" in existing, "老的 sha256 索引被误删")

    # ★ DDL 顺序：ALTER 必须先于 CREATE INDEX
    # 新索引可能引用刚补的列，反过来会 "no such column"。
    first_alter = next(
        (i for i, s in enumerate(applied) if s.startswith("ALTER")), None)
    first_create = next(
        (i for i, s in enumerate(applied) if s.startswith("CREATE INDEX")), None)
    if first_alter is not None and first_create is not None:
        _assert(first_alter < first_create,
                "ALTER 必须先于 CREATE INDEX（DDL 顺序错误）")
    else:
        # 至少应有一个 ALTER（补两列）和一个 CREATE INDEX（补四个索引）
        _assert(first_alter is not None, "应至少执行一次 ALTER")
        _assert(first_create is not None, "应至少执行一次 CREATE INDEX")


def check_sync_is_idempotent(db_file: Path) -> None:
    """连跑两次，第二次无变更。"""
    engine = _make_engine(db_file)
    schema_sync.ensure_schema_and_sync(engine)
    applied2 = schema_sync.sync_schema(engine)
    _assert(not applied2,
            "sync_schema 不幂等：第二次仍执行了 %d 条 DDL" % len(applied2))


def check_sync_does_not_drop_extra(db_file: Path) -> None:
    """手工加一个模型里没声明的索引，schema_sync 不删它 —— 只加不删契约。"""
    engine = _make_engine(db_file)
    schema_sync.ensure_schema_and_sync(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            'CREATE INDEX ix_documents_legacy_custom ON documents (title)')

    schema_sync.sync_schema(engine)

    existing = _list_indexes(engine, "documents")
    _assert("ix_documents_legacy_custom" in existing,
            "sync_schema 删掉了模型未声明的索引（违反只加不删契约）")


def check_sync_composite_column_order(db_file: Path) -> None:
    """复合索引 (folder, original_filename) 的列顺序被保留。"""
    engine = _make_engine(db_file)
    schema_sync.ensure_schema_and_sync(engine)

    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            'SELECT sql FROM sqlite_master WHERE type="index" AND name=?',
            ("ix_documents_folder_name",),
        ).fetchone()
    _assert(row is not None, "ix_documents_folder_name 不存在")
    sql = (row[0] or "").lower()

    i_folder = sql.find("folder")
    i_fname = sql.find("original_filename")
    _assert(i_folder >= 0 and i_fname >= 0,
            "索引 ix_documents_folder_name 的列名未在 DDL 中出现")
    _assert(i_folder < i_fname,
            "复合索引列顺序错误：应先 folder 后 original_filename")


def check_sync_skips_not_null_no_default(db_file: Path) -> None:
    """★ 回归：NOT NULL 且无 default 的列，schema_sync 不强行加，记 WARNING。

    这是 §10.6 教训的直接防线 —— 那种列在 SQLite 上无法安全补，
    必须让部署者改用 Alembic，而不是静默跳过或强行执行后崩。
    """
    engine = _make_engine(db_file)

    probe_name = "_library_probe_notnull"
    probe = Table(
        probe_name, Base.metadata,
        Column("id", String(36), primary_key=True),
        Column("required_col", String(20), nullable=False),   # 无 default
    )
    try:
        # 手工建一张只有 id 列的老表
        with engine.begin() as conn:
            conn.exec_driver_sql(
                'CREATE TABLE %s (id VARCHAR(36) PRIMARY KEY)' % probe_name)

        # 捕获 WARNING 日志
        log_buf = StringIO()
        handler = logging.StreamHandler(log_buf)
        handler.setLevel(logging.WARNING)
        sync_logger = logging.getLogger("gfvfw.services.schema_sync")
        old_level = sync_logger.level
        sync_logger.setLevel(logging.WARNING)
        sync_logger.addHandler(handler)
        try:
            schema_sync.sync_schema(engine)
        finally:
            sync_logger.removeHandler(handler)
            sync_logger.setLevel(old_level)

        cols = _list_columns(engine, probe_name)
        _assert("required_col" not in cols,
                "NOT NULL 无 default 的列不应被自动补上")
        _assert("required_col" in log_buf.getvalue(),
                "跳过补列时必须记录 WARNING")
    finally:
        Base.metadata.remove(probe)
        with engine.begin() as conn:
            conn.exec_driver_sql('DROP TABLE IF EXISTS %s' % probe_name)

# ============================================================================
# §4  Web 层（TestClient）
# ============================================================================

_WEB_APP = None


def _build_web_app_once(tmpdir: Path):
    """建一次 Web 环境（临时库 + 猴补丁）。多次调用返回同一 app。"""
    global _WEB_APP, _WEB_ORIG, _WEB_TMP
    if _WEB_APP is not None:
        return _WEB_APP

    import datetime

    import importlib
    cfgmod = importlib.import_module("gfvfw.config")
    dbmod = importlib.import_module("gfvfw.db")
    appmod = importlib.import_module("gfvfw.web.app")
    depsmod = importlib.import_module("gfvfw.web.deps")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from gfvfw.db import Base
    from gfvfw.models import Member, User
    from gfvfw.security import hash_password
    from gfvfw.services.bootstrap import seed

    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmpdir / "lib_web.sqlite3").as_posix(),
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               expire_on_commit=False)

    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession

    orig_storage = cfgmod.settings.storage_dir
    orig_cache = cfgmod.settings.docs_cache_dir
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)
    cfgmod.settings.docs_cache_dir = tmpdir / "cache"

    now = datetime.datetime.now(datetime.timezone.utc)
    with TestSession() as db:                # ← 用 TestSession 而不是 SessionLocal
        seed(db)
        m = Member(callsign="TESTER", status="active", joined_at=now)
        db.add(m)
        db.flush()
        db.add(User(username="tester",
                    password_hash=hash_password("password123"),
                    status="active", member_id=m.id))
        db.add(User(username="guest",
                    password_hash=hash_password("password123"),
                    status="pending"))
        db.commit()

    _WEB_APP = appmod.app
    _WEB_ORIG = (orig, orig_storage, orig_cache)
    _WEB_TMP = tmpdir
    return _WEB_APP

def _extract_csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""


def _login(client, username: str, password: str) -> int:
    """走完整登录流程（含 CSRF）。"""
    r = client.get("/login")
    if r.status_code != 200:
        return r.status_code
    token = _extract_csrf(r.text)
    r = client.post("/login", data={
        "username": username, "password": password, "csrf_token": token,
    })
    return r.status_code


def _web_client():
    """新建一个不跟随重定向的 TestClient。"""
    from fastapi.testclient import TestClient
    return TestClient(_WEB_APP, follow_redirects=False)


def check_web_guest_denied_on_upload(tmpdir):
    """游客 POST /library/api/upload → 403（无 DOC_UPLOAD）"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        sc = _login(client, "guest", "password123")
        _assert(sc == 303, "游客登录应 303，得到 %d" % sc)

        r = client.post(
            "/library/api/upload",
            files={"file": ("x.txt", b"hello", "text/plain")},
            data={"csrf_token": "whatever"},
        )
        _assert(r.status_code in (403, 401),
                "游客上传应 403，得到 %d" % r.status_code)


def check_web_guest_denied_on_download(tmpdir):
    """游客 GET /library/api/download → 403"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "guest", "password123")
        r = client.get("/library/api/download?id=nonexistent")
        _assert(r.status_code == 403,
                "游客下载应 403，得到 %d" % r.status_code)


def check_web_upload_requires_csrf(tmpdir):
    """队员缺 CSRF 的上传 → 403"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "tester", "password123")
        r = client.post(
            "/library/api/upload",
            files={"file": ("x.txt", b"hello", "text/plain")},
            data={"csrf_token": "wrong-token"},
        )
        _assert(r.status_code == 403,
                "错 CSRF 应 403，得到 %d" % r.status_code)


def check_web_upload_rejects_bad_ext(tmpdir):
    """队员上传 .exe → 400"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "tester", "password123")
        r = client.get("/library")
        token = _extract_csrf(r.text)

        r = client.post(
            "/library/api/upload",
            files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
            data={"csrf_token": token},
        )
        _assert(r.status_code == 400,
                "上传 .exe 应 400，得到 %d，body=%r"
                % (r.status_code, r.text[:200]))


def check_web_upload_basic_roundtrip(tmpdir):
    """队员上传 → 树里出现 → 预览 text"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "tester", "password123")
        r = client.get("/library")
        token = _extract_csrf(r.text)

        r = client.post(
            "/library/api/upload",
            files={"file": ("hello.txt", b"hello world", "text/plain")},
            data={"csrf_token": token, "folder": "test", "title": "Hello"},
        )
        _assert(r.status_code == 200,
                "上传应 200，得到 %d，body=%r"
                % (r.status_code, r.text[:200]))
        doc_id = r.json()["id"]

        r = client.get("/library/api/tree?path=test")
        _assert(r.status_code == 200, "列目录应 200")
        names = [f["name"] for f in r.json()["files"]]
        _assert("hello.txt" in names, "树里应出现 hello.txt，实得 %s" % names)

        r = client.get("/library/api/preview?id=%s" % doc_id)
        _assert(r.status_code == 200, "预览应 200")
        _assert(r.json()["type"] == "text", "txt 预览应 type=text")
        _assert(r.json()["text"] == "hello world", "文本内容一致")


def check_web_duplicate_upload_409(tmpdir):
    """同一 sha256 二次上传 → 409 + existing_doc_id"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "tester", "password123")
        r = client.get("/library")
        token = _extract_csrf(r.text)

        r1 = client.post(
            "/library/api/upload",
            files={"file": ("a.txt", b"same content here", "text/plain")},
            data={"csrf_token": token},
        )
        _assert(r1.status_code == 200,
                "首次上传应 200，得到 %d，body=%r"
                % (r1.status_code, r1.text[:200]))

        r2 = client.post(
            "/library/api/upload",
            files={"file": ("b.txt", b"same content here", "text/plain")},
            data={"csrf_token": token},
        )
        _assert(r2.status_code == 409,
                "重复上传应 409，得到 %d，body=%r"
                % (r2.status_code, r2.text[:200]))
        if r2.status_code == 409:
            detail = r2.json().get("detail", {})
            _assert(detail.get("existing_doc_id") == r1.json()["id"],
                    "409 应带 existing_doc_id")


def check_web_path_traversal_blocked(tmpdir):
    """路径穿越 → 400"""
    _build_web_app_once(tmpdir)
    with _web_client() as client:
        _login(client, "tester", "password123")
        r = client.get("/library/api/tree?path=../../etc")
        _assert(r.status_code == 400,
                "路径穿越应 400，得到 %d" % r.status_code)

# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    global _PASSED, _FAILED
    _PASSED = 0
    _FAILED = 0
    _FAILURES.clear()

    # ★ 不用 TemporaryDirectory：它的 __exit__ 在 Windows 上可能在
    #   engine 未 dispose 时抢先删目录。改为手工 mkdtemp + finally，
    #   保证 dispose 一定先于 rmtree 执行。
    tmpdir = tempfile.mkdtemp(prefix="gfvfw_lib_")
    try:
        tmp = Path(tmpdir)

        _section("§1  Document 模型形状")
        check_document_new_columns_exist()
        check_document_new_columns_not_null()
        check_document_new_columns_have_defaults()
        check_document_indexes_declared()
        check_sha256_not_unique()
        check_document_legacy_columns_intact()

        _section("§2  _sql_literal_for_default")
        check_sql_literal_for_default()

        _section("§3  schema_sync 补列 / 补索引")
        check_sync_indexes_on_empty_db(tmp / "empty.db")
        check_sync_on_legacy_table(tmp / "legacy.db")
        check_sync_is_idempotent(tmp / "idem.db")
        check_sync_does_not_drop_extra(tmp / "extra.db")
        check_sync_composite_column_order(tmp / "order.db")
        check_sync_skips_not_null_no_default(tmp / "notnull.db")

        _section("§4  Web 层（TestClient）")
        check_web_guest_denied_on_upload(tmp)
        check_web_guest_denied_on_download(tmp)
        check_web_upload_requires_csrf(tmp)
        check_web_upload_rejects_bad_ext(tmp)
        check_web_upload_basic_roundtrip(tmp)
        check_web_duplicate_upload_409(tmp)
        check_web_path_traversal_blocked(tmp)
    finally:
        # 顺序敏感：先释放句柄，再删目录
        _dispose_all_engines()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print("=" * 60)
    print("断言：%d 通过 / %d 失败" % (_PASSED, _FAILED))
    if _FAILED:
        print("\n失败明细：")
        for msg in _FAILURES:
            print("  ✗ %s" % msg)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
