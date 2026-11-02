#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r'''
glyphdump.py - 把 JFNT 里任意码位的字形位图画成 ASCII 字符画

用途:
  1) 确认 U+E045 / U+E046 到底是什么图案（应该是 - / + 圆钮里的符号）
  2) 对比"原版字体"与"重建后字体"的同一码位，判断字形是否被覆写
  3) 判断是"位图丢了"还是"索引算错了"（后者会画出一片空白或错位图案）

原理:
  JFNT 头 -> jIMG -> Gfx2 -> BLK 0x0B(表面) / 0x0C(像素)
  像素是 BC5(0x35) + tileMode4，按宏块 32x16 做 pipe/bank XOR 交换。
  只需按需读取目标格子覆盖的块，不必解码整张图集。

用法:
  py glyphdump.py --font update\content\fonts\style_font_DB_3_S.jfnt --cp E045
  py glyphdump.py --font A.jfnt --font2 B.jfnt --cp E045,E046
  py glyphdump.py --font X.jfnt --cp E045 --both      # 同时画 ch0(字身) / ch1(描边)
  py glyphdump.py --font X.jfnt --gi 8048             # 按字形号直取（绕开码位映射）
'''

import sys
import struct

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RAMP = " .:-=+*#%@"   # 由浅到深


def u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ---------------------------------------------------------------- JFNT
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

    def cell_of(self, gi):
        r, c = divmod(gi, self.cols)
        return c * self.cell_w, r * self.cell_h

    def range_of(self, cp):
        for s, e, g in self.ranges:
            if s <= cp <= e:
                return (s, e, g)
        return None


# ---------------------------------------------------------------- GTX
SF = ["dim", "width", "height", "depth", "numMips", "format", "aa", "use",
      "imageSize", "imagePtr", "mipSize", "mipPtr", "tileMode", "swizzle",
      "alignment", "pitch"]


def gtx_parse(g):
    if g[:4] != b"Gfx2":
        raise ValueError("GTX 不是 Gfx2")
    pos = u32(g, 4)
    blocks = []
    while pos + 0x20 <= len(g):
        mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I", g[pos:pos + 0x20])
        if mg != b"BLK{":
            break
        blocks.append(dict(type=typ, hdr=pos, data=pos + bhs, size=dsz))
        pos += bhs + dsz
    sb = next((b for b in blocks if b["type"] == 0x0B), None)
    ib = next((b for b in blocks if b["type"] == 0x0C), None)
    if not sb or not ib:
        raise ValueError("缺 0x0B/0x0C 块")
    surf = dict(zip(SF, struct.unpack(">16I", g[sb["data"]:sb["data"] + 64])))
    return surf, sb, ib


def tm4_addr(xb, yb, pitch_blocks):
    mt = (xb // 32) + (pitch_blocks // 32) * (yb // 16)
    b = lambda v, i: (v >> i) & 1
    order = [b(yb, 0), b(xb, 0), b(xb, 1), b(xb, 2),
             b(xb, 3) ^ b(yb, 3), b(xb, 3) ^ b(yb, 5), b(xb, 4) ^ b(yb, 4),
             b(yb, 1), b(yb, 2)]
    idx = 0
    for p, bit in enumerate(order):
        idx |= bit << p
    return mt * 512 + idx


def bc4_palette(r0, r1):
    if r0 > r1:
        return [r0, r1] + [((8 - k) * r0 + (k - 1) * r1) // 7 for k in range(2, 8)]
    p = [r0, r1] + [((6 - k) * r0 + (k - 1) * r1) // 5 for k in range(2, 6)]
    return p + [0, 255]


def bc4_values(blk8):
    """8 字节 -> 16 个 0..255"""
    r0, r1 = blk8[0], blk8[1]
    bits = int.from_bytes(blk8[2:8], "little")
    pal = bc4_palette(r0, r1)
    out = [0] * 16
    for i in range(16):
        out[i] = pal[(bits >> (3 * i)) & 7]
    return out


class Atlas:
    """按需取块，不解码整图"""

    def __init__(self, jfnt):
        d = jfnt.raw
        j = jfnt.jimg
        jh = d[j:j + 0x80]
        self.go = u32(jh, 0x10) & 0xFFFF
        self.gl = u32(jh, 0x14)
        g = d[j + self.go:j + self.go + self.gl]
        self.surf, self.sb, self.ib = gtx_parse(g)
        self.g = g
        self.fmt = self.surf["format"] & 0xFF
        self.pitch = self.surf["pitch"]
        self.width_px = self.pitch * 4
        self.rows_blocks = self.ib["size"] // 16 // self.pitch
        self.img = g[self.ib["data"]:self.ib["data"] + self.ib["size"]]
        self._cache = {}

    def block(self, bx, by):
        k = (bx, by)
        c = self._cache.get(k)
        if c is not None:
            return c
        if bx >= self.pitch or by >= self.rows_blocks:
            return None
        a = tm4_addr(bx, by, self.pitch)
        off = a * 16
        blk = self.img[off:off + 16]
        if len(blk) < 16:
            return None
        self._cache[k] = blk
        return blk

    def pixel(self, px, py, ch):
        """ch: 0=字身 1=描边"""
        if px < 0 or py < 0 or px >= self.width_px or py >= self.rows_blocks * 4:
            return 0
        bx, by = px // 4, py // 4
        blk = self.block(bx, by)
        if blk is None:
            return 0
        sub = blk[0:8] if ch == 0 else blk[8:16]
        vals = bc4_values(sub)
        k = (py % 4) * 4 + (px % 4)
        return vals[k]

    def cell_bits(self, x0, y0, w, h, ch, binar=64):
        rows = []
        for y in range(h):
            row = bytearray(w)
            for x in range(w):
                row[x] = self.pixel(x0 + x, y0 + y, ch)
            rows.append(bytes(row))
        return rows


def art(rows, tag, binar=64, downsample=1):
    print(f"  --- {tag} ---")
    for r in rows[::downsample]:
        line = []
        for v in r[::downsample]:
            if v < binar:
                line.append(" ")
            else:
                line.append(RAMP[min(len(RAMP) - 1, 1 + v * (len(RAMP) - 2) // 255)])
        print("  |" + "".join(line) + "|")


def dump_ranges(f):
    """列出范围表，标出可能非文字的码位"""
    print(f"\n  范围表 {len(f.ranges)} 段（码位区间 -> 起始字形号 / 数量）:")
    for s, e, g in f.ranges:
        n = e - s + 1
        marks = []
        if s <= 0x7F or e <= 0x7F:
            marks.append("ASCII")
        if any(s <= c <= e for c in (0xE045, 0xE046)):
            marks.append("**含目标符号**")
        if 0xE000 <= s <= 0xF8FF:
            marks.append("PUA")
        if 0x3040 <= s <= 0x30FF or 0x3040 <= e <= 0x30FF:
            marks.append("假名")
        if s >= 0x4E00 and e <= 0x9FFF:
            marks.append("汉字")
        tag = ("  " + ",".join(marks)) if marks else ""
        print("    U+%04X-U+%04X  #%-6d x%-5d%s" % (s, e, g, n, tag))


def dump_font(path, cps, gis, show_both, binar, ds, show_ranges=False):
    d = open(path, "rb").read()
    f = JFNT(d)
    print("=" * 78)
    print(f"{path}")
    print(f"  cell={f.cell_w}x{f.cell_h}  cols={f.cols}  字形={f.count}  "
          f"范围={len(f.ranges)}  jIMG@0x{f.jimg:X}")
    if show_ranges:
        dump_ranges(f)
    try:
        at = Atlas(f)
        print(f"  图集 {at.width_px}px x {at.rows_blocks*4}px  "
              f"format=0x{at.fmt:02X}  pitch={at.pitch}块  数据={at.ib['size']:,} B")
    except Exception as e:
        print(f"  [!] 图集解析失败: {e}")
        return

    targets = []
    for cp in cps:
        gi = f.cmap.get(cp)
        if gi is None:
            print(f"\n  U+{cp:04X}  <不在字符映射中>")
            r = f.range_of(cp)
            if r:
                print(f"    但其码位落在范围 U+{r[0]:04X}-U+{r[1]:04X} base=#{r[2]} 内")
                print(f"    按公式应解析为 #{r[2] + cp - r[0]}"
                      f"（>={f.count} 则越界）")
            else:
                print("    且不在任何范围表中")
            continue
        targets.append((f"U+{cp:04X}", gi))
    for gi in gis:
        targets.append((f"g#{gi}", gi))

    for label, gi in targets:
        print(f"\n  {label}  字形号 #{gi}")
        if gi >= f.count:
            print(f"    [!] 越界：总字形 {f.count}")
            continue
        w = f.widths[gi] if gi < len(f.widths) else -1
        x0, y0 = f.cell_of(gi)
        print(f"    字宽={w}  格子位置=({x0},{y0}) 块=({x0//4},{y0//4})")
        rows0 = at.cell_bits(x0, y0, f.cell_w, f.cell_h, 0, binar)
        nz = sum(1 for r in rows0 for v in r if v >= binar)
        print(f"    ch0 非透明像素 {nz} / {f.cell_w*f.cell_h}")
        art(rows0, "ch0 字身", binar, ds)
        if show_both:
            rows1 = at.cell_bits(x0, y0, f.cell_w, f.cell_h, 1, binar)
            nz1 = sum(1 for r in rows1 for v in r if v >= binar)
            print(f"    ch1 非透明像素 {nz1}")
            art(rows1, "ch1 描边", binar, ds)


def main():
    argv = sys.argv[1:]
    fonts = []
    cps = []
    gis = []
    both = False
    binar = 64
    ds = 1
    show_ranges = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--font", "--font2"):
            fonts.append(argv[i + 1]); i += 2
        elif a == "--cp":
            cps += [int(x.strip().replace("U+", "").replace("0x", ""), 16)
                    for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        elif a == "--gi":
            gis += [int(x) for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        elif a == "--both":
            both = True; i += 1
        elif a == "--ranges":
            show_ranges = True; i += 1
        elif a == "--binar":
            binar = int(argv[i + 1]); i += 2
        elif a == "--ds":
            ds = int(argv[i + 1]); i += 2
        else:
            i += 1

    if not fonts:
        print("需要 --font <路径>")
        return 2
    if not cps and not gis and not show_ranges:
        print("需要 --cp 或 --gi 或 --ranges")
        return 2
    for p in fonts:
        try:
            dump_font(p, cps, gis, both, binar, ds, show_ranges)
        except FileNotFoundError:
            print(f"[错误] 文件不存在: {p}")
        except Exception as e:
            print(f"[错误] {p}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
