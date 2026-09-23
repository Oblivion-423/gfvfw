"""
自校验：**BMS Logbook 上传与名册同步**。

覆盖需求 §4.7/§4.8/§8.1 与 R12 的落地方式：

* 成员可上传**自己的** ``.lbk``（归档、SHA256 去重、可下载、可删除重传）
* 成员填写的声明值**不直接影响名册** —— 否则任何成员都能自封飞行时数
* 只有具备 ``member.rank.edit`` 的人（指挥/管理员）能**确认写入名册**
* 确认时资质**只同步 ``source='logbook'`` 的那一批**，
  手动授予的资质不会被一次确认抹掉
* 权限边界在服务端强制（无权限者构造请求得 403），页面上只是隐藏按钮

运行:
    .venv\\Scripts\\python.exe tests\\logbook_selfcheck.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from gfvfw.db import Base  # noqa: E402
from gfvfw.models import (  # noqa: E402
    AuditLog, LogbookFile, Member, MemberQualification, MemberRole, Qualification,
    Rank, Role, User,
)
from gfvfw.security import hash_password  # noqa: E402
from gfvfw.services import logbook as LB  # noqa: E402
from gfvfw.services.bootstrap import seed  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

PW = "password123"

#: 一份"看起来像"的 logbook：实测真实文件都是 372 字节定长
FAKE_LBK = bytes(range(256)) + bytes(116)


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
              data: bytes = FAKE_LBK) -> Path:
    p = tmpdir / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 74)
    print("BMS Logbook 上传与名册同步自校验")
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
                # 手工授予的资质（用于验证"确认不会抹掉它"）
                q_manual = Qualification(name="手工-长机", category="role", level=9)
                q_log = Qualification(name="Logbook-僚机", category="role", level=5)
                db.add_all([q_manual, q_log])
                db.commit()
                q_manual_id, q_log_id = q_manual.id, q_log.id
                db.add(MemberQualification(member_id=mem_mid,
                                           qualification_id=q_manual_id,
                                           source="manual"))
                db.commit()

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

                src = write_lbk(tdp)
                rec1, warn1 = LB.store_upload(db, mem_mid, "Oblivion.lbk", src,
                                              uploaded_by=mem_uid, note="首次")
                db.commit()
                check("正常文件归档成功", rec1.id and rec1.size_bytes == len(FAKE_LBK))
                check("372 字节不产生体积警告", not warn1, str(warn1))
                check("落盘路径在 storage/logbook 下",
                      rec1.stored_path.startswith("logbook/"), rec1.stored_path)
                check("原件确实写到了磁盘", LB.absolute_path(rec1).exists())

                # 同一成员重复上传完全相同的文件 → 去重
                rec2, warn2 = LB.store_upload(db, mem_mid, "Oblivion.lbk", src,
                                              uploaded_by=mem_uid)
                db.commit()
                check("同一文件重复上传→去重（同一记录）", rec2.id == rec1.id)
                check("去重时给出提示", bool(warn2))
                check("同一成员只有一条记录",
                      len(LB.list_for_member(db, mem_mid)) == 1)

                # 体积异常 → 警告但不拒绝
                odd = write_lbk(tdp, "odd.lbk", b"y" * 1000)
                rec3, warn3 = LB.store_upload(db, mem_mid, "odd.lbk", odd,
                                              uploaded_by=mem_uid)
                db.commit()
                check("体积异常仍归档（只是警告）", rec3.id and bool(warn3))
                check("不同内容→新增一条记录",
                      len(LB.list_for_member(db, mem_mid)) == 2)

                # 另一个成员可以上传**同样内容**的文件（唯一约束是按成员的）
                rec_other, _ = LB.store_upload(db, mem_mid, "dup.lbk", src,
                                               uploaded_by=mem_uid)
                db.commit()
                check("跨成员不互相去重（约束是 member_id+sha256）",
                      rec_other.id == rec1.id)
                rec_owner, _ = LB.store_upload(db, owner_mid, "Oblivion.lbk", src,
                                               uploaded_by=owner_uid)
                db.commit()
                check("★ 别的成员上传同一份文件会新建记录",
                      rec_owner.id != rec1.id)
                rec_owner_id = rec_owner.id

            # ---------------- 服务层：声明与确认 ----------------
            print("\n[2] 声明值：校验")
            with TestSession() as db:
                rec = LB.get(db, rec1.id)
                try:
                    LB.declare(db, rec, rank_id="not-a-real-rank")
                    check("非法军衔被拒", False, "竟然通过了")
                except LB.LogbookError:
                    check("非法军衔被拒", True)
                db.rollback()

                rec = LB.get(db, rec1.id)
                try:
                    LB.declare(db, rec, hours_seconds=-1)
                    check("负时长被拒", False, "竟然通过了")
                except LB.LogbookError:
                    check("负时长被拒", True)
                db.rollback()

                rec = LB.get(db, rec1.id)
                try:
                    LB.declare(db, rec, qualification_ids=["bogus-id"])
                    check("非法资质被拒", False, "竟然通过了")
                except LB.LogbookError:
                    check("非法资质被拒", True)
                db.rollback()

                rec = LB.get(db, rec1.id)
                check("尚未声明时 has_declaration 为假", not rec.has_declaration())
                LB.declare(db, rec, rank_id=rank_high.id, hours_seconds=3600 * 123,
                           sortie_count=456, qualification_ids=[q_log_id])
                db.commit()
                rec = LB.get(db, rec1.id)
                check("声明后 has_declaration 为真", rec.has_declaration())
                check("声明了军衔", rec.declared_rank_id == rank_high.id)
                check("声明了时长", rec.declared_hours_seconds == 3600 * 123)
                check("声明了架次", rec.declared_sorties == 456)
                check("声明了资质", rec.declared_qualification_ids() == [q_log_id])
                check("0 小时是合法值（与「未填」不同）",
                      LB.declare(db, rec, hours_seconds=0) is None
                      and rec.declared_hours_seconds == 0)
                db.rollback()

            print("\n[3] ★ 声明值不直接影响名册")
            with TestSession() as db:
                m = db.get(Member, mem_mid)
                check("声明后名册军衔仍未变", m.rank_id is None,
                      "rank_id=%s" % m.rank_id)
                check("声明后名册累计时长仍未变",
                      m.logbook_hours_seconds is None)
                check("声明后名册累计架次仍未变", m.logbook_sorties is None)
                check("待确认列表里能看到这份归档",
                      [r.id for r in LB.pending_for_member(db, mem_mid)] == [rec1.id])
                check("全站待确认计数 >= 1", LB.pending_count(db) >= 1)

            print("\n[4] ★ 确认写入名册（只同步 logbook 来源的资质）")
            with TestSession() as db:
                rec = LB.get(db, rec1.id)
                LB.declare(db, rec, rank_id=rank_high.id,
                           hours_seconds=3600 * 123, sortie_count=456,
                           qualification_ids=[q_log_id])
                db.commit()
                rec = LB.get(db, rec1.id)
                summary = LB.confirm_logbook(db, rec, actor_user_id=cmd_uid,
                                             actor_role="commander")
                db.commit()

                m = db.get(Member, mem_mid)
                check("军衔已写入", m.rank_id == rank_high.id)
                check("军衔来源标为 logbook", m.rank_source == "logbook")
                check("军衔变更人已记录", m.rank_updated_by == cmd_uid)
                check("累计时长已写入", m.logbook_hours_seconds == 3600 * 123)
                check("累计架次已写入", m.logbook_sorties == 456)
                check("登记时间已记录", m.logbook_updated_at is not None)
                check("确认时间已记录", rec.confirmed_at is not None)
                check("确认人已记录", rec.confirmed_by == cmd_uid)
                check("变更摘要含军衔", "军衔" in summary["changed"], str(summary))
                check("变更摘要含累计飞行时长", "累计飞行时长" in summary["changed"])

                quals = {q.qualification_id: q for q in db.scalars(
                    select(MemberQualification)
                    .where(MemberQualification.member_id == mem_mid)).all()}
                check("logbook 资质已写入", q_log_id in quals)
                check("logbook 资质来源正确",
                      quals[q_log_id].source == "logbook")
                check("★ 手工授予的资质**没有**被抹掉",
                      q_manual_id in quals and quals[q_manual_id].revoked_at is None,
                      "手工资质被撤销了")

                check("确认后不再出现在待确认列表",
                      rec1.id not in [r.id for r in LB.pending_for_member(db, mem_mid)])

            print("\n[5] 确认：声明里被移除的 logbook 资质会被撤销")
            with TestSession() as db:
                rec = LB.get(db, rec1.id)
                LB.declare(db, rec, rank_id=rank_high.id,
                           hours_seconds=3600 * 123, sortie_count=456,
                           qualification_ids=[])       # 取消勾选
                db.commit()
                rec = LB.get(db, rec1.id)
                LB.confirm_logbook(db, rec, actor_user_id=cmd_uid)
                db.commit()
                quals = {q.qualification_id: q for q in db.scalars(
                    select(MemberQualification)
                    .where(MemberQualification.member_id == mem_mid)).all()}
                check("取消勾选后 logbook 资质被撤销",
                      quals[q_log_id].revoked_at is not None)
                check("★ 手工资质依然完好",
                      quals[q_manual_id].revoked_at is None)

            print("\n[6] 无变化时如实报告")
            with TestSession() as db:
                rec = LB.get(db, rec1.id)
                s = LB.confirm_logbook(db, rec, actor_user_id=cmd_uid)
                db.commit()
                check("重复确认报告 no_change", s["no_change"], str(s["changed"]))

            # ---------------- HTTP 层 ----------------
            #
            # ⚠️ 顺序很重要：第 4~6 组已经把 rec1 确认掉了，此时**没有待确认项**，
            #    页面上不会出现"待确认/无权限/确认按钮"那一段。
            #    所以先造一份**待确认**的归档，再验页面与权限。
            print("\n[7] 准备一份待确认归档（供下面的页面与权限检查使用）")
            with TestSession() as db:
                pending_src = write_lbk(tdp, "Rookie.lbk", bytes(range(128)) + b"\x07" * 244)
                rec_pending, _ = LB.store_upload(db, mem_mid, "Rookie.lbk",
                                                 pending_src, uploaded_by=mem_uid)
                LB.declare(db, rec_pending, rank_id=rank_low.id,
                           hours_seconds=3600 * 9, sortie_count=3,
                           qualification_ids=[q_log_id])
                db.commit()
                pending_id = rec_pending.id
                check("已造出一份待确认归档",
                      rec_pending.id in [r.id for r in LB.pending_for_member(db, mem_mid)])

            print("\n[8] 页面与权限（HTTP）")
            with TestClient(app) as client:
                r = client.get("/account/logbook", follow_redirects=False)
                check("匿名访问 → 跳登录", r.status_code == 303, "得到 %d" % r.status_code)

            with TestClient(app) as client:
                check("普通成员登录", login(client, "rookie"))
                r = client.get("/account/logbook")
                check("成员可打开自己的 Logbook 页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("页面说明不会自动解析", "不会自动读取" in r.text)
                check("页面说明需要确认才写入名册", "确认" in r.text)
                check("成员能看到上传表单", 'name="file"' in r.text)
                check("★ 成员看不到确认按钮（无 member.rank.edit）",
                      "确认写入名册" not in r.text)
                check("页面显示待确认但标注无权限",
                      "无此权限" in r.text or "需要「军衔/资质编辑」权限" in r.text)

                r = client.get("/members/%s/logbook" % owner_mid,
                               follow_redirects=False)
                check("★ 成员不能看别人的 Logbook 页（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

                # rec_owner_id 属于 Oblivion，Rookie 不是本人 → 必须 403
                r = client.get("/logbook/%s/download" % rec_owner_id,
                               follow_redirects=False)
                check("★ 成员不能下载别人的原件（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.get("/logbook/%s/download" % rec_pending.id)
                check("成员能下载自己的原件", r.status_code == 200,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                check("指挥登录", login(client, "viper"))
                r = client.get("/members/%s/logbook" % mem_mid)
                check("指挥能打开成员 Logbook 页", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("★ 指挥能看到确认按钮", "确认写入名册" in r.text)
                check("指挥能看到待确认的声明值", "待确认的声明" in r.text)

                r = client.get("/logbook/%s/download" % rec_pending.id)
                check("指挥能下载成员的原件", r.status_code == 200,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                check("教官登录", login(client, "instructor"))
                r = client.get("/members/%s/logbook" % mem_mid)
                check("★ 教官可代他人管理（有 logbook.upload.any）",
                      r.status_code == 200, "得到 %d" % r.status_code)
                check("★ 教官看不到确认按钮（无 member.rank.edit）",
                      "确认写入名册" not in r.text)

            print("\n[9] ★ 指挥通过 HTTP 确认 → 审计必须留改前/改后值")
            with TestSession() as db:
                before_rank = db.get(Member, mem_mid).rank_id

            with TestClient(app) as client:
                login(client, "viper")
                page = client.get("/members/%s/logbook" % mem_mid)
                tok = csrf_of(page.text)
                r = client.post("/logbook/%s/confirm" % pending_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("指挥确认 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("回跳带 did=confirmed",
                      "did=confirmed" in r.headers.get("location", ""))

            with TestSession() as db:
                m = db.get(Member, mem_mid)
                check("★ 确认后名册军衔变为声明值", m.rank_id == rank_low.id)
                check("确认后累计架次变为声明值", m.logbook_sorties == 3)
                entry = db.scalar(select(AuditLog).where(
                    AuditLog.action == "logbook.confirm"))
                check("审计含 logbook.confirm", entry is not None)
                check("确认审计记录了改前值",
                      entry is not None and before_rank is not None
                      and str(before_rank) in (entry.before_json or ""),
                      "before_json=%s" % (entry.before_json if entry else None))
                check("确认审计记录了改后值",
                      entry is not None and str(rank_low.id) in (entry.after_json or ""))
                check("确认审计记录了确认人",
                      entry is not None and entry.actor_user_id == cmd_uid)

            print("\n[10] CSRF 与权限边界（构造请求）")
            with TestClient(app) as client:
                login(client, "rookie")
                r = client.post("/account/logbook/upload",
                                files={"file": ("x.lbk", FAKE_LBK)},
                                data={"csrf_token": ""})
                check("无 CSRF 上传被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)
                r = client.post("/logbook/%s/confirm" % pending_id,
                                data={"csrf_token": ""})
                check("★ 无 CSRF 确认被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                r = client.post("/logbook/%s/confirm" % pending_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 成员带 CSRF 确认也被拒（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                r = client.post("/members/%s/logbook/upload" % owner_mid,
                                files={"file": ("x.lbk", FAKE_LBK)},
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 成员不能代他人上传（403）", r.status_code == 403,
                      "得到 %d" % r.status_code)

            print("\n[11] 上传 → 声明 走完整 HTTP 流程")
            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                payload = bytes(range(200)) + b"\x00" * 172      # 另一份不同内容
                r = client.post("/account/logbook/upload",
                                files={"file": ("Rookie.lbk", payload)},
                                data={"csrf_token": tok, "note": "自检用"},
                                follow_redirects=False)
                check("上传成功 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                check("回跳带 did=uploaded", "did=uploaded" in r.headers.get("location", ""))

                page = client.get("/account/logbook")
                check("新归档出现在页面上", "Rookie.lbk" in page.text)
                tok = csrf_of(page.text)
                with TestSession() as db:
                    new_rec = db.scalar(
                        select(LogbookFile)
                        .where(LogbookFile.member_id == mem_mid)
                        .order_by(LogbookFile.created_at.desc()))
                    new_id = new_rec.id
                    # 用"声明前后的快照"来验不变量，不要硬编码某个数字 ——
                    # 前面的确认步骤已经合法地改过名册值。
                    _m = db.get(Member, mem_mid)
                    roster_before = (_m.rank_id, _m.logbook_hours_seconds,
                                     _m.logbook_sorties)

                r = client.post("/logbook/%s/declare" % new_id,
                                data={"csrf_token": tok, "hours": "42.5",
                                      "sorties": "7",
                                      "qualification_ids": [q_log_id]},
                                follow_redirects=False)
                check("保存声明值 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    rec = db.get(LogbookFile, new_id)
                    check("小时换算为秒正确（42.5h）",
                          rec.declared_hours_seconds == int(42.5 * 3600),
                          "得到 %s" % rec.declared_hours_seconds)
                    check("架次已存", rec.declared_sorties == 7)
                    m = db.get(Member, mem_mid)
                    roster_after = (m.rank_id, m.logbook_hours_seconds,
                                    m.logbook_sorties)
                    check("★ 声明前后名册记录完全不变（声明 ≠ 确认）",
                          roster_before == roster_after,
                          "%s -> %s" % (roster_before, roster_after))
                    check("★ 该归档仍是待确认状态", not rec.is_confirmed)

                r = client.post("/logbook/%s/declare" % new_id,
                                data={"csrf_token": tok, "hours": "abc"},
                                follow_redirects=False)
                check("非数字小时被拒并回显错误",
                      "error=" in r.headers.get("location", ""))

            print("\n[12] 删除后可以重新上传同一个文件")
            with TestClient(app) as client:
                login(client, "rookie")
                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                with TestSession() as db:
                    target = LB.get(db, new_id)
                    stored = LB.absolute_path(target)
                r = client.post("/logbook/%s/delete" % new_id,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("删除 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                with TestSession() as db:
                    check("记录已删除", LB.get(db, new_id) is None)
                check("磁盘原件已清理", not stored.exists())

                page = client.get("/account/logbook")
                tok = csrf_of(page.text)
                r = client.post("/account/logbook/upload",
                                files={"file": ("Rookie.lbk", payload)},
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 同一文件删除后可重新上传（唯一约束没挡住）",
                      r.status_code == 303, "得到 %d" % r.status_code)
                with TestSession() as db:
                    check("重新上传确实新建了记录",
                          len([f for f in LB.list_for_member(db, mem_mid)]) >= 2)

            print("\n[13] 未绑定名册的账号")
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

            print("\n[14] 审计留痕")
            with TestSession() as db:
                actions = [a for a in db.scalars(select(AuditLog.action)).all()]
                for act in ("logbook.upload", "logbook.declare",
                            "logbook.confirm", "logbook.delete"):
                    check("审计含 %s" % act, act in actions)
                conf = db.scalar(select(AuditLog).where(
                    AuditLog.action == "logbook.confirm"))
                check("确认审计记录了改前值",
                      conf is not None and bool(conf.before_json))
                check("确认审计记录了改后值",
                      conf is not None and bool(conf.after_json))

            print("\n[15] 页面文案不得退化为「能自动解析」的承诺")
            with TestClient(app) as client:
                login(client, "rookie")
                r = client.get("/account/logbook")
                for bad in ("自动导入", "自动读取军衔"):
                    check("不出现误导性承诺 %r" % bad, bad not in r.text)
                check("明确说明口径不同（Logbook vs ACMI）",
                      "口径不同" in r.text or "口径不同，不要互相校验" in r.text)
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
