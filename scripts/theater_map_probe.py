"""查清"态势图底图为什么不出来"：把发现链路每一环都打出来。

用法::

    .venv\\Scripts\\python.exe scripts/theater_map_probe.py "G:\\BMS\\Falcon BMS 4.38"

不传参数则读 GFVFW_BMS_INSTALL_PATH（再退回到本机默认路径）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 见 collect_theater_maps.py 里的说明：reconfigure 而不是换掉 sys.stdout。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.campaign.bundle import Bundle          # noqa: E402
from gfvfw.campaign.cmpfile import read_cmp       # noqa: E402
from gfvfw.campaign.maps import (                 # noqa: E402
    MAP_DIR_SETTING_HINT, MIN_MAP_BYTES, MIN_MAP_SIDE, find_theater_maps,
    pick_default_map, png_dimensions)
from gfvfw.campaign.theater import TheaterData    # noqa: E402

bms = Path(sys.argv[1] if len(sys.argv) > 1
           else os.environ.get("GFVFW_BMS_INSTALL_PATH")
           or r"G:\BMS\Falcon BMS 4.38")
custom = os.environ.get(MAP_DIR_SETTING_HINT)

print("BMS 安装目录 : %s（%s）" % (bms, "存在" if bms.is_dir() else "**不存在**"))
print("%s : %s" % (MAP_DIR_SETTING_HINT, custom or "（未设置）"))

# ── 1. 找一份 .cam，看它的剧场根目录在哪 ──────────────────────────────
cams = []
for sub in ("Data/Add-On Hellas 2026", "Data/Add-On Hellas", "Data"):
    d = bms / sub / "Campaign"
    if d.is_dir():
        cams.extend(sorted(d.glob("*.cam")))
if not cams:
    print("\n没找到 .cam，跳过剧场根目录解析")
    sys.exit(0)

cam = cams[0]
b = Bundle.load(cam)
c = read_cmp(b.get_by_ext(".cmp"), b.version)
print("\n样例存档     : %s  剧场 %r" % (cam.name, c.theater_name))
th = TheaterData.load(bms, c.theater_name)
root = getattr(th, "root", None)
print("剧场根目录   : %s" % root)

# ── 2. 逐个子目录看有没有候选图 ───────────────────────────────────────
print("\n── 各候选子目录 ──")
subdirs = ("Docs/02 Maps", "Docs/01 Maps", "Docs/Maps", "Docs/05 Maps", "Docs")
for sub in subdirs:
    d = (Path(root) / sub) if root else None
    if d is None or not d.is_dir():
        print("  %-16s 不存在" % sub)
        continue
    pngs = sorted(d.glob("*.png"))
    print("  %-16s %d 个 png" % (sub, len(pngs)))
    for p in pngs[:8]:
        st = p.stat()
        dims = png_dimensions(p)
        why = []
        if st.st_size < MIN_MAP_BYTES:
            why.append("体积 < %d KB" % (MIN_MAP_BYTES // 1024))
        if dims is None:
            why.append("不是 PNG")
        else:
            w, h = dims
            if w < MIN_MAP_SIDE or h < MIN_MAP_SIDE:
                why.append("边长 < %d" % MIN_MAP_SIDE)
            if w != h:
                why.append("非正方形 %dx%d" % (w, h))
        print("      %-34s %8.1f MB  %s  %s"
              % (p.name, st.st_size / 1048576.0,
                 ("%dx%d" % dims) if dims else "?", "✗ " + "；".join(why) if why else "✓ 可用"))

# 基础剧场：地图在安装根的 Docs 下
if root and Path(root).name.lower() == "data":
    print("\n── 安装根 Docs（基础剧场 Korea 的地图在此）──")
    for sub in ("05 Maps", "04 Maps", "Maps"):
        d = Path(root).parent / "Docs" / sub
        print("  %-16s %s" % (sub, "存在，%d 个 png" % len(list(d.glob("*.png")))
                              if d.is_dir() else "不存在"))

# ── 3. 最终发现结果 ───────────────────────────────────────────────────
print("\n── find_theater_maps() 结果 ──")
maps = find_theater_maps(root, custom, theater=c.theater_name or "")
if not maps:
    print("  （空）—— 这就是页面上没有底图的原因")
else:
    for i, m in enumerate(maps):
        print("  [%d] %-32s %-8s %-9s %s" % (i, m.name, m.side_label,
                                             m.size_label, m.origin))
    d = pick_default_map(maps)
    print("  默认选中：%s（%s，%s）" % (d.name, d.side_label, d.size_label))
