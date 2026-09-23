"""尝试从 ``.lbk`` 样本恢复重复密钥流 —— 只读分析，不写任何文件。

推理链（全部基于可验证的实测事实，见 scripts/lbk_probe.py 的输出）:

1. 四个样本长度都是 **372 字节** → 定长结构。
2. 每个样本在偏移 ``0x00E8`` 起都有一个 **周期 42** 的重复块。
3. ``Obsequies`` 与 ``SZSZS`` 在偏移 189 起有 **177 字节完全相同**
   （两者名字完全不同）→ 该区间的内容与飞行员身份无关。
4. 结合 2、3：尾部很可能是**固定填充**（空槽位）在重复密钥下的像。

若「尾部明文是常量」且「密钥以 42 为周期」两个假设同时成立，
则尾部密文就**直接等于**密钥（相差一个常量），周期 42 的密钥可完整恢复，
进而解开整个文件。本脚本就是验证这条推理。

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_keyrecover.py
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter

DEFAULT_DIR = r"G:\BMS\Falcon BMS 4.38\User\Config"

#: 由 lbk_probe 的周期分析得到
TAIL_START = 0x00E8
PERIODS = (42, 84, 21, 14, 6, 7, 3)

#: 常见填充常量：全 0、空格、0xFF、换行
FILL_CANDIDATES = (0x00, 0x20, 0xFF, 0x0A, 0x2E)


def scannable(dec: bytes) -> float:
    """可打印 ASCII（含常见控制符）占比。"""
    ok = set(range(0x20, 0x7F)) | {0x00, 0x09, 0x0A, 0x0D}
    return sum(1 for c in dec if c in ok) / len(dec)


def recover_key(data: bytes, period: int, fill: int) -> tuple[bytes, int]:
    """用尾部区间推导周期为 ``period`` 的密钥；返回 (密钥, 用到的样本数)。"""
    key = [None] * period
    votes: list[Counter] = [Counter() for _ in range(period)]
    for off in range(TAIL_START, len(data)):
        votes[off % period][data[off] ^ fill] += 1
    used = 0
    for i in range(period):
        if votes[i]:
            val, n = votes[i].most_common(1)[0]
            key[i] = val
            used += n
    return bytes(k or 0 for k in key), used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    args = ap.parse_args()

    files = {f.stem: f.read_bytes()
             for f in sorted(pathlib.Path(args.dir).glob("*.lbk"))}
    if not files:
        raise SystemExit("没有样本")

    best_overall: list[tuple[float, str, int, int, bytes, bytes]] = []

    for name, data in files.items():
        print("=" * 74)
        print("样本 %s（%d 字节）" % (name, len(data)))
        for period in PERIODS:
            # 尾部是否真能整除/覆盖出完整的 period 个相位？
            if len(data) - TAIL_START < period:
                continue
            for fill in FILL_CANDIDATES:
                key, used = recover_key(data, period, fill)
                dec = bytes(data[i] ^ key[i % period] for i in range(len(data)))
                score = scannable(dec)
                best_overall.append((score, name, period, fill, key, dec))
                if score > 0.85:
                    print("  ★ period=%-3d fill=0x%02x 可打印率=%.0f%%  key=%s"
                          % (period, fill, score * 100, key.hex(" ")))
                    print("     解密前 64 字节: %s" % dec[:64])
        # 该样本最好的组合
        mine = [x for x in best_overall if x[1] == name]
        if mine:
            score, _, period, fill, key, dec = max(mine)
            print("  最佳: period=%d fill=0x%02x 可打印率=%.0f%%" % (period, fill, score * 100))
            print("    key = %s" % key.hex(" "))
            print("    明文 = %s" % dec[:96])

    print("\n" + "=" * 74)
    print("全局最佳 5 个组合：")
    for score, name, period, fill, key, dec in sorted(best_overall, reverse=True)[:5]:
        print("  %-12s period=%-3d fill=0x%02x 可打印率=%.0f%%" % (name, period, fill, score * 100))
        print("      %s" % dec[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
