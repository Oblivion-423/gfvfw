"""从 ``LogbookEditor.exe`` 的反汇编里枚举"字段访问"，还原 ``.lbk`` 的内存布局。

原理
----
该工具用 ``0x402e3c`` 取"当前 logbook 结构体指针"（每次访问字段前都会调用它），
紧接着就是 ``mov/movzx ... [eax+0xNN]``（读）或 ``mov [eax+0xNN], ...``（写）。
把这种 ``call 0x402e3c`` 之后紧跟的偏移访问按地址顺序列出来，
就得到字段的**偏移、宽度与读写方向** —— 比猜字节模式可靠得多。

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_layout_probe.py _re/lbk/disasm.txt
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

LINE_RE = re.compile(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{2}\s+)+(.*)$")

#: 取结构体指针的那个辅助函数
GETTER = "0x402e3c"

#: 访问宽度：字节 / 字 / 双字 / 浮点
WIDTHS = (
    ("BYTE PTR", 1), ("WORD PTR", 2), ("DWORD PTR", 4),
)

READ_PAT = re.compile(
    r"^(?:mov|movzx|movsx|cmp|test|fld)\s+.*?(BYTE|WORD|DWORD) PTR \[(eax|edx|ecx|ebx|esi|edi)"
    r"(?:\+0x([0-9a-f]+))?\]")
WRITE_PAT = re.compile(
    r"^mov\s+(BYTE|WORD|DWORD) PTR \[(eax|edx|ecx|ebx|esi|edi)(?:\+0x([0-9a-f]+))?\],")
FSTP_PAT = re.compile(r"^fstp\s+DWORD PTR \[(eax|edx|ecx|ebx|esi|edi)(?:\+0x([0-9a-f]+))?\]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("disasm")
    ap.add_argument("--getter", default=GETTER)
    args = ap.parse_args()

    lines = pathlib.Path(args.disasm).read_text(encoding="utf-8",
                                               errors="replace").splitlines()
    parsed = []
    for ln in lines:
        m = LINE_RE.match(ln)
        if m:
            parsed.append((int(m.group(1), 16), m.group(2).strip()))

    # 找所有 `call <getter>` 的位置
    hits = [i for i, (_a, rest) in enumerate(parsed)
            if rest.startswith("call") and args.getter in rest]
    print("`call %s` 出现 %d 次" % (args.getter, len(hits)))
    print()

    seen: dict[tuple[int, int, str], list[int]] = {}
    for h in hits:
        # 跟随 `call <getter>` 之后的指针寄存器链：
        # 返回值在 eax；常见写法是 mov edx,eax / mov [ebp-x],eax 之后再访问。
        # ⚠️ 只认"由 eax 派生"的寄存器访问，否则会把相邻的无关访问也算进来
        #    （早先不区分寄存器时，多出了 25 个"只读"偏移，其实是别的结构）。
        tracked = {"eax"}
        for a, rest in parsed[h + 1:h + 9]:
            mv = re.match(r"^mov\s+(e[abcd]x|e[sd]i|e[b]p)\s*,\s*(eax|edx)$", rest)
            if mv:
                tracked.add(mv.group(1))
            mv2 = re.match(r"^mov\s+(e[abcd]x|e[sd]i)\s*,\s*DWORD PTR \[ebp-0x[0-9a-f]+\]$",
                           rest)
            if mv2:
                tracked.add(mv2.group(1))

            mm = FSTP_PAT.match(rest)
            if mm and mm.group(1) in tracked:
                off = int(mm.group(2) or "0", 16)
                seen.setdefault((off, 4, "写(float)"), []).append(a)
                continue
            mm = WRITE_PAT.match(rest)
            if mm and mm.group(2) in tracked:
                width = {"BYTE": 1, "WORD": 2, "DWORD": 4}[mm.group(1)]
                off = int(mm.group(3) or "0", 16)
                seen.setdefault((off, width, "写"), []).append(a)
                continue
            mm = READ_PAT.match(rest)
            if mm and mm.group(2) in tracked:
                width = {"BYTE": 1, "WORD": 2, "DWORD": 4}[mm.group(1)]
                off = int(mm.group(3) or "0", 16)
                seen.setdefault((off, width, "读"), []).append(a)

    print("字段访问（按偏移排序）：")
    print("  %-8s %-6s %-10s %s" % ("偏移", "宽度", "方向", "出现的代码地址"))
    print("  " + "-" * 72)
    for (off, width, kind), sites in sorted(seen.items()):
        print("  0x%04x  %-6d %-10s %s"
              % (off, width, kind, " ".join("%08x" % s for s in sites[:5])))
    print()
    print("共 %d 个不同偏移。" % len({k[0] for k in seen}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
