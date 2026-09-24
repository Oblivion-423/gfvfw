"""实况核对：**未登录的访客**能否通过 ``/enroll`` 直接开出一个队员账号。

联队口径是"这一页不需要任何权限"，所以最关键的验证是**完全不登录**也能走通。
TestClient 里的断言能覆盖这条，但真实 HTTP 栈（Cookie、CSRF、表单解析、
重定向）多一层，而且这个脚本可以对着**生产**跑。

用法::

    .venv\\Scripts\\python.exe scripts/enroll_open_probe.py http://127.0.0.1:18081 [呼号]

⚠️ 会**真的建出一个队员账号**（成员 + 账号 + member 角色）。对着生产跑之前
   先想清楚；推荐先拿探针服务器试。
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSRF = re.compile(rb'name="csrf_token"\s+value="([^"]+)"')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18081"
    callsign = sys.argv[2] if len(sys.argv) > 2 else "ProbeEnroll"

    # ⚠️ 故意**不**登录：一个全新的 cookie jar，什么都不带
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                     _NoRedirect())

    def get(url: str) -> tuple[int, bytes]:
        try:
            r = op.open(url, timeout=60)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(url: str, data: dict) -> tuple[int, bytes, str]:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            r = op.open(req, timeout=60)
            return r.status, r.read(), r.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Location") or ""

    print("目标：%s（**全程不登录**）" % base)

    st, html = get(base + "/enroll")
    print("GET  /enroll        -> %d" % st)
    if st != 200:
        print("FAIL  未登录访客打不开 /enroll（期望 200）")
        return 1
    page = html.decode("utf-8", "replace")
    says = "链接本身就是凭证" in page
    print("      页面写明「链接本身就是凭证」：%s" % ("是" if says else "**否**"))
    print("      页面给出收回公开的开关 GFVFW_ENROLL_OPEN：%s"
          % ("是" if "GFVFW_ENROLL_OPEN" in page else "**否**"))
    tok = CSRF.search(html)
    if not tok:
        print("FAIL  页面上没有 CSRF 隐藏域")
        return 1

    username = callsign
    st, body, loc = post(base + "/enroll", {
        "mode": "new", "callsign": callsign, "username": username,
        "password": "Probe-Enroll-2026", "confirm_password": "Probe-Enroll-2026",
        "csrf_token": tok.group(1).decode(),
    })
    print("POST /enroll        -> %d  Location=%s" % (st, loc))
    if st != 303:
        i = body.decode("utf-8", "replace").find("flash")
        print("FAIL  没开出账号：%s" % body.decode("utf-8", "replace")[i:i + 300])
        return 1
    if "did=created" not in loc:
        print("FAIL  跳转里没有 did=created")
        return 1

    # 新账号立刻能登录、且**已经是队员**（能进队内页面）
    op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                      _NoRedirect())
    st, html = get_via(op2, base + "/login")
    t2 = CSRF.search(html)
    if not t2:
        print("FAIL  登录页拿不到 CSRF")
        return 1
    body = urllib.parse.urlencode({"username": username,
                                   "password": "Probe-Enroll-2026",
                                   "csrf_token": t2.group(1).decode()}).encode()
    st_login, _, _ = post_via(op2, base + "/login", body)
    print("新账号登录          -> %d" % st_login)

    st_members, page_members = get_via(op2, base + "/members")
    print("新账号访问 /members -> %d" % st_members)
    nav = page_members.decode("utf-8", "replace").split("</nav>")[0]
    is_member = st_members == 200 and ">游客<" not in nav
    print("      导航身份标签不是「游客」：%s"
          % ("是（说明已是队员）" if is_member else "**否**"))

    ok = st_login == 303 and is_member
    print("\n%s —— 未登录访客可以自己开出队员账号，开完立刻是队员"
          % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                       _NoRedirect())


def get_via(op, url: str) -> tuple[int, bytes]:
    try:
        r = op.open(url, timeout=60)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_via(op, url: str, body: bytes) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = op.open(req, timeout=60)
        return r.status, r.read(), r.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Location") or ""


if __name__ == "__main__":
    sys.exit(main())
