#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r'''
jfntbuild.py - 从 Windows TTF + 字符表重建 Wii Fit U 的 .jfnt

两种操作:
  replace  已有字符原地换字形   几何零变化 / 体积零增长 / 字形号不变
  add      缺失字符追加         图集向下加行 / 范围表加条目
  both     先替换再追加（推荐）

为什么必须组合:
  日文字体里已有大量汉字，但字形是日式的（直/具/真/角/内/骨 等与中文不同）。
  只追加 -> 同一段落里日式与中式混排。
  只替换 -> 缺的字仍然没有。
  所以: 已有的替换掉（免费），缺的追加。

关键实现约束:
  cell_w/cell_h 常不是 4 的倍数（DB_3_S 是 39x55），格子会跨 BC5 的 4x4 块。
  因此不能整块搬运，只能: 解码块 -> 改格内像素 -> 重编码块。
  格外的像素原值保留，重编码只带来 BC4 量化误差。

结构要点:
  JFNT  0x00 'JFNT' | 0x05 flags | 0x08 max_w | 0x09 height | 0x0A cols | 0x0B pad
        0x0C u16 范围数 | 范围 6B xN | u16 字形数 + 每字形 1B 宽度 | 补齐 0x80 | jIMG
        cell_w = max_w + pad,  cell_h = height + pad
  jIMG  0x10 u32 GTX偏移(&0xFFFF) | 0x14 u32 GTX长
        0x1C CRC32(整个 jIMG，0x1C..0x24 置零) | 0x20 CRC32(文件名去扩展名，保留原值)
  GTX   BLK 0x0B 表面: +0x04 宽 +0x08 高 +0x20 imageSize +0x3C pitch +0x8C低13位=(高-1)
        BLK 0x0C 像素
  tileMode4: 宏块 32x16，512 块/宏块
    addr = ((xb>>5) + (pitch>>5)*(yb>>4))*512 + bits[y0,x0,x1,x2, x3^y3,x3^y5,x4^y4, y1,y2]

依赖: Pillow（渲染 + 膨胀）；不需要 numpy。

用法（所有路径均为相对路径，便携版默认字体在 ..\fonts\）:
  py jfntbuild.py --fonts ..\game\_fontwork\fonts_orig\font_data ^
                  --chars ..\game\_fontwork\chars --ttf ..\fonts\AlibabaPuHuiTi-3-75-SemiBold.ttf ^
                  --font style_font_DB_3_S.jfnt --mode both --dry --verify
  py jfntbuild.py ... --mode both -o build\fonts
  py jfntbuild.py ... --mode both -o build\fonts --jarc ..\game\update\content\fonts\font_data.jarc
  py jfntbuild.py ... --mode replace --all-chars      # 所有字体的字符表合并后统一用

按字体指定不同 TTF（体现字重差异）:
  --ttf <默认字体> --ttf-for "字体A,路径A/字体B,路径B"
  多组之间用 / 分隔；未列出的字体用 --ttf 的默认值。
  例:
    --ttf "..\fonts\AlibabaPuHuiTi-3-75-SemiBold.ttf" ^
    --ttf-for "style_font_DB_L_3.jfnt,..\fonts\AlibabaPuHuiTi-3-115-Black.ttf/style_font_EB_L_3.jfnt,..\fonts\AlibabaPuHuiTi-3-115-Black.ttf"
'''

import sys
import os
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError as _e:
    print("需要 Pillow:  py -m pip install pillow")
    print("  实际错误: %s: %s" % (type(_e).__name__, _e))
    raise SystemExit(1)

PUA_LO, PUA_HI = 0xE000, 0xF8FF
SS = 4                      # 超采样倍率

# 实测基准（olmeasure.py 对原版字体测得）:
#   DB_3_S  cell 39x55  height 49  墨迹高 30px (61.2% of height)  描边 r=3
#   DB_3    cell 56x80  height 74  墨迹高 46px (62.2% of height)  描边 r=3
#   两者导出 r=3 说明原版描边是"固定像素半径"，不随字号缩放。
#   ch1 = 方形核膨胀(ch0, r)，IoU 0.98 验证。
# 所以不再用「字号=height*系数」推算，改为对每个字体实测墨迹高并反推 em。
EM_BASE_EM = 34.0
EM_BASE_H = 49.0
OL_BASE_R = 3.0
EM_RATIO = EM_BASE_EM / EM_BASE_H
OL_RATIO = OL_BASE_R / EM_BASE_EM


def u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ================================================================ BC4
def bc4_pal(r0, r1):
    if r0 > r1:
        return [r0, r1] + [((8 - k) * r0 + (k - 1) * r1) // 7 for k in range(2, 8)]
    p = [r0, r1] + [((6 - k) * r0 + (k - 1) * r1) // 5 for k in range(2, 6)]
    return p + [0, 255]


def bc4_dec(b8):
    r0, r1 = b8[0], b8[1]
    bits = int.from_bytes(b8[2:8], "little")
    p = bc4_pal(r0, r1)
    return [p[(bits >> (3 * i)) & 7] for i in range(16)]


def bc4_enc(vals):
    hi = max(vals)
    lo = min(vals)
    if hi == lo:
        r0, r1 = (hi, hi - 1) if hi > 0 else (1, 0)
    else:
        r0, r1 = hi, lo
    p = bc4_pal(r0, r1)
    bits = 0
    for i, v in enumerate(vals):
        best = 1 << 20
        bi = 0
        for k in range(8):
            d = v - p[k]
            if d < 0:
                d = -d
            if d < best:
                best = d
                bi = k
        bits |= bi << (3 * i)
    return bytes((r0, r1)) + bits.to_bytes(6, "little")


# ================================================================ 图集地址
def _bidx(xb, yb):
    b = lambda v, i: (v >> i) & 1
    order = [b(yb, 0), b(xb, 0), b(xb, 1), b(xb, 2),
             b(xb, 3) ^ b(yb, 3), b(xb, 3) ^ b(yb, 5), b(xb, 4) ^ b(yb, 4),
             b(yb, 1), b(yb, 2)]
    n = 0
    for pos, bit in enumerate(order):
        n |= bit << pos
    return n


def baddr(xb, yb, pitch_blocks):
    mt = (xb >> 5) + (pitch_blocks >> 5) * (yb >> 4)
    return mt * 512 + _bidx(xb, yb)


class Atlas:
    """按块寻址的原始像素缓冲；不复用整图解码，避免无谓开销"""

    def __init__(self, data, pitch_blocks, rows_blocks):
        self.d = bytearray(data)
        self.pitch = pitch_blocks
        self.rows = rows_blocks

    def get(self, xb, yb):
        o = baddr(xb, yb, self.pitch) * 16
        return self.d[o:o + 16]

    def set(self, xb, yb, blk):
        o = baddr(xb, yb, self.pitch) * 16
        self.d[o:o + 16] = blk

    def grow(self, new_rows):
        """向下扩行：旧块地址不变（地址只依赖 xb/yb/pitch），新块填零"""
        if new_rows <= self.rows:
            self.rows = new_rows
            need = new_rows * self.pitch * 16
            if len(self.d) > need:
                self.d = self.d[:need]
            return
        need = new_rows * self.pitch * 16
        if len(self.d) < need:
            self.d.extend(b"\0" * (need - len(self.d)))
        self.rows = new_rows


# ================================================================ JFNT
class JFNT:
    def __init__(self, d):
        if d[:4] != b"JFNT":
            raise ValueError("不是 JFNT")
        self.raw = d
        self.max_w = d[8]
        self.height = d[9]
        self.cols = d[10]
        self.pad = d[11]
        self.cell_w = self.max_w + self.pad
        self.cell_h = self.height + self.pad
        n = u16(d, 0x0C)
        self.ranges = [list(struct.unpack(">HHH", d[0x0E + i * 6:0x14 + i * 6]))
                       for i in range(n)]
        end = 0x0E + n * 6
        self.count = u16(d, end)
        self.widths = list(d[end + 2:end + 2 + self.count])
        self.jimg = d.find(b"jIMG", end + 2 + self.count)
        if self.jimg < 0:
            raise ValueError("找不到 jIMG")
        self.cmap = {}
        for s, e, g in self.ranges:
            for c in range(s, e + 1):
                self.cmap[c] = g + (c - s)

        jh = d[self.jimg:self.jimg + 0x80]
        self.gtx_off = u32(jh, 0x10) & 0xFFFF
        self.gtx_len = u32(jh, 0x14)
        self.gtx = d[self.jimg + self.gtx_off:self.jimg + self.gtx_off + self.gtx_len]
        self._gtx_parse()

    def _gtx_parse(self):
        g = self.gtx
        pos = u32(g, 4)
        blocks = []
        while pos + 0x20 <= len(g):
            mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I", g[pos:pos + 0x20])
            if mg != b"BLK{":
                break
            blocks.append((typ, pos, pos + bhs, dsz, bhs))
            pos += bhs + dsz
        self.blocks = blocks
        sb = next((x for x in blocks if x[0] == 0x0B), None)
        ib = next((x for x in blocks if x[0] == 0x0C), None)
        if not sb or not ib:
            raise ValueError("缺 0x0B 或 0x0C 块")
        self.sb = sb
        self.ib = ib
        self.surf = struct.unpack(">16I", g[sb[2]:sb[2] + 64])
        self.fmt = self.surf[5] & 0xFF
        self.tile = self.surf[12]
        self.pitch = self.surf[15]
        self.tex_w = self.surf[1]
        self.tex_h = self.surf[2]
        self.img_size = self.surf[8]
        self.data_off = ib[2]
        self.rows = ib[3] // 16 // self.pitch if self.pitch else 0

    def cell_xy(self, gi):
        r, c = divmod(gi, self.cols)
        return c * self.cell_w, r * self.cell_h

    def geom_for(self, n_glyphs, pad_blocks=16):
        """-> (逻辑高, 逻辑行数, 填充块行, 填充高, imageSize)
        逻辑高 = 字形行数 x cell_h，写进 0x0B+0x08 与 jIMG+0x0A。
        填充块行只用于决定 imageSize 和数据长度，补齐到 16 的倍数。"""
        rows_log = (n_glyphs + self.cols - 1) // self.cols
        h_log = rows_log * self.cell_h
        rb = (h_log + 3) // 4
        rb += (-rb) % pad_blocks
        return h_log, rows_log, rb, rb * 4, rb * self.pitch * 16


# ================================================================ 读原始字形
def read_cell_plane(at, jf, gi, ch):
    """把某个字形的整个 cell 读成 cell_w*cell_h 字节数组"""
    cw, chh = jf.cell_w, jf.cell_h
    x0, y0 = jf.cell_xy(gi)
    out = bytearray(cw * chh)
    cache = {}
    for py in range(y0, y0 + chh):
        for px in range(x0, x0 + cw):
            k = (px >> 2, py >> 2)
            v = cache.get(k)
            if v is None:
                b = at.get(px >> 2, py >> 2)
                sub = b[0:8] if ch == 0 else b[8:16]
                v = bc4_dec(sub) if len(sub) >= 8 else [0] * 16
                cache[k] = v
            out[(py - y0) * cw + (px - x0)] = v[(py & 3) * 4 + (px & 3)]
    return out


def bbox_of(plane, w, h, th=64):
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        base = y * w
        for x in range(w):
            if plane[base + x] >= th:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        return None
    return x0, y0, x1, y1


def sq_dilate(plane, w, h, r):
    """方形核膨胀，与 ImageFilter.MaxFilter(2r+1) 等价"""
    if r <= 0:
        return bytearray(plane)
    tmp = bytearray(w * h)
    for y in range(h):
        base = y * w
        for x in range(w):
            lo = base + (x - r if x > r else 0)
            hi = base + (x + r + 1 if x + r + 1 < w else w)
            tmp[base + x] = max(plane[lo:hi])
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            m = 0
            for dy in range(-r, r + 1):
                yy = y + dy
                if 0 <= yy < h:
                    v = tmp[yy * w + x]
                    if v > m:
                        m = v
            out[y * w + x] = m
    return out


def iou_of(a, b, th=64):
    inter = uni = 0
    for x, y in zip(a, b):
        ua = x >= th
        ub = y >= th
        if ua or ub:
            uni += 1
            if ua and ub:
                inter += 1
    return (inter / uni) if uni else 0.0


# ================================================================ 校准
FULLBOX = "国永中田品目日回园圖圆麵歯"     # 填满字框的字，作标尺
LATINBOX = "HAMBKRN"                      # 无汉字时的标尺


def calibrate(jf, at, ttf, n_sample=24, verbose=True, em_override=None):
    """从原版字体实测: 目标墨迹尺寸、字身位置、描边半径。
       再用同一批标尺字在 TTF 上量同样的量，反推 em。
       用"满格字"做标尺可避免笔画少的字拉低统计量。"""
    cw, chh = jf.cell_w, jf.cell_h

    def med(a):
        a = sorted(a)
        return a[len(a) // 2]

    def pct(a, p):
        a = sorted(a)
        return a[min(len(a) - 1, int(len(a) * p))]

    # ---- 选标尺字：优先满格字，其次"自动挑最饱满的汉字"，最后拉丁 ----
    size_cps = [ord(c) for c in FULLBOX if ord(c) in jf.cmap]
    cjk_all = sorted(c for c in jf.cmap if 0x3400 <= c <= 0x9FFF)
    if len(size_cps) < 3 and len(cjk_all) >= 3:
        # 本字体有汉字，但没有满格标尺字（如 DB_3_I 只有 39 个动作名汉字）。
        # 此时若退回拉丁字母，目标尺寸会按拉丁字高算，
        # 而实际要渲染的是汉字 —— 两者字面率不同，会导致中文偏大或偏小。
        # 做法：先量一批汉字的真实墨迹，取高度前 25% 的作标尺。
        probe = cjk_all[::max(1, len(cjk_all) // 150)][:150]
        meas = []
        for cp in probe:
            gi = jf.cmap.get(cp)
            if gi is None or gi >= jf.count:
                continue
            bb = bbox_of(read_cell_plane(at, jf, gi, 0), cw, chh)
            if not bb:
                continue
            meas.append((bb[3] - bb[1] + 1, cp))
        if len(meas) >= 3:
            hs_ = sorted(m[0] for m in meas)
            thr = hs_[int(len(hs_) * 0.75)]
            size_cps = [m[1] for m in meas if m[0] >= thr][:12]
    latin_mode = len(size_cps) < 3
    if latin_mode:
        size_cps = [ord(c) for c in LATINBOX if ord(c) in jf.cmap]
        if len(size_cps) < 3:
            size_cps = [c for c in sorted(jf.cmap)
                        if 0x41 <= c <= 0x5A or 0x30 <= c <= 0x39][:8]
        if not size_cps:
            raise ValueError("字体里既无汉字也无拉丁字母，无法校准")
        sample = "".join(chr(c) for c in size_cps)
    else:
        sample = "".join(chr(c) for c in size_cps)

    # ---- 统计样本：整个 CJK 区间散布采样（不局限于开头）----
    cjk = sorted(c for c in jf.cmap if 0x4E00 <= c <= 0x9FFF)
    if cjk:
        step = max(1, len(cjk) // (n_sample * 6))
        scan = cjk[::step]
    else:
        scan = [c for c in sorted(jf.cmap) if 0x41 <= c <= 0x5A] or \
               [c for c in sorted(jf.cmap) if c >= 0x20]

    hs_all, ws_all, rs = [], [], []
    hs_size = []          # 标尺字的墨迹高
    ox_size, oy_size = [], []
    seen = 0
    # 标尺字优先测（保证一定被量到，不受散布采样步长影响）
    order = list(size_cps) + [c for c in scan if c not in size_cps]
    for cp in order:
        if seen >= n_sample * 3:
            break
        gi = jf.cmap.get(cp)
        if gi is None or gi >= jf.count:
            continue
        p0 = read_cell_plane(at, jf, gi, 0)
        bb = bbox_of(p0, cw, chh)
        if bb is None:
            continue
        w_ = bb[2] - bb[0] + 1
        h_ = bb[3] - bb[1] + 1
        if h_ < chh * 0.35 and not latin_mode:
            continue
        seen += 1
        hs_all.append(h_)
        ws_all.append(w_)
        if cp in size_cps:
            hs_size.append(h_)
            ox_size.append(bb[0])
            oy_size.append(bb[1])
        p1 = read_cell_plane(at, jf, gi, 1)
        if sum(1 for v in p1 if v >= 64) >= 5:
            best = (-1.0, 0)
            for r in range(0, 9):
                s = iou_of(sq_dilate(p0, cw, chh, r), p1)
                if s > best[0]:
                    best = (s, r)
            if best[0] > 0.80:
                rs.append(best[1])

    if not hs_all:
        raise ValueError("原版字体里找不到可用于校准的字形")

    r_med = med(rs) if rs else 3

    # ---- 目标量 ----
    if len(hs_size) >= 3:
        tgt_h = int(round(sum(hs_size) / len(hs_size)))
        tgt_x = med(ox_size)
        tgt_y = med(oy_size)
        tgt_w = tgt_h
        src_tag = "标尺字 %d/%d" % (len(hs_size), len(size_cps))
    else:
        tgt_h = pct(hs_all, 0.75)
        tgt_w = pct(ws_all, 0.75)
        tgt_x = 0
        tgt_y = max(0, (chh - tgt_h) // 2)
        src_tag = "分位数 p75"
    tgt_w = max(tgt_w, tgt_h)

    # ---- 拟合 em：必须用与实际输出完全相同的管线测量 ----
    # 实际渲染 = SS 超采样 -> LANCZOS 缩到 cell -> BC5 量化 -> 阈值 64 判定墨迹。
    # 若只按"名义 bbox"拟合，抗锯齿边缘会被阈值吃掉 1~2px，字号就系统性偏小。
    big_w = cw * SS * 2
    big_h = chh * SS * 2

    def probe(emv):
        """按真实管线渲染并测量 -> [(x,y,w,h)] 输出(cell)坐标系"""
        try:
            f = ImageFont.truetype(ttf, emv * SS)
        except Exception:
            return []
        out = []
        for ch in sample:
            im = Image.new("L", (big_w, big_h), 0)
            ImageDraw.Draw(im).text((big_w // 2, big_h // 2), ch,
                                    fill=255, font=f, anchor="mm")
            sm = im.resize((big_w // SS, big_h // SS), Image.LANCZOS)
            bb = bbox_of(bytearray(sm.tobytes()), sm.size[0], sm.size[1])
            if bb:
                out.append((bb[0], bb[1],
                            bb[2] - bb[0] + 1, bb[3] - bb[1] + 1))
        return out

    def probe_h(emv):
        r = probe(emv)
        return (sum(t[3] for t in r) / float(len(r))) if r else None

    if em_override:
        em = int(round(em_override))
    else:
        lo = max(6, int(tgt_h * 0.85))
        hi = min(320, int(tgt_h * 1.60) + 3)
        best = None
        for emv in range(lo, hi):
            got = probe_h(emv)
            if got is None:
                continue
            d = abs(got - tgt_h)
            if best is None or d < best[0]:
                best = (d, emv, got)
        if best is None:
            raise ValueError("TTF 无法渲染标尺字符")
        em = best[1]
    got_em = probe_h(em)

    # 位置：墨迹左边距对齐原版的墨迹左边距。
    # 为什么不居中：advance 宽度同为 33，墨迹在格内的横向位置决定字面在
    # 一行文字中的实际落点。左边对齐才能保证整行排版与原版一致。
    # 居中会让每个字向右偏 1px，整行累积偏移。
    rel = probe(em)
    if not rel:
        raise ValueError("无法测量渲染偏移")
    cx_center = big_w // (2 * SS)
    cy_center = big_h // (2 * SS)
    med_ink_x = med([t[0] for t in rel])
    med_ink_y = med([t[1] for t in rel])
    ink_w = med([t[2] for t in rel])
    ink_h = med([t[3] for t in rel])

    # TTF 墨迹左边 相对 em 方框中心 的偏移，反推绘制原点
    off_x = med_ink_x - cx_center
    off_y = med_ink_y - cy_center
    posx = (tgt_x - off_x) * SS
    posy = (tgt_y - off_y) * SS

    # 溢出检查（相对中位字的偏差），仅报告
    ov = [0, 0, 0, 0]
    for t in rel:
        ox = t[0] - med_ink_x
        oy = t[1] - med_ink_y
        L = tgt_x + ox
        T = tgt_y + oy
        R = L + t[2] - 1
        B = T + t[3] - 1
        ov = [max(ov[0], r_med - L), max(ov[1], r_med - T),
              max(ov[2], R - (cw - 1 - r_med)),
              max(ov[3], B - (chh - 1 - r_med))]
    ov = [max(0, v) for v in ov]

    if verbose:
        print("  校准: 标尺=%s (%s)" % (sample, src_tag))
        print("        原版墨迹 %dx%d 于 (%d,%d)  cell=%dx%d height=%d"
              % (tgt_w, tgt_h, tgt_x, tgt_y, cw, chh, jf.height))
        print("        统计样本 %d 字，墨迹高 中位=%d p75=%d 最大=%d"
              % (len(hs_all), med(hs_all), pct(hs_all, 0.75), max(hs_all)))
        print("        描边 r=%d (取自 %d 字, IoU>0.80)" % (r_med, len(rs)))
        print("        TTF 拟合 em=%d（同管线实测墨迹高 %.1f，目标 %d）"
              % (em, got_em or 0, tgt_h))
        print("        实际墨迹 %dx%d 于 (%d,%d)  占高%.1f%%"
              % (ink_w, ink_h,
                 int(round(tgt_x)), int(round(tgt_y)),
                 ink_h * 100.0 / jf.height))
        if max(ov) > 0:
            print("        描边越界(左/上/右/下) %s px  ← 与原版同样贴边，不修正位置"
                  % ov)
    return em, r_med, (posx, posy), (tgt_w, tgt_h, tgt_x, tgt_y)


# ================================================================ TTF 覆盖检查
def ttf_missing(ttf, chars, size=32):
    """返回 TTF 里没有字形的码位集合。
       缺失时 PIL 会画 .notdef（豆腐块/空白），在 replace 模式下会
       毁掉原有的日文字形，所以必须先剔除。"""
    try:
        f = ImageFont.truetype(ttf, size)
    except Exception:
        return set()
    try:
        ref = f.getmask("\U0010FFFD")
        ref_sig = (ref.size, bytes(ref))
    except Exception:
        ref_sig = None
    out = set()
    for c in chars:
        try:
            m = f.getmask(chr(c))
            sig = (m.size, bytes(m))
        except Exception:
            out.add(c)
            continue
        if ref_sig is not None and sig == ref_sig:
            out.add(c)
        elif m.size[0] == 0 or m.size[1] == 0:
            out.add(c)
    return out


# ================================================================ 渲染
class Renderer:
    """用校准得到的 em / 描边半径 / 绘制位置渲染。
       ch1 = 方形核膨胀(ch0, r)，与原版模型一致（实测 IoU 0.98）。"""

    def __init__(self, jf, ttf, em, r, pos):
        self.jf = jf
        self.cw = jf.cell_w
        self.ch = jf.cell_h
        self.em = int(em)
        self.r = max(0, int(r))
        self.font = ImageFont.truetype(ttf, self.em * SS)
        self.pos = (float(pos[0]), float(pos[1]))

    def render(self, ch):
        W, H = self.cw * SS, self.ch * SS
        im = Image.new("L", (W, H), 0)
        # anchor 必须与 calibrate() 的 probe() 一致（都是 "mm"）。
        # PIL 的垂直 "m" = ascender~descender 中点 = em 方框中心，
        # 这正是字体设计里 CJK 字身的定位基准；换成 "la" 会整体右下偏移。
        ImageDraw.Draw(im).text(self.pos, ch, fill=255,
                                font=self.font, anchor="mm")
        a0 = im.resize((self.cw, self.ch), Image.LANCZOS)
        a1 = a0.filter(ImageFilter.MaxFilter(2 * self.r + 1)) if self.r > 0 else a0
        return bytearray(a0.tobytes()), bytearray(a1.tobytes())

    def desc(self):
        return ("em=%d 描边r=%d 位置(%.1f,%.1f)  cell=%dx%d height=%d"
                % (self.em, self.r, self.pos[0], self.pos[1],
                   self.cw, self.ch, self.jf.height))


# ================================================================ 写入字形
def write_glyph(at, jf, gi, a0, a1):
    """把 cell 位图写进图集: 解码受影响块 -> 改格内像素 -> 重编码
       格子跨越块边界时，格外像素保持原值。"""
    cw, chh = jf.cell_w, jf.cell_h
    x0, y0 = jf.cell_xy(gi)
    bx0, bx1 = x0 >> 2, (x0 + cw - 1) >> 2
    by0, by1 = y0 >> 2, (y0 + chh - 1) >> 2
    for by in range(by0, by1 + 1):
        for bx in range(bx0, bx1 + 1):
            blk = at.get(bx, by)
            if len(blk) < 16:
                blk = b"\0" * 16
            v0 = bc4_dec(blk[0:8])
            v1 = bc4_dec(blk[8:16])
            for dy in range(4):
                py = (by << 2) + dy
                cy = py - y0
                if cy < 0 or cy >= chh:
                    continue
                rowbase = cy * cw
                for dx in range(4):
                    px = (bx << 2) + dx
                    cx = px - x0
                    if cx < 0 or cx >= cw:
                        continue
                    k = dy * 4 + dx
                    v0[k] = a0[rowbase + cx]
                    v1[k] = a1[rowbase + cx]
            at.set(bx, by, bc4_enc(v0) + bc4_enc(v1))


# ================================================================ 组装
def build_jfnt(jf, at, h_log, widths, ranges):
    """h_log: 逻辑高（字形行数 x cell_h），写入 height 字段。
       imageSize 用填充后的块行数计算。"""
    # GTX
    g = jf.gtx
    img = bytes(at.d[:at.rows * jf.pitch * 16])
    sb_d = jf.sb[2]
    ng = bytearray(g[:jf.data_off])
    struct.pack_into(">I", ng, sb_d + 0x08, h_log)              # 逻辑高
    struct.pack_into(">I", ng, sb_d + 0x20, len(img))           # imageSize（填充量）
    reg = u32(bytes(ng), sb_d + 0x8C)
    struct.pack_into(">I", ng, sb_d + 0x8C,
                     (reg & ~0x1FFF) | ((h_log - 1) & 0x1FFF))  # height-1（逻辑）
    struct.pack_into(">I", ng, jf.ib[1] + 0x14, len(img))       # 0x0C 块数据长
    tail = g[jf.data_off + jf.ib[3]:]
    ng = bytes(ng) + img + tail

    # jIMG
    total = jf.gtx_off + len(ng)
    total += (-total) % 0x100
    h = bytearray(jf.raw[jf.jimg:jf.jimg + 0x80])
    struct.pack_into(">I", h, 4, total)
    struct.pack_into(">H", h, 0x0A, h_log & 0xFFFF)              # jIMG 高（逻辑）
    struct.pack_into(">I", h, 0x14, len(ng))
    body = bytes(h) + ng + b"\0" * (total - jf.gtx_off - len(ng))
    out = bytearray(body)
    keep = bytes(jf.raw[jf.jimg + 0x20:jf.jimg + 0x24])
    out[0x1C:0x24] = b"\0" * 8
    struct.pack_into(">I", out, 0x1C, zlib.crc32(bytes(out)) & 0xFFFFFFFF)
    out[0x20:0x24] = keep

    # JFNT 头 + 范围表 + 宽度表
    # 范围表必须按起始码位升序：引擎可能用二分查找，乱序会导致查不到而越界。
    rs = sorted((list(r) for r in ranges), key=lambda r: (r[0], r[1]))
    hdr = bytearray(jf.raw[:0x0C]) + struct.pack(">H", len(rs))
    for s, e, gi in rs:
        hdr += struct.pack(">HHH", s, e, gi)
    hdr += struct.pack(">H", len(widths)) + bytes(widths)
    hdr += b"\0" * ((-len(hdr)) % 0x80)
    return bytes(hdr) + bytes(out)


def check_ranges(jf_old, ranges, count):
    """校验范围表：升序、不重叠、字形索引不越界。返回问题列表"""
    probs = []
    rs = sorted((list(r) for r in ranges), key=lambda r: (r[0], r[1]))
    prev = None
    for s, e, gi in rs:
        if s > e:
            probs.append("范围反向 U+%04X..U+%04X" % (s, e))
        if gi + (e - s) >= count:
            probs.append("索引越界 U+%04X..U+%04X gi=%d+%d >= %d"
                         % (s, e, gi, e - s, count))
        if prev is not None and s <= prev[1]:
            probs.append("范围重叠 U+%04X..U+%04X 与 U+%04X..U+%04X"
                         % (prev[0], prev[1], s, e))
        prev = (s, e)
    return probs, rs


def read_cell_ink(at, jf, gi, th=64):
    """扫整个格子统计墨点，用于复核"""
    cw, chh = jf.cell_w, jf.cell_h
    x0, y0 = jf.cell_xy(gi)
    cache = {}
    ink = 0
    for by in range(y0 >> 2, ((y0 + chh - 1) >> 2) + 1):
        for bx in range(x0 >> 2, ((x0 + cw - 1) >> 2) + 1):
            blk = at.get(bx, by)
            cache[(bx, by)] = bc4_dec(blk[0:8]) if len(blk) >= 8 else [0] * 16
    for py in range(y0, y0 + chh):
        cy = py - y0
        for px in range(x0, x0 + cw):
            cx = px - x0
            v = cache[(px >> 2, py >> 2)][(py & 3) * 4 + (px & 3)]
            if v >= th:
                ink += 1
    return ink


# ================================================================ jARC
def yaz0_dec(d):
    n = u32(d, 4)
    out = bytearray()
    src = 16
    while len(out) < n and src < len(d):
        code = d[src]
        src += 1
        for i in range(8):
            if len(out) >= n or src >= len(d):
                break
            if code & (0x80 >> i):
                out.append(d[src])
                src += 1
            else:
                b1 = d[src]
                b2 = d[src + 1]
                src += 2
                dist = ((b1 & 0xF) << 8 | b2) + 1
                m = b1 >> 4
                if m == 0:
                    m = d[src] + 0x12
                    src += 1
                else:
                    m += 2
                for _ in range(m):
                    out.append(out[-dist])
    return bytes(out)


def yaz0_varint(n):
    """Yaz0 头部保留 8 字节，用于回写时填原值"""
    return n


def jarc_unwrap(d):
    """剥掉外层压缩 -> (容器模式, 原始jARC字节, 外层信息)
       模式: 'jcmp' / 'yaz0' / 'raw'"""
    info = {"mode": "raw"}
    if d[:4] == b"jCMP":
        info = {"mode": "jcmp", "hdr": bytes(d[:20]),
                "flags": d[8:12], "raw_size": u32(d, 16)}
        d = zlib.decompress(d[20:20 + u32(d, 12)])
    elif d[:4] == b"Yaz0":
        info = {"mode": "yaz0", "hdr": bytes(d[:16]),
                "raw_size": u32(d, 4)}
        d = yaz0_dec(d)
    if d[:4] != b"jARC":
        raise ValueError("内层不是 jARC（首 4 字节 %r）" % d[:4])
    return info, d


def jarc_entries(raw):
    n = u32(raw, 12)
    ents = []
    for i in range(n):
        o = 16 + i * 16
        off, sz, no, f4 = struct.unpack_from(">IIII", raw, o)
        e = raw.find(b"\0", no, min(len(raw), no + 256))
        ents.append([raw[no:e].decode("utf-8", "replace"), off, sz, f4])
    return ents


def yaz0_enc(data):
    """Yaz0 编码。关键：flag 位含义 — bit=1 是字面量，bit=0 是回引。
       回引: b1 高4位 = len-2（为0则长格式），低4位 = dist-1 的高位
             b2 = dist-1 低8位 ；长格式再多 1 字节 = len-0x12
       已用 11 组数据 roundtrip 验证（含全零/随机/长游程/UTF-16）。"""
    n = len(data)
    out = bytearray(b"Yaz0" + struct.pack(">I", n) + b"\0" * 8)
    i = 0
    while i < n:
        code_pos = len(out)
        out.append(0)
        code = 0
        for bit in range(8):
            if i >= n:
                break
            best_len = 0
            best_dist = 0
            lo = max(0, i - 0x1000)
            p = lo
            while p < i:
                if data[p] == data[i]:
                    L = 0
                    lim = min(0x111, n - i)
                    while L < lim and data[p + L] == data[i + L]:
                        L += 1
                    if L > best_len:
                        best_len = L
                        best_dist = i - p
                        if L >= lim:
                            break
                p += 1
            if best_len >= 3:
                dd = best_dist - 1           # bit 留 0 = 回引
                if best_len <= 0x11:
                    out.append(((best_len - 2) << 4) | (dd >> 8))
                    out.append(dd & 0xFF)
                else:
                    out.append(dd >> 8)
                    out.append(dd & 0xFF)
                    out.append(best_len - 0x12)
                i += best_len
            else:
                code |= 0x80 >> bit          # bit 1 = 字面量
                out.append(data[i])
                i += 1
        out[code_pos] = code
    return bytes(out)


def detect_align(ents):
    offs = [e[1] for e in ents]
    for cand in (0x200, 0x100, 0x80, 0x40, 0x20, 0x10, 8, 4):
        if all(o % cand == 0 for o in offs):
            return cand
    return 4


# ---- 条目级压缩：jARC 里的每个 .jfnt 常常是单独 Yaz0 压缩的 ----
def entry_unwrap(blob):
    """-> (mode, 原始头, 解压后字节)"""
    if blob[:4] == b"Yaz0":
        return "yaz0", bytes(blob[:16]), yaz0_dec(blob)
    if blob[:4] == b"jCMP":
        return "jcmp", bytes(blob[:20]), zlib.decompress(blob[20:20 + u32(blob, 12)])
    return "raw", b"", blob


def entry_wrap(mode, hdr, raw):
    if mode == "yaz0":
        return yaz0_enc(raw)
    if mode == "jcmp":
        comp = zlib.compress(raw, 9)
        h = bytearray(hdr)
        struct.pack_into(">I", h, 4, 20 + len(comp))
        struct.pack_into(">I", h, 12, len(comp))
        struct.pack_into(">I", h, 16, len(raw))
        return bytes(h) + comp
    return raw


def jarc_write(raw, ents, blobs, align):
    n = len(ents)
    hdr_size = 16 + n * 16
    names = bytearray()
    noffs = []
    for name, _o, _s, _f in ents:
        noffs.append(hdr_size + len(names))
        names += name.encode("utf-8") + b"\0"
    data_start = hdr_size + len(names)
    data_start += (-data_start) % align
    offs = []
    cur = data_start
    for i in range(n):
        offs.append(cur)
        cur += len(blobs[i])
        if i != n - 1:
            cur += (-cur) % align
    total = cur
    out = bytearray(b"jARC" + struct.pack(">III", total, u32(raw, 8), n))
    for i, (name, _o, _s, f4) in enumerate(ents):
        out += struct.pack(">IIII", offs[i], len(blobs[i]), noffs[i], f4)
    out += names
    out += b"\0" * (data_start - len(out))
    for i in range(n):
        if len(out) != offs[i]:
            raise RuntimeError("jARC 偏移错位 %d != %d" % (len(out), offs[i]))
        out += blobs[i]
        if i != n - 1:
            out += b"\0" * ((-len(out)) % align)
    return bytes(out)


def is_cjk_ideograph(cp):
    """只认汉字本体。ASCII/数字/拉丁/中日标点/假名/全角符号一律排除。
       原版字体里这些字形本来就是对的，重画会改变字距与风格。"""
    return (0x3400 <= cp <= 0x4DBF        # 扩展 A
            or 0x4E00 <= cp <= 0x9FFF      # 基本区
            or 0xF900 <= cp <= 0xFAFF      # 兼容汉字
            or 0x20000 <= cp <= 0x3FFFF)   # 扩展 B 及以后


# ================================================================ 单字体处理
def process(path, chars, ttf, mode, dry, verify, em=None,
            ol=None, cjk_only=False, noop=False):
    d = Path(path).read_bytes()
    jf = JFNT(d)
    if jf.fmt != 0x35:
        raise ValueError("仅支持 BC5(0x35)，此字体 0x%02X" % jf.fmt)
    if jf.tile != 4:
        raise ValueError("仅支持 tileMode 4，此字体 %d" % jf.tile)

    # --noop: 原样重写，用来验证写入器是否忠实（应产出逐字节相同的结果）
    if noop:
        at0 = Atlas(jf.gtx[jf.data_off:jf.data_off + jf.ib[3]],
                    jf.pitch, jf.rows)
        out = build_jfnt(jf, at0, jf.tex_h,
                         list(jf.widths), [list(r) for r in jf.ranges])
        print("\n=== %s ===" % Path(path).name)
        print("  [noop] 原样重写 %d -> %d B" % (len(d), len(out)))
        if out == d:
            print("  逐字节一致 OK —— 写入器忠实，未引入任何改动")
        else:
            n = min(len(d), len(out))
            first = next((k for k in range(n) if d[k] != out[k]), n)
            ndiff = sum(1 for k in range(n) if d[k] != out[k])
            print("  [!] 不一致：长度 %d vs %d，首个差异 @0x%X，共 %d 字节不同"
                  % (len(d), len(out), first, ndiff))
            lo = max(0, first - 16)
            hi = min(n, first + 32)
            print("    原: %s" % " ".join("%02X" % x for x in d[lo:hi]))
            print("    新: %s" % " ".join("%02X" % x for x in out[lo:hi]))
            print("        %s" % (" " * (3 * (first - lo)) + "^^"))
            # 报告差异集中区间
            segs = []
            k = 0
            while k < n:
                if d[k] != out[k]:
                    s = k
                    while k < n and d[k] != out[k]:
                        k += 1
                    segs.append((s, k - 1))
                else:
                    k += 1
            if segs:
                print("    差异区间 %d 段，前 8 段: %s"
                      % (len(segs), ", ".join("0x%X-0x%X" % (s, e)
                                              for s, e in segs[:8])))
        return out

    chars = {c for c in chars if not chr(c).isspace()
             and not (PUA_LO <= c <= PUA_HI) and c >= 0x20}

    skipped = set()
    rescued = set()
    if cjk_only:
        skipped = {c for c in chars if not is_cjk_ideograph(c)}
        chars = {c for c in chars if is_cjk_ideograph(c)}
        # 排除非汉字的初衷是"保留原版已有字形"。若原版本身没有这个字符，
        # 排除就等于永久缺字，反而更糟 —— 这种必须补进来，用 TTF 画。
        rescued = {c for c in skipped if c not in jf.cmap}
        if rescued:
            chars |= rescued
            skipped -= rescued

    # 范围表用 u16 存码位，超出 BMP 的字符无法表达，必须剔除
    over = {c for c in chars if c > 0xFFFF}
    if over:
        chars = chars - over
        skipped |= over
        print("  [!] %d 个字符超出 U+FFFF，范围表(u16)无法表达，已跳过"
              % len(over))

    # TTF 缺字预检：缺失的字一律不碰（否则会用豆腐块覆盖掉原日文字形）
    lack = ttf_missing(ttf, chars)
    if lack:
        chars = chars - lack

    have = {c for c in chars if c in jf.cmap}
    miss = {c for c in chars if c not in jf.cmap}

    do_rep = mode in ("replace", "both")
    do_add = mode in ("add", "both")
    rep = sorted(have) if do_rep else []
    add = sorted(miss) if do_add else []
    n_new = jf.count + len(add)

    print("\n=== %s ===" % Path(path).name)
    print("  cell=%dx%d cols=%d 字形=%d 范围=%d"
          % (jf.cell_w, jf.cell_h, jf.cols, jf.count, len(jf.ranges)))
    if cjk_only and (skipped or rescued):
        if skipped:
            s = sorted(skipped)
            asc = [c for c in s if c < 0x80]
            oth = [c for c in s if c >= 0x80]
            print("  --cjk-only: 跳过 %d 个非汉字（保留原版字形）" % len(skipped))
            if asc:
                print("    ASCII/数字: %s" % "".join(chr(c) for c in asc[:70]))
            if oth:
                print("    其他符号:   %s" % "".join(chr(c) for c in oth[:70]))
            # 最终仍被跳过的字符必须原版本来就有，否则会一直缺字
            gone = sorted(c for c in skipped if c not in jf.cmap)
            if gone:
                print("    [!] 其中 %d 个本字体原本也没有，会缺字: %s"
                      % (len(gone), "".join(chr(c) for c in gone[:50])))
                print("        %s"
                      % " ".join("U+%04X" % c for c in gone[:20]))
            elif not rescued:
                print("    这些字符在原版字体里都有，不会缺字")
        if rescued:
            print("  例外补入 %d 个（原版也没有，排除就会缺字，用 TTF 绘制）: %s"
                  % (len(rescued), "".join(chr(c) for c in sorted(rescued))))
            print("    %s"
                  % " ".join("U+%04X" % c for c in sorted(rescued)))
    print("  图集 %dpx x %dpx  pitch=%d块  imageSize=%d  数据=%d,"
          % (jf.tex_w, jf.tex_h, jf.pitch, jf.img_size, jf.ib[3]))
    print("  需要 %d 字：替换 %d，追加 %d"
          % (len(chars), len(rep), len(add)))
    if lack:
        show = "".join(chr(c) for c in sorted(lack)[:60])
        print("  [!] TTF 缺 %d 个字形，已跳过（不会破坏原字形）:" % len(lack))
        print("      %s%s" % (show, " ..." if len(lack) > 60 else ""))

    rows_old = jf.rows
    h_log, rows_log, rb, h_pad, est = jf.geom_for(n_new)
    grow = max(rows_old, rb)
    print("  字形 %d -> %d  逻辑 %d 行 x %d px = 高 %d（原 %d）"
          % (jf.count, n_new, rows_log, jf.cell_h, h_log, jf.tex_h))
    print("  块行 %d -> %d（填充高 %d px）  图集 %d B -> %d B"
          % (rows_old, grow, h_pad, jf.ib[3], est))
    if est > 48 * 1024 * 1024:
        print("  [!] 图集 %d MB，实机内存风险" % (est // 1048576))
    if not rep and not add:
        print("  无需改动")
        return None

    # 校准（从原版字体实测，不依赖经验系数）
    calib_at = Atlas(jf.gtx[jf.data_off:jf.data_off + jf.ib[3]],
                     jf.pitch, rows_old)
    em_use, r_auto, pos, _tgt = calibrate(jf, calib_at, ttf,
                                          em_override=em)
    r_med = int(ol) if ol is not None else r_auto

    # ---- 笔画粗细对比（换字重时判断描边是否还相称）----
    # 取样必须是"原版里已存在的汉字"，且要均匀散布。
    # 早期版本取 sorted(rep+add)[:12]，结果取到 ASCII/标点/假名这些稀疏字形，
    # 算出来的"原版密度"与汉字无关，结论完全失真。
    try:
        _f = ImageFont.truetype(ttf, int(em_use) * SS)
        _W, _H = jf.cell_w * SS * 2, jf.cell_h * SS * 2
        _cand = sorted(c for c in set(rep) & set(jf.cmap)
                       if 0x4E00 <= c <= 0x9FFF)
        if not _cand:
            _cand = sorted(c for c in set(rep) & set(jf.cmap)
                           if 0x3400 <= c <= 0x9FFF)
        if _cand:
            _st = max(1, len(_cand) // 12)
            _cps = _cand[::_st][:12]
        else:
            _cps = []

        def _dens(plane):
            """包围盒内墨迹占比"""
            bb = bbox_of(plane, jf.cell_w, jf.cell_h)
            if not bb:
                return None
            n = a = 0
            for y in range(bb[1], bb[3] + 1):
                row = y * jf.cell_w
                for x in range(bb[0], bb[2] + 1):
                    a += 1
                    if plane[row + x] >= 64:
                        n += 1
            return (n / float(a)) if a else None

        def _stroke(plane):
            """反复 3x3 腐蚀，估算最粗笔画宽度（px）"""
            cur = plane
            base = sum(1 for v in plane if v >= 64)
            if base <= 0:
                return 0
            rounds = 0
            while rounds < 40:
                nxt = bytearray(len(cur))
                for y in range(1, jf.cell_h - 1):
                    row = y * jf.cell_w
                    up, dn = row - jf.cell_w, row + jf.cell_w
                    for x in range(1, jf.cell_w - 1):
                        i = row + x
                        if cur[i] < 64:
                            continue
                        if (cur[up + x - 1] >= 64 and cur[up + x] >= 64
                                and cur[up + x + 1] >= 64
                                and cur[row + x - 1] >= 64
                                and cur[row + x + 1] >= 64
                                and cur[dn + x - 1] >= 64
                                and cur[dn + x] >= 64
                                and cur[dn + x + 1] >= 64):
                            nxt[i] = 255
                cur = nxt
                rounds += 1
                n = sum(1 for v in cur if v >= 64)
                if n <= base * 0.10 or n == 0:
                    break
            return rounds * 2

        _nd = _ns = 0
        _nm = 0
        for _cp in _cps:
            _im = Image.new("L", (_W, _H), 0)
            ImageDraw.Draw(_im).text(pos, chr(_cp), fill=255,
                                     font=_f, anchor="mm")
            _sm = _im.resize((jf.cell_w, jf.cell_h), Image.LANCZOS)
            _px = bytearray(_sm.tobytes())
            _d = _dens(_px)
            if _d is None:
                continue
            _nd += _d
            _ns += _stroke(_px)
            _nm += 1

        _od = _os = 0
        _om = 0
        for _cp in _cps:
            _gi = jf.cmap.get(_cp)
            if _gi is None or _gi >= jf.count:
                continue
            _p = read_cell_plane(calib_at, jf, _gi, 0)
            _d = _dens(_p)
            if _d is None:
                continue
            _od += _d
            _os += _stroke(_p)
            _om += 1

        if _nm and _om:
            new_d, old_d = _nd / _nm, _od / _om
            new_s, old_s = _ns / float(_nm), _os / float(_om)
            print("  笔画: 墨水率 原版 %.1f%% TTF %.1f%% (x%.2f)   采样 %d 汉字: %s"
                  % (old_d * 100, new_d * 100,
                     (new_d / old_d) if old_d else 0, len(_cps),
                     "".join(chr(c) for c in _cps[:12])))
            print("        笔画宽 原版 %.1fpx TTF %.1fpx (x%.2f)"
                  % (old_s, new_s, (new_s / old_s) if old_s else 0))
            ratio = (new_d / old_d) if old_d else 1.0
            sratio = (new_s / old_s) if old_s else 1.0
            if ratio > 1.15 or sratio > 1.15:
                print("        ← 比原版粗 %.0f%%，该字重偏重；描边 %dpx 占笔画 %.0f%%"
                      % ((max(ratio, sratio) - 1) * 100, r_med,
                         3.0 / new_s * 100 if new_s else 0))
            elif ratio < 0.88 or sratio < 0.88:
                print("        ← 比原版细 %.0f%%，该字重偏轻；描边 %dpx 占笔画 %.0f%%"
                      % ((1 - min(ratio, sratio)) * 100, r_med,
                         3.0 / new_s * 100 if new_s else 0))
            else:
                print("        ✓ 字重与原版吻合（偏差 <15%%），描边 %dpx 占笔画 %.0f%%"
                      % (r_med, 3.0 / new_s * 100 if new_s else 0))
        else:
            print("  （笔画对比跳过：本字体没有可用于对比的原版汉字）")
    except Exception as e:
        print("  （笔画对比跳过: %s）" % e)

    if dry:
        rd = Renderer(jf, ttf, em_use, r_med, pos)
        print("  将用: %s" % rd.desc())
        print("  [dry] 不构建")
        return None

    at = Atlas(jf.gtx[jf.data_off:jf.data_off + jf.ib[3]], jf.pitch, rows_old)
    at.grow(grow)
    rd = Renderer(jf, ttf, em_use, r_med, pos)
    print("  渲染: %s" % rd.desc())

    # ---- 替换 ----
    for k, cp in enumerate(rep):
        gi = jf.cmap[cp]
        if gi >= jf.count:
            continue
        a0, a1 = rd.render(chr(cp))
        write_glyph(at, jf, gi, a0, a1)
        if (k + 1) % 200 == 0:
            sys.stdout.write("\r    替换 %d/%d" % (k + 1, len(rep)))
            sys.stdout.flush()
    if rep:
        print("\r    替换 %d/%d 完成" % (len(rep), len(rep)))

    # ---- 追加 ----
    # 新字形的 advance 宽度：取原版已有汉字宽度的中位数，
    # 而不是 max_w。max_w 是"最大墨迹宽"，用它当 advance 会让字距偏宽。
    cjk_w = [jf.widths[jf.cmap[c]] for c in sorted(jf.cmap)
             if 0x4E00 <= c <= 0x9FFF and jf.cmap.get(c, 1 << 30) < jf.count]
    if cjk_w:
        cjk_w.sort()
        w_new = cjk_w[len(cjk_w) // 2]
        print("  追加宽度: 原版汉字 advance 中位=%d（max_w=%d，样本 %d）"
              % (w_new, jf.max_w, len(cjk_w)))
    else:
        w_new = jf.max_w
        print("  追加宽度: 无参考汉字，用 max_w=%d" % w_new)
    widths = list(jf.widths)
    ranges = [list(r) for r in jf.ranges]
    for k, cp in enumerate(add):
        gi = jf.count + k
        a0, a1 = rd.render(chr(cp))
        write_glyph(at, jf, gi, a0, a1)
        widths.append(w_new)
        merged = False
        for r in ranges:
            if r[1] + 1 == cp and r[2] + (cp - r[0]) == gi:
                r[1] = cp
                merged = True
                break
        if not merged:
            ranges.append([cp, cp, gi])
        if (k + 1) % 100 == 0:
            sys.stdout.write("\r    追加 %d/%d" % (k + 1, len(add)))
            sys.stdout.flush()
    if add:
        print("\r    追加 %d/%d 完成" % (len(add), len(add)))

    out = build_jfnt(jf, at, h_log, widths, ranges)
    print("  文件 %d -> %d B（%+.2f MB）"
          % (len(d), len(out), (len(out) - len(d)) / 1048576.0))

    # 范围表结构校验（升序/不重叠/索引不越界）
    probs, rs = check_ranges(jf, ranges, len(widths))
    was_sorted = (rs == [list(r) for r in ranges])
    print("  范围表: %d 段 %s — %s"
          % (len(rs),
             "(原顺序已升序)" if was_sorted else "(已重排为升序)",
             "结构正常" if not probs else "**问题 %d 项**" % len(probs)))
    for pb in probs[:6]:
        print("    [!] %s" % pb)

    # ---- 复核 ----
    try:
        nf = JFNT(out)
        ok_r = sum(1 for c in rep + add if c in nf.cmap)
        print("  复核: 字形=%d 范围=%d" % (nf.count, len(nf.ranges)))
        print("        高=%d（原 %d，+%d）" % (nf.tex_h, jf.tex_h, nf.tex_h - jf.tex_h))
        print("        imageSize=%d  ÷32768=%s"
              % (nf.img_size,
                 "整数OK" if nf.img_size % 32768 == 0 else "**非整数异常**"))
        print("        块行=%d  ÷16=%s"
              % (nf.rows, "整数OK" if nf.rows % 16 == 0 else "**非整数异常**"))
        print("        码位可查 %d/%d" % (ok_r, len(rep) + len(add)))
        if verify:
            na = Atlas(nf.gtx[nf.data_off:nf.data_off + nf.ib[3]], nf.pitch, nf.rows)
            blank = []
            for cp in rep[:20] + add[:20]:
                gi = nf.cmap[cp]
                ink = read_cell_ink(na, nf, gi)
                if ink < 5:
                    blank.append((cp, ink))
            print("        --verify: 抽查 %d 个，空白 %d 个"
                  % (min(40, len(rep) + len(add)), len(blank)))
            if blank:
                print("          空白码位: %s"
                      % " ".join("U+%04X(%d)" % (c, k) for c, k in blank[:10]))

            # 替换的字：检查墨迹留白（左/上/右/下），并与原版对比
            cjk_rep = [c for c in rep if c >= 0x4E00]
            sample_rep = (cjk_rep or rep)[:40]
            clip = []
            dxs = []
            dys = []
            m_orig = [999, 999, 999, 999]     # 左 上 右 下
            m_new = [999, 999, 999, 999]
            for cp in sample_rep:
                oi = jf.cmap.get(cp)
                ni = nf.cmap.get(cp)
                if oi is None or ni is None or oi >= jf.count:
                    continue
                ob = bbox_of(read_cell_plane(calib_at, jf, oi, 0),
                             jf.cell_w, jf.cell_h)
                nb = bbox_of(read_cell_plane(na, nf, ni, 0),
                             nf.cell_w, nf.cell_h)
                if not nb:
                    continue
                # 两边统一口径：都按"墨迹留白 - 描边半径"算
                mo = [nf.cell_w - 1 - nb[2] - r_med,
                      nf.cell_h - 1 - nb[3] - r_med]
                mg = [nb[0] - r_med, nb[1] - r_med] + mo
                for i in range(4):
                    m_new[i] = min(m_new[i], mg[i])
                # 墨迹本身跑到格子外 -> 真裁切
                if nb[0] < 0 or nb[1] < 0 or nb[2] >= nf.cell_w \
                        or nb[3] >= nf.cell_h:
                    clip.append(cp)
                if ob:
                    dxs.append(nb[0] - ob[0])
                    dys.append(nb[1] - ob[1])
                    og = [ob[0] - r_med, ob[1] - r_med,
                          jf.cell_w - 1 - ob[2] - r_med,
                          jf.cell_h - 1 - ob[3] - r_med]
                    for i in range(4):
                        m_orig[i] = min(m_orig[i], og[i])
            if clip:
                print("        [!] %d 个字形墨迹超出格子: %s"
                      % (len(clip),
                         " ".join("U+%04X" % c for c in clip[:8])))
            else:
                print("        --verify 裁切: 无（墨迹均在格内）")
            if dxs:
                def mdn(a):
                    a = sorted(a)
                    return a[len(a) // 2]
                print("        --verify 位置: Δx 中位=%+d Δy 中位=%+d  "
                      "范围 x[%+d,%+d] y[%+d,%+d]"
                      % (mdn(dxs), mdn(dys),
                         min(dxs), max(dxs), min(dys), max(dys)))
                print("        --verify 留白(左/上/右/下) 新版 %s  原版 %s"
                      % (m_new, m_orig))
                # 原版本身就有负余量（描边顶到格边并轻微裁切）。
                # 1px 级差异发生在描边最外缘的抗锯齿上，肉眼不可辨，
                # 只有 >=2px 才值得警告。
                worse = [m_new[i] - m_orig[i] for i in range(4)
                         if m_orig[i] < 900 and m_new[i] < m_orig[i]]
                if worse and min(worse) <= -2:
                    print("          [!] 比原版多溢出 %d px" % (-min(worse)))
                elif worse:
                    print("          （比原版多溢出 1 px，在描边抗锯齿边缘，可忽略）")
        lost = [hex(c) for c in (0xE045, 0xE046) if c in jf.cmap and c not in nf.cmap]
        if lost:
            print("        [!] PUA 丢失: %s" % lost)
        else:
            print("        PUA E045/E046 映射保留")
    except Exception as e:
        print("  [!] 复核失败: %s: %s" % (type(e).__name__, e))
    return out


# ================================================================ 主
def main():
    argv = sys.argv[1:]
    fonts_dir = r"update\content\fonts"
    chars_path = ""
    ttf = str(HERE.parent / "fonts" / "AlibabaPuHuiTi-3-75-SemiBold.ttf")
    out_dir = ""
    jarc = ""
    only = ""
    mode = "both"
    dry = False
    verify = False
    all_chars = False
    cjk_only = False
    noop = False
    ttf_map = {}        # 字体名 -> TTF 路径（覆盖全局 --ttf）
    em = None
    ol = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--fonts":
            fonts_dir = argv[i + 1]; i += 2
        elif a == "--chars":
            chars_path = argv[i + 1]; i += 2
        elif a == "--ttf":
            ttf = argv[i + 1]; i += 2
        elif a == "--ttf-for":
            # --ttf-for style_font_DB_L_3.jfnt,path/to/a.ttf [style_font_EB_L_3.jfnt,path/to/b.ttf]
            # 一个 --ttf-for 可带多组，用空格分隔不够明确，故用 / 分隔多组
            for grp in argv[i + 1].split("/"):
                grp = grp.strip()
                if not grp:
                    continue
                if "," not in grp:
                    print("[!] --ttf-for 需要 '字体名,路径' 形式: %s" % grp)
                    continue
                fn, fp = grp.split(",", 1)
                ttf_map[fn.strip()] = fp.strip()
            i += 2
        elif a in ("-o", "--out"):
            out_dir = argv[i + 1]; i += 2
        elif a == "--jarc":
            jarc = argv[i + 1]; i += 2
        elif a == "--font":
            only = (only + "," + argv[i + 1]) if only else argv[i + 1]
            i += 2
        elif a == "--mode":
            mode = argv[i + 1]; i += 2
        elif a == "--outline" or a == "--ol":
            ol = float(argv[i + 1]); i += 2
        elif a == "--em":
            em = float(argv[i + 1]); i += 2
        elif a == "--em-ratio":
            em = None
            print("[提示] --em-ratio 已废弃，改用自动校准；需要固定字号请用 --em")
            i += 2
        elif a == "--ol-ratio":
            ol = None
            print("[提示] --ol-ratio 已废弃；需要用 --ol <像素半径>")
            i += 2
        elif a == "--dry":
            dry = True; i += 1
        elif a == "--verify":
            verify = True; i += 1
        elif a == "--all-chars":
            all_chars = True; i += 1
        elif a == "--cjk-only":
            cjk_only = True; i += 1
        elif a == "--noop":
            noop = True; i += 1
        else:
            i += 1

    if not Path(ttf).exists():
        print("[错误] TTF 不存在: %s" % ttf)
        print("  常见: msyh.ttc  simhei.ttf  Deng.ttf  SourceHanSansSC-Regular.otf")
        return 1
    if mode not in ("replace", "add", "both"):
        print("--mode 只能是 replace / add / both")
        return 1

    fdir = Path(fonts_dir)
    if not fdir.exists():
        print("[错误] 字体目录不存在: %s" % fdir)
        return 1

    # 字符集来源
    union = set()
    per_font = {}
    if all_chars and chars_path:
        p = Path(chars_path)
        if not p.exists():
            print("[错误] 字符表不存在: %s" % p)
            return 1
        union = {ord(c) for c in p.read_text(encoding="utf-8") if not c.isspace()}
        print("统一字符集 %d 字 <- %s" % (len(union), p))
    else:
        cdir = Path(chars_path) if chars_path else None
        if cdir and cdir.is_dir():
            for f in cdir.glob("*.txt"):
                cs = {ord(c) for c in f.read_text(encoding="utf-8") if not c.isspace()}
                per_font[f.stem] = cs
            print("按字体读取字符表: %d 个" % len(per_font))
        elif cdir and cdir.is_file():
            union = {ord(c) for c in cdir.read_text(encoding="utf-8") if not c.isspace()}
            print("统一字符集 %d 字 <- %s" % (len(union), cdir))
        else:
            print("[错误] 需要 --chars <目录 或 txt>")
            return 1

    print("=" * 92)
    print("字体目录 %s   TTF %s   模式 %s" % (fdir, ttf, mode))
    if only:
        print("仅处理: %s" % only)
    if out_dir:
        print("输出 %s" % out_dir)
    if dry:
        print("[dry 试运行]")

    only_set = set()
    if only:
        for part in only.replace(";", ",").split(","):
            part = part.strip()
            if part:
                only_set.add(part if part.lower().endswith(".jfnt") else part + ".jfnt")
                only_set.add(part)
    built = {}
    for p in sorted(fdir.glob("*.jfnt")):
        if only_set and p.name not in only_set:
            continue
        cs = per_font.get(p.name, union)
        if not cs and not noop:
            print("\n=== %s === 没有字符表，跳过" % p.name)
            continue
        # 按字体选用 TTF（--ttf-for 覆盖全局 --ttf）
        ttf_use = ttf_map.get(p.name, ttf)
        if p.name in ttf_map:
            print("\n[TTF] %s 使用 %s" % (p.name, ttf_use))
            if not Path(ttf_use).exists():
                print("  [!] 文件不存在，跳过该字体: %s" % ttf_use)
                continue
        try:
            res = process(str(p), cs or set(), ttf_use, mode, dry, verify,
                          em, ol, cjk_only, noop)
        except Exception as e:
            print("\n=== %s === 失败: %s: %s" % (p.name, type(e).__name__, e))
            continue
        if res is not None:
            built[p.name] = res
            if out_dir:
                op = Path(out_dir)
                op.mkdir(parents=True, exist_ok=True)
                (op / p.name).write_bytes(res)
        elif dry and out_dir:
            pass

    if out_dir and dry:
        print("\n[dry] 未写任何文件")

    # 体积对照（实机内存是死机的首要嫌疑，必须可核算）
    if built and not dry:
        tot = sum(len(b) for b in built.values())
        print("\n" + "=" * 92)
        print("体积账（未压缩 jfnt 是实机要分配的量）")
        print("=" * 92)
        for nm, blob in sorted(built.items()):
            print("  %-36s %12d B  %8.2f MB" % (nm, len(blob),
                                                 len(blob) / 1048576.0))
        print("  %-36s %12d B  %8.2f MB" % ("合计", tot, tot / 1048576.0))
        print("  参照: 方案A(2 槽各 17965KB) = 35.07 MB，不死机")
        print("        方案B(5 个字体)        = 60+ MB，挑战界面死机")
        print("  注: 实机同一时刻只会加载当屏用到的字体，")
        print("     所以「总和」仅作粗略参照，不等于峰值占用")
        if tot > 40 * 1048576:
            print("  [!] 已超过方案A 的水平，注意实机内存")

    # 部署清单（韩化文档: 独立 .jfnt 与 font_data.jarc 内副本必须同时更新）
    if built and not dry:
        print("\n" + "=" * 92)
        print("部署清单（两份副本必须同时放进游戏，缺一不可）")
        print("=" * 92)
        for nm, blob in sorted(built.items()):
            print("  [1] 独立字体  update\\content\\fonts\\%s   %d B"
                  % (nm, len(blob)))
        print("  [2] 归档     update\\content\\fonts\\font_data.jarc"
              "%s" % ("" if jarc else "   ← 未回包，需加 --jarc"))
        if not jarc:
            print("      [!] 只放了独立字体是不够的，游戏读的是归档里的副本")
        print("  校验: 两处条目解压后必须字节一致")

    if jarc and built and not dry:
        print("\n=== 回包 %s ===" % jarc)
        src = Path(jarc).read_bytes()
        print("  原文件 %d B  首字节 %s"
              % (len(src), " ".join("%02X" % x for x in src[:12])))
        try:
            info, raw = jarc_unwrap(src)
            ents = jarc_entries(raw)
        except Exception as e:
            print("[错误] 解包失败: %s: %s" % (type(e).__name__, e))
            print("  提示: 若外层既非 jCMP 也非 Yaz0，请把原文件路径贴回来")
            return 1
        print("  外层模式 %s  内部 jARC %d B  条目 %d"
              % (info["mode"], len(raw), len(ents)))

        align = detect_align(ents)
        print("  原 jARC 对齐 0x%X" % align)

        # 先探测每个条目的压缩方式（关键：jARC 内条目常是单独 Yaz0）
        print("\n  条目压缩探测:")
        modes = {}
        plains = {}
        for name, off, sz, f4 in ents:
            blob = raw[off:off + sz]
            mode, hdr, plain = entry_unwrap(blob)
            modes[name] = mode
            plains[name] = plain
            print("    %-36s %9d B  %-5s -> %9d B  首字节 %s"
                  % (name, sz, mode, len(plain),
                     " ".join("%02X" % x for x in blob[:6])))

        # Yaz0 编码器自测：先于替换执行，编码器不可靠时能立刻发现
        for name, off, sz, f4 in ents:
            mode, hdr0, plain = entry_unwrap(raw[off:off + sz])
            if mode == "yaz0" and plain[:4] == b"JFNT":
                chunk = plain[:65536]
                re = yaz0_enc(chunk)
                back = yaz0_dec(re)
                orig_ratio = sz / float(max(1, len(plain)))
                new_ratio = len(re) / float(len(chunk))
                print("\n  Yaz0 编码器自测（前 64KB）: 解压->重编码->再解压 %s"
                      % ("一致 OK" if back == chunk else "**不一致，勿用**"))
                print("    原条目压缩比 %.2f:1，重编码 %.2f:1（越接近越好）"
                      % (1.0 / max(1e-9, orig_ratio), 1.0 / max(1e-9, new_ratio)))
                break

        blobs = []
        nrep = 0
        print()
        for name, off, sz, f4 in ents:
            if name in built:
                _m, hdr0, _p = entry_unwrap(raw[off:off + sz])
                newblob = entry_wrap(modes[name], hdr0, built[name])
                blobs.append(newblob)
                nrep += 1
                print("  替换 %-36s %9d -> %9d B (未压 %9d, %s)"
                      % (name, sz, len(newblob), len(built[name]), modes[name]))
            else:
                blobs.append(raw[off:off + sz])
        body = jarc_write(raw, ents, blobs, align)

        if info["mode"] == "jcmp":
            comp = zlib.compress(body, 9)
            hdr = bytearray(info["hdr"])
            struct.pack_into(">I", hdr, 4, 20 + len(comp))
            struct.pack_into(">I", hdr, 12, len(comp))
            struct.pack_into(">I", hdr, 16, len(body))
            out = bytes(hdr) + comp
        elif info["mode"] == "yaz0":
            enc = yaz0_enc(body)
            hdr = bytearray(info["hdr"])
            struct.pack_into(">I", hdr, 4, len(body))
            out = bytes(hdr) + enc[16:]
        else:
            out = body

        op = Path(out_dir or ".") / Path(jarc).name
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_bytes(out)
        print("\n  内部 jARC %d B  外层 %s -> %d B  替换 %d 项"
              % (len(body), info["mode"], len(out), nrep))
        print("  输出 %s" % op)

        # 回读校验：逐条解压后比对
        try:
            info2, raw2 = jarc_unwrap(out)
            ents2 = jarc_entries(raw2)
            ok = 0
            bad = []
            for (nm2, off2, sz2, _f) in ents2:
                if nm2 in built:
                    _m2, _h2, plain2 = entry_unwrap(raw2[off2:off2 + sz2])
                    if plain2 == built[nm2]:
                        ok += 1
                    else:
                        bad.append(nm2)
            print("  回读校验: %d 条目，%d/%d 替换项解压后字节一致%s"
                  % (len(ents2), ok, nrep,
                     "" if ok == nrep else "  [!] 不一致: %s，请勿使用" % bad))
            # JFNT 结构复检
            for (nm2, off2, sz2, _f) in ents2:
                if nm2 in built:
                    _m2, _h2, plain2 = entry_unwrap(raw2[off2:off2 + sz2])
                    nf2 = JFNT(plain2)
                    print("    %-36s 字形=%d 高=%d imageSize=%d"
                          % (nm2, nf2.count, nf2.tex_h, nf2.img_size))
        except Exception as e:
            print("  回读校验失败: %s: %s" % (type(e).__name__, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
