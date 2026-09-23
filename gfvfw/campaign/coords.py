"""战役坐标换算。

BMS 里存在**两套**坐标，且同一套坐标在不同文件里字段命名不一致——这是
战役解析最容易出错的地方，故集中在此模块并写清依据。

1. 战役网格（campaign grid）
   ------------------------
   ``0 .. 1023`` 的整数（目标点可以是小数）。原点在**西南角**，每格 1 km，
   整个剧场 1024 km。约定 **x = 东、y = 北**。

2. 世界坐标（world）
   ------------------
   部分文件存的是"英尺"为单位的偏移量，需除以 ``3280.84``（= 1 km 的英尺数）
   才是网格值。且这些文件里 BMS 把字段命名为 ``X``/``Y`` 时 **X 是北、Y 是东**，
   与网格约定的 x=东、y=北**相反**。

各来源的实际口径（均已用真实存档与地理坐标核对）
------------------------------------------------

====================================  ==========================================
来源                                   网格值算法
====================================  ==========================================
``CampObjData.xml`` 的
``PositionX``/``PositionY``（英尺）     ``east = PositionY / 3280.84``
                                        ``north = PositionX / 3280.84``
``.cmp`` 中队 ``SquadInfo.x/y``（英尺） ``east = y / 3280.84``
                                        ``north = x / 3280.84``
``.uni`` 单位记录（已是网格 int16）      ``east = x``，``north = y``
``.cmp`` 事件 ``EventNode``（网格 int16）``east = 第 1 个 int16``
                                        ``north = 第 2 个 int16``
====================================  ==========================================

核对依据（真实存档 Hellas，剧场中心 38.0N/25.0E，1024 km）：

* 目标点 ``campId=4`` "Andravida Airbase"：XML ``(1675571.2, 608236.2)`` 英尺
  → ``(east=185.39, north=510.71)``，与 CamReader 输出完全一致。
* 事件 "Greek air defenses fired on Turkish aircraft northwest of **Moudros**"
  位于 ``(east=530, north=723)``；Moudros（39.87N, 25.27E）反算应为
  ``(535, 720)`` —— 吻合，证明第 1 个 int16 是**东**。
* 地面单位 "14th Infantry" 的 ``attackObjId`` 指向 "**Cavuskoy**"
  ``(east=611.3, north=811.23)``，反算 40.69N/26.18E —— 正是土耳其色雷斯
  靠近希腊边境处，吻合。

⚠️ CamReader 自身有一处不一致：中队位置**没有**做英尺→网格换算，直接把
世界英尺当作网格输出（``JsonExporter.cs`` 第 289 行）。本实现予以修正，
统一换算成网格，否则中队会画到地图外。
"""
from __future__ import annotations

import math

__all__ = [
    "FEET_PER_KM", "GRID_SIZE", "EARTH_RADIUS_KM",
    "feet_to_grid", "world_xy_to_grid", "clamp_grid",
    "grid_to_latlon", "latlon_to_grid", "Projection",
]

#: 1 km 等于多少英尺（= 1000 / 0.3048）
FEET_PER_KM = 3280.84

#: 战役网格边长（0..1023）
GRID_SIZE = 1024

#: 求球面距离用
EARTH_RADIUS_KM = 6371.0088


def feet_to_grid(value_ft: float) -> float:
    """英尺 → 网格（1 格 = 1 km）。"""
    return value_ft / FEET_PER_KM


def world_xy_to_grid(position_x_ft: float, position_y_ft: float) -> tuple[float, float]:
    """``CampObjData.xml`` / 中队用的世界英尺坐标 → ``(east, north)`` 网格。

    ⚠️ 注意这里 BMS 的 ``X`` 是**北**、``Y`` 是**东**，故返回值顺序与入参相反。
    """
    return feet_to_grid(position_y_ft), feet_to_grid(position_x_ft)


def clamp_grid(value: float, *, expand: float = 0.0) -> float:
    """把网格值夹到 ``[0, GRID_SIZE)``，避免极端数据画出地图外。"""
    lo = -expand
    hi = GRID_SIZE + expand
    return max(lo, min(hi, value))


class Projection:
    """BMS 的横轴墨卡托（tmerc）投影，用于网格 ↔ 经纬度换算。

    参数取自 ``campaign_state.json`` 的 ``meta.projection``，例如 Hellas::

        +proj=tmerc +lon_0=25 +ellps=WGS84 +k=0.9996 +units=m
        +x_0=512000 +y_0=-3693820

    设计为纯 Python、无第三方依赖（项目要求不引入 pyproj）。
    """

    __slots__ = ("lon_0", "lat_0", "k_0", "x_0", "y_0", "a", "f", "size_km")

    def __init__(self, lon_0: float, lat_0: float = 0.0, k_0: float = 0.9996,
                 x_0: float = 0.0, y_0: float = 0.0,
                 a: float = 6378137.0, f: float = 1 / 298.257223563,
                 size_km: float = 1024.0):
        self.lon_0 = lon_0
        self.lat_0 = lat_0
        self.k_0 = k_0
        self.x_0 = x_0
        self.y_0 = y_0
        self.a = a
        self.f = f
        self.size_km = size_km

    @property
    def e2(self) -> float:
        return self.f * (2 - self.f)

    @classmethod
    def from_proj_string(cls, proj: str, *, size_km: float = 1024.0) -> "Projection":
        """解析 ``+proj=tmerc +lon_0=.. +k=.. +x_0=.. +y_0=..`` 形式的串。"""
        kw: dict[str, float] = {}
        for tok in proj.split():
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            try:
                kw[k.lstrip("+")] = float(v)
            except ValueError:
                continue
        return cls(lon_0=kw.get("lon_0", 0.0), lat_0=kw.get("lat_0", 0.0),
                   k_0=kw.get("k", kw.get("k_0", 0.9996)),
                   x_0=kw.get("x_0", 0.0), y_0=kw.get("y_0", 0.0),
                   a=kw.get("a", 6378137.0), f=1 / kw["rf"] if kw.get("rf") else
                   1 / 298.257223563,
                   size_km=size_km)

    # -- 正算 -----------------------------------------------------------

    def _forward_m(self, lat_deg: float, lon_deg: float) -> tuple[float, float]:
        a, e2, k0 = self.a, self.e2, self.k_0
        ep2 = e2 / (1 - e2)
        lat = math.radians(lat_deg)
        dlon = math.radians(lon_deg - self.lon_0)
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        T = math.tan(lat) ** 2
        C = ep2 * math.cos(lat) ** 2
        A = math.cos(lat) * dlon
        M = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat
                 - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat)
                 + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat)
                 - (35 * e2 ** 3 / 3072) * math.sin(6 * lat))
        x = k0 * N * (A + (1 - T + C) * A ** 3 / 6
                      + (5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * A ** 5 / 120) + self.x_0
        y = k0 * (M + N * math.tan(lat) * (A ** 2 / 2
                                           + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24
                                           + (61 - 58 * T + T ** 2 + 600 * C - 330 * ep2)
                                           * A ** 6 / 720)) + self.y_0
        return x, y

    # -- 反算 -----------------------------------------------------------

    def _inverse_m(self, x: float, y: float) -> tuple[float, float]:
        a, e2, k0 = self.a, self.e2, self.k_0
        ep2 = e2 / (1 - e2)
        M = (y - self.y_0) / k0
        mu = M / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
        e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
        phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
                + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
                + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
                + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
        C1 = ep2 * math.cos(phi1) ** 2
        T1 = math.tan(phi1) ** 2
        N1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
        R1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
        D = (x - self.x_0) / (N1 * k0)
        lat = phi1 - (N1 * math.tan(phi1) / R1) * (
            D ** 2 / 2
            - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2 - 9 * ep2) * D ** 4 / 24
            + (61 + 90 * T1 + 298 * C1 + 45 * T1 ** 2 - 252 * ep2 - 3 * C1 ** 2)
            * D ** 6 / 720)
        lon = (D - (1 + 2 * T1 + C1) * D ** 3 / 6
               + (5 - 2 * C1 + 28 * T1 - 3 * C1 ** 2 + 8 * ep2 + 24 * T1 ** 2)
               * D ** 5 / 120) / math.cos(phi1)
        return math.degrees(lat), math.degrees(lon) + self.lon_0

    # -- 网格接口 --------------------------------------------------------

    def grid_to_latlon(self, east: float, north: float) -> tuple[float, float]:
        """网格 ``(east, north)`` km → ``(lat, lon)`` 度。

        ⚠️ 网格就是**投影米 / 1000**，**不再**减 ``x_0``/``y_0`` —— 这两个偏移
        已经包含在投影结果里（``x_0=512000`` 正好让 ``lon_0`` 落在网格 512，
        即剧场中心）。实测：Andravida(185.39, 510.71) → 37.92N/21.29E 吻合。
        """
        return self._inverse_m(east * 1000.0, north * 1000.0)

    def latlon_to_grid(self, lat: float, lon: float) -> tuple[float, float]:
        """``(lat, lon)`` 度 → 网格 ``(east, north)`` km，是 :meth:`grid_to_latlon` 的逆。"""
        x, y = self._forward_m(lat, lon)
        return x / 1000.0, y / 1000.0


#: 常见剧场的投影参数（``campaign_state.json`` 里有权威值，这里只作缺省回退）
KNOWN_PROJECTIONS: dict[str, tuple[float, float]] = {
    # 剧场名（小写）: (lon_0, size_km)
    "korea": (127.0, 1024.0),
    "hellas": (25.0, 1024.0),
    "ikaros": (25.0, 1024.0),
    "israel": (35.0, 1024.0),
    "balkans": (20.0, 1024.0),
}
