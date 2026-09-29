"""ACMI → Tacview「Export Flight Log」XML 转换器。

为什么需要这个模块
------------------
战斗分析器（:mod:`gfvfw.tacview_analyzer`）吃的是 Tacview
「File → Export Flight Log」导出的 **XML 事件文件**。但让每个队员在
Tacview 里手动导出太麻烦 —— 本模块把 ``.acmi`` 录像**直接转换**成等价的
XML 事件流，上传后一步完成分析。

⚠️ 关键事实（2026-09 对真实 BMS 4.38 录像逐一验证）：
BMS 写出的 ACMI 里**没有** Shot/Hit/Destroyed 事件 —— 只有
``Event=LeftArea``（对象移出）与少量 ``Event=Message``。Tacview 导出 XML
时的战斗事件全部是它**从轨迹数据推断**出来的。本模块用同样的思路重建推断，
所有阈值都以真实录像实测分布为依据：

* **开火（HasFired）** = 武器对象（``Weapon+Missile/Bomb/Rocket``）首次出现。
  射手 = 首现位置附近（≤300m）**同阵营**、有 ``Pilot=`` 的在空飞机
  （实测武器首现位置离射手几十米；地面发射的 SAM/反坦克导弹附近没有
  同阵营飞机 → 无射手 → 不进统计，正确）。
* **命中（HasBeenHitBy）** = ``Misc+Explosion*`` 爆炸对象（BMS 在弹药起爆时
  生成，实测 31 枚武器 ↔ 31 个爆炸、时间差 ≈0）+ 附近 ≤300m 内**已死亡**的
  同一枚武器 → 目标 = 爆炸点 ≤150m 内最近的 Air/Sea/Ground 对象。
  无目标 → 脱靶（不产生事件）。
* **摧毁（HasBeenDestroyed）** = 命中后目标**再无位置更新**（静止单位只有
  创建行、被毁后不再出现；在空飞机若存活会继续被更新）→ 有射手即记击杀；
  另外在空**有人驾驶**飞机中途消失（消失时 CAS>30）→ 记被击落（无归属）。
* **起飞/降落** = 有人飞机 CAS 的滞回越限（≥80kt 离地 / ≤30kt 接地）。

口径说明：推断结果与 Tacview 自家导出**可能有个别出入**（尤其编队间距
<300m 时的射手归属、集束弹药 150m 内"命中即摧毁"的近似）——页面标注
"推断口径"即指此。gun 弹壳（``Projectile+Shell``）不生成开火事件。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from .acmi_parser import KV_RE, OBJ_ID_RE, TS_RE, _ATTR_TAIL_FULL_RE, open_acmi_text

#: 武器首现位置 → 射手的最大距离（米）。实测几十米；编队间距过近时可能
#: 归到僚机头上 —— 这是无 Parent 数据下的推断极限。
SPAWN_RADIUS_M = 300.0

#: 爆炸点 → 命中目标的最大距离（米）。实测直击 1~20m，集束弹药 ~130m。
HIT_RADIUS_M = 150.0

#: 爆炸 → 配对武器的距离上限（米）。实测 ≤160m，集束散布到 ~260m。
EXPLOSION_WEAPON_RADIUS_M = 300.0

#: 爆炸时刻 → 武器最后出现时刻的时间窗（秒）。实测 |dt| ≤ 2s。
EXPLOSION_WEAPON_WINDOW_S = 6.0

#: 命中后目标再无更新的判定窗（秒）。
DESTROYED_QUIET_S = 5.0

#: 有人飞机离地 / 接地的 CAS 阈值（节）。地面滑行实测在 0/1kt 抖动。
AIRBORNE_CAS_KT = 80.0
GROUNDED_CAS_KT = 30.0

#: 武器类别（Type 的 ``+`` 后缀部分）。
_WEAPON_KINDS = ("Missile", "Bomb", "Rocket")
#: 不参与目标判定的辅助类别。
_IGNORED_PREFIXES = ("Weapon+", "Misc+", "Navaid+", "Projectile+")


def _maybe_unquote(v: str) -> str:
    """BMS 写 ACMI 文本属性不做 URL 编码，但按规范遇到 %xx 时应解码。"""
    return unquote(v) if "%" in v else v


class _Obj:
    __slots__ = ("oid", "type", "name", "pilot", "coalition", "locked",
                 "lon", "lat", "alt", "cas", "first", "last", "airborne",
                 "takeoff_emitted", "shooter_obj")

    def __init__(self, oid: str) -> None:
        self.oid = oid
        self.type = self.name = self.pilot = self.coalition = None
        self.locked: str | None = None
        self.lon: float | None = None
        self.lat: float | None = None
        self.alt: float | None = None
        self.cas: float | None = None
        self.first: float | None = None
        self.last: float | None = None
        self.airborne = False
        self.takeoff_emitted = False
        self.shooter_obj: _Obj | None = None

    @property
    def kind(self) -> str | None:
        """Type 的主类别（``Air+FixedWing`` → ``Air``）。"""
        return self.type.split("+", 1)[0] if self.type else None

    @property
    def is_weapon(self) -> bool:
        return (self.type or "").startswith("Weapon+") and \
            self.type.split("+", 1)[-1] in _WEAPON_KINDS

    @property
    def is_targetable(self) -> bool:
        """能否被爆炸点判为命中目标（排除武器/诱饵/爆炸/弹壳等）。"""
        return bool(self.kind) and not self.type.startswith(_IGNORED_PREFIXES)


def _hav(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def convert_acmi_to_debriefing_xml(path: str, *, title: str = "") -> tuple[str, dict]:
    """把 ``.acmi``（裸文本或 ZIP 容器）转换为 Tacview 导出 XML。

    返回 ``(xml_text, info)``；``info`` 携带推断统计（shots/hits/kills/…），
    供调用方记录日志或核对。解析失败抛异常（调用方决定如何呈现）。
    """
    objs: dict[str, _Obj] = {}
    events: list[dict] = []
    weapons: list[_Obj] = []
    explosions: list[_Obj] = []
    pending_air_kills: list[dict] = []          # 空中目标的击杀待确认
    glob = {"recorder": None, "source": None, "reference_time": None}
    cur_ts = 0.0
    min_ts: float | None = None
    max_ts = 0.0

    # ------------------------------------------------------------------ 流式扫描
    lines, _container, _inner = open_acmi_text(path)
    try:
        for raw in lines:
            line = raw.rstrip("\r\n").lstrip("\ufeff")
            if not line:
                continue
            m = TS_RE.match(line)
            if m:
                try:
                    cur_ts = float(m.group(1))
                except ValueError:
                    continue
                if min_ts is None or cur_ts < min_ts:
                    min_ts = cur_ts
                if cur_ts > max_ts:
                    max_ts = cur_ts
                continue

            if "," not in line:
                k, _, v = line.partition("=")
                if k in ("DataRecorder", "DataSource", "ReferenceTime"):
                    glob[k[0].lower() + k[1:]] = v.strip()
                continue

            oid, rest = line.split(",", 1)
            if oid == "0":
                # 全局对象携带的头部属性（实测 BMS 把这些写在 0 号对象上）
                for k, v in KV_RE.findall(rest):
                    if k == "DataRecorder":
                        glob["recorder"] = _maybe_unquote(v)
                    elif k == "DataSource":
                        glob["source"] = _maybe_unquote(v)
                    elif k == "ReferenceTime":
                        glob["reference_time"] = _maybe_unquote(v)
                continue
            if not OBJ_ID_RE.match(oid):
                continue

            st = objs.get(oid)
            if st is None:
                st = _Obj(oid)
                objs[oid] = st

            body = rest
            if rest.startswith("T="):
                after = rest[2:]
                m2 = _ATTR_TAIL_FULL_RE.search(after)
                if m2:
                    tstr, body = after[:m2.start()], after[m2.start() + 1:]
                else:
                    tstr, body = after, ""
                tparts = [p.strip() for p in
                          (tstr.split("|") if "|" in tstr else tstr.split(","))]
                try:
                    if tparts[0]:
                        st.lon = float(tparts[0])
                    if len(tparts) > 1 and tparts[1]:
                        st.lat = float(tparts[1])
                    if len(tparts) > 2 and tparts[2]:
                        st.alt = float(tparts[2])
                except ValueError:
                    pass

            if st.first is None:
                st.first = cur_ts
            st.last = cur_ts

            kvs = dict(KV_RE.findall(body)) if body else {}
            if "Type" in kvs:
                st.type = _maybe_unquote(kvs["Type"]) or None
            if "Name" in kvs:
                st.name = _maybe_unquote(kvs["Name"]) or None
            if "Pilot" in kvs:
                st.pilot = _maybe_unquote(kvs["Pilot"]) or None
            if "Coalition" in kvs:
                st.coalition = _maybe_unquote(kvs["Coalition"]) or None
            if "LockedTarget" in kvs and kvs["LockedTarget"] not in ("", "0"):
                st.locked = kvs["LockedTarget"]
            for key in ("CAS", "IAS"):
                if key in kvs:
                    try:
                        st.cas = float(kvs[key])
                    except ValueError:
                        pass

            if st.is_weapon and st.first == cur_ts:
                weapons.append(st)
                _attribute_shot(st, objs, events, cur_ts)
            if (st.type and st.type.startswith("Misc+Explosion")
                    and st.first == cur_ts):
                explosions.append(st)
            # 有人飞机：进场 / 起飞 / 降落
            if st.pilot and st.kind == "Air":
                if st.first == cur_ts:
                    events.append(_evt(cur_ts, "HasEnteredTheArea", st, loc=st))
                if not st.airborne and (st.cas or 0) >= AIRBORNE_CAS_KT:
                    st.airborne = True
                    if not st.takeoff_emitted:
                        st.takeoff_emitted = True
                        events.append(_evt(cur_ts, "HasTakenOff", st, loc=st))
                elif st.airborne and (st.cas or 0) <= GROUNDED_CAS_KT:
                    st.airborne = False
                    events.append(_evt(cur_ts, "HasLanded", st, loc=st))
    finally:
        # open_acmi_text 返回的迭代器持有 ZIP 句柄，需显式关闭
        close = getattr(lines, "close", None)
        if close:
            close()

    if not objs and not weapons:
        raise ValueError("未读到任何 ACMI 对象 —— 文件可能不是 Tacview ACMI 格式")

    # ------------------------------------------------------------------ 爆炸 → 命中/摧毁
    info = {"shots": 0, "hits": 0, "kills": 0, "explosions": len(explosions),
            "objects": len(objs)}
    for e in explosions:
        if e.lon is None or e.lat is None:
            continue
        w = _weapon_for_explosion(e, weapons)
        if w is None or w.shooter_obj is None:      # 配不上武器或无射手 → 跳过
            continue
        victim = _victim_for_explosion(e, objs, exclude={w.oid, w.shooter_obj.oid})
        if victim is None:
            continue                                 # 爆炸附近没有目标 → 脱靶
        events.append(_evt(e.first, "HasBeenHitBy", victim,
                           secondary=w, parent=w.shooter_obj, loc=e))
        info["hits"] += 1
        # 摧毁判定：命中后目标再无更新（空中目标延后到扫描结束确认）
        if victim.kind == "Air":
            pending_air_kills.append({"victim": victim, "t": e.first,
                                      "killer": w.shooter_obj})
        else:
            events.append(_evt(e.first, "HasBeenDestroyed", victim,
                               secondary=w.shooter_obj, loc=victim))
            info["kills"] += 1

    # ------------------------------------------------------------------ 收尾
    for pend in pending_air_kills:
        v = pend["victim"]
        if v.last is not None and v.last <= pend["t"] + DESTROYED_QUIET_S:
            events.append(_evt(pend["t"], "HasBeenDestroyed", v,
                               secondary=pend["killer"], loc=v))
            info["kills"] += 1
    # 在空有人飞机中途消失（未降落）→ 被击落（无归属）
    if min_ts is not None:
        for st in objs.values():
            if (st.pilot and st.kind == "Air" and st.last is not None
                    and st.last < max_ts - 60.0 and st.airborne
                    and st.cas is not None and st.cas > GROUNDED_CAS_KT):
                events.append(_evt(st.last, "HasBeenDestroyed", st, loc=st))

    info["shots"] = sum(1 for e in events if e["action"] == "HasFired")
    events.sort(key=lambda e: e["time"])
    return _render_xml(events, objs, glob, min_ts or 0.0, max_ts,
                       title=title), info | {"events": len(events)}


# --------------------------------------------------------------------------


def _attribute_shot(w: _Obj, objs: dict[str, _Obj], events: list[dict],
                    ts: float) -> None:
    """武器首现 → 归属射手（同阵营在空有人飞机，最近者优先）。"""
    if w.lon is None or w.lat is None:
        return
    best: tuple[float, _Obj] | None = None
    for o in objs.values():
        if (o.pilot and o.kind == "Air" and o.coalition == w.coalition
                and o.oid != w.oid and o.lon is not None):
            d = _hav(w.lon, w.lat, o.lon, o.lat)
            if d <= SPAWN_RADIUS_M and (best is None or d < best[0]):
                best = (d, o)
    if best is None:
        return
    w.shooter_obj = best[1]
    locked = objs.get(w.locked) if w.locked else None
    events.append(_evt(ts, "HasFired", best[1], secondary=w,
                       locked=locked, loc=w))


def _weapon_for_explosion(e: _Obj, weapons: list[_Obj]) -> _Obj | None:
    best: tuple[float, float, _Obj] | None = None   # (dt, dist, weapon)
    for w in weapons:
        if w.lon is None or w.last is None or e.first is None:
            continue
        dt = e.first - w.last
        if abs(dt) > EXPLOSION_WEAPON_WINDOW_S:
            continue
        d = _hav(e.lon, e.lat, w.lon, w.lat)
        if d <= EXPLOSION_WEAPON_RADIUS_M and (best is None or (abs(dt), d) < (abs(best[0]), best[1])):
            best = (dt, d, w)
    return best[2] if best else None


def _victim_for_explosion(e: _Obj, objs: dict[str, _Obj],
                          exclude: set[str]) -> _Obj | None:
    best: tuple[float, _Obj] | None = None
    for o in objs.values():
        if o.oid in exclude or not o.is_targetable or o.lon is None:
            continue
        d = _hav(e.lon, e.lat, o.lon, o.lat)
        if d <= HIT_RADIUS_M and (best is None or d < best[0]):
            best = (d, o)
    return best[1] if best else None


def _evt(time: float, action: str, primary: _Obj, *,
         secondary: _Obj | None = None, parent: _Obj | None = None,
         locked: _Obj | None = None, loc: _Obj | None = None) -> dict:
    return {"time": time or 0.0, "action": action, "primary": primary,
            "secondary": secondary, "parent": parent, "locked": locked,
            "loc": loc}


def _obj_xml(tag: str, o: _Obj) -> str:
    attrs = []
    try:
        attrs.append('ID="%d"' % int(o.oid, 16))
    except ValueError:
        return ""
    inner = []
    if o.kind:
        inner.append("<Type>%s</Type>" % _esc(o.type))
    if o.name:
        inner.append("<Name>%s</Name>" % _esc(o.name))
    if o.coalition:
        inner.append("<Coalition>%s</Coalition>" % _esc(o.coalition))
    if o.pilot:
        inner.append("<Pilot>%s</Pilot>" % _esc(o.pilot))
    if not inner:
        return ""
    return "<%s %s>%s</%s>" % (tag, " ".join(attrs), "".join(inner), tag)


def _esc(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _loc_xml(o: _Obj | None) -> str:
    if o is None or o.lon is None or o.lat is None:
        return ""
    alt = o.alt if o.alt is not None else 0.0
    return ("<Location><Longitude>%.6f</Longitude><Latitude>%.6f</Latitude>"
            "<Altitude>%.2f</Altitude></Location>" % (o.lon, o.lat, alt))


def _render_xml(events: list[dict], objs: dict[str, _Obj], glob: dict,
                min_ts: float, max_ts: float, title: str) -> str:
    out = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>']
    out.append('<TacviewDebriefing Version="1.0">')
    out.append("<FlightRecording>")
    out.append("<Source>%s</Source>" % _esc(glob.get("source") or "Falcon 4.0"))
    out.append("<Recorder>%s</Recorder>" % _esc(
        glob.get("recorder") or "GFVFW ACMI 转换"))
    out.append("</FlightRecording>")
    out.append("<Mission>")
    out.append("<Title>%s</Title>" % _esc(title or "converted-acmi"))
    if glob.get("reference_time"):
        out.append("<MissionTime>%s</MissionTime>" % _esc(glob["reference_time"]))
    out.append("<Duration>%.2f</Duration>" % max(0.0, max_ts - min_ts))
    out.append("</Mission>")
    out.append("<Events>")
    for e in events:
        parts = ["<Event>", "<Time>%.2f</Time>" % (e["time"] - min_ts)]
        loc = _loc_xml(e["loc"] or e["primary"])
        if loc:
            parts.append(loc)
        parts.append(_obj_xml("PrimaryObject", e["primary"]))
        parts.append("<Action>%s</Action>" % e["action"])
        if e.get("secondary"):
            parts.append(_obj_xml("SecondaryObject", e["secondary"]))
        if e.get("parent"):
            parts.append(_obj_xml("ParentObject", e["parent"]))
        if e.get("locked"):
            parts.append(_obj_xml("LockedObject", e["locked"]))
        parts.append("</Event>")
        out.append("".join(parts))
    out.append("</Events>")
    out.append("</TacviewDebriefing>")
    return "\n".join(out)
