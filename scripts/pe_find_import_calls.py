"""在 objdump 反汇编里定位对指定导入函数的调用，并回推所属函数。

动机：`LogbookEditor.exe` 是 32 位 MinGW/Qt4 程序，没有可用的反编译器。
但"文件读写在哪"是可以机械定位的 —— 解析 IAT 拿到 `fopen`/`fread`/`fwrite`
的绝对地址，再在反汇编里找 `call DWORD PTR [<iat>]`，落在哪个函数里就一目了然。
反混淆代码通常就紧挨着读取调用。

用法::

    .venv\\Scripts\\python.exe scripts\\pe_find_import_calls.py \\
        <exe> _re/lbk/disasm.txt fopen fread fwrite fseek
"""
from __future__ import annotations

import argparse
import pathlib
import re
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def sections(b: bytes) -> list[tuple[str, int, int, int, int]]:
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    nsec = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    base = pe + 24 + opt_size
    out = []
    for i in range(nsec):
        o = base + i * 40
        name = b[o:o + 8].rstrip(b"\x00").decode("latin-1")
        vsize, vma, rsize, roff = struct.unpack_from("<IIII", b, o + 8)
        out.append((name, vma, vsize, roff, rsize))
    return out


def iat_map(b: bytes) -> dict[int, str]:
    """``{IAT 槽位的 VMA: 函数名}``。

    ⚠️ 导入描述符里的 ``FirstThunk`` 是 **RVA**，而反汇编里显示的是 **VMA** ——
    必须加上映像基址（PE32 的 ``ImageBase``，这里 0x400000），
    否则搜 ``0x234e8`` 永远搜不到 ``ds:0x4234e8``。
    """
    secs = sections(b)
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    opt = pe + 24
    image_base = struct.unpack_from("<I", b, opt + 28)[0]      # PE32 ImageBase
    dd = opt + 96

    def r2o(r: int):
        for _n, vma, vsize, roff, _rs in secs:
            if vsize and vma <= r < vma + vsize:
                return roff + (r - vma)
        return None

    out: dict[int, str] = {}
    for idx in (1, 12):                     # 1=import, 12=IAT
        rva, size = struct.unpack_from("<II", b, dd + idx * 8)
        if not rva:
            continue
        off = r2o(rva)
        if off is None:
            continue
        i = 0
        while i < 500:
            o = off + i * 20
            oft, _t, _f, namerva, fthunk = struct.unpack_from("<IIIII", b, o)
            if namerva == 0 and fthunk == 0 and oft == 0:
                break
            dll_off = r2o(namerva)
            if dll_off is None:
                break
            dll = b[dll_off:b.index(b"\x00", dll_off)].decode("latin-1")
            tho = r2o(oft or fthunk)
            if tho is not None and fthunk:
                j = 0
                while j < 3000:
                    ent = struct.unpack_from("<I", b, tho + j * 4)[0]
                    if ent == 0:
                        break
                    if not (ent & 0x80000000):
                        fo = r2o(ent)
                        if fo is not None:
                            fn = b[fo + 2:b.index(b"\x00", fo + 2)].decode("latin-1")
                            out[image_base + fthunk + j * 4] = "%s!%s" % (dll, fn)
                    j += 1
            i += 1
    return out


#: objdump 的一行形如::
#:     40835c:\tff 25 e8 34 42 00    \tjmp    DWORD PTR ds:0x4234e8
#: ⚠️ 字节列是**多个** ``xx `` 记号，不能只用一个 ``\S+`` 匹配
#:    （早先那样写会让整条规则静默失配，得到"0 处调用"的假结论）。
_LINE_RE = re.compile(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{2}\s+)+(.*)$")


def parse_line(ln: str) -> tuple[int, str] | None:
    m = _LINE_RE.match(ln)
    if not m:
        return None
    return int(m.group(1), 16), m.group(2).strip()


def find_thunks(disasm_path: pathlib.Path, iat: dict[int, str]) -> dict[int, str]:
    """``{thunk 指令地址: 函数名}``。

    MinGW 的 PE 会为每个导入生成一个桩 ``jmp DWORD PTR ds:<iat>``，
    真正的调用点写的是 ``call <thunk>``，所以要先把这层映射建出来。
    """
    out: dict[int, str] = {}
    pat = re.compile(r"^jmp\s+DWORD PTR ds:0x([0-9a-f]+)")
    for ln in disasm_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_line(ln)
        if not parsed:
            continue
        site, rest = parsed
        m = pat.match(rest)
        if not m:
            continue
        slot = int(m.group(1), 16)
        if slot in iat:
            out[site] = iat[slot]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("disasm")
    ap.add_argument("names", nargs="+", help="要查找的导入函数名（子串匹配）")
    ap.add_argument("--window", type=int, default=400,
                    help="回推函数起点时最多向前看多少行")
    args = ap.parse_args()

    b = pathlib.Path(args.exe).read_bytes()
    iat = iat_map(b)
    disasm_path = pathlib.Path(args.disasm)
    thunks = find_thunks(disasm_path, iat)

    def matches(name: str) -> bool:
        return any(n.lower() in name.lower() for n in args.names)

    targets: dict[int, str] = {addr: name for addr, name in iat.items() if matches(name)}
    targets.update({addr: name for addr, name in thunks.items() if matches(name)})
    if not targets:
        print("IAT 里没有匹配的函数。可用的相关导入：")
        for addr, name in sorted(iat.items(), key=lambda kv: kv[1]):
            if any(k in name.lower() for k in ("file", "read", "write", "open", "seek")):
                print("  0x%08x  %s" % (addr, name))
        return 1

    print("目标（含 thunk 与 IAT 槽位）：")
    for addr, name in sorted(targets.items()):
        kind = "thunk" if addr in thunks else "IAT  "
        print("  0x%08x  %s  %s" % (addr, kind, name))
    print()

    lines = disasm_path.read_text(encoding="utf-8", errors="replace").splitlines()
    func_re = re.compile(r"^([0-9a-f]{8}) <(.+)>:$")
    boundaries = []
    for ln in lines:
        m = func_re.match(ln.strip())
        if m:
            boundaries.append((int(m.group(1), 16), m.group(2)))
    boundaries.sort()

    def enclosing(addr: int) -> tuple[int, str]:
        best = (0, "?")
        for a, n in boundaries:
            if a <= addr:
                best = (a, n)
            else:
                break
        return best

    # 只认"调用点"：`call 0x...`。不要匹配数据引用或 thunk 定义本身。
    call_pat = re.compile(r"^call\s+0x([0-9a-f]+)")
    by_func: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for ln in lines:
        parsed = parse_line(ln)
        if not parsed:
            continue
        site, rest = parsed
        m = call_pat.match(rest)
        if not m:
            continue
        name = targets.get(int(m.group(1), 16))
        if name is None:
            continue
        fa, fn = enclosing(site)
        by_func.setdefault((fa, fn), []).append((site, name))

    for (fa, fn), calls in sorted(by_func.items()):
        names = sorted({n for _, n in calls})
        print("函数 0x%08x <%s>" % (fa, fn))
        print("    调用：%s" % ", ".join(names))
        for site, name in calls[:12]:
            print("      0x%08x  %s" % (site, name))
    print()
    print("命中 %d 个函数，共 %d 处调用。"
          % (len(by_func), sum(len(v) for v in by_func.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
