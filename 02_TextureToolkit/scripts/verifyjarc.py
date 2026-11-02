#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
verifyjarc.py - 逐条比对「原始 jarc」和「改后 jarc」，确认只有该变的变了

用于部署前的最后一道保险。完整重封（[B]）会平移所有条目偏移，
虽然自检能过，但必须确认：
  1. 条目名单与顺序完全一致
  2. 未修改的条目内容逐字节相同（按名字对齐，不靠偏移）
  3. 修改过的条目确实变了
  4. 偏移无重叠、无诡异间隙、尾部无多余

用法:
  py verifyjarc.py update\content\ui\ui_fb_tex.jarc out_sd\ui\ui_fb_tex.jarc
  py verifyjarc.py 原.jarc 新.jarc --expect tml_tex.jarc
  py verifyjarc.py 原.jarc 新.jarc --nested tml_tex.jarc --expect tml_stamp_pic001_us_eng.jtex
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import putback as pb


def payload(data_outer):
    """去掉 jCMP 包装，返回 (inner, 说明)"""
    inner, ci = pb.jcmp_unwrap(data_outer)
    kind = "jCMP(jARC)" if ci else "裸 " + inner[:4].decode("ascii", "replace")
    return inner, ci, kind


def check_layout(ents, data, tag):
    """偏移合法性：不重叠、不越界、尾部"""
    ok = True
    ordered = sorted(ents, key=lambda e: e.off)
    for a, b in zip(ordered, ordered[1:]):
        if a.off + a.size > b.off:
            print(f"    [!] {tag} 重叠: {a.name} 结束于 0x{a.off+a.size:X}，"
                  f"但 {b.name} 从 0x{b.off:X} 开始")
            ok = False
    for e in ents:
        if e.off + e.size > len(data):
            print(f"    [!] {tag} 越界: {e.name} 到 0x{e.off+e.size:X} > {len(data):,}")
            ok = False
    tail = len(data) - max(e.off + e.size for e in ents)
    if tail:
        print(f"    [i] {tag} 尾部 {tail} B")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orig")
    ap.add_argument("new")
    ap.add_argument("--expect", action="append", default=[],
                    help="预期被修改的条目名（可多次指定）")
    ap.add_argument("--nested", default=None,
                    help="只比较外层里指定名字的嵌套条目内部")
    a = ap.parse_args()

    po, pn = Path(a.orig), Path(a.new)
    if not po.is_file() or not pn.is_file():
        print(f"[错误] 文件不存在: {po} / {pn}")
        return 1

    raw_o, raw_n = po.read_bytes(), pn.read_bytes()
    io, cio, ko = payload(raw_o)
    inew, cin, kn = payload(raw_n)

    print(f"原始 {po.name}: {len(raw_o):,} B  {ko}")
    print(f"改后 {pn.name}: {len(raw_n):,} B  {kn}")
    print(f"  内层 {len(io):,} -> {len(inew):,} B  ({len(inew)-len(io):+,})")

    if io[:4] != b"jARC" or inew[:4] != b"jARC":
        print("[错误] 内层不是 jARC")
        return 1

    eo, unk_o, tot_o, cnt_o = pb.jarc_parse(io)
    en, unk_n, tot_n, cnt_n = pb.jarc_parse(inew)
    print(f"\n条目 {cnt_o} -> {cnt_n}  "
          + ("✓" if cnt_o == cnt_n else "✗ 数量变了！"))
    print(f"unknown 0x{unk_o:08X} -> 0x{unk_n:08X}  "
          + ("✓" if unk_o == unk_n else "✗ 变了"))

    # 名单与顺序
    no = [e.name for e in eo]
    nn = [e.name for e in en]
    if no != nn:
        print("  ✗ 条目名单/顺序不同：")
        only_o = set(no) - set(nn)
        only_n = set(nn) - set(no)
        if only_o:
            print(f"    只在新文件缺失: {sorted(only_o)[:10]}")
        if only_n:
            print(f"    只在新文件多出: {sorted(only_n)[:10]}")
        if not only_o and not only_n:
            print("    集合相同但顺序不同（可能影响游戏读取顺序，需注意）")
        return 1
    print("  条目名单与顺序一致 ✓")

    print("\n[布局检查]")
    ok_o = check_layout(eo, io, "原")
    ok_n = check_layout(en, inew, "新")

    # 逐条内容比对（按名字，忽略偏移）
    # 嵌套模式下的预期拆分：
    #   外层 —— 只有容器本身应当变化
    #   内层 —— expect 里不属于外层名单的条目
    outer_names = {e.name for e in eo}
    if a.nested:
        outer_expect = set()
        for e in eo:
            if e.name == a.nested or Path(e.name).stem == Path(a.nested).stem:
                outer_expect.add(e.name)
        inner_expect = {x for x in set(a.expect) if x not in outer_names}
    else:
        outer_expect = set(a.expect)
        inner_expect = set()

    changed, same, mismatch_unexpected = [], 0, []
    for e1, e2 in zip(eo, en):
        b1 = io[e1.off:e1.off + e1.size]
        b2 = inew[e2.off:e2.off + e2.size]
        if b1 == b2:
            same += 1
        else:
            changed.append((e1.name, e1.size, e2.size))
            if outer_expect and e1.name not in outer_expect:
                mismatch_unexpected.append(e1.name)

    print(f"\n[内容比对]")
    print(f"  未变 {same} 个，变化 {len(changed)} 个")
    for nm, s1, s2 in changed:
        mark = "（预期）" if (not outer_expect or nm in outer_expect) else "（意外！）"
        print(f"    {nm}: {s1:,} -> {s2:,} B  {mark}")

    if mismatch_unexpected:
        print(f"\n  ✗ 有 {len(mismatch_unexpected)} 个非预期条目被改动:")
        for nm in mismatch_unexpected[:10]:
            print(f"      {nm}")
        return 1
    if outer_expect and not a.nested:
        missing = outer_expect - {c[0] for c in changed}
        if missing:
            print(f"\n  [!] 预期改动但这些没变: {sorted(missing)}")
            print("      可能是替换源与原内容完全相同（比如本来就是这个字）")

    # 嵌套检查
    if a.nested:
        print(f"\n[嵌套检查] {a.nested}")
        def grab(ents, data, key):
            for e in ents:
                if e.name == key or Path(e.name).stem == Path(key).stem:
                    return e, data[e.off:e.off + e.size]
            return None, None
        e1, b1 = grab(eo, io, a.nested)
        e2, b2 = grab(en, inew, a.nested)
        if e1 is None or e2 is None:
            print("  [错误] 找不到嵌套条目")
            return 1
        print(f"  外层条目 {e1.size:,} -> {e2.size:,} B")
        i1, c1 = pb.jcmp_unwrap(b1)
        i2, c2 = pb.jcmp_unwrap(b2)
        print(f"  内层 {len(i1):,} -> {len(i2):,} B  "
              f"flags 0x{c1['flags']:08X} / 0x{c2['flags']:08X}")
        ne1, u1, t1, n1 = pb.jarc_parse(i1)
        ne2, u2, t2, n2 = pb.jarc_parse(i2)
        print(f"  条目 {n1} -> {n2}  " + ("✓" if n1 == n2 else "✗"))
        check_layout(ne1, i1, "原内层")
        check_layout(ne2, i2, "新内层")
        ch = []
        sm = 0
        for x, y in zip(ne1, ne2):
            if x.name != y.name:
                print(f"  ✗ 名称不对齐: {x.name} vs {y.name}")
                return 1
            bb1 = i1[x.off:x.off + x.size]
            bb2 = i2[y.off:y.off + y.size]
            if bb1 != bb2:
                ch.append(x.name)
            else:
                sm += 1
        print(f"  未变 {sm} 个，变化 {len(ch)} 个")
        for nm in ch:
            print(f"    {nm}")
        if inner_expect:
            bad = [nm for nm in ch if nm not in inner_expect]
            if bad:
                print(f"  ✗ 内层非预期改动: {bad}")
                return 1
            missing2 = inner_expect - set(ch)
            if missing2:
                print(f"  [i] 内层预期改动但这些没变: {sorted(missing2)}")
                print("      通常是替换源与原内容本来就相同，不是错误")

    print(f"\n# 结论: " + ("通过 ✓ 可以部署" if ok_o and ok_n else "有问题，勿部署 ✗"))
    return 0 if (ok_o and ok_n) else 1


if __name__ == "__main__":
    sys.exit(main())
