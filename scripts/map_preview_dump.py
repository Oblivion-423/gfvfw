"""把真实渲染出来的态势图存成一张可截图的预览页。

为什么要这么做
--------------
"底图有没有出来"只有浏览器知道：``<image href>`` 指向的 URL 是否 200、
SVG 里的合成顺序对不对、``opacity``/``dim`` 有没有把图压成全黑 ——
这些都是纯 HTML 断言看不出来的。所以这里把**服务器真实返回的**
态势图 SVG 取下来，把底图换成 data URI 内联进去（这样离线截图也能显示），
再套上应用的样式表存成一个 HTML，交给 vision 工具截图与看图。

用法::

    .venv\\Scripts\\python.exe scripts/map_preview_dump.py \\
        http://127.0.0.1:18081 <密码> --out var/_map_preview.html
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSRF = re.compile(rb'name="csrf_token"\s+value="([^"]+)"')
SVG_RE = re.compile(r"(<svg\b.*?</svg>)", re.S)
IMG_RE = re.compile(r'href="(/theater/[^"]*/map/image/\d+)"')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="导出态势图预览页（供截图/看图）")
    ap.add_argument("base", help="服务地址，如 http://127.0.0.1:18081")
    ap.add_argument("password", help="admin 密码")
    ap.add_argument("--campaign", default=None, help="战役 id；不给就取列表第一个")
    ap.add_argument("--out", default="var/_map_preview.html")
    ap.add_argument("--map-query", default="", help="附加查询串，如 layer=all")
    args = ap.parse_args()

    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                     _NoRedirect())

    def get(url: str) -> tuple[int, bytes]:
        try:
            r = op.open(url, timeout=120)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(url: str, data: dict) -> tuple[int, bytes]:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            r = op.open(req, timeout=60)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    st, html = get(args.base + "/login")
    m = CSRF.search(html)
    if not m:
        print("FAIL 拿不到登录页")
        return 1
    st, _ = post(args.base + "/login",
                 {"username": "admin", "password": args.password,
                  "csrf_token": m.group(1).decode()})
    print("登录 -> %d" % st)
    if st != 303:
        print("FAIL 登录失败")
        return 1

    cid = args.campaign
    if cid is None:
        st, html = get(args.base + "/theater")
        ids = re.findall(rb"/theater/([0-9a-fA-F-]{36})", html)
        if not ids:
            print("FAIL 战役列表里没有战役")
            return 1
        cid = ids[0].decode()
    print("战役 %s" % cid)

    url = "%s/theater/%s/map" % (args.base, cid)
    if args.map_query:
        url += "?" + args.map_query
    st, html = get(url)
    page = html.decode("utf-8", "replace")
    print("态势图页 -> %d（%d 字节）" % (st, len(page)))

    sm = SVG_RE.search(page)
    if not sm:
        print("FAIL 页面里没有 <svg>")
        return 1
    svg = sm.group(1)

    im = IMG_RE.search(svg)
    if im:
        img_url = args.base + im.group(1)
        ist, blob = get(img_url)
        print("底图 %s -> %d（%d 字节）" % (im.group(1), ist, len(blob)))
        if ist == 200:
            svg = svg.replace(im.group(1),
                              "data:image/png;base64,"
                              + base64.b64encode(blob).decode())
            print("已内联底图（data URI，%.1f MB base64）" % (len(blob) * 4 / 3 / 1048576))
    else:
        print("⚠️ 页面里没有 <image> —— 底图没出来，截图只会看到点线")

    # 带上应用样式表，配色与真实页面一致（深色底）
    out = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<style>"
           "body{background:#0d1117;color:#c9d1d9;margin:0;padding:12px;"
           "font-family:system-ui,'Segoe UI',sans-serif}"
           "svg{width:1000px;height:1000px;display:block;background:#0d1117;"
           "border:1px solid #30363d;border-radius:6px}"
           "h1{font-size:15px;font-weight:600;margin:0 0 8px}"
           "</style></head><body>"
           "<h1>态势图预览（底图已内联）— 战役 %s</h1>%s</body></html>"
           % (cid, svg))
    Path(args.out).write_text(out, encoding="utf-8")
    print("已写出 %s（%d 字节）" % (args.out, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
