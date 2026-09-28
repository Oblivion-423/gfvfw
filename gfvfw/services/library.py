"""资料库服务：上传落盘、元数据、目录树、预览转换。

设计约束
--------
* **物理存储与逻辑目录解耦**：``stored_path = docs/<id>.<ext>`` 恒定，
  目录层级完全由 ``Document.folder`` 表达；改目录只改列不动文件。
* **软删除**：``deleted_at`` 过滤；``sha256`` 不设 UNIQUE，
  删除后同一文件可重传（应用层用 ``find_by_sha256`` 提示）。
* **转换结果落盘缓存**：key = ``<sha256>/v<N>``，N 是 ``_CONVERSION_VERSION``。
  内容不变则复用；转换逻辑变更时递增 N 自动失效。
* **路径安全**：``folder`` 经 ``normalize_folder`` 规范化，
  拒绝 ``..`` / 反斜杠 / 控制字符；物理路径拼接后校验仍在 ``storage_dir`` 内。
* **上传流式处理**：不整文件读入内存；超过 max 立即中止并清理临时文件。

不负责
------
* 权限判断（在路由层由 ``require()`` 守卫）
* 审计内容（通过 ``audit_service.record`` 记录，由 audit 模块决定细节）
* HTTP 响应（返回 dict，路由层负责序列化）
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Optional       # Any 是新增的

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import audit as audit_service
from ..config import settings
from ..db import new_id
from ..models.site import Document

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 扩展名白名单
# ---------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTS: frozenset[str] = frozenset({
    ".pdf",
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".txt", ".md", ".markdown", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".tif", ".tiff",
})

IMAGE_EXTS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
})
TIFF_EXTS:  frozenset[str] = frozenset({".tif", ".tiff"})
PDF_EXTS:   frozenset[str] = frozenset({".pdf"})
MD_EXTS:    frozenset[str] = frozenset({".md", ".markdown"})
DOCX_EXTS:  frozenset[str] = frozenset({".docx"})               # .doc 不预览
SHEET_EXTS: frozenset[str] = frozenset({".xlsx", ".xls"})
TEXT_EXTS:  frozenset[str] = frozenset({
    ".txt", ".log", ".csv", ".json", ".xml", ".ini", ".conf",
    ".py", ".js", ".css", ".html", ".java", ".c", ".cpp", ".go", ".rs", ".sh",
})

MIME_FOR_EXT = {
    ".png": "image/png",  ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",  ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
}

#: 转换逻辑版本号。任何影响输出字节的修改都要 +1 —— 缓存 key 含版本，
#: 保证旧缓存自动失效，无需手工清理。
_CONVERSION_VERSION = 1

#: 目录最大层数（防"无限嵌套"拖垮树渲染）
MAX_FOLDER_DEPTH = 8

#: 单次预览的 TIF 最多渲染页数（防"1000 页 TIF"把浏览器打崩）
MAX_TIFF_PAGES = 100

#: 文本预览最大字节（超过截断）
MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024

#: 表格预览最大行数
MAX_SHEET_ROWS = 1000


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class LibraryError(Exception):
    """资料库业务异常基类。"""


class UploadRejected(LibraryError):
    """上传被拒绝（大小超限、类型不合法、空文件）。"""


class DuplicateUpload(LibraryError):
    """同一 sha256 已存在且未指定 ``allow_duplicate``。"""

    def __init__(self, message: str, *, existing_doc_id: str):
        super().__init__(message)
        self.existing_doc_id = existing_doc_id


class InvalidFolder(LibraryError):
    """folder 字符串不合法。"""


class DocumentNotFound(LibraryError):
    """doc_id 无对应记录（或已被软删除）。"""


# ---------------------------------------------------------------------------
# 工具：路径与文件名
# ---------------------------------------------------------------------------

_FOLDER_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")


def normalize_folder(raw: str | None) -> str:
    """规范化逻辑目录字符串。

    * 空 / None → ``''``（根）
    * 去首尾空白与首尾 ``/``
    * 折叠连续 ``/``
    * 拒绝 ``..`` 段、反斜杠、控制字符
    * 深度上限 ``MAX_FOLDER_DEPTH``
    * 单段长度上限 64，总长上限 512

    返回规范化后的字符串（不含前导/尾随斜杠）。
    """
    if not raw:
        return ""
    s = str(raw).strip().strip("/")
    if not s:
        return ""
    if _FOLDER_BAD_CHARS.search(s):
        raise InvalidFolder("目录名含非法字符（控制字符或反斜杠）")
    parts = [p.strip() for p in s.split("/")]
    parts = [p for p in parts if p]
    if len(parts) > MAX_FOLDER_DEPTH:
        raise InvalidFolder("目录层级超过上限 %d" % MAX_FOLDER_DEPTH)
    for p in parts:
        if p == "..":
            raise InvalidFolder("目录名不得含 '..'")
        if len(p) > 64:
            raise InvalidFolder("目录名单段长度超过 64")
    result = "/".join(parts)
    if len(result) > 512:
        raise InvalidFolder("目录路径总长超过 512")
    return result


def _ext_of(filename: str) -> str:
    """取小写扩展名（含点）。无扩展名返回 ``''``。"""
    return Path(filename).suffix.lower()


def _clean_original_filename(raw: str) -> str:
    """清理上传时的原始文件名。

    * 剥离任何路径部分（部分老浏览器会带上）
    * 去掉控制字符
    * 限长 255（保留扩展名优先）
    * 空值回退为 ``"unnamed"``
    """
    name = (raw or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip()
    if not name:
        return "unnamed"
    if len(name) <= 255:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 16:
        keep = 255 - len(ext) - 1
        return stem[:keep] + "." + ext
    return name[:255]


def _docs_root() -> Path:
    """物理资料根目录：``<storage_dir>/docs``。"""
    return Path(settings.storage_dir) / "docs"


def _tmp_root() -> Path:
    """上传临时目录（与 docs 同盘，保证 rename 原子）。"""
    return _docs_root() / ".tmp"


def storage_path_for(doc: Document) -> Path:
    """由 ``stored_path`` 解析出的绝对路径，并校验仍在 ``storage_dir`` 内。

    防止历史数据或人为篡改的 ``stored_path`` 越界读到系统文件。
    """
    base = Path(settings.storage_dir).resolve()
    full = (base / doc.stored_path).resolve()
    try:
        full.relative_to(base)
    except ValueError:
        raise LibraryError("stored_path 越界：%s" % doc.stored_path)
    return full


# ---------------------------------------------------------------------------
# 工具：扩展名判定
# ---------------------------------------------------------------------------

def is_allowed_extension(ext: str) -> bool:
    return ext.lower() in ALLOWED_UPLOAD_EXTS


def is_previewable(ext: str) -> bool:
    e = ext.lower()
    return (
        e in IMAGE_EXTS or e in PDF_EXTS or e in TIFF_EXTS
        or e in MD_EXTS or e in DOCX_EXTS or e in SHEET_EXTS
        or e in TEXT_EXTS
    )


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

def _stream_to_temp(
    stream: BinaryIO,
    tmp_path: Path,
    max_bytes: int,
    chunk_size: int = 1 << 20,  # 1 MiB
) -> tuple[str, int]:
    """流式读入，边算 sha256 边落盘。返回 ``(sha256_hex, size_bytes)``。

    超过 ``max_bytes`` 立即中止（抛 ``UploadRejected``），
    不把文件全部读完才知道超限。
    """
    h = hashlib.sha256()
    total = 0
    with open(tmp_path, "wb") as f:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UploadRejected(
                    "文件超过上限 %.1f MB" % (max_bytes / 1024 / 1024)
                )
            h.update(chunk)
            f.write(chunk)
    if total == 0:
        raise UploadRejected("上传的文件为空")
    return h.hexdigest(), total


def save_upload(
    *,
    session: Session,
    stream: BinaryIO,
    original_filename: str,
    folder: str = "",
    category: str = "other",
    title: Optional[str] = None,
    description: Optional[str] = None,
    version: Optional[str] = None,
    visibility: str = "members",
    aircraft_type_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    allow_duplicate: bool = False,
    request: Any = None,
) -> Document:
    """保存一次上传。返回**未提交**的 ``Document`` 行。

    调用方负责事务边界（commit / rollback）。理由：与现有 services 一致，
    便于把"上传资料 + 关联到战役"这类组合操作放进一个事务。

    ``allow_duplicate=False``（默认）时同一 ``sha256`` 会抛 ``DuplicateUpload``，
    并在异常里带 ``existing_doc_id`` 供路由层跳转。
    """
    folder = normalize_folder(folder)
    ext = _ext_of(original_filename)
    if not is_allowed_extension(ext):
        raise UploadRejected("不支持的文件类型：%s" % (ext or "无扩展名"))

    clean_name = _clean_original_filename(original_filename)

    max_bytes = settings.docs_max_upload_bytes
    warn_bytes = settings.docs_warn_upload_bytes

    _docs_root().mkdir(parents=True, exist_ok=True)
    _tmp_root().mkdir(parents=True, exist_ok=True)
    tmp_path = _tmp_root() / ("%s.part" % new_id())

    final_path: Optional[Path] = None
    try:
        sha, size = _stream_to_temp(stream, tmp_path, max_bytes)

        if size > warn_bytes:
            log.warning(
                "上传文件超过警告阈值：%s (%.1f MB > %.1f MB)",
                clean_name, size / 1024 / 1024, warn_bytes / 1024 / 1024,
            )

        existing = find_by_sha256(session, sha)
        if existing is not None and not allow_duplicate:
            who = existing.uploaded_by or "未知用户"
            raise DuplicateUpload(
                "此文件已存在（%s 上传，标题：%s）" % (who, existing.title),
                existing_doc_id=existing.id,
            )

        doc_id = new_id()
        stored_rel = "docs/%s%s" % (doc_id, ext)
        final_path = Path(settings.storage_dir) / stored_rel
        tmp_path.rename(final_path)

        doc = Document(
            id=doc_id,
            title=(title or "").strip() or Path(clean_name).stem or "未命名",
            category=category or "other",
            description=description or None,
            folder=folder,
            original_filename=clean_name,
            stored_path=stored_rel,
            sha256=sha,
            size_bytes=size,
            mime_type=(
                MIME_FOR_EXT.get(ext)
                or mimetypes.guess_type(clean_name)[0]
                or "application/octet-stream"
            ),
            version=version or None,
            visibility=visibility,
            aircraft_type_id=aircraft_type_id,
            download_count=0,
            uploaded_by=actor_user_id,
        )
        session.add(doc)
        session.flush()

        audit_service.record_audit(
            session,
            actor_user_id=actor_user_id,
            action="create",
            target_table="documents",
            target_id=doc.id,
            after={"title": doc.title, "folder": doc.folder,
                   "size_bytes": doc.size_bytes, "sha256": sha},
            request=request,
        )
        return doc

    except Exception:
        # 失败清理：删除临时文件与已落盘的最终文件，避免孤儿
        for p in (tmp_path, final_path):
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    log.warning("清理失败，残留文件：%s", p)
        raise


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _alive():
    """默认过滤软删除。供各查询复用。"""
    return Document.deleted_at.is_(None)


def get_document(session: Session, doc_id: str, *,
                 include_deleted: bool = False) -> Optional[Document]:
    """按 id 取。默认不含已软删除。"""
    stmt = select(Document).where(Document.id == doc_id)
    if not include_deleted:
        stmt = stmt.where(_alive())
    return session.scalars(stmt).one_or_none()


def get_document_or_raise(session: Session, doc_id: str, *,
                          include_deleted: bool = False) -> Document:
    doc = get_document(session, doc_id, include_deleted=include_deleted)
    if doc is None:
        raise DocumentNotFound("资料不存在：%s" % doc_id)
    return doc


def list_folder(session: Session, folder: str = "") -> list[Document]:
    """列出某目录下的直接文件（不含子目录）。已按原始文件名排序。"""
    folder = normalize_folder(folder)
    stmt = (
        select(Document)
        .where(_alive())
        .where(Document.folder == folder)
        .where(Document.category != "image")   # 系统内部图不进资料树
        .order_by(Document.original_filename)
    )
    return list(session.scalars(stmt))


def list_subfolders(session: Session, folder: str = "") -> list[str]:
    """列出某目录下的**直接**子目录名（去重、排序）。"""
    folder = normalize_folder(folder)
    prefix = (folder + "/") if folder else ""

    stmt = (
        select(Document.folder)
        .where(_alive())
        .where(Document.category != "image")
        .where(Document.folder.like(prefix + "%"))
        .where(Document.folder != folder)
        .distinct()
    )
    subs: set[str] = set()
    for f in session.scalars(stmt):
        rest = f[len(prefix):]
        first = rest.split("/", 1)[0]
        if first:
            subs.add(first)
    return sorted(subs)


def search(session: Session, keyword: str, *, limit: int = 50) -> list[Document]:
    """title / original_filename / description 三列 LIKE。

    按 §7 Q-3 的"一期 LIKE + 索引"策略；数据量到万级再上 FTS5。
    """
    q = (keyword or "").strip()
    if not q:
        return []
    like = "%" + q + "%"
    stmt = (
        select(Document)
        .where(_alive())
        .where(Document.category != "image")
        .where(or_(
            Document.title.like(like),
            Document.original_filename.like(like),
            Document.description.like(like),
        ))
        .order_by(Document.updated_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def find_by_sha256(session: Session, sha256: str) -> Optional[Document]:
    """按 sha256 找**未删除**的记录。用于应用层去重提示。"""
    stmt = (
        select(Document)
        .where(_alive())
        .where(Document.sha256 == sha256)
        .limit(1)
    )
    return session.scalars(stmt).first()


def count_by_folder(session: Session, folder: str = "") -> int:
    """某目录下的文件数（不含子目录、不含系统图）。"""
    folder = normalize_folder(folder)
    stmt = (
        select(Document.id)
        .where(_alive())
        .where(Document.folder == folder)
        .where(Document.category != "image")
    )
    return len(list(session.scalars(stmt)))


# ---------------------------------------------------------------------------
# 修改 / 删除
# ---------------------------------------------------------------------------

_EDITABLE_FIELDS = frozenset({
    "title", "description", "folder", "category", "version",
    "visibility", "aircraft_type_id",
})


def update_document(
    session: Session,
    doc_id: str,
    *,
    actor_user_id: Optional[str] = None,
    request: Any = None,
    **fields,
) -> Document:
    """更新元数据。只接受 ``_EDITABLE_FIELDS`` 里的键。

    ``folder`` 会经 ``normalize_folder`` 规范化。
    变更留痕走 ``audit_service.record``。
    """
    doc = get_document_or_raise(session, doc_id)

    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise LibraryError("不可编辑字段：%s" % sorted(unknown))

    before: dict = {}
    after: dict = {}
    for k, v in fields.items():
        if k == "folder":
            v = normalize_folder(v)
        old = getattr(doc, k)
        if old == v:
            continue
        before[k] = old
        after[k] = v
        setattr(doc, k, v)

    if after:
        session.flush()
        audit_service.record(
            session,
            actor_user_id=actor_user_id,
            action="update",
            target_table="documents",
            target_id=doc.id,
            before=before,
            after=after,
            request=request,
        )
    return doc


def delete_document(
    session: Session,
    doc_id: str,
    *,
    actor_user_id: Optional[str] = None,
    reason: Optional[str] = None,
    request: Any = None,
) -> None:
    """软删除。物理文件保留，可通过 ``restore_document`` 恢复。"""
    doc = get_document_or_raise(session, doc_id)
    doc.deleted_at = _utcnow()
    session.flush()
    audit_service.record_audit(
        session,
        actor_user_id=actor_user_id,
        action="delete",
        target_table="documents",
        target_id=doc.id,
        reason=reason,
        request=request,
    )


def restore_document(
    session: Session,
    doc_id: str,
    *,
    actor_user_id: Optional[str] = None,
    request: Any = None,
) -> Document:
    """撤销软删除。"""
    doc = get_document_or_raise(session, doc_id, include_deleted=True)
    if doc.deleted_at is None:
        return doc
    doc.deleted_at = None
    session.flush()
    audit_service.record_audit(
        session,
        actor_user_id=actor_user_id,
        action="restore",
        target_table="documents",
        target_id=doc.id,
        request=request,
    )
    return doc


def bump_download(
    session: Session,
    doc_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> None:
    """下载计数 +1。不写审计（下载量太大，写审计会淹没日志）。"""
    doc = get_document_or_raise(session, doc_id)
    doc.download_count = (doc.download_count or 0) + 1
    session.flush()


def _utcnow():
    """统一取 UTC now。与项目其它服务保持一致（存 UTC，展示转 UTC+8）。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 预览：缓存路径
# ---------------------------------------------------------------------------

def _cache_root_for(doc: Document) -> Path:
    """某文档的缓存根目录：``<cache_dir>/<sha256>/v<N>``。

    含版本号，转换逻辑升级时自动切新目录，旧缓存不再命中。
    """
    return (
        Path(settings.docs_cache_dir)
        / doc.sha256
        / ("v%d" % _CONVERSION_VERSION)
    )


# ---------------------------------------------------------------------------
# 预览：文本
# ---------------------------------------------------------------------------

def _read_text_preview(path: Path, max_bytes: int = MAX_TEXT_PREVIEW_BYTES) -> str:
    """读文本，超限截断并加提示。多编码尝试。"""
    raw = path.read_bytes()[:max_bytes]
    truncated = path.stat().st_size > max_bytes
    for enc in ("utf-8", "gbk", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += "\n\n[内容超过 %d KB，已截断]" % (max_bytes // 1024)
    return text


# ---------------------------------------------------------------------------
# 预览：Markdown / DOCX / 表格
# ---------------------------------------------------------------------------

def _md_to_html(path: Path) -> str:
    import markdown
    text = path.read_text(encoding="utf-8", errors="replace")
    return markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
    )


def _docx_to_html(path: Path) -> str:
    import mammoth
    with open(path, "rb") as f:
        return mammoth.convert_to_html(f).value


def _read_spreadsheet(path: Path) -> list[dict]:
    ext = path.suffix.lower()
    sheets: list[dict] = []

    if ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(str(path))
        for sheet in book.sheets():
            rows = []
            for r in range(min(sheet.nrows, MAX_SHEET_ROWS)):
                rows.append([str(sheet.cell_value(r, c))
                             for c in range(sheet.ncols)])
            sheets.append({"name": sheet.name, "rows": rows})
    else:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        try:
            for name in wb.sheetnames:
                ws = wb[name]
                rows = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= MAX_SHEET_ROWS:
                        break
                    rows.append(["" if c is None else str(c) for c in row])
                sheets.append({"name": name, "rows": rows})
        finally:
            wb.close()
    return sheets


# ---------------------------------------------------------------------------
# 预览：TIF
# ---------------------------------------------------------------------------

def _tiff_to_png_pages(path: Path, *, max_pages: int = MAX_TIFF_PAGES) -> list[bytes]:
    """TIF → PNG 字节列表。多页按顺序。

    ⚠️ ``img.copy()`` 必须在 ``seek`` 之后立即调，否则后续 seek 会
    让之前的帧失效（Pillow 复用底层缓冲）。
    """
    from PIL import Image
    pages: list[bytes] = []
    with Image.open(str(path)) as img:
        n = getattr(img, "n_frames", 1)
        for i in range(min(n, max_pages)):
            img.seek(i)
            frame = img.copy()
            if frame.mode not in ("RGB", "RGBA", "L", "P"):
                frame = frame.convert("RGB")
            buf = BytesIO()
            frame.save(buf, format="PNG")
            pages.append(buf.getvalue())
    return pages


# ---------------------------------------------------------------------------
# 预览：缓存包装
# ---------------------------------------------------------------------------

def _tiff_pages_cached(doc: Document, path: Path) -> list[bytes]:
    cache = _cache_root_for(doc)
    meta_file = cache / "tiff.json"

    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            count = int(meta["count"])
            pages = []
            for i in range(count):
                pf = cache / ("tiff-%d.png" % i)
                if not pf.is_file():
                    break
                pages.append(pf.read_bytes())
            else:
                return pages
        except (OSError, ValueError, KeyError):
            log.warning("TIF 缓存损坏，重新生成：%s", cache)

    pages = _tiff_to_png_pages(path)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for i, data in enumerate(pages):
            (cache / ("tiff-%d.png" % i)).write_bytes(data)
        meta_file.write_text(
            json.dumps({"count": len(pages), "v": _CONVERSION_VERSION}),
            encoding="utf-8",
        )
    except OSError:
        log.warning("写 TIF 缓存失败，本次走内存结果：%s", cache)
    return pages


def _html_cached(doc: Document, path: Path, *, kind: str,
                 producer) -> str:                            # noqa: ANN001
    """通用 HTML 缓存（kind='md' 或 'docx'）。

    producer: ``(path) -> str``。
    """
    cache = _cache_root_for(doc)
    cache_file = cache / ("%s.html" % kind)
    if cache_file.is_file():
        try:
            return cache_file.read_text(encoding="utf-8")
        except OSError:
            log.warning("HTML 缓存不可读，重新生成：%s", cache_file)

    html = producer(path)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
    except OSError:
        log.warning("写 HTML 缓存失败：%s", cache_file)
    return html


def _spreadsheet_cached(doc: Document, path: Path) -> list[dict]:
    cache = _cache_root_for(doc)
    cache_file = cache / "sheets.json"
    if cache_file.is_file():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("表格缓存损坏，重新生成：%s", cache_file)

    sheets = _read_spreadsheet(path)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(sheets, ensure_ascii=False), encoding="utf-8",
        )
    except OSError:
        log.warning("写表格缓存失败：%s", cache_file)
    return sheets


# ---------------------------------------------------------------------------
# 预览：分发
# ---------------------------------------------------------------------------

def preview_payload(doc: Document, *, use_cache: bool = True) -> dict:
    """按扩展名分发生成预览数据。

    返回值形状（路由层直接 JSON 化）：

      ``{"type": "image"}``          图片：由路由生成 raw URL
      ``{"type": "pdf"}``            PDF：同上
      ``{"type": "tiff", "pages": [<base64>, ...]}``
      ``{"type": "html", "html": "..."}``
      ``{"type": "text", "text": "..."}``
      ``{"type": "spreadsheet", "sheets": [{"name", "rows"}, ...]}``
      ``{"type": "unsupported", "message": "..."}``
    """
    ext = _ext_of(doc.original_filename or doc.stored_path)
    path = storage_path_for(doc)
    if not path.is_file():
        return {"type": "unsupported", "message": "文件缺失（可能已被管理员清理）"}

    if ext in IMAGE_EXTS:
        return {"type": "image"}
    if ext in PDF_EXTS:
        return {"type": "pdf"}

    try:
        if ext in TIFF_EXTS:
            if use_cache:
                pages = _tiff_pages_cached(doc, path)
            else:
                pages = _tiff_to_png_pages(path)
            return {
                "type": "tiff",
                "pages": [base64.b64encode(p).decode("ascii") for p in pages],
            }
        if ext in MD_EXTS:
            html = (_html_cached(doc, path, kind="md", producer=_md_to_html)
                    if use_cache else _md_to_html(path))
            return {"type": "html", "html": html}
        if ext in DOCX_EXTS:
            html = (_html_cached(doc, path, kind="docx", producer=_docx_to_html)
                    if use_cache else _docx_to_html(path))
            return {"type": "html", "html": html}
        if ext in TEXT_EXTS:
            return {"type": "text", "text": _read_text_preview(path)}
        if ext in SHEET_EXTS:
            sheets = (_spreadsheet_cached(doc, path)
                      if use_cache else _read_spreadsheet(path))
            return {"type": "spreadsheet", "sheets": sheets}
    except Exception as e:                                  # noqa: BLE001
        log.exception("预览生成失败 doc=%s ext=%s", doc.id, ext)
        return {"type": "unsupported",
                "message": "预览生成失败：%s" % e}

    # .doc 等允许上传但不支持预览的格式
    if ext == ".doc":
        return {"type": "unsupported",
                "message": "旧版 .doc 格式暂不支持网页预览，请下载后本地查看。"}
    return {"type": "unsupported",
            "message": "暂不支持预览 %s 格式" % (ext or "该")}

def list_folder(session: Session, folder: str = "",
                *, visibility_in: list[str] | None = None) -> list[Document]:
    folder = normalize_folder(folder)
    stmt = (
        select(Document)
        .where(_alive())
        .where(Document.folder == folder)
        .where(Document.category != "image")
    )
    if visibility_in is not None:
        stmt = stmt.where(Document.visibility.in_(visibility_in))
    stmt = stmt.order_by(Document.original_filename)
    return list(session.scalars(stmt))

def list_subfolders(session: Session, folder: str = "",
                    *, visibility_in: list[str] | None = None) -> list[str]:
    folder = normalize_folder(folder)
    prefix = (folder + "/") if folder else ""
    stmt = (
        select(Document.folder)
        .where(_alive())
        .where(Document.category != "image")
        .where(Document.folder.like(prefix + "%"))
        .where(Document.folder != folder)
        .distinct()
    )
    if visibility_in is not None:
        stmt = stmt.where(Document.visibility.in_(visibility_in))
    subs: set[str] = set()
    for f in session.scalars(stmt):
        rest = f[len(prefix):]
        first = rest.split("/", 1)[0]
        if first:
            subs.add(first)
    return sorted(subs)

def search(session: Session, keyword: str, *, limit: int = 50,
           visibility_in: list[str] | None = None) -> list[Document]:
    q = (keyword or "").strip()
    if not q:
        return []
    like = "%" + q + "%"
    stmt = (
        select(Document)
        .where(_alive())
        .where(Document.category != "image")
        .where(or_(
            Document.title.like(like),
            Document.original_filename.like(like),
            Document.description.like(like),
        ))
    )
    if visibility_in is not None:
        stmt = stmt.where(Document.visibility.in_(visibility_in))
    stmt = stmt.order_by(Document.updated_at.desc()).limit(limit)
    return list(session.scalars(stmt))

def count_by_folder(session: Session, folder: str = "",
                    *, visibility_in: list[str] | None = None) -> int:
    folder = normalize_folder(folder)
    stmt = (
        select(Document.id)
        .where(_alive())
        .where(Document.folder == folder)
        .where(Document.category != "image")
    )
    if visibility_in is not None:
        stmt = stmt.where(Document.visibility.in_(visibility_in))
    return len(list(session.scalars(stmt)))