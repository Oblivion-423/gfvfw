"""``.cam`` 容器（bundle）目录解析。

移植自 ``CamReader/Core/Bundle.cs``。

布局
----
::

    offset 0                : uint32  目录偏移 dir
    (中间为各内嵌文件数据)
    dir                     : uint32  内嵌文件数 n
    dir+4                   : n 条目录项
    目录项                  : uint8 名长 nl, nl 字节 ASCII 名,
                              uint32 数据偏移, uint32 数据长度

版本号取自内嵌的 ``.ver`` 文件（内容是十进制 ASCII 整数）；取不到时
CamReader 回退为 ``72``。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

__all__ = ["EmbeddedFile", "Bundle", "BundleError", "DEFAULT_VERSION"]

#: CamReader 在找不到 ``.ver`` 时使用的回退版本
DEFAULT_VERSION = 72

#: 目录项数量的合理上限 —— 防止损坏文件让解析器申请天量内存
_MAX_ENTRIES = 4096


class BundleError(ValueError):
    """`.cam` 容器结构非法。"""


@dataclass(frozen=True)
class EmbeddedFile:
    """内嵌文件在 ``.cam`` 中的位置。"""

    name: str
    offset: int
    size: int

    @property
    def ext(self) -> str:
        """小写扩展名（含点），例如 ``".cmp"``。"""
        i = self.name.rfind(".")
        return self.name[i:].lower() if i >= 0 else ""


class Bundle:
    """一个已解析的 ``.cam`` 存档。"""

    __slots__ = ("path", "raw", "files", "version")

    def __init__(self, path: Path, raw: bytes,
                 files: list[EmbeddedFile], version: int):
        self.path = path
        self.raw = raw
        self.files = files
        self.version = version

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def from_bytes(cls, raw: bytes, path: Path | None = None) -> "Bundle":
        if len(raw) < 8:
            raise BundleError("文件不足 8 字节，不是 .cam 容器：%d 字节" % len(raw))

        dir_off = struct.unpack_from("<I", raw, 0)[0]
        if dir_off + 4 > len(raw):
            raise BundleError("目录偏移 %d 超出文件长度 %d" % (dir_off, len(raw)))

        count = struct.unpack_from("<I", raw, dir_off)[0]
        if count > _MAX_ENTRIES:
            raise BundleError("目录项数量异常：%d（上限 %d）" % (count, _MAX_ENTRIES))

        cur = dir_off + 4
        files: list[EmbeddedFile] = []
        for i in range(count):
            if cur + 1 > len(raw):
                raise BundleError("目录第 %d 项越界（偏移 %d）" % (i, cur))
            name_len = raw[cur]
            cur += 1
            if cur + name_len + 8 > len(raw):
                raise BundleError("目录第 %d 项越界：名长 %d，偏移 %d" % (i, name_len, cur))
            name = raw[cur:cur + name_len].decode("ascii", "replace")
            cur += name_len
            off, size = struct.unpack_from("<II", raw, cur)
            cur += 8
            if off + size > len(raw):
                raise BundleError(
                    "内嵌文件 %r 数据越界：偏移 %d + 长度 %d > 文件长度 %d"
                    % (name, off, size, len(raw)))
            files.append(EmbeddedFile(name, off, size))

        version = cls._read_version(raw, files)
        return cls(path or Path("<bytes>"), raw, files, version)

    @classmethod
    def load(cls, path: str | Path) -> "Bundle":
        p = Path(path)
        return cls.from_bytes(p.read_bytes(), p)

    @staticmethod
    def _read_version(raw: bytes, files: list[EmbeddedFile]) -> int:
        for f in files:
            if f.name.lower().endswith(".ver"):
                txt = raw[f.offset:f.offset + f.size].decode("ascii", "replace")
                txt = txt.strip("\0 \t\r\n")
                try:
                    return int(txt)
                except ValueError:
                    pass
        return DEFAULT_VERSION

    # -- 取内嵌文件 ------------------------------------------------------

    def get(self, name: str) -> bytes | None:
        """按名字取内嵌文件原始字节（大小写不敏感）。"""
        low = name.lower()
        for f in self.files:
            if f.name.lower() == low:
                return self.raw[f.offset:f.offset + f.size]
        return None

    def get_by_ext(self, ext: str) -> bytes | None:
        """按扩展名取第一个匹配的内嵌文件原始字节（大小写不敏感）。"""
        low = ext.lower()
        if not low.startswith("."):
            low = "." + low
        for f in self.files:
            if f.name.lower().endswith(low):
                return self.raw[f.offset:f.offset + f.size]
        return None

    def find_by_ext(self, ext: str) -> EmbeddedFile | None:
        """按扩展名找目录项（不复制数据）。"""
        low = ext.lower()
        if not low.startswith("."):
            low = "." + low
        for f in self.files:
            if f.name.lower().endswith(low):
                return f
        return None

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.files]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<Bundle %s v%d files=%d>" % (self.path.name, self.version, len(self.files))
