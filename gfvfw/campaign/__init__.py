"""BMS ``.cam`` 战役存档解析。

本包是 `CamReader`（C#）的 Python 移植。CamReader 是 Falcon BMS 战役存档的
命令行解析器，产出 ``campaign_state.json`` 供态势地图使用。

`.cam` 结构
-----------
    [uint32 目录偏移][ ... 内嵌文件数据 ... ][目录]
    目录: [uint32 文件数][ (uint8 名长, 名, uint32 偏移, uint32 长度) * n ]

内嵌文件为 ``.cmp`` ``.obd`` ``.uni`` ``.tea`` ``.evt`` ``.plt`` ``.pst``
``.pol`` ``.ver``，各自多为 LZSS 压缩，头部记录解压后长度。

BMS 安装目录的剧场数据（类表、单位/载具/武器/特征库、目标数据）用于把
``.uni`` 的字节流还原成有名字、有属性的单位与目标点，因此解析战役必须能
读到 BMS 安装的 ``Data`` 目录（或对应 Add-On 剧场目录）。

模块划分
--------
* :mod:`gfvfw.campaign.lzss`   —— LZSS 解压 + 两种带长度头的展开
* :mod:`gfvfw.campaign.bundle` —— ``.cam`` 容器目录
* :mod:`gfvfw.campaign.cmpfile`—— ``.cmp`` 头部（战役元数据、队伍、中队）
* :mod:`gfvfw.campaign.camdata`—— ``.obj/.tea/.evt/.pol/.pst``
* :mod:`gfvfw.campaign.theater`—— BMS 剧场数据表（XML）
* :mod:`gfvfw.campaign.units`  —— ``.uni`` 单位流
* :mod:`gfvfw.campaign.state`  —— 组装完整战役态势
"""
