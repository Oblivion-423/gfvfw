"""列出所有路由及其权限守卫（只读，用于规划"列表公开 / 详情队内"的边界）。

⚠️ 本项目的 FastAPI/Starlette 版本用 ``_IncludedRouter`` 包装 include_router，
   所以 ``app.routes`` 里看不到具体路径 —— 必须逐个 router 模块取。

⚠️ 守卫判定**不能**直接对函数源码做子串搜索：``/register`` 的 docstring 里
   写了「本页本身就必须对未登录用户开放 …… 而不是 ``require_login``」，
   于是 ``"require_login" in src`` 会把一个真·公开页误报成需要登录。
   所以这里先从签名里取真正的 ``Depends(...)`` 依赖（可靠），
   再对**剥掉字符串与注释**后的源码做正则兜底。
"""
import importlib
import inspect
import io
import re
import sys
import tokenize
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
#
# ⚠️ ``\s*`` 必须写在括号**前面**：:func:`strip_literals` 用空格拼接 token，
#    源码变成 ``Depends ( require ( LOG_VIEW ) )``，
#    只写 ``Depends\(`` 会一条都匹配不到。
GUARD_BARE = re.compile(r"Depends\s*\(\s*(require_member|require_login)\s*\)")
GUARD_FACTORY = re.compile(
    r"Depends\s*\(\s*(require_any|require)\s*\(([^)]*)\)?\s*\)")


def strip_literals(src: str) -> str:
    """去掉字符串字面量与注释，避免 docstring 被当成代码。

    ⚠️ 这正是本脚本曾经的 bug：``/register`` 的 docstring 里提到
    ``require_login``，于是这个公开页被报成"需要登录"。
    """
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 拿不到 token 流时退化为原文本（宁可多报，不可漏报）
        return src
    return " ".join(out)


def guard_from_signature(ep) -> str | None:
    """从函数签名里读真正的依赖对象 —— 这是权威来源，不看文本。"""
    try:
        sig = inspect.signature(ep)
    except (TypeError, ValueError):
        return None
    try:
        from fastapi import params as fparams
    except ImportError:                       # pragma: no cover
        return None
    for p in sig.parameters.values():
        d = p.default
        if not isinstance(d, fparams.Depends) or d.dependency is None:
            continue
        dep = d.dependency
        name = getattr(dep, "__name__", "")
        if name in ("require_member", "require_login"):
            return name
        qual = getattr(dep, "__qualname__", "")
        if qual.startswith("require_any.") or qual.startswith("require."):
            # 工厂返回的闭包：权限点只能从源码里读
            return "factory:" + qual.split(".")[0]
    return None


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
            code = strip_literals(src)
            f = GUARD_FACTORY.search(code)
            by_sig = guard_from_signature(ep)
            if by_sig in ("require_member", "require_login"):
                guard = by_sig
            elif f:
                guard = "%s(%s)" % (f.group(1), f.group(2).strip().rstrip(")"))
            elif by_sig is not None and by_sig.startswith("factory:"):
                # 签名确认有守卫，但源码里没匹配到 ⟶ 明确标出来，别当"公开"
                guard = "?? 有守卫但读不出权限点"
            elif "get_principal" in code:
                guard = "— 无守卫（仅取身份）"
            elif code:
                guard = "— 无守卫（公开）"
        rows.append((path, methods, guard, name))

rows.sort()
print("%-46s %-10s %-40s %s" % ("PATH", "METHODS", "GUARD", "MODULE"))
print("-" * 120)
for p, m, g, n in rows:
    print("%-46s %-10s %-40s %s" % (p, m, g, n))
print("\n共 %d 条路由" % len(rows))
