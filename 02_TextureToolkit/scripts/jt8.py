#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
jt8.py - jIMG/GTX 全格式解码 + 清单模式（绝不静默跳过）

相对 jt7 的改动:
  1. 支持 BC2(0x32) / BC4(0x34) / BC5(0x35)
  2. 任何不支持的格式都明确打印，不再 pass 吞掉
  3. --list 只列清单：文件 / 偏移 / 尺寸 / 格式，用于快速定位
  4. BC4 输出灰度图（alpha 形状），BC5 同时输出 ch0/ch1

用法:
  py jt8.py 目录 -r -e jtex,jmdl --list
  py jt8.py 目录 -r -e jtex,jmdl -d out
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


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def jimg_range(d):
    return u32(d, 0x10) & 0xFFFF, u32(d, 0x14)


SF = ["dim", "width", "height", "depth", "numMips", "format", "aa", "use",
      "imageSize", "imagePtr", "mipSize", "mipPtr", "tileMode", "swizzle",
      "alignment", "pitch"]
FMT = {0x1A: "RGBA8", 0x31: "BC1", 0x32: "BC2", 0x33: "BC3",
       0x34: "BC4", 0x35: "BC5"}
BPB = {0x1A: 4, 0x31: 8, 0x32: 16, 0x33: 16, 0x34: 8, 0x35: 16}


def gtx_parse(g):
    pos, blocks = u32(g, 4), []
    while pos + 0x20 <= len(g):
        mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I", g[pos:pos + 0x20])
        if mg != b"BLK{":
            break
        blocks.append(dict(type=typ, data=pos + bhs, size=dsz))
        pos += bhs + dsz
    sb = next((b for b in blocks if b["type"] == 0x0B), None)
    ib = next((b for b in blocks if b["type"] == 0x0C), None)
    if not sb or not ib:
        raise ValueError(f"缺 BLK: {[hex(b['type']) for b in blocks]}")
    surf = dict(zip(SF, struct.unpack(">16I", g[sb["data"]:sb["data"] + 64])))
    return surf, ib


def tm4(x, y, pitch, bpb):
    mt = (x // 32) + (pitch // 32) * (y // 16)

    def b(v, i):
        return (v >> i) & 1

    if bpb == 4:
        o = [b(x,0), b(x,1), b(y,0), b(x,2), b(y,1), b(y,2),
             b(x,3)^b(y,3), b(x,3)^b(y,5), b(x,4)^b(y,4)]
    elif bpb == 8:
        o = [b(x,0), b(y,0), b(x,1), b(x,2), b(y,1),
             b(x,3)^b(y,3), b(x,3)^b(y,5), b(x,4)^b(y,4), b(y,2)]
    else:
        o = [b(y,0), b(x,0), b(x,1), b(x,2),
             b(x,3)^b(y,3), b(x,3)^b(y,5), b(x,4)^b(y,4), b(y,1), b(y,2)]
    v = 0
    for p, bit in enumerate(o):
        v |= bit << p
    return mt * 512 + v


def _c565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def bc1_pal(blk, four=False):
    c0, c1 = blk[0] | (blk[1] << 8), blk[2] | (blk[3] << 8)
    p0, p1 = _c565(c0), _c565(c1)
    if four or c0 > c1:
        return [p0, p1,
                tuple((2*a+b)//3 for a, b in zip(p0, p1)),
                tuple((a+2*b)//3 for a, b in zip(p0, p1))]
    return [p0, p1, tuple((a+b)//2 for a, b in zip(p0, p1)), (0, 0, 0)]


def bc4_pal(blk):
    a0, a1 = blk[0], blk[1]
    if a0 > a1:
        return [a0, a1] + [((7-i)*a0 + i*a1)//7 for i in range(1, 7)]
    return [a0, a1] + [((5-i)*a0 + i*a1)//5 for i in range(1, 5)] + [0, 255]


def _ck(t, p):
    return struct.pack(">I", len(p)) + t + p + struct.pack(">I", zlib.crc32(t+p) & 0xFFFFFFFF)


def write_png(path, w, h, pix, gray=False):
    bpp = 1 if gray else 4
    raw = bytearray()
    stride = w * bpp
    for y in range(h):
        raw.append(0)
        raw += pix[y*stride:(y+1)*stride]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + _ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8,
                                                0 if gray else 6, 0, 0, 0))
                     + _ck(b"IDAT", zlib.compress(bytes(raw), 6))
                     + _ck(b"IEND", b""))


def decode(jimg):
    go, gl = jimg_range(jimg)
    g = jimg[go:go+gl]
    surf, ib = gtx_parse(g)
    fmt = surf["format"] & 0xFF
    if fmt not in BPB:
        raise ValueError(f"未知格式 0x{fmt:02X}")
    if surf["tileMode"] != 4:
        raise ValueError(f"tileMode {surf['tileMode']} != 4")
    W, H, pitch, bpb = surf["width"], surf["height"], surf["pitch"], BPB[fmt]
    data = g[ib["data"]:ib["data"] + ib["size"]]
    rows = len(data) // bpb // pitch
    lin = [[None]*pitch for _ in range(rows)]
    for y in range(rows):
        for x in range(pitch):
            s = tm4(x, y, pitch, bpb) * bpb
            lin[y][x] = data[s:s+bpb]

    rgba = bytearray(W*H*4)
    extra = []

    if fmt == 0x1A:
        for y in range(min(H, rows)):
            for x in range(min(W, pitch)):
                q = lin[y][x]
                o = (y*W+x)*4
                rgba[o:o+4] = q

    elif fmt in (0x31, 0x32, 0x33):
        amask = bytearray(W*H)
        ainfo = None
        if fmt == 0x32:
            amask = bytearray(W*H)
        for by in range(rows):
            for bx in range(pitch):
                blk = lin[by][bx]
                ox, oy = bx*4, by*4
                if fmt == 0x31:
                    pal = bc1_pal(blk)
                    bits = blk[4] | (blk[5]<<8) | (blk[6]<<16) | (blk[7]<<24)
                    av = [255]*16
                elif fmt == 0x33:
                    pal = bc1_pal(blk[8:16], four=True)
                    bits = blk[12] | (blk[13]<<8) | (blk[14]<<16) | (blk[15]<<24)
                    p4 = bc4_pal(blk[0:8])
                    a4 = int.from_bytes(blk[2:8], "little")
                    av = [p4[(a4 >> (3*i)) & 7] for i in range(16)]
                else:                                   # BC2
                    pal = bc1_pal(blk[8:16], four=True)
                    bits = blk[12] | (blk[13]<<8) | (blk[14]<<16) | (blk[15]<<24)
                    a4 = int.from_bytes(blk[0:8], "little")
                    av = [((a4 >> (4*i)) & 0xF)*17 for i in range(16)]
                for i in range(16):
                    x, y = ox + (i & 3), oy + (i >> 2)
                    if x < W and y < H:
                        c = pal[(bits >> (2*i)) & 3]
                        o = (y*W+x)*4
                        rgba[o] = c[0]; rgba[o+1] = c[1]; rgba[o+2] = c[2]
                        rgba[o+3] = av[i]
        if fmt != 0x31:
            amask = bytes(rgba[3::4])
            extra.append(("alpha", amask))

    elif fmt in (0x34, 0x35):
        nch = 2 if fmt == 0x35 else 1
        ch = [bytearray(W*H) for _ in range(nch)]
        for by in range(rows):
            for bx in range(pitch):
                blk = lin[by][bx]
                ox, oy = bx*4, by*4
                for c in range(nch):
                    p4 = bc4_pal(blk[8*c:8*c+8])
                    a4 = int.from_bytes(blk[8*c+2:8*c+8], "little")
                    for i in range(16):
                        x, y = ox + (i & 3), oy + (i >> 2)
                        if x < W and y < H:
                            ch[c][y*W+x] = p4[(a4 >> (3*i)) & 7]
        for i in range(W*H):
            o = i*4
            if nch == 2:
                rgba[o] = ch[0][i]; rgba[o+1] = ch[1][i]
                rgba[o+2] = 0; rgba[o+3] = 255
            else:
                v = ch[0][i]
                rgba[o] = v; rgba[o+1] = v; rgba[o+2] = v; rgba[o+3] = 255
        for i, c in enumerate(ch):
            extra.append((f"ch{i}", bytes(c)))

    return rgba, W, H, surf, fmt, extra


def find_all_jimg(d):
    out, i = [], 0          # 原为 0，append 会抛 AttributeError 并被吞掉 -> 零输出
    while True:
        i = d.find(b"jIMG", i)
        if i < 0:
            return out
        try:
            go, _gl = jimg_range(d[i:])
            size = u32(d, i + 4)
            if d[i+go:i+go+4] == b"Gfx2" and 0x800 < size and i + size <= len(d):
                out.append((i, size))
                i += size
                continue
        except Exception:
            pass
        i += 4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-d", "--out-dir", default="jt8_out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-r", "--recursive", action="store_true")
    ap.add_argument("-e", "--ext", default="jtex,jmdl")
    a = ap.parse_args()

    exts = tuple("." + s.strip().lower() for s in a.ext.split(",") if s.strip())
    files = []
    for p in a.inputs:
        q = Path(p)
        if q.is_file():
            files.append(q)
        elif q.is_dir():
            it = q.rglob("*") if a.recursive else q.glob("*")
            files += sorted(f for f in it if f.is_file() and
                            (not exts or f.suffix.lower() in exts))

    summary = {}
    out = Path(a.out_dir)
    nfiles_with_tex = 0
    ntex = 0

    print(f"# 待扫描文件 {len(files)} 个（后缀 {','.join(exts) or '全部'}）")

    for f in files:
        raw = f.read_bytes()
        spots = find_all_jimg(raw)
        if not spots:
            continue
        nfiles_with_tex += 1
        ntex += len(spots)
        for n, (off, size) in enumerate(spots):
            jimg = raw[off:off+size]
            try:
                go, gl = jimg_range(jimg)
                surf, ib = gtx_parse(jimg[go:go+gl])
                fmt = surf["format"] & 0xFF
                W, H, pitch = surf["width"], surf["height"], surf["pitch"]
                name = FMT.get(fmt, f"0x{fmt:02X}")
                key = (W, H, name)
                summary[key] = summary.get(key, 0) + 1
                if a.list:
                    print(f"{f.stem:42s} [{n}] {W:5d}x{H:<5d} {name:6s} "
                          f"pitch={pitch:4d} mips={surf['numMips']}")
                    continue
                rgba, W, H, surf, fmt, extra = decode(jimg)
                stem = f"{f.stem}__t{n}"
                if fmt in (0x34, 0x35):
                    write_png(out / f"{stem}.png", W, H, rgba, gray=False)
                    for nm, buf in extra:
                        write_png(out / f"{stem}_{nm}.png", W, H, buf, gray=True)
                else:
                    write_png(out / f"{stem}.png", W, H, rgba)
                    for nm, buf in extra:
                        write_png(out / f"{stem}_{nm}.png", W, H, buf, gray=True)
                print(f"{f.stem:42s} [{n}] {W:5d}x{H:<5d} {name:6s} -> {stem}.png"
                      + (f"  ({len(extra)} 通道)" if extra else ""))
            except Exception as e:
                print(f"{f.stem:42s} [{n}] @{off:#x} !! {type(e).__name__}: {e}")

    print(f"\n# 命中文件 {nfiles_with_tex} 个，贴图 {ntex} 张")
    if nfiles_with_tex == 0:
        print("# [警告] 一个 jIMG 都没解析到 —— 路径/后缀不对，或文件未解包")
    if a.list and summary:
        print("\n--- 按尺寸/格式汇总 ---")
        for k in sorted(summary, key=lambda t: -summary[t]):
            print(f"  {k[0]:5d}x{k[1]:<5d} {k[2]:6s}  x{summary[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())