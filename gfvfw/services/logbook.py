"""BMS Logbook（``.lbk``）归档、自动解析与名册同步。

职责边界（有意设计）
--------------------
1. **归档 + 自动解析。** ``.lbk`` 是私有二进制格式，**已经解出**
   （见 :mod:`gfvfw.lbk_parser`：372 字节定长，差分异或加密，
   密钥 ``"Falcon is your Master"``）。所以上传后**不需要手动输入** ——
   :func:`store_upload` 直接把军衔 / 累计飞行时长 / 累计架次 / 勋章写入名册。
   原件同时完整归档：绑定游戏内身份、留下审计凭据、可随时下载核对，
   且解析器改进后能用 :func:`reparse` 在原件上重跑（不必让成员重传）。
2. **解析失败不影响归档。** BMS 版本升级可能静默改动格式，那时
   ``logbook_files.parse_error`` 会记下原因，页面明确告知"已归档、未解析"，
   原件仍可下载核对 —— 绝不因为解析失败而丢掉成员的文件。
   （见 ``models/identity.py::LogbookFile`` 与 requirements §8.1。）
3. **名册只收四个口径的字段。** 统计区（击杀、任务数、评分等）的含义
   已按联队对照表（``log.xlsx``）确认并正式命名（见 :mod:`gfvfw.lbk_parser`），
   但除**执行任务数 → 累计架次**外仍**只展示、不写入名册** —— 名册没有
   对应列，全量数据留在 ``parsed_json`` 里备将来使用。
   见 :func:`apply_parsed`。
4. **口径不混用。** Logbook 的累计时长是**跨存档历史总量**（含本系统上线前），
   与 ACMI 统计出的架次之和不是一回事，页面上必须分别标注。
5. **没有"审核/确认"环节。** 按联队要求 Logbook 数据保存即入库
   （:func:`apply_parsed` 在归档事务内直接生效），也没有手填表单 ——
   手动录入路径已随自动解析一起移除。

用法::

    from gfvfw.services import logbook as LB
    rec, warn = LB.store_upload(db, member_id, filename, tmp_path, uploaded_by=uid)
    LB.reparse(db, rec, actor_user_id=uid)      # 解析器改进后回填
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..models import LogbookFile, Member, MemberAward, Rank
from .. import lbk_parser as LBP

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
                 ) -> tuple[LogbookFile, list[str], str]:
    """归档一份 Logbook **并自动解析**。

    返回 ``(记录, 提示列表, 状态)``。状态取值：

    ``"stored"``
        新归档且解析成功（名册已按文件内容更新）。
    ``"stored_unparsed"``
        **原件已归档，但自动解析失败** —— 页面必须区分于成功，
        不能谎报"已填入名册"。
    ``"duplicate"``
        同一成员上传过**完全相同**（SHA256 相同）的文件，未重复落盘、未重复应用。

    联队要求：上传后**不需要手动输入**，所以这里会把能解析出来的字段
    直接写进名册（军衔 / 累计飞行时长 / 累计架次 / 勋章）。
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
        return existing, ["这份文件此前已经上传过了，未重复归档。"], "duplicate"

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

    warn, ok, _summary = _parse_into(db, rec, src.read_bytes(),
                                     actor_user_id=uploaded_by)
    return rec, warn, ("stored" if ok else "stored_unparsed")

def _parse_into(db: Session, rec: LogbookFile, data: bytes, *,
                actor_user_id: Optional[str]
                ) -> tuple[list[str], bool, dict[str, Any]]:
    """解析 ``data``，把结果落到 ``rec`` 与名册上。

    返回 ``(提示, 是否解析成功, 变更摘要)``。摘要为空 dict 表示解析失败。

    :func:`store_upload` 与 :func:`reparse` 共用的唯一解析实现 ——
    两个入口各写一遍曾经让字段落库出现分歧。
    """
    try:
        parsed = LBP.parse(data)
    except LBP.LbkError as exc:
        # 解析失败**不影响归档** —— 原件已经存好了，将来格式搞清楚了还能重解析
        rec.parse_error = str(exc)
        rec.parsed_at = utcnow()
        log.warning("logbook 解析失败 file=%s：%s", rec.original_filename, exc)
        return ["文件已归档，但自动解析失败：%s" % exc], False, {}

    rec.parse_error = None
    rec.parser_version = LBP.PARSER_VERSION
    rec.parsed_at = utcnow()
    rec.parsed_json = json.dumps(
        {"fields": parsed.fields,
         "certain": [s.name for s in LBP.FIELDS if s.certain],
         "uncertain": parsed.uncertain,
         "warnings": parsed.warnings},
        ensure_ascii=False, default=str)
    summary = apply_parsed(db, rec, parsed, actor_user_id=actor_user_id)
    # ⚠️ 呼号提示排在前面，但**不能挤掉**"已自动填入…"的摘要 ——
    #    两条都要出现在页面上（路由会把它们拼起来一起带回去）。
    warn = _callsign_mismatch(db, rec, parsed)
    if summary.get("no_change"):
        warn.append("解析成功，但名册记录与文件内容一致，无需更新。")
    else:
        warn.append("已自动填入名册：%s。" % "、".join(summary["changed"]))
    return warn, True, summary


def _callsign_mismatch(db: Session, rec: LogbookFile,
                       parsed: "LBP.LbkRecord") -> list[str]:
    """文件里的呼号与名册呼号不一致时给出提示（**不阻断**）。

    动机：``logbook.upload`` 允许成员上传自己的文件，但服务端无法验证
    "这个文件确实是他的" —— 成员完全可以传队友的 ``.lbk``。
    文件里的呼号是游戏内写的，是最直接的反证材料，所以摊到提示里。

    ⚠️ 只是**提示**：游戏内呼号与站内呼号本来就可能不同
    （例如站内叫 Brian、游戏里叫 Raven），因此不能据此拒绝上传。
    """
    file_cs = (parsed.callsign or "").strip()
    if not file_cs:
        return []
    member = db.get(Member, rec.member_id)
    if member is None or not (member.callsign or "").strip():
        return []
    if file_cs.lower() == member.callsign.strip().lower():
        return []
    return ["注意：文件里的呼号是「%s」，与名册呼号「%s」不同 —— "
            "请确认没有传成别人的文件（若只是游戏内呼号不同，忽略本提示）。"
            % (file_cs, member.callsign)]


def record_parse_failure(db: Session, rec: LogbookFile, error: str) -> None:
    """把"这次解析失败"记到归档上（不提交，由调用方决定事务边界）。

    调用方通常是 :func:`reparse` 失败后的路由：那里已经把事务回滚掉了
    （不能让半截解析污染名册），但**失败这件事本身要留下** ——
    否则归档列表会一直显示旧的「已写入名册」徽标，与事实不符。
    """
    rec.parse_error = error
    rec.parsed_at = utcnow()


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


def reparse(db: Session, rec: LogbookFile, *,
            actor_user_id: Optional[str]) -> dict[str, Any]:
    """用**已归档的原件**重新解析并按结果同步名册。

    用途：解析器改进后回填历史存档，不必让成员重传。
    解析失败会抛 :class:`LogbookError`（与上传不同 —— 上传时失败也要保住归档，
    重解析时失败则应当让操作者看到原因）。
    """
    path = absolute_path(rec)
    if not path.exists():
        raise LogbookError("原件已不在服务器上，无法重新解析。")
    warn, ok, summary = _parse_into(db, rec, path.read_bytes(),
                                    actor_user_id=actor_user_id)
    if not ok:
        raise LogbookError(warn[0] if warn else "解析失败。")
    return summary


# --------------------------------------------------------------------------
# 解析结果 → 名册
# --------------------------------------------------------------------------

#: 6 个勋章字段（文件偏移）→ (稳定 code, 显示名)。
#:
#: ⚠️ **偏移与名称的对应关系是推断的**：官方工具正好有 6 个 ``edtMedal*``
#: 控件，而文件里正好有 6 个勋章字节（0x8c..0x91），数量吻合；
#: 具体哪个字节对应哪枚勋章，是按官方界面的字段顺序推断的。
#: ``level`` 存原始字节值（实测会随飞行增加，所以不是布尔）。
MEDAL_FIELDS: tuple[tuple[int, str, str], ...] = (
    (0x8c, "dist_fly_cross", "Distinguished Flying Cross"),
    (0x8d, "longevity", "Longevity"),
    (0x8e, "air_medal", "Air Medal"),
    (0x8f, "korea_campaign", "Korean Campaign"),
    (0x90, "air_force_cross", "Air Force Cross"),
    (0x91, "silver_star", "Silver Star"),
)

#: 「累计架次」取自**执行任务数**（偏移 ``0x6a``，字段 ``missions_flown``）。
#:
#: ⚠️ **口径修正（2026-09，联队字段对照表 ``log.xlsx``）**：此前这里误把
#: ``0x76`` 当架次 —— 当时的时间序列佐证（同一人 7 个月 +45，同期飞行小时
#: +44.69）实为巧合，联队对照表确认 ``0x76`` 是**击落敌机数**、``0x6a`` 才是
#: 执行任务数。真实样本交叉验证：292 任务对 296.29 飞行小时 ≈ 1.015 小时/任务，
#: 量纲比旧口径更吻合。修正后需要用 ``scripts/reparse_logbooks.py --apply``
#: 或页面上的「重新解析」回填历史名册（旧值是击落数，偏小）。
MISSIONS_OFFSET = 0x6a

#: 架次取值字段：先认正式命名，再退回解析器 v1 的偏移名（历史 ``parsed_json``
#: 里存的是旧名；重解析回填后旧名不再出现）。
_SORTIE_FIELD_CANDIDATES = ("missions_flown",
                            "counter_%02x" % MISSIONS_OFFSET)


def _sortie_count(parsed: "LBP.LbkRecord") -> Optional[int]:
    """取"累计架次"（= Logbook 的**执行任务数**，一次任务计一架次）。

    先认正式命名（联队对照表确认后解析器已改名 ``missions_flown``），
    再退回偏移名 ``counter_6a`` —— 后者只出现在**尚未重解析**的历史
    ``parsed_json`` 里（那条路径按旧名取值，语义相同）。
    """
    for key in _SORTIE_FIELD_CANDIDATES:
        val = parsed.fields.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return None


def apply_parsed(db: Session, rec: LogbookFile, parsed: "LBP.LbkRecord", *,
                 actor_user_id: Optional[str]) -> dict[str, Any]:
    """把**已解析**的 Logbook 内容写入名册。返回变更摘要。

    只写**已确证**的字段（见 :data:`LBP.FIELDS` 的 ``certain``），
    推断字段一律不写 —— 宁可少填，也不要把猜的偏移当成事实塞进名册。
    """
    member = db.get(Member, rec.member_id)
    if member is None:
        raise LogbookError("找不到对应成员。")

    before = {
        "rank_id": member.rank_id,
        "logbook_hours_seconds": member.logbook_hours_seconds,
        "logbook_sorties": member.logbook_sorties,
    }
    changed: list[str] = []

    # ---- 军衔：文件里的下标 + 1 == 本系统 ranks.level ----
    idx = parsed.fields.get("rank_index")
    if isinstance(idx, int):
        rank = db.scalar(select(Rank).where(Rank.level == idx + 1))
        if rank is not None and member.rank_id != rank.id:
            member.rank_id = rank.id
            member.rank_source = "logbook"
            member.rank_updated_at = utcnow()
            member.rank_updated_by = actor_user_id
            changed.append("军衔（%s）" % rank.name)

    # ---- 累计飞行时长 ----
    hours = parsed.flight_hours
    if hours is not None and hours > 0:
        secs = int(round(hours * 3600))
        if member.logbook_hours_seconds != secs:
            member.logbook_hours_seconds = secs
            changed.append("累计飞行时长")

    # ---- 累计架次（唯一的"推断字段入库"例外，见 _SORTIE_FIELD_CANDIDATES）----
    sorties = _sortie_count(parsed)
    if sorties is not None and member.logbook_sorties != sorties:
        member.logbook_sorties = sorties
        changed.append("累计架次")

    if ("累计飞行时长" in changed or "累计架次" in changed):
        member.logbook_updated_at = utcnow()
        member.logbook_updated_by = actor_user_id

    # ---- 勋章：只同步 logbook 来源的，手工录入的不动 ----
    medal_changed = _sync_awards(db, member, parsed, actor_user_id)
    if medal_changed:
        changed.append("勋章（%s）" % medal_changed)

    after = {
        "rank_id": member.rank_id,
        "logbook_hours_seconds": member.logbook_hours_seconds,
        "logbook_sorties": member.logbook_sorties,
    }

    #: 已解析即视为"已入库"。⚠️ 列名是 ``confirmed_*``，但**语义是
    #: "已写入名册的时刻 / 操作者"**（历史遗留命名，保留是为了不破坏已有数据，
    #: 见本模块末尾「关于已移除的手动登记路径」）。
    rec.confirmed_at = utcnow()
    rec.confirmed_by = actor_user_id

    return {
        "member_id": member.id,
        "callsign": member.callsign,
        "before": before,
        "after": after,
        "changed": changed,
        "no_change": not changed,
    }


def _sync_awards(db: Session, member: Member, parsed: "LBP.LbkRecord",
                 actor_user_id: Optional[str]) -> str:
    """按解析出的 6 个勋章字节同步 :class:`MemberAward`。

    ⚠️ 只增不删：文件里某枚勋章变回 0 时**不撤销**已有记录 ——
    那更可能是换档/重开导致的历史清零，而不是"荣誉被收回"。
    返回变更摘要文本（空串表示无变化）。
    """
    added = updated = 0
    for off, code, label in MEDAL_FIELDS:
        val = parsed.fields.get("medal_%s" % code)
        if val is None:
            # 字段名可能因推断命名不同而取不到，退回按偏移取
            val = parsed.fields.get("medal_" + _MEDAL_BY_OFFSET.get(off, code))
        if not isinstance(val, int) or val <= 0:
            continue
        row = db.scalar(
            select(MemberAward)
            .where(MemberAward.member_id == member.id,
                   MemberAward.code == code))
        if row is None:
            db.add(MemberAward(member_id=member.id, code=code, name=label,
                               level=val, source="logbook",
                               note="由 Logbook 自动解析",
                               updated_by=actor_user_id))
            added += 1
        elif row.level != val:
            row.level = val
            row.source = "logbook"
            row.updated_by = actor_user_id
            updated += 1
    bits = []
    if added:
        bits.append("新增 %d 枚" % added)
    if updated:
        bits.append("更新 %d 枚" % updated)
    return "，".join(bits)


#: 偏移 → 当前字段名（用于按偏移回退取值的健壮性）
_MEDAL_BY_OFFSET = {off: code for off, code, _ in MEDAL_FIELDS}


def list_awards(db: Session, member_id: str) -> list[MemberAward]:
    return list(db.scalars(
        select(MemberAward)
        .where(MemberAward.member_id == member_id)
        .order_by(MemberAward.code)).all())


# --------------------------------------------------------------------------
# 关于已移除的"手动登记"路径
# --------------------------------------------------------------------------
#
# 历史上这里有一组函数 —— ``declare()`` / ``apply_to_roster()`` /
# ``confirm_logbook()`` / ``revoke_logbook()`` —— 让成员**手填**从
# LogbookEditor 界面上读到的军衔与飞行时数，再由指挥确认。
#
# `.lbk` 格式解出之后（见 :mod:`gfvfw.lbk_parser`），这条路径被整体删除：
#  * 手填是错的来源 —— 成员可以自封时数，而且没人会去核对；
#  * 它构成**第二条写入名册的通路**，绕过了自动解析，容易与解析结果打架。
# 现在唯一写入名册的入口是 :func:`apply_parsed`。
#
# ``logbook_files.declared_*`` 与 ``confirmed_*`` 这几列**保留在表里但不再写入**
# （``confirmed_at/confirmed_by`` 被 :func:`apply_parsed` 复用为"已写入名册的
# 时刻/操作者"，语义一致）。不删列是因为 ``schema_sync`` 只能加列不能改列，
# 删列会让已有归档记录对不上。


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
