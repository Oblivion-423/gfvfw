"""从 PE 的 .rdata/.data 提取带地址的 C 字符串与字节表 —— 纯标准库。

为什么需要：``objdump -s`` 的十六进制转储每行会丢掉字符串之间的填充，
直接拼接会把相邻字符串粘成一条，无法定位。本脚本按 ``\\x00`` 切分并保留地址，
这样才能把"字符串地址"与"引用它的代码"对上。

用法::

    .venv\\Scripts\\python.exe scripts\\pe_strings_addressed.py <exe> [--min 4] [--grep 关键词]
"""
from __future__ import annotations

import argparse
import pathlib
import struct
import sys


def sections(b: bytes) -> list[tuple[str, int, int, int, int]]:
    """返回 ``[(名字, VMA, VSIZE, 文件偏移, 原始大小)]``。"""
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


def extract(b: bytes, want: tuple[str, ...], min_len: int) -> None:
    for name, vma, vsize, roff, rsize in sections(b):
        if name not in want or not rsize:
            continue
        blob = b[roff:roff + rsize]
        print("=" * 78)
        print("节 %s  VMA=0x%08x  %d 字节" % (name, vma, len(blob)))
        print("=" * 78)
        # 按 \x00 切分，保留地址
        start = 0
        for i in range(len(blob) + 1):
            if i == len(blob) or blob[i] == 0:
                chunk = blob[start:i]
                if len(chunk) >= min_len and all(32 <= c < 127 or c in (9, 10, 13)
                                                 for c in chunk):
                    print("  0x%08x  %s" % (vma + start, chunk.decode("latin-1")))
                start = i + 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--min", type=int, default=4)
    ap.add_argument("--only", default=".rdata,.data")
    args = ap.parse_args()

    b = pathlib.Path(args.path).read_bytes()
    extract(b, tuple(s.strip() for s in args.only.split(",")), args.min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
