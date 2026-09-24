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
#: 战役管理详情页里的普通 `<img>`（「战区地图」面板）
IMG_TAG_RE = re.compile(r'src="(/theater/[^"]*/map/image/\d+)"')
CSS_RE = re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"[^>]*>')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _abs(base: str, href: str) -> str:
    """把 href 变成可请求的绝对地址（已经是绝对地址的原样返回）。"""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "http:" + href
    return base.rstrip("/") + "/" + href.lstrip("/")


def _dump_detail(args, cid, page, get, out_path: Path) -> int:
    """把战役管理详情页（/theater/{id}）存成自包含预览页。

    为什么要看这一眼：「战区地图」面板是不是**真的把图渲染出来了**，纯 HTML
    断言答不了 —— `<img>` 的 URL 200、字节数对，但被 CSS 压成 0 高、被
    overflow 裁掉、被 max-width 拉到看不见，都只有看图才知道。
    这里把样式表和底图都内联进去，离屏也能看出真实样子。
    """
    # 1) 内联样式表（否则离线打开是一堆无样式文字，看不出真实版式）
    #    ⚠️ 不能无脑 `base + href`：模板里用的是 `url_for('static', …)`，
    #       渲染出来**已经是绝对地址**，拼一下就成了
    #       "http://hosthttp://host/static/app.css"（getaddrinfo 直接失败）。
    replaced_css = False
    for tag, href in [(m.group(0), m.group(1)) for m in CSS_RE.finditer(page)]:
        st, css = get(_abs(args.base, href))
        if st == 200:
            page = page.replace(tag, "<style>%s</style>"
                                     % css.decode("utf-8", "replace"), 1)
            print("已内联样式表 %s（%d 字节）" % (href, len(css)))
            replaced_css = True
    if not replaced_css:
        print("⚠️ 没内联到样式表 —— 截图里的版式会与真实页面不同")

    # 2) 底图：**写成旁边的文件**，用相对路径引用。
    #    ⚠️ 不要内联成 data: URI —— 实测（Chrome headless，同一张 7.8 MB 底图）:
    #       放 `<img src="file:///…">` 截图 1.79 MB（图正常画出），
    #       放 `<img src="data:image/png;base64,…">`（10 MB 属性）截图只有 0.25 MB，
    #       整页一个亮像素都没有 —— 图**根本没画**，会被误判成"面板没出来"。
    #       写文件 + 相对路径既自包含又可靠。
    hits = IMG_TAG_RE.findall(page)
    if not hits:
        print("⚠️ 页面里没有底图 <img> —— 面板没出来，或选了「不显示」")
    for i, src in enumerate(dict.fromkeys(hits)):
        ist, blob = get(_abs(args.base, src))
        print("底图 %s -> %d（%d 字节）" % (src, ist, len(blob)))
        if ist == 200:
            side = out_path.with_name(out_path.stem + ".map%d.png" % i)
            side.write_bytes(blob)
            page = page.replace(src, side.name)
            print("底图已写到 %s（相对路径引用）" % side.name)

    # 3) 把剩下的站内链接改成绝对地址，免得截图时看起来像断链
    page = re.sub(r'(href|src)="(/[^"]*)"', r'\1="%s\2"' % args.base, page)

    out_path.write_text(page, encoding="utf-8")
    print("已写出 %s（%d 字节）" % (out_path, len(page)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导出态势图预览页（供截图/看图）")
    ap.add_argument("base", help="服务地址，如 http://127.0.0.1:18081")
    ap.add_argument("password", help="admin 密码")
    ap.add_argument("--campaign", default=None, help="战役 id；不给就取列表第一个")
    ap.add_argument("--out", default="var/_map_preview.html")
    ap.add_argument("--map-query", default="", help="附加查询串，如 layer=all")
    ap.add_argument("--page", default="map", choices=("map", "detail"),
                    help="map=态势图（SVG）；detail=战役管理详情页（战区地图面板）")
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

    url = "%s/theater/%s%s" % (args.base, cid,
                               "/map" if args.page == "map" else "")
    if args.map_query:
        url += ("&" if "?" in url else "?") + args.map_query
    st, html = get(url)
    page = html.decode("utf-8", "replace")
    print("%s -> %d（%d 字节）"
          % ("态势图页" if args.page == "map" else "详情页", st, len(page)))
    if st != 200:
        print("FAIL 页面不是 200")
        return 1

    if args.page == "detail":
        return _dump_detail(args, cid, page, get, Path(args.out))

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
