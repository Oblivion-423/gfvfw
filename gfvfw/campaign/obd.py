"""``.obd`` 解析（目标点增量）。

移植自 ``CamReader/Parsers/ObdFile.cs``。

``.obd`` 存的是**目标点状态增量**：每个目标点的占有方、补给、燃油、损失与
各子部件（feature）状态。它是目标点占有关系的**最高优先**来源——CamReader
的导出顺序是 ``.obd 增量 > .obj 记录 > .uni Objective 记录``。

记录布局（**版本门控**，见 ``ObdFile.cs`` 第 25-37 行）
------------------------------------------------------
::

    uint32  Id.num_        ← 这一项**就是**目标点的 CampId
    uint32  Id.creator_
    uint32  LastRepair
    uint8   Owner
    uint8   Supply
    uint8   Fuel
    uint8   Losses
    uint8   NumFStatus
    <FStatus>              见下
    [ver 83..99 追加]      跳过 3、再读 1 字节 Owner、跳过 3、
                           读 1 字节 nf 后再跳过 nf+1

``FStatus`` 的长度规则：

* ``ver < 64``：只读 1 字节；但 ``NumFStatus == 0`` 时**也**前进 1 字节
  （照搬 C# 行为，虽然看着多余）
* ``ver >= 64``：读 ``NumFStatus`` 字节

解压头用的是"带计数"的那种：``[int32 压缩长度][int16 记录数][int32 解压长度][数据]``，
所以直接复用 :func:`gfvfw.campaign.lzss.expand_with_count`。

⚠️ 实测：``.obd`` 头部申报的**压缩长度偏高**（真实存档里申报 3089，
实际压缩流只有 3083 字节）。用 :func:`expand_with_count` 无影响——它只用
解压长度、并把剩余字节整体交给解压器。但**任何按压缩长度做边界校验的代码
都会被这 6 字节坑到**，所以本模块刻意不对 ``comp_sz`` 设断言。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .lzss import expand_with_count

__all__ = ["ObjDelta", "read_obd", "OBD_COMP_SIZE_QUIRK"]

#: 说明性常量：``.obd`` 头部 compSz 不可信（见模块文档）
OBD_COMP_SIZE_QUIRK = True


class ObdError(ValueError):
    """``.obd`` 结构非法或越界。"""


@dataclass
class ObjDelta:
    """一个目标点的增量。"""

    #: 目标点号（与 ``CampObjData.xml`` 的 ``CampId`` 同一取值空间）
    camp_id: int = 0
    id_creator: int = 0
    last_repair: int = 0
    owner: int = -1
    supply: int = -1
    fuel: int = -1
    losses: int = 0
    num_fstatus: int = 0
    f_status: list[int] = field(default_factory=list)

    @property
    def damaged_features(self) -> int:
        """状态非零的子部件数（即已损毁/受影响的数量）。"""
        return sum(1 for v in self.f_status if v)


def read_obd(raw: bytes, version: int) -> dict[str, Any]:
    """解析 ``.obd`` 内嵌文件的**原始（压缩）字节**。

    返回 ``{"count": 申报记录数, "deltas": [ObjDelta, ...],
    "declared_size": 解压申报长度, "consumed": 实际消费字节}``。
    """
    count, u_sz, d = expand_with_count(raw)
    out: dict[str, Any] = {"count": count, "deltas": [],
                           "declared_size": u_sz, "consumed": 0}
    if not d:
        return out

    pos = 0
    n = len(d)

    def need(k: int) -> None:
        if pos + k > n:
            raise ObdError(".obd 越界：偏移 %d 需要 %d 字节，只剩 %d（总长 %d）"
                           % (pos, k, n - pos, n))

    for i in range(count):
        if pos >= n:
            break
        try:
            need(4 + 4 + 4)
            camp_id = struct.unpack_from("<I", d, pos)[0]; pos += 4
            id_creator = struct.unpack_from("<I", d, pos)[0]; pos += 4
            last_repair = struct.unpack_from("<I", d, pos)[0]; pos += 4
            need(5)
            owner = d[pos]; pos += 1
            supply = d[pos]; pos += 1
            fuel = d[pos]; pos += 1
            losses = d[pos]; pos += 1
            num_fstatus = d[pos]; pos += 1

            f_status: list[int] = []
            if version < 64:
                # 照搬 C#：NumFStatus 为 0 时同样前进 1 字节
                if num_fstatus > 0:
                    need(1)
                    f_status = [d[pos]]
                pos += 1
            else:
                need(num_fstatus)
                f_status = list(d[pos:pos + num_fstatus])
                pos += num_fstatus

            if 83 <= version < 100:
                need(3 + 1 + 3 + 1)
                pos += 3
                owner = d[pos]; pos += 1
                pos += 3
                nf = d[pos]; pos += 1
                need(nf + 1)
                pos += nf + 1

            out["deltas"].append(ObjDelta(
                camp_id=camp_id, id_creator=id_creator, last_repair=last_repair,
                owner=owner, supply=supply, fuel=fuel, losses=losses,
                num_fstatus=num_fstatus, f_status=f_status))
        except ObdError:
            # 单条记录坏掉不至于整份丢弃；如实记下已解出多少条
            out["error"] = "第 %d 条记录解析失败，偏移 %d" % (i + 1, pos)
            break

    out["consumed"] = pos
    return out
