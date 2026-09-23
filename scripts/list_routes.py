"""列出所有路由及其权限守卫（只读，用于规划公开/队内的边界）。

⚠️ 本项目的 FastAPI/Starlette 版本用 ``_IncludedRouter`` 包装 include_router，
   所以 ``app.routes`` 里看不到具体路径 —— 必须逐个 router 模块取。
"""
import importlib
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = [
    "home", "auth", "apply", "applications", "account", "logbook", "members",
    "acmi", "missions", "sorties", "campaigns", "theater", "stats",
    "placeholders",
]
# ⚠️ 两种用法都要认：
#   Depends(require_member)            —— 直接引用守卫函数（本身即依赖，无参数）
#   Depends(require(LOG_VIEW))         —— 工厂：require(权限点...) 返回依赖
# 早先只写了后者的模式，于是 require_member 全被误报成"无守卫"。
GUARD_BARE = re.compile(r"Depends\(\s*(require_member|require_login)\s*\)")
GUARD_FACTORY = re.compile(
    r"Depends\(\s*(require_any|require)\s*\(([^)]*)\)?\s*\)")

rows = []
for name in MODULES:
    mod = importlib.import_module("gfvfw.web.routers.%s" % name)
    for r in mod.router.routes:
        path = getattr(r, "path", None)
        methods = ",".join(sorted(getattr(r, "methods", None) or []))
        ep = getattr(r, "endpoint", None)
        guard = "??"
        if ep is not None:
            try:
                src = inspect.getsource(ep)
            except (OSError, TypeError):
                src = ""
            m = GUARD_BARE.search(src)
            f = GUARD_FACTORY.search(src)
            if m:
                guard = m.group(1)
            elif f:
                guard = "%s(%s)" % (f.group(1), f.group(2).strip().rstrip(")"))
            elif "require_login" in src:
                guard = "require_login"
            elif "get_principal" in src:
                guard = "— 无守卫（仅取身份）"
            elif src:
                guard = "— 无守卫（公开）"
        rows.append((path, methods, guard, name))

rows.sort()
print("%-46s %-10s %-40s %s" % ("PATH", "METHODS", "GUARD", "MODULE"))
print("-" * 120)
for p, m, g, n in rows:
    print("%-46s %-10s %-40s %s" % (p, m, g, n))
print("\n共 %d 条路由" % len(rows))
