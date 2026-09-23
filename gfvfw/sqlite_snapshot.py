"""SQLite 底层工具：一致性快照、完整性校验、版本特性自检。

⚠️⚠️ **本模块刻意不导入 `gfvfw.config`**（也不导入任何会间接导入它的东西）。
这不是风格问题，是一条安全约束 —— 原因如下：

`gfvfw.config` 在**模块导入时**就构造单例 ``settings = Settings()``，
也就是说 **import 的那一刻**就决定了"连哪个数据库"。
而探针脚本（``scripts/lbk_*_probe.py`` 等）的写法是：

1. 先 ``os.environ["GFVFW_DATABASE_URL"] = 快照路径``
2. 再 ``import gfvfw.…``

顺序一旦反了，``settings`` 就绑定到**线上库**，探针会去读写真实数据 ——
而这些脚本的整个设计前提就是"不碰线上库"。

这个坑真的踩过：把 ``snapshot_sqlite`` 放在 ``gfvfw/db.py`` 里之后，
探针顶部的 ``from gfvfw.db import snapshot_sqlite`` 会连带导入 config，
于是探针登录尝试打到了线上库，把真实管理员账号**连败 5 次锁掉了**。

所以：**需要在本模块里用 settings 才能做的事，一律不做**；
快照与校验都只接受显式传入的路径。探针要检查配置是否指对了，
用 :func:`assert_isolated_snapshot`（它**在函数体内**惰性导入 config，
所以模块导入仍然是干净的）。

``gfvfw/db.py`` 会重新导出这里的名字，方便既有代码继续 ``from gfvfw.db import …``；
但**探针脚本应当直接从这里导入**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

# --------------------------------------------------------------------------
# 版本特性自检（防"开发机新、服务器老"）
# --------------------------------------------------------------------------

#: 本项目**保证支持**的最低 SQLite 版本。
#:
#: 依据是实际部署环境：Alibaba Cloud Linux 3 / RHEL 8 系自带 **3.26.0**。
#: 开发机（Windows + Python 3.10）自带 3.37+，所以**在开发机上测不出**任何
#: 版本差异 —— 这正是下面这张表存在的理由。
MIN_SQLITE_VERSION = "3.26.0"

#: 需要**新于** :data:`MIN_SQLITE_VERSION` 的 SQL 关键字 → 最低版本。
#:
#: ⚠️ 这里踩过一次真实的坑：备份用的是 ``VACUUM INTO``（SQLite **3.27+**），
#:    在开发机上一直正常（3.39），到服务器上直接
#:    ``sqlite3.OperationalError: near "INTO": syntax error`` ——
#:    备份失败、更新中止，而报错完全没提"版本"两个字。
#:    正确做法是用 Python 层的 ``Connection.backup()``（见 :func:`snapshot_sqlite`），
#:    它背后是 SQLite **3.6.11** 就有的在线备份 API，哪儿都有。
#:
#: ⚠️ 下面每一行都带 ``sqlite-version-ok``：它们是**定义**，不是用法。
#:    真去用这些特性时才会被 :func:`check_sqlite_feature_level` 抓出来。
SQLITE_VERSION_GATED_SQL: dict[str, str] = {
    "VACUUM INTO": "3.27.0",        # sqlite-version-ok: 本表的键，非用法
    "NULLS LAST": "3.30.0",         # sqlite-version-ok: 本表的键，非用法
    "NULLS FIRST": "3.30.0",        # sqlite-version-ok: 本表的键，非用法
    "RETURNING": "3.35.0",          # sqlite-version-ok: 本表的键，非用法
    "STRICT": "3.37.0",             # sqlite-version-ok: 本表的键，非用法
    "->>": "3.38.0",                # sqlite-version-ok: 本表的键，非用法
    "GENERATED ALWAYS": "3.31.0",   # sqlite-version-ok: 本表的键，非用法
}

#: 扫描时跳过的目录（第三方/运行时数据不参与）
_SCAN_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "var", "reference", "_re",
    ".pytest_cache", "node_modules",
})


def sqlite_version() -> str:
    """当前解释器实际链接的 SQLite 版本（排查用）。"""
    return sqlite3.sqlite_version


def _docstring_lines(tree) -> set[int]:                 # noqa: ANN001
    """收集**文档字符串**占用的行号。

    只认真正的 docstring（模块/类/函数体的第一条表达式语句），
    不认普通字符串字面量 —— 因为 SQL 永远是写在普通字符串里的，
    那条界线正好是"解释性文字"与"真的在用"的分界。
    """
    import ast

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        val = first.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                lines.add(ln)
    return lines


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """返回 ``[(行号, 代码文本)]``：**去掉注释与 docstring，但保留普通字符串**。

    ⚠️ 这三者的取舍是这条检查能不能用的关键：

    * **注释要去掉** —— 否则"我们不用 ``VACUUM INTO``，因为它要 3.27"这种
      解释性注释会被当成真的用了，检查永远红；而人的自然反应是删掉那些
      解释，正好把教训一起删了。
    * **docstring 要去掉** —— 同上，理由写在 docstring 里是最自然的地方。
    * **普通字符串要保留** —— 恰恰相反的理由：SQL 永远是写在普通字符串里的，
      把它们删掉就等于把这个检查要抓的东西全删了。
      （第一版就是这么写的，结果一条都抓不到，形同虚设。）
    """
    import ast
    import io
    import tokenize

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    doc_lines: set[int] = set()
    try:
        doc_lines = _docstring_lines(ast.parse(text))
    except (SyntaxError, ValueError):
        pass                       # 解析不了就退化为"不排除 docstring"

    by_line: dict[int, list[str]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            # 去注释；保留 STRING
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            by_line.setdefault(tok.start[0], []).append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 拿不到 token 流时退化为整行扫描（宁可误报，不可漏报）
        return [(ln, raw) for ln, raw in
                enumerate(text.splitlines(), 1) if ln not in doc_lines]

    return [(ln, " ".join(parts)) for ln, parts in sorted(by_line.items())
            if ln not in doc_lines]


def check_sqlite_feature_level(root: Optional[Union[str, Path]] = None) -> list[str]:
    """扫描源码，找出需要高于 :data:`MIN_SQLITE_VERSION` 的 SQLite 写法。

    返回问题列表（空 = 通过）。与 ``gfvfw.db.check_portability`` 互补：
    那条管"跨数据库方言"，这条管"跨 SQLite 版本"。

    注释与 docstring 已剔除，所以"为什么不用 ``VACUUM INTO``"这类解释不会被
    误报；而 SQL 写在普通字符串里，仍然会被抓到。确实需要某个新特性时，
    在那一行加上 ``sqlite-version-ok`` 并写清理由即可放行
    （:data:`SQLITE_VERSION_GATED_SQL` 自己的定义行就是这么豁免的）。
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    problems: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if _SCAN_SKIP_DIRS & set(path.parts):
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, code in _code_lines(path):
            if not code.strip():
                continue
            # 豁免标记写在**原始行**上 —— 它在注释里，已被上面剥掉了
            raw = raw_lines[lineno - 1] if lineno <= len(raw_lines) else ""
            if "sqlite-version-ok" in raw:
                continue
            for token, minver in SQLITE_VERSION_GATED_SQL.items():
                if token in code:
                    problems.append(
                        "%s:%d 代码里使用了需要 SQLite %s 的 `%s`"
                        "（本项目最低支持 %s）：%s"
                        % (path.relative_to(root), lineno, minver, token,
                           MIN_SQLITE_VERSION, code.strip()[:90])
                    )
    return problems


# --------------------------------------------------------------------------
# 一致性快照（备份的唯一实现）
# --------------------------------------------------------------------------

def snapshot_sqlite(src: Union[str, Path], dest: Union[str, Path]) -> int:
    """把 SQLite 库**一致性**快照到 ``dest``，返回字节数。

    为什么不能直接 ``cp``
    ---------------------
    库跑在 WAL 模式下，直接复制 ``.sqlite3`` 会得到**撕裂的快照**：
    已提交但仍在 ``-wal`` 里的事务不在主文件里，而 ``-wal`` 又不一定同时被复制。

    为什么用 ``Connection.backup()`` 而不是 ``VACUUM INTO``
    ------------------------------------------------------
    ``VACUUM INTO`` 要 SQLite **3.27+**。开发机是 3.39，服务器（RHEL 8 系）
    是 **3.26.0** —— 于是"本地一直好好的备份"到线上直接
    ``near "INTO": syntax error``，而且报错完全不提版本。

    ``sqlite3.Connection.backup()`` 是 Python 3.7+ 的接口，背后是 SQLite
    **3.6.11** 就存在的**在线备份 API**，任何还能跑的 SQLite 都有它；
    它同样产出一份完整、可直接打开的副本，不需要停服务。
    **刻意只保留这一条实现路径**（不做版本嗅探、不写 fallback）：
    双路径意味着"本地测的是 A、线上跑的是 B"，而这个 bug 就是这么来的。

    体积上 ``VACUUM INTO`` 会顺带整理碎片、略小一些（本实现是逐页复制，
    含空闲页）。这点差异**不值得**换回一个"取决于服务器 SQLite 版本"的实现。

    ⚠️ 目标文件**已存在就报错** —— 绝不悄悄覆盖上一份备份。
    """
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise FileNotFoundError("数据库文件不存在：%s" % src)
    if dest.exists():
        raise FileExistsError("快照目标已存在：%s" % dest)

    # 只读打开源库：备份过程绝不能写源
    src_con = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True)
    dst_con = sqlite3.connect(str(dest))
    try:
        # backup() 会把 WAL 中已提交的事务一并带过去，得到一致性快照
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    return dest.stat().st_size


def verify_snapshot(dest: Union[str, Path],
                    required_tables: tuple[str, ...] = (
                        "members", "users", "sorties", "missions")) -> None:
    """校验一份快照真的能用：``integrity_check`` + 关键表存在。

    ⚠️ 备份不能用"文件存在"来证明有效 —— 一个 0 字节文件也存在。
    """
    dest = Path(dest)
    con = sqlite3.connect(str(dest))
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        status = row[0] if row else "?"
        if status != "ok":
            raise RuntimeError("快照完整性检查未通过：%s" % status)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in required_tables if t not in tables]
        if missing:
            raise RuntimeError("快照缺少表：%s" % ", ".join(missing))
    finally:
        con.close()


def assert_isolated_snapshot(snapshot: Union[str, Path]) -> None:
    """探针安全闸：确认当前配置**真的**指向快照，而不是线上库。

    探针脚本（``scripts/lbk_*_probe.py``）的做法是"先设 ``GFVFW_DATABASE_URL``
    指向快照，再导入应用"。这个顺序**极易写错**：只要在设环境变量之前导入了
    任何会带出 ``gfvfw.config`` 的模块，``settings`` 就永久绑定到**线上库**，
    探针会去读写真实数据。

    这不是假设 —— 真发生过：探针登录尝试打到线上库，把真实管理员账号
    连败 5 次锁掉了。所以每个探针在导入应用之后**必须**调用本函数。

    ⚠️ config 是**在函数体内**惰性导入的，所以本模块的导入仍然是干净的。
    """
    from .config import settings          # 惰性导入：保持本模块无 config 依赖

    want = Path(snapshot).as_posix()
    got = settings.database_url
    if want not in got:
        raise SystemExit(
            "✗ 探针配置错误：settings.database_url = %r\n"
            "  但它应当指向快照 %s\n"
            "  说明 gfvfw.config 在环境变量设好**之前**就被导入了 ——\n"
            "  十有八九是脚本顶部 import 了 gfvfw.* 里的东西。\n"
            "  它会去读写**线上库**，绝不能继续。\n"
            "  修法：把 import 移到设置 GFVFW_DATABASE_URL 之后，\n"
            "        或者只从 gfvfw.sqlite_snapshot 导入（该模块不依赖 config）。"
            % (got, want))
