"""静态体检：**模板里用到的开关变量，渲染它的路由到底有没有传？**

为什么需要这个脚本
------------------
Jinja 里未定义的变量在 ``{% if %}`` 里求值为假 —— **不报错、不渲染任何东西**。
于是"入口写好了却永远不显示"这种 bug 完全静默：

* ``theater/detail.html`` 用了 ``can_upload``，但只有 ``theater_index`` 设置过它
  ⟹ 战役详情页上那两个"去上报一份 / 再上报一份存档"按钮**永远是隐藏的**；
* ``members/detail.html`` 需要 ``can_delete``，而路由忘了传
  ⟹ 「作废此成员 / 解绑该账号」按钮永远不出现（用户反馈"不能删除"的成因之一）。

肉眼和 grep 都很难发现：``can_upload`` 在项目里**确实存在**，只是不在那条路由上。
所以这里做真正的对应关系检查：

1. 用 AST 读每个路由模块，解析每处 ``render(request, "<模板>", {...})``；
2. 收集它传的**字面键**，以及 ``**helper(...)`` 展开的 helper 里所有字典字面量的键
   （含 ``ctx = {...}`` / ``ctx.update({...})`` / ``x[k] = ...`` 这类写法）；
3. 递归展开模板的 ``{% include %}``，收集其中被当**开关**用的变量
   （形如 ``can_xxx`` / ``show_xxx`` / ``has_xxx`` 的 ``{% if %}`` 条件）；
4. 对一个模板，把所有渲染它的路由传的键求并集 —— 只要**有一条**路由补齐了就算过
   （因为模板是按页面渲染的，不是按路由）；
5. 报出"用了却谁也不传"的变量。

⚠️ 只认强信号（``{% if can_x %}`` 形式的开关），不做全量变量检查 ——
   全量检查会被宏参数、loop 变量、过滤器参数淹没，最后没人看。

用法::

    .venv\\Scripts\\python.exe scripts/template_context_audit.py
    .venv\\Scripts\\python.exe scripts/template_context_audit.py --verbose
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "gfvfw" / "web" / "templates"
ROUTERS = ROOT / "gfvfw" / "web" / "routers"

#: 当成"开关变量"的名字形态
FLAG_RE = re.compile(r"^(can_|show_|has_|is_admin)[a-z0-9_]*$")
#: {% if can_x %} / {% if not can_x %} / {% elif can_x %}
IF_RE = re.compile(r"\{%-?\s*(?:if|elif)\s+(not\s+)?([A-Za-z_][A-Za-z0-9_.]*)")
#: {% set x = ... %} —— 模板自己定义的，不算上下文变量
SET_RE = re.compile(r"\{%-?\s*set\s+([A-Za-z_][A-Za-z0-9_]*)")
INCLUDE_RE = re.compile(r"\{%-?\s*include\s+\"([^\"]+)\"")


def dict_keys(node: ast.AST) -> set[str]:
    """从一个字典字面量取所有字符串键。"""
    if not isinstance(node, ast.Dict):
        return set()
    out: set[str] = set()
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.add(k.value)
    return out


def helper_keys(module: ast.Module) -> dict[str, set[str]]:
    """模块内每个函数"会往上下文里塞哪些键"（尽量宽松地收集）。

    覆盖三种常见写法：
    * ``return {...}``
    * ``x = {...}`` 然后 ``return x``
    * ``x.update({...})`` / ``x.update(k=v)``
    """
    out: dict[str, set[str]] = {}
    for fn in module.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        keys: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                keys |= dict_keys(node.value)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                keys |= dict_keys(node.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "update":
                for arg in node.args:
                    keys |= dict_keys(arg)
                keys |= {kw.arg for kw in node.keywords if kw.arg}
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
        out[fn.name] = keys
    return out


def template_flags(path: Path) -> tuple[set[str], set[str]]:
    """模板里被当开关用的变量 + 它 include 了哪些模板。"""
    txt = path.read_text(encoding="utf-8")
    flags = {m.group(2).split(".")[0] for m in IF_RE.finditer(txt)
             if FLAG_RE.match(m.group(2).split(".")[0])}
    flags -= set(SET_RE.findall(txt))
    return flags, set(INCLUDE_RE.findall(txt))


def function_context_keys(fn: ast.AST, helpers: dict[str, set[str]],
                          consts: dict[str, set[str]]) -> dict[str, set[str]]:
    """在**一个函数体内**做局部符号解析：``变量名 → 它会往上下文里带的键``。

    ⚠️ 必须做这一步，否则漏掉最常见的写法::

        ctx = _nav_ctx(db, camp, principal)   # 键在这一句里
        ctx.update({...})                     # 又加了一些
        return render(request, "t.html", ctx) # 这里传的是**变量名**

    只认 ``render(..., {...})`` 那种字典字面量的话，上面这条会被判成"什么都没传"
    —— 又给出假警报。假警报和漏报一样有害：会让人不再看这个脚本。
    """
    local: dict[str, set[str]] = {}

    def resolve(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Dict):
            keys: set[str] = set()
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
                elif k is None:
                    keys |= _resolve_spread(v, helpers, local)
            return keys
        return _resolve_spread(node, helpers, local)

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            keys = resolve(node.value)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    local[tgt.id] = local.get(tgt.id, set()) | keys
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "update" and isinstance(node.func.value, ast.Name):
            name = node.func.value.id
            keys: set[str] = set()
            for arg in node.args:
                keys |= resolve(arg)
            for kw in node.keywords:
                if kw.arg is None:
                    keys |= resolve(kw.value)
                else:
                    keys.add(kw.arg)
            local[name] = local.get(name, set()) | keys
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) \
                and isinstance(node.value, ast.Name):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                local[node.value.id] = local.get(node.value.id, set()) | {sl.value}
    return local


def _resolve_spread(node: ast.AST, helpers: dict[str, set[str]],
                    local: dict[str, set[str]]) -> set[str]:
    """解析一个"来源可能带键"的表达式：helper 调用 / 局部变量 / 字典字面量。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return set(helpers.get(node.func.id, set()))
    if isinstance(node, ast.Name):
        return set(local.get(node.id, set())) | set(helpers.get(node.id, set()))
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            elif k is None:
                keys |= _resolve_spread(v, helpers, local)
        return keys
    return set()


def module_context_keys(tree: ast.Module) -> list[tuple[str, set[str]]]:
    """返回每个 ``render(...)`` 调用：``(模板名, 该次调用能提供的键)``。"""
    helpers = helper_keys(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    helpers[tgt.id] = dict_keys(node.value)

    renders: list[tuple[str, set[str]]] = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = function_context_keys(fn, helpers, {})
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "render"):
                continue
            if len(node.args) < 2:
                continue
            tpl = node.args[1]
            if not (isinstance(tpl, ast.Constant) and isinstance(tpl.value, str)):
                continue
            keys: set[str] = set()
            if len(node.args) >= 3:
                keys |= _resolve_spread(node.args[2], helpers, local)
            for kw in node.keywords:
                if kw.arg is None:
                    keys |= _resolve_spread(kw.value, helpers, local)
                elif kw.arg:
                    keys.add(kw.arg)
            renders.append((tpl.value, keys))
    return renders


def main() -> int:
    ap = argparse.ArgumentParser(description="模板开关变量 × 路由上下文 对应体检")
    ap.add_argument("--verbose", action="store_true", help="连通过的也列出来")
    args = ap.parse_args()

    # 1) 路由 → {模板: 传了哪些键}
    provided: dict[str, set[str]] = {}

    def add(template: str, keys: set[str]) -> None:
        provided.setdefault(template, set()).update(keys)

    for py in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for tpl, keys in module_context_keys(tree):
            add(tpl, keys)

    # 2) 模板 → 用到的开关（含 include 递归）
    cache: dict[str, tuple[set[str], set[str]]] = {}

    def flags_of(name: str) -> set[str]:
        if name in cache:
            return cache[name][0]
        p = TEMPLATES / name
        if not p.exists():
            cache[name] = (set(), set())
            return set()
        flags, incs = template_flags(p)
        cache[name] = (flags, incs)
        for inc in incs:
            flags |= flags_of(inc)
        cache[name] = (flags, incs)
        return flags

    all_templates = sorted(str(p.relative_to(TEMPLATES)).replace("\\", "/")
                           for p in TEMPLATES.rglob("*.html"))

    problems: list[tuple[str, list[str]]] = []
    for name in all_templates:
        flags = flags_of(name)
        if not flags:
            continue
        have = provided.get(name, set())
        missing = sorted(f for f in flags if f not in have)
        if missing:
            problems.append((name, missing))
        elif args.verbose:
            print("  ok   %-38s %s" % (name, sorted(flags)))

    print("=" * 74)
    print("模板开关变量 × 路由上下文 体检")
    print("=" * 74)
    print("模板 %d 个，其中有开关变量的 %d 个"
          % (len(all_templates),
             sum(1 for n in all_templates if flags_of(n))))
    print()
    if not problems:
        print("★ 没有「用了却没人传」的开关变量。")
    else:
        for name, missing in problems:
            print("  ✗ %-40s 没人传：%s" % (name, ", ".join(missing)))
            print("      ↳ 这些开关恒为假，对应的按钮/区块**永远不会显示**")
    print()
    print("=" * 74)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
