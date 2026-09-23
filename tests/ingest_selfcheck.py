"""
端到端测试：ACMI 文件 → 数据库记录的完整链路。

覆盖:
  1. 建表与可移植性
  2. 上传→解析→落库（acmi_files / acmi_actors）
  3. SHA256 去重（重复上传同一文件不产生重复记录）
  4. 归并建议 → 人工确认 → Mission + Sortie 生成
  5. **未认领飞行员不生成架次**（AI/名册外成员）
  6. 飞行员认领后回填，且后续导入自动命中
  7. 解析失败留档（不静默丢弃）

运行:
    .venv\\Scripts\\python.exe tests\\test_ingest_pipeline.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from gfvfw.db import Base, check_portability  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AcmiActor, AcmiFile, Member, Mission, PilotMapping, Sortie,
)
from gfvfw.services.ingest import AcmiIngestService  # noqa: E402

REAL_DIR = r"G:\BMS\Falcon BMS 4.38\User\Acmi"

FAILURES: list[str] = []
CHECKS = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


# --------------------------------------------------------------------------
# 合成 ACMI（两个飞行员 + 一个 AI，物理自洽）
# --------------------------------------------------------------------------

def synthetic_acmi(pilots=("Alpha", "Bravo"), with_pilot_field=True) -> bytes:
    lines = [
        "FileType=text/acmi/tacview\n",
        "FileVersion=2.1\n",
        "0,DataRecorder=Falcon BMS 4.38.1\n",
        "0,ReferenceTime=2024-8-16T00:00:00Z\n",
    ]
    # 人驾对象
    for i, p in enumerate(pilots):
        oid = "%x" % (9 + i)
        lines.append(
            "%s,T=126.5|36.1%d|7.0|0|0|0|400000|250000|90,"
            "CallSign=Viper6%d,Coalition=ROK,CAS=0,Name=F-16CM-52,Pilot=%s,"
            "Type=Air+FixedWing\n" % (oid, i, i, p))
    # AI 对象（无 Pilot=）
    lines.append(
        "f,T=126.6|36.20|7.0|0|0|0|410000|251000|90,"
        "Coalition=ROK,CAS=0,Name=F-16C-52 ROKAF,Type=Air+FixedWing\n")
    lines.append("#0.0\n")
    for i, p in enumerate(pilots):
        oid = "%x" % (9 + i)
        lines.append("%s,T=126.5|36.1%d|6000|||0|400000|251112|,CAS=300,Mach=0.8\n"
                     % (oid, i))
    lines.append("#1800.0\n")
    for i, p in enumerate(pilots):
        oid = "%x" % (9 + i)
        lines.append("%s,T=126.5|36.4%d|7.0|||0|400000|283360|,CAS=0,Mach=0\n"
                     % (oid, i))
    return "".join(lines).encode("utf-8")


def write_acmi(dirpath: Path, name: str, body: bytes, as_zip: bool = True) -> Path:
    """写出 ACMI 文件。as_zip=True 时模拟真实的 .zip.acmi 形态。"""
    p = dirpath / name
    if as_zip:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("acmi.txt", body)
        p.write_bytes(buf.getvalue())
    else:
        p.write_bytes(body)
    return p


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------

def make_session(storage: Path) -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _close(db: Session) -> None:
    """关闭会话并释放引擎连接。

    ⚠️ 必须先 dispose 再删除临时目录：Windows 上 SQLite 仍持有文件句柄时
    ``shutil.rmtree`` 会抛 ``NotADirectoryError``，把真实断言结果掩盖掉。
    """
    bind = db.bind
    db.close()
    if bind is not None:
        bind.dispose()


def test_pipeline() -> None:
    print("\n[1] 完整链路：上传 → 解析 → 落库 → 归并 → 确认")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        storage = tdp / "storage"
        storage.mkdir()
        db = make_session(storage)
        svc = AcmiIngestService(storage_dir=storage)

        # 准备名册成员
        m_alpha = Member(callsign="Alpha")
        m_bravo = Member(callsign="Bravo")
        db.add_all([m_alpha, m_bravo])
        db.flush()

        f1 = write_acmi(tdp, "2026-06-01_10-00-00.zip.acmi", synthetic_acmi())
        res = svc.ingest_file(db, f1, uploaded_by=None)

        check("解析成功", res.acmi_file.parse_status == "parsed",
              "得到 %s (%s)" % (res.acmi_file.parse_status, res.acmi_file.parse_error))
        check("非重复文件", not res.duplicate)
        check("识别到 2 个带名飞行员", res.acmi_file.objects_with_pilot_name == 2,
              "得到 %s" % res.acmi_file.objects_with_pilot_name)
        check("识别到 1 个无名 AI", res.acmi_file.unnamed_ai_actors == 1,
              "得到 %s" % res.acmi_file.unnamed_ai_actors)
        check("文件名时间解析正确",
              res.acmi_file.filename_time is not None
              and res.acmi_file.filename_time.strftime("%Y-%m-%d %H:%M:%S")
              == "2026-06-01 10:00:00",
              "得到 %s" % res.acmi_file.filename_time)
        check("任务开始时间 = 文件名 − t_max（1800s）",
              res.acmi_file.recorded_start_at is not None
              and res.acmi_file.recorded_start_at.strftime("%H:%M:%S") == "09:30:00",
              "得到 %s" % res.acmi_file.recorded_start_at)
        check("落盘文件存在", (storage / res.acmi_file.stored_path).exists())

        actors = list(db.scalars(select(AcmiActor)))
        check("仅带名对象建 actor 行（AI 不建）", len(actors) == 2,
              "得到 %d" % len(actors))
        check("未认领飞行员被列出",
              set(res.unclaimed_pilots) == {"Alpha", "Bravo"},
              "得到 %s" % res.unclaimed_pilots)

        # ---- 去重 ----
        res2 = svc.ingest_file(db, f1, original_filename="另一份同名副本.zip.acmi")
        check("重复上传被识别（SHA256 去重）", res2.duplicate)
        check("未产生第二条 acmi_files 记录",
              len(list(db.scalars(select(AcmiFile)))) == 1)

        # ---- 认领 ----
        svc.claim_pilot(db, "Alpha", m_alpha.id)
        svc.claim_pilot(db, "Bravo", m_bravo.id)
        db.flush()
        refreshed = list(db.scalars(select(AcmiActor)))
        check("认领后 actor 回填 member_id",
              all(a.member_id is not None for a in refreshed),
              "得到 %s" % [a.member_id for a in refreshed])
        check("认领后 is_member_flight 为真",
              all(a.is_member_flight for a in refreshed))

        # ---- 归并 ----
        plan = svc.suggest_merge(db, [res.acmi_file.id])
        check("生成归并批次", plan.batch.status == "suggested")
        check("归并批次包含飞行员", set(plan.pilot_names) == {"Alpha", "Bravo"},
              "得到 %s" % plan.pilot_names)
        check("归并方案快照已存", bool(plan.batch.plan_json))

        # ---- 确认入库 ----
        mission = svc.confirm_merge(db, plan.batch.id, mission_type="training")
        db.flush()
        sorties = list(db.scalars(select(Sortie)))
        check("确认后生成 2 条架次", len(sorties) == 2, "得到 %d" % len(sorties))
        check("架次全部归属该任务",
              all(s.mission_id == mission.id for s in sorties))
        check("架次 member_id 已绑定",
              all(s.member_id in (m_alpha.id, m_bravo.id) for s in sorties))
        check("起飞次数按联队口径 = 1",
              all(s.takeoff_count == 1 for s in sorties))
        check("机型已归一化",
              all(not hasattr(s, "aircraft_raw_name")
                  or s.aircraft_raw_name == "F-16CM-52" for s in sorties))
        db.commit()
        _close(db)


def test_unclaimed_not_counted() -> None:
    print("\n[2] 未认领飞行员不生成架次（AI / 名册外成员不进统计）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        storage = tdp / "storage"
        storage.mkdir()
        db = make_session(storage)
        svc = AcmiIngestService(storage_dir=storage)

        m1 = Member(callsign="Alpha")
        db.add(m1)
        db.flush()
        svc.claim_pilot(db, "Alpha", m1.id)

        # 文件含 Alpha（已认领）与 Ghost（未认领）
        f1 = write_acmi(tdp, "2026-06-02_10-00-00.zip.acmi",
                        synthetic_acmi(pilots=("Alpha", "Ghost")))
        res = svc.ingest_file(db, f1)
        check("Ghost 被列为未认领", "Ghost" in res.unclaimed_pilots,
              "得到 %s" % res.unclaimed_pilots)

        plan = svc.suggest_merge(db, [res.acmi_file.id])
        svc.confirm_merge(db, plan.batch.id)
        db.flush()

        sorties = list(db.scalars(select(Sortie)))
        check("只为已认领者生成架次（1 条）", len(sorties) == 1,
              "得到 %d" % len(sorties))
        if sorties:
            check("架次属于 Alpha", sorties[0].raw_pilot_name == "Alpha",
                  "得到 %s" % sorties[0].raw_pilot_name)

        actors = list(db.scalars(select(AcmiActor)))
        check("两个飞行员都有 actor 记录", len(actors) == 2, "得到 %d" % len(actors))
        check("Ghost 的 actor 未绑定成员",
              any(a.pilot_name == "Ghost" and a.member_id is None for a in actors))
        _close(db)


def test_parse_failure_recorded() -> None:
    print("\n[3] 解析失败必须留档，不静默丢弃")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        storage = tdp / "storage"
        storage.mkdir()
        db = make_session(storage)
        svc = AcmiIngestService(storage_dir=storage)

        # 构造一个 ZIP 内不含 acmi.txt 的坏文件
        bad = tdp / "2026-06-03_10-00-00.zip.acmi"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.md", "not an acmi")
        bad.write_bytes(buf.getvalue())

        res = svc.ingest_file(db, bad)
        check("坏文件被标记为 failed", res.acmi_file.parse_status == "failed",
              "得到 %s" % res.acmi_file.parse_status)
        check("失败原因已记录", bool(res.acmi_file.parse_error),
              "得到 %r" % res.acmi_file.parse_error)
        check("原件仍被保存", (storage / res.acmi_file.stored_path).exists())
        _close(db)


def test_real_file_end_to_end() -> None:
    print("\n[4] 真实 ACMI 端到端（只读源文件，不导入历史数据）")

    if not os.path.isdir(REAL_DIR):
        print("  跳过：未找到真实 ACMI 目录")
        return
    names = sorted(os.listdir(REAL_DIR))
    if not names:
        print("  跳过：目录为空")
        return
    # 选一个中等大小的文件
    cands = sorted((os.path.join(REAL_DIR, n) for n in names if n.endswith(".acmi")),
                   key=lambda p: abs(os.path.getsize(p) - 500_000))
    src = cands[0]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        storage = tdp / "storage"
        storage.mkdir()
        db = make_session(storage)
        svc = AcmiIngestService(storage_dir=storage)

        res = svc.ingest_file(db, src)
        check("真实文件解析成功", res.acmi_file.parse_status == "parsed",
              "得到 %s / %s" % (res.acmi_file.parse_status, res.acmi_file.parse_error))
        check("提取到飞行员名", res.acmi_file.objects_with_pilot_name is not None
              and res.acmi_file.objects_with_pilot_name >= 1,
              "得到 %s" % res.acmi_file.objects_with_pilot_name)
        check("任务开始时间已换算", res.acmi_file.recorded_start_at is not None)
        print("      文件 %s" % os.path.basename(src))
        print("      存档时刻(UTC) %s" % res.acmi_file.recorded_end_at)
        print("      任务开始(UTC) %s" % res.acmi_file.recorded_start_at)
        print("      带名对象 %s / 无名 AI %s / 时长 %.0f 秒"
              % (res.acmi_file.objects_with_pilot_name,
                 res.acmi_file.unnamed_ai_actors,
                 res.acmi_file.duration_seconds or 0))
        print("      未认领飞行员 %s" % res.unclaimed_pilots)

        # ★ 不变量：时间窗宽度必须等于录制时长。
        #   曾经 recorded_start_at 被写成 t=0 基准点（剧本纪元），于是窗口
        #   比真实录制宽出"首标记"那一整段，而 duration_seconds 又取了末标记，
        #   两者一起把任务时长撑大。这条断言把三者的关系钉死。
        af = res.acmi_file
        if af.recorded_start_at and af.recorded_end_at and af.duration_seconds:
            width = (af.recorded_end_at - af.recorded_start_at).total_seconds()
            check("时间窗宽度 == 录制时长", abs(width - af.duration_seconds) < 0.01,
                  "宽度 %.1f 秒 vs 时长 %.1f 秒" % (width, af.duration_seconds))
        else:
            check("时间窗宽度 == 录制时长", False,
                  "字段缺失 start=%s end=%s dur=%s"
                  % (af.recorded_start_at, af.recorded_end_at, af.duration_seconds))
        # 录制时长必须 <= 末标记（首标记 >= 0），且此处应是严格小于
        if af.max_relative_seconds:
            check("录制时长 < 末标记（说明首标记不为 0）",
                  (af.duration_seconds or 0) < af.max_relative_seconds,
                  "时长 %s 末标记 %s" % (af.duration_seconds, af.max_relative_seconds))
            check("最小标记已入库", af.min_relative_seconds is not None,
                  "min=%s" % af.min_relative_seconds)
        _close(db)


def main() -> int:
    print("=" * 70)
    probs = check_portability()
    check("模型可移植性检查通过（0 问题）", len(probs) == 0, "得到 %s" % probs)

    test_pipeline()
    test_unclaimed_not_counted()
    test_parse_failure_recorded()
    test_real_file_end_to_end()

    print("\n" + "=" * 70)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
