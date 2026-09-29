"""Tacview XML 战斗分析器（内置自 TacviewLogAnalyzer 0.1.6，MIT 许可）。

来源：https://github.com/oakdesign/TacviewLogAnalyzer
（仓库根 ``TacviewLogAnalyzer-0.1.6.zip``，2026-09 引入；LICENSE 见本目录）。

它分析的是 **Tacview 的「Export Flight Log」XML 导出**（用户在 Tacview 里
打开 ``.acmi`` → File → Export Flight Log 得到的 ``.xml``），**不是** ``.acmi``
本体 —— 两者是同一录像的两种表达，XML 里带结构化事件
（谁开火 / 命中了谁 / 摧毁了谁 / 起飞降落），因此能做 ``.acmi`` 侧做不了的
**击杀归属**与命中链分析。

模块划分（与上游一致，便于日后同步更新）：
* :mod:`.models`    数据模型（Action 枚举、EventRecord 等）
* :mod:`.parser`    XML 解析（``xml.etree``，纯标准库）
* :mod:`.linking`   事件链关联：确定性（weapon_id/target_id）+ 启发式
                    （炸弹连投），含拦截检测与区域杀伤归属
* :mod:`.stats`     飞行员统计（起降/弹射/被击落、A-A 按目标机型分组）
* :mod:`.viewmodel` 面向展示的树状视图（本站模板直接消费它的 JSON）

⚠️ **刻意不内置**上游的 ``cli.py`` / ``webapp.py`` / ``webui/``：
入口与页面已并入本站（任务详情页的「Tacview 战斗分析」区块与
``/tacview/{id}`` 分析页，见 :mod:`gfvfw.web.routers.tacview`），
站内再造一套独立 Web 服务只会多一个要维护的进程。
上游核心代码**未做修改**（逐字节拷贝），改动请尽量以
「升级上游版本 + 站内适配层」的方式进行，方便比对差异。
"""

from .models import Action, EventRecord, TacviewDebriefing  # noqa: F401
from .parser import parse_file as parse_xml_file             # noqa: F401
from .viewmodel import build_pilot_view_model                # noqa: F401

__all__ = ["Action", "EventRecord", "TacviewDebriefing",
           "parse_xml_file", "build_pilot_view_model"]
