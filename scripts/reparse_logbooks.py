"""
回填历史 Logbook 归档：用**已归档的原件**重新解析并同步名册。

为什么需要
----------
``logbook_files`` 里可能存着**在自动解析上线之前**归档的文件 —— 那时
``.lbk`` 格式还没解出，系统只归档原件、由成员手填数值（甚至什么都没填）：

* ``parser_version`` / ``parsed_at`` / ``parsed_json`` 为空
* ``members.logbook_hours_seconds`` / ``logbook_sorties`` / 勋章为空

格式解出之后，这些归档不必让成员重传 —— 原件都在 ``var/storage/logbook/`` 里，
直接重跑解析即可。本脚本就是干这件事的。

它与页面上的「重新解析」按钮**做同一件事**（都走
:func:`gfvfw.services.logbook.reparse`），区别只是批量处理所有归档、
并且会写审计。解析失败的文件**不会被删**，只在 ``parse_error`` 里记下原因。

用法
----
    # 先看会改什么（默认只读，不写库）
    .venv\\Scripts\\python.exe scripts\\reparse_logbooks.py

    # 确认后写入
    .venv\\Scripts\\python.exe scripts\\reparse_logbooks.py --apply

    # 只处理某一个成员（呼号）
    .venv\\Scripts\\python.exe scripts\\reparse_logbooks.py --apply --callsign Oblivion
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from gfvfw.db import SessionLocal  # noqa: E402
from gfvfw.models import LogbookFile, Member  # noqa: E402
from gfvfw.services import logbook as LB  # noqa: E402
from gfvfw.services.audit import record_audit  # noqa: E402


def _fmt_hours(seconds) -> str:
    if seconds is None:
        return "—"
    return "%.2f h" % (seconds / 3600.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="回填历史 Logbook 归档")
    ap.add_argument("--apply", action="store_true",
                    help="真正写入数据库（默认只读预览）")
    ap.add_argument("--callsign", default=None,
                    help="只处理指定呼号的成员")
    ap.add_argument("--actor", default=None,
                    help="审计里的操作者账号名（默认取第一个 owner）")
    args = ap.parse_args()

    with SessionLocal() as db:
        stmt = select(LogbookFile).order_by(LogbookFile.created_at)
        files = list(db.scalars(stmt).all())
        if args.callsign:
            keep = []
            for f in files:
                m = db.get(Member, f.member_id)
                if m is not None and m.callsign.lower() == args.callsign.lower():
                    keep.append(f)
            files = keep

        if not files:
            print("没有需要处理的 Logbook 归档。")
            return 0

        # 审计需要一个真实的 user id（外键）；找不到就让 record_audit 记 NULL
        actor_user_id = None
        if args.actor:
            from gfvfw.models import User
            u = db.scalar(select(User).where(User.username == args.actor))
            if u is None:
                print("找不到账号 %s" % args.actor, file=sys.stderr)
                return 2
            actor_user_id = u.id
        else:
            from gfvfw.models import User
            u = db.scalar(select(User).order_by(User.created_at).limit(1))
            actor_user_id = u.id if u is not None else None

        print("=" * 74)
        print("Logbook 归档重解析（%s）" % ("写入" if args.apply else "只读预览"))
        print("=" * 74)

        n_ok = n_fail = n_skip = 0
        for rec in files:
            member = db.get(Member, rec.member_id)
            who = member.callsign if member else rec.member_id[:8]
            path = LB.absolute_path(rec)
            print("\n[%s] %s  (%s)" % (who, rec.original_filename,
                                       rec.created_at))
            if not path.exists():
                # 原件丢了就没法重解析 —— 明确说出来，不要静默跳过
                print("  ⚠️ 原件不在服务器上：%s —— 跳过" % path)
                n_skip += 1
                continue

            changed_before = None
            if member is not None:
                changed_before = (member.rank_id, member.logbook_hours_seconds,
                                  member.logbook_sorties)
                print("  改前：军衔=%s 累计=%-10s 架次=%s" % (
                    member.rank.name if member.rank_id else "—",
                    _fmt_hours(member.logbook_hours_seconds),
                    member.logbook_sorties))

            if not args.apply:
                # 只读预览也要走一次解析（在内存里），但不提交
                from gfvfw import lbk_parser as LBP
                try:
                    parsed = LBP.parse(path.read_bytes())
                except LBP.LbkError as exc:
                    print("  ✗ 解析失败：%s" % exc)
                    n_fail += 1
                    continue
                print("  改后（预览）：军衔=%s 累计=%-10s 架次=%s 勋章=%s" % (
                    parsed.rank_code or "—",
                    "%.2f h" % (parsed.flight_hours or 0.0),
                    parsed.fields.get("missions_flown"),
                    parsed.medals or "无"))
                n_ok += 1
                continue

            try:
                summary = LB.reparse(db, rec, actor_user_id=actor_user_id)
            except LB.LogbookError as exc:
                # 失败也要把原因留在归档上（页面据此显示「解析失败」）
                LB.record_parse_failure(db, rec, str(exc))
                record_audit(db, actor_user_id, "logbook.reparse_failed",
                             "logbook_files", rec.id,
                             reason="回填重解析失败：%s" % exc)
                db.commit()
                print("  ✗ 解析失败：%s" % exc)
                n_fail += 1
                continue

            record_audit(db, actor_user_id, "logbook.reparse", "members",
                         rec.member_id,
                         before=summary.get("before"), after=summary.get("after"),
                         reason="回填历史 Logbook：%s"
                                % ("、".join(summary["changed"]) or "无变化"))
            db.commit()

            # ⚠️ SessionLocal 是 expire_on_commit=False，且上面已经读过 member.rank，
            #    所以关系缓存还停在改前的军衔上 —— 必须显式过期，
            #    否则脚本会打印一个"改后"却仍是旧军衔的假象。
            db.expire_all()
            m = db.get(Member, rec.member_id)
            print("  改后：军衔=%s 累计=%-10s 架次=%s" % (
                (m.rank.name if m.rank_id else "—"),
                _fmt_hours(m.logbook_hours_seconds), m.logbook_sorties))
            awards = LB.list_awards(db, rec.member_id)
            if awards:
                print("  勋章：%s" % "、".join(
                    "%s×%d" % (a.name, a.level) for a in awards))
            if summary.get("changed"):
                print("  变更：%s" % "、".join(summary["changed"]))
            elif changed_before == (m.rank_id, m.logbook_hours_seconds,
                                    m.logbook_sorties):
                print("  （名册无变化）")
            n_ok += 1

        print("\n" + "=" * 74)
        print("成功 %d，失败 %d，跳过（原件缺失）%d" % (n_ok, n_fail, n_skip))
        if not args.apply:
            print("这是只读预览 —— 加 --apply 才会真正写库。")
        print("=" * 74)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
