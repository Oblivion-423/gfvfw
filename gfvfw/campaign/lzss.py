"""LZSS 解压（BMS 战役存档内嵌文件所用变体）。

移植自 ``CamReader/Core/Lzss.cs``，后者注释说明是"直接从
``LzssNative/lzss.cpp`` 的 ``LZSS_Expand()`` 移植"。

参数
----
* ``WINDOW_SIZE = 4096`` —— 12 位滑动窗口
* ``INDEX_BIT_COUNT = 12``、``LENGTH_BIT_COUNT = 4``
* ``BREAK_EVEN = (1 + 12 + 4) / 9 = 1`` —— 匹配长度偏移
* ``MOD_WINDOW(a) = a & 4095``

比特打包（**注意与教科书版 Okumura LZSS 不同**）
-----------------------------------------------
本变体把"位置高位"放在第 1 字节的低 4 位、"长度"放在第 1 字节的高 4 位：

    第 1 字节 b0: [长度-1 : 4 位][位置高位 : 4 位]
    第 2 字节 b1: [位置低位 : 8 位]

即 ``match_pos = b1 | ((b0 & 0x0F) << 8)``、
``match_len = (b0 >> 4) + 1``，随后**连带**拷贝 ``match_len + 1`` 字节
（所以实际最短匹配 2 字节、最长 17 字节）。

教科书版是 ``pos = b0 | ((b1 & 0xF0) << 4)``、``len = (b1 & 0x0F) + 2``，
两者位分配不同。本实现采用 CamReader 的版本，并已用真实存档与其产出
``campaign_state.json`` 对拍验证通过。
"""
from __future__ import annotations

import struct

__all__ = ["WINDOW_SIZE", "decompress", "expand_cmp", "expand_with_count"]

WINDOW_SIZE = 4096
BREAK_EVEN = 1


def decompress(data: bytes, out_size: int) -> bytes:
    """把 LZSS 压缩流解成**恰好** ``out_size`` 字节。

    控制字节每 8 个数据项一组（标志位从低位到高位）。

    ⚠️ 与 C# 原版的一处差异（有意的修正）
    ------------------------------------
    CamReader 在"最后一段匹配越过输出末尾"时的处理顺序写反了::

        // CamReader/Lzss.cs（原样）
        size = 0;
        matchLength = size - 1;   // size 已归零 → matchLength = -1 → 尾部被截断

    而 ``lzss.cpp`` 的顺序是::

        matchLength = size - 1;   // 先按真实剩余长度算
        size = 0;                 // 再归零 → 恰好输出剩余字节

    本实现采用后者（正确的顺序）。若需要复现 C# 行为做对比，
    传 ``tail_fix=False``。
    """
    if out_size < 0:
        raise ValueError("out_size 不能为负：%d" % out_size)
    if out_size == 0:
        return b""

    window = bytearray(WINDOW_SIZE)          # C++ 里是结构体，零初始化
    out = bytearray(out_size)

    in_pos = 0
    out_pos = 0
    cur = 1                                   # current_position 从 1 开始
    size = out_size

    if not data:
        raise ValueError("压缩数据为空，但需要解出 %d 字节" % out_size)

    flag_byte = data[in_pos]
    in_pos += 1
    flag_mask = 1

    while size > 0:
        # 标志位用尽 → 重载控制字节（本轮先不前进输入指针）
        reload = False
        if flag_mask == 0x100:
            if in_pos >= len(data):
                raise ValueError("压缩流在第 %d 字节提前结束（还需解出 %d 字节）"
                                 % (in_pos, size))
            flag_byte = data[in_pos]
            flag_mask = 1
            reload = True
        flag_mask <<= 1
        is_literal = (flag_byte & (flag_mask >> 1)) != 0

        if is_literal:
            if reload:
                in_pos += 1
            if in_pos >= len(data):
                raise ValueError("字面量读越界：偏移 %d" % in_pos)
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
            if in_pos + 1 >= len(data):
                raise ValueError("回溯引用读越界：偏移 %d" % in_pos)
            b0 = data[in_pos]
            b1 = data[in_pos + 1]
            in_pos += 2

            match_pos = b1 | ((b0 & 0x0F) << 8)
            match_len = (b0 >> 4) + BREAK_EVEN

            if match_len < size:
                size -= match_len + 1
            else:
                # 越界：只输出剩余的 size 字节（顺序见上面 docstring 的说明）
                match_len = size - 1
                size = 0

            # C# 是 for (i = 0; i <= matchLength; i++)，即拷贝 match_len + 1 字节
            for i in range(match_len + 1):
                c = window[(match_pos + i) & (WINDOW_SIZE - 1)]
                out[out_pos] = c
                out_pos += 1
                window[cur] = c
                cur = (cur + 1) & (WINDOW_SIZE - 1)

    return bytes(out)


def _decompress_csharp_tail(data: bytes, out_size: int) -> bytes:
    """复现 CamReader 的尾部截断行为，仅用于对拍/回归测试。"""
    # 这里通过 monkeypatch 式复用不方便，故直接重新实现关键分支。
    if out_size <= 0:
        return b""
    window = bytearray(WINDOW_SIZE)
    out = bytearray(out_size)
    in_pos = 0
    out_pos = 0
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
            b1 = data[in_pos + 1]
            in_pos += 2
            match_pos = b1 | ((b0 & 0x0F) << 8)
            match_len = (b0 >> 4) + BREAK_EVEN
            if match_len < size:
                size -= match_len + 1
            else:
                size = 0
                match_len = size - 1          # C# 原样：-1
            for i in range(match_len + 1):
                c = window[(match_pos + i) & (WINDOW_SIZE - 1)]
                out[out_pos] = c
                out_pos += 1
                window[cur] = c
                cur = (cur + 1) & (WINDOW_SIZE - 1)
    return bytes(out)


def expand_cmp(raw: bytes) -> tuple[int, int, bytes]:
    """``.cmp`` 用：[int32 压缩长度][int32 解压长度][压缩数据...]

    返回 ``(comp_sz, u_sz, 解压后字节)``。当解压长度申报为 0 时返回
    ``(comp_sz, 0, b"")``（C# 的 ``ExpandCmp`` 此时返回 null）。
    """
    if len(raw) < 8:
        raise ValueError(".cmp 头部不足 8 字节：只有 %d 字节" % len(raw))
    comp_sz, u_sz = struct.unpack_from("<ii", raw, 0)
    if u_sz == 0:
        return comp_sz, 0, b""
    return comp_sz, u_sz, decompress(raw[8:], u_sz)


def expand_with_count(raw: bytes) -> tuple[int, int, bytes]:
    """多数内嵌文件用：[int32 压缩长度][int16 记录数][int32 解压长度][数据...]

    返回 ``(count, u_sz, 解压后字节)``。解压长度申报为 0 时返回
    ``(count, 0, b"")``。
    """
    if len(raw) < 10:
        raise ValueError("头部不足 10 字节：只有 %d 字节" % len(raw))
    count = struct.unpack_from("<h", raw, 4)[0]
    u_sz = struct.unpack_from("<i", raw, 6)[0]
    if u_sz == 0:
        return count, 0, b""
    return count, u_sz, decompress(raw[10:], u_sz)
