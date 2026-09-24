"""实况核对：**在「战役管理」(/theater) 里真的能把战役删掉并恢复**。

为什么单独写这个脚本：用户反馈"战役管理中仍然不能删除战役"，而作废 handler
其实一直都在（挂在 `/campaigns` 下）—— 问题在于**入口不在用户用的那一页**。
所以这里按用户的实际路径走一遍：

    /theater 列表 → 找到「作废」按钮 → POST 作废 → 战役从列表消失
    → /theater?deleted=1 → 「恢复」→ 战役回到列表

全程走真实 HTTP（Cookie / CSRF / 表单 / 重定向），不做任何数据库直连。

⚠️ 会**真的作废又恢复**一个战役。恢复后战役本身回到原状，但**它下面的任务
   不会自动归回**（`campaign_id` 被置空，这是有意的设计：作废时没有记录那些
   任务原本属于谁）。所以跑完这个探针之后，那个快照里的「战役记录」会少掉
   该战役的任务 —— **再跑别的实况核查前请重新生成快照**：

       .venv\\Scripts\\python.exe scripts/make_probe_snapshot.py <密码>

用法::

    .venv\\Scripts\\python.exe scripts/theater_delete_probe.py http://127.0.0.1:18081 <密码>
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
#: 列表行里的「作废」表单（action 指向 /campaigns/<id>/delete）
DEL_FORM = re.compile(r'/campaigns/([0-9a-fA-F-]{36})/delete')
RESTORE_FORM = re.compile(r'/campaigns/([0-9a-fA-F-]{36})/restore')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18081"
    password = sys.argv[2] if len(sys.argv) > 2 else "Theater-Del-2026"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(),
                                     _NoRedirect())
    fails: list[str] = []

    def get(url: str) -> tuple[int, str]:
        try:
            r = op.open(url, timeout=60)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(url: str, data: dict) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            r = op.open(req, timeout=60)
            return r.status, (r.headers.get("Location") or "")
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("Location") or "")

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                              "" if ok else "  ← " + detail))
        if not ok:
            fails.append(label)

    st, html = get(base + "/login")
    m = CSRF.search(html.encode())
    st, _ = post(base + "/login", {"username": "admin", "password": password,
                                  "csrf_token": m.group(1).decode()})
    check("登录", st == 303, "status=%d" % st)

    # ── 1. 战役管理列表上有「作废」按钮吗 ────────────────────────────
    st, html = get(base + "/theater")
    check("GET /theater 200", st == 200, "status=%d" % st)
    del_ids = DEL_FORM.findall(html)
    check("★ 战役管理列表里有「作废」入口（这就是用户找不到的那个）",
          bool(del_ids), "页面里没有 /campaigns/<id>/delete 表单")
    if not del_ids:
        return 1
    cid = del_ids[0]
    check("★ 列表里有「显示已作废」入口", "theater?deleted=1" in html)

    # ── 2. 详情页上也有吗 ────────────────────────────────────────────
    st, html = get(base + "/theater/%s" % cid)
    check("GET /theater/<id> 200", st == 200, "status=%d" % st)
    check("★ 战役详情页里有「作废此战役」按钮", "作废此战役" in html)
    check("详情页有「再上报一份存档」（can_upload 真的传进来了）",
          "再上报一份存档" in html or "去上报一份" in html)

    # ── 3. 真的作废 ──────────────────────────────────────────────────
    tok = CSRF.search(html.encode()).group(1).decode()
    st, loc = post(base + "/campaigns/%s/delete" % cid,
                   {"csrf_token": tok})
    check("★ POST 作废 → 303", st == 303, "status=%d" % st)
    check("提示里说明是软删除且可恢复",
          "恢复" in urllib.parse.unquote(loc), urllib.parse.unquote(loc)[:120])

    st, html = get(base + "/theater")
    check("★ 作废后：战役从战役管理列表消失", cid not in html,
          "还在列表里 —— 作废没生效")

    # ── 4. 已作废视图 + 恢复 ─────────────────────────────────────────
    st, html = get(base + "/theater?deleted=1")
    check("GET /theater?deleted=1 200", st == 200, "status=%d" % st)
    res_ids = RESTORE_FORM.findall(html)
    check("★ 已作废视图里能看到它", cid in res_ids, "没找到该战役")
    tok = CSRF.search(html.encode()).group(1).decode()
    st, loc = post(base + "/campaigns/%s/restore" % cid, {"csrf_token": tok})
    check("★ POST 恢复 → 303", st == 303, "status=%d" % st)

    st, html = get(base + "/theater")
    check("★ 恢复后：战役回到战役管理列表", cid in html or
          "作废" in html and cid in DEL_FORM.findall(html),
          "没回到列表")

    st, html = get(base + "/theater?deleted=1")
    check("★ 恢复后不再出现在已作废视图里", cid not in RESTORE_FORM.findall(html))

    print()
    print("=" * 70)
    print("战役管理里删战役：%s" % ("PASS" if not fails else
                                  "FAIL（%d 项）" % len(fails)))
    for f in fails:
        print("  FAILED:", f)
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
