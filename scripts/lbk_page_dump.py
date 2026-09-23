"""打印线上快照下 Logbook 页面的可读文本（人工过目用）。"""
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

c = sqlite3.connect(str(ROOT / "var" / "probe" / "snapshot.sqlite3"))
c.row_factory = sqlite3.Row
member = c.execute("select id, callsign from members where callsign='Oblivion'"
                   ).fetchone()
c.close()

with TestClient(create_app()) as client:
    page = client.get("/login")
    client.post("/login", data={"username": "admin", "password": PW,
                                "csrf_token": CSRF.search(page.text).group(1)},
                follow_redirects=False)
    html = client.get("/members/%s/logbook" % member["id"]).text

# 去掉 script/style，把块级标签换行，便于阅读
body = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
body = re.sub(r"(?is)<(br|/tr|/p|/h1|/h2|/h3|/div|/table|/details|/summary)\s*/?>",
              "\n", body)
body = re.sub(r"(?is)<th[^>]*>", " | ", body)
body = re.sub(r"(?is)<td[^>]*>", " | ", body)
body = re.sub(r"(?s)<[^>]+>", "", body)
body = body.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
body = body.replace("&gt;", ">").replace("&#34;", '"').replace("&quot;", '"')
lines = [ln.strip() for ln in body.splitlines()]
out = []
for ln in lines:
    if ln and (not out or out[-1] != ln):
        out.append(ln)
print("\n".join(out))
