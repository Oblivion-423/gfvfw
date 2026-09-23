"""核对 DEPLOY.md §5 里要拷到服务器的剧场数据文件是否真的存在（只读）。

部署时最怕"文档里的路径过时了"：rsync 会静默少拷，
服务器上表现为地图算不出经纬度或缺名字，排查起来很绕。
本脚本在**开发机**上逐条核对，缺哪个就报哪个。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: 与 deploy/DEPLOY.md §5 的 rsync 列表**逐条对应**。
#: 改这里就要同步改文档，反之亦然。
REQUIRED = (
    "Data/TerrData/Objects/Falcon4_CT.xml",
    "Data/TerrData/Objects/Falcon4_UCD.xml",
    "Data/TerrData/Objects/Falcon4_VCD.xml",
    "Data/TerrData/Objects/Falcon4_WCD.xml",
    "Data/TerrData/Objects/Falcon4_RCD.xml",
    "Data/TerrData/Objects/Falcon4_FCD.xml",
    "Data/TerrData/Objects/ObjectiveRelatedData",
    "Data/Campaign/CampObjData.xml",
    "Data/Campaign/strings.txt",
    "Data/TerrData/Korea/NewTerrain/Theater.txt",
)

DEFAULT_BMS = r"G:\BMS\Falcon BMS 4.38"


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BMS)
    print("=" * 74)
    print("剧场数据核对：%s" % root)
    print("=" * 74)

    if not root.exists():
        print("✗ BMS 安装目录不存在：%s" % root)
        return 2

    missing: list[str] = []
    total = 0
    for rel in REQUIRED:
        p = root / rel
        if not p.exists():
            missing.append(rel)
            print("  ✗ 缺失  %s" % rel)
            continue
        size = dir_size(p)
        total += size
        kind = "目录" if p.is_dir() else "文件"
        print("  ✓ %-46s %s %8.2f MB" % (rel, kind, size / 1024 / 1024))

    print("-" * 74)
    print("必需部分合计：%.1f MB（文档里写的是 47.9 MB 量级）"
          % (total / 1024 / 1024))
    if missing:
        print("\n⚠️ 有 %d 项缺失 —— DEPLOY.md §5 的 rsync 命令会少拷这些，"
              "服务器上会缺名字/类型或算不出经纬度。" % len(missing))
        for m in missing:
            print("   - %s" % m)
        return 1
    print("\n全部就位：DEPLOY.md §5 的 rsync 命令可以直接用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
