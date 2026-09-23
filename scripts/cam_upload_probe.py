"""实况探针：真的走 HTTP 上传一份 ``.cam``，看服务器给什么。

**为什么需要它**（而不是只用 TestClient）：
2026 年的一次线上事故里，用户上传 ``.cam`` 后看到的是光秃秃的
``Internal Server Error``。成因是 ``.cam`` 里一个未初始化的实体槽位
（``unit_id=0xFFFF0001``、``id_creator=0xFFFFFFFF``）被读成 ``z=NaN``，
SQLite 把 NaN 当 NULL，撞上 ``campaign_units.z`` 的 NOT NULL 约束；
会话随即进入 PendingRollback，而当时的失败处理又拿这个脏会话去查战役列表，
于是 ``PendingRollbackError`` 把真正的错误信息盖掉了。

自校验（``tests/campaign_theater_selfcheck.py`` §10）已经在 TestClient 上
复现并覆盖了这条链路；这个脚本的价值在于**再多一层真实 HTTP 栈**
（multipart 解析、Starlette 异常处理器、模板渲染），并且可以对着
**生产/预发服务器**跑 —— 那才是用户真正点击的地方。

用法::

    # 本机探针服务器（推荐：绝不拿生产库试错）
    .venv\\Scripts\\python.exe -m gfvfw --host 127.0.0.1 --port 18081
    .venv\\Scripts\\python.exe scripts\\cam_upload_probe.py \\
        http://127.0.0.1:18081 <密码> "G:\\BMS\\...\\Save-Day  3 02 00 46.cam"

    # 只验证上传页能不能打开（不给 .cam 路径）
    .venv\\Scripts\\python.exe scripts\\cam_upload_probe.py http://127.0.0.1:18080

退出码：0 = 一切正常；1 = 出现 500 / 非预期状态码 / 打不开页面。

⚠️ 这个脚本会**真的往目标库写一条存档记录**。对着生产跑之前先想清楚
（这也是推荐用探针服务器的原因）。
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
#: 认得出"是服务器异常页"而不是我们自己的友好提示
BOOM = ("Internal Server Error", "错误编号", "Traceback")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟随 303 —— 我们要看的就是那个 303 本身。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(), _NoRedirect())


def _open(op, req, timeout: float = 300.0):
    try:
        r = op.open(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace"), r.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location")


def _get(op, url: str):
    return _open(op, urllib.request.Request(url), timeout=30.0)


def _post_form(op, url: str, data: dict):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _open(op, req, timeout=30.0)


def _post_multipart(op, url: str, fields: dict, files: list[tuple[str, str, bytes]]):
    """手搓 multipart —— 不想为了一个探针引入 requests 依赖。"""
    boundary = "----gfvfwprobe" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, k, v)).encode()
    for name, filename, data in files:
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"; '
                'filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                % (boundary, name, filename)).encode()
        out += data
        out += b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        url, data=bytes(out),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    return _open(op, req, timeout=300.0)


def _excerpt(body: str, needle: str = "flash", span: int = 400) -> str:
    i = body.find(needle)
    return body[i:i + span] if i >= 0 else body[:span]


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "http://127.0.0.1:18081"
    password = argv[2] if len(argv) > 2 else None
    cam = Path(argv[3]) if len(argv) > 3 else None

    if password is None:
        print("需要一个登录密码作为第 2 个参数（web 登录名固定为 admin）")
        return 2

    op = _opener()

    status, html, _ = _get(op, base + "/login")
    m = CSRF.search(html)
    if not m:
        print("FAIL  拿不到登录页 / CSRF（HTTP %d）" % status)
        return 1
    status, _html, _ = _post_form(
        op, base + "/login",
        {"username": "admin", "password": password, "csrf_token": m.group(1)})
    print("登录            -> %d" % status)
    if status != 303:
        print("FAIL  登录没有 303（密码错？）")
        return 1

    status, html, _ = _get(op, base + "/theater/upload")
    print("上传页          -> %d" % status)
    if status != 200:
        print("FAIL  打不开上传页 —— " + _excerpt(html))
        return 1
    m = CSRF.search(html)
    if not m:
        print("FAIL  上传页里没有 CSRF 隐藏域")
        return 1

    if cam is None:
        print("（未给 .cam 路径，只验证到页面可打开为止）PASS")
        return 0

    if not cam.is_file():
        print("FAIL  %s 不存在" % cam)
        return 1

    data = cam.read_bytes()
    status, body, loc = _post_multipart(
        op, base + "/theater/upload",
        {"csrf_token": m.group(1), "campaign_id": ""},
        [("file", cam.name, data)])
    print("上传 %s（%d 字节） -> %d  Location=%s"
          % (cam.name, len(data), status, loc))

    if status == 500 or any(k in body for k in BOOM):
        print("FAIL  ★ 得到的是服务器异常页，不是可读提示：")
        print("      " + _excerpt(body, "错误编号", 300).replace("\n", " ")[:300])
        return 1
    if status != 303:
        # 解析失败也是可读的 400 —— 把提示原样打出来，便于判断是哪一类失败
        print("注意  非 303（HTTP %d），页面提示：" % status)
        print("      " + _excerpt(body).replace("\n", " ")[:300])
        return 1

    target = loc if (loc or "").startswith("/") else "/theater"
    status, html, _ = _get(op, base + target)
    print("跳转目标        -> %d  %s" % (status, target))
    if status != 200:
        print("FAIL  跳转目标打不开")
        return 1

    print("PASS  —— 上传链路正常，没有出现服务器异常页")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
