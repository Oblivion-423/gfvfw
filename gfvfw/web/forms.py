"""
表单输入解析：把用户填的**展示时区（UTC+8）**文本转成**存储用的 UTC**。

为什么要单独一个模块
--------------------
本项目铁律：**存储一律 UTC，展示一律 UTC+8**（需求 §7.3）。
表单是展示层的一部分，所以：

* 表单回填（GET）要把库里的 UTC 转成 UTC+8 再塞进 ``<input>``；
* 表单提交（POST）要把用户填的 UTC+8 当成本地时间，再转回 UTC 存库。

⚠️ 如果哪一步漏了，时间就会**整整差 8 小时**，而且看起来"像个正常时间"，
极难发现 —— 所以这两个方向都只在这里实现一次，路由不得自己写。

⚠️ 另外：``datetime-local`` 输入框提交的字符串**没有时区**，
必须显式按 UTC+8 解释，不能交给 ``datetime.fromisoformat`` 就完事
（那会得到一个 naive 时间，写库后语义不明）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from .templating import DISPLAY_TZ, to_display_tz


class FieldError(ValueError):
    """表单字段校验失败。路由把它转成 400 或回填页面。"""


def parse_local_datetime(value: Optional[str],
                         field: str = "时间") -> Optional[datetime]:
    """``2026-08-08T21:02`` / ``2026-08-08 21:02`` → 带时区的 UTC datetime。

    接受 ``datetime-local`` 的 ``T`` 分隔形式，也接受空格分隔与
    带秒的形式。空串返回 ``None``（表示"清空该字段"）。
    """
    if value is None:
        return None
    text = value.strip().replace(" ", "T")
    if not text:
        return None
    # 去掉可能带的 Z / 偏移，一律按展示时区解释 —— 表单里不出现时区选择
    text = text.rstrip("Z")
    if "+" in text:
        text = text.split("+", 1)[0]
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        raise FieldError("%s格式不对（应为 2026-08-08T21:02）：%r" % (field, value))
    return naive.replace(tzinfo=DISPLAY_TZ).astimezone(timezone.utc)


def local_input_value(dt: Optional[datetime]) -> str:
    """库里的 UTC → ``<input type="datetime-local">`` 需要的字符串。"""
    d = to_display_tz(dt)
    return d.strftime("%Y-%m-%dT%H:%M") if d else ""


def parse_local_date(value: Optional[str],
                     field: str = "日期") -> Optional[datetime]:
    """``2026-08-08`` → 该日 UTC+8 零点对应的 UTC 时刻。

    与 :func:`members.parse_date` 不同：那个把日期当成 **UTC 零点**，
    这里把它当成**展示时区的零点**。新增代码请用这个 —— 用户填"8 号"，
    意思是当地 8 号，不是 UTC 8 号。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise FieldError("%s格式不对（应为 2026-08-08）：%r" % (field, value))
    return datetime(d.year, d.month, d.day, tzinfo=DISPLAY_TZ).astimezone(
        timezone.utc)


def parse_int(value: Optional[str], field: str = "数值",
              default: int = 0, minimum: Optional[int] = None,
              maximum: Optional[int] = None) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(float(value.strip()))
    except (TypeError, ValueError):
        raise FieldError("%s必须是整数：%r" % (field, value))
    if minimum is not None and n < minimum:
        raise FieldError("%s不能小于 %d" % (field, minimum))
    if maximum is not None and n > maximum:
        raise FieldError("%s不能大于 %d" % (field, maximum))
    return n


def parse_float(value: Optional[str], field: str = "数值",
                default: Optional[float] = None,
                minimum: Optional[float] = None) -> Optional[float]:
    if value is None or value.strip() == "":
        return default
    try:
        x = float(value.strip())
    except (TypeError, ValueError):
        raise FieldError("%s必须是数字：%r" % (field, value))
    if minimum is not None and x < minimum:
        raise FieldError("%s不能小于 %g" % (field, minimum))
    return x


def parse_bool(value: Optional[str]) -> bool:
    """复选框：任何非空值视为勾选。"""
    return bool(value and value.strip())
