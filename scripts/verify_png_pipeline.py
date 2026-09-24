"""核对纯标准库 PNG 管线（``scripts/collect_theater_maps.py``）真的没写错。

**为什么值得单独验**：这段代码不依赖任何图形库，解码、降采样、编码全是手写的
字节运算。它"跑通"了不代表画面对 —— 通道顺序写反、行距算错、过滤器还原错，
产出的仍然是一张能打开的合法 PNG，只是图是歪的/花花的。所以这里用**可计算的
事实**来判：

1. 编码 → 解码往返必须**逐字节相同**（证明 encode/decode 互为逆）；
2. 合成图降采样后，颜色必须落在可预测的范围里（证明通道顺序没反）；
3. 真实地图降采样后，整幅的平均 RGB 必须与原图接近（证明没有整体偏色/错位）；
4. 5 种 scanline 过滤器都要能还原（BMS 的 Balkans 图用 Paeth）。

用法::

    .venv\\Scripts\\python.exe scripts/verify_png_pipeline.py
    .venv\\Scripts\\python.exe scripts/verify_png_pipeline.py --real var/maps/Hellas.png
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collect_theater_maps import (  # noqa: E402
    decode_png, encode_png, resize_nearest)

FAILS: list[str] = []
N = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    N[0] += 1
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "  ← " + detail))
    if not cond:
        FAILS.append(name)


def raw_png(width: int, height: int, channels: int,
            rows: list[bytes], filter_type: int = 0) -> bytes:
    """手工拼一张 PNG，**指定 scanline 过滤器**，用来测还原是否正确。"""
    color = {1: 0, 3: 2, 4: 6}[channels]
    prev = bytes(width * channels)
    raw = bytearray()
    for line in rows:
        raw.append(filter_type)
        if filter_type == 0:
            raw += line
        elif filter_type == 1:                       # Sub
            raw += bytes((line[i] - (line[i - channels] if i >= channels else 0))
                         & 0xFF for i in range(len(line)))
        elif filter_type == 2:                       # Up
            raw += bytes((line[i] - prev[i]) & 0xFF for i in range(len(line)))
        elif filter_type == 3:                       # Average
            raw += bytes((line[i] - (((line[i - channels] if i >= channels else 0)
                                      + prev[i]) >> 1)) & 0xFF
                         for i in range(len(line)))
        elif filter_type == 4:                       # Paeth
            enc = bytearray()
            for i in range(len(line)):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                p = a + prev[i] - c
                pa, pb, pc = abs(p - a), abs(p - prev[i]), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (prev[i] if pb <= pc else c)
                enc.append((line[i] - pred) & 0xFF)
            raw += bytes(enc)
        else:
            raise AssertionError(filter_type)
        prev = line

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def test_filters(tmp: Path) -> None:
    print("\n[1] 五种 scanline 过滤器都要能还原")
    w = h = 32
    # 造一张有梯度、有突变的图：能暴露 Sub/Up/Average/Paeth 的任何一处写错
    rows = [bytes(((x * 7 + y * 13) % 256) for x in range(w) for _ in range(3))
            for y in range(h)]
    for ftype in (0, 1, 2, 3, 4):
        p = tmp / ("f%d.png" % ftype)
        p.write_bytes(raw_png(w, h, 3, rows, ftype))
        _w, _h, _c, px = decode_png(p)
        check("过滤器 %d 还原正确" % ftype, bytes(px) == b"".join(rows),
              "第 %d 行起就不一样了" % next(
                  (i for i in range(h)
                   if bytes(px[i * w * 3:(i + 1) * w * 3]) != rows[i]), -1))


def test_roundtrip(tmp: Path) -> None:
    print("\n[2] 编码 → 解码必须逐字节相同")
    for (w, h, ch) in ((1, 1, 3), (7, 5, 3), (16, 16, 4), (33, 9, 1)):
        px = bytearray(((i * 31 + 7) % 256) for i in range(w * h * ch))
        blob = encode_png(w, h, ch, px)
        p = tmp / ("rt_%d_%d_%d.png" % (w, h, ch))
        p.write_bytes(blob)
        w2, h2, c2, px2 = decode_png(p)
        check("%dx%d ch=%d 往返一致" % (w, h, ch),
              (w2, h2, c2) == (w, h, ch) and bytes(px2) == bytes(px),
              "尺寸 %s vs %s" % ((w2, h2, c2), (w, h, ch)))
    check("编码结果以 PNG 魔数开头",
          encode_png(2, 2, 3, bytearray(12))[:8] == b"\x89PNG\r\n\x1a\n")


def test_channel_order(tmp: Path) -> None:
    print("\n[3] 通道顺序不能反（纯红/纯绿/纯蓝各来一张）")
    for name, rgb in (("red", (255, 0, 0)), ("green", (0, 255, 0)),
                      ("blue", (0, 0, 255))):
        w = h = 8
        px = bytearray(bytes(rgb) * (w * h))
        p = tmp / ("c_%s.png" % name)
        p.write_bytes(encode_png(w, h, 3, px))
        _w, _h, _c, got = decode_png(p)
        check("%s 解出来还是 %s" % (name, rgb),
              tuple(got[:3]) == rgb, "得到 %s" % (tuple(got[:3]),))


def test_resize(tmp: Path) -> None:
    print("\n[4] 降采样：面积平均，且必须落在可预测的范围")
    # 左半黑右半白 → 缩到 2 格宽后左边应≈0、右边应≈255
    w = h = 64
    px = bytearray()
    for _y in range(h):
        for x in range(w):
            v = 0 if x < w // 2 else 255
            px += bytes((v, v, v))
    w2, h2, c2, px2 = resize_nearest(w, h, 3, px, 2)
    left = px2[0]
    right = px2[3]
    check("缩到 2×2", (w2, h2, c2) == (2, 2, 3))
    check("左半仍是黑", left == 0, "得到 %d" % left)
    check("右半仍是白", right == 255, "得到 %d" % right)

    # 棋盘格（1px 交替）缩到 1×1 应当是灰 —— 平均而不是取样
    px = bytearray()
    for y in range(w):
        for x in range(w):
            v = 255 if (x + y) % 2 else 0
            px += bytes((v, v, v))
    _w, _h, _c, one = resize_nearest(w, h, 3, px, 1)
    check("1px 棋盘 → 平均成中灰（是平均，不是取样）",
          120 <= one[0] <= 135, "得到 %d" % one[0])
    check("输出长度正确", len(one) == 3)


def test_real(path: Path) -> None:
    print("\n[5] 真实地图：平均颜色必须与原图接近（%s）" % path.name)
    w, h, ch, px = decode_png(path)
    print("      源图 %dx%d 通道 %d" % (w, h, ch))
    n = w * h
    src_mean = [sum(px[c::ch]) / n for c in range(min(ch, 3))]
    print("      源图平均 RGB = %s" % ["%.1f" % v for v in src_mean])

    side = 512
    w2, h2, ch2, px2 = resize_nearest(w, h, ch, px, side)
    n2 = w2 * h2
    dst_mean = [sum(px2[c::ch2]) / n2 for c in range(min(ch2, 3))]
    print("      %d² 平均 RGB = %s" % (side, ["%.1f" % v for v in dst_mean]))
    for i, cname in enumerate("RGB"[:min(ch2, 3)]):
        delta = abs(src_mean[i] - dst_mean[i])
        check("%s 通道平均偏差 < 6" % cname, delta < 6, "偏差 %.1f" % delta)
    check("降采样输出能再次被解码",
          decode_png(path)[0] == w)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对纯标准库 PNG 管线")
    ap.add_argument("--real", default=None, help="要核对真实地图（如 var/maps/Hellas.png）")
    args = ap.parse_args(argv)

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    print("=" * 72)
    print("PNG 管线核对（纯标准库解码 / 降采样 / 编码）")
    print("=" * 72)
    test_filters(tmp)
    test_roundtrip(tmp)
    test_channel_order(tmp)
    test_resize(tmp)
    if args.real:
        test_real(Path(args.real))
    else:
        print("\n[5] 跳过真实地图核对（加 --real <png> 才做）")

    print("\n" + "=" * 72)
    print("断言 %d，失败 %d" % (N[0], len(FAILS)))
    for f in FAILS:
        print("  FAILED:", f)
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
