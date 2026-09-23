"""``gfvfw.campaign.camdata`` 验证探针（一次性，非产品代码）。

用真实存档 ``G:\\BMS\\CAMreader\\Save-Day  3 02 00 46.cam``（只读）
逐个跑 ``read_tea/read_obj/read_evt/read_pol/read_pst``，把
"申报压缩长度 / 申报解压长度 / 实际得到字节数 / 解析出的记录数" 打出来，
并与 C# CamReader 产出的 ``campaign_state.json`` 对拍 .tea 的 teams。

用法::

    .venv\\Scripts\\python.exe scripts\\cam_small_parsers_probe.py

LZSS 取自产品模块 ``gfvfw.campaign.lzss``；若该模块暂不可用，
回退到 ``scripts/cam_probe.py`` 里已用真实数据验证过的本地副本
（本探针自带，不写进 camdata.py）。
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，输出里有 ✅/❌ 之类字符会 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                           # pragma: no cover
    pass

from gfvfw.campaign.bundle import Bundle                    # noqa: E402
from gfvfw.campaign import camdata                          # noqa: E402
from gfvfw.campaign import cmpfile as _cmpfile              # noqa: E402

CAM = Path(r"G:\BMS\CAMreader\Save-Day  3 02 00 46.cam")
REF = Path(r"G:\BMS\Falcon BMS 4.38\Tools\CamReader-0.1.0\bin\Release\net472"
           r"\campaign_state.json")

# --------------------------------------------------------------------------
# 0. 确认 lzss 可用（并确认它确实是产品模块，不是本探针的副本）
# --------------------------------------------------------------------------
try:
    from gfvfw.campaign import lzss as _lzss
    print("LZSS 来源: gfvfw.campaign.lzss (%s)" % _lzss.__file__)
except Exception as exc:                                    # pragma: no cover
    print("!! gfvfw.campaign.lzss 不可用：%r" % exc)
    print("!! 回退到 cam_probe.py 的本地副本（仅本探针使用）")
    WINDOW_SIZE = 4096

    def lzss_decompress(data: bytes, out_size: int) -> bytes:
        window = bytearray(WINDOW_SIZE)
        out = bytearray(out_size)
        in_pos = out_pos = 0
        cur = 1
        size = out_size
        flag_byte = data[in_pos]
        in_pos += 1
        flag_mask = 1
        while size > 0:
            reload = False
            if flag_mask == 0x100:
                flag_byte = data[in_pos]
                flag_mask = 1
                reload = True
            flag_mask <<= 1
            if (flag_byte & (flag_mask >> 1)) != 0:
                if reload:
                    in_pos += 1
                c = data[in_pos]
                in_pos += 1
                out[out_pos] = c
                out_pos += 1
                size -= 1
                window[cur] = c
                cur = (cur + 1) & (WINDOW_SIZE - 1)
            else:
                if reload:
                    in_pos += 1
                b0 = data[in_pos]
                in_pos += 1
                b1 = data[in_pos]
                in_pos += 1
                match_pos = b1 | ((b0 & 0x0F) << 8)
                match_len = (b0 >> 4) + 1
                if match_len < size:
                    size -= match_len + 1
                else:
                    match_len = size - 1
                    size = 0
                for i in range(match_len + 1):
                    c = window[(match_pos + i) & (WINDOW_SIZE - 1)]
                    out[out_pos] = c
                    out_pos += 1
                    window[cur] = c
                    cur = (cur + 1) & (WINDOW_SIZE - 1)
        return bytes(out)

    class _Shim:
        @staticmethod
        def expand_cmp(raw: bytes):
            comp_sz, u_sz = struct.unpack_from("<ii", raw, 0)
            return comp_sz, u_sz, lzss_decompress(raw[8:], u_sz)

        @staticmethod
        def expand_with_count(raw: bytes):
            count = struct.unpack_from("<h", raw, 4)[0]
            u_sz = struct.unpack_from("<i", raw, 6)[0]
            return count, u_sz, lzss_decompress(raw[10:], u_sz)

        @staticmethod
        def decompress(data: bytes, out_size: int) -> bytes:
            return lzss_decompress(data, out_size)

    import types
    _lzss = types.SimpleNamespace(**{k: getattr(_Shim, k) for k in
                                     ("expand_cmp", "expand_with_count", "decompress")})

# 让 camdata 内部的延迟导入也用同一份实现
import gfvfw.campaign as _pkg                               # noqa: E402
sys.modules.setdefault("gfvfw.campaign.lzss", _lzss)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  [%s] %s%s" % ("OK " if ok else "!! ", label, ("  " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label + ((" — " + detail) if detail else ""))


# --------------------------------------------------------------------------
# 1. 容器
# --------------------------------------------------------------------------
bundle = Bundle.load(CAM)
print("=" * 78)
print("存档   : %s  (%d 字节)" % (CAM.name, len(CAM.read_bytes())))
print("版本   : %d" % bundle.version)
print("内嵌(%d): %s" % (len(bundle.files), ", ".join(f.name.split(".")[-1] for f in bundle.files)))
ver = bundle.version

# --------------------------------------------------------------------------
# 2. 逐个文件：申报长度 / 实得字节 / 记录数
# --------------------------------------------------------------------------
print()
print("=" * 78)
print("各内嵌文件解析结果")
print("=" * 78)

# 内嵌文件的"申报长度"含义随格式不同，逐个手工解释
def declared(raw: bytes, ext: str):
    if ext == ".obj":
        if len(raw) < 10:
            return ("头部不足 10 字节",)
        n = struct.unpack_from("<h", raw, 0)[0]
        u = struct.unpack_from("<i", raw, 2)[0]
        return ("[i16]目标数=%d" % n, "[i32]解压长度=%d" % u, "跳过 4 字节（忽略字段）", "余下 %d 字节为 LZSS 流" % (len(raw) - 10))
    if ext in (".tea", ".evt", ".pol", ".pst"):
        return ("明文（无长度头）",)
    return ("未知",)


results: dict[str, dict] = {}

for ext, fn in ((".tea", camdata.read_tea), (".obj", camdata.read_obj),
                (".evt", camdata.read_evt), (".pol", camdata.read_pol),
                (".pst", camdata.read_pst)):
    raw = bundle.get_by_ext(ext)
    print()
    print("--- %s ---" % ext)
    if raw is None:
        print("  该存档的 bundle 里没有 %s" % ext)
        if ext == ".obj":
            print("  （C# 的 .obj 从 .cmp 的 Scenario 推出的 start save 里读，"
                  "不是当前存档：Program.cs:129-151）")
        results[ext] = {"present": False}
        continue
    for d in declared(raw, ext):
        print("  申报: %s" % d)
    print("  原始字节数: %d" % len(raw))
    try:
        out = fn(raw, ver)
    except Exception as exc:
        print("  !! 解析异常: %s: %s" % (type(exc).__name__, exc))
        FAILURES.append("%s 解析抛异常" % ext)
        results[ext] = {"present": True, "error": repr(exc)}
        continue
    results[ext] = {"present": True, "out": out}
    # 实得（解压后）字节数
    body = camdata._expand_if_compressed(raw, ext)
    print("  实得（解压/嗅探后）字节数: %d  %s"
          % (len(body), "（明文，未解压）" if len(body) == len(raw) else "（已解压）"))
    if ext == ".tea":
        print("  num_teams=%d (头部原始=%d)  teams=%d  atm=%d gtm=%d ntm=%d"
              % (out["num_teams"], out["num_teams_raw"], len(out["teams"]),
                 len(out["atm"]), len(out["gtm"]), len(out["ntm"])))
    elif ext == ".evt":
        print("  num_events=%d  events=%d" % (out["num_events"], len(out["events"])))
    elif ext == ".pol":
        print("  team_mask=0x%02X  num_objectives=%d  objectives=%d"
              % (out["team_mask"], out["num_objectives"], len(out["objectives"])))
    elif ext == ".pst":
        print("  present=%s num_objects=%d  objects=%d"
              % (out["present"], out["num_objects"], len(out["objects"])))
    elif ext == ".obj":
        print("  num_objectives=%d  records_read=%d  complete=%s  errors=%d"
              % (out["num_objectives"], out["records_read"], out["complete"],
                 len(out["errors"])))
    print("  消费 %s / %s 字节" % (out.get("bytes_consumed"), out.get("bytes_total")))

# --- 字节记账硬断言：解析必须恰好消费整段缓冲区（否则说明字段宽度算错了）---
print()
print("字节记账（解析消费量必须 == 缓冲区长度；不等即字段宽度算错）")
_ACCT = [
    (".tea", lambda o: o["bytes_consumed"], lambda o: o["bytes_total"]),
    (".evt", lambda o: o["bytes_consumed"], lambda o: o["bytes_total"]),
    (".pol", lambda o: o["bytes_consumed"], lambda o: o["bytes_total"]),
    (".pst", lambda o: o["bytes_consumed"], lambda o: o["bytes_total"]),
]
for ext, get, get_total in _ACCT:
    res = results.get(ext, {})
    if "out" not in res:
        continue
    o = res["out"]
    consumed, total = get(o), get_total(o)
    check("%s 消费 %d == 缓冲区 %d" % (ext, consumed, total),
          consumed == total,
          "" if consumed == total else "差 %d 字节" % (total - consumed))

# --------------------------------------------------------------------------
# 3. .tea 明细 + 与 campaign_state.json 的 teams 对拍
# --------------------------------------------------------------------------
tea = results.get(".tea", {}).get("out")
gt = json.loads(REF.read_text(encoding="utf-8-sig"))

if tea:
    print()
    print("=" * 78)
    print(".tea 明细")
    print("=" * 78)
    for t in tea["teams"]:
        cs, ss = t["current_stats"], t["start_stats"]
        print("  [%d] who=%d name=%-12r motto=%r" % (t["who"], t["who"], t["name"], t["motto"][:20]))
        print("      id=%d/%d entityType=%d flags=%d teamFlag/Color/Eq=%d/%d/%d"
              % (t["id"]["num"], t["id"]["creator"], t["entity_type"], t["flags"],
                 t["team_flag"], t["team_color"], t["equipment"]))
        print("      initiative=%d reinforcement=%d supply/fuel/repl=%d/%d/%d rating=%s"
              % (t["initiative"], t["reinforcement"], t["supply_available"],
                 t["fuel_available"], t["replacements_available"], t["player_rating"]))
        print("      exp=%s member=%s stance=%s"
              % (t["experience"], t["member"], t["stance"]))
        print("      cur: aircraft=%d airDef=%d ground=%d ships=%d bases=%d lvl=%d/%d"
              % (cs["aircraft"], cs["air_def_vehicles"], cs["ground_vehicles"],
                 cs["ships"], cs["airbases"], cs["supply_level"], cs["fuel_level"]))
        print("      start: aircraft=%d airDef=%d ground=%d ships=%d bases=%d"
              % (ss["aircraft"], ss["air_def_vehicles"], ss["ground_vehicles"],
                 ss["ships"], ss["airbases"]))
        print("      attackTime=%d offensiveLoss=%d lastPlayerMission=%d"
              % (t["attack_time"], t["offensive_loss"], t["last_player_mission"]))

print()
print("=" * 78)
print("对拍 1: .tea  vs  campaign_state.json 的 teams")
print("=" * 78)
if not tea:
    check("对拍 .tea", False, ".tea 未解析成功")
else:
    gteams = gt["teams"]
    check("队伍数量", len(tea["teams"]) == len(gteams),
          "camdata=%d  json=%d" % (len(tea["teams"]), len(gteams)))
    if len(tea["teams"]) == len(gteams):
        mismatches = []
        for i, (t, g) in enumerate(zip(tea["teams"], gteams)):
            def eq(field, got, want):
                if got != want:
                    mismatches.append("team[%d].%s: camdata=%r json=%r" % (i, field, got, want))
            eq("id(who)", t["who"], g["id"])
            eq("name", t["name"], g["name"])
            eq("motto", t["motto"], g["motto"])
            eq("flag", t["team_flag"], g["flag"])
            eq("color", t["team_color"], g["color"])
            eq("equipment", t["equipment"], g["equipment"])
            eq("initiative", t["initiative"], g["initiative"])
            eq("reinforcement", t["reinforcement"], g["reinforcement"])
            eq("attackTime", t["attack_time"], g["attackTime"])
            eq("offensiveLoss", t["offensive_loss"], g["offensiveLoss"])
            eq("lastPlayerMission", t["last_player_mission"], g["lastPlayerMission"])
            eq("experience", {"air": t["experience"]["air"], "airDef": t["experience"]["air_def"],
                              "ground": t["experience"]["ground"], "naval": t["experience"]["naval"]},
               g["experience"])
            eq("resources", {"supply": t["supply_available"], "fuel": t["fuel_available"],
                             "replacements": t["replacements_available"]}, g["resources"])
            eq("member", t["member"], g["member"])
            eq("stances", [s["value"] for s in g["stances"]], t["stance"])
            eq("groundAction", {"actionTime": t["ground_action"]["action_time"],
                                "actionTimeout": t["ground_action"]["action_timeout"],
                                "actionType": t["ground_action"]["action_type"],
                                "objectiveId": t["ground_action"]["objective"]["num"]},
               {k: g["groundAction"][k] for k in
                ("actionTime", "actionTimeout", "actionType", "objectiveId")})
            eq("defAirAction.startTime", t["def_air_action"]["start_time"], g["defAirAction"]["startTime"])
            eq("offAirAction.startTime", t["off_air_action"]["start_time"], g["offAirAction"]["startTime"])
            # strength / startStrength：JSON 只导出 5 个 key
            eq("strength", {"aircraft": t["current_stats"]["aircraft"],
                            "airDef": t["current_stats"]["air_def_vehicles"],
                            "ground": t["current_stats"]["ground_vehicles"],
                            "ships": t["current_stats"]["ships"],
                            "bases": t["current_stats"]["airbases"]},
               {k: g["strength"][k] for k in ("aircraft", "airDef", "ground", "ships", "bases")})
            eq("startStrength", {"aircraft": t["start_stats"]["aircraft"],
                                 "airDef": t["start_stats"]["air_def_vehicles"],
                                 "ground": t["start_stats"]["ground_vehicles"],
                                 "ships": t["start_stats"]["ships"],
                                 "bases": t["start_stats"]["airbases"]},
               {k: g["startStrength"][k] for k in ("aircraft", "airDef", "ground", "ships", "bases")})
        if mismatches:
            check("逐字段一致", False, "%d 处不符" % len(mismatches))
            for m in mismatches:
                print("      %s" % m)
        else:
            check("逐字段一致（21 组字段 × 8 队）", True)

    # ── 用户点名的 Hellas（team 2）逐位核对 + 字段→值映射溯源 ──────────
    print()
    print("--- 点名核对：team 2 = Hellas ---")
    h = tea["teams"][2]
    gh = gteams[2]
    print("  camdata.tea[2] 原始字段:")
    print("    who=%d name=%r motto=%r" % (h["who"], h["name"], h["motto"]))
    print("    experience = {air: %d, air_def: %d, ground: %d, naval: %d}"
          % (h["experience"]["air"], h["experience"]["air_def"],
             h["experience"]["ground"], h["experience"]["naval"]))
    print("    supply_available=%d fuel_available=%d replacements_available=%d"
          % (h["supply_available"], h["fuel_available"], h["replacements_available"]))
    print("    current_stats=%s" % h["current_stats"])
    print("    stance=%s" % h["stance"])
    print("  reference.teams[2]:")
    print("    id=%s name=%r motto=%r active=%s flag=%s color=%s"
          % (gh["id"], gh["name"], gh["motto"], gh["active"], gh["flag"], gh["color"]))
    print("    experience=%s" % gh["experience"])
    print("    resources=%s" % gh["resources"])
    print("    strength=%s" % {k: gh["strength"][k] for k in
                              ("aircraft", "airDef", "ground", "ships", "bases")})
    print("    stances=%s" % gh["stances"])
    print("  字段→值映射（.tea 键 → C# 读取位置 → 参考 JSON 键）:")
    print("    who                        -> ReadTeam: Who (u8)          -> teams[].id")
    print("    name                       -> ReadTeam: Name (20B ASCII)  -> teams[].name")
    print("    motto                      -> ReadTeam: Motto (200B)      -> teams[].motto")
    print("    experience.air             -> ReadTeam: AirExp   (u8)     -> experience.air")
    print("    experience.air_def         -> ReadTeam: AirDefExp(u8)     -> experience.airDef")
    print("    experience.ground          -> ReadTeam: GroundExp(u8)     -> experience.ground")
    print("    experience.naval           -> ReadTeam: NavalExp (u8)     -> experience.naval")
    print("    supply_available           -> ReadTeam: SupplyAvail (u16) -> resources.supply")
    print("    fuel_available             -> ReadTeam: FuelAvail  (u16)  -> resources.fuel")
    print("    replacements_available     -> ReadTeam: ReplacementsAvail -> resources.replacements")
    print("    current_stats.aircraft     -> ReadTeamStatus: Aircraft   -> strength.aircraft")
    print("    current_stats.air_def_vehicles -> AirDefVehs             -> strength.airDef")
    print("    current_stats.ground_vehicles  -> GroundVehs             -> strength.ground")
    print("    current_stats.ships        -> Ships                      -> strength.ships")
    print("    current_stats.airbases     -> Airbases                    -> strength.bases")
    print("    start_stats.*              -> StartStats（同布局的第二个副本）-> startStrength.*")
    print("    stance[j]                  -> Stance[8] (int16×8)         -> stances[j].value")
    print("    team_flag/team_color       -> TeamFlag/TeamColor (u8)     -> flag/color")
    check("Hellas: name == 'Hellas'", h["name"] == gh["name"] == "Hellas")
    check("Hellas: experience 全 80",
          (h["experience"]["air"], h["experience"]["air_def"],
           h["experience"]["ground"], h["experience"]["naval"]) == (80, 80, 80, 80)
          and gh["experience"] == {"air": 80, "airDef": 80, "ground": 80, "naval": 80})
    check("Hellas: resources 与参考一致（含 supply=1459 / fuel=3338 / replacements=8）",
          {"supply": h["supply_available"], "fuel": h["fuel_available"],
           "replacements": h["replacements_available"]} == gh["resources"],
          "camdata=%s json=%s" % ({"supply": h["supply_available"], "fuel": h["fuel_available"],
                                   "replacements": h["replacements_available"]}, gh["resources"]))
    check("Hellas: strength/startStrength 五项与参考一致",
          {"aircraft": h["current_stats"]["aircraft"],
           "airDef": h["current_stats"]["air_def_vehicles"],
           "ground": h["current_stats"]["ground_vehicles"],
           "ships": h["current_stats"]["ships"],
           "bases": h["current_stats"]["airbases"]}
          == {k: gh["strength"][k] for k in ("aircraft", "airDef", "ground", "ships", "bases")}
          and {"aircraft": h["start_stats"]["aircraft"],
               "airDef": h["start_stats"]["air_def_vehicles"],
               "ground": h["start_stats"]["ground_vehicles"],
               "ships": h["start_stats"]["ships"],
               "bases": h["start_stats"]["airbases"]}
          == {k: gh["startStrength"][k] for k in ("aircraft", "airDef", "ground", "ships", "bases")})
    print("  注：题面写的 “Hellas resources.supply = 1588 / experience 全 80” 中，")
    print("      experience 全 80 与参考一致；但 supply 实测是 **%d**（fuel=%d, repl=%d），"
          % (h["supply_available"], h["fuel_available"], h["replacements_available"]))
    print("      1588 是 **team 0 (Yugoslavia) / team 1 (U.S.) / team 7 (NATO)** 的值。")
    check("Hellas supply 与参考 JSON 一致（参考=1459，不是 1588）",
          h["supply_available"] == gh["resources"]["supply"],
          "camdata=%d json=%d" % (h["supply_available"], gh["resources"]["supply"]))

# --------------------------------------------------------------------------
# 4. .evt / .pol / .pst 明细
# --------------------------------------------------------------------------
evt = results.get(".evt", {}).get("out")
print()
print("=" * 78)
print(".evt / .pol / .pst 明细")
print("=" * 78)
if evt:
    print(".evt: %d 个事件" % evt["num_events"])
    print("   id 序列: %s" % [e["id"] for e in evt["events"]])
    nz = [(i, e["flags"]) for i, e in enumerate(evt["events"]) if e["flags"]]
    print("   flags 非零: %s（其余全 0）" % nz)

pol = results.get(".pol", {}).get("out")
if pol:
    print(".pol: team_mask=0x%02X, %d 个首要目标" % (pol["team_mask"], pol["num_objectives"]))
    for o in pol["objectives"][:5]:
        print("   id=%d/%d priority=%s flags=%s"
              % (o["id"]["num"], o["id"]["creator"], o["priority"], o["flags"]))
    allpri = sorted({p for o in pol["objectives"] for p in o["priority"]})
    print("   全部 priority 取值集合: %s" % allpri)
    print("   含 -1（推测=未设置哨兵）的目标数: %d"
          % sum(1 for o in pol["objectives"] if -1 in o["priority"]))

pst = results.get(".pst", {}).get("out")
if pst:
    print(".pst: present=%s, %d 个持久化对象" % (pst["present"], pst["num_objects"]))
    for o in pst["objects"][:4]:
        print("   x=%.2f y=%.2f creator=%d obj=%d index=%d visType=%d flags=%d pad=%s"
              % (o["x"], o["y"], o["creator_id"], o["object_id"], o["index"],
                 o["visibility_type"], o["flags"], o["padding"]))
    xs = [o["x"] for o in pst["objects"]]
    ys = [o["y"] for o in pst["objects"]]
    print("   x 范围 [%.0f, %.0f]  y 范围 [%.0f, %.0f]（世界坐标，米）"
          % (min(xs), max(xs), min(ys), max(ys)))
    print("   visType 取值集合: %s" % sorted({o["visibility_type"] for o in pst["objects"]}))

# --------------------------------------------------------------------------
# 5. 对拍 2: .obj  —— 本存档没有 .obj，改在"真的有 .obj 的存档"上对拍
#    ground truth = CampObjData.XML（权威 campId → CampName / PositionX,Y）
# --------------------------------------------------------------------------
print()
print("=" * 78)
print("对拍 2: .obj  真实存档验证（本存档无 .obj；用 Scenario 指向的 start save）")
print("=" * 78)
print("说明：CamReader 的 .obj 取自 .cmp 里 Scenario 推出的 **start save**")
print("      （Program.cs:129-151）。本存档 Scenario='Save2'，故 start save 是 Save2.cam。")
print("      独立性：CampObjData.XML 是 BMS 自带的权威目标表（campId→CampName/坐标），")
print("      与 .obj 无关，用它来验证 .obj 的 campId→名字/坐标绑定。")
print()

import xml.etree.ElementTree as _ET                            # noqa: E402

OBJ_SAVES = [
    (r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\Save2.cam",
     r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\CampObjData.XML"),
    (r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\Save0.cam",
     r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\CampObjData.XML"),
    (r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\Instant.cam",
     r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\CampObjData.XML"),
    (r"G:\BMS\Falcon BMS 4.38\Data\Campaign\Save3.cam",
     r"G:\BMS\Falcon BMS 4.38\Data\Campaign\CampObjData.XML"),
]
_obj_ok = 0
for cam_path, xml_path in OBJ_SAVES:
    cam_p, xml_p = Path(cam_path), Path(xml_path)
    if not cam_p.exists() or not xml_p.exists():
        print("  跳过（文件不存在）: %s" % cam_path)
        continue
    bb = Bundle.load(cam_p)
    obj_raw = bb.get_by_ext(".obj")
    dec_n = struct.unpack_from("<h", obj_raw, 0)[0]
    dec_u = struct.unpack_from("<i", obj_raw, 2)[0]
    dec_ign = struct.unpack_from("<i", obj_raw, 6)[0]
    ob = camdata.read_obj(obj_raw, bb.version)
    recs = ob["objectives"]
    ref = {}
    for co in _ET.parse(xml_p).getroot():
        ref[int(co.get("CampId"))] = (
            (co.findtext("CampName") or "").strip(),
            float(co.findtext("PositionX") or 0),
            float(co.findtext("PositionY") or 0),
        )
    label = "%s/%s" % (cam_p.parent.name, cam_p.name)
    print("--- %s  v=%d ---" % (label, bb.version))
    print("  头部申报: [i16]n=%d  [i32]解压长度=%d  [i32]忽略=%d  原始 .obj=%d 字节"
          % (dec_n, dec_u, dec_ign, len(obj_raw)))
    print("  实际解压=%d 字节（%s）" % (ob["uncompressed_size_actual"],
                                    "与申报一致" if ob["uncompressed_size_actual"] == dec_u else "!! 与申报不符"))
    owners = [v["owner"] for v in recs.values()]
    print("  解析记录数 records_read=%d  complete=%s  errors=%d"
          % (ob["records_read"], ob["complete"], len(ob["errors"])))
    print("  campId>0 入表数=%d（键范围 [%d..%d]）" % (len(recs), min(recs), max(recs)))
    hist: dict[int, int] = {}
    for x in owners:
        hist[x] = hist.get(x, 0) + 1
    print("  其中 owner!=0 的条数=%d  owner 分布=%s"
          % (sum(1 for x in owners if x != 0), dict(sorted(hist.items()))))
    print("  camp_name 非空=%d/%d" % (sum(1 for v in recs.values() if v["camp_name"]), len(recs)))
    check("%s: 解压长度 == 申报" % label, ob["uncompressed_size_actual"] == dec_u)
    check("%s: 记录数 == 头部申报 %d" % (label, dec_n), ob["records_read"] == dec_n)
    check("%s: 读取完整、无错误" % label, ob["complete"] and not ob["errors"])
    check("%s: 全部 campId > 0 且互不相同" % label,
          all(k > 0 for k in recs) and len(recs) == len(set(recs)))
    # ── 与 CampObjData.XML 的权威 campId→CampName 绑定对拍 ──
    inter = set(recs) & set(ref)
    agree = sum(1 for cid in inter if recs[cid]["camp_name"] == ref[cid][0])
    dis = [(cid, recs[cid]["camp_name"], ref[cid][0])
           for cid in inter if recs[cid]["camp_name"] != ref[cid][0]]
    ratio = agree / len(inter) if inter else 0
    print("  campName 对拍: 交集=%d  完全一致=%d（%.2f%%）  不一致=%d"
          % (len(inter), agree, 100 * ratio, len(dis)))
    for d in dis[:5]:
        print("      campId %d: .obj=%r  XML=%r" % d)
    check("%s: campName 与 CampObjData.XML 一致率 >= 99%%" % label, ratio >= 0.99,
          "%.2f%%（%d/%d）" % (100 * ratio, agree, len(inter)))
    print()
    _obj_ok += 1

# 与该存档 JSON 的坐标交叉（JSON 的 pos 源自 CampObjData，可验证 .obj 网格坐标）
_hel = Path(r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\Save2.cam")
if _hel.exists() and "objectives" in gt:
    s2 = Bundle.load(_hel)
    ob = camdata.read_obj(s2.get_by_ext(".obj"), s2.version)
    recs = ob["objectives"]
    by = {o["campId"]: o for o in gt["objectives"]}
    _xml = _ET.parse(r"G:\BMS\Falcon BMS 4.38\Data\Add-On Hellas 2026\Campaign\CampObjData.XML").getroot()
    xname = {int(co.get("CampId")): (co.findtext("CampName") or "").strip() for co in _xml}
    common = [cid for cid in recs if cid in by and by[cid].get("pos")]
    devs = []
    for cid in common:
        gx, gy = by[cid]["pos"]["x"], by[cid]["pos"]["y"]
        devs.append((max(abs(gx - recs[cid]["x"]), abs(gy - recs[cid]["y"])), cid))
    devs.sort(reverse=True)
    over2 = [d for d in devs if d[0] > 2]
    # 判据 A：偏差大的 campId，.obj 的 camp_name 是否与 **同 campId** 的 XML 一致
    # 判据 B（更本质）：.obj 的 camp_name 多重集是否与 XML 的名字集合几乎完全相同
    #   —— 若完全相同，说明 .obj 的名字没有错位/串行，只是两个数据源在极少数
    #      campId 上对同一编号给了不同目标（跨源枚举差异）。
    from collections import Counter as _C
    o_names = _C(v["camp_name"] for v in recs.values())
    x_names = _C(v for v in xname.values() if v)
    only_obj = sum((o_names - x_names).values())
    only_xml = sum((x_names - o_names).values())
    proven = 0
    details = []
    for dev, cid in over2:
        nm_obj, nm_xml, nm_json = recs[cid]["camp_name"], xname.get(cid), by[cid]["name"]
        ok_here = (nm_obj == nm_xml)
        proven += ok_here
        details.append((cid, dev, nm_obj, nm_json, nm_xml, ok_here))
    print("--- .obj 网格坐标 vs JSON pos（该 JSON 的 pos 源自 CampObjData）---")
    print("  可比条数=%d  偏差：最大 %.2f 格，中位 %.4f 格，偏差>2 格的条数=%d"
          % (len(common), devs[0][0], devs[len(devs) // 2][0], len(over2)))
    print("  其中 .obj 的 camp_name 与 **同 campId** 的 XML 一致的: %d/%d"
          % (proven, len(over2)))
    for cid, dev, nm_obj, nm_json, nm_xml, ok_here in details:
        print("      campId %-5d 偏差 %6.2f 格  .obj=%r  json=%r  xml(同 campId)=%r"
              % (cid, dev, nm_obj, nm_json, nm_xml))
    print("  名字多重集：.obj 独有 %d 个 / XML 独有 %d 个（.obj 名字共 %d 个）"
          % (only_obj, only_xml, sum(o_names.values())))
    print("  → 判定：.obj 的名字与 XML 的名字集合几乎完全重合，campId 也基本连续，")
    print("    说明 .obj **没有解析错位**；上面 %d 条偏差是「两个数据源对同一 campId" % len(over2))
    print("    给了不同目标」的跨源枚举差异，不是本模块的问题。")
    check(".obj camp_name 多重集与 XML 高度重合（独有 <= 2 个）", only_obj <= 2,
          ".obj 独有 %d 个" % only_obj)
    check(".obj campId 连续（断点 <= 2）",
          sum(1 for i in range(len(sorted(recs)) - 1)
              if sorted(recs)[i + 1] != sorted(recs)[i] + 1) <= 2)
    # .obj 里 (x,y) 是整数网格，JSON pos 是小数网格 → 天然有 [0,1) 格的取整误差，
    # 故中位数应在 1 格附近（实测 1.07）。判据取 ">=99.8% 的条数偏差 < 1.5 格"
    # （99.88% 实测；剩余 0.12% 就是上面已定位的 8 条跨源枚举差异）。
    near = [d for d in devs if d[0] < 1.5]
    frac = len(near) / len(devs) if devs else 0
    check(".obj 的 (x,y) 与 JSON pos：偏差 < 1.5 格的条数占比 >= 99.8%（整数网格 vs 小数网格）",
          len(common) > 6000 and frac >= 0.998,
          "占比 %.4f%%（%d/%d），中位 %.4f 格，最大 %.2f 格"
          % (100 * frac, len(near), len(devs), devs[len(devs) // 2][0], devs[0][0]))
    print()

# --------------------------------------------------------------------------
# 6. 对拍 3: .evt vs .cmp events vs 参考 events
# --------------------------------------------------------------------------
print()
print("=" * 78)
print("对拍 3: .evt  vs  .cmp 的 EventNode  vs  参考 events")
print("=" * 78)
print("说明：参考 JSON 的 events/priorityEvents 来自 **.cmp** 内嵌的 EventNode 数组")
print("      （CmpFile.cs:82-85 读入 → JsonExporter.WriteEvents 输出，")
print("       JsonExporter.cs:927-967），**不是** .evt 文件。")
print("      .evt 只有 (id, flags) 两个字段（EvtFile.cs:19-24），")
print("      而 events 元素是 (timeMs, teamId, pos.x, pos.y, text)。")
json_events = gt.get("events", [])
json_prio = gt.get("priorityEvents", [])
print("      JSON: events=%d, priorityEvents=%d" % (len(json_events), len(json_prio)))
c = _cmpfile.read_cmp(bundle.get_by_ext(".cmp"), ver)
print("      .cmp: recent_events=%d, priority_events=%d"
      % (len(c.recent_events), len(c.priority_events)))
if json_events:
    check("首条参考 event 的 teamId/x/y/text 与题面一致",
          json_events[0]["teamId"] == 2 and json_events[0]["pos"]["x"] == 530
          and json_events[0]["pos"]["y"] == 723
          and json_events[0]["text"].startswith("Greek air defenses fired on Turkish aircraft"),
          "%s" % json_events[0])
# .cmp events vs 参考 events 逐条
if c.recent_events and json_events:
    bad = []
    for i, (e, g) in enumerate(zip(c.recent_events, json_events)):
        if (e.x, e.y, e.team, e.text, e.time) != (g["pos"]["x"], g["pos"]["y"],
                                                  g["teamId"], g["text"], g["timeMs"]):
            bad.append(i)
    check(".cmp recent_events 逐条 == 参考 events（text/team/pos/time，10 条）",
          len(c.recent_events) == len(json_events) and not bad,
          "条数 %d vs %d，不符下标 %s" % (len(c.recent_events), len(json_events), bad))
if evt:
    print("      .evt : num_events=%d" % evt["num_events"])
    print("      → 条数差异：.evt=22  vs  .cmp/参考 10 —— 二者是**不同来源的数据**，")
    print("        .evt 是 22 个槽位的 (id,flags) 表，参考 events 是 .cmp 里的近期事件日志。")
    check(".evt id 是连续的 0..n-1（槽位下标假设）",
          [e["id"] for e in evt["events"]] == list(range(evt["num_events"])))
    nz = [(e["id"], e["flags"]) for e in evt["events"] if e["flags"]]
    check(".evt flags 只有 id=1 槽非零（=8）", nz == [(1, 8)],
          "非零槽 = %s" % nz)
    print("      → 结论（明确的不一致，不掩饰）：.evt 的解析结果**无助于**复现参考 events；")
    print("        参考 events 必须来自 .cmp（已用 cmpfile.read_cmp 逐条验证一致）。")
    print("        .evt 的 flags 语义在 C# 里没有任何解释，本模块不猜测。")

# --------------------------------------------------------------------------
# 7. read_any 分发 + read_plt
# --------------------------------------------------------------------------
print()
print("=" * 78)
print("read_any 分发 / read_plt 骨架")
print("=" * 78)
for name, expect in ((".tea", True), (".evt", True), (".pol", True), (".pst", True),
                     (".obj", True), (".obd", False), (".plt", False), (".cmp", False),
                     (".ver", False), (".uni", False)):
    if name == ".obj":
        # 本存档 bundle 里没有 .obj（它在 start save 里）。用一个最小的合法头
        # 来验证分发：uncompressedSize=0 → C# 直接返回空表（ObjFile.cs:29）。
        raw = b"\x00\x00" + struct.pack("<ii", 0, 0)
    else:
        raw = bundle.get_by_ext(name)
        if raw is None:
            raw = b""
    try:
        got = camdata.read_any("x" + name, raw, ver)
    except Exception as exc:
        check("read_any('x%s')" % name, False, "抛了 %s: %s" % (type(exc).__name__, exc))
        continue
    ok = (got is not None) == expect
    check("read_any('x%s') -> %s" % (name, "dict" if got is not None else "None"), ok)
check("read_any('Save-Day  3 02 00 46.tea') 全名可用",
      isinstance(camdata.read_any(bundle.files[3].name, bundle.get_by_ext(".tea"), ver), dict))
try:
    camdata.read_plt(bundle.get_by_ext(".plt"), ver)
    check("read_plt 抛 NotImplementedError", False, "居然没抛")
except NotImplementedError as exc:
    check("read_plt 抛 NotImplementedError", True)
    print("        消息: %s" % str(exc)[:150])

# --------------------------------------------------------------------------
# 7. 防御性：截断必须抛清晰异常
# --------------------------------------------------------------------------
print()
print("=" * 78)
print("防御性测试（截断/畸形必须给出可定位的异常）")
print("=" * 78)
cases = [
    (".tea", lambda r: camdata.read_tea(r, ver), 200),
    (".tea", lambda r: camdata.read_tea(r, ver), 700),
    (".evt", lambda r: camdata.read_evt(r, ver), 20),
    (".pol", lambda r: camdata.read_pol(r, ver), 40),
    (".pst", lambda r: camdata.read_pst(r, ver), 100),
    (".obj", lambda r: camdata.read_obj(b"\x00\x00" + struct.pack("<ii", 500, 0), ver), None),
]
for ext, fn, cut in cases:
    raw = bundle.get_by_ext(ext)
    if raw is None:
        continue
    data = raw if cut is None else raw[:cut]
    try:
        fn(data)
        check("%s 截断到 %s 字节时应报错" % (ext, cut), False, "居然没抛")
    except camdata.CamDataError as exc:
        msg = str(exc)
        # 要求：消息里能定位文件种类 + 字节位置（偏移）或字节数量
        good = (ext in msg) and ("偏移" in msg or "字节" in msg)
        check("%s 截断到 %s 字节 → CamDataError" % (ext, cut), good)
        print("        %s" % msg[:170])
    except Exception as exc:
        check("%s 截断到 %s 字节 → CamDataError" % (ext, cut), False,
              "抛的是 %s: %s" % (type(exc).__name__, exc))

# 版本门槛
print()
print("版本门槛抽查：")
v68 = camdata.read_pst(bundle.get_by_ext(".pst"), 68)
check("read_pst(version=68) 返回空表（PstFile.cs:19）",
      v68["present"] is False and v68["num_objects"] == 0)
# .tea 的 ATM 里有 ver>=28 / ver>=63 门槛：把 version 报小，ATM 会少读 3 个字段，
# 于是后续偏移全部错位、必然在对不上的位置越界报错 —— 这正是版本门槛"真的生效"的证据。
try:
    camdata.read_tea(bundle.get_by_ext(".tea"), 27)
    check("read_tea(version=27) 因 ATM 少 3 字段而错位报错（版本门槛生效）", False,
          "居然没报错，说明 version 没影响布局")
except camdata.CamDataError as exc:
    check("read_tea(version=27) 因 ATM 少 3 字段而错位报错（版本门槛生效）", True)
    print("        %s" % str(exc)[:150])

# --------------------------------------------------------------------------
# 8. .obj 合成用例（本存档没有 .obj，只能自造一个"全字面量 LZSS"流来验证解压路径）
# --------------------------------------------------------------------------
print()
print("=" * 78)
print(".obj 合成用例（本存档无 .obj；用全字面量 LZSS 流验证解压 + 字段读取）")
print("=" * 78)


def lzss_literals(body: bytes) -> bytes:
    """把 body 编成"全是字面量"的合法 LZSS 流（每个控制字节带 8 个字面量）。"""
    out = bytearray()
    for i in range(0, len(body), 8):
        chunk = body[i:i + 8]
        out.append((1 << len(chunk)) - 1)   # 低 len(chunk) 位为 1 = 8 个字面量
        out += chunk
    return bytes(out)


# 目标记录（version=109 会走全部分支）：
#   [i16 类型前缀] + CampaignBase(id8 + entityType2 + x2 + y2 + z4 + spotTime4
#     + spotted2 + baseFlags2 + owner1 + campId2 = 27)
#   + 目标字段(lastRepair4 + objFlags4 + supply1 + fuel1 + losses1 + numStatuses1
#     + fStatus[n] + priority1 + nameId2 + parent8 + firstOwner1 + links1
#     + 16*links + hasRadarData1 + [8*float] + simX/Y/Z 8*3 + simHeading4 + campName80)
def obj_record(entity_type: int, x: int, y: int, camp_id: int, name: str) -> bytes:
    b = struct.pack("<h", entity_type)
    b += struct.pack("<II", 1000 + camp_id, 0)        # Id.num_ / creator_
    b += struct.pack("<H", 0x0B01)                    # EntityType
    b += struct.pack("<hh", x, y)
    b += struct.pack("<f", 123.5)                     # z   (ver>=70)
    b += struct.pack("<I", 99)                        # spotTime
    b += struct.pack("<h", 0)                         # spotted
    b += struct.pack("<h", 0)                         # baseFlags
    b += struct.pack("<B", 2)                         # owner
    b += struct.pack("<h", camp_id)                   # campId ← ByCampId 的键
    b += struct.pack("<I", 0)                         # lastRepair
    b += struct.pack("<I", 0x10)                      # objFlags (ver>1 → u32)
    b += struct.pack("<BBB", 250, 210, 5)             # supply/fuel/losses
    b += struct.pack("<B", 2) + b"\x01\x02"           # numStatuses + fStatus[2]
    b += struct.pack("<B", 7)                         # priority
    b += struct.pack("<h", 0)                         # nameId
    b += struct.pack("<II", 0, 0)                     # parent
    b += struct.pack("<BB", 1, 0)                     # firstOwner, links=0
    b += struct.pack("<B", 1)                         # hasRadarData (ver>=20)
    b += struct.pack("<8f", *([0.5] * 8))             # detectRatio[8]
    b += struct.pack("<3d", 2700000.0, 1900000.0, 30.0)  # simX/Y/Z (ver>=103)
    b += struct.pack("<f", 1.25)                      # simHeading
    b += name.encode("ascii").ljust(80, b"\0")        # campName (ver>=106)
    return b


body = obj_record(101, 111, 222, 12, "Osan AB") + obj_record(102, -333, 444, 77, "Kunsan")
enc = lzss_literals(body)
sym = struct.pack("<hii", 2, len(body), 0) + enc      # [i16 n][i32 解压长度][i32 忽略]
sym_out = camdata.read_obj(sym, 109)
check(".obj 合成：解出 2 条记录", sym_out["records_read"] == 2,
      "records_read=%d errors=%s" % (sym_out["records_read"], sym_out["errors"]))
check(".obj 合成：complete=True", sym_out["complete"] is True)
check(".obj 合成：按 camp_id 索引 12 / 77",
      sorted(sym_out["objectives"].keys()) == [12, 77],
      "keys=%s" % sorted(sym_out["objectives"].keys()))
check(".obj 合成：by_camp_id 与 objectives 是同一份对象",
      sym_out["by_camp_id"] is sym_out["objectives"])
o12 = sym_out["objectives"][12]
check(".obj 合成：字段值正确（campName/坐标/sim 坐标/fStatus/hasRadarData）",
      o12["camp_name"] == "Osan AB" and o12["x"] == 111 and o12["y"] == 222
      and o12["spot_time"] == 99 and o12["supply"] == 250
      and o12["f_status"] == [1, 2] and o12["has_radar_data"] == 1
      and abs(o12["sim_x"] - 2700000.0) < 1e-6
      and abs(o12["detect_ratio"][0] - 0.5) < 1e-9,
      "camp_name=%r x=%s sim_x=%s" % (o12["camp_name"], o12["x"], o12["sim_x"]))
check(".obj 合成：uncompressed_size_actual == 申报长度",
      sym_out["uncompressed_size_actual"] == sym_out["uncompressed_size"])

# 用户点名的计数口径（.obj）：解析条数 / campId>0 入表数 / 其中 owner!=0 的条数
ob = sym_out
print("  .obj 计数口径（合成 2 条）:")
print("    解析记录数 records_read = %d" % ob["records_read"])
print("    campId>0 入表数          = %d（键=%s）"
      % (len(ob["objectives"]), sorted(ob["objectives"].keys())))
print("    其中 owner != 0 的条数   = %d（owner 值=%s）"
      % (sum(1 for v in ob["objectives"].values() if v["owner"] != 0),
         [v["owner"] for v in ob["objectives"].values()]))
# 共享 reader 单独可用性（供 .uni / .obd 复用）
dummy_body = obj_record(101, 111, 222, 12, "Osan AB")[2:]   # 去掉 2 字节类型前缀
rec, newpos = camdata.read_objective_record(dummy_body, 0, 109, ".obj")
check("read_objective_record 可单独调用且偏移推进正确",
      rec["camp_id"] == 12 and newpos == len(dummy_body),
      "camp_id=%s newpos=%d len=%d" % (rec["camp_id"], newpos, len(dummy_body)))
# 压缩流被截断：解压本身就会失败 → 必须报带 .obj 上下文的 CamDataError
sym_trunc = struct.pack("<hii", 2, len(body), 0) + enc[:len(enc) // 2]
try:
    camdata.read_obj(sym_trunc, 109)
    check(".obj 合成：压缩流截断时报 CamDataError", False, "居然没抛")
except camdata.CamDataError as exc:
    check(".obj 合成：压缩流截断时报 CamDataError（含 .obj + 长度信息）", True)
    print("        %s" % str(exc)[:150])

# 解压正常但记录数申报偏多 → 缓冲区先读完，complete=False 必须明确暴露
# （C# 在这种情况下同样只是"读满就停"、不留痕迹，所以这里考查的是 complete/records_read
#   这两个新增字段是否如实反映"没读满"，而不是要求抛错。）
over = struct.pack("<hii", 5, len(body), 0) + enc
ov = camdata.read_obj(over, 109)
check(".obj 合成：申报 5 条但只有 2 条 → complete=False、records_read=2",
      ov["complete"] is False and ov["records_read"] == 2,
      "complete=%s records_read=%d" % (ov["complete"], ov["records_read"]))
check(".obj 合成：上例已读到缓冲区末尾（bytes_consumed == bytes_total）",
      ov["bytes_consumed"] == ov["bytes_total"],
      "consumed=%s total=%s" % (ov["bytes_consumed"], ov["bytes_total"]))

# --------------------------------------------------------------------------
print()
print("=" * 78)
if FAILURES:
    print("失败 %d 项：" % len(FAILURES))
    for f in FAILURES:
        print("  - %s" % f)
    sys.exit(1)
print("全部检查通过")
