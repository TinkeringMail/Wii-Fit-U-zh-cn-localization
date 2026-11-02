#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r'''
weightpick.py - 为某个 jfnt 挑选最接近原版笔画的 TTF 字重

为什么需要:
  原版各字体的笔画粗细差别很大（实测 DB_L_3 墨水率 35%、EB_L_3 55%）。
  同一个 TTF 字重不可能同时适配两者。
  本脚本对一批 TTF 各自校准 em、渲染同一批字，与原版逐一比对，
  给出墨水率比值和笔画宽度，直接挑最接近的那一档。

两个指标:
  density  包围盒内墨迹占比（阈值 64）。衡量"整体黑度"，粗略但稳定。
  stroke   笔画宽度估计：对字形反复腐蚀，直到墨迹降到 10% 以下的次数 x2。
           这是绝对值（px），可与描边半径 r=3 直接比较。

说明:
  腐蚀用 3x3 方块核，笔画宽度 S 每轮缩 2px，故消失轮数 ≈ ceil(S/2)，取 2*轮数。
  汉字横竖笔通常略细于最大值，所以估计值偏向最粗笔画，属正常。

用法:
  py weightpick.py --font style_font_DB_L_3.jfnt
  py weightpick.py --font style_font_DB_L_3.jfnt --ttf-dir "..\fonts"
  py weightpick.py --font style_font_EB_L_3.jfnt --list "a.ttf,b.ttf"
'''

import sys
import glob as _glob
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image, ImageDraw, ImageFont

from jfntbuild import (JFNT, Atlas, read_cell_plane, bbox_of, calibrate,
                       Renderer, SS)


def erode(plane, w, h):
    """3x3 方块核腐蚀（8 邻域全为墨才算墨）"""
    out = bytearray(w * h)
    for y in range(1, h - 1):
        row = y * w
        up = row - w
        dn = row + w
        for x in range(1, w - 1):
            i = row + x
            if plane[i] < 64:
                continue
            if (plane[up + x - 1] >= 64 and plane[up + x] >= 64 and
                    plane[up + x + 1] >= 64 and plane[row + x - 1] >= 64 and
                    plane[row + x + 1] >= 64 and plane[dn + x - 1] >= 64 and
                    plane[dn + x] >= 64 and plane[dn + x + 1] >= 64):
                out[i] = 255
    return out


def measure(plane, w, h):
    """-> (density, stroke_px, ink_count)"""
    bb = bbox_of(plane, w, h)
    if not bb:
        return (0.0, 0, 0)
    x0, y0, x1, y1 = bb
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    ink = sum(1 for y in range(y0, y1 + 1)
              for x in range(x0, x1 + 1)
              if plane[y * w + x] >= 64)
    dens = ink / float(bw * bh)
    cur = plane
    base = ink
    rounds = 0
    while rounds < 40:
        cur = erode(cur, w, h)
        n = sum(1 for v in cur if v >= 64)
        rounds += 1
        if n <= base * 0.10 or n == 0:
            break
    return (dens, rounds * 2, ink)


def main():
    argv = sys.argv[1:]
    fonts_dir = r"update\content\fonts"
    font = "style_font_DB_L_3.jfnt"
    ttf_dir = str(HERE.parent / "fonts")
    list_s = ""
    n_char = 12
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--font":
            font = argv[i + 1]; i += 2
        elif a == "--fonts":
            fonts_dir = argv[i + 1]; i += 2
        elif a == "--ttf-dir":
            ttf_dir = argv[i + 1]; i += 2
        elif a == "--list":
            list_s = argv[i + 1]; i += 2
        elif a == "--n":
            n_char = int(argv[i + 1]); i += 2
        else:
            i += 1

    fp = Path(fonts_dir) / font
    if not fp.exists():
        print("[错误] 字体不存在: %s" % fp)
        return 1
    jf = JFNT(fp.read_bytes())
    at = Atlas(jf.gtx[jf.data_off:jf.data_off + jf.ib[3]], jf.pitch, jf.rows)

    # 候选 TTF
    cands = []
    if list_s:
        cands = [t.strip() for t in list_s.split(",") if t.strip()]
    else:
        pat = str(Path(ttf_dir) / "AlibabaPuHuiTi-3-*.ttf")
        cands = sorted(_glob.glob(pat))
        if not cands:
            cands = sorted(_glob.glob(str(Path(ttf_dir) / "*.ttf")))[:40]
    cands = [c for c in cands if Path(c).exists()]
    if not cands:
        print("[错误] 找不到候选 TTF，用 --list 指定")
        return 1

    # 统一取样：本字体已有的汉字，均匀取 n_char 个
    cjk = sorted(c for c in jf.cmap if 0x4E00 <= c <= 0x9FFF)
    if not cjk:
        cjk = sorted(jf.cmap)
    step = max(1, len(cjk) // n_char)
    sample = cjk[::step][:n_char]
    chars = "".join(chr(c) for c in sample)

    print("=" * 96)
    print("字体 %s   cell=%dx%d 字形=%d" % (font, jf.cell_w, jf.cell_h, jf.count))
    print("取样 %d 字: %s" % (len(sample), chars))

    # 原版基准
    d_sum = s_sum = n = 0
    per_orig = []
    for cp in sample:
        gi = jf.cmap.get(cp)
        if gi is None or gi >= jf.count:
            continue
        p = read_cell_plane(at, jf, gi, 0)
        d, s, _ink = measure(p, jf.cell_w, jf.cell_h)
        if d <= 0:
            continue
        d_sum += d
        s_sum += s
        n += 1
        per_orig.append((cp, d, s))
    if not n:
        print("[错误] 原版取样全空")
        return 1
    o_dens = d_sum / n
    o_str = s_sum / float(n)
    print("\n原版基准: 墨水率 %.1f%%   笔画宽 %.1f px   （描边 r=3）"
          % (o_dens * 100, o_str))
    print("  %s" % "  ".join("%s:%d%%/%dpx" % (chr(cp), d * 100, s)
                             for cp, d, s in per_orig[:8]))

    print("\n" + "-" * 96)
    print("%-46s%7s%8s%9s%8s%8s" % ("TTF", "em", "墨水率", "比值", "笔画", "笔画比"))
    print("-" * 96)
    rows = []
    for t in cands:
        try:
            em, r_auto, pos, _tg = calibrate(jf, at, t, verbose=False)
            rd = Renderer(jf, t, em, r_auto, pos)
        except Exception as e:
            print("%-46s  失败: %s" % (Path(t).name[:44], e))
            continue
        dd = ss = 0
        m = 0
        for cp in sample:
            a0, _a1 = rd.render(chr(cp))
            d, s, ink = measure(a0, jf.cell_w, jf.cell_h)
            if ink <= 0:
                continue
            dd += d
            ss += s
            m += 1
        if not m:
            print("%-46s  取样全空" % Path(t).name[:44])
            continue
        dens = dd / m
        strw = ss / float(m)
        ratio = dens / o_dens if o_dens else 0
        sratio = strw / o_str if o_str else 0
        tag = ""
        if abs(ratio - 1) <= 0.08:
            tag = "  ★最接近"
        rows.append((abs(ratio - 1), Path(t).name, em, dens, ratio, strw, sratio))
        print("%-46s%7d%7.1f%%%9.2f%8.1f%8.2f%s"
              % (Path(t).name[:44], em, dens * 100, ratio, strw, sratio, tag))

    if not rows:
        return 1
    rows.sort()
    print("\n" + "=" * 96)
    print("按墨水率接近度排序（取前 5）:")
    for k, (dd, nm, em, dens, ratio, strw, sratio) in enumerate(rows[:5]):
        mark = " ←推荐" if k == 0 else ""
        print("  %-44s em=%-3d 墨水率 %.1f%% (x%.2f) 笔画 %.1fpx (x%.2f)%s"
              % (nm[:44], em, dens * 100, ratio, strw, sratio, mark))

    best = rows[0]
    print("\n建议: --ttf-for \"%s,%s\"" % (font, Path(best[1]).name))
    print("描边对照: 原版笔画 %.1f px，描边 r=3 → 描边约为笔画的 %.0f%%"
          % (o_str, 3.0 / o_str * 100 if o_str else 0))
    print("          该 TTF 笔画 %.1f px，r=3 → %.0f%%"
          % (best[5], 3.0 / best[5] * 100 if best[5] else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
