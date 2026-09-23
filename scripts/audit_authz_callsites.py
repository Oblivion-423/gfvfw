"""一次性审计脚本：检查每个 ``principal.can()`` 调用点是否处在
``Depends(require(...))`` / ``Depends(require_login)`` 守卫之下。

动机：权限判定散落在路由体里（``if not principal.can(X): 403``）是常见写法，
但那样**只**依赖 ``Principal.permissions`` 的正确性。若 ``permissions`` 因为
某个未知 ``status`` 取值而没有被清空，这些调用点就会变成真实越权。

本脚本只做静态检查，不改动任何文件。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROUTERS = pathlib.Path("gfvfw/web/routers")

# 出现在模板里的（只影响按钮显隐），与真正的授权判定分开看
TEMPLATE_ONLY = {"can_edit_own", "can_manage_campaign"}


def gather_signature(lines: list[str], def_idx: int) -> str:
    """把 ``def`` 起直到行尾冒号的整段签名拼起来（支持多行）。"""
    buf: list[str] = []
    k = def_idx
    while k < len(lines):
        buf.append(lines[k])
        if lines[k].rstrip().endswith(":"):
            break
        k += 1
    return "\n".join(buf)


def main() -> int:
    total = 0
    unguarded: list[str] = []

    for path in sorted(ROUTERS.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [i for i, ln in enumerate(lines) if "principal.can" in ln or "p.can(" in ln]
        if not hits:
            continue
        print("=" * 74)
        print(path.name)
        for i in hits:
            total += 1
            dec = defn = None
            for j in range(i, max(-1, i - 120), -1):
                if defn is None and re.match(r"\s*def ", lines[j]):
                    defn = j
                if dec is None and "@router." in lines[j]:
                    dec = j
                if dec is not None and defn is not None:
                    break

            sig = gather_signature(lines, defn) if defn is not None else ""
            guarded = ("require(" in sig) or ("require_login" in sig)
            name = lines[defn].strip() if defn is not None else "?"
            deco = lines[dec].strip() if dec is not None else "?"
            mark = "受守卫" if guarded else "★ 无 require() 守卫"
            print("  L%-4d %-46s %s" % (i + 1, deco[:46], mark))
            print("         %s" % name[:70])
            if not guarded:
                unguarded.append("%s:%d %s" % (path.name, i + 1, deco))

    print("=" * 74)
    print("can() 调用点共 %d 个；其中无 require() 守卫的 %d 个" % (total, len(unguarded)))
    for u in unguarded:
        print("  ★", u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
