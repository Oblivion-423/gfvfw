"""
决定性验证：**服务器上不安装 BMS**，只放必需的 47.9 MB 剧场数据，
能否得到与完整 BMS 安装完全一致的战役解析结果？

做法：
  1. 用真实 BMS 安装解析一份真实 .cam → 参考结果；
  2. 在临时目录里搭一个"假安装目录"，**只**复制解析必需的 8 个文件 +
     ObjectiveRelatedData，路径结构照搬 BMS；
  3. 用假安装目录解析同一份 .cam → 对照结果；
  4. 逐字段比对，并另跑一次"完全不配置安装目录"看是否给出可读的报错。

运行:
    .venv\\Scripts\\python.exe scripts\\bms_free_server_probe.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.campaign.state import build_state  # noqa: E402
from gfvfw.campaign.theater import (  # noqa: E402
    TheaterData, _find_dir, _find_file, clear_cache,
)
from gfvfw.config import settings  # noqa: E402

CAM_DIR = Path(r"G:\BMS\backup\Campaign")

#: 解析必需的 8 个文件（大小写两种拼写都试）
NEEDED_OBJECTS = (
    ("Falcon4_CT.xml", "FALCON4_CT.XML"),
    ("Falcon4_UCD.xml", "FALCON4_UCD.XML"),
    ("Falcon4_VCD.xml", "FALCON4_VCD.XML"),
    ("Falcon4_WCD.xml", "FALCON4_WCD.XML"),
    ("Falcon4_RCD.xml", "FALCON4_RCD.XML"),
    ("Falcon4_FCD.xml", "FALCON4_FCD.XML"),
)
NEEDED_CAMPAIGN = (
    ("CampObjData.xml", "CampObjData.XML"),
    ("strings.txt", "Strings.txt"),
)

FAILS: list[str] = []


def check(label, ok, detail=""):
    # ⚠️ detail 是**失败时**的说明（形如"缺失 [...]"），只在失败时打印。
    #    否则会出现 `PASS 至少一份存档含事件  全部存档事件数均为 0` 这种
    #    自相矛盾、极易误读的输出。
    print("      %s  %s%s" % ("PASS" if ok else "FAIL", label,
                            "" if ok else ("  " + detail)))
    if not ok:
        FAILS.append("%s %s" % (label, detail))


def fake_install(real: Path, dest: Path) -> tuple[Path, int]:
    """在 dest 里搭出只含必需数据的"安装目录"，返回 (路径, 字节数)。"""
    data = dest / "Data"
    objects = data / "TerrData" / "Objects"
    campaign = data / "Campaign"
    objects.mkdir(parents=True)
    campaign.mkdir(parents=True)

    copied = 0
    for names in NEEDED_OBJECTS:
        p = _find_file(real / "Data" / "TerrData" / "Objects", *names)
        if p is not None:
            shutil.copy2(p, objects / p.name)
            copied += p.stat().st_size
    for names in NEEDED_CAMPAIGN:
        p = _find_file(real / "Data" / "Campaign", *names)
        if p is not None:
            shutil.copy2(p, campaign / p.name)
            copied += p.stat().st_size

    ocd = _find_dir(real / "Data" / "TerrData" / "Objects", "ObjectiveRelatedData")
    if ocd is not None:
        shutil.copytree(ocd, objects / ocd.name)
        copied += sum(f.stat().st_size for f in ocd.rglob("*") if f.is_file())

    # ⚠️ Theater.txt 也必须带 —— 它给的是投影参数，缺了它地图页就算不出经纬度
    #    （bullseye_latlon / 光标经纬度读数都会变空），而它只有几百字节。
    for name in ("Korea",):
        src = _find_file(real / "Data" / "TerrData" / name / "NewTerrain",
                         "Theater.txt")
        if src is not None:
            dst_dir = data / "TerrData" / name / "NewTerrain"
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / src.name)
            copied += src.stat().st_size
            print("      已复制投影参数：TerrData/%s/NewTerrain/%s" % (name, src.name))
    return dest, copied


def snapshot(cam: Path, install: Path) -> dict:
    """用某个安装目录解析，抽出一份可比的指纹。"""
    clear_cache()
    th = TheaterData.load(install, "", cache=False)   # "" → Korea
    st = build_state(cam, th)
    from collections import Counter
    own = Counter(o.team_id for o in st.objectives)
    types = Counter(o.type_name for o in st.objectives)
    by_kind = Counter(u.unit_kind for u in st.units)
    # ⚠️ 投影必须从 theater_info 取真值。早先写成
    #    getattr(st.projection, "proj_string", None) —— Projection 上根本没有
    #    这个属性，于是两边都是 None，这项对比变成了空对空。
    info = getattr(th, "theater_info", None)
    proj_str = getattr(info, "projection_string", None) if info else None
    proj_obj = None
    if proj_str:
        from gfvfw.campaign.coords import Projection
        proj_obj = Projection.from_proj_string(proj_str)
    return {
        "theater": st.theater,
        "scenario": st.scenario,
        "cam_version": st.cam_version,
        "time_label": st.campaign_time_label,
        "day": st.campaign_day,
        "active_teams": st.active_teams,
        "situation": st.situation,
        "bullseye": (st.bullseye_east, st.bullseye_north),
        "theater_size": (st.theater_size_x, st.theater_size_y),
        "objectives": len(st.objectives),
        "ownership": dict(sorted(own.items())),
        "type_names": dict(sorted(types.items())),
        "unit_kinds": dict(sorted(by_kind.items())),
        "units_total": len(st.units),
        "squadrons": len(st.squadrons),
        "events": len(st.events),
        "event_texts": [e.text for e in st.events][:5],
        "teams": [(t.team_id, t.name, t.active, t.supply, t.fuel)
                  for t in st.teams],
        "proj": proj_str,
        "proj_params": None if proj_obj is None else (
            proj_obj.lon_0, proj_obj.lat_0, proj_obj.k_0, proj_obj.size_km),
        "bullseye_latlon": None if proj_obj is None else tuple(
            round(v, 6) for v in proj_obj.grid_to_latlon(
                float(st.bullseye_east), float(st.bullseye_north))),
        "unit_counts": dict(st.unit_counts or {}),
        "missing": sorted(th.missing),
        "type_unresolved": sum(1 for o in st.objectives
                               if o.type_name in ("Type-1", "Unknown", "")),
        "sections": sorted(st.sections or []),
    }


def main() -> int:
    real = Path(settings.bms_install_path or "")
    print("=" * 74)
    print("无 BMS 服务器可行性验证")
    print("真实安装目录：%s" % real)
    print("=" * 74)
    if not real.is_dir():
        print("!! 未配置真实 BMS 安装目录")
        return 1

    cams = sorted(CAM_DIR.glob("*.cam"), key=lambda p: -p.stat().st_size)
    if not cams:
        print("!! 在 %s 找不到 .cam" % CAM_DIR)
        return 1
    print("待测存档 %d 份（%s …）" % (len(cams), cams[0].name))

    # ---------- 参考结果：完整 BMS 安装 ----------
    print("\n[A] 用完整 BMS 安装解析全部存档（参考结果）")
    refs: dict[str, dict] = {}
    for cam in cams:
        try:
            refs[cam.name] = snapshot(cam, real)
        except Exception as exc:  # noqa: BLE001
            print("      %-34s 解析失败：%s" % (cam.name, str(exc)[:60]))
    for name, r in refs.items():
        print("      %-34s %s %-6s 目标点=%-5d 单位=%-4d 事件=%d"
              % (name, r["theater"], r["scenario"], r["objectives"],
                 r["units_total"], r["events"]))
    if not refs:
        print("!! 没有一份能解析成功")
        return 1

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dest = Path(td) / "fake-bms"
        print("\n[B] 搭一个只含必需数据的假安装目录（不装 BMS）")
        _, nbytes = fake_install(real, dest)
        print("      体积 %.1f MB，文件数 %d"
              % (nbytes / 1024 / 1024,
                 sum(1 for p in dest.rglob("*") if p.is_file())))

        print("\n[C] 用假安装目录解析同样的存档，逐字段比对")
        event_covered = False
        for name, ref in refs.items():
            got = snapshot(cams[[c.name for c in cams].index(name)], dest)
            keys = [k for k in ref if k not in ("missing", "event_texts")]
            bad = [k for k in keys if ref[k] != got.get(k)]
            print("      %-34s 关键字段 %d/%d 一致%s"
                  % (name, len(keys) - len(bad), len(keys),
                     "" if not bad else "  差异=" + ", ".join(bad)))
            for k in bad:
                check("%s · %s" % (name, k), False,
                      "完整=%r 假安装=%r" % (ref[k], got.get(k)))
            check("%s 无缺失表" % name, got["missing"] == [],
                  "缺失 %s" % got["missing"])
            check("%s 目标点类型全部解析" % name, got["type_unresolved"] == 0,
                  "未解析 %d" % got["type_unresolved"])
            check("%s 投影一致" % name, got["proj_params"] == ref["proj_params"],
                  "%r vs %r" % (got["proj_params"], ref["proj_params"]))
            check("%s 经纬度一致" % name,
                  got["bullseye_latlon"] == ref["bullseye_latlon"],
                  "%r vs %r" % (got["bullseye_latlon"], ref["bullseye_latlon"]))
            if ref["events"]:
                event_covered = True
                check("%s 事件文本逐条一致" % name,
                      ref["event_texts"] == got["event_texts"],
                      "%r vs %r" % (ref["event_texts"][:2], got["event_texts"][:2]))

        # ⚠️ 若所有存档都没事件，上面的"事件文本一致"就是空对空的通过
        check("至少一份存档含事件（事件对比非空对空）", event_covered,
              "全部存档事件数均为 0")
        # 同理：投影必须有真值
        check("至少一份存档有投影参数（投影对比非空对空）",
              any(r["proj"] for r in refs.values()),
              "proj 全为 None")

        print("\n[D] 完全不配置安装目录时的报错")
        from gfvfw.services.campaign import theater_data
        old = settings.bms_install_path
        settings.bms_install_path = None
        try:
            theater_data("Korea")
            check("未配置时应报错", False, "居然没抛异常")
        except FileNotFoundError as exc:
            msg = str(exc)
            check("未配置时给出可读报错", "GFVFW_BMS_INSTALL_PATH" in msg, msg[:110])
        finally:
            settings.bms_install_path = old
            clear_cache()

    print("\n" + "=" * 74)
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
    else:
        print("结论：只复制必需剧场数据（不安装 BMS）即可得到完全一致的结果。")
    print("=" * 74)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
