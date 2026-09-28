"""library 服务层自检：上传、去重、软删、恢复、搜索、预览分发。"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from gfvfw.db import Base
from gfvfw.services import library
from gfvfw.services import schema_sync


_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []
_ENGINES: list[Engine] = []


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


def _make_session(db_file: Path):
    engine = create_engine("sqlite:///%s" % db_file, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _ENGINES.append(engine)
    schema_sync.ensure_schema_and_sync(engine)
    return sessionmaker(bind=engine, future=True)()


def _dispose_all():
    for e in _ENGINES:
        try:
            e.dispose()
        except Exception:
            pass
    _ENGINES.clear()

def _wipe_documents(session) -> None:
    """彻底清空 documents 表，让后续 case 从零开始。

    用 SQL 层 DELETE 而非 session.delete —— 即使 SoftDeleteMixin 拦截了
    ORM 的 delete 把它变成软删，这里也是物理删，保证测试间不累积。
    同时清掉物理文件。
    """
    from sqlalchemy import delete as sa_delete
    from gfvfw.models.site import Document

    # 先收集文件路径（在 DELETE 之前）
    rows = session.query(Document).all()
    paths = [library.storage_path_for(d) for d in rows]

    session.execute(sa_delete(Document))
    session.commit()

    for p in paths:
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass

# ============================================================================
# §1  normalize_folder
# ============================================================================

def check_normalize_folder():
    nf = library.normalize_folder
    _assert(nf(None) == "", "None → ''")
    _assert(nf("") == "", "'' → ''")
    _assert(nf("  ") == "", "空白 → ''")
    _assert(nf("/") == "", "'/' → ''")
    _assert(nf("manuals") == "manuals", "单层")
    _assert(nf("/manuals/") == "manuals", "去首尾斜杠")
    _assert(nf("manuals//f16") == "manuals/f16", "折叠连续斜杠")
    _assert(nf(" manuals / f16 ") == "manuals / f16".replace(" ", ""),
            "去段内空白")

    for bad in ("../etc", "a/../b", "a\\b", "a\x00b"):
        try:
            nf(bad)
            _assert(False, "应拒绝非法 folder：%r" % bad)
        except library.InvalidFolder:
            _assert(True, "")


# ============================================================================
# §2  上传
# ============================================================================

def check_upload_basic(session):
    content = b"hello world"
    doc = library.save_upload(
        session=session,
        stream=io.BytesIO(content),
        original_filename="test.pdf",
        folder="manuals/f16",
        title="F-16 手册",
        category="manual",
    )
    session.commit()

    _assert(doc.id, "doc.id 应已生成")
    _assert(doc.folder == "manuals/f16", "folder 被规范化")
    _assert(doc.original_filename == "test.pdf", "原文件名保留")
    _assert(doc.title == "F-16 手册", "title 生效")
    _assert(doc.size_bytes == len(content), "size_bytes 正确")
    _assert(doc.sha256 == __import__("hashlib").sha256(content).hexdigest(),
            "sha256 正确")
    _assert(doc.stored_path == "docs/%s.pdf" % doc.id,
            "物理路径 = docs/<id>.pdf")

    # 物理文件存在
    p = library.storage_path_for(doc)
    _assert(p.is_file(), "物理文件应已落盘")
    _assert(p.read_bytes() == content, "文件内容一致")


def check_upload_rejects_bad_ext(session):
    try:
        library.save_upload(
            session=session,
            stream=io.BytesIO(b"x"),
            original_filename="malware.exe",
        )
        _assert(False, "应拒绝 .exe")
    except library.UploadRejected:
        _assert(True, "")


def check_upload_rejects_empty(session):
    try:
        library.save_upload(
            session=session,
            stream=io.BytesIO(b""),
            original_filename="empty.txt",
        )
        _assert(False, "应拒绝空文件")
    except library.UploadRejected:
        _assert(True, "")


def check_upload_rejects_oversize(session):
    # 用极小上限验证中止路径
    from gfvfw.config import settings
    old = settings.docs_max_upload_mb
    settings.docs_max_upload_mb = 0   # 0 MB → 0 字节
    try:
        library.save_upload(
            session=session,
            stream=io.BytesIO(b"x" * 100),
            original_filename="big.txt",
        )
        _assert(False, "应拒绝超限")
    except library.UploadRejected:
        _assert(True, "")
    finally:
        settings.docs_max_upload_mb = old


def check_upload_dedup(session):
    """同一 sha256 第二次上传 → DuplicateUpload，且带 existing_doc_id。"""
    content = b"dedup probe"
    d1 = library.save_upload(
        session=session, stream=io.BytesIO(content),
        original_filename="a.txt",
    )
    session.commit()

    try:
        library.save_upload(
            session=session, stream=io.BytesIO(content),
            original_filename="b.txt",
        )
        _assert(False, "应抛 DuplicateUpload")
    except library.DuplicateUpload as e:
        _assert(e.existing_doc_id == d1.id, "异常带正确 existing_doc_id")


def check_upload_dedup_bypass(session):
    """allow_duplicate=True 时同 sha256 可再传一份。"""
    content = b"bypass probe"
    d1 = library.save_upload(
        session=session, stream=io.BytesIO(content),
        original_filename="a.txt",
    )
    d2 = library.save_upload(
        session=session, stream=io.BytesIO(content),
        original_filename="b.txt", allow_duplicate=True,
    )
    session.commit()
    _assert(d1.id != d2.id, "两份记录 id 不同")
    _assert(d1.sha256 == d2.sha256, "sha256 相同")


def check_upload_cleanup_on_failure(session, storage_dir: Path):
    """上传失败不得留孤儿文件。只比对本次调用前后的差集。"""
    docs_dir = storage_dir / "docs"
    before = {p.name for p in docs_dir.glob("*")}
    try:
        library.save_upload(
            session=session,
            stream=io.BytesIO(b"x"),
            original_filename="bad.exe",
        )
    except library.UploadRejected:
        pass
    after = {p.name for p in docs_dir.glob("*")}
    new = after - before - {".tmp"}
    _assert(not new, "失败上传留了孤儿：%s" % new)

# ============================================================================
# §3  目录
# ============================================================================



def check_list_folder(session):
    _wipe_documents(session)

    library.save_upload(session=session, stream=io.BytesIO(b"a"),
                        original_filename="a.txt", folder="f1")
    library.save_upload(session=session, stream=io.BytesIO(b"b"),
                        original_filename="b.txt", folder="f1")
    library.save_upload(session=session, stream=io.BytesIO(b"c"),
                        original_filename="c.txt", folder="f2")
    library.save_upload(session=session, stream=io.BytesIO(b"d"),
                        original_filename="d.txt", folder="")
    session.commit()

    f1 = library.list_folder(session, "f1")
    _assert(len(f1) == 2, "f1 应有两个文件，实际 %d" % len(f1))
    _assert({d.original_filename for d in f1} == {"a.txt", "b.txt"},
            "f1 文件名集合不对")

    root = library.list_folder(session, "")
    _assert({d.original_filename for d in root} == {"d.txt"},
            "根目录应只有 d.txt")


def check_list_subfolders(session):
    _wipe_documents(session)

    library.save_upload(session=session, stream=io.BytesIO(b"1"),
                        original_filename="x.txt", folder="a/b/c")
    library.save_upload(session=session, stream=io.BytesIO(b"2"),
                        original_filename="y.txt", folder="a/b")
    library.save_upload(session=session, stream=io.BytesIO(b"3"),
                        original_filename="z.txt", folder="a/d")
    library.save_upload(session=session, stream=io.BytesIO(b"4"),
                        original_filename="w.txt", folder="e")
    session.commit()

    root_subs = library.list_subfolders(session, "")
    _assert(root_subs == ["a", "e"], "根子目录应为 [a,e]，实得 %s" % root_subs)

    a_subs = library.list_subfolders(session, "a")
    _assert(a_subs == ["b", "d"], "a 子目录应为 [b,d]，实得 %s" % a_subs)

    ab_subs = library.list_subfolders(session, "a/b")
    _assert(ab_subs == ["c"], "a/b 子目录应为 [c]，实得 %s" % ab_subs)


# ============================================================================
# §4  搜索
# ============================================================================

def check_search(session):
    _wipe_documents(session)

    library.save_upload(session=session, stream=io.BytesIO(b"s1"),
                        original_filename="checklist_v1.txt",
                        folder="",
                        title="F-16 起降检查单")
    library.save_upload(session=session, stream=io.BytesIO(b"s2"),
                        original_filename="manual.pdf",
                        folder="", title="F-16 手册")
    session.commit()

    r1 = library.search(session, "检查单")
    _assert(len(r1) >= 1, "搜'检查单'应有结果")

    r2 = library.search(session, "F-16")
    _assert(len(r2) >= 2, "搜'F-16'应≥2 条")

    r3 = library.search(session, "不存在的关键词xyz")
    _assert(len(r3) == 0, "无匹配应返回空列表")

    r4 = library.search(session, "")
    _assert(r4 == [], "空关键词返回 []")


# ============================================================================
# §5  软删除与恢复
# ============================================================================

def check_soft_delete_and_restore(session):
    _wipe_documents(session)
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"delete probe"),
        original_filename="del.txt")
    session.commit()

    library.delete_document(session, d.id)
    session.commit()

    _assert(library.get_document(session, d.id) is None,
            "软删后默认查不到")
    got = library.get_document(session, d.id, include_deleted=True)
    _assert(got is not None and got.deleted_at is not None, "含删查询可见")

    # ★ 关键：软删状态下同 sha256 可重传
    # （sha256 无 UNIQUE 约束 + find_by_sha256 只查未删记录，
    #   两者缺一不可；任一条失效这里会抛 DuplicateUpload）
    d2 = library.save_upload(
        session=session, stream=io.BytesIO(b"delete probe"),
        original_filename="del2.txt")
    session.commit()
    _assert(d2.id != d.id, "删后同 sha256 可重传")

    # 恢复原记录
    library.restore_document(session, d.id)
    session.commit()
    _assert(library.get_document(session, d.id) is not None, "恢复后可见")
    _assert(d2.id != d.id, "恢复 d 不影响 d2")

# ============================================================================
# §6  预览分发
# ============================================================================

def check_preview_dispatch(session):
    # 文本
    d = library.save_upload(
        session=session, stream=io.BytesIO("你好".encode("utf-8")),
        original_filename="t.txt")
    session.commit()
    p = library.preview_payload(d)
    _assert(p["type"] == "text" and p["text"] == "你好", "txt → text")

    # PNG（图片）
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8),
        original_filename="t.png")
    session.commit()
    p = library.preview_payload(d)
    _assert(p["type"] == "image", "png → image")

    # PDF
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"%PDF-1.4 fake"),
        original_filename="t.pdf")
    session.commit()
    p = library.preview_payload(d)
    _assert(p["type"] == "pdf", "pdf → pdf")

    # Markdown
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"# Title\n\nbody"),
        original_filename="t.md")
    session.commit()
    p = library.preview_payload(d)
    _assert(
        p["type"] == "html" and "<h1" in p.get("html", ""),
        "md → html，实际得到 %r" % p,
    )

    # .doc：允许上传，预览返回 unsupported
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"\xd0\xcf\x11\xe0fake doc"),
        original_filename="legacy.doc")
    session.commit()
    p = library.preview_payload(d)
    _assert(p["type"] == "unsupported", ".doc → unsupported")
    _assert("旧版" in p["message"] or "doc" in p["message"].lower(),
            ".doc 提示应说明原因")


def check_preview_missing_file(session):
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"gone"),
        original_filename="gone.txt")
    session.commit()
    library.storage_path_for(d).unlink()
    p = library.preview_payload(d)
    _assert(p["type"] == "unsupported", "文件缺失应返回 unsupported")


# ============================================================================
# §7  TIF 缓存
# ============================================================================

def check_tiff_cache_roundtrip(session):
    """TIF 转 PNG：首次生成缓存，第二次命中。"""
    try:
        from PIL import Image
    except ImportError:
        return  # Pillow 未装则跳过

    img = Image.new("RGB", (10, 10), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="TIFF")
    buf.seek(0)

    d = library.save_upload(
        session=session, stream=buf, original_filename="t.tif")
    session.commit()

    p1 = library.preview_payload(d)
    _assert(p1["type"] == "tiff" and len(p1["pages"]) == 1, "单页 TIF")
    _assert(p1["pages"][0].startswith("iVBOR") or len(p1["pages"][0]) > 0,
            "PNG base64 非空")

    cache = library._cache_root_for(d)
    _assert(cache.is_dir(), "缓存目录已建")

    p2 = library.preview_payload(d)
    _assert(p2["pages"] == p1["pages"], "二次命中返回一致")


# ============================================================================
# §8  stored_path 越界防护
# ============================================================================

def check_stored_path_traversal_blocked(session):
    d = library.save_upload(
        session=session, stream=io.BytesIO(b"x"),
        original_filename="x.txt")
    session.commit()

    # 篡改 stored_path
    d.stored_path = "../../../etc/passwd"
    session.flush()

    try:
        library.storage_path_for(d)
        _assert(False, "越界 stored_path 应被拒")
    except library.LibraryError:
        _assert(True, "")


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    global _PASSED, _FAILED
    _PASSED = 0
    _FAILED = 0
    _FAILURES.clear()

    tmpdir = tempfile.mkdtemp(prefix="gfvfw_lib_svc_")
    try:
        tmp = Path(tmpdir)
        storage = tmp / "storage"
        storage.mkdir()

        # 重定向 settings
        from gfvfw.config import settings
        old_storage = settings.storage_dir
        old_cache = settings.docs_cache_dir
        settings.storage_dir = storage
        settings.docs_cache_dir = tmp / "cache"

        try:
            session = _make_session(tmp / "db.sqlite")

            _section("§1  normalize_folder")
            check_normalize_folder()

            _section("§2  上传")
            check_upload_basic(session)
            check_upload_rejects_bad_ext(session)
            check_upload_rejects_empty(session)
            check_upload_rejects_oversize(session)
            check_upload_dedup(session)
            check_upload_dedup_bypass(session)
            check_upload_cleanup_on_failure(session, storage)

            _section("§3  目录")
            check_list_folder(session)
            check_list_subfolders(session)

            _section("§4  搜索")
            check_search(session)

            _section("§5  软删除与恢复")
            check_soft_delete_and_restore(session)

            _section("§6  预览分发")
            check_preview_dispatch(session)
            check_preview_missing_file(session)

            _section("§7  TIF 缓存")
            check_tiff_cache_roundtrip(session)

            _section("§8  stored_path 越界防护")
            check_stored_path_traversal_blocked(session)

            session.close()
        finally:
            settings.storage_dir = old_storage
            settings.docs_cache_dir = old_cache
    finally:
        _dispose_all()
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