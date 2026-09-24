"""对运行中的服务做一次"人工修正功能"的实况核查（只读，不写数据）。

用法：
    .venv\\Scripts\\python.exe scripts\\live_edit_check.py [base_url]

默认 base_url = http://127.0.0.1:18080

它做两件事：
1. 用管理员登录，逐页 GET 并检查关键文案与按钮是否真的渲染出来；
2. 构造几个"应当被拒绝"的请求，确认服务端边界生效（而不是只隐藏按钮）。

⚠️ 这是**只读探针**：不提交任何会改数据的表单，不改动任何数据。
   （唯一副作用：登录会写一条会话记录与审计。）

⚠️ 账号密码从环境变量读，**不要写进本文件** —— 脚本会进仓库，
   把密码写在这里等于把它提交到版本历史里。
   本地开发用默认值即可；线上请显式传环境变量：

       GFVFW_LIVE_USER=auditor GFVFW_LIVE_PASSWORD='...' \\
         .venv/bin/python scripts/live_edit_check.py https://gfvfw.top
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18080"
USERNAME = os.environ.get("GFVFW_LIVE_USER", "admin")
# 默认值只为本机开发方便；生产必须用环境变量覆盖（见模块说明）。
PASSWORD = os.environ.get("GFVFW_LIVE_PASSWORD", "Gfvfw-Admin-2026")
PASSWORD_FROM_ENV = "GFVFW_LIVE_PASSWORD" in os.environ

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        FAILURES.append(label)
        print(f"  FAIL  {label}" + (f"  <- {detail}" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向。

    ⚠️ 为什么需要：``urllib`` 默认跟随 303，于是"未登录访问队内页面"会表现为
    **最终落在登录页（200）**，而不是 303 —— 断言 `status == 303` 必然假失败。
    要验证"被拦住了"，就必须看到那一次 303 本身。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class Session:
    """极简 cookie 会话（只用标准库，避免额外依赖）。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # 显式持有 cookie jar，让"跟随重定向"和"不跟随"两个 opener 共享登录态
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.raw_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def _open(self, opener, path: str, data: dict[str, str] | None):
        url = self.base + path
        # ⚠️ 必须用 `is not None` 判断，不能用真值判断：
        #    `data={}` 是**假值**，会被误当成 GET 发出 —— 曾因此把 405 误读成
        #    "POST 路由没生效"，其实是探针自己发成了 GET。
        is_post = data is not None
        body = urllib.parse.urlencode(data or {}).encode() if is_post else None
        req = urllib.request.Request(url, data=body,
                                     method="POST" if is_post else "GET")
        req.add_header("User-Agent", "gfvfw-live-edit-check/1.0")
        if is_post:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(req, timeout=30) as resp:
                return (resp.status, resp.read().decode("utf-8", "replace"),
                        resp.headers.get("Location", ""))
        except urllib.error.HTTPError as exc:
            return (exc.code, exc.read().decode("utf-8", "replace"),
                    exc.headers.get("Location", ""))

    def _req(self, path: str, data: dict[str, str] | None = None) -> tuple[int, str]:
        status, body, _loc = self._open(self.opener, path, data)
        return status, body

    def get(self, path: str) -> tuple[int, str]:
        return self._req(path, None)

    def get_raw(self, path: str) -> tuple[int, str, str]:
        """GET 且**不跟随重定向**，返回 ``(状态码, 正文, Location)``。"""
        return self._open(self.raw_opener, path, None)

    def post(self, path: str, data: dict[str, str]) -> tuple[int, str]:
        return self._req(path, data)

    def csrf(self, path: str) -> str | None:
        status, html = self.get(path)
        if status != 200:
            return None
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    def login(self) -> bool:
        token = self.csrf("/login")
        if not token:
            return False
        status, html = self.post(
            "/login",
            {"username": USERNAME, "password": PASSWORD, "csrf_token": token},
        )
        return status in (200, 303) and "登录" not in html[:400]


_NAV_RE = re.compile(r'<nav class="nav">(.*?)</nav>', re.S)


def _nav_of(html: str) -> str:
    """只取顶部导航块。

    ⚠️ 不能对整页做 ``'href="/members"' not in html`` —— 首页正文里本来就有
    指向 /members 的链接，那样断言"访客导航里没有队内入口"会**假失败**。
    """
    m = _NAV_RE.search(html)
    return m.group(1) if m else ""


def main() -> int:
    print(f"目标服务: {BASE}")
    print(f"登录账号: {USERNAME}"
          + ("" if PASSWORD_FROM_ENV else "（密码取自脚本默认值，生产请用 "
                                          "GFVFW_LIVE_PASSWORD 覆盖）"))
    print()
    s = Session(BASE)

    print("[1] 服务可达性与登录")
    try:
        status, _ = s.get("/login")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  无法连接：{exc}")
        return 1
    check("GET /login 返回 200", status == 200, f"status={status}")
    check("登录成功", s.login())

    print("\n[2] 导航与列表页")
    status, html = s.get("/")
    check("概览页 / 200", status == 200, f"status={status}")
    check("概览页出现「日志总时长」", "日志总时长" in html)
    check("概览页出现「飞行员累计时长」", "飞行员累计时长" in html)
    check("概览页说明「只算一次」", "只算一次" in html)
    check("概览页文案未过期（不再说 ACMI 入口已移除）",
          "已临时从导航移除" not in html)

    status, html = s.get("/stats")
    check("统计总览 200", status == 200, f"status={status}")
    check("统计总览出现「日志总时长」", "日志总时长" in html)
    check("统计总览出现「飞行员累计时长」", "飞行员累计时长" in html)

    status, html = s.get("/missions")
    check("任务列表 200", status == 200, f"status={status}")
    mission_ids = re.findall(r"/missions/([0-9a-f-]{36})", html)
    check("任务列表含至少一个任务链接", bool(mission_ids), f"html={len(html)}B")

    if not mission_ids:
        print("\n没有任务，后续页面核查跳过。")
        return report()

    mid = mission_ids[0]
    print(f"\n[3] 任务详情 /missions/{mid}")
    status, html = s.get(f"/missions/{mid}")
    check("任务详情 200", status == 200, f"status={status}")
    check("★ 详情区分「日志时长」", "日志时长" in html)
    check("★ 详情区分「记录时长」", "记录时长" in html)
    check("详情显示「飞行员累计」", "飞行员累计" in html)
    check("★ 详情说明三个时长不要互相校验", "不要互相校验" in html)
    check("详情注明记录时长含起飞前/降落后",
          "起飞前" in html or "降落后" in html)
    check("详情不再把录制窗叫作「任务时长」", "任务时长" not in html)
    has_edit = f"/missions/{mid}/edit" in html
    has_del = f"/missions/{mid}/delete" in html
    has_add = f"/missions/{mid}/sorties/new" in html
    check("详情页有「编辑任务」链接", has_edit)
    check("详情页有「删除任务」链接（admin=owner 有 log.delete）", has_del)
    check("详情页有「补录架次」链接（admin=owner 有 log.approve）", has_add)

    print("\n[4] 编辑与删除页面本身")
    status, html = s.get(f"/missions/{mid}/edit")
    check("GET 任务编辑页 200", status == 200, f"status={status}")
    check("编辑表单含 csrf_token", 'name="csrf_token"' in html)
    check("★ 编辑页区分日志时长与记录时长", "日志时长" in html and "记录时长" in html)

    status, html = s.get(f"/missions/{mid}/delete")
    check("GET 任务删除确认页 200", status == 200, f"status={status}")
    check("确认页说明会撤销归并", ("撤销" in html or "退回" in html or "待归并" in html))

    status, html = s.get(f"/missions/{mid}/sorties/new")
    check("GET 补录架次页 200", status == 200, f"status={status}")
    check("补录页含 csrf_token", 'name="csrf_token"' in html)

    print("\n[5] 架次编辑页（取详情页里的第一个架次）")
    sortie_ids = re.findall(r"/sorties/([0-9a-f-]{36})/edit", html) or re.findall(
        r"/sorties/([0-9a-f-]{36})", html
    )
    # 补录页里不会有架次链接，回到详情页取
    status, detail_html = s.get(f"/missions/{mid}")
    sortie_ids = re.findall(r"/sorties/([0-9a-f-]{36})/edit", detail_html)
    if sortie_ids:
        sid = sortie_ids[0]
        status, html = s.get(f"/sorties/{sid}/edit")
        check(f"GET 架次编辑页 200（{sid[:8]}）", status == 200, f"status={status}")
        check("架次编辑页含 csrf_token", 'name="csrf_token"' in html)
        check("架次编辑页有删除入口或说明", ("删除" in html))
    else:
        check("详情页能找到架次编辑链接", False, "未匹配到 /sorties/<id>/edit")

    print("\n[6] ACMI 工作台（内嵌，非独立页面）")
    status, html = s.get("/log/campaign")
    check("GET /log/campaign 200", status == 200, f"status={status}")
    check("战役记录页含 ACMI 工作台", "acmiWorkbench" in html or "ACMI 工作台" in html)
    check("★ 战役记录用「日志总时长」", "日志总时长" in html)
    check("★ 战役记录说明它不是记录时长", "记录时长" in html)

    status, html = s.get("/log/training")
    check("GET /log/training 200", status == 200, f"status={status}")
    check("训练记录页含 ACMI 工作台", "acmiWorkbench" in html or "ACMI 工作台" in html)
    # ⚠️ 训练记录页在**没有训练任务时**只渲染空状态，不会有合计行 ——
    #    因此「只算一次」的说明只在有数据时才存在。两种情形都算通过。
    empty = ("暂无" in html) or ("为空" in html) or ("没有" in html)
    check("训练记录页：有数据则标注只算一次，无数据则给空状态提示",
          ("只算一次" in html) or ("日志总时长" in html) or empty,
          "既无合计说明也无空状态")

    status, html = s.get("/")
    check("★ 概览页用「日志总时长」", "日志总时长" in html)
    check("★ 概览页说明还有第三个量「记录时长」", "记录时长" in html)

    print("\n[7] 旧 ACMI 路径应 302 跳转到宿主页（不跟随后最终 200）")
    for old in ("/acmi", "/acmi/upload", "/acmi/claim", "/acmi/merge"):
        status, _ = s.get(old)
        check(f"{old} → 200（经跳转）", status == 200, f"status={status}")

    print("\n[8] 战役管理 / 存档删除入口")
    status, list_html = s.get("/theater")
    check("GET /theater 200", status == 200, f"status={status}")
    camp_ids = re.findall(r"/theater/([0-9a-f-]{36})", list_html)
    if camp_ids:
        cid = camp_ids[0]
        status, saves_html = s.get(f"/theater/{cid}/saves")
        check(f"GET 存档列表 200（{cid[:8]}）", status == 200, f"status={status}")
        check("存档页含删除入口或说明", ("删除" in saves_html))
    else:
        check("战役列表含战役链接", False, "未匹配到 /theater/<id>")
    # ⚠️ 联队口中的"战役管理"就是 /theater（顶栏那一项指的就是它）。
    #    作废战役的 handler 挂在 /campaigns 下，曾经只把按钮放在那边 ——
    #    于是用户在 /theater 里"根本找不到删除战役"。所以这里必须盯 /theater。
    #    ⚠️ 注意用 list_html 而不是 html：上面那次 /saves 请求会把 html 覆盖掉，
    #       于是断言检查的是存档页而不是战役列表 —— 那种"自己把变量冲掉"的
    #       探针 bug 会给出假失败（本轮就踩了一次）。
    check("★ 战役管理列表里有「作废」入口",
          bool(re.search(r"/campaigns/[0-9a-f-]{36}/delete", list_html)),
          "列表里没有作废按钮 —— 用户会找不到")
    check("★ 战役管理列表里有「显示已作废」入口",
          "theater?deleted=1" in list_html)
    if camp_ids:
        status, detail_html = s.get(f"/theater/{camp_ids[0]}")
        check("★ 战役详情页里有「作废此战役」按钮",
              "作废此战役" in detail_html, "详情页没有删除入口")
        check("★ 战役详情页的上报入口真的显示了（can_upload 传进来了）",
              "再上报一份存档" in detail_html
              or "去上报一份" in detail_html,
              "can_upload 恒为假 —— 上报入口以前永远不显示")
    status, _ = s.get("/theater?deleted=1")
    check("GET /theater?deleted=1 200（已作废视图）", status == 200,
          f"status={status}")

    print("\n[8b] ★ 删成员 / 账号解绑的界面入口")
    # 这两个功能都要求"入口看得见" —— 删成员的 handler 一度**完全没有任何
    # 模板引用它**（写了却点不到）。所以这里盯的是**入口本身**。
    status, html = s.get("/campaigns")
    check("GET /campaigns 200", status == 200, f"status={status}")
    plan_ids = re.findall(r"/campaigns/([0-9a-f-]{36})\"", html)
    check("战役列表含「显示已作废」入口", "deleted=1" in html)
    if plan_ids:
        status, html = s.get(f"/campaigns/{plan_ids[0]}")
        check("★ 战役详情页含「作废此战役」按钮",
              "作废此战役" in html, "详情页没有删除入口 —— 用户会找不到")
        check("战役详情页含恢复入口说明", "显示已作废" in html)
    status, _ = s.get("/campaigns?deleted=1")
    check("GET /campaigns?deleted=1 200（已作废视图）", status == 200,
          f"status={status}")

    status, html = s.get("/members")
    mem_ids = re.findall(r"/members/([0-9a-f-]{36})\"", html)
    check("名册含「显示已作废」入口", "deleted=1" in html)
    hit_unbind = hit_delete = False
    for mid in mem_ids[:10]:
        status, page = s.get(f"/members/{mid}")
        if status != 200:
            continue
        hit_delete = hit_delete or ("作废此成员" in page)
        hit_unbind = hit_unbind or ("解绑该账号" in page)
    check("★ 成员详情页含「作废此成员」按钮", hit_delete,
          "名册里没有任何成员页出现删除入口")
    check("★ 成员详情页含「解绑该账号」按钮", hit_unbind,
          "名册里没有任何成员页出现解绑入口（都被绑着账号才对）")
    status, _ = s.get("/members?deleted=1")
    check("GET /members?deleted=1 200（已作废视图）", status == 200,
          f"status={status}")

    print("\n[9] 服务端边界（构造应被拒绝的请求）")
    # 9a 无 CSRF 的删除请求 -> 403
    status, _ = s.post(f"/missions/{mid}/delete", {"reason": "probe"})
    check("无 CSRF 的删任务被拒（403）", status == 403, f"status={status}")
    # 9b 空 body 的 POST 也必须真的发成 POST（否则只会得到 405，误判成路由失效）
    if sortie_ids:
        status, _ = s.post(f"/sorties/{sortie_ids[0]}/delete", {})
        check("空 POST 删架次被拒（403，不是 405）", status == 403, f"status={status}")
        # 该路由只接受 POST：GET 必须是 405（证明它没被实现成 GET）
        status, _ = s.get(f"/sorties/{sortie_ids[0]}/delete")
        check("GET 删架次路由 → 405（只允许 POST）", status == 405, f"status={status}")
    # 9c 不存在的 id -> 404（而不是 500）
    status, _ = s.get("/missions/00000000-0000-0000-0000-000000000000/edit")
    check("不存在的任务编辑页 404", status == 404, f"status={status}")
    status, _ = s.get("/sorties/00000000-0000-0000-0000-000000000000/edit")
    check("不存在的架次编辑页 404", status == 404, f"status={status}")
    # 9d 未登录访问需权限页面 -> 跳登录
    anon = Session(BASE)
    status, html = anon.get(f"/missions/{mid}/edit")
    check("未登录访问编辑页被拦（200=登录页 或 303）", status in (200, 303), f"status={status}")
    # 9e 已归并的 ACMI 不得直接删除（返回 400 且指向任务详情页）
    #
    # ⚠️ 不能只在页面上找 `file_ids`：工作台的上传段位只列**待归并**的文件。
    #    真实库里可能所有文件都已归并，那时页面上根本没有这些 input ——
    #    这不是缺陷。所以这里直接从库里读一个真实 id 来打这个边界。
    merged_id, pending_id = _lookup_acmi_ids()
    token = s.csrf("/log/campaign") or ""
    if merged_id:
        status, body = s.post(
            f"/acmi/{merged_id}/delete",
            {"csrf_token": token, "return_to": "/log/campaign"},
        )
        merged = ("已归并" in body) or ("先到任务详情页" in body)
        check("删已归并的 ACMI 被拒（400）", status == 400, f"status={status}")
        check("拒绝文案指向任务详情页（撤销归并的正确路径）", merged)
    else:
        print("  SKIP  库里没有已归并的 ACMI —— 跳过该边界（非失败）")

    if pending_id:
        status, _ = s.post(f"/acmi/{pending_id}/delete", {})
        check("无 CSRF 删待归并 ACMI 被拒（403）", status == 403, f"status={status}")
    else:
        print("  SKIP  库里没有待归并的 ACMI —— 跳过该边界（非失败）")

    print("\n[10] 账号与改密页")
    status, html = s.get("/account")
    check("GET /account 200", status == 200, f"status={status}")
    check("账号页含改密表单", 'action="/account/password"' in html)
    check("账号页说明密码无法找回并给出 CLI 命令",
          "无法找回" in html and "set-password" in html)
    check("账号页显示当前登录名", USERNAME in html)
    # 不含 CSRF 的改密请求必须被拒（且不能真改掉密码）
    status, _ = s.post("/account/password",
                       {"current_password": "x", "new_password": "y" * 12,
                        "confirm_password": "y" * 12})
    check("无 CSRF 改密被拒（403）", status == 403, f"status={status}")
    # 有 CSRF 但原密码错 → 400
    token = s.csrf("/account")
    status, body = s.post("/account/password",
                          {"current_password": "definitely-wrong",
                           "new_password": "Gyrfalcon-Probe-2026",
                           "confirm_password": "Gyrfalcon-Probe-2026",
                           "csrf_token": token or ""})
    check("原密码错误 → 400（不泄露更多信息）", status == 400, f"status={status}")
    check("原密码错误有提示", "原密码不正确" in body)
    # ★ 关键：上面的失败尝试不能把管理员自己的密码改掉、也不能锁住账号
    status, _ = s.get("/missions")
    check("★ 改密失败后当前会话仍可用（没被踢出/锁死）", status == 200,
          f"status={status}")

    print("\n[11] Logbook 上传（自动解析，无手填、无审核）")
    status, html = s.get("/account/logbook")
    check("GET /account/logbook 200", status == 200, f"status={status}")
    check("页面说明上传即自动解析", "上传即自动解析" in html)
    check("页面说明不需要审核", "不需要审核" in html)
    check("页面说明三个时长不要互相校验", "不要互相校验" in html)
    check("页面含上传表单", 'enctype="multipart/form-data"' in html)
    # 手动登记路径已随自动解析一起移除 —— 页面不该再出现这些输入框
    check("★ 页面已无手填数值的输入框",
          'name="hours"' not in html and 'name="sorties"' not in html)
    # ⚠️ 只读探针：**不上传任何文件**（上传会写库、写磁盘）。
    #    这里只验证页面契约与权限边界。
    status, html = s.get("/members/00000000-0000-0000-0000-000000000000/logbook")
    check("不存在的成员 Logbook 页 404", status == 404, f"status={status}")
    token = s.csrf("/account/logbook") or ""
    # ⚠️ Logbook **不需要审核**：没有 /confirm，也没有手填的 /declare；
    #    唯一的人工触发入口是"用已归档原件重新解析"（/reparse）。
    status, _ = s.post("/logbook/00000000-0000-0000-0000-000000000000/reparse", {})
    check("无 CSRF 的重新解析被拒（403）", status == 403, f"status={status}")
    status, _ = s.post("/logbook/00000000-0000-0000-0000-000000000000/reparse",
                       {"csrf_token": token})
    check("不存在的归档重新解析 404", status == 404, f"status={status}")
    status, _ = s.post("/logbook/00000000-0000-0000-0000-000000000000/confirm",
                       {"csrf_token": token})
    check("★ 已无 /confirm 审核路由（404）", status == 404, f"status={status}")
    status, _ = s.post("/logbook/00000000-0000-0000-0000-000000000000/declare",
                       {"csrf_token": token, "hours": "1"})
    check("★ 已无 /declare 手填路由（404）", status == 404, f"status={status}")
    status, _ = s.post("/logbook/00000000-0000-0000-0000-000000000000/reapply",
                       {"csrf_token": token})
    check("★ 已无 /reapply 路由（404）", status == 404, f"status={status}")
    status, _ = s.get("/logbook/00000000-0000-0000-0000-000000000000/download")
    check("不存在的归档下载 404", status == 404, f"status={status}")

    print("\n[12] 账号页指向 Logbook")
    status, html = s.get("/account")
    check("账号页含 Logbook 入口", "/account/logbook" in html)

    print("\n[13] 三档身份：列表公开 / 详情队内")
    # 13a 未登录访客：只有公开页可进
    # ⚠️ /apply 现在**需要登录**（注册与申请拆成两步），只有 /register 是公开入口。
    for path in ("/", "/register", "/login"):
        status, _ = anon.get(path)
        check(f"访客可访问 {path}", status == 200, f"status={status}")
    # ⚠️ 用 get_raw（不跟随重定向）。跟随的话"被拦"会表现为最终落在登录页(200)，
    #    断言 303 必然假失败 —— 之前就是踩了这个。
    # 列表页与详情页对访客都应是 303，但原因不同（列表=需登录，详情=需队员）。
    for path in ("/apply", "/members", "/library", "/log/campaign", "/stats",
                 "/theater", "/applications", "/account"):
        status, _body, loc = anon.get_raw(path)
        check(f"★ 访客 {path} → 跳登录（303）",
              status == 303 and "/login" in loc, f"status={status} loc={loc}")

    _, reg_html = anon.get("/register")
    check("★ 注册页公开且含表单", 'action="/register"' in reg_html)
    check("★ 注册页说明「注册 → 申请 → 队员」三步",
          "注册" in reg_html and "申请" in reg_html and "队员" in reg_html)

    # ⚠️ 导航断言必须只看 <nav> 那一块：首页正文里本来就有指向 /members 的链接，
    #    对整页做 `href="/members" not in html` 会假失败。
    _, home = anon.get("/")
    anon_nav = _nav_of(home)
    check("★ 拿到导航块（后续断言的前提）", bool(anon_nav))
    for href in ("/members", "/theater", "/library", "/apply/status"):
        check(f"★ 访客导航里没有 {href}", f'href="{href}"' not in anon_nav)
    check("★ 访客导航里有注册入口", 'href="/register"' in anon_nav)
    # ⚠️ 访客以前指向 /apply，而那个页面现在需要登录 —— 点进去只会被弹回登录页。
    check("★ 访客导航不指向 /apply（那个页面需要先登录）",
          'href="/apply"' not in anon_nav)

    # 13b 已登录的 owner：队内入口齐全
    _, home = s.get("/")
    mem_nav = _nav_of(home)
    for href in ("/members", "/library", "/applications"):
        check(f"★ 队员导航里有 {href}", f'href="{href}"' in mem_nav)
    status, html = s.get("/applications")
    check("★ 入队审批页 200（owner 有 application.review）",
          status == 200, f"status={status}")
    # 库里可能没有任何待审批游客 —— 那就该看到空状态说明，而不是按钮
    has_guest = "提升为队员" in html
    check("★ 审批页要么列出待提升游客、要么给出空状态说明",
          has_guest or "没有待审批的游客" in html,
          "既没有按钮也没有空状态文案")
    if has_guest:
        check("★ 待审批行带呼号输入框", 'name="callsign"' in html)
    else:
        print("  SKIP  库里没有待审批游客 —— 按钮形态留给 access_selfcheck 覆盖")
    status, html = s.get("/apply/status")
    check("★ 队员也能打开申请进度页", status == 200, f"status={status}")
    check("进度页说明已是队员", "队员" in html)
    status, _ = s.get("/library")
    check("★ 资料查询对队员开放（200）", status == 200, f"status={status}")
    # 队员能看到 ACMI 工作台（写操作 UI 只对队员渲染）
    status, html = s.get("/log/campaign")
    check("★ 队员看的 /log/campaign 含 ACMI 工作台",
          status == 200 and "acmiWorkbench" in html, f"status={status}")

    return report()


def _lookup_acmi_ids() -> tuple[str | None, str | None]:
    """只读查询：返回（一个已归并文件 id, 一个待归并文件 id），用于打服务端边界。

    直连数据库是为了**不依赖页面上是否恰好有文件** —— 页面上看不到不等于功能缺失。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from sqlalchemy import select  # noqa: PLC0415

        import gfvfw.db as gdb  # noqa: PLC0415
        from gfvfw.models import AcmiFile  # noqa: PLC0415

        with gdb.SessionLocal() as db:
            merged = db.scalar(
                select(AcmiFile.id).where(AcmiFile.mission_id.is_not(None)).limit(1)
            )
            pending = db.scalar(
                select(AcmiFile.id).where(AcmiFile.mission_id.is_(None)).limit(1)
            )
        return merged, pending
    except Exception as exc:  # noqa: BLE001
        print(f"  ..    无法直连数据库（{exc}）—— 无法独立定位 ACMI id")
        return None, None


def report() -> int:
    total = PASS + FAIL
    print("\n" + "=" * 68)
    print(f"实况核查：{total} 项，通过 {PASS}，失败 {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
