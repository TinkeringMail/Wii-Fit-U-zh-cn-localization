#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
jarcinfo.py - 完整解剖一个 jarc：条目、间隙、尾部、重建可行性

jarcdiag.py 只看前 N 条，这个工具把全部条目列出来并做统计，
专门用来解决「对齐推断失败」的归档。

输出重点:
  [1] 头部字段
  [2] 数据区起点 / 名字块实际占用 / 按顺序拼接长度 / 差值
  [3] 每条的 offset 是否首尾相接，间隙值分布
  [4] 文件尾部剩余字节
  [5] 用多种规则重建，逐个比对，定位首个差异和差异区域

用法:
  py jarcinfo.py update\content\ui\ui_fb_tex.jarc
  py jarcinfo.py update\content\ui\ui_fb_tex.jarc --entries
"""
import argparse
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def unwrap(d):
    info = None
    if len(d) >= 20 and d[:4] == b"jCMP":
        size, flags, zs, rs = struct.unpack_from(">IIII", d, 4)
        info = dict(flags=flags, zsize=zs, rawsize=rs, declared=size)
        d = zlib.decompress(d[20:20 + zs])
    return d, info


class E:
    __slots__ = ("i", "off", "size", "noff", "f4", "name")

    def __init__(self, i, off, size, noff, f4, name):
        self.i, self.off, self.size = i, off, size
        self.noff, self.f4, self.name = noff, f4, name


def parse(d):
    total, unknown, count = struct.unpack_from(">III", d, 4)
    ents = []
    for i in range(count):
        o = 16 + i * 16
        off, size, noff, f4 = struct.unpack_from(">IIII", d, o)
        e = d.find(b"\x00", noff) if noff < len(d) else -1
        if e < 0:
            e = len(d)
        ents.append(E(i, off, size, noff, f4,
                      d[noff:e].decode("utf-8", "replace")))
    return ents, unknown, total, count


def build(ents, blobs, unknown, align, data=None, name_pad=0):
    """用 putback.jarc_build（已修正：最后一条不补对齐）做重建比对"""
    import putback as pb
    if data is None:
        raise ValueError("build 需要原始 inner 数据用于取名块")
    ds, names = pb.jarc_layout(ents, data)
    if name_pad:
        names = names + b"\x00" * name_pad
        ds = ds + name_pad
    return pb.jarc_build(ents, blobs, unknown, align, ds, names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jarc")
    ap.add_argument("--entries", action="store_true", help="列出全部条目")
    a = ap.parse_args()

    path = Path(a.jarc)
    outer = path.read_bytes()
    inner, ci = unwrap(outer)
    print(f"文件 {path.name}: {len(outer):,} B")
    if ci:
        print(f"  外层 jCMP: flags=0x{ci['flags']:08X} zsize={ci['zsize']:,} "
              f"raw={ci['rawsize']:,} declared={ci['declared']:,} "
              f"{'OK' if ci['declared'] == len(outer) else '不符!'}")
    else:
        print("  外层: 裸数据（无 jCMP 包装）")
    print(f"  内层: {len(inner):,} B  magic={inner[:4].decode('ascii', 'replace')}")

    ents, unknown, total, count = parse(inner)
    n = count
    tbl_end = 16 + n * 16
    print(f"\n[1] 头部")
    print(f"  声明总长 {total:,}  {'✓' if total == len(inner) else '!'}")
    print(f"  unknown 0x{unknown:08X}   条目数 {n}")
    print(f"  条目表 0x10..0x{tbl_end:X}  ({n} x 16 = {n*16:,} B)")

    offs = sorted(e.off for e in ents)
    ds = offs[0] if offs else tbl_end
    sizes = [e.size for e in ents]
    print(f"\n[2] 数据区")
    print(f"  最小 off = 0x{ds:X} ({ds:,})")
    print(f"  名字块空间 = 0x{ds:X} - 0x{tbl_end:X} = {ds - tbl_end:,} B")
    mine = sum(len(e.name.encode()) + 1 for e in ents)
    print(f"  名字按顺序拼接 = {mine:,} B   差值 = {ds - tbl_end - mine:+,} B")
    print(f"  条目 size 合计 = {sum(sizes):,} B")
    print(f"  文件总长 - 数据区起点 = {len(inner) - ds:,} B")
    print(f"  差值（总长-起点-size合计）= {len(inner) - ds - sum(sizes):+,} B")

    print(f"\n[3] 块间连续性")
    bo = sorted(ents, key=lambda e: e.off)
    gaps = []
    for x, y in zip(bo, bo[1:]):
        g = y.off - (x.off + x.size)
        if g:
            gaps.append((x.name, y.name, g))
    if not gaps:
        print("  所有数据块首尾紧密相接 ✓")
    else:
        print(f"  {len(gaps)} 处间隙/重叠：")
        for an, bn, g in gaps[:10]:
            print(f"    {an} -> {bn}: {'间隙' if g > 0 else '重叠'} {abs(g):,}")
        if len(gaps) > 10:
            print(f"    ... 另有 {len(gaps)-10} 处")
        pos = [g for _a, _b, g in gaps if g > 0]
        if pos:
            print(f"  间隙值分布: {Counter(pos).most_common(8)}")

    print(f"\n[4] 尾部")
    last_end = max(e.off + e.size for e in ents)
    print(f"  最后一个块结束于 0x{last_end:X} ({last_end:,})")
    tail = len(inner) - last_end
    print(f"  尾部剩余 {tail:,} B" + (" ✓ 无多余" if tail == 0 else " ← 有多余数据"))
    if tail and tail <= 64:
        print(f"  尾部内容: {inner[last_end:].hex(' ').upper()}")

    if a.entries:
        print(f"\n[5] 全部 {n} 条")
        for e in ents:
            d4 = inner[e.off:e.off + 4]
            tag = d4.hex(" ").upper() if not d4.isascii() else d4.decode("ascii")
            print(f"  [{e.i:3d}] off=0x{e.off:08X} size={e.size:9,} "
                  f"end=0x{e.off+e.size:08X} f4=0x{e.f4:08X} {tag:6s} {e.name}")

    print(f"\n[6] 重建实验")
    blobs = [inner[e.off:min(e.off + e.size, len(inner))] for e in ents]
    short = [(e.name, e.size, len(b)) for e, b in zip(ents, blobs)
             if len(b) != e.size]
    if short:
        print(f"  [!] {len(short)} 个条目越界被截断: {short[:3]}")

    best = None
    for align in (0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400):
        for npad in (0, 1, 2, 3, 4):
            buf = build(ents, blobs, unknown, align, inner, npad)
            ld = len(buf) - len(inner)
            k = min(len(buf), len(inner))
            bd = sum(1 for x, y in zip(buf[:k], inner[:k]) if x != y)
            score = bd + abs(ld)
            if best is None or score < best[2]:
                best = (align, npad, score, ld, bd, buf)
            if score == 0:
                print(f"  align=0x{align:X} name_pad={npad}: 完全一致 ✓")
                return 0

    align, npad, score, ld, bd, buf = best
    print(f"  最接近: align=0x{align:X} name_pad={npad}")
    print(f"    长度差 {ld:+,}   内容差 {bd:,}   综合 {score:,}")
    first = None
    for i in range(min(len(buf), len(inner))):
        if buf[i] != inner[i]:
            first = i
            break
    if first is None:
        print("    前缀完全相同，仅长度不同")
        print(f"    原文件多出 {(len(inner)-len(buf)) if len(inner)>len(buf) else 0} B")
    else:
        region = ("条目表" if first < tbl_end else
                  "名字区" if first < ds else "数据区")
        print(f"    首个差异 @0x{first:X} [{region}]")
        seg_b = buf[first:first + 32]
        seg_i = inner[first:first + 32]
        print(f"      重建 |{seg_b.hex(' ').upper()}|")
        print(f"      原始 |{seg_i.hex(' ').upper()}|")
    return 0


if __name__ == "__main__":
    sys.exit(main())
