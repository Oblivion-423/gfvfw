"""打印公开申请页与审批页的可读文本（人工过目用）。"""
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["GFVFW_DATABASE_URL"] = (
    "sqlite+pysqlite:///%s" % (ROOT / "var" / "probe" / "snapshot.sqlite3").as_posix())
os.environ["GFVFW_STORAGE_DIR"] = str(ROOT / "var" / "storage")
os.environ["GFVFW_SECRET_KEY"] = "probe-only-secret-key"

from fastapi.testclient import TestClient  # noqa: E402

from gfvfw.web.app import create_app  # noqa: E402

PW = "Probe-PW-2026"
CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def text_of(html: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    body = re.sub(r"(?is)<(br|/tr|/p|/h1|/h2|/h3|/div|/table|/details|/summary)\s*/?>",
                  "\n", body)
    body = re.sub(r"(?is)<(th|td)[^>]*>", " | ", body)
    body = re.sub(r"(?s)<[^>]+>", "", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#34;", '"'), ("&quot;", '"'), ("&#39;", "'")):
        body = body.replace(a, b)
    out = []
    for ln in (x.strip() for x in body.splitlines()):
        if ln and (not out or out[-1] != ln):
            out.append(ln)
    return "\n".join(out)


app = create_app()
print("#" * 74)
print("# 未登录访客看到的 /apply")
print("#" * 74)
with TestClient(app) as anon:
    print(text_of(anon.get("/apply").text))

print()
print("#" * 74)
print("# 未登录访客看到的 / （首页身份提示 + 导航）")
print("#" * 74)
with TestClient(app) as anon:
    body = text_of(anon.get("/").text)
print("\n".join(body.splitlines()[:22]))

print()
print("#" * 74)
print("# 管理员看到的 /applications")
print("#" * 74)
# 造一个待审批游客，才能看到审批行的形态
c = sqlite3.connect(str(ROOT / "var" / "probe" / "snapshot.sqlite3"))
c.execute("delete from applications where id like 'probe-%'")
c.execute("delete from users where id like 'probe-%'")
c.execute("insert into users (id, username, password_hash, status, created_at,"
          " updated_at, failed_login_count) values (?,?,?,?,datetime('now'),"
          " datetime('now'),0)",
          ("probe-guest-0001", "Newbie", "x", "pending"))
c.execute("insert into applications (id, desired_callsign, experience, intent,"
          " contact, status, resulting_user_id, source_ip_hash, created_at,"
          " updated_at) values (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
          ("probe-app-0001", "Newbie", "飞过 Falcon 4.0 约 200 小时",
           "想飞对空、能参加周末战役", "QQ 123456", "submitted",
           "probe-guest-0001", "deadbeef"))
c.commit()
c.close()

with TestClient(app) as client:
    page = client.get("/login")
    client.post("/login", data={"username": "admin", "password": PW,
                                "csrf_token": CSRF.search(page.text).group(1)},
                follow_redirects=False)
    print(text_of(client.get("/applications").text))
