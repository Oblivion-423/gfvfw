"""从联队队标原图生成首页用的两份 PNG（裁掉透明留白 + 缩到显示尺寸）。

为什么要这个脚本
----------------
队标原图是 810×766 的 PNG，但**内容只占 x158..611 / y105..641**（454×537），
四周是纯透明留白。直接摆到页面上会有约 30% 的空白框，队标显得又小又偏；
而且原图约 350 KB，首页上只显示 84 px 高，手机端白下 300 KB。

联队日后换了队标，重跑一次即可（不带参数就用仓库里存的原图）：

    .venv\\Scripts\\python.exe scripts/prepare_logo.py

产出（三份都进 git）：
    gfvfw/web/static/img/logo-source.png  **原图存档**（810×766，含原始的透明留白）
    gfvfw/web/static/img/logo-full.png    裁过留白，像素尺寸不变（无损）
    gfvfw/web/static/img/logo.png         再按面积平均缩到高 240（首页实际引用这份）

⚠️ 三处容易做错的地方，这里都特意处理了：

1. **缩放必须按 alpha 加权（预乘）**。透明像素的 RGB 是未定义的（多数工具写 0），
   直接平均会在边缘糊出一圈黑边。做法是先累加 ``rgb * a`` 与 ``a``，
   最后用总 alpha 反预乘。
2. **裁剪要留一点边距**，否则队标贴着框边，看起来像被切掉了。
3. **不要把队标"放进一个卡片里"**。它是**透明底**的，套背景/圆角/边框会盖住
   轮廓。首页 CSS（`.wing-logo`）刻意只给 drop-shadow，不给底色 ——
   想加浅色底盘之前先看下面那条对比度体检的结论。

跑完还会打印一份**对比度体检**：把队标合成到若干个候选底色上，统计有多少不透明
像素的 WCAG 对比度低于 1.6（＝糊进背景里）。结论有点反直觉 ——
队标主体是中灰，所以**中灰底盘反而更差**，只有接近白的底盘才真的改善。
数字会随每次运行重新算，换队标后先看它再决定要不要垫底盘。

纯标准库实现（zlib + 手写 PNG 编解码），**不引入 Pillow/numpy** ——
本项目对依赖极度克制，只为处理一张静态图不值得加图像库。
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "gfvfw" / "web" / "static" / "img"

#: 不带参数时用的原图 —— 就是产出目录里的存档那一份，所以重跑无需参数
DEFAULT_SRC = DEFAULT_OUT / "logo-source.png"

#: 显示用的高度上限（CSS 里按 96px 摆，留约 2.5× 供高分屏）
TARGET_HEIGHT = 240

#: 内容框外保留的边距比例（相对内容的长边）
PAD_RATIO = 0.02

#: 页面底色，取自 app.css 的 `--bg`。改主题时要一起改。
PAGE_BG = (0x10, 0x14, 0x1A)

#: 首页 CSS 里 .wing-logo 的实际显示高度（app.css）
DISPLAY_HEIGHT = 96

#: 对比度体检用的候选底盘 —— 结论见 app.css 的 .wing-logo 注释
CONTRAST_BACKINGS = (
    ("不加底盘（--bg #10141a）", PAGE_BG),
    ("#2b3644（--border）", (0x2B, 0x36, 0x44)),
    ("#3a485a（--bg-elevated）", (0x3A, 0x48, 0x5A)),
    ("#8a97a6（--fg-muted）", (0x8A, 0x97, 0xA6)),
    ("#dfe6ee（--fg，接近白）", (0xDF, 0xE6, 0xEE)),
)


# --------------------------------------------------------------------------
# PNG 解码
# --------------------------------------------------------------------------

def decode_png(path: Path) -> tuple[int, int, int, bytearray]:
    """返回 ``(宽, 高, 通道数, 像素字节)``。

    支持 8 位非隔行的灰度/RGB/灰度+alpha/RGBA —— 队标是工具导出的普通
    RGBA PNG，够用了。遇到别的格式会**明确报错**，而不是悄悄给出一张错图。
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s 不是 PNG" % path)

    pos = 8
    idat = bytearray()
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body[:13])
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln

    if depth != 8:
        raise ValueError("只支持 8 位深度，实际 %s" % depth)
    if interlace:
        raise ValueError("不支持隔行（Adam7）PNG")
    if ctype not in (0, 2, 4, 6):
        raise ValueError("不支持的 color type %s" % ctype)

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(w * h * channels)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:                       # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ft == 2:                     # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                     # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                     # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError("未知的滤波类型 %s" % ft)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, channels, out


def to_rgba(w: int, h: int, channels: int, px: bytearray) -> bytearray:
    """统一成 RGBA —— 灰度/无 alpha 的图也能走同一条后面处理。"""
    if channels == 4:
        return px
    out = bytearray(w * h * 4)
    for i in range(w * h):
        s, d = i * channels, i * 4
        if channels == 1:                  # 灰度
            out[d] = out[d + 1] = out[d + 2] = px[s]
            out[d + 3] = 255
        elif channels == 2:                # 灰度 + alpha
            out[d] = out[d + 1] = out[d + 2] = px[s]
            out[d + 3] = px[s + 1]
        else:                              # RGB
            out[d:d + 3] = px[s:s + 3]
            out[d + 3] = 255
    return out


# --------------------------------------------------------------------------
# PNG 编码（8 位 RGBA，滤波类型 0）
# --------------------------------------------------------------------------

def write_png(path: Path, w: int, h: int, px: bytearray) -> None:
    stride = w * 4
    raw = b"".join(b"\x00" + bytes(px[y * stride:(y + 1) * stride])
                   for y in range(h))

    def chunk(typ: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 9))
                     + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# 处理
# --------------------------------------------------------------------------

def content_box(w: int, h: int, px: bytearray, threshold: int = 8):
    """不透明内容的包围框 ``(x0, y0, x1, y1)``（右下为开区间）。"""
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            if px[row + x * 4 + 3] > threshold:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        raise ValueError("这张图整幅都是透明的 —— 大概不是队标？")
    return minx, miny, maxx + 1, maxy + 1


def crop(w: int, px: bytearray, x0: int, y0: int, cw: int, ch: int) -> bytearray:
    out = bytearray(cw * ch * 4)
    for y in range(ch):
        src = ((y0 + y) * w + x0) * 4
        out[y * cw * 4:(y + 1) * cw * 4] = px[src:src + cw * 4]
    return out


def area_resize(px: bytearray, w: int, h: int, nw: int, nh: int) -> bytearray:
    """面积平均 + **alpha 加权（预乘）**，避免透明边缘糊出黑边。

    每个目标像素覆盖源图上的一个矩形，按重叠面积给源像素加权；
    颜色先乘 alpha 再累加，最后用总 alpha 反预乘回来。
    """
    out = bytearray(nw * nh * 4)
    sx, sy = w / nw, h / nh
    for dy in range(nh):
        y0f, y1f = dy * sy, (dy + 1) * sy
        iy0, iy1 = int(y0f), min(h, int(y1f) + 1)
        for dx in range(nw):
            x0f, x1f = dx * sx, (dx + 1) * sx
            ix0, ix1 = int(x0f), min(w, int(x1f) + 1)
            ar = ag = ab = aa = wsum = 0.0
            for y in range(iy0, iy1):
                wy = min(y + 1, y1f) - max(y, y0f)
                if wy <= 0:
                    continue
                row = y * w * 4
                for x in range(ix0, ix1):
                    wx = min(x + 1, x1f) - max(x, x0f)
                    if wx <= 0:
                        continue
                    ww = wx * wy
                    wsum += ww
                    i = row + x * 4
                    a = px[i + 3] / 255.0
                    ar += px[i] * a * ww
                    ag += px[i + 1] * a * ww
                    ab += px[i + 2] * a * ww
                    aa += px[i + 3] * ww
            o = (dy * nw + dx) * 4
            if wsum <= 0 or aa <= 0:
                continue
            aw = aa / 255.0                 # 目标像素的 alpha 权重和
            out[o] = min(255, int(ar / aw + 0.5))
            out[o + 1] = min(255, int(ag / aw + 0.5))
            out[o + 2] = min(255, int(ab / aw + 0.5))
            out[o + 3] = min(255, int(aa / wsum + 0.5))
    return out


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if not src.is_file():
        print("找不到原图：%s" % src, file=sys.stderr)
        return 2
    outdir.mkdir(parents=True, exist_ok=True)

    w, h, ch, raw_px = decode_png(src)
    px = to_rgba(w, h, ch, raw_px)
    print("原图      %dx%d（%d 通道）" % (w, h, ch))

    x0, y0, x1, y1 = content_box(w, h, px)
    print("内容框    x %d..%d  y %d..%d" % (x0, x1 - 1, y0, y1 - 1))

    pad = max(4, int(PAD_RATIO * max(x1 - x0, y1 - y0)))
    cx0, cy0 = max(0, x0 - pad), max(0, y0 - pad)
    cx1, cy1 = min(w, x1 + pad), min(h, y1 + pad)
    cw, chh = cx1 - cx0, cy1 - cy0
    print("裁剪后    %dx%d（留 %dpx 边距）" % (cw, chh, pad))

    full = crop(w, px, cx0, cy0, cw, chh)
    p_full = outdir / "logo-full.png"
    write_png(p_full, cw, chh, full)
    print("  → %s  %d 字节" % (p_full.relative_to(ROOT), p_full.stat().st_size))

    nh = min(TARGET_HEIGHT, chh)
    nw = max(1, round(cw * nh / chh))
    small = area_resize(full, cw, chh, nw, nh)
    p_small = outdir / "logo.png"
    write_png(p_small, nw, nh, small)
    print("  → %s  %dx%d  %d 字节"
          % (p_small.relative_to(ROOT), nw, nh, p_small.stat().st_size))

    print("\n首页引用的就是 %s（静态目录挂载在 /static，"
          "所以 URL 是 /static/img/logo.png）。" % p_small.name)

    # 顺手报告"深色主题下会损失多少细节" —— 换队标时最该先看这个数
    print("\n深色主题（--bg #10141a）下的可读性：")
    for name, bg in CONTRAST_BACKINGS:
        lost = _lost_fraction(small, nw * nh, bg)
        print("  %-28s 对比度<1.6 的不透明像素 %5.1f%%" % (name, lost))
    print("  ⚠️ 中灰底盘**反而更差** —— 队标主体就是中灰，会被底盘吃掉。"
          "只有接近白的底盘才真的改善，详见 app.css 的 .wing-logo 注释。")

    print("\n想亲眼看一眼按显示尺寸渲染的效果：")
    print("  %s --preview-on-dark" % Path(__file__).name)
    return 0


def _lost_fraction(px: bytearray, npx: int, bg: tuple[int, int, int]) -> float:
    """把队标合成到底色上，算「WCAG 对比度 < 1.6」的不透明像素占比。

    1.6 是个刻意宽松的阈值 —— 只是想量出"有多少像素糊进背景里"，
    不是拿 WCAG 正文标准去要求一张徽记。
    """
    def rel(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def rl(rgb) -> float:
        return 0.2126 * rel(rgb[0]) + 0.7152 * rel(rgb[1]) + 0.0722 * rel(rgb[2])

    bgl = rl(bg)
    tot = lost = 0
    for i in range(0, npx * 4, 4):
        a = px[i + 3]
        if a <= 8:
            continue
        af = a / 255.0
        c = tuple(px[i + k] * af + bg[k] * (1 - af) for k in range(3))
        hi, lo = max(rl(c), bgl), min(rl(c), bgl)
        if (hi + 0.05) / (lo + 0.05) < 1.6:
            lost += 1
        tot += 1
    return 100.0 * lost / tot if tot else 0.0


def preview_on_dark() -> int:
    """按首页实际显示尺寸把队标合成到页面底色上，写一张图供人眼核对。

    ⚠️ 数字（对比度占比）只能说明"有多少像素糊了"，**不能**说明整体还认不认得出。
    换队标之后请真的看一眼这张合成图再决定要不要加底盘。
    """
    src = DEFAULT_OUT / "logo.png"
    if not src.is_file():
        print("先跑一次生成 %s" % src, file=sys.stderr)
        return 2
    w, h, ch, raw = decode_png(src)
    px = to_rgba(w, h, ch, raw)

    nh = DISPLAY_HEIGHT
    nw = max(1, round(w * nh / h))
    small = area_resize(px, w, h, nw, nh)

    bg = PAGE_BG
    pad_x, pad_y = 60, 30
    cw, chh = nw + pad_x * 2, nh + pad_y * 2
    canvas = bytearray()
    for _ in range(cw * chh):
        canvas += bytes(bg) + b"\xff"
    for y in range(nh):
        for x in range(nw):
            i = (y * nw + x) * 4
            a = small[i + 3] / 255.0
            if a <= 0:
                continue
            o = ((y + pad_y) * cw + (x + pad_x)) * 4
            for c in range(3):
                canvas[o + c] = int(small[i + c] * a + bg[c] * (1 - a) + 0.5)

    out = ROOT / "var" / "logo_on_dark.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, cw, chh, canvas)
    print("按 %dpx 高合成到 --bg 上：%s" % (nh, out))
    print("（var/ 不进 git；看完可以删）")
    return 0


if __name__ == "__main__":
    if "--preview-on-dark" in sys.argv:
        sys.exit(preview_on_dark())
    sys.exit(main())
