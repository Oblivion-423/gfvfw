"""BMS Logbook（``.lbk``）归档与名册同步。

职责边界（有意设计）
--------------------
1. **只归档，不解析。** ``.lbk`` 是私有二进制格式，且 BMS 版本升级可能静默改动它
   （见 ``models/identity.py::LogbookFile`` 的说明与 requirements §8.1）。
   归档带来三件事：绑定游戏内身份、留下审计凭据、可随时下载原件核对。
2. **声明值与名册记录分离。** 成员自己填的军衔/累计时长落在
   ``logbook_files.declared_*``，**不直接影响名册**；只有具备敏感权限的人
   调用 :func:`confirm_logbook` 才写入 ``members.*``。
   否则任何成员都能自封飞行时数与军衔。
3. **口径不混用。** Logbook 的累计时长是**跨存档历史总量**（含本系统上线前），
   与 ACMI 统计出的架次之和不是一回事，页面上必须分别标注。

用法::

    from gfvfw.services import logbook as LB
    rec, warn = LB.store_upload(db, member_id, filename, tmp_path, uploaded_by=uid)
    LB.declare(db, rec, rank_id=..., hours_seconds=..., sortie_count=...)
    LB.confirm_logbook(db, rec, actor_user_id=uid, actor_role="commander")
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..models import (
    LogbookFile, Member, MemberQualification, Qualification, Rank, User,
)

log = logging.getLogger("gfvfw.logbook")

#: ``.lbk`` 体积上限。实测真实文件仅 372 字节；给足余量但拦掉误传的大文件
#: （例如把整份 BMS 安装目录里的东西选进来）。
MAX_LOGBOOK_BYTES = 1024 * 1024

#: 接受的扩展名（小写）。
ALLOWED_SUFFIXES = (".lbk",)


class LogbookError(ValueError):
    """可展示给用户的错误（由路由转成表单提示）。"""


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _park_path(sha256: str, filename: str) -> Path:
    """落盘到 ``logbook/<年月>/<hash16>__<原名>``。"""
    now = utcnow()
    sub = Path(settings.storage_dir) / "logbook" / now.strftime("%Y-%m")
    sub.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    return sub / ("%s__%s" % (sha256[:16], safe))


def store_upload(db: Session, member_id: str, filename: str, source_path: Path,
                 uploaded_by: str, note: Optional[str] = None
                 ) -> tuple[LogbookFile, list[str]]:
    """归档一份 Logbook。返回 ``(记录, 警告列表)``。

    幂等：同一成员重复上传**完全相同**的文件时，返回已有记录且不再落盘。

    校验有意宽松 —— 我们并不解析内容，所以"拒绝可疑文件"的价值低，
    但对**扩展名**与**体积**做检查，避免误传把存储塞满。
    """
    name = (filename or "").strip() or "logbook.lbk"
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise LogbookError(
            "只接受 BMS Logbook 文件（扩展名应为 %s）。"
            "文件在 BMS 安装目录的 User\\Config\\<呼号>.lbk。" % " 或 ".join(ALLOWED_SUFFIXES))

    src = Path(source_path)
    if not src.exists():
        raise LogbookError("上传的文件不完整，请重试。")

    sha256, size = _hash_file(src)
    if size == 0:
        raise LogbookError("文件是空的，请确认选对了文件。")
    if size > MAX_LOGBOOK_BYTES:
        raise LogbookError("文件超过上限 %d KB，看起来不像 Logbook。"
                           % (MAX_LOGBOOK_BYTES // 1024))

    existing = db.scalar(
        select(LogbookFile)
        .where(LogbookFile.member_id == member_id,
               LogbookFile.sha256 == sha256))
    if existing is not None:
        return existing, ["这份文件此前已经上传过了，未重复归档。"]

    dest = _park_path(sha256, name)
    if not dest.exists():
        shutil.copy2(src, dest)

    rec = LogbookFile(
        member_id=member_id,
        original_filename=name,
        stored_path=str(dest.relative_to(Path(settings.storage_dir))).replace("\\", "/"),
        sha256=sha256,
        size_bytes=size,
        uploaded_by=uploaded_by,
        note=(note or "").strip() or None,
    )
    db.add(rec)
    db.flush()

    warn: list[str] = []
    # 实测真实 logbook 都是 372 字节的定长文件；体积异常时给个温和提示，
    # 但**不拒绝** —— 飞得多的存档可能更大。
    if size not in (372,):
        warn.append(
            "已归档，但体积（%d 字节）与实测样本（372 字节）不同。"
            "若这不是 Logbook 文件，请删除后重传。" % size)
    return rec, warn


def delete_upload(db: Session, rec: LogbookFile) -> None:
    """删除记录并清理磁盘原件（**硬删除**，以便同一文件可重新上传）。"""
    path = Path(settings.storage_dir) / rec.stored_path
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:                      # 磁盘异常不应阻断数据库清理
        log.warning("删除 logbook 文件失败 %s：%s", path, exc)
    db.delete(rec)


def absolute_path(rec: LogbookFile) -> Path:
    return Path(settings.storage_dir) / rec.stored_path


# --------------------------------------------------------------------------
# 声明值
# --------------------------------------------------------------------------

def declare(db: Session, rec: LogbookFile, *,
            rank_id: Optional[str] = None,
            hours_seconds: Optional[int] = None,
            sortie_count: Optional[int] = None,
            qualification_ids: Optional[list[str]] = None,
            apply_now: bool = True,
            actor_user_id: Optional[str] = None,
            actor_role: Optional[str] = None) -> dict[str, Any]:
    """记录成员从 LogbookEditor 界面读出的值。

    **按联队要求：Logbook 数据直接归档，不需要审核。**
    因此 ``apply_now=True``（默认）时会**立刻写入名册**，
    不再有"声明 → 待确认 → 指挥确认"的中间态。

    ``None`` 表示"这一项没填"，与"填了 0"不同 —— 0 小时是合法值。

    返回变更摘要（供审计与页面提示）；``apply_now=False`` 时只存声明值，
    返回空摘要（保留这条路径便于将来需要审核时启用）。
    """
    if rank_id:
        if db.get(Rank, rank_id) is None:
            raise LogbookError("军衔取值不合法。")
    if hours_seconds is not None and hours_seconds < 0:
        raise LogbookError("累计飞行时长不能为负数。")
    if sortie_count is not None and sortie_count < 0:
        raise LogbookError("累计架次不能为负数。")

    ids = [q for q in (qualification_ids or []) if q]
    if ids:
        known = set(db.scalars(
            select(Qualification.id).where(Qualification.id.in_(ids))).all())
        missing = [q for q in ids if q not in known]
        if missing:
            raise LogbookError("有 %d 项资质取值不合法。" % len(missing))
        ids = sorted(set(ids))

    rec.declared_rank_id = rank_id or None
    rec.declared_hours_seconds = hours_seconds
    rec.declared_sorties = sortie_count
    rec.declared_qualification_ids_json = json.dumps(ids) if ids else None

    if not apply_now:
        return {"changed": [], "no_change": True}

    summary = apply_to_roster(db, rec, actor_user_id=actor_user_id,
                              actor_role=actor_role)
    return summary


# --------------------------------------------------------------------------
# 写入名册
# --------------------------------------------------------------------------

def apply_to_roster(db: Session, rec: LogbookFile, *,
                    actor_user_id: Optional[str],
                    actor_role: Optional[str] = None) -> dict[str, Any]:
    """把声明值写入名册记录。返回变更摘要（用于审计与页面提示）。

    **按联队要求，Logbook 数据直接归档、无需审核** —— 所以本函数在
    保存声明值时就被调用（见 :func:`declare`），不再有独立的"确认"步骤。

    ⚠️ **调用方负责权限与审计**。当前实现由 ``logbook.upload``
    （成员可改自己的）把关；写入的值一律标 ``source='logbook'`` 并记录
    操作者与时间，所以事后完全可追溯、可回退。

    资质处理口径：**只同步 ``source='logbook'`` 的资质**。
    手动授予的资质不会被撤销 —— 否则一次 Logbook 登记就会抹掉教官的考核记录。
    """
    member = db.get(Member, rec.member_id)
    if member is None:
        raise LogbookError("找不到对应成员。")

    before: dict[str, Any] = {
        "rank_id": member.rank_id,
        "logbook_hours_seconds": member.logbook_hours_seconds,
        "logbook_sorties": member.logbook_sorties,
    }
    changed: list[str] = []

    if rec.declared_rank_id:
        if member.rank_id != rec.declared_rank_id:
            changed.append("军衔")
        member.rank_id = rec.declared_rank_id
        member.rank_source = "logbook"
        member.rank_updated_at = utcnow()
        member.rank_updated_by = actor_user_id

    if rec.declared_hours_seconds is not None:
        if member.logbook_hours_seconds != rec.declared_hours_seconds:
            changed.append("累计飞行时长")
        member.logbook_hours_seconds = rec.declared_hours_seconds

    if rec.declared_sorties is not None:
        if member.logbook_sorties != rec.declared_sorties:
            changed.append("累计架次")
        member.logbook_sorties = rec.declared_sorties

    if (rec.declared_hours_seconds is not None
            or rec.declared_sorties is not None):
        member.logbook_updated_at = utcnow()
        member.logbook_updated_by = actor_user_id

    # ---- 资质：只同步 logbook 来源的那一批 ----
    declared = set(rec.declared_qualification_ids())
    if declared or _has_logbook_quals(db, member.id):
        for qid in sorted(declared):
            row = db.scalar(
                select(MemberQualification)
                .where(MemberQualification.member_id == member.id,
                       MemberQualification.qualification_id == qid))
            if row is None:
                db.add(MemberQualification(
                    member_id=member.id, qualification_id=qid,
                    source="logbook", granted_by=actor_user_id,
                    updated_by=actor_user_id,
                    note="由 Logbook 登记"))
                changed.append("新增资质")
            elif row.revoked_at is not None:
                row.revoked_at = None
                row.source = "logbook"
                row.updated_by = actor_user_id
                changed.append("恢复资质")
        # 撤销：仅限 logbook 来源且本次未声明的
        for row in db.scalars(
                select(MemberQualification)
                .where(MemberQualification.member_id == member.id,
                       MemberQualification.source == "logbook",
                       MemberQualification.revoked_at.is_(None))).all():
            if row.qualification_id not in declared:
                row.revoked_at = utcnow()
                row.updated_by = actor_user_id
                changed.append("撤销资质")

    #: ⚠️ 列名是 ``confirmed_*``，但**语义已随"不需要审核"改为"已写入名册的时刻/操作者"**。
    #: 保留列名是为了不破坏已有数据（``schema_sync`` 只能加列不能改名）。
    #: 见 :func:`apply_to_roster`。
    rec.confirmed_at = utcnow()
    rec.confirmed_by = actor_user_id

    after = {
        "rank_id": member.rank_id,
        "logbook_hours_seconds": member.logbook_hours_seconds,
        "logbook_sorties": member.logbook_sorties,
    }
    return {
        "member_id": member.id,
        "callsign": member.callsign,
        "before": before,
        "after": after,
        # 去重但保持稳定顺序，便于测试与审计阅读
        "changed": sorted(set(changed)),
        "no_change": not changed,
    }


def _has_logbook_quals(db: Session, member_id: str) -> bool:
    return db.scalar(
        select(MemberQualification.id)
        .where(MemberQualification.member_id == member_id,
               MemberQualification.source == "logbook",
               MemberQualification.revoked_at.is_(None))
        .limit(1)) is not None


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def list_for_member(db: Session, member_id: str) -> list[LogbookFile]:
    """某成员的全部 Logbook 归档，最新在前。"""
    return list(db.scalars(
        select(LogbookFile)
        .where(LogbookFile.member_id == member_id)
        .order_by(LogbookFile.created_at.desc())).all())


def current_for_member(db: Session, member_id: str) -> Optional[LogbookFile]:
    """最新一份归档（"当前使用的 Logbook"）。没有则返回 None。"""
    return db.scalar(
        select(LogbookFile)
        .where(LogbookFile.member_id == member_id)
        .order_by(LogbookFile.created_at.desc())
        .limit(1))


def applied_for_member(db: Session, member_id: str) -> Optional[LogbookFile]:
    """最新一份**已写入名册**的归档。"""
    return db.scalar(
        select(LogbookFile)
        .where(LogbookFile.member_id == member_id,
               LogbookFile.confirmed_at.is_not(None))
        .order_by(LogbookFile.created_at.desc())
        .limit(1))



def get(db: Session, logbook_id: str) -> Optional[LogbookFile]:
    return db.get(LogbookFile, logbook_id)
