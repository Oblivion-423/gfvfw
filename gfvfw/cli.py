"""
命令行工具 —— 首次部署与运维辅助。

用法::

    .venv\\Scripts\\python.exe -m gfvfw.cli create-admin --callsign Oblivion
    .venv\\Scripts\\python.exe -m gfvfw.cli create-member --callsign Viper
    .venv\\Scripts\\python.exe -m gfvfw.cli list-members
    .venv\\Scripts\\python.exe -m gfvfw.cli grant-role --callsign Oblivion --role commander
    .venv\\Scripts\\python.exe -m gfvfw.cli set-password --username admin --generate
    .venv\\Scripts\\python.exe -m gfvfw.cli seed          # 仅播种基础数据

为什么需要它
------------
联队账号采用"申请后开通"，**没有自助注册**。
第一个管理员账号必须在命令行创建（此时系统里还没有任何能登录的人）。
``set-password`` 则是**忘记密码时唯一的出路** —— 密码是单向哈希，
找不回来，只能重置。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy import select

from .db import SessionLocal, engine
from .models import Member, MemberRole, Role, User
from .permissions import ROLE_DEFINITIONS
from .security import hash_password, new_token, password_problem
from .services.audit import record_audit
from .services.bootstrap import ensure_schema, seed

log = logging.getLogger("gfvfw.cli")


def _setup() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)-7s %(message)s")
    ensure_schema(engine)


def _get_or_create_member(db, callsign: str) -> Member:      # noqa: ANN001
    m = db.scalar(select(Member).where(Member.callsign == callsign))
    if m is None:
        m = Member(callsign=callsign, status="active")
        db.add(m)
        db.flush()
        print("  已创建名册成员：%s" % callsign)
    return m


def _assign_role(db, member: Member, role_code: str) -> None:  # noqa: ANN001
    role = db.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise SystemExit("角色不存在：%s（可用：%s）"
                         % (role_code, ", ".join(ROLE_DEFINITIONS)))
    existing = db.scalar(select(MemberRole).where(
        MemberRole.member_id == member.id,
        MemberRole.role_id == role.id,
        MemberRole.revoked_at.is_(None)))
    if existing is None:
        db.add(MemberRole(member_id=member.id, role_id=role.id))
        print("  已授予角色：%s" % ROLE_DEFINITIONS[role_code][0])


def cmd_create_admin(args) -> None:
    _setup()
    with SessionLocal() as db:
        seed(db)
        existing = db.scalar(select(User).where(User.username == args.username))
        if existing is not None:
            raise SystemExit("用户名已存在：%s" % args.username)

        password = args.password
        if not password:
            password = getpass.getpass("请输入密码：")
            confirm = getpass.getpass("请再输入一次：")
            if password != confirm:
                raise SystemExit("两次输入不一致")
        # ⚠️ 与 Web 改密共用同一套策略（security.password_problem），
        #    否则会出现"网页不让设的密码命令行能设"。
        problem = password_problem(password)
        if problem:
            raise SystemExit(problem)

        member = _get_or_create_member(db, args.callsign)
        user = User(
            username=args.username,
            password_hash=hash_password(password),
            status="active",                 # 管理员直接激活
            member_id=member.id,
        )
        db.add(user)
        db.flush()
        _assign_role(db, member, "owner")
        db.commit()
        print("\n管理员创建成功：")
        print("  用户名 : %s" % args.username)
        print("  呼号   : %s" % args.callsign)
        print("  角色   : 超级管理员")
        print("\n请立即登录并修改密码。")


def cmd_create_member(args) -> None:
    _setup()
    with SessionLocal() as db:
        seed(db)
        if db.scalar(select(Member).where(Member.callsign == args.callsign)):
            raise SystemExit("呼号已存在：%s" % args.callsign)
        member = Member(callsign=args.callsign, status=args.status)
        db.add(member)
        db.flush()
        if args.username:
            password = args.password or getpass.getpass("请输入密码：")
            problem = password_problem(password)
            if problem:
                raise SystemExit(problem)
            db.add(User(username=args.username,
                        password_hash=hash_password(password),
                        status="active", member_id=member.id))
            _assign_role(db, member, args.role)
        db.commit()
        print("已创建成员：%s" % args.callsign)


def cmd_list_members(args) -> None:
    _setup()
    with SessionLocal() as db:
        rows = db.execute(
            select(Member.callsign, Member.status, User.username)
            .outerjoin(User, User.member_id == Member.id)
            .where(Member.deleted_at.is_(None))
            .order_by(Member.callsign)
        ).all()
        if not rows:
            print("名册为空。")
            return
        print("%-16s %-10s %s" % ("呼号", "状态", "账号"))
        print("-" * 44)
        for callsign, status, username in rows:
            print("%-16s %-10s %s" % (callsign, status, username or "（无）"))


def cmd_grant_role(args) -> None:
    _setup()
    with SessionLocal() as db:
        member = db.scalar(select(Member).where(Member.callsign == args.callsign))
        if member is None:
            raise SystemExit("名册中找不到呼号：%s" % args.callsign)
        _assign_role(db, member, args.role)
        db.commit()
        print("完成。")


def cmd_seed(args) -> None:
    _setup()
    with SessionLocal() as db:
        created = seed(db)
    print("基础数据播种：", created)


def cmd_set_password(args) -> None:
    """重置某个账号的密码。

    这是**忘记密码时唯一的出路** —— 密码是 argon2id 单向哈希，
    没有任何"找回"的可能，只能由运维重置。
    """
    _setup()
    if not args.username and not args.callsign:
        raise SystemExit("必须给 --username 或 --callsign 之一")

    with SessionLocal() as db:
        if args.username:
            user = db.scalar(select(User).where(User.username == args.username))
            who = "用户名 %s" % args.username
        else:
            member = db.scalar(
                select(Member).where(Member.callsign == args.callsign,
                                     Member.deleted_at.is_(None)))
            if member is None:
                raise SystemExit("名册中找不到呼号：%s" % args.callsign)
            user = db.scalar(select(User).where(User.member_id == member.id))
            who = "呼号 %s" % args.callsign

        if user is None:
            raise SystemExit("找不到账号：%s" % who)

        # 生成随机密码
        if args.generate:
            password = new_token(12)          # ~16 字符 URL 安全随机串
        elif args.password:
            password = args.password
        else:
            password = getpass.getpass("请输入新密码：")
            confirm = getpass.getpass("请再输入一次：")
            if password != confirm:
                raise SystemExit("两次输入不一致")

        problem = password_problem(password)
        if problem:
            raise SystemExit(problem)

        old_status = user.status
        user.password_hash = hash_password(password)
        # ⚠️ 必须同时解锁：账号被锁定时正是最需要重置密码的场景，
        #    若只改哈希而留着 locked_until，运维会以为重置失败。
        was_locked = bool(user.locked_until)
        user.failed_login_count = 0
        user.locked_until = None

        record_audit(db, None, "user.password.reset", "users", user.id,
                     before={"locked": was_locked, "status": old_status},
                     after={"status": user.status},
                     reason=args.reason or "运维通过 CLI 重置密码")
        db.commit()

        print("\n密码已重置：")
        print("  用户名 : %s" % user.username)
        print("  现状态 : %s%s" % (user.status,
                                  "" if user.status == "active" else "  ⚠️ 账号未激活，仍登录不了"))
        if was_locked:
            print("  已解锁 : 是（此前处于登录锁定）")
        if args.generate or args.password:
            print("  新密码 : %s" % password)
            print("\n⚠️ 上面的密码只显示这一次，请立刻转告本人并让其登录后自行修改。")
        else:
            print("\n请让本人登录后到「账号」页自行修改。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gfvfw.cli", description="GFVFW 运维命令")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="创建超级管理员（首个账号必须用它）")
    p.add_argument("--username", default="admin")
    p.add_argument("--callsign", required=True)
    p.add_argument("--password", default="")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("create-member", help="创建名册成员（可同时开账号）")
    p.add_argument("--callsign", required=True)
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--status", default="active",
                   choices=["active", "reserve", "retired", "probation"])
    p.add_argument("--role", default="member", choices=list(ROLE_DEFINITIONS))
    p.set_defaults(func=cmd_create_member)

    p = sub.add_parser("list-members", help="列出名册与账号绑定情况")
    p.set_defaults(func=cmd_list_members)

    p = sub.add_parser("grant-role", help="给成员授予角色")
    p.add_argument("--callsign", required=True)
    p.add_argument("--role", required=True, choices=list(ROLE_DEFINITIONS))
    p.set_defaults(func=cmd_grant_role)

    p = sub.add_parser("seed", help="仅播种基础数据（军衔/机型/角色）")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("set-password",
                       help="重置账号密码（忘记密码时唯一的出路，会自动解锁）")
    p.add_argument("--username", default="", help="按登录名定位账号")
    p.add_argument("--callsign", default="", help="按呼号定位账号（与 --username 二选一）")
    p.add_argument("--password", default="",
                   help="非交互设置密码；省略则交互输入两次")
    p.add_argument("--generate", action="store_true",
                   help="生成随机强密码并打印一次（推荐给运维代设初始密码）")
    p.add_argument("--reason", default="", help="写入审计的原因")
    p.set_defaults(func=cmd_set_password)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
