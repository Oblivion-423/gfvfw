"""把 BMS 自带的剧场全图**挑一张、压到能用的体积、摆成服务器要的样子**。

为什么需要它
------------
态势图的底图来自 BMS 安装目录（``Docs/**/Maps/*.png``），而**服务器上通常
没有** —— 整套四个剧场的图加起来 1.6 GB（Hellas 16K 单张就有 768 MB），
所以部署文档一直写着"不要拷底图"。结果就是线上态势图永远没有背景图。

但**每个剧场都有一张 4K 图，只有 7~14 MB**。把这一张拷上去就够了：

    Hellas   7.4 MB   HellasMap4K_Airports.png
    Balkans  7.6 MB   Balkans Map 1_4K.png
    Israel  13.4 MB   ITO_Map_4K.png
    Korea   10.7 MB   1_KTO_4k_Blank.png

这个脚本做的就是这件事：扫出每个剧场可用的图 → 每个剧场挑一张 →
（可选）纯标准库降采样到指定边长 → 按 ``<剧场名>.png`` 命名写进目标目录。
命名很重要：``gfvfw.campaign.maps`` 会用文件名里的剧场名把"别人家的地图"
挡掉，否则 Hellas 的战役可能被盖上 Korea 的地图。

用法
----
::

    # 1) 只看会挑哪几张、有多大（不写任何文件）
    .venv\\Scripts\\python.exe scripts\\collect_theater_maps.py

    # 2) 真正导出到 var/maps/（本地开发用，配合 GFVFW_BMS_MAP_DIR）
    .venv\\Scripts\\python.exe scripts\\collect_theater_maps.py --out var/maps

    # 3) 导出并顺手降采样到 2048（7~14 MB → 约 1~3 MB，肉眼几乎看不出差别）
    .venv\\Scripts\\python.exe scripts\\collect_theater_maps.py --out var/maps --side 2048

    # 4) 直接给出上传到服务器的命令
    .venv\\Scripts\\python.exe scripts\\collect_theater_maps.py --out var/maps --rsync user@host

之后在服务器上把 ``GFVFW_BMS_MAP_DIR`` 指向那个目录并重启服务即可
（见 ``deploy/DEPLOY.md`` 第 5 节）。

⚠️ 关于降采样：项目一直不用 Pillow，这里也不引入。纯标准库解 PNG 需要
逐字节还原 scanline 过滤器，**4096² 大约要几十秒**；8192² 以上会慢到
不可接受，脚本会直接拒掉并让你改用 --side 更小的源图或直接拷 4K 原图。
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zlib
from pathlib import Path

# ⚠️ 用 reconfigure 而不是 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, …)`。
#    后者会把 stdout **换掉**：包装器一旦被回收/关闭，后续任何 print 都抛
#    `ValueError: I/O operation on closed file` —— 而且常在跑到一半时才炸，
#    看起来像"脚本莫名其妙少做了几步"。reconfigure 只改编码与错误策略，
#    不动那个对象。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.campaign.bundle import Bundle            # noqa: E402
from gfvfw.campaign.cmpfile import read_cmp         # noqa: E402
from gfvfw.campaign.maps import (                   # noqa: E402
    MAP_DIR_SETTING_HINT, discover_theater_maps, pick_default_map,
    png_dimensions)

#: 纯标准库解 PNG 的现实上限（边长）。再大就慢到不该做。
MAX_DECODE_SIDE = 4096


# --------------------------------------------------------------------------
# 找出每个剧场
# --------------------------------------------------------------------------

def find_theaters(bms: Path) -> list[tuple[str, list[Path]]]:
    """列出 ``(剧场名, [候选剧场根目录…])``。

    做法是**从 .cam 存档反推**：读它的 .cmp 头就能拿到剧场名 —— 这比猜目录名
    可靠（Add-On Hellas 的剧场名是 ``Hellas``，基础剧场是 ``Korea``）。

    ⚠️ 一个剧场名可能对应**多个**目录：基础 Korea 的存档在 ``Data/Campaign``，
    而 ``Add-On Korea TvT`` 也报同一个剧场名，它自己却**没有** KTO 地图
    （那些在安装根的 ``Docs/05 Maps``）。只留第一个候选的话，就会挑到那个
    空目录、然后得出"这个剧场没有可用底图"的错误结论。所以这里把所有候选
    都留着，由调用方挑第一个**真的有图**的。
    """
    out: dict[str, list[Path]] = {}
    cams = sorted(bms.glob("Data/**/Campaign/*.cam")) + \
        sorted(bms.glob("Data/Campaign/*.cam"))
    for cam in cams:
        try:
            b = Bundle.load(cam)
            c = read_cmp(b.get_by_ext(".cmp"), b.version)
        except Exception:                                   # noqa: BLE001
            continue
        name = (c.theater_name or "").strip()
        if not name:
            continue
        # Campaign 目录的上一级就是剧场根（<安装>/Data 或 <安装>/Data/Add-On X）
        root = cam.parent.parent
        bucket = out.setdefault(name, [])
        if root not in bucket:
            bucket.append(root)
    # 基础安装（根目录就叫 Data）放最前面：它的 Docs 才是 KTO 地图的所在
    for bucket in out.values():
        bucket.sort(key=lambda p: (p.name.lower() != "data", str(p)))
    return sorted(out.items())


# --------------------------------------------------------------------------
# 纯标准库 PNG 解码 / 降采样 / 编码
# --------------------------------------------------------------------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(path: Path) -> tuple[int, int, int, bytearray]:
    """解码成 ``(宽, 高, 通道数, 原始字节)``。只支持 8 位非隔行真彩/灰度。

    故意只做"够用"的子集：BMS 的剧场图都是 8 位 RGB/RGBA 非隔行。
    遇到别的就明确报错，而不是猜。
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos = 8
    width = height = depth = color = interlace = None
    idat = bytearray()
    palette: list[tuple[int, int, int]] = []
    trns = b""
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]  # type: ignore
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IEND":
            break

    if depth != 8:
        raise ValueError("只支持 8 位色深（这张是 %s）" % depth)
    if interlace:
        raise ValueError("不支持隔行（Adam7）PNG")
    if color == 2:
        channels = 3
    elif color == 6:
        channels = 4
    elif color == 0:
        channels = 1
    elif color == 3:
        channels = 1      # 调色板：先按"每像素 1 字节的下标"解过滤器
    else:
        raise ValueError("不支持的 PNG 颜色类型 %s" % color)

    raw = _unfilter(zlib.decompress(bytes(idat)), width, height, channels)
    if color == 3:
        # ⚠️ 调色板展开必须放在**解过滤器之后**。
        #    早先的写法把两者揉在一起，只认过滤器 0/1/2，于是遇到用 Paeth(4)
        #    的调色板 PNG（BMS 的 Balkans 地图就是）直接抛错。
        #    下标流本身就是 width 字节/行，用通用解过滤器走一遍最省事也最不容易错。
        raw = _expand_palette(raw, width, height, palette, trns)
        channels = 4 if trns else 3
    return width, height, channels, raw


def _unfilter(raw: bytes, width: int, height: int,
              channels: int) -> bytearray:
    """把 PNG 的 scanline 过滤器全部还原，**支持全部 5 种过滤器类型**。"""
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ValueError("IDAT 解压后只有 %d 字节，expected ≥ %d"
                         % (len(raw), expected))
    for y in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ftype == 0:
            pass
        elif ftype == 1:                      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:                      # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                      # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                      # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError("未知的 scanline 过滤器类型 %d（第 %d 行）" % (ftype, y))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _expand_palette(indices: bytearray, width: int, height: int,
                    palette: list, trns: bytes) -> bytearray:
    """调色板下标 → RGB(A)。输入是**已解过滤器**的下标流。"""
    has_alpha = bool(trns)
    dst_channels = 4 if has_alpha else 3
    out = bytearray(width * height * dst_channels)
    for y in range(height):
        base = y * width
        dst = base * dst_channels
        for x in range(width):
            idx = indices[base + x]
            r, g, b = palette[idx] if idx < len(palette) else (0, 0, 0)
            d = dst + x * dst_channels
            out[d] = r
            out[d + 1] = g
            out[d + 2] = b
            if has_alpha:
                out[d + 3] = trns[idx] if idx < len(trns) else 255
    return out


def resize_nearest(width: int, height: int, channels: int, pixels: bytearray,
                   side: int) -> tuple[int, int, int, bytearray]:
    """**盒式平均**降采样到 ``side × side``。

    用面积平均而不是最近邻：地图上有细线条（国界、跑道），最近邻会丢成
    断线，平均则保留灰阶痕迹。整倍数降采样时退化成精确的分块平均。
    """
    out = bytearray(side * side * channels)
    for oy in range(side):
        y0 = oy * height // side
        y1 = max(y0 + 1, (oy + 1) * height // side)
        for ox in range(side):
            x0 = ox * width // side
            x1 = max(x0 + 1, (ox + 1) * width // side)
            n = (y1 - y0) * (x1 - x0)
            acc = [0] * channels
            for y in range(y0, y1):
                base = y * width * channels
                for x in range(x0, x1):
                    p = base + x * channels
                    for c in range(channels):
                        acc[c] += pixels[p + c]
            d = (oy * side + ox) * channels
            for c in range(channels):
                out[d + c] = acc[c] // n
    return side, side, channels, out


def encode_png(width: int, height: int, channels: int,
               pixels: bytearray) -> bytes:
    """编码成 8 位真彩 PNG。**不做行间预测**（filter 0），实现简单且够小。"""
    color = {1: 0, 3: 2, 4: 6}[channels]
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # filter type: None
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def safe_name(theater: str) -> str:
    """剧场名 → 安全文件名（保留字母数字与少数符号）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", theater).strip("._")
    return cleaned or "theater"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="挑出每个剧场的 4K 底图并（可选）降采样，供服务器使用")
    ap.add_argument("--bms", default=os.environ.get("GFVFW_BMS_INSTALL_PATH")
                    or r"G:\BMS\Falcon BMS 4.38",
                    help="BMS 安装目录（默认读 GFVFW_BMS_INSTALL_PATH）")
    ap.add_argument("--out", default=None,
                    help="导出目录；不给则只做干跑（不写任何文件）")
    ap.add_argument("--side", type=int, default=0,
                    help="降采样到该边长（正方形）。0 = 原样拷贝（推荐）")
    ap.add_argument("--max-mb", type=float, default=20.0,
                    help="单张图的体积上限，超过就跳过（默认 20）")
    ap.add_argument("--rsync", default=None, metavar="USER@HOST",
                    help="顺便打印 rsync 命令")
    ap.add_argument("--dest", default="/srv/gfvfw/bms-data/maps",
                    help="服务器上的目标目录（配合 --rsync）")
    args = ap.parse_args(argv)

    bms = Path(args.bms)
    if not bms.is_dir():
        print("找不到 BMS 安装目录：%s" % bms)
        return 2

    theaters = find_theaters(bms)
    if not theaters:
        print("在 %s 下没找到任何 .cam 存档，无法确定剧场" % bms)
        return 2
    print("BMS 安装目录：%s" % bms)
    print("发现 %d 个剧场：%s" % (len(theaters), "、".join(n for n, _ in theaters)))

    custom = os.environ.get(MAP_DIR_SETTING_HINT)
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print()
    picked: list[tuple[str, Path]] = []
    for name, roots in theaters:
        # 逐个候选目录试：同一个剧场名可能对应多个目录，其中一些根本没有地图
        found_any = None
        choice = None
        for root in roots:
            disc = discover_theater_maps(root, custom, theater=name)
            bms_maps = [m for m in disc.maps if m.origin == "bms"]
            if bms_maps and found_any is None:
                found_any = (root, len(bms_maps))
            usable = [m for m in bms_maps
                      if m.size_bytes <= args.max_mb * 1048576]
            if usable:
                choice = pick_default_map(usable) or usable[0]
                break
        if choice is None:
            if found_any is None:
                print("  %-10s ✗ 这些目录里都没有剧场全图" % name)
                for root in roots:
                    print("  %-10s   试过 %s" % ("", root))
            else:
                print("  %-10s ✗ 有 %d 张图，但都超过 %.0f MB 上限"
                      % (name, found_any[1], args.max_mb))
            continue
        line = "  %-10s %-30s %-7s %8.1f MB" % (
            name, choice.name, choice.side_label, choice.size_bytes / 1048576)
        if args.side and choice.width > args.side:
            if choice.width > MAX_DECODE_SIDE:
                print(line + "  ✗ 边长 >%d，纯标准库解不动；请改用更小的源图"
                      % MAX_DECODE_SIDE)
                continue
            line += "  → %d²" % args.side
        print(line)
        picked.append((name, choice.path))

    if not picked:
        print("\n没有任何剧场可用。")
        return 1

    if out_dir is None:
        print("\n（干跑，没有写文件。加 --out <目录> 才会真正导出）")
        return 0

    print()
    saved: list[str] = []
    for name, src in picked:
        dest = out_dir / (safe_name(name) + ".png")
        if args.side and (png_dimensions(src) or (0, 0))[0] > args.side:
            print("  降采样 %s → %d² …（纯标准库，4096² 需几十秒）" % (src.name, args.side))
            w, h, ch, px = decode_png(src)
            w2, h2, ch2, px2 = resize_nearest(w, h, ch, px, args.side)
            dest.write_bytes(encode_png(w2, h2, ch2, px2))
        else:
            dest.write_bytes(src.read_bytes())
        mb = dest.stat().st_size / 1048576
        print("  ✓ %-22s %s（%.1f MB）" % (dest.name, dest, mb))
        saved.append(dest.name)

    total = sum((out_dir / n).stat().st_size for n in saved) / 1048576
    print("\n导出 %d 张，合计 %.1f MB → %s" % (len(saved), total, out_dir))
    print("文件名里的剧场名是关键：gfvfw 用它把'别人家的地图'挡掉。")

    print("\n服务器上要做的事：")
    print("  1) 把这些图放到一个目录，例如 %s" % args.dest)
    print("  2) 在 /etc/gfvfw/env 里加一行：")
    print("       %s=%s" % (MAP_DIR_SETTING_HINT, args.dest))
    print("  3) 在 gfvfw.service 的 ReadWritePaths 里不需要加它（只读即可），")
    print("     但目录要对 gfvfw 可读：chown -R gfvfw:gfvfw %s" % args.dest)
    print("  4) systemctl restart gfvfw")
    if args.rsync:
        print("\n或直接从本机推上去：")
        print("  rsync -av --mkpath %s/ %s:%s/"
              % (out_dir.as_posix(), args.rsync, args.dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
