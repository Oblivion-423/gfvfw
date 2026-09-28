"""资料库真实 HTTP 探针（只读，不写数据）。

对着运行中的服务跑，核对部署后的行为。

用法：
    set GFVFW_LIVE_URL=http://127.0.0.1:8000
    set GFVFW_LIVE_USER=tester
    set GFVFW_LIVE_PASSWORD=password123
    python scripts/library_probe.py

检查项：
  * 匿名访客：/library 303 到登录页；/library/api/upload 303/401/403
  * 登录队员：/library 200；文件树 API 200；预览不存在 id 404
  * 路径穿越 /libary/api/tree?path=../../etc → 400
  * 缺/错 CSRF 的上传 → 403（**这是真漏洞的守门员**）

⚠️ 全程只发 GET 和无副作用的 POST（用错 CSRF 触发 403，不会真的写入）。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

import httpx

BASE = os.environ.get("GFVFW_LIVE_URL", "http://127.0.0.1:8000")
USER = os.environ.get("GFVFW_LIVE_USER")
PWD  = os.environ.get("GFVFW_LIVE_PASSWORD")

PASSED = 0
FAILED = 0
FAILURES: list[str] = []

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def _check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  ✓ %s" % msg)
    else:
        FAILED += 1
        FAILURES.append(msg)
        print("  ✗ %s" % msg)


def _csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def probe() -> int:
    print("== 目标：%s ==" % BASE)

    # ----------------------------------------------------------------------
    print("\n== [1] 匿名访客 ==")
    with httpx.Client(base_url=BASE, follow_redirects=False, timeout=10) as c:
        r = c.get("/library")
        _check(r.status_code == 303,
               "GET /library → 303（跳登录），得到 %d" % r.status_code)

        r = c.post("/library/api/upload",
                   files={"file": ("x.txt", b"x")},
                   data={"csrf_token": ""})
        _check(r.status_code in (303, 401, 403),
               "POST /library/api/upload → 303/401/403，得到 %d" % r.status_code)

        r = c.get("/library/api/download?id=__nonexistent__")
        _check(r.status_code in (303, 401, 403),
               "GET /library/api/download → 303/401/403，得到 %d" % r.status_code)

    if not USER or not PWD:
        print("\n⚠️ 未设 GFVFW_LIVE_USER / GFVFW_LIVE_PASSWORD，跳过登录段")
        return 0

    # ----------------------------------------------------------------------
    print("\n== [2] 登录队员 ==")
    with httpx.Client(base_url=BASE, follow_redirects=False, timeout=10) as c:
        r = c.get("/login")
        _check(r.status_code == 200, "GET /login → 200")
        if r.status_code != 200:
            return 1
        token = _csrf(r.text)
        _check(bool(token), "登录页含 csrf_token")

        r = c.post("/login", data={
            "username": USER, "password": PWD, "csrf_token": token,
        })
        _check(r.status_code == 303,
               "POST /login → 303，得到 %d" % r.status_code)
        if r.status_code != 303:
            return 1

    # ----------------------------------------------------------------------
    print("\n== [3] 资料库只读接口 ==")
    with httpx.Client(base_url=BASE, follow_redirects=False, timeout=30) as c:
        # 登录后仍需要 CSRF（有些项目会在登录后给 session cookie）
        r = c.get("/login")
        login_token = _csrf(r.text)
        c.post("/login", data={
            "username": USER, "password": PWD, "csrf_token": login_token,
        })

        r = c.get("/library")
        _check(r.status_code == 200, "GET /library → 200，得到 %d" % r.status_code)
        _check('class="explorer"' in r.text or 'id="tree"' in r.text,
               "页面含文件树容器")
        _check("/static/library.js" in r.text, "页面引用 library.js")

        r = c.get("/library/api/tree?path=")
        _check(r.status_code == 200, "列根目录 → 200，得到 %d" % r.status_code)
        if r.status_code == 200:
            data = r.json()
            _check(isinstance(data.get("folders"), list), "返回 folders 列表")
            _check(isinstance(data.get("files"), list), "返回 files 列表")

        r = c.get("/library/api/tree?path=../../etc")
        _check(r.status_code == 400,
               "路径穿越 → 400，得到 %d" % r.status_code)

        r = c.get("/library/api/preview?id=__nonexistent__")
        _check(r.status_code == 404,
               "不存在 id → 404，得到 %d" % r.status_code)

        r = c.get("/library/api/search?q=")
        _check(r.status_code == 200, "空关键词搜索 → 200")

    # ----------------------------------------------------------------------
    print("\n== [4] CSRF 守门员 ==")
    with httpx.Client(base_url=BASE, follow_redirects=False, timeout=30) as c:
        r = c.get("/login")
        login_token = _csrf(r.text)
        c.post("/login", data={
            "username": USER, "password": PWD, "csrf_token": login_token,
        })

        # 故意用错 token：应 403，绝不能 200
        r = c.post("/library/api/upload",
                   files={"file": ("probe.txt", b"probe", "text/plain")},
                   data={"csrf_token": "definitely-wrong"})
        _check(r.status_code == 403,
               "错 CSRF 上传 → 403，得到 %d（200 = CSRF 漏检）"
               % r.status_code)

    return 0


if __name__ == "__main__":
    probe()
    print("\n" + "=" * 60)
    print("断言：%d 通过 / %d 失败" % (PASSED, FAILED))
    if FAILED:
        for m in FAILURES:
            print("  ✗ %s" % m)
    sys.exit(1 if FAILED else 0)