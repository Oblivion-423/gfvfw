"""Tacview XML「Export Flight Log」的归档与战斗分析服务。

与 :mod:`gfvfw.services.ingest`（``.acmi`` → 架次入库）共用一条上传链路：
**ACMI 工作台上传 .acmi 时**（``ingest_file``）顺手做战斗分析（本模块
:func:`analyze_acmi_archive`），归并确认时按 ``sha256`` 挂到任务上；
任务详情页内嵌展示，完整击杀链在 ``/tacview/{id}``。不做架次入库，
只做**战斗分析**（击杀归属、命中链、武器效能、飞行结局），
分析结果整树存 ``tacview_xml_files.vm_json``。

为什么结果可以存 JSON 而不用重算入口
------------------------------------
``.acmi`` 的解析结果会随"认领映射"变化，所以要能重跑；而 XML 的事件链
分析只取决于**文件内容**（``sha256`` 去重保证同一文件只有一份结果），
与名册无关 —— 解析一次存快照即可，查看永远零成本。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import tacview_analyzer as TA
from ..config import settings
from ..db import utcnow
from ..models import TacviewXmlFile

log = logging.getLogger("gfvfw.services.tacview")

#: 与 ACMI 上限共用一个配置（XML 导出通常只有几 MB，256MB 绰绰有余）。
MAX_TACVIEW_XML_BYTES = settings.max_acmi_bytes


class TacviewError(Exception):
    """上传/解析失败的用户可读错误。"""


def absolute_path(rec: TacviewXmlFile) -> Path:
    p = Path(rec.stored_path)
    return p if p.is_absolute() else Path(settings.storage_dir) / p


def get(db: Session, file_id: str) -> TacviewXmlFile | None:
    return db.get(TacviewXmlFile, file_id)


def list_for_mission(db: Session, mission_id: str) -> list[TacviewXmlFile]:
    return list(db.scalars(
        select(TacviewXmlFile).where(TacviewXmlFile.mission_id == mission_id)
        .order_by(TacviewXmlFile.created_at)))


def view_model(rec: TacviewXmlFile) -> dict | None:
    """取出存好的分析树；解析失败或尚未解析时返回 ``None``。"""
    if not rec.vm_json:
        return None
    try:
        return json.loads(rec.vm_json)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 落盘 + 解析
# --------------------------------------------------------------------------

def _hash_file(path: str | Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _park_path(sha256: str, filename: str) -> Path:
    """按 ``tacview/<年月>/<sha256>__<原名>`` 落盘（与 ACMI 同一去重策略）。"""
    import time

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "tacview.xml"
    sub = Path(settings.storage_dir) / "tacview" / time.strftime("%Y%m")
    sub.mkdir(parents=True, exist_ok=True)
    return sub / ("%s__%s" % (sha256[:16], safe))


def store_and_parse(db: Session, source_path: str | Path,
                    original_filename: str, *,
                    uploaded_by: str | None = None,
                    mission_id: str | None = None) -> tuple[TacviewXmlFile, bool]:
    """归档一份 XML 导出并立即解析出分析树。

    输入可以是 Tacview 的 **XML 导出**，也可以直接是 **.acmi 录像**
    （裸文本或 ZIP 容器）—— 后者先经 :mod:`gfvfw.acmi_debrief` 转换成
    等价的导出 XML 再解析，``source_format`` 记录来路。

    返回 ``(记录, 是否新建)`` —— 重复上传（``sha256`` 命中）返回旧记录与
    ``False``，只补 ``mission_id``，不重复落盘（与 ``acmi_files`` 同口径）。
    解析失败**不抛异常**：照常归档原件，把原因写进 ``parse_error``
    （用户上传的文件不该因为解析器跟不上就"消失"）。
    """
    src = Path(source_path)
    sha256, size = _hash_file(src)
    if size > MAX_TACVIEW_XML_BYTES:
        raise TacviewError(
            "文件过大（%d MB > 上限 %d MB）"
            % (size // (1024 * 1024), MAX_TACVIEW_XML_BYTES // (1024 * 1024)))

    existing = db.scalar(select(TacviewXmlFile).where(TacviewXmlFile.sha256 == sha256))
    if existing is not None:
        if mission_id and not existing.mission_id:
            existing.mission_id = mission_id
            db.flush()
        return existing, False

    # ---- 识别输入格式：.acmi 先转成导出 XML ----
    with open(src, "rb") as fh:
        head = fh.read(2048)
    head_l = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"<TacviewDebriefing" in head_l or head_l.startswith(b"<?xml"):
        source_format = "xml"
    else:
        source_format = "acmi"

    rec = TacviewXmlFile(
        mission_id=mission_id,
        sha256=sha256,
        original_filename=original_filename,
        stored_path="",                # 由下方创建分支填入
        size_bytes=size,
        source_format=source_format,
        uploaded_by=uploaded_by,
    )
    if source_format == "acmi":
        _build_from_acmi(db, rec, src)
    else:
        dest = _park_path(sha256, original_filename)
        shutil.copyfile(src, dest)
        rec.stored_path = str(dest.relative_to(Path(settings.storage_dir))).replace("\\", "/")
        db.add(rec)
        db.flush()
        _parse_into(db, rec, dest)
    return rec, True


def analyze_acmi_archive(db: Session, acmi_file,
                         *, uploaded_by: str | None = None):
    """为 ACMI 工作台**已归档**的 .acmi 生成战斗分析（幂等）。

    在 :meth:`AcmiIngestService.ingest_file` 落库后调用：转换 + 解析一次，
    生成 ``mission_id=None`` 的分析记录；归并确认时按 ``sha256`` 挂到任务上
    （见 :meth:`AcmiIngestService.confirm_merge`）。已有同 ``sha256`` 记录
    （含分析页单独上传过的）直接复用，不重复分析。
    """
    existing = db.scalar(
        select(TacviewXmlFile).where(TacviewXmlFile.sha256 == acmi_file.sha256))
    if existing is not None:
        return existing
    path = Path(settings.storage_dir) / acmi_file.stored_path
    if not path.exists():
        log.warning("战斗分析跳过：ACMI 原件不在 %s", path)
        return None
    rec = TacviewXmlFile(
        sha256=acmi_file.sha256,
        original_filename=acmi_file.original_filename,
        stored_path="",
        size_bytes=acmi_file.size_bytes,
        source_format="acmi",
        uploaded_by=uploaded_by or acmi_file.uploaded_by,
    )
    _build_from_acmi(db, rec, path)
    return rec


def _build_from_acmi(db: Session, rec: TacviewXmlFile, src: Path) -> None:
    """把 ``.acmi`` 转换成导出 XML 并落库解析。

    转换失败（文件不是可用的 ACMI）**不抛异常**：归档原件、把原因写进
    ``parse_error`` —— 用户上传的文件不该因为解析器跟不上就"消失"。
    """
    from .. import acmi_debrief
    try:
        xml_text, conv_info = acmi_debrief.convert_acmi_to_debriefing_xml(
            str(src), title=rec.original_filename)
    except Exception as exc:                            # noqa: BLE001
        log.warning("ACMI 转换失败 %s：%s", rec.original_filename, exc)
        dest = _park_path(rec.sha256, rec.original_filename)
        shutil.copyfile(src, dest)
        rec.stored_path = str(dest.relative_to(Path(settings.storage_dir))).replace("\\", "/")
        rec.parse_error = ("不是可用的 ACMI 录像（%s）" % exc)[:2000]
        db.add(rec)
        db.flush()
        return
    log.info("ACMI 转换为导出 XML：%s → %d 事件（射击 %d / 命中 %d / 击杀 %d）",
             rec.original_filename, conv_info["events"],
             conv_info["shots"], conv_info["hits"], conv_info["kills"])
    import tempfile
    tmp = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(xml_text)
        tmp.close()
        dest = _park_path(rec.sha256, rec.original_filename + ".converted.xml")
        shutil.copyfile(tmp.name, dest)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    rec.stored_path = str(dest.relative_to(Path(settings.storage_dir))).replace("\\", "/")
    rec.size_bytes = dest.stat().st_size
    db.add(rec)
    db.flush()
    _parse_into(db, rec, dest)


def reparse(db: Session, rec: TacviewXmlFile) -> None:
    """用**已归档的原件**重新解析（分析器升级后回填历史存档用）。"""
    path = absolute_path(rec)
    if not path.exists():
        raise TacviewError("原件已不在服务器上，无法重新解析")
    _parse_into(db, rec, path)


def _parse_into(db: Session, rec: TacviewXmlFile, path: Path) -> None:
    """解析并写入 ``rec``。失败时记录 ``parse_error`` —— **不回滚**：
    解析是纯 Python 操作、不碰数据库，归档行必须保留（用户上传的
    文件不该因为解析器跟不上就"消失"）。"""
    rec.parse_error = None
    rec.vm_json = None
    try:
        deb = TA.parse_xml_file(str(path))
        vm = TA.build_pilot_view_model(deb.events, deb.mission)
    except Exception as exc:                            # noqa: BLE001
        rec.parse_error = str(exc)[:2000]
        db.flush()
        log.warning("Tacview XML 解析失败 %s：%s", rec.original_filename, exc)
        return

    rec.format_version = deb.version
    rec.recorder = (deb.flight_recording.recorder
                    if deb.flight_recording else None)
    rec.mission_title = deb.mission.title if deb.mission else None
    rec.mission_duration_seconds = deb.mission.duration if deb.mission else None
    rec.event_count = len(deb.events)

    pilots = vm.get("pilots") or []
    ov = vm.get("overview") or {}
    rec.human_pilots = len(pilots)
    rec.total_shots = sum(int(p["totals"]["shots"]) for p in pilots)
    rec.total_hits = sum(int(p["totals"]["hits"]) for p in pilots)
    rec.total_kills = sum(int(p["totals"]["kills"]) for p in pilots)
    rec.total_misses = sum(int(p["totals"]["misses"]) for p in pilots)
    rec.landed_pilots = int(ov.get("landedPilots") or 0)
    rec.ejected_or_shot_pilots = int(ov.get("ejectedOrShotPilots") or 0)
    rec.vm_json = json.dumps(vm, ensure_ascii=False, separators=(",", ":"))
    rec.updated_at = utcnow()
    db.flush()


# --------------------------------------------------------------------------
# 删除
# --------------------------------------------------------------------------

def delete_upload(db: Session, rec: TacviewXmlFile) -> None:
    """硬删除（行 + 磁盘原件）：同一文件重传靠 sha256 判重，必须真删。"""
    stored = rec.stored_path
    db.execute(delete(TacviewXmlFile).where(TacviewXmlFile.id == rec.id))
    db.flush()
    p = Path(stored)
    if not p.is_absolute():
        p = Path(settings.storage_dir) / p
    try:
        if p.is_file():
            p.unlink()
    except OSError as exc:
        log.warning("Tacview XML 原件删除失败 %s：%s", p, exc)
