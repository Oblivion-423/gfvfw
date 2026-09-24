"""验证「战役管理」详情页（/theater/{id}）里也能看到战区底图。

用户口径：「任何实现战役管理中，显示战区地图」——
联队口中的"战役管理"就是 /theater/{id}，以前只有 /theater/{id}/map 才有底图。

本探针覆盖两段：

  A 段（有底图的服务器）：/theater/{id} 出现「战区地图」面板 + 真实的 <img>；
    ?map=none / ?map=<i> 的选择可用；图片路由本身返回 PNG；
    **同时回归** /theater/{id}/map 没被这次重构改坏（底图还在、关掉还关得掉）。

  B 段（没有底图的服务器）：两个页面都必须出现那段"怎么让底图出来"的说明 ——
    这段说明本轮抽成了 theater/_map_missing.html 由两页共用，
    所以**两页都得验证**，否则抽错了只在另一页上炸。

用法::

    .venv\\Scripts\\python.exe scripts\\map_in_campaign_probe.py \
        http://127.0.0.1:18081 http://127.0.0.1:18082
"""
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Windows 控制台默认 GBK，打不出 ✓/✗ —— 用 reconfigure（**不要**换 TextIOWrapper，
# 那会在中途关掉底层 buffer，运行到一半报 "I/O operation on closed file"）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                         # noqa: BLE001
    pass

A = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18081"
B = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:18082"

USERNAME = os.environ.get("GFVFW_LIVE_USER", "admin")
PASSWORD = os.environ.get("GFVFW_LIVE_PASSWORD", "Map-In-Campaign-2026")

ok = 0
bad = 0


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  \u2713 {name}")
    else:
        bad += 1
        print(f"  \u2717 {name}" + (f"  [{detail}]" if detail else ""))


class S:
    """极简会话。

    ⚠️ 必须用 ``http.cookiejar`` + ``HTTPCookieProcessor``，**不能**手工把
       Set-Cookie 抄下来再塞回 Cookie 头：登录成功后服务端会**换掉** session
       cookie（防会话固定），而 urllib 跟随 303 跳转时复用的是我们自己加的那个
       旧 Cookie 头 —— 结果"登录成功"（最终 URL 是 /）但拿到的仍是**匿名**页面，
       表现为后面每条断言都 404/403，看起来像权限问题。真踩过。
    """

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _open(self, path, data=None, headers=None):
        url = self.base + path
        # `data is not None`，不能写真值判断：b"" 是假值会被误发成 GET。
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", "gfvfw-map-in-campaign-probe/1.0")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def get(self, path):
        sc, raw, hdrs = self._open(path)
        return sc, raw.decode("utf-8", "replace"), hdrs

    def get_raw(self, path):
        return self._open(path)

    def post(self, path, fields):
        body = urllib.parse.urlencode(fields).encode()
        sc, raw, hdrs = self._open(
            path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        return sc, raw.decode("utf-8", "replace"), hdrs

    def csrf(self, path):
        _, html, _ = self.get(path)
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    def login(self):
        tok = self.csrf("/login")
        if not tok:
            return False
        sc, html, _ = self.post(
            "/login", {"username": USERNAME, "password": PASSWORD,
                       "csrf_token": tok})
        return sc == 200 and "退出" in html


def campaigns(s):
    """从 /theater 列表里取战役 id（列表页只要登录即可看）。"""
    sc, html, _ = s.get("/theater")
    assert sc == 200, f"/theater -> {sc}"
    return sorted(set(re.findall(r"/theater/([0-9a-f-]{36})", html)))


print("=" * 72)
print(f"A 段：有底图的服务器 {A}")
print("=" * 72)
sa = S(A)
sc, html, _ = sa.get("/login")
check("GET /login 200", sc == 200, f"status={sc}")
check("登录成功", sa.login())

ids = campaigns(sa)
check("战役管理列表拿到战役", len(ids) >= 1, f"ids={ids}")

detail_id = None
for cid in ids:
    sc, html, _ = sa.get(f"/theater/{cid}")
    if sc == 200 and "战区地图" in html:
        detail_id = cid
        break
check("找到带存档的战役详情页", detail_id is not None)
if detail_id is None:
    print("  （后面的断言跳过）")
else:
    cid = detail_id
    sc, html, _ = sa.get(f"/theater/{cid}")
    check("详情页 200", sc == 200, f"status={sc}")
    check("详情页有「战区地图」面板", "战区地图" in html)
    check("详情页面板里有 <img>",
          re.search(r'<img[^>]*src="/theater/%s/map/image/\d+"' % re.escape(cid), html)
          is not None)
    # ⚠️ 断言"**没有**这两个属性"。实测（同一张 4096² 底图、同窗口）：
    #    · 裸 <img>          → 1 548 410 字节，底图画出来了
    #    · loading="lazy"    → 视口外的图根本不取，面板只剩空框
    #    · decoding="async"  → 10 144 字节，整页没有亮像素，只剩空框
    #    底图必须**确定画出来** —— "底图不显示"是联队报过的故障。
    check("底图不是 loading=lazy（否则面板会只剩空框）",
          'loading="lazy"' not in html)
    check("底图不是 decoding=async（否则先画空框）",
          'decoding="async"' not in html)
    check("有跳转完整态势图的主链接",
          f'href="/theater/{cid}/map?map=' in html)
    check("有底图挑选徽章",
          len(re.findall(r'href="/theater/%s\?map=' % re.escape(cid), html)) >= 1,
          f"n={len(re.findall(r'href=./theater/%s..map=' % re.escape(cid), html))}")
    check("有「不显示」开关", f'href="/theater/{cid}?map=none"' in html)

    # 图片路由本体
    m = re.search(r'src="(/theater/%s/map/image/\d+)"' % re.escape(cid), html)
    if m:
        sc, raw, hdrs = sa.get_raw(m.group(1))
        check("底图路由 200", sc == 200, f"status={sc}")
        check("底图是 PNG", raw[:8] == b"\x89PNG\r\n\x1a\n", f"magic={raw[:8]!r}")
        check("底图有内容", len(raw) > 64 * 1024, f"bytes={len(raw)}")
        check("底图带强缓存",
              "max-age=" in (hdrs.get("Cache-Control") or ""),
              hdrs.get("Cache-Control"))
    else:
        check("提取到图片 URL", False)

    # ?map=none → 关闭
    sc, off_html, _ = sa.get(f"/theater/{cid}?map=none")
    check("?map=none 详情页 200", sc == 200, f"status={sc}")
    check("?map=none 显示「底图已关闭」", "底图已关闭" in off_html)
    check("?map=none 不再输出 <img>",
          f'/theater/{cid}/map/image/' not in off_html)
    check("?map=none 时徽章仍可点回来（面板还在）",
          f'href="/theater/{cid}?map=' in off_html or "不显示" in off_html)

    # ?map=<i> 选择生效：让第 1 张（若有）变成 accent
    idxs = sorted(set(int(x) for x in
                      re.findall(r'href="/theater/%s\?map=(\d+)"' % re.escape(cid),
                                 html)))
    check("底图数量 >= 1", len(idxs) >= 1, f"idxs={idxs}")
    if len(idxs) >= 2:
        pick = idxs[1]
        sc, pick_html, _ = sa.get(f"/theater/{cid}?map={pick}")
        seg = re.search(
            r'<a class="badge([^"]*)"\s+href="/theater/%s\?map=%d"' % (re.escape(cid), pick),
            pick_html)
        check(f"?map={pick} 时该徽章高亮", seg is not None and "accent" in seg.group(1),
              seg.group(1) if seg else "no match")
        check(f"?map={pick} 时图片用第 {pick} 张",
              f'/theater/{cid}/map/image/{pick}"' in pick_html)

    # 回归：态势图页没被重构改坏
    sc, m_html, _ = sa.get(f"/theater/{cid}/map")
    check("态势图仍 200", sc == 200, f"status={sc}")
    check("态势图仍有底图 <image id=basemap>", 'id="basemap"' in m_html)
    check("态势图仍能关底图（?map=none）",
          "&map=none" in m_html)
    sc, m_off, _ = sa.get(f"/theater/{cid}/map?map=none")
    check("态势图 ?map=none 200", sc == 200, f"status={sc}")
    check("态势图 ?map=none 后没有 basemap", 'id="basemap"' not in m_off)
    check("有底图时态势图不显示缺图说明", "怎么让底图出来" not in m_html)

    # 详情页顶部链接没被破坏
    check("详情页仍有「战场态势图」入口",
          f'href="/theater/{cid}/map"' in html)

print()
print("=" * 72)
print(f"B 段：没有底图的服务器 {B}")
print("=" * 72)
try:
    sb = S(B)
    sc, _, _ = sb.get("/login")
    check("GET /login 200", sc == 200, f"status={sc}")
    check("登录成功", sb.login())
    ids_b = campaigns(sb)
    check("B 段拿到战役", len(ids_b) >= 1, f"ids={ids_b}")
    hit = None
    for c in ids_b:
        sc, h, _ = sb.get(f"/theater/{c}")
        if sc == 200 and "战区地图" in h:
            hit = c
            break
    check("B 段找到带存档的战役", hit is not None)
    if hit:
        sc, h, _ = sb.get(f"/theater/{hit}")
        check("B 段详情页仍 200", sc == 200, f"status={sc}")
        check("B 段详情页缺图说明来自共用 partial（详情页）",
              "怎么让底图出来" in h)
        check("B 段详情页提示导出脚本",
              "collect_theater_maps.py" in h)
        check("B 段详情页没有 <img> 底图",
              f'/theater/{hit}/map/image/' not in h)
        sc, mh, _ = sb.get(f"/theater/{hit}/map")
        check("B 段态势图仍 200", sc == 200, f"status={sc}")
        check("B 段态势图缺图说明来自共用 partial（态势图）",
              "怎么让底图出来" in mh)
        check("B 段态势图提示导出脚本", "collect_theater_maps.py" in mh)
except Exception as e:                                    # noqa: BLE001
    check(f"B 段连接 {B}", False, repr(e))

print()
print("=" * 72)
print(f"结果：{ok} 通过 / {bad} 失败")
print("=" * 72)
sys.exit(1 if bad else 0)
