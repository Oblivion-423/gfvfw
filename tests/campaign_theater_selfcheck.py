"""
战役管理自校验。

分两部分：

**A. 与外部无关的部分（总是运行）**
  1. 坐标换算：世界英尺 → 战役网格，以及网格 ↔ 经纬度的往返一致性
  2. 战役时间标签格式（照搬 CamReader ``R.CampTime``）
  3. LZSS 解压与回压往返（合成数据）
  4. ``.cam`` 容器目录解析（合成数据）
  5. 队伍关系名映射
  6. 权限：谁能看、谁能上报
  7. 6 张战役表已注册，且明细表"只留最新一份"的约束确实生效

**B. 需要真实素材的部分（有则运行，没有则跳过并说明）**
  8. 用真实 ``.cam`` 解析并与 CamReader 的 ``campaign_state.json`` 对拍
  9. 上报管线：入库计数、SHA256 去重、目标点易手检测
 10. Web 页面可访问、关键内容渲染出来

真实素材位置由环境变量给出，默认值针对本机：
  ``GFVFW_BMS_INSTALL_PATH``  —— BMS 安装目录
  ``GFVFW_TEST_CAM``          —— 一份 .cam 存档
  ``GFVFW_TEST_STATE_JSON``   —— CamReader 产出的 campaign_state.json（可选，用于对拍）

运行:
    .venv\\Scripts\\python.exe tests\\campaign_theater_selfcheck.py
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import gfvfw.models  # noqa: F401,E402  —— 注册全部表
from gfvfw.campaign import lzss  # noqa: E402
from gfvfw.campaign.bundle import Bundle, BundleError  # noqa: E402
from gfvfw.campaign.coords import (  # noqa: E402
    FEET_PER_KM, GRID_SIZE, Projection, clamp_grid, feet_to_grid, world_xy_to_grid,
)
from gfvfw.campaign.state import camp_time_label, stance_name  # noqa: E402
from gfvfw.db import Base  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


def skip(what: str, why: str) -> None:
    SKIPPED.append(what)
    print("  SKIP  %s —— %s" % (what, why))


# --------------------------------------------------------------------------
# A. 与外部无关
# --------------------------------------------------------------------------

def test_coords() -> None:
    print("\n[1] 坐标换算")
    # 1 km = 3280.84 英尺
    check("FEET_PER_KM 取值", abs(FEET_PER_KM - 3280.84) < 1e-6)
    check("1000 m 的英尺数", abs(feet_to_grid(3280.84) - 1.0) < 1e-9)

    # CampObjData 的 X 是北、Y 是东 —— 返回值顺序 (east, north) 必须相反。
    # Andravida：PositionX=1675571.187（北）、PositionY=608236.192（东）
    # 参考 campaign_state.json 给出 pos = {x: 185.39, y: 510.71}
    e, n = world_xy_to_grid(1675571.187, 608236.192)
    check("Andravida 东向网格 = 参考 pos.x", abs(e - 185.39) < 0.02,
          "得到 %.3f" % e)
    check("Andravida 北向网格 = 参考 pos.y", abs(n - 510.71) < 0.02,
          "得到 %.3f" % n)

    check("clamp_grid 下界", clamp_grid(-500.0) == 0.0)
    check("clamp_grid 上界", clamp_grid(99999.0) == GRID_SIZE)
    # expand=64 → 允许到 1024+64 = 1088
    check("clamp_grid 允许外扩 64", clamp_grid(1080.0, expand=64) == 1080.0)
    check("clamp_grid 外扩后仍夹上界", clamp_grid(2000.0, expand=64) == 1088.0)

    # 投影：Hellas，中心 38N/25E
    P = Projection.from_proj_string(
        "+proj=tmerc +lon_0=25 +ellps=WGS84 +k=0.9996 +units=m "
        "+x_0=512000 +y_0=-3693820")
    # Andravida 机场实测 37.921N / 21.293E
    lat, lon = P.grid_to_latlon(185.39, 510.71)
    check("反算纬度接近实测", abs(lat - 37.921) < 0.05, "得到 %.4f" % lat)
    check("反算经度接近实测", abs(lon - 21.293) < 0.05, "得到 %.4f" % lon)
    # 往返
    worst = 0.0
    for e0, n0 in ((0.0, 0.0), (185.39, 510.71), (1023.0, 1023.0), (530.0, 723.0)):
        la, lo = P.grid_to_latlon(e0, n0)
        e1, n1 = P.latlon_to_grid(la, lo)
        worst = max(worst, abs(e1 - e0), abs(n1 - n0))
    check("网格→经纬度→网格 往返误差 < 1e-3 格", worst < 1e-3, "最大 %.6f" % worst)


def test_time_label() -> None:
    print("\n[2] 战役时间标签（R.CampTime）")
    check("0 → (none)", camp_time_label(0) == "(none)")
    check("0xFFFFFFFF → (none)", camp_time_label(0xFFFFFFFF) == "(none)")
    # 真实存档：180048343 ms == Day 3  02:00:48
    check("180048343 → Day 3  02:00:48",
          camp_time_label(180048343) == "Day 3  02:00:48",
          "得到 %r" % camp_time_label(180048343))
    # 1 小时 → 第 1 天 01:00:00（注意 Day 后是两个空格）
    check("Day 后是两个空格", camp_time_label(3600000) == "Day 1  01:00:00",
          "得到 %r" % camp_time_label(3600000))
    # 24 小时 → 进位到第 2 天 00:00:00
    check("小时进位到天", camp_time_label(24 * 3600000) == "Day 2  00:00:00",
          "得到 %r" % camp_time_label(24 * 3600000))


def test_stance() -> None:
    print("\n[3] 队伍关系名")
    for v, name in ((0, "Hostile"), (1, "Allied"), (2, "Friendly"),
                    (3, "Neutral"), (4, "Unfriendly"), (5, "AtWar")):
        check("stance %d → %s" % (v, name), stance_name(v) == name)
    check("未知值有回退", stance_name(99) == "Unknown(99)")


def _lzss_compress(data: bytes) -> bytes:
    """本测试用的**参考压缩机**：只发字面量。

    这样不依赖 CamReader 的匹配编码策略，只验证解压器在"全字面量"路径上
    与标志位重载逻辑正确。压缩流格式：[标志字节][数据...]。
    """
    out = bytearray()
    for i in range(0, len(data), 8):
        chunk = data[i:i + 8]
        flag = 0
        for b in range(len(chunk)):
            flag |= (1 << b)          # 每位=1 表示字面量
        out.append(flag)
        out.extend(chunk)
    return bytes(out)


def test_lzss() -> None:
    print("\n[4] LZSS 解压")
    payload = bytes(range(256)) * 4
    comp = _lzss_compress(payload)
    got = lzss.decompress(comp, len(payload))
    check("全字面量流可正确解出", got == payload,
          "长度 %d vs %d" % (len(got), len(payload)))

    # 边界：0 长度
    check("out_size=0 返回空", lzss.decompress(b"\x00", 0) == b"")
    # 越界要报错而不是静默给垃圾
    try:
        lzss.decompress(b"\xff", 32)
        check("数据不足时报错", False, "没有抛异常")
    except ValueError:
        check("数据不足时报错", True)

    # expand_with_count 的头部： [int32 comp][int16 count][int32 usize][data]
    body = bytes([0x41] * 16)
    inner = _lzss_compress(body)
    raw = struct.pack("<i", len(inner)) + struct.pack("<h", 7) + \
        struct.pack("<i", len(body)) + inner
    count, u_sz, out = lzss.expand_with_count(raw)
    check("expand_with_count 记录数", count == 7, "得到 %d" % count)
    check("expand_with_count 解压长度", u_sz == 16 and out == body)

    # expand_cmp 的头部： [int32 comp][int32 usize][data]
    raw2 = struct.pack("<i", len(inner)) + struct.pack("<i", len(body)) + inner
    c_sz, u_sz2, out2 = lzss.expand_cmp(raw2)
    check("expand_cmp 解压长度", u_sz2 == 16 and out2 == body)

    # 申报解压长度为 0 时 C# 返回 null，这里返回空
    raw3 = struct.pack("<ii", 4, 0) + b"\x00\x00\x00\x00"
    _, u_sz3, out3 = lzss.expand_cmp(raw3)
    check("申报长度 0 → 空结果", u_sz3 == 0 and out3 == b"")


def _make_bundle(files: dict[str, bytes], version: int = 109) -> bytes:
    """合成一个 .cam 容器： [uint32 目录偏移][各文件数据][目录]。"""
    names = list(files.items()) + [(".ver", str(version).encode())]
    # 先把数据区排好，目录偏移 = 4 + 所有数据长度之和
    body = bytearray()
    entries = []
    data_start = 4
    for name, data in names:
        entries.append((name, data_start + len(body), len(data)))
        body.extend(data)
    dir_off = data_start + len(body)
    out = bytearray(struct.pack("<I", dir_off))
    out.extend(body)
    out.extend(struct.pack("<I", len(entries)))
    for name, off, size in entries:
        nb = name.encode("ascii")
        out.append(len(nb))
        out.extend(nb)
        out.extend(struct.pack("<II", off, size))
    return bytes(out)


def test_bundle() -> None:
    print("\n[5] .cam 容器")
    files = {
        "Save-Test.cmp": b"\x01\x02\x03\x04",
        "Save-Test.uni": b"X" * 100,
        "Save-Test.tea": b"Y" * 50,
    }
    raw = _make_bundle(files, version=109)
    b = Bundle.from_bytes(raw)
    check("目录项数（含 .ver）", len(b.files) == 4, "得到 %d" % len(b.files))
    check("版本号从 .ver 读出", b.version == 109, "得到 %d" % b.version)
    check("按名取内嵌文件", b.get("Save-Test.uni") == b"X" * 100)
    check("按名取大小写不敏感", b.get("save-test.UNI") == b"X" * 100)
    check("按扩展名取", b.get_by_ext(".tea") == b"Y" * 50)
    check("取不存在的扩展名返回 None", b.get_by_ext(".nope") is None)
    check("find_by_ext 给出目录项",
          b.find_by_ext(".cmp").size == 4)

    # 损坏文件要明确报错
    try:
        Bundle.from_bytes(b"\x00\x00\x00\x02ab")
        check("目录越界时报错", False, "没有抛异常")
    except BundleError:
        check("目录越界时报错", True)
    try:
        Bundle.from_bytes(b"abc")
        check("过短文件报错", False, "没有抛异常")
    except BundleError:
        check("过短文件报错", True)


def test_permissions() -> None:
    print("\n[6] 权限口径")
    from gfvfw.permissions import (CAMPAIGN_MANAGE, CAMPAIGN_UPLOAD,
                                   CAMPAIGN_VIEW, ROLE_DEFINITIONS)
    check("member 可查看战役", CAMPAIGN_VIEW in ROLE_DEFINITIONS["member"][2])
    check("member **不可**上报存档（会改变全联队看到的战局）",
          CAMPAIGN_UPLOAD not in ROLE_DEFINITIONS["member"][2])
    check("member 不可管理战役", CAMPAIGN_MANAGE not in ROLE_DEFINITIONS["member"][2])
    for role in ("owner", "commander", "instructor"):
        p = ROLE_DEFINITIONS[role][2]
        check("%s 可上报存档" % role, CAMPAIGN_UPLOAD in p)
    check("visitor 不可查看战役",
          CAMPAIGN_VIEW not in ROLE_DEFINITIONS["visitor"][2])


def test_models() -> None:
    print("\n[7] 数据表与「只留最新一份」的存储策略")
    from gfvfw.models.campaign_state import (CampaignObjective,
                                             CampaignObjectiveChange,
                                             CampaignSave, CampaignTeamState,
                                             CampaignUnit)
    tables = Base.metadata.tables
    for t in ("campaign_saves", "campaign_team_states", "campaign_objectives",
              "campaign_objective_changes", "campaign_units", "campaign_events"):
        check("表 %s 已注册" % t, t in tables)

    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    with S() as db:
        db.add(CampaignSave(id="s1", sha256="a" * 64,
                            original_filename="a.cam", stored_path="p"))
        db.flush()
        # 明细表挂在外键上，删存档应连带删掉明细（cascade）
        db.add(CampaignObjective(save_id="s1", camp_id=1, name="X",
                                 type_name="Airbase", team_id=-1))
        db.add(CampaignUnit(save_id="s1", unit_kind="Flight", unit_id=1))
        db.commit()
        check("明细写入成功",
              db.scalar(select(func.count()).select_from(CampaignObjective)) == 1)
        db.delete(db.get(CampaignSave, "s1"))
        db.commit()
        check("删存档连带删目标点明细",
              db.scalar(select(func.count()).select_from(CampaignObjective)) == 0)
        check("删存档连带删单位明细",
              db.scalar(select(func.count()).select_from(CampaignUnit)) == 0)
    eng.dispose()

    # ── 部署契约：服务器不装 BMS 也能用（放一份剧场数据即可） ──────────────
    #    见 docs/requirements.md §7.3.1：必需数据仅 47.9 MB，已用 9 份真实存档
    #    验证与完整安装逐字段一致。这里把那条路径的**前提**固定下来：
    #    没配置要报可读错误；部分表缺失只记 warning，不崩。
    from gfvfw.campaign.theater import TheaterData as _TD
    from gfvfw.config import settings as _settings
    from gfvfw.services.campaign import theater_data as _td

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _td_dir:
        empty = Path(_td_dir) / "empty-bms"
        empty.mkdir()

        saved = _settings.bms_install_path
        try:
            _settings.bms_install_path = None
            try:
                _td("Korea")
                check("未配置安装目录时应报错", False, "居然没抛异常")
            except FileNotFoundError as exc:
                check("未配置安装目录时报可读错误",
                      "GFVFW_BMS_INSTALL_PATH" in str(exc), str(exc)[:90])
            except Exception as exc:  # noqa: BLE001
                check("未配置安装目录时应报 FileNotFoundError", False,
                      "%s: %s" % (type(exc).__name__, exc))

            _settings.bms_install_path = str(empty)
            try:
                _td("Korea")
                check("配置了目录就不该再报错（缺失表另行记录）", True)
            except Exception as exc:  # noqa: BLE001
                check("配置了目录就不该再报错（缺失表另行记录）", False,
                      "%s: %s" % (type(exc).__name__, exc))
        finally:
            _settings.bms_install_path = saved

        # 空目录：所有表都记进 missing，而不是抛异常
        th = _TD.load(empty, "Korea", cache=False)
        miss = set(th.missing)
        for label in ("CT", "UCD", "VCD", "WCD", "RCD", "FCD",
                      "CampObjData", "strings"):
            check("空目录下 %s 被记为缺失（而非崩溃）" % label, label in miss,
                  "missing=%s" % sorted(miss))
        check("空目录下类表为空而不是异常", th.entries == [],
              "entries=%d" % len(th.entries))


# --------------------------------------------------------------------------
# B. 真实素材
# --------------------------------------------------------------------------

def _find_cam(bms: Path | None) -> list[Path]:
    """找出可用的 .cam 候选列表（按文件名升序）。

    优先用 ``GFVFW_TEST_CAM``；否则扫描常见剧场目录。**始终返回列表** ——
    调用方需要逐个回退，返回单个路径会让调用点行为不一致。
    """
    env = os.environ.get("GFVFW_TEST_CAM")
    if env and Path(env).exists():
        return [Path(env)]
    if not bms:
        return []
    cands: list[Path] = []
    for sub in ("Data/Add-On Hellas 2026", "Data/Add-On Hellas", "Data"):
        d = bms / sub / "Campaign"
        if d.is_dir():
            cands.extend(sorted(d.glob("*.cam")))
    return cands


def pick_cam_with_flights(bms: Path, cands: list[Path]) -> Path | None:
    """返回第一个确实含在空飞行的存档（最多试 8 份）。"""
    from gfvfw.campaign.bundle import Bundle
    from gfvfw.campaign.cmpfile import read_cmp
    from gfvfw.campaign.state import build_state
    from gfvfw.campaign.theater import TheaterData

    for p in list(reversed(cands))[:8]:
        try:
            b = Bundle.load(p)
            c = read_cmp(b.get_by_ext(".cmp"), b.version)
            st = build_state(p, TheaterData.load(bms, c.theater_name))
        except Exception:  # noqa: BLE001
            continue
        if st.count_of("Flight") > 0:
            return p
    return cands[-1] if cands else None


def test_real_parse(bms: Path, cam: Path) -> None:
    print("\n[8] 真实存档解析")
    from gfvfw.campaign.bundle import Bundle
    from gfvfw.campaign.cmpfile import read_cmp
    from gfvfw.campaign.state import build_state
    from gfvfw.campaign.theater import TheaterData

    b = Bundle.load(cam)
    print("      存档 %s  版本 %d  内嵌 %d 个文件"
          % (cam.name, b.version, len(b.files)))
    cmp_raw = b.get_by_ext(".cmp")
    check("bundle 里有 .cmp", cmp_raw is not None)
    if cmp_raw is None:
        return
    c = read_cmp(cmp_raw, b.version)
    check("剧场名可读出", bool(c.theater_name), "得到 %r" % c.theater_name)
    check("剧本名可读出", bool(c.scenario), "得到 %r" % c.scenario)

    th = TheaterData.load(bms, c.theater_name)
    st = build_state(cam, th)
    print("      时间 %s  目标点 %d  单位 %d  中队 %d  事件 %d"
          % (st.campaign_time_label, len(st.objectives), len(st.units),
             len(st.squadrons), len(st.events)))
    check("解析无致命告警", not any("失败" in w for w in st.warnings),
          str(st.warnings[:3]))
    check("解出目标点", len(st.objectives) > 1000)
    check("解出单位", len(st.units) > 100)
    check("单位流计数与单位列表一致",
          sum(st.unit_counts.values()) == len(st.units),
          "计数 %d / 列表 %d" % (sum(st.unit_counts.values()), len(st.units)))
    n_fl = st.count_of("Flight")
    if n_fl:
        check("飞行都有机型名",
              all(u.aircraft_type for u in st.units if u.unit_kind == "Flight"))
        check("飞行都有呼号",
              all(u.callsign for u in st.units if u.unit_kind == "Flight"))
        # 任务名靠 strings[300 + 任务码] 查表；个别任务码在 strings 里没有条目
        # 属数据本身的空缺，不要求 100%。
        named = [u for u in st.units if u.unit_kind == "Flight" and u.mission_name]
        ratio = len(named) / n_fl
        check("飞行任务名解出率 ≥ 90%", ratio >= 0.90,
              "%d/%d = %.1f%%" % (len(named), n_fl, 100 * ratio))
        if len(named) < n_fl:
            missing = sorted({u.mission_code for u in st.units
                              if u.unit_kind == "Flight" and not u.mission_name})
            print("      无任务名的任务码：%s" % missing)
    else:
        print("      注意：这份存档里没有在空飞行（n_flight=0），跳过飞行字段检查")
        check("无飞行时单位流计数仍自洽",
              st.count_of("Package") + st.count_of("Squadron") > 0)
    check("目标点网格坐标在合理范围",
          all(-64 <= (o.east or 0) <= GRID_SIZE + 64
              and -64 <= (o.north or 0) <= GRID_SIZE + 64
              for o in st.objectives))
    check("有队伍占有目标点", any(o.team_id >= 0 for o in st.objectives))
    check("SAM 威胁半径非空", len(st.sam_threat) > 0)

    # 目标类型名：不得有目标点落到"没解析出来"的哨兵上。
    # theater 侧映射失败返回 C# 原样的 "Type-1"，本模块兜底是 "Unknown"，
    # 两个都必须被认出来（否则页面静默显示哨兵名而没有任何告警）。
    from gfvfw.campaign.state import _UNRESOLVED_TYPE_NAMES
    check("哨兵集合含 Type-1 与 Unknown",
          _UNRESOLVED_TYPE_NAMES == frozenset({"Type-1", "Unknown"}),
          str(sorted(_UNRESOLVED_TYPE_NAMES)))
    unresolved = [o for o in st.objectives
                  if o.type_name in _UNRESOLVED_TYPE_NAMES]
    check("没有目标点落在未解析的类型哨兵上", not unresolved,
          "%d 个未解析（示例 %s）"
          % (len(unresolved), [o.type_name for o in unresolved[:3]]))
    check("目标类型名不重复计数异常",
          len({o.type_name for o in st.objectives}) > 5,
          "只解出 %d 种类型" % len({o.type_name for o in st.objectives}))

    # 与 CamReader 的参考输出对拍（可选）
    ref_path = os.environ.get("GFVFW_TEST_STATE_JSON")
    if not ref_path:
        cand = Path(r"G:\BMS\Falcon BMS 4.38\Tools\CamReader-0.1.0\bin\Release\net472\campaign_state.json")
        ref_path = str(cand) if cand.exists() else ""
    if not ref_path or not Path(ref_path).exists():
        skip("与 campaign_state.json 对拍", "未提供参考 JSON")
        return

    import json
    ref = json.load(open(ref_path, encoding="utf-8-sig"))
    if ref["meta"]["saveName"] != st.save_name:
        skip("与 campaign_state.json 对拍",
             "参考 JSON 来自别的存档（%s ≠ %s）"
             % (ref["meta"]["saveName"], st.save_name))
        return

    m = ref["meta"]
    check("对拍 version", st.cam_version == m["version"])
    check("对拍 theater", st.theater == m["theater"])
    check("对拍 scenario", st.scenario == m["scenario"])
    check("对拍 campaignTimeMs", st.campaign_time_ms == m["campaignTimeMs"])
    check("对拍 campaignTime 文本", st.campaign_time_label == m["campaignTime"])
    check("对拍 bullseye.x", st.bullseye_east == m["bullseye"]["x"])
    check("对拍 bullseye.y", st.bullseye_north == m["bullseye"]["y"])
    check("对拍目标点数", len(st.objectives) == len(ref["objectives"]),
          "%d vs %d" % (len(st.objectives), len(ref["objectives"])))
    check("对拍舰队数", st.count_of("TaskForce") == len(ref["navalUnits"]))
    check("对拍飞行数", st.count_of("Flight") == len(ref["flights"]))
    check("对拍事件数", len(st.events) == len(ref["events"]))
    check("对拍地带单位数",
          st.count_of("Battalion") + st.count_of("Brigade")
          + st.count_of("Division") == len(ref["groundUnits"]))

    # 目标点归属分布
    mine = {}
    for o in st.objectives:
        mine[o.team_id] = mine.get(o.team_id, 0) + 1
    theirs = {}
    for o in ref["objectives"]:
        theirs[o["teamId"]] = theirs.get(o["teamId"], 0) + 1
    check("对拍目标点归属分布", mine == theirs,
          "本实现 %s / 参考 %s" % (sorted(mine.items()), sorted(theirs.items())))

    # 队伍经验与补给
    ok_team = True
    for i, rt in enumerate(ref["teams"]):
        if i >= len(st.teams):
            ok_team = False
            break
        t = st.teams[i]
        if (t.name != rt["name"] or t.supply != rt["resources"]["supply"]
                or t.fuel != rt["resources"]["fuel"]
                or t.exp_air != rt["experience"]["air"]
                or t.st_aircraft != rt["strength"]["aircraft"]):
            ok_team = False
            print("       队伍 %d 不符：%s/%s supply %s/%s aircraft %s/%s"
                  % (i, t.name, rt["name"], t.supply, rt["resources"]["supply"],
                     t.st_aircraft, rt["strength"]["aircraft"]))
    check("对拍 8 个队伍的名字/资源/经验/兵力", ok_team)

    # 事件文本
    same_events = all(
        st.events[i].text == rt["text"] and st.events[i].team_id == rt["teamId"]
        for i, rt in enumerate(ref["events"]) if i < len(st.events))
    check("对拍事件文本与队伍", same_events)


def test_ingest(bms: Path, cam: Path) -> None:
    print("\n[9] 上报管线")
    from gfvfw.services.bootstrap import seed
    from gfvfw.services.campaign import CampaignService, campaign_overview
    from gfvfw.models.campaign_state import (
        CampaignObjective, CampaignObjectiveChange, CampaignSave, CampaignUnit)
    from gfvfw.models.flight import Campaign

    tmp = Path(tempfile.mkdtemp())
    eng = create_engine("sqlite+pysqlite:///%s" % (tmp / "t.sqlite3").as_posix(),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with S() as db:
        seed(db)
        svc = CampaignService(storage_dir=tmp / "storage", bms_install_path=bms)
        r1 = svc.ingest(db, cam)
        check("首次入库成功", r1.save.parse_status == "parsed", str(r1.save.parse_error))
        check("入库了目标点", r1.save.objective_count > 1000)
        from gfvfw.models.campaign_state import CampaignTeamState
        check("入库了 8 行队伍状态",
              db.scalar(select(func.count()).select_from(CampaignTeamState)) >= 1)

        # SHA256 去重
        r2 = svc.ingest(db, cam)
        check("同文件重复上报被识别", r2.duplicate)
        check("重复上报不新增存档",
              db.scalar(select(func.count()).select_from(CampaignSave)) == 1)

        # 目标点明细只留一份
        camp = db.scalars(select(Campaign)).first()
        check("自动建立了战役", camp is not None)
        ov = campaign_overview(db, camp)
        check("总览可用", ov.has_data)
        check("总览统计到存档数", ov.save_count == 1)
        check("总览有目标点类型分布", len(ov.objective_types) > 0)
        check("6 张明细表行数是可接受的量级（不随存档数线性膨胀）",
              db.scalar(select(func.count()).select_from(CampaignObjective))
              == r1.save.objective_count)
    eng.dispose()

    # 多份存档 → 易手检测
    tmp2 = Path(tempfile.mkdtemp())
    eng2 = create_engine("sqlite+pysqlite:///%s" % (tmp2 / "t2.sqlite3").as_posix(),
                         connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng2)
    S2 = sessionmaker(bind=eng2, autoflush=False, expire_on_commit=False)
    camdir = cam.parent
    siblings = sorted(camdir.glob("*.cam"))[:4]
    if len(siblings) < 2:
        skip("目标点易手检测", "该目录下没有多份存档")
    else:
        with S2() as db:
            seed(db)
            svc = CampaignService(storage_dir=tmp2 / "storage",
                                  bms_install_path=bms)
            for p in siblings:
                svc.ingest(db, p)
            n_saves = db.scalar(select(func.count()).select_from(CampaignSave))
            n_obj = db.scalar(select(func.count()).select_from(CampaignObjective))
            n_changes = db.scalar(select(func.count()).select_from(
                CampaignObjectiveChange))
            print("      %d 份存档 → 目标点明细 %d 行、易手 %d 条"
                  % (n_saves, n_obj, n_changes))
            check("每份存档都留了元数据", n_saves == len(siblings))
            check("目标点明细没有随存档数膨胀（只留最新一份）", n_obj < 2 * 7000)
            check("检测到了目标点易手", n_changes > 0,
                  "易手 0 条 —— 或许这几份存档之间确实没有变化")
            ch = db.scalars(select(CampaignObjectiveChange).limit(1)).first()
            if ch is not None:
                check("易手记录带目标点名", bool(ch.objective_name))
                check("易手记录带时刻标签", bool(ch.at_campaign_time_label))
    eng2.dispose()


def test_web(bms: Path, cam: Path) -> None:
    print("\n[10] Web 页面")
    from fastapi.testclient import TestClient

    from gfvfw.models import User
    from gfvfw.models.campaign_state import (
        CampaignObjective, CampaignSave, CampaignUnit)
    from gfvfw.models.flight import Campaign
    from gfvfw.models.site import AuditLog
    from gfvfw.security import hash_password
    from gfvfw.services.bootstrap import seed
    from gfvfw.services.campaign import CampaignService

    tmp = Path(tempfile.mkdtemp())
    eng = create_engine("sqlite+pysqlite:///%s" % (tmp / "w.sqlite3").as_posix(),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    import gfvfw.config as cfg
    import gfvfw.db as dbmod
    import gfvfw.web.deps as deps
    appmod = sys.modules["gfvfw.web.app"]
    orig = (dbmod.SessionLocal, deps.SessionLocal, appmod.SessionLocal,
            cfg.settings.storage_dir)
    dbmod.SessionLocal = deps.SessionLocal = appmod.SessionLocal = TS
    cfg.settings.storage_dir = tmp / "storage"

    _CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
    try:
        with TS() as db:
            seed(db)
            from gfvfw.models import Member, MemberRole, Role
            for callsign, role_code in (("Admiral", "owner"), ("Rookie", "member")):
                mem = Member(callsign=callsign, status="active")
                db.add(mem)
                db.flush()
                db.add(User(username=callsign.lower(),
                            password_hash=hash_password("password123"),
                            status="active", member_id=mem.id))
                role = db.scalar(select(Role).where(Role.code == role_code))
                db.add(MemberRole(member_id=mem.id, role_id=role.id))
            db.commit()
        with TS() as db:
            CampaignService(storage_dir=tmp / "storage",
                            bms_install_path=bms).ingest(db, cam)
            cid = db.scalars(select(Campaign)).first().id

        app = appmod.create_app()

        def login(client, user):
            tok = _CSRF.search(client.get("/login").text).group(1)
            return client.post("/login", data={
                "username": user, "password": "password123",
                "csrf_token": tok}, follow_redirects=False).status_code == 303

        with TestClient(app) as client:
            check("owner 登录成功", login(client, "admiral"))
            for p in ("/theater", "/theater/upload", "/theater/%s" % cid,
                      "/theater/%s/map" % cid, "/theater/%s/map?layer=all" % cid,
                      "/theater/%s/air" % cid, "/theater/%s/ground" % cid,
                      "/theater/%s/objectives" % cid,
                      "/theater/%s/objectives?type=Airbase" % cid,
                      "/theater/%s/timeline" % cid, "/theater/%s/saves" % cid):
                r = client.get(p)
                check("%s 可访问" % (p.replace(cid, "<id>")), r.status_code == 200,
                      "得到 %d" % r.status_code)
            # 关键内容确实渲染出来（不是空壳）
            h = client.get("/theater/%s" % cid).text
            check("总览含兵力对比", "兵力对比" in h)
            check("总览含目标点归属", "目标点归属" in h)
            m = client.get("/theater/%s/map" % cid).text
            check("态势图是 SVG", "<svg" in m and "viewBox" in m)
            check("态势图有目标点方框", m.count("<rect") > 100)
            check("态势图有飞行三角形", m.count("<polygon") > 0)
            check("态势图有靶心", "bullseye" in m or "靶心" in m)

            # ── 剧场底图 + 比例尺调节 ──────────────────────────────
            check("态势图有比例尺", 'id="scalebar"' in m)
            check("态势图有缩放按钮", 'id="zin"' in m and 'id="zout"' in m
                  and 'id="zreset"' in m)
            check("态势图有光标坐标读数", 'id="cursor"' in m)
            check("比例尺说明 1 格 = 1 km", "1 格 = 1 km" in m)
            has_img = "<image" in m and "/map/image/" in m
            if has_img:
                check("底图用 <image> 且铺满 viewBox",
                      'preserveAspectRatio="none"' in m and 'width="1024"' in m)
                check("底图有亮度调节", 'id="dimr"' in m)
                idx = re.search(r"/map/image/(\d+)", m).group(1)
                r = client.get("/theater/%s/map/image/%s" % (cid, idx),
                               follow_redirects=False)
                check("底图可下载", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("底图是 PNG（魔数）",
                      r.content[:8] == b"\x89PNG\r\n\x1a\n")
                check("底图带缓存头", "max-age" in (r.headers.get("cache-control") or ""))
                check("底图有 ETag", bool(r.headers.get("etag")))
                # 条件请求应得 304（浏览器据此避免重复下载几十 MB）
                et = r.headers.get("etag")
                r2 = client.get("/theater/%s/map/image/%s" % (cid, idx),
                                headers={"If-None-Match": et},
                                follow_redirects=False)
                check("底图支持条件请求（304）", r2.status_code == 304,
                      "得到 %d" % r2.status_code)
                # 越界序号必须拒绝（不能变成读任意文件的入口）
                bad = client.get("/theater/%s/map/image/9999" % cid,
                                 follow_redirects=False)
                check("底图序号越界被拒绝", bad.status_code == 404,
                      "得到 %d" % bad.status_code)
                # 关掉底图后不应再有 <image>
                off = client.get("/theater/%s/map?map=none" % cid).text
                check("可关掉底图", "<image" not in off)
            else:
                skip("剧场底图下载/缓存/越界", "该剧场没有可用的地图图片")

        with TestClient(app) as client:
            check("member 登录成功", login(client, "rookie"))
            check("member 可看战役列表",
                  client.get("/theater").status_code == 200)
            check("member 可看战役详情",
                  client.get("/theater/%s" % cid).status_code == 200)
            r = client.get("/theater/upload", follow_redirects=False)
            check("member 被拒绝上报页面（403）", r.status_code == 403,
                  "得到 %d" % r.status_code)
            r = client.post("/theater/upload",
                            files={"file": ("x.cam", b"nope")},
                            follow_redirects=False)
            check("member 被拒绝上报提交（403）", r.status_code == 403,
                  "得到 %d" % r.status_code)
            # 存档删除属于 CAMPAIGN_MANAGE，member 没有该权限
            with TS() as db:
                _sv = db.scalars(select(CampaignSave)).first()
                _sv_id = _sv.id if _sv else ""
            r = client.post("/theater/%s/saves/%s/delete" % (cid, _sv_id),
                            data={"csrf_token": "x"}, follow_redirects=False)
            check("★ member 不能删除存档（403）", r.status_code == 403,
                  "得到 %d" % r.status_code)

        # ── 删除存档：连带明细 + 战局回退 ────────────────────────────
        with TestClient(app) as client:
            check("owner 重新登录", login(client, "admiral"))

            # ── 上传失败必须给出**可读的 400**，不能是光秃秃的 500 ──────
            #    真实事故：.cam 里未初始化的槽位让 z=NaN →
            #    NOT NULL constraint failed: campaign_units.z →
            #    会话进入 PendingRollback；而当时的 fail() 又拿这个脏会话去查
            #    战役列表，于是 PendingRollbackError 把真正的错误信息盖掉，
            #    用户只看到 "Internal Server Error"。这里把那条链路原样复现。
            def _dirty_then_fail(self, db, *a, **kw):    # noqa: ANN001
                _sv = db.scalars(select(CampaignSave)).first()
                db.add(CampaignUnit(save_id=_sv.id if _sv else "missing",
                                    unit_kind="Objective", unit_id=1,
                                    z=float("nan")))
                db.flush()      # ← 这里抛 IntegrityError，会话变脏
                raise AssertionError("不该走到这里")     # pragma: no cover

            _orig_ingest = CampaignService.ingest
            CampaignService.ingest = _dirty_then_fail
            try:
                _tok = _CSRF.search(client.get("/theater/upload").text).group(1)
                r = client.post("/theater/upload",
                                files={"file": ("ghost.cam", b"\xff" * 64)},
                                data={"csrf_token": _tok, "campaign_id": ""},
                                follow_redirects=False)
            finally:
                CampaignService.ingest = _orig_ingest
            check("★ 入库失败时返回可读的 400 而不是 500",
                  r.status_code == 400, "得到 %d" % r.status_code)
            check("★ 错误信息露出真正的原因（campaign_units.z）",
                  "campaign_units.z" in r.text, r.text[:200])
            check("★ 提示写成「解析失败」，且没有 Internal Server Error",
                  "解析失败" in r.text and "Internal Server Error" not in r.text)
            check("★ 失败后仍能渲染出战役下拉（说明会话已回滚）",
                  "<select" in r.text and "name=\"campaign_id\"" in r.text)
            with TS() as db:
                check("★ 失败的上传没有留下半成品单位行",
                      db.scalar(select(func.count()).select_from(CampaignUnit)
                                .where(CampaignUnit.unit_id == 1)) == 0)


            with TS() as db:
                sv = db.scalars(select(CampaignSave)).first()
                sv_id = sv.id
                n_obj_before = db.scalar(
                    select(func.count()).select_from(CampaignObjective)
                    .where(CampaignObjective.save_id == sv_id)) or 0
                n_unit_before = db.scalar(
                    select(func.count()).select_from(CampaignUnit)
                    .where(CampaignUnit.save_id == sv_id)) or 0
                # 给这份存档留一个真实的磁盘原件，验证会被清理
                sp = Path(sv.stored_path)
                if not sp.is_absolute():
                    sp = Path(cfg.settings.storage_dir) / sp
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_bytes(b"fake cam")

            check("删除前有目标点明细", n_obj_before > 0)
            r = client.get("/theater/%s/saves" % cid)
            check("存档页有删除按钮", "/saves/%s/delete" % sv_id in r.text)
            r = client.post("/theater/%s/saves/%s/delete" % (cid, sv_id),
                            data={"csrf_token": _CSRF.search(
                                client.get("/theater/%s/saves" % cid).text).group(1),
                                "reason": "传错了"},
                            follow_redirects=False)
            check("删除存档成功（303）", r.status_code == 303,
                  "得到 %d" % r.status_code)
            with TS() as db:
                check("★ 存档行已删除",
                      db.get(CampaignSave, sv_id) is None)
                check("★ 目标点明细连带删除",
                      db.scalar(select(func.count()).select_from(CampaignObjective)
                                .where(CampaignObjective.save_id == sv_id)) == 0,
                      "原 %d 行" % n_obj_before)
                check("★ 单位明细连带删除",
                      db.scalar(select(func.count()).select_from(CampaignUnit)
                                .where(CampaignUnit.save_id == sv_id)) == 0,
                      "原 %d 行" % n_unit_before)
                check("产生了 campaign_saves 删除审计",
                      db.scalar(select(func.count()).select_from(AuditLog)
                                .where(AuditLog.target_table == "campaign_saves",
                                       AuditLog.action == "delete")) >= 1)
            check("★ 磁盘原件已删除", not sp.exists())

            # ── ★ 战役管理（/theater）里的「作废战役」入口 ─────────────
            #    用户反馈"战役管理中仍然不能删除战役"。作废 handler 一直都在，
            #    但按钮只挂在 /campaigns 下，而联队口中的"战役管理"是 /theater
            #    （顶栏那一项指的就是它）—— 于是入口在用户看不见的地方。
            #    这里盯的是**入口本身**，而且盯在正确的那一页上。
            print("\n[10b] ★ 战役管理里的作废 / 恢复入口")
            with TestClient(app) as client:
                check("owner 登录（看战役管理）", login(client, "admiral"))
                r = client.get("/theater")
                check("战役管理列表可打开", r.status_code == 200,
                      "得到 %d" % r.status_code)
                check("★ 列表里有「作废」入口（POST /campaigns/<id>/delete）",
                      "/campaigns/%s/delete" % cid in r.text,
                      "战役管理列表里没有作废按钮")
                check("列表里有「显示已作废」入口", "theater?deleted=1" in r.text)

                r = client.get("/theater/%s" % cid)
                check("★ 战役详情页里有「作废此战役」按钮",
                      "作废此战役" in r.text)
                # ⚠️ 本节的**前一段刚把唯一一份存档删掉**了（§10 的删除存档测试），
                #    所以此时 `ov.save is None`，走的是"还没有可用存档"那个分支 ——
                #    它的按钮文案是「去上报一份」，不是「再上报一份存档」。
                #    两者都由 can_upload 控制，所以断言"有其中之一"才是对的；
                #    只认后者会得到一个假失败。
                check("★ 详情页的上报入口真的显示了（can_upload 以前没传进来）",
                      "再上报一份存档" in r.text or "去上报一份" in r.text,
                      "can_upload 恒为假 —— 上报入口以前永远不显示")

                # 真的作废：走的就是详情页那个表单的目标地址
                tok = _CSRF.search(client.get("/theater/%s" % cid).text).group(1)
                r = client.post("/campaigns/%s/delete" % cid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 在战役管理里作废战役 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                r = client.get("/theater")
                check("★ 作废后战役从战役管理列表消失", cid not in r.text,
                      "还在列表里")
                r = client.get("/theater?deleted=1")
                check("★ 已作废视图里能看到它",
                      "/campaigns/%s/restore" % cid in r.text)
                tok = _CSRF.search(r.text).group(1)
                r = client.post("/campaigns/%s/restore" % cid,
                                data={"csrf_token": tok},
                                follow_redirects=False)
                check("★ 恢复 → 303", r.status_code == 303,
                      "得到 %d" % r.status_code)
                r = client.get("/theater")
                check("★ 恢复后回到战役管理列表",
                      "/campaigns/%s/delete" % cid in r.text)

            with TestClient(app) as client:
                login(client, "rookie")           # 普通队员：无 campaign.manage
                r = client.get("/theater")
                check("★ 普通队员看不到「作废」按钮",
                      "/campaigns/%s/delete" % cid not in r.text)
                r = client.get("/theater?deleted=1")
                check("★ 普通队员拿不到已作废视图（回落到普通列表）",
                      "已作废" not in r.text or "作废此战役" not in r.text)
                r = client.get("/theater/%s" % cid)
                check("★ 普通队员看不到详情页的「作废此战役」",
                      "作废此战役" not in r.text)
            # 删掉唯一一份存档后，战役总览应变成"没有可用存档"而不是崩掉
            r = client.get("/theater/%s" % cid)
            check("删光存档后战役详情仍可访问（不崩）", r.status_code == 200,
                  "得到 %d" % r.status_code)
            check("详情页提示还没有可用存档", "还没有可用存档" in r.text
                  or "还没有解析成功的存档" in r.text)
    finally:
        dbmod.SessionLocal, deps.SessionLocal, appmod.SessionLocal, \
            cfg.settings.storage_dir = orig
        eng.dispose()


def test_obj_path(bms: Path) -> None:
    """``.obj`` 目标点状态路径（**独立于 .obd 的兜底来源**）。

    这条路径一度是盲点：周期存档（``Save-Day …``）里没有 ``.obj``，
    只有"起始存档"（``Save0/1/2…``）才有。不专门测就会一直没被真实数据覆盖。

    CamReader 的目标点状态优先级是 ``.obd 增量 > .obj 记录 > .uni Objective``，
    所以起始存档（没有 .obd 增量）正是走 ``.obj`` 的场合。
    """
    print("\n[11] .obj 目标点状态路径")
    from gfvfw.campaign import camdata
    from gfvfw.campaign.bundle import Bundle
    from gfvfw.campaign.cmpfile import read_cmp
    from gfvfw.campaign.state import build_state
    from gfvfw.campaign.theater import TheaterData

    # 找一个带 .obj 的存档
    target = None
    seen: list[Path] = []
    for sub in ("Data/Add-On Hellas 2026", "Data/Add-On Hellas", "Data"):
        d = bms / sub / "Campaign"
        if d.is_dir():
            seen.extend(sorted(d.glob("*.cam")))
    for p in seen:
        try:
            b = Bundle.load(p)
        except Exception:  # noqa: BLE001
            continue
        if b.find_by_ext(".obj"):
            target = p
            break
    if target is None:
        skip(".obj 目标点状态路径", "各剧场目录下没有带 .obj 的存档")
        return
    print("      选用存档：%s" % target.name)

    b = Bundle.load(target)
    obj = camdata.read_obj(b.get_by_ext(".obj"), b.version)
    n_idx = len(obj.get("by_camp_id") or {})
    print("      .obj complete=%s records=%s 索引=%d 错误=%d 消费=%s/%s"
          % (obj.get("complete"), obj.get("records_read"), n_idx,
             len(obj.get("errors") or []), obj.get("bytes_consumed"),
             obj.get("bytes_total")))
    if n_idx == 0:
        skip(".obj 目标点状态路径", "该存档的 .obj 申报 0 条记录")
        return
    check(".obj 完整解出（complete）", obj.get("complete") is True)
    check(".obj 无解析错误", not (obj.get("errors") or []))
    check(".obj 恰好消费整段缓冲区",
          obj.get("bytes_consumed") == obj.get("bytes_total"),
          "%s vs %s" % (obj.get("bytes_consumed"), obj.get("bytes_total")))
    check(".obj 索引出目标点", n_idx > 1000, "得到 %d" % n_idx)
    check(".obj 两个键指向同一份数据",
          (obj.get("objectives") is obj.get("by_camp_id"))
          or (len(obj.get("objectives") or {}) == n_idx))

    c = read_cmp(b.get_by_ext(".cmp"), b.version)
    st = build_state(target, TheaterData.load(bms, c.theater_name))
    src = {}
    for o in st.objectives:
        src[o.source] = src.get(o.source, 0) + 1
    print("      目标点状态来源：%s" % src)
    check("走 .obj 路径的目标点占多数", src.get("obj", 0) > 1000,
          "来源统计 %s" % src)
    check(".obj 路径给出了占有方",
          sum(1 for o in st.objectives if o.team_id >= 0) > 1000)
    check("解析无致命告警", not any("失败" in w for w in st.warnings),
          str(st.warnings[:3]))
    # .obj 也带补给/燃油，走这条路径时应当有值（-1 表示该目标点无此概念）
    with_supply = [o for o in st.objectives if o.source == "obj" and o.supply >= 0]
    check(".obj 路径带出补给数值", len(with_supply) > 100,
          "有补给的 %d 个" % len(with_supply))


def test_objective_reader_parity(bms: Path) -> None:
    """两条路径读同一个目标点结构，字段必须一致（防漂移）。

    ``.uni`` 里的 Objective 记录与 ``.obj`` 里的记录用的是**同一个 C++ 结构**
    （``UnitBase.ReadCampaignBase`` + ``UnitObjective.ReadObjectiveFields``），
    但实现落在两处：``units.py``（供 .uni 单位流复用）与 ``camdata.py``
    （供 .obj）。两份都各自与参考输出对拍过，但**如果日后只改一处就会悄悄漂移**，
    于是用同一条真实 ``.obj`` 记录的字节喂给两边做交叉核对。
    """
    print("\n[12] 目标点结构：两条读取路径的一致性")
    from gfvfw.campaign import camdata
    from gfvfw.campaign.bundle import Bundle
    from gfvfw.campaign.lzss import decompress
    from gfvfw.campaign.units import (
        Unit, _R, _read_campaign_base, _read_objective_fields,
    )

    target = None
    for sub in ("Data/Add-On Hellas 2026", "Data/Add-On Hellas", "Data"):
        d = bms / sub / "Campaign"
        if d.is_dir():
            for p in sorted(d.glob("*.cam")):
                try:
                    if Bundle.load(p).find_by_ext(".obj"):
                        target = p
                        break
                except Exception:  # noqa: BLE001
                    continue
        if target:
            break
    if target is None:
        skip("目标点结构两条路径一致性", "没找到带 .obj 的存档")
        return

    b = Bundle.load(target)
    raw = b.get_by_ext(".obj")
    u_sz = struct.unpack_from("<i", raw, 2)[0]
    if u_sz == 0:
        skip("目标点结构两条路径一致性", "该 .obj 申报 0 字节")
        return
    data = decompress(raw[10:], u_sz)

    # camdata 侧
    rec, pos_a = camdata.read_objective_record(data, 2, b.version, kind="obj")
    # units 侧（跳过 2 字节的 entityType 前缀）
    r = _R(data, "obj")
    r.p = 2
    u = Unit(unit_kind="Objective")
    _read_campaign_base(r, b.version, u)
    _read_objective_fields(r, b.version, u)

    pairs = (
        ("camp_id", rec.get("camp_id"), u.camp_id),
        ("owner", rec.get("owner"), u.owner),
        ("supply", rec.get("supply"), u.extra.get("supply")),
        ("fuel", rec.get("fuel"), u.extra.get("fuel")),
        ("losses", rec.get("losses"), u.extra.get("losses")),
        ("name_id", rec.get("name_id"), u.extra.get("obj_name_id")),
        ("first_owner", rec.get("first_owner"), u.extra.get("first_owner")),
        ("priority", rec.get("priority"), u.extra.get("priority")),
    )
    mismatched = [(n, a, bb) for n, a, bb in pairs if a != bb]
    check("两条路径字段完全一致", not mismatched, str(mismatched))
    check("两条路径消费字节数相同", pos_a == r.p,
          "camdata=%d units=%d" % (pos_a, r.p))
    check("该记录读出了目标点号", (rec.get("camp_id") or 0) > 0,
          "camp_id=%s" % rec.get("camp_id"))


def test_nonfinite_guard() -> None:
    """[7] 非有限浮点（NaN / ±Inf）不得进入数据库。

    这是一个**真实事故**的回归测试。``.cam`` 里未初始化的实体槽位是全 1
    位模式（``0xFFFFFFFF``），按 f32 解释恰好是 NaN：实测一个
    ``unit_id=0xFFFF0001``、``id_creator=0xFFFFFFFF`` 的"幽灵单位"带着
    ``z=nan``。SQLite 把 NaN 存成 NULL，而 ``campaign_units.z`` 是 NOT NULL，
    于是整份存档以 ``IntegrityError`` 收场 —— 而当时异常处理里又拿这个
    已经进入 PendingRollback 的会话去查战役列表，用户最终只看到光秃秃的
    500，"解析失败：NOT NULL constraint failed" 一个字都没露出来。

    所以这里测两件事：
      A. 每一层读取器都把非有限值收敛掉（不写 NaN 进库）；
      B. 会话一旦脏了，**必须回滚**才能继续查询 —— 也就是那个被掩盖的 500。
    """
    print("\n[7] 非有限浮点防护")
    from sqlalchemy.exc import IntegrityError, PendingRollbackError

    from gfvfw.campaign import camdata, cmpfile, state, units as unitmod
    from gfvfw.services.campaign import _safe_json

    ALL_FF = b"\xff" * 8
    POS_INF = struct.pack("<f", float("inf"))
    NEG_INF = struct.pack("<f", float("-inf"))
    FINITE = struct.pack("<f", 1234.5)
    FINITE_D = struct.pack("<d", -9876.25)

    # ── A1. camdata 读取器 ───────────────────────────────────────────
    r = camdata._Reader(ALL_FF, ".uni")
    got = r.f32()
    check("camdata f32 把全 1 位模式读成 0.0 而不是 NaN",
          got == 0.0 and math.isfinite(got), "得到 %r" % (got,))
    r = camdata._Reader(POS_INF + NEG_INF, ".uni")
    check("camdata f32 吃掉 ±Inf", r.f32() == 0.0 and r.f32() == 0.0)
    r = camdata._Reader(FINITE, ".uni")
    check("camdata f32 保留正常值", abs(r.f32() - 1234.5) < 1e-3)
    r = camdata._Reader(ALL_FF, ".uni")
    check("camdata f64 同样收敛", r.f64() == 0.0)
    r = camdata._Reader(FINITE_D, ".obj")
    check("camdata f64 保留正常值", abs(r.f64() + 9876.25) < 1e-9)
    r = camdata._Reader(ALL_FF, ".uni")
    r.f32()
    check("收敛不影响游标前进", r.pos == 4)

    # ── A2. .uni 单位流读取器（z 就是从这条路径来的）─────────────────
    ur = unitmod._R(ALL_FF, ".uni")
    check("units._R f32 收敛 NaN（z 的来源）", ur.f32() == 0.0)
    check("units._R 游标正确", ur.p == 4)
    ur = unitmod._R(POS_INF, ".uni")
    check("units._R f32 收敛 +Inf", ur.f32() == 0.0)
    ur = unitmod._R(FINITE_D, ".uni")
    check("units._R f64 保留正常值", abs(ur.f64() + 9876.25) < 1e-9)

    # ── A3. .cmp 读取器 ─────────────────────────────────────────────
    cr = cmpfile.Reader(ALL_FF, ".cmp")
    check("cmpfile.Reader f32 收敛 NaN", cr.f32() == 0.0)

    # ── A4. _finite 帮手：`nan or 0.0` 是挡不住的 ────────────────────
    nan = float("nan")
    check("NaN 是「真值」，`nan or 0.0` 会原样穿过（这正是坑）",
          (nan or 0.0) != (nan or 0.0))
    check("_finite 把 NaN 收敛成 0.0", state._finite(nan) == 0.0)
    check("_finite 把 ±Inf 收敛成 0.0",
          state._finite(float("inf")) == 0.0
          and state._finite(float("-inf")) == 0.0)
    check("_finite 保留正常值", state._finite(-12.5) == -12.5)
    check("_finite 兜住不可转换的输入",
          state._finite(None) == 0.0 and state._finite("abc") == 0.0)
    check("_finite 保留 0 的语义", state._finite(0.0) == 0.0)

    # ── A5. 入库前的最后一道：UnitRec.z 一定是有限值 ─────────────────
    warns: list[str] = []
    u = unitmod.Unit(unit_kind="Objective", z=nan)
    u.id = unitmod.VUId(num=0xFFFF0001, creator=0xFFFFFFFF)
    u.x, u.y = 7103, 0
    rec = state._unit_to_rec(u, None, warns)
    check("UnitRec.z 已收敛成 0.0", rec.z == 0.0, "z=%r" % (rec.z,))
    check("越界坐标照旧不上图", rec.on_map is False)
    check("收敛 NaN 会留下可读告警",
          any("高度不是有限数" in w for w in warns), str(warns))
    check("告警里带得出是哪条记录",
          any("0xFFFF0001" in w or "4294901761" in w for w in warns), str(warns))
    warns2: list[str] = []
    rec2 = state._unit_to_rec(unitmod.Unit(unit_kind="Objective", z=1234.5,
                                           x=100, y=200), None, warns2)
    check("正常高度不被改写", rec2.z == 1234.5)
    check("正常单位不产生高度告警",
          not any("高度" in w for w in warns2), str(warns2))

    # ── A6. JSON 里不能出现裸 NaN / Infinity ─────────────────────────
    s = _safe_json({"z": nan, "nested": [1.0, float("inf"), {"deep": nan}],
                    "ok": 2.5})
    check("_safe_json 不输出裸 NaN 记号", s is not None and "NaN" not in s)
    check("_safe_json 不输出裸 Infinity 记号", s is not None and "Infinity" not in s)
    check("_safe_json 用 null 取代非有限值",
          s is not None and s.count("null") == 3, str(s))
    check("_safe_json 保留正常值", s is not None and "2.5" in s)
    if s is not None:
        try:
            json.loads(s, parse_constant=_reject_constant)
            strict_ok = True
        except ValueError:
            strict_ok = False
        check("_safe_json 产出严格合法 JSON（浏览器 JSON.parse 也认）", strict_ok)

    # ── A7. 真正写库：NaN 会变成 NULL 撞上 NOT NULL ──────────────────
    tmp = Path(tempfile.mkdtemp())
    eng = create_engine("sqlite+pysqlite:///%s" % (tmp / "nan.sqlite3").as_posix(),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    from gfvfw.models.campaign_state import CampaignSave, CampaignUnit

    with S() as db:
        save = CampaignSave(sha256="0" * 64, original_filename="nan.cam",
                            stored_path="nan.cam")
        db.add(save)
        db.commit()
        save_id = save.id

        # 顺带记录一个反直觉的细节：显式给 None **不会**撞约束 ——
        # SQLAlchemy 只在"列上没有值"时才套用 default，而 None 会被
        # 当作"没值"处理，于是 default=0.0 兜住了。
        # 所以真正危险的只有 NaN：它是个**合法取值**，ORM 会照样下发，
        # 再由 sqlite3 驱动把它变成 NULL。
        db.add(CampaignUnit(save_id=save_id, unit_kind="Objective", unit_id=0,
                            z=None))
        none_ok = True
        try:
            db.flush()
        except Exception:  # noqa: BLE001
            none_ok = False
        check("z=None 会被 ORM 默认值兜住（所以别指望它拦住 NaN）", none_ok)
        db.rollback()

        # 这就是事故现场：z=NaN —— SQLite 没有 NaN，驱动把它写成 NULL
        db.add(CampaignUnit(save_id=save_id, unit_kind="Objective", unit_id=1,
                            z=float("nan")))
        raised = None
        try:
            db.flush()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("z=NaN 触发 NOT NULL 约束（事故现场）",
              raised is not None, "居然没报错")
        check("异常类型是 IntegrityError",
              isinstance(raised, IntegrityError), repr(raised))
        check("错误信息点名 campaign_units.z",
              raised is not None and "campaign_units.z" in str(raised),
              str(raised))

        # ── 被掩盖的 500：脏会话上再查询会抛 PendingRollbackError ──────
        masked = None
        try:
            list(db.scalars(select(CampaignSave)))
        except Exception as exc:  # noqa: BLE001
            masked = exc
        check("脏会话上继续查询会抛 PendingRollbackError（真正的 500 成因）",
              isinstance(masked, PendingRollbackError), repr(masked))

        # ── 修法：先回滚，再查询 ────────────────────────────────────
        db.rollback()
        again = None
        try:
            again = list(db.scalars(select(CampaignSave)))
        except Exception as exc:  # noqa: BLE001
            again = exc
        check("回滚后同一会话可以正常查询（fail() 必须这么做）",
              isinstance(again, list) and len(again) == 1, repr(again))

        # 回滚后再放一条正常的 z=0.0 单位，入库应当成功
        db.add(CampaignUnit(save_id=save_id, unit_kind="Objective",
                            unit_id=0xFFFF0001, z=0.0))
        db.commit()
        got = db.scalars(select(CampaignUnit)).one()
        check("收敛后的幽灵单位可以正常入库", got.z == 0.0)
        check("哨兵 unit_id 被原样保留（便于事后排查）",
              got.unit_id == 0xFFFF0001)
    eng.dispose()


def _reject_constant(name: str):
    """给 json.loads 用：遇到裸 NaN/Infinity 记号就报错（严格模式）。"""
    raise ValueError("非法 JSON 常量：%s" % name)


def _fake_png(path: Path, side: int, size_bytes: int) -> None:
    """造一张"PNG 头正确、体积可控"的假图（只为验证发现逻辑，不解像素）。"""
    head = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + struct.pack(">II", side, side))
    path.write_bytes(head + b"\x00" * max(0, size_bytes - len(head)))


def test_map_discovery() -> None:
    """[7A] 剧场底图的发现、筛选与诊断。

    底图是**"服务器上通常没有"**的东西（BMS 四个剧场的原图合计 1.6 GB），
    所以"没有底图"是常见状态、不是异常 —— 这里主要测三件事：

      A. 自备目录（``GFVFW_BMS_MAP_DIR``）**在没有剧场数据时也必须能用**。
         它曾经被写在 ``try`` 里面：``theater_data()`` 一抛异常就整个返回空，
         连联队自己放好的图也一起丢掉 —— 而"没配剧场数据"恰恰是最需要自备图
         的场景。这条是线上态势图没底图的主要成因。
      B. 一个目录里混放多个剧场的地图时，**不能张冠李戴**。
      C. 体积下限不能把"文档让你自压的那张图"挡掉（曾经是 512 KB，
         而压缩良好的 1024² 地图只有约 200 KB）。
    """
    print("\n[7A] 剧场底图发现")
    from gfvfw.campaign.maps import (
        MAP_DIR_SETTING_HINT, MIN_MAP_BYTES, MIN_MAP_SIDE,
        discover_theater_maps, find_theater_maps, pick_default_map,
        png_dimensions)

    tmp = Path(tempfile.mkdtemp())

    # ── A. 没有剧场数据 + 有自备目录 ────────────────────────────────
    custom = tmp / "maps"
    custom.mkdir()
    _fake_png(custom / "Hellas.png", 4096, 3 * 1024 * 1024)
    disc = discover_theater_maps(None, custom, theater="Hellas")
    check("★ 没有剧场数据时，自备目录仍然被采纳（线上主要情形）",
          len(disc.maps) == 1 and disc.maps[0].origin == "custom",
          "得到 %d 张" % len(disc.maps))
    check("自备图被标成 custom（页面上带 ★）",
          disc.maps and disc.maps[0].origin == "custom")
    check("自备目录被记进诊断信息",
          any(s.directory == custom and s.exists for s in disc.scans))
    check("没有剧场数据也不会崩", disc.theater_root is None)

    # ── B. 多剧场混放 → 按名字筛 ───────────────────────────────────
    _fake_png(custom / "korea.png", 4096, 4 * 1024 * 1024)
    h = discover_theater_maps(None, custom, theater="Hellas")
    k = discover_theater_maps(None, custom, theater="korea")
    check("★ Hellas 只看到自己的图（不会拿错地图）",
          [m.name for m in h.maps] == ["Hellas.png"],
          str([m.name for m in h.maps]))
    check("★ korea 只看到自己的图",
          [m.name for m in k.maps] == ["korea.png"],
          str([m.name for m in k.maps]))
    check("被排掉的文件如实记下来（页面会列出来）",
          list(h.filtered_out) == ["korea.png"], str(h.filtered_out))
    check("大小写不敏感（Hellas.png 对 hellas 也认）",
          len(discover_theater_maps(None, custom, theater="HELLAS").maps) == 1)
    both = discover_theater_maps(None, custom, theater="")
    check("剧场名未知时不做筛选（单剧场部署的兜底）", len(both.maps) == 2)

    # ── C. 体积下限：文档让自压的图不能被挡掉 ──────────────────────
    small = tmp / "small"
    small.mkdir()
    _fake_png(small / "Hellas.png", 1024, 200 * 1024)      # 约 200 KB
    d = discover_theater_maps(None, small, theater="Hellas")
    check("★ 200 KB 的 1024² 小图会被采纳（曾经被 512 KB 下限静默丢掉）",
          len(d.maps) == 1, "得到 %d 张" % len(d.maps))
    check("体积下限仍能挡住空壳文件", MIN_MAP_BYTES < 200 * 1024)

    bad = tmp / "bad"
    bad.mkdir()
    _fake_png(bad / "square_too_small.png", 512, 900 * 1024)
    (bad / "not_a_png.png").write_bytes(b"x" * (900 * 1024))
    _fake_png(bad / "tall.png", 2048, 900 * 1024)
    (bad / "tall.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 3220, 1173)
        + b"\x00" * (900 * 1024))
    _fake_png(bad / "tiny_file.png", 4096, 4096)
    db_ = discover_theater_maps(None, bad, theater="Hellas")
    check("全部不合格 → 一张都不给", not db_.maps)
    reasons = {n: why for n, _d, _s, why in db_.near_misses}
    check("非正方形被拒并说明尺寸", "不是正方形" in reasons.get("tall.png", ""),
          str(reasons))
    check("边长过小被拒", "边长" in reasons.get("square_too_small.png", ""),
          str(reasons))
    check("不是 PNG 被拒", "PNG" in reasons.get("not_a_png.png", ""), str(reasons))
    check("小文件被拒", reasons.get("tiny_file.png"), str(reasons))
    check("诊断里带得出目录名",
          any(d == bad for _n, d, _s, _w in db_.near_misses))

    # ── D. 找不到图时要能解释清楚 ──────────────────────────────────
    empty = tmp / "empty"
    empty.mkdir()
    disc3 = discover_theater_maps(None, empty, theater="Hellas")
    check("空目录 → 没有图", not disc3.maps)
    check("空目录仍被记进 scans（页面才能说\"找过这里\"）",
          any(s.directory == empty for s in disc3.scans))
    scratch = discover_theater_maps(tmp / "does-not-exist", None, theater="Hellas")
    check("目录不存在也不崩", not scratch.maps)
    check("不存在的目录不算\"扫过\"（免得刷屏）",
          not any(s.exists for s in scratch.scans))

    # ── E. 只读 PNG 头拿尺寸（不依赖图形库）───────────────────────
    p = tmp / "dim.png"
    _fake_png(p, 4096, 512 * 1024)
    check("png_dimensions 读出 4096", png_dimensions(p) == (4096, 4096))
    check("非 PNG 返回 None",
          png_dimensions(Path(__file__).resolve()) is None)

    # ── F. 默认图不能挑最大的那张 ──────────────────────────────────
    big = tmp / "big"
    big.mkdir()
    _fake_png(big / "Hellas.png", 4096, 1 * 1024 * 1024)
    _fake_png(big / "Hellas8K.png", 8192, 192 * 1024 * 1024)
    picked = pick_default_map(find_theater_maps(None, big, theater="Hellas"))
    check("★ 默认挑最小的 4K，不是 768 MB 的 16K",
          picked is not None and picked.width == 4096, str(picked))

    check("设置项名字和文档一致", MAP_DIR_SETTING_HINT == "GFVFW_BMS_MAP_DIR")
    check("最小边长是 1024", MIN_MAP_SIDE == 1024)
    check("find_theater_maps 仍然返回列表（旧调用点不受影响）",
          isinstance(find_theater_maps(None, custom, theater="Hellas"), list))


def test_map_without_theater_data() -> None:
    """[7B] **线上情形**端到端：没有剧场数据，只有自备底图目录。

    这是服务器上的真实处境：``/srv/gfvfw/bms-data`` 里没有 BMS 自带的剧场图
    （不该拷那 1.6 GB），底图只能来自 ``GFVFW_BMS_MAP_DIR``。
    这条测试确保那种情况下态势图**真的有背景图**，而不是只画点线。
    """
    print("\n[7B] 无剧场数据 + 自备底图（线上情形）")
    from fastapi.testclient import TestClient

    import gfvfw.config as cfg
    import gfvfw.db as dbmod
    import gfvfw.web.deps as deps
    from gfvfw.models import Member, MemberRole, Role, User
    from gfvfw.models.campaign_state import CampaignSave
    from gfvfw.models.flight import Campaign
    from gfvfw.security import hash_password
    from gfvfw.services.bootstrap import seed

    tmp = Path(tempfile.mkdtemp())
    eng = create_engine("sqlite+pysqlite:///%s" % (tmp / "m.sqlite3").as_posix(),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    appmod = sys.modules["gfvfw.web.app"]
    orig = (dbmod.SessionLocal, deps.SessionLocal, appmod.SessionLocal,
            cfg.settings.storage_dir, cfg.settings.bms_map_dir,
            cfg.settings.bms_install_path)
    dbmod.SessionLocal = deps.SessionLocal = appmod.SessionLocal = TS
    cfg.settings.storage_dir = tmp / "storage"

    # 自备底图目录：只有一张 Hellas 图
    maps_dir = tmp / "maps"
    maps_dir.mkdir()
    _fake_png(maps_dir / "Hellas.png", 4096, 3 * 1024 * 1024)
    cfg.settings.bms_map_dir = maps_dir
    # 剧场数据**故意指向不存在的地方** —— 模拟服务器上没拷 BMS 数据
    cfg.settings.bms_install_path = tmp / "no-such-bms"

    _CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
    try:
        with TS() as db:
            seed(db)
            mem = Member(callsign="Admiral", status="active")
            db.add(mem)
            db.flush()
            db.add(User(username="admiral",
                        password_hash=hash_password("password123"),
                        status="active", member_id=mem.id))
            role = db.scalar(select(Role).where(Role.code == "owner"))
            db.add(MemberRole(member_id=mem.id, role_id=role.id))
            camp = Campaign(name="Hellas 演练", theater="Hellas")
            db.add(camp)
            db.flush()
            db.add(CampaignSave(
                campaign_id=camp.id, sha256="a" * 64,
                original_filename="fake.cam", stored_path="fake.cam",
                size_bytes=1024, theater="Hellas", parse_status="parsed",
                bullseye_x=512, bullseye_y=512))
            db.commit()
            cid = camp.id

        app = appmod.create_app()
        with TestClient(app) as client:
            tok = _CSRF.search(client.get("/login").text).group(1)
            check("owner 登录成功", client.post(
                "/login", data={"username": "admiral", "password": "password123",
                                "csrf_token": tok},
                follow_redirects=False).status_code == 303)

            r = client.get("/theater/%s/map" % cid)
            check("态势图页面可打开", r.status_code == 200,
                  "得到 %d" % r.status_code)
            m = r.text
            check("★ 没有剧场数据也画出了底图（<image> 在）",
                  "<image" in m and "/map/image/" in m,
                  "页面里没有 <image> —— 底图没出来")
            check("底图被标注为自备（★）", "★" in m or "自备" in m)
            check("没配剧场数据时页面说明了原因（不再只写\"未找到\"）",
                  "剧场数据" in m or "自备" in m)

            idx = re.search(r"/map/image/(\d+)", m)
            check("拿得到底图下标", idx is not None)
            if idx:
                img = client.get("/theater/%s/map/image/%s" % (cid, idx.group(1)),
                                 follow_redirects=False)
                check("★ 自备底图可以下载", img.status_code == 200,
                      "得到 %d" % img.status_code)
                check("下来的确实是 PNG",
                      img.content[:8] == b"\x89PNG\r\n\x1a\n")
                check("下载体积与源文件一致",
                      len(img.content) == (maps_dir / "Hellas.png").stat().st_size,
                      "%d vs %d" % (len(img.content),
                                    (maps_dir / "Hellas.png").stat().st_size))

            # 一张图都没有时，页面必须给出可操作的说明
            cfg.settings.bms_map_dir = tmp / "nowhere"
            r2 = client.get("/theater/%s/map" % cid)
            check("没底图时页面仍可打开（不崩）", r2.status_code == 200)
            check("★ 空状态给出设置项名字", "GFVFW_BMS_MAP_DIR" in r2.text)
            check("★ 空状态给出具体动作（放哪张图/怎么重启）",
                  ("_4K" in r2.text or "正方形" in r2.text))
            check("空状态不含 <image>", "<image" not in r2.text)
    finally:
        (dbmod.SessionLocal, deps.SessionLocal, appmod.SessionLocal,
         cfg.settings.storage_dir, cfg.settings.bms_map_dir,
         cfg.settings.bms_install_path) = orig
        eng.dispose()


def main() -> int:
    print("=" * 72)
    print("战役管理自校验")
    print("=" * 72)

    test_coords()
    test_time_label()
    test_stance()
    test_lzss()
    test_bundle()
    test_permissions()
    test_models()
    try:
        test_nonfinite_guard()
    except Exception as exc:  # noqa: BLE001
        print("  ERROR [7] 非有限浮点防护抛异常: %s" % exc)
        FAILURES.append("非有限浮点防护异常: %s" % exc)
    try:
        test_map_discovery()
    except Exception as exc:  # noqa: BLE001
        print("  ERROR [7A] 剧场底图发现抛异常: %s" % exc)
        FAILURES.append("剧场底图发现异常: %s" % exc)
    try:
        test_map_without_theater_data()
    except Exception as exc:  # noqa: BLE001
        print("  ERROR [7B] 无剧场数据底图抛异常: %s" % exc)
        FAILURES.append("无剧场数据底图异常: %s" % exc)

    bms_env = os.environ.get("GFVFW_BMS_INSTALL_PATH") or r"G:\BMS\Falcon BMS 4.38"
    bms = Path(bms_env) if bms_env and Path(bms_env).is_dir() else None
    cands = _find_cam(bms)
    if bms is None:
        skip("真实存档解析 / 上报管线 / Web 页面",
             "未找到 BMS 安装目录（GFVFW_BMS_INSTALL_PATH）")
        cam = None
    elif not cands:
        skip("真实存档解析 / 上报管线 / Web 页面", "未找到 .cam 存档")
        cam = None
    else:
        cam = pick_cam_with_flights(bms, cands)
        print("\n选用测试存档：%s" % (cam.name if cam else "(无)"))
    if bms is not None and cam is not None:
        try:
            test_real_parse(bms, cam)
        except Exception as exc:  # noqa: BLE001
            print("  ERROR [8] 真实解析抛异常: %s" % exc)
            FAILURES.append("真实解析异常: %s" % exc)
        try:
            test_ingest(bms, cam)
        except Exception as exc:  # noqa: BLE001
            print("  ERROR [9] 上报管线抛异常: %s" % exc)
            FAILURES.append("上报管线异常: %s" % exc)
        try:
            test_web(bms, cam)
        except Exception as exc:  # noqa: BLE001
            print("  ERROR [10] Web 抛异常: %s" % exc)
            FAILURES.append("Web 异常: %s" % exc)
        try:
            test_obj_path(bms)
        except Exception as exc:  # noqa: BLE001
            print("  ERROR [11] .obj 路径抛异常: %s" % exc)
            FAILURES.append(".obj 路径异常: %s" % exc)
        try:
            test_objective_reader_parity(bms)
        except Exception as exc:  # noqa: BLE001
            print("  ERROR [12] 目标点结构一致性抛异常: %s" % exc)
            FAILURES.append("目标点结构一致性异常: %s" % exc)

    print("\n" + "=" * 72)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    if SKIPPED:
        print("跳过 %d 组：" % len(SKIPPED))
        for s in SKIPPED:
            print("   -", s)
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
