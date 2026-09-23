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
            # ⚠️ 必须先找**装饰器**，再从装饰器往后找 def —— 反过来会被
            #    路由体里的内层辅助函数（如 ``def fail(msg)``）抢先命中，
            #    于是把内层函数的签名当成路由守卫，报出"无守卫"的假警报。
            dec = None
            for j in range(i, max(-1, i - 200), -1):
                if "@router." in lines[j]:
                    dec = j
                    break
            defn = None
            if dec is not None:
                for j in range(dec, min(len(lines), dec + 40)):
                    # ⚠️ 必须容忍 ``async def`` —— 只匹配 ``^\s*def ``
                    #    会跳过异步路由，一路找不到函数而把守卫报成"无"。
                    if re.match(r"\s*(?:async\s+)?def ", lines[j]):
                        defn = j
                        break

            sig = gather_signature(lines, defn) if defn is not None else ""
            # ⚠️ 四个守卫都要认：只查 "require(" 与 "require_login" 会把
            #    require_member / require_any 守卫的路由误报成"无守卫"，
            #    而"列表公开、详情仅队员"的改法正好大量使用 require_member。
            guarded = any(tok in sig for tok in (
                "require(", "require_any(", "require_login", "require_member"))
            name = lines[defn].strip() if defn is not None else "?"
            deco = lines[dec].strip() if dec is not None else "?"
            if dec is None:
                # 没有 @router. 装饰器 ⟶ 这是辅助函数，守卫由其调用方提供。
                mark = "（辅助函数，守卫在调用方）"
            else:
                mark = "受守卫" if guarded else "★ 无 require() 守卫"
            print("  L%-4d %-46s %s" % (i + 1, deco[:46], mark))
            print("         %s" % name[:70])
            if dec is not None and not guarded:
                unguarded.append("%s:%d %s" % (path.name, i + 1, deco))

    print("=" * 74)
    print("can() 调用点共 %d 个；其中无 require() 守卫的 %d 个" % (total, len(unguarded)))
    for u in unguarded:
        print("  ★", u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
