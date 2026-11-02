#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
nestput.py - 把改好的文件塞回「嵌套 jarc」（jarc 里套 jarc）

场景:
  ui_fb_tex.jarc
    └─ tml_tex.jarc          <- 内层
         └─ tml_stamp_pic001_us_eng.jtex
  putback.py 只处理一层，遇到这种会「匹配 0 个」。

做法:
  1. 读外层 jCMP -> jARC，找到内层条目（按名字 stem 匹配，容错 .jarc 后缀）
  2. 内层解 jCMP -> jARC，按名字替换你改好的文件（长度必须相同）
  3. 内层用原 flags 重新压
  4. 内层 blob 长度没变 -> 外层原地覆盖（最安全）
     长度变了 -> 外层完整重封，但先验证对齐可用（重建 == 原件）才敢写

用法:
  py nestput.py update\content\ui\ui_fb_tex.jarc -i tml_tex -n work_stamp -o out_sd\ui\ui_fb_tex.jarc
  py nestput.py update\content\ui\ui_fb_tex.jarc -i tml_tex --report
"""
import argparse
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import putback as pb


def find_entry(ents, key):
    k = key.lower()
    cands = []
    for e in ents:
        nm = e.name.lower()
        if nm == k or nm == k + ".jarc" or Path(nm).stem == k:
            cands.append(e)
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outer", help="外层 jarc 路径")
    ap.add_argument("-i", "--inner", required=True, help="内层条目名（去后缀即可）")
    ap.add_argument("-n", "--new", default=None, help="改好的文件目录")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--level", type=int, default=9)
    a = ap.parse_args()

    outer_path = Path(a.outer)
    if not outer_path.is_file():
        print(f"[错误] 找不到 {outer_path}")
        return 1
    raw_outer = outer_path.read_bytes()
    jarc_outer, cinfo_outer = pb.jcmp_unwrap(raw_outer)
    if jarc_outer[:4] != b"jARC":
        print(f"[错误] 外层不是 jARC（{jarc_outer[:4].hex(' ').upper()}）")
        return 1

    ents_o, unk_o, tot_o, cnt_o = pb.jarc_parse(jarc_outer)
    print(f"外层 {outer_path.name}: {len(raw_outer):,} B -> "
          f"jARC {len(jarc_outer):,} B  条目 {cnt_o}  unknown=0x{unk_o:08X}")

    al_o, ld_o, bd_o = pb.jarc_fit_align(ents_o, unk_o, jarc_outer)
    ok_o = (ld_o == 0 and bd_o == 0)
    print(f"  外层对齐推断: 0x{al_o:X}  "
          + ("重建与原件完全一致 ✓" if ok_o
             else f"不完全一致（长度差 {ld_o:+,}, 内容差 {bd_o:,}）"))

    hits = find_entry(ents_o, a.inner)
    if not hits:
        print(f"\n[错误] 外层里找不到条目 '{a.inner}'")
        print("  可用的条目名（前 30 个）:")
        for e in ents_o[:30]:
            print(f"    {e.name}  ({e.size:,} B)")
        return 1
    if len(hits) > 1:
        print(f"[警告] 匹配到 {len(hits)} 个，取第一个: {hits[0].name}")
    e_in = hits[0]
    blob = jarc_outer[e_in.off:e_in.off + e_in.size]
    print(f"  内层条目: {e_in.name}  {e_in.size:,} B  "
          f"magic={blob[:4].decode('ascii', 'replace')}")

    jarc_in, cinfo_in = pb.jcmp_unwrap(blob)
    if jarc_in[:4] != b"jARC":
        print(f"[错误] 内层不是 jARC（{jarc_in[:4].hex(' ').upper()}）")
        return 1
    ents_i, unk_i, tot_i, cnt_i = pb.jarc_parse(jarc_in)
    print(f"  内层 jARC: {len(jarc_in):,} B  条目 {cnt_i}  unknown=0x{unk_i:08X}")
    if cinfo_in:
        print(f"  内层 jCMP: flags=0x{cinfo_in['flags']:08X} raw={cinfo_in['rawsize']:,} "
              f"{'OK' if cinfo_in['ok'] else '长度不符!'}")

    al_i, ld_i, bd_i = pb.jarc_fit_align(ents_i, unk_i, jarc_in)
    ok_i = (ld_i == 0 and bd_i == 0)
    print(f"  内层对齐推断: 0x{al_i:X}  "
          + ("重建与原件完全一致 ✓" if ok_i
             else f"不完全一致（长度差 {ld_i:+,}, 内容差 {bd_i:,}）"))

    if a.report:
        print(f"\n  内层条目清单（{cnt_i} 个）:")
        for e in ents_i:
            d = jarc_in[e.off:e.off + 4]
            tag = ("jCMP" if d == pb.JCMP else
                   "jIMG" if d == b"jIMG" else d.hex(" ").upper())
            print(f"    [{e.idx:3d}] off=0x{e.off:08X} size={e.size:9,} "
                  f"{tag:8s} {e.name}")
        return 0

    nd = Path(a.new) if a.new else None
    if not nd or not nd.is_dir():
        print(f"[错误] 需要 -n 指定改好的文件目录（或加 --report 只看结构）")
        return 1

    real = [p for p in sorted(nd.rglob("*")) if p.is_file()]
    files = {p.name: p for p in real}
    files.update({p.stem: p for p in real})
    print(f"\n  新文件目录 {nd}: 共 {len(real)} 个文件")
    for p in real:
        print(f"      {p.name}  {p.stat().st_size:,} B")

    repl, matched = {}, []
    for e in ents_i:
        p = files.get(e.name) or files.get(Path(e.name).stem)
        if not p:
            continue
        nb = p.read_bytes()
        if len(nb) != e.size:
            print(f"    [!] {e.name}: 长度 {e.size:,} -> {len(nb):,}，"
                  f"长度必须相同才能原地替换")
            continue
        repl[e.name] = nb
        matched.append(e.name)
        print(f"    {e.name}: {e.size:,} B 替换")

    if not repl:
        print("  [错误] 没有可替换的条目（长度不符或文件名不匹配）")
        print(f"  内层条目名示例: {[e.name for e in ents_i[:6]]}")
        return 1

    # 内层原地替换
    buf = bytearray(jarc_in)
    for e in ents_i:
        if e.name in repl:
            buf[e.off:e.off + e.size] = repl[e.name]
    jarc_in2 = bytes(buf)
    assert len(jarc_in2) == len(jarc_in)

    # 内层重压
    blob2 = pb.jcmp_wrap(jarc_in2, cinfo_in, a.level) if cinfo_in else jarc_in2
    print(f"\n  内层重压: {len(blob):,} -> {len(blob2):,} B")

    # 外层：能原地就原地
    if len(blob2) == e_in.size:
        out_outer = bytearray(jarc_outer)
        out_outer[e_in.off:e_in.off + e_in.size] = blob2
        jarc_outer2 = bytes(out_outer)
        mode = "[A] 外层原地覆盖（内层长度未变）"
    else:
        if not ok_o:
            print(f"\n  [!] 外层对齐无法验证，且内层长度变了（需完整重封）")
            print(f"      长度 {e_in.size:,} -> {len(blob2):,}")
            print("      继续可能产生错位的归档。先跑 --report 把输出发我。")
            return 1
        blobs = [blob2 if e.name == e_in.name
                 else jarc_outer[e.off:e.off + e.size] for e in ents_o]
        ds_o, nm_o = pb.jarc_layout(ents_o, jarc_outer)
        jarc_outer2 = pb.jarc_build(ents_o, blobs, unk_o, al_o, ds_o, nm_o)
        mode = (f"[B] 外层完整重封（对齐 0x{al_o:X}，已通过一致性校验）"
                f" {len(jarc_outer):,} -> {len(jarc_outer2):,} B")
    print(f"  {mode}")

    out_outer = pb.jcmp_wrap(jarc_outer2, cinfo_outer, a.level) if cinfo_outer \
        else jarc_outer2
    out_path = Path(a.out) if a.out else outer_path.with_name(
        outer_path.stem + "_patched.jarc")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_outer)
    print(f"  输出 {len(raw_outer):,} -> {len(out_outer):,} B  ->  {out_path}")

    # 自检：重新读输出，逐层核对
    print("\n  自检:")
    chk_o, chk_ci = pb.jcmp_unwrap(out_path.read_bytes())
    ce_o, _u, _t, cn_o = pb.jarc_parse(chk_o)
    print(f"    外层条目 {cn_o}/{cnt_o}  " + ("✓" if cn_o == cnt_o else "✗"))
    ce_in = [e for e in ce_o if e.name == e_in.name][0]
    cj_in, _ci = pb.jcmp_unwrap(chk_o[ce_in.off:ce_in.off + ce_in.size])
    ce_i, _u2, _t2, cn_i = pb.jarc_parse(cj_in)
    print(f"    内层条目 {cn_i}/{cnt_i}  " + ("✓" if cn_i == cnt_i else "✗"))
    bad = 0
    for e1, e2 in zip(ents_i, ce_i):
        if e1.name != e2.name:
            bad += 1
            continue
        want = repl.get(e1.name, jarc_in[e1.off:e1.off + e1.size])
        if cj_in[e2.off:e2.off + e2.size] != want:
            bad += 1
    print(f"    内层内容核对 {'全部通过 ✓' if bad == 0 else f'{bad} 处不符 ✗'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
