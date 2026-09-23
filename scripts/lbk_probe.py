"""分析 BMS Logbook (``.lbk``) 的结构 —— 只读，不改任何文件。

背景：``docs/requirements.md`` §8.1 的结论是「强混淆二进制，一期不解析」。
但那条结论的依据只是"看起来是二进制、没有公开文档"。本脚本用**四个真实样本**
做结构分析，把"看起来像"换成可验证的事实：

1. 文件长度是否固定（→ 是否存在定长记录数组）
2. 是否存在重复块、周期是多少（→ 是否是 XOR 流密码 / 定长槽位）
3. 两两异或：若用同一密钥流，则 ``C_A ^ C_B == P_A ^ P_B``，
   能直接暴露"两个样本共享的明文"（如空槽位填充）
4. 尝试单字节 XOR 与已知明文猜测（ascii 名字、常见英文串）

用法::

    .venv\\Scripts\\python.exe scripts\\lbk_probe.py
    .venv\\Scripts\\python.exe scripts\\lbk_probe.py --dir "G:\\BMS\\...\\Config"
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter

DEFAULT_DIR = r"G:\BMS\Falcon BMS 4.38\User\Config"


def load(dir_path: pathlib.Path) -> dict[str, bytes]:
    files = sorted(dir_path.glob("*.lbk"))
    if not files:
        raise SystemExit("目录里没有 .lbk：%s" % dir_path)
    return {f.stem: f.read_bytes() for f in files}


def show_sizes(files: dict[str, bytes]) -> None:
    print("=" * 74)
    print("[1] 文件长度")
    for name, b in files.items():
        print("  %-14s %5d 字节" % (name, len(b)))
    sizes = {len(b) for b in files.values()}
    print("  长度是否一致：%s（%s）" % (len(sizes) == 1, sorted(sizes)))
    if len(sizes) == 1:
        n = sizes.pop()
        print("  因数分解：", [d for d in range(2, n + 1) if n % d == 0][:20])


def find_period(b: bytes, min_frac: float = 0.25) -> list[tuple[int, int]]:
    """找出所有周期 p：使得存在一个长度 >= len(b)*min_frac 的后缀在 b 里重复出现。

    返回 ``[(周期, 重复段起始偏移)]``。
    """
    out = []
    n = len(b)
    for p in range(1, n // 2 + 1):
        # 从尾部往前找最长的重复段
        run = 0
        best_start = None
        for i in range(n - 1, p - 1, -1):
            if b[i] == b[i - p]:
                run += 1
                best_start = i - p
            else:
                if run >= n * min_frac:
                    out.append((p, best_start + p))
                run = 0
        if run >= n * min_frac:
            out.append((p, best_start + p))
    # 去掉被更小周期覆盖的冗余项
    dedup: dict[int, int] = {}
    for p, st in out:
        dedup.setdefault(p, st)
    return sorted(dedup.items())


def show_periods(files: dict[str, bytes]) -> None:
    print("\n" + "=" * 74)
    print("[2] 重复块与周期（判定是否存在定长槽位 / 流密码的关键）")
    for name, b in files.items():
        per = find_period(b)
        if not per:
            print("  %-14s 未发现明显周期" % name)
            continue
        # 只打印最小的几个
        for p, start in per[:4]:
            unit = b[start:start + p]
            print("  %-14s 周期=%-4d 起始=0x%04x  单元=%s"
                  % (name, p, start, unit.hex(" ")))
    # 全样本共同的周期更有说服力
    common = None
    for b in files.values():
        ps = {p for p, _ in find_period(b)}
        common = ps if common is None else (common & ps)
    if common:
        print("  ★ 所有样本共有的周期：", sorted(common)[:10])


def xor_pair(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def show_xor_matrix(files: dict[str, bytes]) -> None:
    print("\n" + "=" * 74)
    print("[3] 两两异或")
    print("    若同一密钥流，则 C_A^C_B == P_A^P_B —— 共享明文会变成大段 0x00")
    names = list(files)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            x = xor_pair(files[names[i]], files[names[j]])
            zeros = sum(1 for c in x if c == 0)
            # 找出最长的连续 0 段
            best = cur = 0
            best_at = 0
            for k, c in enumerate(x):
                if c == 0:
                    cur += 1
                    if cur > best:
                        best, best_at = cur, k - cur + 1
                else:
                    cur = 0
            print("  %-12s ^ %-12s 相同字节=%-4d 最长连续相同=%d @0x%04x"
                  % (names[i], names[j], zeros, best, best_at))


def show_byte_histogram(files: dict[str, bytes]) -> None:
    print("\n" + "=" * 74)
    print("[4] 字节分布（低熵=可能未加密的紧凑结构；均匀=可能已加密）")
    for name, b in files.items():
        c = Counter(b)
        print("  %-14s 不同取值=%-4d 最常见=%s"
              % (name, len(c), ", ".join("%02x×%d" % (k, v) for k, v in c.most_common(4))))


def try_single_byte_xor(files: dict[str, bytes]) -> None:
    print("\n" + "=" * 74)
    print("[5] 单字节 XOR 试验（看是否有可读文本浮现）")
    printable = set(range(0x20, 0x7f))
    for name, b in files.items():
        best = []
        for k in range(256):
            dec = bytes(x ^ k for x in b)
            score = sum(1 for x in dec if x in printable) / len(dec)
            best.append((score, k, dec))
        best.sort(reverse=True)
        score, k, dec = best[0]
        print("  %-14s 最佳 key=0x%02x 可打印率=%.0f%%" % (name, k, score * 100))
        print("                 %s" % dec[:64])
        if score > 0.9:
            print("                 ★ 高可打印率，值得细看")


def show_head_diff(files: dict[str, bytes]) -> None:
    print("\n" + "=" * 74)
    print("[6] 头部逐字节对比（寻找字段边界）")
    names = list(files)
    hdr = 48
    print("  off  " + "  ".join("%-11s" % n[:11] for n in names))
    for off in range(hdr):
        vals = [files[n][off] for n in names]
        # 若某两个样本在该字节只差 1 个比特，标出来 —— 那通常意味着同一字段
        flags = ""
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                d = vals[i] ^ vals[j]
                if d and bin(d).count("1") == 1:
                    flags += " %s^%s:bit%d" % (names[i][:3], names[j][:3], d.bit_length() - 1)
        print("  0x%02x  %s%s" % (off, "  ".join("%02x        " % v for v in vals), flags))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    args = ap.parse_args()

    d = pathlib.Path(args.dir)
    print("样本目录：%s" % d)
    files = load(d)
    show_sizes(files)
    show_periods(files)
    show_xor_matrix(files)
    show_byte_histogram(files)
    try_single_byte_xor(files)
    show_head_diff(files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
