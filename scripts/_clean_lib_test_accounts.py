import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# scripts/_clean_lib_test_accounts.py
from gfvfw.db import SessionLocal
from gfvfw.models import User, Member

with SessionLocal() as s:
    # 先清 audit_log 里引用这些用户的记录（开发库，允许删）
    from gfvfw.models import AuditLog
    from sqlalchemy import delete
    test_user_ids = [u.id for u in s.query(User)
                     .filter(User.username.in_(("tester", "guest"))).all()]
    if test_user_ids:
        s.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(test_user_ids)))
        s.flush()

    for uname in ("tester", "guest"):
        u = s.query(User).filter_by(username=uname).first()
        if u:
            print("删除用户", u.username)
            s.delete(u)
    s.flush()

    m = s.query(Member).filter_by(callsign="TESTER").first()
    if m:
        print("删除名册", m.callsign)
        s.delete(m)
    s.commit()