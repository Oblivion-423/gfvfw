"""
ACMI 解析器测试 —— 基于真实 ACMI 文件与合成用例。

运行:
    python -m tests.test_acmi_parser
或:
    python tests/test_acmi_parser.py

设计原则（吸取本次开发教训）：
    1. 每个断言都用**真实文件**或**构造的确定性输入**，不靠肉眼读输出。
    2. 关键不变量（人驾计数、Pilot 提取、距离双路一致）必须显式断言。
    3. 失败时打印诊断上下文，避免"看到 0 却不知道为什么"。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gfvfw.acmi_parser import (  # noqa: E402
    AcmiParser, haversine_m, normalize_aircraft, parse_file, parse_filename_time,
    open_acmi_text, _ATTR_TAIL_FULL_RE, KV_RE,
)

REAL_DIR = r"G:\BMS\Falcon BMS 4.38\User\Acmi"

FAILURES: list[str] = []
CHECKS = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append("%s %s" % (name, detail))


# --------------------------------------------------------------------------
# 合成用例（确定性，不依赖磁盘）
# --------------------------------------------------------------------------

SYNTHETIC_ACMI = [
    "FileType=text/acmi/tacview\n",
    "FileVersion=2.1\n",
    "0,DataRecorder=Falcon BMS 4.38.1\n",
    "0,ReferenceTime=2024-8-16T00:00:00Z\n",
    "#0.0\n",
    # 人驾飞机：含 Pilot=，初始停在地面、速度为零
    # ⚠️ u/v 与 lon/lat 必须描述同一条直线飞行，否则距离双路校验会（正确地）报警。
    #    北向每 0.01° 约 1112 m，故 v 每步 +1112。
    "9,T=126.5|36.10|7.0|0|0|0|400000|250000|90,"
    "CallSign=Viper61,Coalition=ROK,CAS=0,Name=F-16CM-52,Pilot=TestPilot,Type=Air+FixedWing\n",
    # AI 飞机：同机型，但无 Pilot=
    "a,T=126.6|36.20|7.0|0|0|0|410000|251000|90,"
    "Coalition=ROK,CAS=0,Name=F-16CM-52,Type=Air+FixedWing\n",
    "#10.0\n",
    # 起飞爬升
    "9,T=126.5|36.11|6000|||0|400000|251112|,CAS=320,Mach=0.8\n",
    "#20.0\n",
    "9,T=126.5|36.12|6500|||0|400000|252224|,CAS=340,LongitudinalGForce=10.5\n",
    "#30.0\n",
    "9,T=126.5|36.13|6100|||0|400000|253336|,Event=Shot|AIM-120C\n",
    "#40.0\n",
    # 降落：末速为零
    "9,T=126.5|36.14|7.0|||0|400000|254448|,CAS=0,Mach=0\n",
]


#: ⚠️ **回归用例：时间标记不从 0 开始。**
#: 真实 BMS ACMI 的时间戳以剧本纪元（``ReferenceTime``）为基点，首个标记
#: 常常是个大数（实测 36000.2 = 整整 10 小时）。上面那份合成用例从 ``#0.0``
#: 开始，``min == 0``，于是"时长 = 末标记"这个错误**完全看不出来** ——
#: 这正是它长期漏网的原因。本用例把起始标记挪到 36000，时长仍必须算 40 秒。
SYNTHETIC_ACMI_OFFSET_START = [
    "FileType=text/acmi/tacview\n",
    "FileVersion=2.1\n",
    "0,DataRecorder=Falcon BMS 4.38.1\n",
    "0,ReferenceTime=2025-10-25T00:00:00Z\n",
    "#36000.0\n",
    "9,T=126.5|36.10|6000|||0|400000|250000|90,"
    "CallSign=Viper61,Coalition=ROK,CAS=320,Mach=0.8,"
    "Name=F-16CM-52,Pilot=TestPilot,Type=Air+FixedWing\n",
    "#36020.0\n",
    "9,T=126.5|36.12|6500|||0|400000|252224|,CAS=340,Mach=0.82\n",
    "#36040.0\n",
    "9,T=126.5|36.14|6100|||0|400000|254448|,CAS=0,Mach=0\n",
]


def test_offset_start_duration() -> None:
    """时间标记不从 0 开始时，录制时长仍须是「末 − 首」。"""
    print("\n[1b] 时间标记不从 0 开始（录制时长回归用例）")
    info = AcmiParser().parse_stream(
        SYNTHETIC_ACMI_OFFSET_START, path="2026-01-02_15-26-30.acmi")

    check("首标记 = 36000", abs(info.min_relative_seconds - 36000.0) < 0.01,
          "得到 %r" % info.min_relative_seconds)
    check("末标记 = 36040", abs(info.max_relative_seconds - 36040.0) < 0.01,
          "得到 %r" % info.max_relative_seconds)
    # ★ 核心断言：时长是 40 秒，不是 36040 秒
    check("录制时长 = 末 − 首 = 40 秒",
          abs(info.duration_seconds - 40.0) < 0.01,
          "得到 %r（旧算法会给 36040.0）" % info.duration_seconds)
    check("录制时长远小于末标记", info.duration_seconds < info.max_relative_seconds,
          "时长 %r 末标记 %r" % (info.duration_seconds, info.max_relative_seconds))

    # 时间窗宽度必须等于录制时长（t=0 基准点不属于时间窗）
    if info.recording_start_utc and info.recording_end_utc:
        width = (info.recording_end_utc - info.recording_start_utc).total_seconds()
        check("时间窗宽度 == 录制时长", abs(width - info.duration_seconds) < 0.01,
              "宽度 %r" % width)
        # 起点必须晚于 t=0 基准点（因为首标记 36000 > 0）
        check("录制起点晚于 t=0 基准点 36000 秒",
              abs((info.recording_start_utc - info.time_origin_utc).total_seconds()
                  - 36000.0) < 0.01)
    else:
        check("能算出录制起止时刻", False, "recording_start/end 为空")

    # 不该因为"末标记很大"就误报超时
    check("不误报时长超阈值",
          not any("超过" in w and "阈值" in w for w in info.warnings),
          "warnings=%s" % info.warnings)


def test_synthetic() -> None:
    print("\n[1] 合成用例（确定性）")
    p = AcmiParser()
    info = p.parse_stream(SYNTHETIC_ACMI, path="2026-01-02_03-04-05.acmi")

    check("带飞行员名对象数 == 1", info.objects_with_pilot_name == 1,
          "得到 %d" % info.objects_with_pilot_name)
    check("无飞行员名 AI 数 == 1", info.unnamed_ai_actors == 1,
          "得到 %d" % info.unnamed_ai_actors)
    check("架次数 == 1", len(info.sorties) == 1, "得到 %d" % len(info.sorties))
    if not info.sorties:
        return
    s = info.sorties[0]
    check("Pilot 提取正确", s.raw_pilot_name == "TestPilot",
          "得到 %r" % s.raw_pilot_name)
    check("CallSign 提取正确", s.tactical_callsign == "Viper61",
          "得到 %r" % s.tactical_callsign)
    check("机型归一化到 Block 52", s.aircraft_standard_name == "F-16C Block 52",
          "得到 %r" % s.aircraft_standard_name)
    check("机型已收录", s.aircraft_known)

    # ---- 起降判定（用户口径）----
    check("起飞次数 == 1（出现即计）", s.takeoff_count == 1,
          "得到 %d" % s.takeoff_count)
    check("确实升空过", s.took_off, "max_cas=%r" % s.max_cas_kts)
    check("末速为零", s.end_cas_kts == 0.0, "得到 %r" % s.end_cas_kts)
    check("降落次数 == 1", s.landing_count == 1, "得到 %d" % s.landing_count)
    check("已降落标记", s.landed)
    check("最大 CAS == 340", s.max_cas_kts == 340.0, "得到 %r" % s.max_cas_kts)
    check("最大海拔 == 6500", s.max_altitude_m == 6500.0,
          "得到 %r" % s.max_altitude_m)

    check("飞行时长 > 0", s.flight_seconds > 0, "得到 %.1f" % s.flight_seconds)
    check("距离 > 0", s.distance_meters > 0, "得到 %.1f" % s.distance_meters)
    check("武器发射 == 1", s.weapons_fired == 1, "得到 %d" % s.weapons_fired)
    check("过载超限计数 >= 1", s.exceedance_count >= 1,
          "得到 %d" % s.exceedance_count)
    check("最大过载 == 10.5", s.max_g == 10.5, "得到 %r" % s.max_g)

    # 时间锚点：文件名 03:04:05 − t_max(40s) = 03:03:25
    check("任务开始时间 = 文件名−t_max",
          info.mission_start_utc is not None
          and info.mission_start_utc.strftime("%H:%M:%S") == "03:03:25",
          "得到 %r" % info.mission_start_utc)
    # ---- 距离双路一致性 ----
    # 真实数据上两路偏差实测 0.0~0.1%（见 tests 输出），此处放宽到 5% 以容纳合成数据的舍入。
    if s.distance_meters and s.distance_meters_uv:
        lo, hi = sorted((s.distance_meters, s.distance_meters_uv))
        check("距离双路一致（<5%）", (hi - lo) / hi < 0.05,
              "geo=%.1f uv=%.1f" % (s.distance_meters, s.distance_meters_uv))
        check("航程量级合理（1~20 km）",
              1000 < s.distance_meters < 20000,
              "得到 %.0f" % s.distance_meters)


def test_helpers() -> None:
    print("\n[2] 辅助函数")
    check("haversine 1 度纬度 ≈ 111.2 km",
          abs(haversine_m(0, 0, 0, 1) - 111194.9) < 200,
          "得到 %.1f" % haversine_m(0, 0, 0, 1))
    std, known = normalize_aircraft("F-16CM-52")
    check("归一化 F-16CM-52", std == "F-16C Block 52" and known,
          "得到 %r %r" % (std, known))
    std, known = normalize_aircraft("不存在的机型")
    check("未收录机型返回 None", std is None and not known,
          "得到 %r %r" % (std, known))
    dt = parse_filename_time(r"C:\x\2026-08-04_10-27-51.zip.acmi")
    check("文件名时间解析",
          dt is not None and dt.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-04 10:27:51",
          "得到 %r" % dt)


def test_boundary_extraction() -> None:
    """T= 值含逗号时，属性区起点必须正确识别。"""
    print("\n[3] T= 与属性区边界识别")
    line = ("9,T=26.409252|40.544404|579.12|0|3.9|124.6|631334.5|795291.49|117.6,"
            "AOA=3.9,CAS=174,CallSign=Tiger41,Name=F-16C B52M HAF,"
            "Pilot=Oblivion,Type=Air+FixedWing")
    oid, rest = line.split(",", 1)
    after = rest[2:]
    m = _ATTR_TAIL_FULL_RE.search(after)
    check("识别到属性区", m is not None)
    body = after[m.start() + 1:] if m else ""
    kvs = dict(KV_RE.findall(body))
    check("属性区含 Name", kvs.get("Name") == "F-16C B52M HAF",
          "得到 %r" % kvs.get("Name"))
    check("属性区含 Pilot", kvs.get("Pilot") == "Oblivion",
          "得到 %r" % kvs.get("Pilot"))
    check("属性区含 Type", kvs.get("Type") == "Air+FixedWing",
          "得到 %r" % kvs.get("Type"))
    check("T= 保留 9 个分量", len(after[:m.start()].split("|")) == 9,
          "得到 %d" % len(after[:m.start()].split("|")))

    # 逗号分隔的 T= 形式
    line2 = ("9,T=126.5,36.1,6000,0,0,0,400000,250000,90,"
             "Name=F-16CM-52,Pilot=CommaForm,Type=Air+FixedWing")
    oid2, rest2 = line2.split(",", 1)
    after2 = rest2[2:]
    m2 = _ATTR_TAIL_FULL_RE.search(after2)
    body2 = after2[m2.start() + 1:] if m2 else ""
    kvs2 = dict(KV_RE.findall(body2))
    check("逗号形式也能提取 Pilot", kvs2.get("Pilot") == "CommaForm",
          "得到 %r" % kvs2.get("Pilot"))


# --------------------------------------------------------------------------
# 真实文件用例
# --------------------------------------------------------------------------

def real_files(limit: int | None = None) -> list[str]:
    if not os.path.isdir(REAL_DIR):
        return []
    fs = [os.path.join(REAL_DIR, n) for n in os.listdir(REAL_DIR)
          if n.lower().endswith(".acmi")]
    fs.sort(key=lambda p: os.path.getsize(p))
    return fs[:limit] if limit else fs


def truth_human_count(path: str) -> int:
    """独立实现：直接对解压后文本做 Pilot= 计数（不依赖解析器的边界逻辑）。"""
    with open(path, "rb") as fh:
        if fh.read(2) != b"PK":
            raw = open(path, "r", encoding="utf-8", errors="replace").read()
        else:
            raw = zipfile.ZipFile(path).open("acmi.txt").read().decode("utf-8", "replace")
    n = 0
    for line in raw.splitlines():
        parts = line.split(",")
        if not parts or not parts[0] or not all(c in "0123456789abcdefABCDEF" for c in parts[0]):
            continue
        if "Pilot=" in line and "Type=Air" in line:
            n += 1
    return n


def test_real_files(limit: int = 6) -> None:
    print("\n[4] 真实 ACMI 文件（最小 %d 个）" % limit)
    files = real_files()
    if not files:
        print("  跳过：未找到真实 ACMI 目录")
        return
    pending: set[str] = set()
    for p in files[:limit]:
        info = parse_file(p)
        name = os.path.basename(p)
        truth = truth_human_count(p)
        check("%s 带名对象数与真值一致" % name,
              info.objects_with_pilot_name == truth,
              "解析=%d 真值=%d" % (info.objects_with_pilot_name, truth))
        if truth:
            check("%s 生成了架次" % name, len(info.sorties) >= 1,
                  "得到 %d" % len(info.sorties))
            for s in info.sorties:
                check("%s 飞行员名非空" % name, bool(s.raw_pilot_name))
                # 注意：非联队机型（如 MiG-17PF）出现属正常，
                # 记为"待补映射"而非失败 —— 这正是需要人确认的信号。
                if s.aircraft_standard_name is None:
                    pending.add(s.aircraft_raw_name or "?")
    if pending:
        print("  待补映射机型（属正常，需人工确认是否联队机型）: %s"
              % ", ".join(sorted(pending)))


def test_large_file(limit_mb: int = 20) -> None:
    print("\n[5] 大文件流式处理（≥%d MB）" % limit_mb)
    cands = [p for p in real_files() if True]
    big = [p for p in cands if os.path.getsize(p) >= limit_mb * 1024 * 1024]
    if not big:
        print("  跳过：无足够大的样本")
        return
    p = max(big, key=os.path.getsize)
    import time
    t0 = time.time()
    info = parse_file(p)
    dt = time.time() - t0
    print("  %s  %.1f MB  %d 行  %.1f 秒  带名=%d  无名AI=%d  架次=%d"
          % (os.path.basename(p), os.path.getsize(p) / 1048576.0,
             info.line_count, dt, info.objects_with_pilot_name,
             info.unnamed_ai_actors, len(info.sorties)))
    check("大文件解析成功", info.line_count > 0)
    check("大文件识别出带名对象", info.objects_with_pilot_name >= 1,
          "得到 %d" % info.objects_with_pilot_name)
    check("大文件生成了架次", len(info.sorties) >= 1)


def test_real_distance_consistency() -> None:
    """真实数据上双路距离必须高度一致 —— 这是对 T= 分量解读的关键校验。"""
    print("\n[6] 真实数据：双路距离一致性（校验 T= 分量解读）")
    files = [p for p in real_files() if os.path.getsize(p) > 1_000_000]
    files.sort(key=os.path.getsize, reverse=True)
    if not files:
        print("  跳过：无足够大的样本")
        return
    worst = 0.0
    n = 0
    for p in files[:3]:
        info = parse_file(p)
        for s in info.sorties:
            if s.distance_meters > 10000 and s.distance_meters_uv > 10000:
                lo, hi = sorted((s.distance_meters, s.distance_meters_uv))
                dev = (hi - lo) / hi * 100.0
                worst = max(worst, dev)
                n += 1
    check("真实数据双路偏差 < 1%%（实测样本 %d 条）" % n, n > 0 and worst < 1.0,
          "最大偏差 %.2f%%" % worst)


def main() -> int:
    test_synthetic()
    test_offset_start_duration()
    test_helpers()
    test_boundary_extraction()
    test_real_files()
    test_large_file()
    test_real_distance_consistency()

    print("\n" + "=" * 62)
    print("断言总数 %d，失败 %d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  FAILED:", f)
    print("=" * 62)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
