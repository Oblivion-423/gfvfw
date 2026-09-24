"""HTML 结构体检：页面里有没有**嵌套的 <form>**。

为什么单独查这个：新加的「账号与危险操作」面板里放了两个表单（解绑、作废），
很容易不小心把一个塞进另一个里面。嵌套表单在 HTML 里是**非法**的，
浏览器的解析行为是"丢掉内层标签"—— 外面的按钮照样能点，但内层表单的
字段/动作会错乱，而且**页面上看不出任何异常**。所以必须用解析器查，不能靠肉眼。

顺带查：每个 form 是否都带 csrf_token（漏一个就等于那个操作没防护）。

用法::

    .venv\\Scripts\\python.exe scripts/html_form_probe.py http://127.0.0.1:18081 <密码>
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSRF = re.compile(rb'name="csrf_token"\s+value="([^"]+)"')


class FormAudit(HTMLParser):
    """记录 form 的嵌套深度与每个 form 的 action / 是否有 csrf。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.max_depth = 0
        self.forms: list[dict] = []
        self._cur: list[dict] = []
        #: 每个 form 的 index，用于把 input 归到最近的 form
        self.inputs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            f = {"action": a.get("action", ""), "has_csrf": False,
                 "depth": self.depth, "method": a.get("method", "get")}
            self.forms.append(f)
            self._cur.append(f)
        elif tag == "input" and self._cur:
            if a.get("name") == "csrf_token":
                self._cur[-1]["has_csrf"] = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth = max(0, self.depth - 1)
            if self._cur:
                self._cur.pop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18081"
    password = sys.argv[2] if len(sys.argv) > 2 else "Del-Unbind-2026"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                     _NoRedirect())

    def get(url: str) -> tuple[int, str]:
        try:
            r = op.open(url, timeout=60)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(url: str, data: dict) -> int:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            return op.open(req, timeout=60).status
        except urllib.error.HTTPError as e:
            return e.code

    st, html = get(base + "/login")
    m = CSRF.search(html.encode())
    post(base + "/login", {"username": "admin", "password": password,
                           "csrf_token": m.group(1).decode()})

    # 收集要体检的页面
    st, members = get(base + "/members")
    mids = re.findall(r'/members/([0-9a-fA-F-]{36})"', members)
    st, camps = get(base + "/campaigns")
    cids = re.findall(r'/campaigns/([0-9a-fA-F-]{36})"', camps)

    pages = ["/", "/members", "/members?deleted=1", "/campaigns",
             "/campaigns?deleted=1", "/theater", "/applications", "/enroll",
             "/account", "/stats"]
    pages += ["/members/%s" % i for i in mids[:6]]
    pages += ["/campaigns/%s" % i for i in cids[:3]]

    bad_nested, bad_csrf, checked = [], [], 0
    for path in pages:
        status, html = get(base + path)
        if status != 200 or "<form" not in html:
            continue
        checked += 1
        a = FormAudit()
        a.feed(html)
        if a.max_depth > 1:
            bad_nested.append((path, a.max_depth))
        for f in a.forms:
            # 只要求 **POST** 表单带 csrf（GET 搜索表单不需要）
            if f["method"].lower() == "post" and not f["has_csrf"]:
                bad_csrf.append((path, f["action"]))
        print("  %-42s form %2d 个，最深嵌套 %d %s"
              % (path, len(a.forms), a.max_depth,
                 "**嵌套！**" if a.max_depth > 1 else ""))

    print("\n体检了 %d 个含表单的页面" % checked)
    print("嵌套表单：%s" % (bad_nested or "无"))
    print("POST 表单缺 CSRF：%s" % (bad_csrf or "无"))
    return 1 if (bad_nested or bad_csrf) else 0


if __name__ == "__main__":
    sys.exit(main())
