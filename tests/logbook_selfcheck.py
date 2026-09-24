"""
自校验：**BMS Logbook 上传、自动解析与名册同步**（不需要审核、不需要手填）。

联队口径：**上传 Logbook 之后不需要任何手动输入。**
``.lbk`` 格式已经解出（见 ``gfvfw/lbk_parser.py``），所以本套测试的重点是：

* 上传 ``.lbk`` 时**自动解析**并直接把军衔 / 累计时长 / 累计架次 / 勋章写入名册
* 页面上**没有手填表单**（手动登记路径已随自动解析一起移除）
* 解析失败时**原件仍然归档**，且页面如实显示"已归档、未解析"，不谎报已入库
* 归档本身的性质：SHA256 去重、可下载、可删除后重传
* 权限边界在服务端强制（无权限者构造请求得 403）
* Logbook 累计时长 / 日志时长 / 记录时长是**三个不同的量**，页面上分别标注

运行:
    .venv\\Scripts\\python.exe tests\\logbook_selfcheck.py
"""
from __future__ import annotations

import os
import re
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw import lbk_parser as LBP  # noqa: E402
from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AuditLog, LogbookFile, Member, MemberAward, MemberQualification, MemberRole,
    Qualification, Rank, Role, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services import logbook as LB  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

PW = "password123"

#: 一份**格式合法**的 logbook（用解析器的 encode 造出来，能被真实解析路径读回）。
#: ⚠️ 不要用随机字节冒充 —— 那样只能测到"归档"，测不到"自动解析"。
MEDALS_ALL_ZERO = (0x8c, 0x8d, 0x8e, 0x8f, 0x90, 0x91)


def build_lbk(*, name: str = "Joe Pilot", callsign: str = "Oblivion",
              squadron: str = "GFVFW", date: str = "09/22/26",
              hours: float = 296.29, rank_index: int = 4,
              ace_factor: float = 1.0, missions: int = 161,
              aa_kills: int = 0, medals: dict[int, int] | None = None) -> bytes:
    """造一份 372 字节、校验通过的 ``.lbk``。

    ``missions`` 写入 0x6a「执行任务数」（名册累计架次的来源），
    ``aa_kills`` 写入 0x76「击落敌机数」—— 两者分开给值，
    用来守住"击落数不得被当成架次"的回归（2026-09 口径修正）。
    """
    plain = bytearray(LBP.FILE_SIZE)

    def put_str(off: int, text: str, maxlen: int) -> None:
        raw = text.encode("latin-1")[:maxlen - 1]
        plain[off:off + len(raw)] = raw

    put_str(0x00, name, 0x15)
    put_str(0x15, callsign, 0x13)
    put_str(0x2d, date, 9)
    put_str(0x3a, squadron, 0x0d)
    struct.pack_into("<f", plain, 0x48, hours)
    struct.pack_into("<f", plain, 0x4c, ace_factor)
    struct.pack_into("<I", plain, 0x50, rank_index)
    struct.pack_into("<H", plain, 0x6a, missions)
    struct.pack_into("<H", plain, 0x76, aa_kills)
    for off, val in (medals or {}).items():
        plain[off] = val
    # 0x170 的哨兵保持 0（官方读取器的校验条件）
    return LBP.encode(bytes(plain))


#: 旧测试里那份"只是体积对"的垃圾文件 —— 用来验证**解析失败也要保住归档**
GARBAGE_LBK = bytes(range(256)) + bytes(116)


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


def csrf_of(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def build_app(tmpdir: Path):
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmpdir / "logbook.sqlite3").as_posix(),
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import gfvfw.config as cfgmod
    import gfvfw.db as dbmod
    import gfvfw.web.deps as depsmod
    appmod = sys.modules["gfvfw.web.app"]

    orig = (dbmod.SessionLocal, depsmod.SessionLocal, appmod.SessionLocal)
    dbmod.SessionLocal = depsmod.SessionLocal = appmod.SessionLocal = TestSession

    # ⚠️ 必须同时重定向 storage_dir，否则会往真实存储目录写孤儿文件
    orig_storage = cfgmod.settings.storage_dir
    cfgmod.settings.storage_dir = tmpdir / "storage"
    cfgmod.settings.storage_dir.mkdir(parents=True, exist_ok=True)

    with TestSession() as db:
        seed(db)
    return appmod.create_app(), TestSession, orig, orig_storage


def make_user(db, callsign: str, role_code: str):
    member = Member(callsign=callsign, status="active")
    db.add(member)
    db.flush()
    user = User(username=callsign.lower(),
                password_hash=hash_password(PW), status="active",
                member_id=member.id)
    db.add(user)
    role = db.scalar(select(Role).where(Role.code == role_code))
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    db.commit()
    return member.id, user.id


def login(client, username: str) -> bool:
    page = client.get("/login")
    r = client.post("/login",
                    data={"username": username, "password": PW,
                          "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    return r.status_code == 303


def write_lbk(tmpdir: Path, name: str = "Oblivion.lbk",
              data: bytes = None) -> Path:
    p = tmpdir / name
    p.write_bytes(build_lbk() if data is None else data)
    return p


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 74)
    print("BMS Logbook 上传 / 自动解析 / 名册同步自校验")
    print("=" * 74)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        app, TestSession, orig, orig_storage = build_app(tdp)
        try:
            with TestSession() as db:
                owner_mid, owner_uid = make_user(db, "Oblivion", "owner")
                cmd_mid, cmd_uid = make_user(db, "Viper", "commander")
                mem_mid, mem_uid = make_user(db, "Rookie", "member")
                ins_mid, ins_uid = make_user(db, "Instructor", "instructor")
                ranks = list(db.scalars(select(Rank).order_by(Rank.level)).all())
                rank_low, rank_high = ranks[0], ranks[-1]
                # 手工授予的资质（用于验证"自动解析不会抹掉它"）
                q_manual = Qualification(name="手工-长机", category="role", level=9)
                db.add(q_manual)
                db.commit()
                q_manual_id = q_manual.id
                db.add(MemberQualification(member_id=mem_mid,
                                           qualification_id=q_manual_id,
                                           source="manual"))
                db.commit()

            # ---------------- 解析器本身 ----------------
            print("\n[0] 解析器：编解码与字段读取")
            with TestSession() as db:
                raw = build_lbk()
                check("造出的样本是 372 字节", len(raw) == 372, str(len(raw)))
                check("★ encode/decode 互为逆（不是同一个函数）",
                      LBP.decode(LBP.encode(raw)) == raw)
                check("★ encode 与 decode 确实不是同一个变换",
                      LBP.encode(raw) != LBP.decode(raw))
                rec = LBP.parse(raw)
                check("呼号解析正确（未被截断）",
                      rec.callsign == "Oblivion", repr(rec.callsign))
                check("姓名解析正确", rec.name == "Joe Pilot", repr(rec.name))
                check("中队解析正确", rec.squadron == "GFVFW", repr(rec.squadron))
                check("日期解析正确", rec.fields.get("date") == "09/22/26",
                      repr(rec.fields.get("date")))
                check("★ 飞行小时解析正确",
                      abs(rec.flight_hours - 296.29) < 0.01, str(rec.flight_hours))
                check("★ 军衔下标解析正确", rec.fields.get("rank_index") == 4)
                check("★ 军衔码映射正确", rec.rank_code == "Lieutenant colonel",
                      repr(rec.rank_code))
                check("★ 执行任务数（偏移 0x6a）解析正确",
                      rec.fields.get("missions_flown") == 161,
                      str(rec.fields.get("missions_flown")))
                check("★ 击落敌机数（偏移 0x76）解析正确",
                      rec.fields.get("aa_kills") == 0,
                      str(rec.fields.get("aa_kills")))
                check("ace_factor 解析正确",
                      abs(rec.fields.get("ace_factor") - 1.0) < 1e-6)

                # ---- 联队对照表（log.xlsx）落地：命名 / label / 旧名翻译 ----
                names = {s.name for s in LBP.FIELDS}
                renamed = set(LBP.LEGACY_FIELD_NAMES.values())
                check("★ 统计区 25 个字段已按对照表命名并全部确证",
                      not any(s.name.startswith(("counter_", "value_"))
                              for s in LBP.FIELDS)
                      and len(LBP.LEGACY_FIELD_NAMES) == 25
                      and all(s.certain for s in LBP.FIELDS
                              if s.name in renamed),
                      str(sorted(LBP.LEGACY_FIELD_NAMES)))
                check("★ 已确证字段都有可读 label",
                      all(s.label for s in LBP.FIELDS if s.certain),
                      str([s.name for s in LBP.FIELDS if s.certain and not s.label]))
                check("★ 旧名 → 新名映射的值都是现存字段名",
                      set(LBP.LEGACY_FIELD_NAMES.values()) <= names,
                      str(set(LBP.LEGACY_FIELD_NAMES.values()) - names))
                check("★ 旧名不再作为现行字段名出现",
                      not (set(LBP.LEGACY_FIELD_NAMES) & names),
                      str(set(LBP.LEGACY_FIELD_NAMES) & names))
                check("★ 统计字段不再进 uncertain（对照表确认前会进）",
                      not (renamed & set(rec.uncertain)),
                      str(rec.uncertain))
                check("★ 解析器版本已升到 2（对照表命名）",
                      LBP.PARSER_VERSION == "2", LBP.PARSER_VERSION)

                # 长文件名的边界：旧的 7 字节呼号长度把 Oblivion 截成了 Oblivio
                long_cs = build_lbk(callsign="Bartholomew")
                check("★ 长呼号不被截断",
                      LBP.parse(long_cs).callsign == "Bartholomew",
                      repr(LBP.parse(long_cs).callsign))

                for bad, label, kw in (
                        (raw[:371], "长度不对被拒", "372"),
                        (GARBAGE_LBK, "垃圾内容被哨兵校验拦下", "校验字")):
                    try:
                        LBP.parse(bad)
                        check(label, False, "竟然通过了")
                    except LBP.LbkError as exc:
                        check(label, kw in str(exc), str(exc))
                check("strict=False 时只警告不抛",
                      bool(LBP.parse(GARBAGE_LBK, strict=False).warnings))

            # ---------------- 服务层：存储校验 ----------------
            print("\n[1] 归档：校验与去重")
            with TestSession() as db:
                try:
                    LB.store_upload(db, mem_mid, "notes.txt",
                                    write_lbk(tdp, "notes.txt", b"hello world"),
                                    uploaded_by=mem_uid)
                    check("非 .lbk 扩展名被拒", False, "竟然通过了")
                except LB.LogbookError as exc:
                    check("非 .lbk 扩展名被拒", "扩展名" in str(exc), str(exc))
                db.rollback()

                try:
                    LB.store_upload(db, mem_mid, "empty.lbk",
                                    write_lbk(tdp, "empty.lbk", b""),
                                    uploaded_by=mem_uid)
                    check("空文件被拒", False, "竟然通过了")
                except LB.LogbookError as exc:
                    check("空文件被拒", "空" in str(exc), str(exc))
                db.rollback()

                big = write_lbk(tdp, "big.lbk", b"x" * (LB.MAX_LOGBOOK_BYTES + 1))
                try:
                    LB.store_upload(db, mem_mid, "big.lbk", big, uploaded_by=mem_uid)
                    check("超过上限被拒", False, "竟然通过了")
                except LB.LogbookError as exc:
                    check("超过上限被拒", "上限" in str(exc), str(exc))
                db.rollback()

                # ---- 正常文件：归档 + 自动解析 ----
                src = write_lbk(tdp)
                rec1, warn1, st1 = LB.store_upload(
                    db, mem_mid, "Oblivion.lbk", src, uploaded_by=mem_uid,
                    note="首次")
                db.commit()
                check("正常文件归档成功",
                      rec1.id and rec1.size_bytes == 372, str(rec1.size_bytes))
                check("★ 状态为 stored（新归档且解析成功）", st1 == "stored", st1)
                check("落盘路径在 storage/logbook 下",
                      rec1.stored_path.startswith("logbook/"), rec1.stored_path)
                check("原件确实写到了磁盘", LB.absolute_path(rec1).exists())
                check("★ 无解析错误", rec1.parse_error is None,
                      str(rec1.parse_error))
                check("★ 解析器版本已记录",
                      rec1.parser_version == LBP.PARSER_VERSION)
                check("★ 解析时刻已记录", rec1.parsed_at is not None)
                check("★ parsed_json 已保存", bool(rec1.parsed_json))
                check("提示里说明了自动填入了什么",
                      any("已自动填入名册" in w for w in warn1), str(warn1))

                rec2, warn2, st2 = LB.store_upload(db, mem_mid, "Oblivion.lbk",
                                                  src, uploaded_by=mem_uid)
                db.commit()
                check("同一文件重复上传→去重（同一记录）", rec2.id == rec1.id)
                check("★ 去重时状态为 duplicate", st2 == "duplicate", st2)
                check("去重时给出提示", bool(warn2))
                check("同一成员只有一条记录",
                      len(LB.list_for_member(db, mem_mid)) == 1)

                # ---- 解析失败：仍然归档，但如实报告 ----
                # 先记下解析失败**之前**的名册状态，才能证明这次上传没动它
                roster_before = (db.get(Member, mem_mid).rank_id,
                                 db.get(Member, mem_mid).logbook_hours_seconds,
                                 db.get(Member, mem_mid).logbook_sorties)
                odd = write_lbk(tdp, "odd.lbk", GARBAGE_LBK)
                rec3, warn3, st3 = LB.store_upload(db, mem_mid, "odd.lbk", odd,
                                                   uploaded_by=mem_uid)
                db.commit()
                check("★ 解析失败仍归档（原件保住）",
                      rec3.id is not None and rec3.size_bytes == 372)
                check("★ 状态标为 stored_unparsed（不谎报已入库）",
                      st3 == "stored_unparsed", st3)
                check("★ parse_error 已记录", bool(rec3.parse_error))
                check("★ 提示说明解析失败", any("解析失败" in w for w in warn3),
                      str(warn3))
                m_now = db.get(Member, mem_mid)
                check("★ 解析失败**不改动**名册（保住上一次成功解析的值）",
                      roster_before == (m_now.rank_id, m_now.logbook_hours_seconds,
                                        m_now.logbook_sorties),
                      "%s -> %s" % (roster_before,
                                    (m_now.rank_id, m_now.logbook_hours_seconds,
                                     m_now.logbook_sorties)))

                rec_owner, _, _ = LB.store_upload(db, owner_mid, "Oblivion.lbk",
                                                 src, uploaded_by=owner_uid)
                db.commit()
                check("★ 别的成员上传同一份文件会新建记录",
                      rec_owner.id != rec1.id)
                rec_owner_id = rec_owner.id
                rec1_id, rec3_id = rec1.id, rec3.id

            # ---------------- 服务层：自动写入名册 ----------------
            print("\n[2] ★ 自动解析 → 名册（没有手填、没有审核）")
            with TestSession() as db:
                rec = LB.get(db, rec1_id)
                m = db.get(Member, mem_mid)
                # build_lbk: rank_index=4 → RANKS[4] = Lieutenant colonel → level 5
                expect_rank = db.scalar(
                    select(Rank).where(Rank.level == 5))
                check("★ 军衔已自动写入名册", m.rank_id == expect_rank.id,
                      "rank_id=%s" % m.rank_id)
                check("★ 军衔与文件里的下标一致（4 → 第 5 级）",
                      expect_rank.level == 5 and expect_rank.name == "中校",
                      "%s/%s" % (expect_rank.level, expect_rank.name))
                check("军衔来源标为 logbook", m.rank_source == "logbook")
                check("军衔变更人已记录", m.rank_updated_by == mem_uid)
                check("★ 累计飞行时长已自动写入（296.29h）",
                      m.logbook_hours_seconds == int(round(296.29 * 3600)),
                      str(m.logbook_hours_seconds))
                check("★ 累计架次已自动写入（161，来自「执行任务数」0x6a）",
                      m.logbook_sorties == 161,
                      str(m.logbook_sorties))
                check("登记时间已记录", m.logbook_updated_at is not None)
                check("登记人已记录", m.logbook_updated_by == mem_uid)
                check("归档标为已写入名册", rec.is_confirmed)
                check("写入时刻已记录", rec.confirmed_at is not None)

                # ---- 回归：击落敌机数（0x76）绝不能再被当成架次 ----
                # 口径修正前（2026-09），架次误取 0x76；对照表确认它是击落数。
                # 这里两值故意拉开（50 对 999），用错任何一个偏移都会暴露。
                sep = build_lbk(callsign="Viper", hours=100.0,
                                missions=50, aa_kills=999)
                p = write_lbk(tdp, "sep.lbk", sep)
                LB.store_upload(db, cmd_mid, "sep.lbk", p, uploaded_by=cmd_uid)
                db.commit()
                check("★ 击落敌机数不再被当成累计架次（0x6a 才是）",
                      db.get(Member, cmd_mid).logbook_sorties == 50,
                      str(db.get(Member, cmd_mid).logbook_sorties))

                quals = {q.qualification_id: q for q in db.scalars(
                    select(MemberQualification)
                    .where(MemberQualification.member_id == mem_mid)).all()}
                check("★ 手工授予的资质**没有**被解析流程抹掉",
                      q_manual_id in quals and quals[q_manual_id].revoked_at is None,
                      "手工资质被撤销了")

            print("\n[3] 勋章：按文件字节同步，只增不撤")
            with TestSession() as db:
                medals = {0x8c: 2, 0x8e: 1, 0x8f: 8, 0x90: 2}
                raw = build_lbk(medals=medals, hours=300.0, missions=165)
                p = write_lbk(tdp, "medals.lbk", raw)
                rec, warn, st = LB.store_upload(db, owner_mid, "medals.lbk", p,
                                                uploaded_by=owner_uid)
                db.commit()
                check("带勋章的文件解析成功", st == "stored", st + str(warn))
                awards = {a.code: a for a in LB.list_awards(db, owner_mid)}
                check("★ 勋章已写入", len(awards) == 4, str(sorted(awards)))
                check("★ air_force_cross 值正确",
                      awards.get("air_force_cross") is not None
                      and awards["air_force_cross"].level == 2,
                      str(awards.get("air_force_cross")))
                check("★ korea_campaign 值正确（非布尔，可为 8）",
                      awards.get("korea_campaign") is not None
                      and awards["korea_campaign"].level == 8,
                      str(awards.get("korea_campaign")))
                check("勋章来源标为 logbook",
                      all(a.source == "logbook" for a in awards.values()))
                check("值为 0 的勋章不建记录",
                      "silver_star" not in awards, str(sorted(awards)))

                # 换一份"勋章变少"的文件：已获得的不撤销
                raw2 = build_lbk(medals={0x8e: 1}, hours=310.0, missions=170)
                p2 = write_lbk(tdp, "medals2.lbk", raw2)
                LB.store_upload(db, owner_mid, "medals2.lbk", p2,
                                uploaded_by=owner_uid)
                db.commit()
                awards2 = {a.code: a for a in LB.list_awards(db, owner_mid)}
                check("★ 文件里消失的勋章**不撤销**（换档不等于荣誉被收回）",
                      "air_force_cross" in awards2 and "korea_campaign" in awards2,
                      str(sorted(awards2)))

            print("\n[3b] 呼号不符时提示（不阻断，但必须说出来）")
            with TestSession() as db:
                # Oblivion 的成员目录里放"不是他的呼号"的文件
                other = build_lbk(callsign="SomeoneElse", hours=120.0,
                                  missions=44, rank_index=1)
                p = write_lbk(tdp, "wrong.lbk", other)
                rec_w, warn_w, st_w = LB.store_upload(
                    db, owner_mid, "wrong.lbk", p, uploaded_by=owner_uid)
                db.commit()
                check("呼号不符仍归档成功（只是提示，不拒绝）",
                      st_w == "stored", st_w)
                check("★ 提示里点名了文件里的呼号",
                      any("SomeoneElse" in w for w in warn_w), str(warn_w))
                check("★ 提示里给出了名册呼号",
                      any("Oblivion" in w for w in warn_w), str(warn_w))
                check("★ 提示同时保留了'已自动填入'的摘要",
                      any("已自动填入名册" in w for w in warn_w), str(warn_w))

                # 呼号一致时不该有多余提示
                same = build_lbk(callsign="Oblivion", hours=130.0, missions=48)
                p2 = write_lbk(tdp, "right.lbk", same)
                _, warn_r, _ = LB.store_upload(db, owner_mid, "right.lbk", p2,
                                               uploaded_by=owner_uid)
                db.commit()
                check("呼号一致时没有呼号提示",
                      not any("呼号" in w for w in warn_r), str(warn_r))

            print("\n[4] 重新解析：内容一致时如实报告无变化")
            with TestSession() as db:
                rec = LB.get(db, rec1_id)
                before = (db.get(Member, mem_mid).rank_id,
                          db.get(Member, mem_mid).logbook_hours_seconds,
                          db.get(Member, mem_mid).logbook_sorties)
                s = LB.reparse(db, rec, actor_user_id=mem_uid)
                db.commit()
                after = (db.get(Member, mem_mid).rank_id,
                         db.get(Member, mem_mid).logbook_hours_seconds,
                         db.get(Member, mem_mid).logbook_sorties)
                check("★ 同一份文件重解析 → no_change", s["no_change"],
                      str(s.get("changed")))
                check("重解析后名册值不变", before == after)

            print("\n[5] 重新解析：原件丢失 → 明确报错")
            with TestSession() as db:
                if os.name == "nt":
                    # 原件确实在磁盘上时，先确认正常路径可用
                    rec = LB.get(db, rec1_id)
                    check("原件存在时 reparse 不报错",
                          LB.reparse(db, rec, actor_user_id=mem_uid) is not None)
                rec = LB.get(db, rec3_id)
                missing = LB.absolute_path(rec)
                if missing.exists():
                    missing.unlink()
                try:
                    LB.reparse(db, rec, actor_user_id=mem_uid)
                    check("原件缺失时 reparse 报错", False, "竟然通过了")
                except LB.LogbookError as exc:
                    check("原件缺失时 reparse 报错", "原件" in str(exc), str(exc))
                db.rollback()

            # ---------------- HTTP 层 ----------------
            print("\n[6] 页面与权限（HTTP）")
            with TestClient(app) as client:
                r = client.get("/account/logbook", follow_redirects=False)
                check("匿名访问 → 跳登录", r.status_code == 303,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                check("普通成员登录", login(client, "rookie"))
                r = client.get("/account/logbook")
                check("成员可打开自己的 Logbook 页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("★ 页面说明上传即自动解析", "上传即自动解析" in r.text)
                check("★ 页面明说不需要手动输入", "不需要手动输入" in r.text
                      or "无需手动输入" in r.text)
                check("★ 页面说明不需要审核", "不需要审核" in r.text)
                check("页面没有「待确认」字样", "待确认" not in r.text)
                check("页面没有「确认写入名册」按钮", "确认写入名册" not in r.text)
                check("★ 页面没有手填数值的输入框",
                      'name="hours"' not in r.text and 'name="sorties"' not in r.text
                      and 'name="rank_id"' not in r.text)
                check("成员能看到上传表单", 'name="file"' in r.text)

                # 解析结果必须展示出来
                check("★ 页面展示解析出的字段表", "从文件里读到的内容" in r.text)
                check("★ 页面展示飞行小时", "296.29" in r.text
                      or "flight_hours" in r.text)
                check("★ 页面展示勋章区", "勋章" in r.text)
                check("★ 页面展示战果统计分组（联队对照表命名）",
                      "战果统计" in r.text and "狗斗记录" in r.text
                      and "空战战果" in r.text and "对地与海上战果" in r.text,
                      "缺少分组标题")
                check("★ 战果统计展示执行任务数与击落敌机数",
                      "执行任务数" in r.text and "击落敌机数" in r.text)
                check("★ 架次来源标注为「执行任务数」",
                      "取自文件里的「执行任务数」" in r.text)
                check("★ 未确认的数值折叠且标注含义未确认",
                      "含义未确认" in r.text and "<details" in r.text)
                check("★ 明确说明推断字段不写入名册",
                      "不写入名册" in r.text or "未写入名册" in r.text)

                # 三个时长必须分别标注
                check("页面区分「Logbook 累计时长」", "Logbook 累计时长" in r.text)
                check("页面区分「日志时长」", "日志时长" in r.text)
                check("页面区分「记录时长」", "记录时长" in r.text)

                r = client.get("/members/%s/logbook" % owner_mid,
                               follow_redirects=False)
                check("★ 成员不能看别人的 Logbook 页（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # rec_owner 属于 Oblivion，Rookie 不是本人 → 必须 403
                r = client.get("/logbook/%s/download" % rec_owner_id,
                               follow_redirects=False)
                check("★ 成员不能下载别人的原件（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.get("/logbook/%s/download" % rec1_id)
                check("成员能下载自己的原件", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("★ 下载内容与上传的密文逐字节一致",
                      r.content == build_lbk())

            with TestClient(app) as client:
                check("指挥登录", login(client, "viper"))
                r = client.get("/members/%s/logbook" % mem_mid)
                check("指挥能打开成员 Logbook 页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                r = client.get("/logbook/%s/download" % rec1_id)
                check("指挥能下载成员的原件", r.status_code == 200,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                check("教官登录", login(client, "instructor"))
                r = client.get("/members/%s/logbook" % mem_mid)
                check("★ 教官可代他人管理（有 logbook.upload.any）",
                      r.status_code == 200, "得到 %d" % r.status_code)

            print("\n[7] CSRF 与权限边界（构造请求）")
            with TestClient(app) as client:
                login(client, "rookie")
                r = client.post("/account/logbook/upload",
                                files={"file": ("x.lbk", build_lbk())},
                                data={"csrf_token": ""})
                check("无 CSRF 上传被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/logbook/%s/reparse" % rec1_id,
                                data={"csrf_token": ""})
                check("★ 无 CSRF 重新解析被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                r = client.post("/members/%s/logbook/upload" % owner_mid,
                                files={"file": ("x.lbk", build_lbk())},
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 成员不能代他人上传（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # 成员也不能动**别人的**归档
                r = client.post("/logbook/%s/reparse" % rec_owner_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 成员不能重新解析别人的归档（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/logbook/%s/delete" % rec_owner_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 成员不能删除别人的归档（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            print("\n[8] 上传 → 自动入库，走完整 HTTP 流程")
            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                # 一份"新数据"：小时与架次都比当前名册大
                new_raw = build_lbk(hours=350.5, rank_index=5, missions=190, aa_kills=230,
                                    medals={0x8e: 3})
                r = client.post("/account/logbook/upload",
                                files={"file": ("Rookie.lbk", new_raw)},
                                data={"csrf_token": tok, "note": "自检用"},
                                follow_redirects=False)
                check("上传成功 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                loc = r.headers.get("location", "")
                check("★ 回跳带 did=uploaded（解析成功）",
                      "did=uploaded" in loc, loc)
                check("★ 回跳带回解析摘要 warning", "warning=" in loc, loc)

                with TestSession() as db:
                    m = db.get(Member, mem_mid)
                    check("★ 名册累计时长已按新文件更新",
                          m.logbook_hours_seconds == int(round(350.5 * 3600)),
                          str(m.logbook_hours_seconds))
                    check("★ 名册累计架次已更新", m.logbook_sorties == 190,
                          str(m.logbook_sorties))
                    check("★ 击落敌机数（0x76=230）没有混进架次",
                          m.logbook_sorties != 230)
                    r5 = db.scalar(select(Rank).where(Rank.level == 6))
                    check("★ 名册军衔已晋升到 6 级（index 5）",
                          m.rank_id == r5.id, str(m.rank_id))

                # 页面必须把"自动填入了什么"显示出来 —— 跟着 303 走一遍，
                # 否则测不到 flash（提示是靠重定向的 query 参数带回来的）
                page = client.get(loc)
                check("★ 页面上能看到自动填入的摘要",
                      "已自动填入名册" in page.text)
                # 这份文件的呼号是 Oblivion，而成员是 Rookie → 必须有呼号提示，
                # 且**不能**因为多了这条提示就把"已自动填入"挤掉
                check("★ 页面同时给出呼号不符提示", "与名册呼号" in page.text)
                check("★ 页面显示更新后的时长",
                      "350.5" in page.text or "350" in page.text)
                check("★ 页面显示勋章区", "勋章" in page.text)

                # 解析失败的文件 → did=uploaded_unparsed，且不能谎报已入库
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                bad_raw = bytes(range(200)) + b"\x00" * 172
                r = client.post("/account/logbook/upload",
                                files={"file": ("Broken.lbk", bad_raw)},
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 解析失败的上传仍 303（归档成功）", r.status_code == 303,
                      "得到 %d" % r.status_code)
                loc = r.headers.get("location", "")
                check("★ 回跳带 did=uploaded_unparsed（不谎报已入库）",
                      "did=uploaded_unparsed" in loc, loc)

                page = client.get("/account/logbook")
                check("★ 页面显示「解析失败」面板", "解析失败" in page.text)
                check("★ 页面上的失败提示不含「已自动解析写入名册」的假话",
                      page.text.count("已自动解析写入名册") == 0)

            print("\n[9] 删除后可以重新上传同一个文件")
            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                with TestSession() as db:
                    newest = db.scalar(
                        select(LogbookFile)
                        .where(LogbookFile.member_id == mem_mid)
                        .order_by(LogbookFile.created_at.desc()))
                    new_id = newest.id
                    stored = LB.absolute_path(newest)
                r = client.post("/logbook/%s/delete" % new_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("删除 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    check("记录已删除", LB.get(db, new_id) is None)
                check("磁盘原件已清理", not stored.exists())

            print("\n[10] 未绑定名册的账号")
            with TestSession() as db:
                db.add(User(username="opsadmin",
                            password_hash=hash_password(PW), status="active",
                            member_id=None))
                db.commit()
            with TestClient(app) as client:
                check("管理员登录", login(client, "opsadmin"))
                r = client.get("/account/logbook")
                check("未绑名册→专用说明页 200", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("说明页解释这是正常状态", "没有绑定名册成员" in r.text)

            print("\n[11] 审计留痕")
            with TestSession() as db:
                actions = list(db.scalars(select(AuditLog.action)).all())
                for act in ("logbook.upload", "logbook.delete"):
                    check("审计含 %s" % act, act in actions)
                for gone in ("logbook.confirm", "logbook.apply", "logbook.declare"):
                    check("★ 已移除的动作 %s 不再出现" % gone, gone not in actions)
                up = db.scalar(select(AuditLog).where(
                    AuditLog.action == "logbook.upload"))
                check("上传审计记录了文件信息",
                      up is not None and bool(up.after_json))
                check("上传审计记录了操作者",
                      up is not None and up.actor_user_id is not None)

            print("\n[12] 时长口径：三个量必须分别标注，不得混用")
            with TestSession() as db:
                from gfvfw.services.stats import (
                    mission_flight_seconds, mission_recording_seconds,
                )
                check("可调用 mission_flight_seconds（日志时长）",
                      callable(mission_flight_seconds))
                check("可调用 mission_recording_seconds（记录时长）",
                      callable(mission_recording_seconds))
            with TestClient(app) as client:
                login(client, "rookie")
                r = client.get("/account/logbook")
                check("Logbook 页三个时长同框出现",
                      all(k in r.text for k in ("Logbook 累计时长", "日志时长", "记录时长")))
                check("明确提示不要互相校验", "不要互相校验" in r.text)

            print("\n[13] 勋章表数据完整性")
            with TestSession() as db:
                rows = list(db.scalars(select(MemberAward)).all())
                check("勋章记录存在", bool(rows), str(len(rows)))
                check("勋章 level 都是非负整数",
                      all(isinstance(r.level, int) and r.level >= 0 for r in rows))
                check("勋章 code 唯一（每人每枚一条）",
                      len({(r.member_id, r.code) for r in rows}) == len(rows))
                check("所有勋章都有可读名称",
                      all(r.name for r in rows))

            print("\n[14] 成员详情页内嵌 Logbook 数据（纯数据，无偏移无注释）")
            with TestClient(app) as client:
                check("普通成员登录", login(client, "rookie"))
                r = client.get("/members/%s" % mem_mid)
                check("成员详情页 200", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("★ 详情页直接展示「Logbook 数据」面板（无需进管理页）",
                      "Logbook 数据" in r.text, "缺少面板标题")
                check("★ 展示累计架次（[8] 上传的 190）",
                      "累计架次" in r.text and "190" in r.text)
                check("★ 展示战果统计四个分组",
                      all(k in r.text for k in
                          ("战役与任务", "空战战果", "对地与海上战果", "狗斗记录")),
                      "缺少分组标题")
                check("★ 展示执行任务数与击落敌机数",
                      "执行任务数" in r.text and "击落敌机数" in r.text)
                check("★ 击落敌机数 230（0x76）作为数据上屏",
                      "230" in r.text)
                check("★ 勋章直接展示（Air Medal ×3）", "Air Medal" in r.text)
                check("★ 不显示文件偏移", "0x" not in r.text)
                check("★ 不显示「含义未确认」/「推断」/「待核对」等注释",
                      "含义未确认" not in r.text and "推断" not in r.text
                      and "待核对" not in r.text)
                check("★ 档案里旧的「Logbook 累计」行已并入面板",
                      "Logbook 累计" not in r.text)
                # 没有归档的成员不显示面板（不能渲染出一个空壳）
                # （owner 也有解析成功的归档；教官成员从未上传过 → 用他验证）
                r = client.get("/members/%s" % ins_mid)
                check("★ 无解析数据的成员不显示该面板",
                      "Logbook 数据" not in r.text)

        finally:
            import gfvfw.config as _c
            import gfvfw.db as _d
            import gfvfw.web.deps as _p
            _a = sys.modules["gfvfw.web.app"]
            _d.SessionLocal, _p.SessionLocal, _a.SessionLocal = orig
            _c.settings.storage_dir = orig_storage

    print("\n" + "=" * 74)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 74)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
