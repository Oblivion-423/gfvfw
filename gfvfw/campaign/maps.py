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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["TheaterMap", "MapScan", "MapDiscovery",
           "find_theater_maps", "discover_theater_maps", "pick_default_map",
           "png_dimensions", "MAP_DIR_SETTING_HINT"]

#: 提示：可让联队自备地图
MAP_DIR_SETTING_HINT = "GFVFW_BMS_MAP_DIR"

#: 认作"剧场全图"的最小边长（低于此值多半是图标/缩略图）
MIN_MAP_SIDE = 1024

#: 认作"剧场全图"的最小体积（低于此值多半不是全图）。
#:
#: ⚠️ 这个下限**故意定得很低**。它曾经是 512 KB —— 而文档告诉联队
#: "自压一张正方形 PNG 放进 GFVFW_BMS_MAP_DIR"，一张压缩良好的 1024²
#: 地图大约只有 **200 KB**：那条建议产出的文件会被这个阈值**静默丢掉**，
#: 用户按文档做了却看不到底图，且页面上没有任何提示说明为什么。
#: 真正的"是不是全图"由 MIN_MAP_SIDE + 正方形两个条件把住（图标、
#: 停机坪图、壁纸都过不了），体积只用来挡住明显损坏的空壳文件。
MIN_MAP_BYTES = 64 * 1024

#: 每个目录最多记录多少条"看到了但没用上"的候选（供页面自我诊断）
MAX_REJECTED_PER_DIR = 20

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


@dataclass(frozen=True)
class MapScan:
    """一个被检查过的目录，以及它给出（或没给出）什么。"""

    directory: Path
    origin: str
    exists: bool
    accepted: int = 0
    #: ``(文件名, 字节数, 为什么没用上)``
    rejected: tuple[tuple[str, int, str], ...] = ()


@dataclass
class MapDiscovery:
    """剧场地图发现结果 + **可展示的诊断信息**。"""

    maps: list[TheaterMap] = field(default_factory=list)
    scans: list[MapScan] = field(default_factory=list)
    custom_dir: Optional[Path] = None
    #: 剧场根目录（可能为 None —— 比如服务器上没配剧场数据）
    theater_root: Optional[Path] = None
    #: 发现过程中的致命错误（剧场数据缺失等），供页面照实说明
    problems: list[str] = field(default_factory=list)
    #: 自备目录里因为"不属于当前剧场"而被排除的文件
    filtered_out: tuple[str, ...] = ()

    @property
    def custom_dir_has_maps(self) -> bool:
        return any(m.origin == "custom" for m in self.maps)

    @property
    def scanned_dirs(self) -> list[MapScan]:
        """只列出**真的存在**的目录（不存在的目录列出来只会刷屏）。"""
        return [s for s in self.scans if s.exists]

    @property
    def near_misses(self) -> list[tuple[str, Path, int, str]]:
        """看起来像地图、但没通过检查的文件（按体积降序）。"""
        out = [(name, s.directory, size, why)
               for s in self.scans for (name, size, why) in s.rejected]
        out.sort(key=lambda t: -t[2])
        return out[:MAX_REJECTED_PER_DIR]


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


def _scan_dir(directory: Path, origin: str) -> tuple[list[TheaterMap], "MapScan"]:
    """扫一个目录，同时**如实记下看到了但没用上的文件**。

    为什么要记"没用的"：底图缺失时页面必须能回答"我到底找过哪里、为什么不算"。
    只回一个空列表的话，用户看到的就是"未找到剧场地图"这句话，
    而真正的原因（比如目录里只有非正方形的停机坪图）谁也猜不到。
    """
    out: list[TheaterMap] = []
    rejected: list[tuple[str, int, str]] = []
    if not directory.is_dir():
        return out, MapScan(directory=directory, origin=origin, exists=False)

    for p in sorted(directory.glob("*.png")):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < MIN_MAP_BYTES:
            rejected.append((p.name, st.st_size, "文件太小，不像全图"))
            continue
        dims = png_dimensions(p)
        if dims is None:
            rejected.append((p.name, st.st_size, "不是可识别的 PNG"))
            continue
        w, h = dims
        if w != h:
            rejected.append((p.name, st.st_size, "不是正方形 %d×%d" % (w, h)))
            continue        # 战役网格是正方形，非正方形图无法一格对一格铺满
        if w < MIN_MAP_SIDE:
            rejected.append((p.name, st.st_size, "边长 %d < %d" % (w, MIN_MAP_SIDE)))
            continue
        out.append(TheaterMap(path=p, name=p.name, width=w, height=h,
                              size_bytes=st.st_size, origin=origin))

    # 按体积升序，最大的那些通常是 8K/16K，塞不进提示里也没必要
    rejected.sort(key=lambda r: -r[1])
    return out, MapScan(directory=directory, origin=origin, exists=True,
                        accepted=len(out),
                        rejected=tuple(rejected[:MAX_REJECTED_PER_DIR]))


def _name_key(s: str) -> str:
    """把名字压成可比对的键：只留字母数字，小写。"""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def discover_theater_maps(theater_root: Path | str | None,
                          custom_dir: Path | str | None = None,
                          theater: str = "") -> MapDiscovery:
    """列出可用全图，**并给出完整的搜索过程**（供页面自我诊断）。

    :param theater_root: 剧场根目录（``TheaterData.root``）；给 ``None``
        也能工作 —— 此时只扫自备目录（服务器上没配剧场数据时就是这个场景，
        联队自压的图仍然必须能用）
    :param custom_dir: 联队自备地图目录（``GFVFW_BMS_MAP_DIR``）
    :param theater: 剧场名，用于在自备目录里优先取同名文件
    """
    disc = MapDiscovery(custom_dir=Path(custom_dir) if custom_dir else None,
                        theater_root=Path(theater_root) if theater_root else None)
    seen: set[Path] = set()

    def add(items: Iterable[TheaterMap]) -> None:
        for m in items:
            key = m.path.resolve()
            if key in seen:
                continue
            seen.add(key)
            disc.maps.append(m)

    # 自备目录优先（联队自己压的图通常小得多）。
    # ⚠️ 这一段**不依赖剧场数据**：它是服务器上的主要途径。
    if disc.custom_dir:
        maps, scan = _scan_dir(disc.custom_dir, "custom")
        disc.scans.append(scan)
        # ⚠️ 一个自备目录里可能同时放了好几个剧场的地图（`Hellas.png` /
        #    `Korea.png` / …）。不按剧场名筛一下的话，每个剧场都会把**别人家
        #    的地图**也列出来，还可能把它当成默认底图 —— 那就成了拿错地图
        #    盖在态势图上，比没有底图更糟。约定：文件名里含剧场名即算本剧场；
        #    一个都没匹配上时才退回"整个目录都用"（单剧场部署的常见情形）。
        if theater and maps:
            key = _name_key(theater)
            match = [m for m in maps if key and key in _name_key(m.path.stem)]
            if match:
                disc.filtered_out = tuple(
                    m.name for m in maps if m not in match)
                maps = match
        add(maps)

    if disc.theater_root:
        root = disc.theater_root
        for sub in _MAP_SUBDIRS:
            d = root / sub
            maps, scan = _scan_dir(d, "bms")
            disc.scans.append(scan)
            add(maps)
        # ⚠️ 基础剧场（Korea）的地图不在 ``Data`` 下，而在**安装根**的 ``Docs`` 里：
        #    剧场根 == <安装>/Data 时其兄弟目录 <安装>/Docs 才是地图所在。
        #    必须用 ``root.name == 'data'`` 判定，否则给 Add-On 剧场扫描安装根的
        #    Docs 会把 Korea 的地图当成该剧场的地图（张冠李戴）。
        if root.name.lower() == "data":
            install_docs = root.parent / "Docs"
            for sub in _INSTALL_DOC_SUBDIRS:
                d = install_docs / sub
                maps, scan = _scan_dir(d, "bms")
                disc.scans.append(scan)
                add(maps)

    # 体积升序；同体积时按名字稳定排序
    disc.maps.sort(key=lambda m: (m.size_bytes, m.name))
    return disc


def find_theater_maps(theater_root: Path | str | None,
                      custom_dir: Path | str | None = None,
                      theater: str = "") -> list[TheaterMap]:
    """只要图列表（``discover_theater_maps`` 的薄封装，保持旧调用点不变）。"""
    return discover_theater_maps(theater_root, custom_dir,
                                 theater=theater).maps


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
