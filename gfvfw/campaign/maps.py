"""剧场地图图片的发现与选择。

BMS 安装目录里每个剧场都自带若干**正方形**剧场全图（``4096²`` / ``8192²`` /
``16384²``）。因为战役网格是 ``0..1023`` 的正方形，正方形地图正好一格对一格，
可以直接当 SVG 背景铺满整个 viewBox，不需要额外的对齐参数。

为什么不做缩放/转码
------------------
``HellasMap16K.png`` 有 **768 MB**、8K 有 192 MB，即使 4K 也有 48 MB。
本模块**只做发现与选择**，不做转码——转码需要 Pillow，而项目一直保持
依赖最小化。改法有两条，都留给使用者决定：

* 让联队自己压一张放到 ``GFVFW_BMS_MAP_DIR``（该目录优先），或
* 显式装 Pillow 后加一个生成缓存图的步骤（可把 4K 压到几 MB）。

因此页面上会把**每张图的体积列出来**，让人自己权衡"清晰度 vs 首次加载"。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["TheaterMap", "find_theater_maps", "pick_default_map",
           "png_dimensions", "MAP_DIR_SETTING_HINT"]

#: 提示：可让联队自备地图
MAP_DIR_SETTING_HINT = "GFVFW_BMS_MAP_DIR"

#: 认作"剧场全图"的最小边长（低于此值多半是图标/缩略图）
MIN_MAP_SIDE = 1024

#: 认作"剧场全图"的最小体积（低于此值多半不是全图）
MIN_MAP_BYTES = 512 * 1024

#: 只扫描这些子目录（相对剧场根），避免把 Charts/Wallpapers 里的图全捞进来
_MAP_SUBDIRS = (
    "Docs/02 Maps",
    "Docs/01 Maps",
    "Docs/Maps",
    "Docs/05 Maps",
    "Docs",
)

#: 基础剧场的图在 ``<安装>/Docs`` 下的这些子目录里（见 find_theater_maps 的说明）
_INSTALL_DOC_SUBDIRS = (
    "05 Maps",
    "04 Maps",
    "Maps",
)


@dataclass(frozen=True)
class TheaterMap:
    """一张可用的剧场全图。"""

    path: Path
    name: str
    width: int
    height: int
    size_bytes: int
    #: 来源：bms（BMS 自带）或 custom（联队自备目录）
    origin: str = "bms"

    @property
    def is_square(self) -> bool:
        return self.width == self.height

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000.0

    @property
    def size_label(self) -> str:
        mb = self.size_bytes / 1048576.0
        return "%.1f MB" % mb if mb >= 1 else "%d KB" % (self.size_bytes // 1024)

    @property
    def side_label(self) -> str:
        return "%d²" % self.width

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return "%s (%s, %s)" % (self.name, self.side_label, self.size_label)


def png_dimensions(path: Path) -> Optional[tuple[int, int]]:
    """只读 PNG 头拿宽高——**不依赖任何图形库**。

    返回 ``None`` 表示不是可识别的 PNG（或读不出来）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        w, h = struct.unpack(">II", head[16:24])
    except struct.error:
        return None
    if w <= 0 or h <= 0:
        return None
    # 挡住明显不合理的尺寸，避免把畸形文件当图用
    if w > 65536 or h > 65536:
        return None
    return w, h


def _scan_dir(directory: Path, origin: str) -> list[TheaterMap]:
    out: list[TheaterMap] = []
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.png")):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < MIN_MAP_BYTES:
            continue
        dims = png_dimensions(p)
        if dims is None:
            continue
        w, h = dims
        if w < MIN_MAP_SIDE or h < MIN_MAP_SIDE or w != h:
            continue        # 战役网格是正方形，非正方形图无法一格对一格铺满
        out.append(TheaterMap(path=p, name=p.name, width=w, height=h,
                              size_bytes=st.st_size, origin=origin))
    return out


def find_theater_maps(theater_root: Path | str | None,
                      custom_dir: Path | str | None = None,
                      theater: str = "") -> list[TheaterMap]:
    """列出某个剧场可用的全图，**按体积升序**（小图在前）。

    :param theater_root: 剧场根目录（``TheaterData.root``）
    :param custom_dir: 联队自备地图目录；里面形如 ``<剧场名>.png``、``*.png`` 都会被收
    :param theater: 剧场名，用于在自备目录里优先取同名文件
    """
    found: list[TheaterMap] = []
    seen: set[Path] = set()

    def add(items: Iterable[TheaterMap]) -> None:
        for m in items:
            key = m.path.resolve()
            if key in seen:
                continue
            seen.add(key)
            found.append(m)

    # 自备目录优先（联队自己压的图通常小得多）
    if custom_dir:
        cd = Path(custom_dir)
        if cd.is_dir():
            add(_scan_dir(cd, "custom"))

    if theater_root:
        root = Path(theater_root)
        for sub in _MAP_SUBDIRS:
            d = root / sub
            if d.is_dir():
                add(_scan_dir(d, "bms"))
        # ⚠️ 基础剧场（Korea）的地图不在 ``Data`` 下，而在**安装根**的 ``Docs`` 里：
        #    剧场根 == <安装>/Data 时其兄弟目录 <安装>/Docs 才是地图所在。
        #    必须用 ``root.name == 'data'`` 判定，否则给 Add-On 剧场扫描安装根的
        #    Docs 会把 Korea 的地图当成该剧场的地图（张冠李戴）。
        if root.name.lower() == "data":
            install_docs = root.parent / "Docs"
            for sub in _INSTALL_DOC_SUBDIRS:
                d = install_docs / sub
                if d.is_dir():
                    add(_scan_dir(d, "bms"))

    # 体积升序；同体积时按名字稳定排序
    found.sort(key=lambda m: (m.size_bytes, m.name))
    return found


def pick_default_map(maps: list[TheaterMap],
                     *, prefer_side: int = 4096) -> Optional[TheaterMap]:
    """挑一个默认图：优先边长 == ``prefer_side``（4K）里**体积最小**的。

    没有 4K 就退而取最接近 4K 的较小边长；再没有就取整体最小的。

    默认挑最小的 4K 而不是最大的——地图是页面首次加载的主要成本，
    768 MB 的 16K 图不该是默认值。
    """
    if not maps:
        return None
    exact = [m for m in maps if m.width == prefer_side]
    if exact:
        return min(exact, key=lambda m: (m.size_bytes, m.name))
    smaller = [m for m in maps if m.width <= prefer_side]
    if smaller:
        best_side = max(m.width for m in smaller)
        return min((m for m in smaller if m.width == best_side),
                   key=lambda m: (m.size_bytes, m.name))
    return min(maps, key=lambda m: (m.size_bytes, m.name))
