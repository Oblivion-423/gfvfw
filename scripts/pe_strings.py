"""从 PE 里提取字符串与导入表 —— 纯标准库，不需要反编译器。

用途：判断 ``LogbookEditor.exe`` 是否用了系统加密 API、是否静态链接了格式代码、
以及是否有能揭示 ``.lbk`` 字段含义的字符串。

用法::

    .venv\\Scripts\\python.exe scripts\\pe_strings.py <exe路径> [--min 5] [--grep 关键词]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import struct
import sys
from collections import Counter


def sections(b: bytes) -> list[tuple[str, int, int, int]]:
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    nsec = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    base = pe + 24 + opt_size
    out = []
    for i in range(nsec):
        o = base + i * 40
        name = b[o:o + 8].rstrip(b"\x00").decode("latin-1")
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", b, o + 8)
        out.append((name, vaddr, vsize, raddr))
    return out


def imports(b: bytes) -> list[tuple[str, list[str]]]:
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    opt = pe + 24
    dd = opt + 96
    rva, _ = struct.unpack_from("<II", b, dd + 8)
    if not rva:
        return []
    secs = sections(b)

    def r2o(r: int) -> int | None:
        """RVA → 文件偏移。**必须精确按节边界**：
        早先写成 `va <= r < va + vsize + 0x1000` 这种宽松范围，
        结果落到错误的节里，解出来的 DLL 名与函数名全是乱码。"""
        for _n, va, vs, ra in secs:
            if vs and va <= r < va + vs:
                return ra + (r - va)
        return None

    off = r2o(rva)
    if off is None:
        return []
    out = []
    i = 0
    while i < 200:
        o = off + i * 20
        oft, _t, _f, namerva, fthunk = struct.unpack_from("<IIIII", b, o)
        if namerva == 0 and fthunk == 0 and oft == 0:
            break
        no = r2o(namerva)
        if no is None:
            break
        end = b.index(b"\x00", no)
        dll = b[no:end].decode("latin-1")
        funcs: list[str] = []
        tho = r2o(oft or fthunk)
        if tho is not None:
            j = 0
            while j < 5000:
                ent = struct.unpack_from("<I", b, tho + j * 4)[0]
                if ent == 0:
                    break
                if not (ent & 0x80000000):
                    fo = r2o(ent)
                    if fo is not None:
                        fe = b.index(b"\x00", fo + 2)
                        funcs.append(b[fo + 2:fe].decode("latin-1"))
                j += 1
        out.append((dll, funcs))
        i += 1
    return out


ASCII_RE = re.compile(rb"[\x20-\x7e]{%d,}")
UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--min", type=int, default=5)
    ap.add_argument("--grep", default="")
    args = ap.parse_args()

    b = pathlib.Path(args.path).read_bytes()
    print("文件：%s（%d 字节）" % (args.path, len(b)))
    print("\n=== 节 ===")
    for name, va, vs, ra in sections(b):
        print("  %-9s vaddr=0x%-7x vsize=%-7d raw@0x%x" % (name, va, vs, ra))

    print("\n=== 导入表 ===")
    imps = imports(b)
    for dll, funcs in imps:
        print("  %s  (%d 个函数)" % (dll, len(funcs)))
        for f in funcs:
            print("      %s" % f)
    if not imps:
        print("  （无导入表 / 解析失败）")

    # 是否用了系统加密 API —— 这决定"是否有加密"这一根本问题
    print("\n=== 加密相关导入 ===")
    kw = ("crypt", "aes", "des", "rc4", "hash", "md5", "sha", "rand", "xor")
    hit = False
    for dll, funcs in imps:
        for f in funcs:
            if any(k in f.lower() for k in kw):
                print("  ★ %s!%s" % (dll, f))
                hit = True
    if not hit:
        print("  没有找到任何系统加密 API 导入 —— 若真有加密，只能是自带实现")

    print("\n=== ASCII 字符串（>=%d，共 %d 条）===" % (args.min, 0))
    asc = [m.group().decode("latin-1") for m in ASCII_RE.finditer(b, args.min)
           ] if False else [m.group().decode("latin-1")
                            for m in re.finditer(rb"[\x20-\x7e]{%d,}" % args.min, b)]
    u16 = [m.group().decode("utf-16-le")
           for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % args.min, b)]
    print("  共 %d 条 ASCII / %d 条 UTF-16" % (len(asc), len(u16)))

    if args.grep:
        pat = re.compile(args.grep, re.I)
        print("\n  --- 匹配 %r ---" % args.grep)
        for s in asc + u16:
            if pat.search(s):
                print("    %s" % s)
        return 0

    print("\n  前 60 条较长的：")
    for s in sorted(set(asc), key=len, reverse=True)[:60]:
        print("    %s" % s[:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())
