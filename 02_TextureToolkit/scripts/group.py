#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
group.py - 按尺寸分组找同规格的一组贴图（比如「7 张同样大小的滑动页」）

思路:
  一组同用途的图（滑动页、印章套图）尺寸必然完全相同。
  所以把所有贴图按 (宽, 高, 格式) 分组，按组内数量排序，
  再按 --count 精确过滤，就能把目标一网打尽。

  同时支持导出整组缩略表（contact sheet），一眼看完。

用法:
  py group.py out_ui --count 7                    # 找恰好 7 张同尺寸的组
  py group.py out_ui --count 7 --sheet sh7        # 顺便导出缩略表
  py group.py out_ui --min-count 5 --top 30       # 看有哪些多图组
  py group.py out_ui\aot_tex --count 9 --sheet sa  # 印章的 9 个语言版本
  py group.py out_ui --name info                  # 只统计名字含 info 的
"""
import argparse
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import jt8
except ImportError:
    sys.exit("找不到 jt8.py，请放在同一目录")


def ck(t, p):
    return (struct.pack(">I", len(p)) + t + p
            + struct.pack(">I", zlib.crc32(t + p) & 0xFFFFFFFF))


def write_png(path, w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + ck(b"IDAT", zlib.compress(bytes(raw), 6))
                     + ck(b"IEND", b""))


def thumb(rgba, W, H, cw, ch):
    """等比缩放居中，深底"""
    out = bytearray(b"\x20\x20\x20\xff" * (cw * ch))
    if W <= 0 or H <= 0:
        return bytes(out)
    sc = min(cw / W, ch / H)
    tw, th = max(1, int(W * sc)), max(1, int(H * sc))
    ox, oy = (cw - tw) // 2, (ch - th) // 2
    for ty in range(th):
        sy = min(H - 1, ty * H // th)
        base = sy * W * 4
        for tx in range(tw):
            sx = min(W - 1, tx * W // tw)
            s = base + sx * 4
            d = ((oy + ty) * cw + ox + tx) * 4
            out[d:d + 3] = rgba[s:s + 3]
            out[d + 3] = 255
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-r", "--recursive", action="store_true", default=True)
    ap.add_argument("-e", "--ext", default="jtex,jmdl,jarc")
    ap.add_argument("--count", type=int, default=None,
                    help="只看组内恰好 N 张的组")
    ap.add_argument("--min-count", type=int, default=3)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--name", default="", help="只统计名字含此串的图")
    ap.add_argument("--ar-min", type=float, default=0.0,
                    help="最小宽高比，如 1.70 筛 16:9")
    ap.add_argument("--ar-max", type=float, default=0.0,
                    help="最大宽高比，如 1.85")
    ap.add_argument("--min-w", type=int, default=0, help="最小宽度像素")
    ap.add_argument("--max-n", type=int, default=0,
                    help="组内数量上限（配合 --min-count 收窄范围）")
    ap.add_argument("--sheet", default=None, help="导出缩略表到该目录")
    ap.add_argument("--full", action="store_true", help="列表不截断，全部打印")
    ap.add_argument("--export", default=None,
                    help="把每张单独导出成同名 PNG 到该目录")
    ap.add_argument("--cw", type=int, default=180)
    ap.add_argument("--ch", type=int, default=80)
    ap.add_argument("--cols", type=int, default=7)
    a = ap.parse_args()

    exts = tuple("." + s.strip().lower() for s in a.ext.split(",") if s.strip())
    files = []
    for p in a.inputs:
        q = Path(p)
        if q.is_file():
            files.append(q)
        elif q.is_dir():
            files += sorted(f for f in q.rglob("*")
                            if f.is_file() and f.suffix.lower() in exts)
        else:
            print(f"[跳过] 不存在: {p}")

    print(f"# 待扫描 {len(files)} 个文件（后缀 {','.join(exts)}）")
    if not files:
        print("# [警告] 无文件")
        return 1

    groups = defaultdict(list)      # (W,H,fmt) -> [(file, idx, rgba)]
    ntex = nfail = 0

    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        for n, (off, size) in enumerate(jt8.find_all_jimg(raw)):
            ntex += 1
            try:
                surf, ib = jt8.gtx_parse(
                    raw[off + (jt8.u32(raw, off + 0x10) & 0xFFFF):][:jt8.u32(raw, off + 0x14)])
                W, H = surf["width"], surf["height"]
                fmt = surf["format"] & 0xFF
            except Exception:
                nfail += 1
                continue
            if a.name and a.name.lower() not in f.stem.lower():
                continue
            if a.min_w and W < a.min_w:
                continue
            ar = W / H if H else 0
            if a.ar_min and ar < a.ar_min:
                continue
            if a.ar_max and ar > a.ar_max:
                continue
            groups[(W, H, fmt)].append((f, n, off, size))

    print(f"# 贴图 {ntex} 张，解析失败 {nfail}，唯一尺寸组 {len(groups)}")

    # 排序：组内数量降序
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    sel = [(k, v) for k, v in ordered
           if (len(v) == a.count if a.count else len(v) >= a.min_count)]
    if a.max_n:
        sel = [(k, v) for k, v in sel if len(v) <= a.max_n]
    if a.ar_min or a.ar_max:
        print(f"  （宽高比过滤 {a.ar_min or 0:.2f} ~ {a.ar_max or 9.99:.2f}，"
              f"最小宽 {a.min_w}）")
    if a.count:
        print(f"\n# 恰好 {a.count} 张同尺寸的组: {len(sel)} 个")
    else:
        print(f"\n# 组内 >= {a.min_count} 张的组（前 {a.top}）:")

    if not sel:
        print("  （没有匹配的组，试试调整 --count / --min-count）")
        return 0

    for k, v in sel[:a.top]:
        W, H, fmt = k
        name = jt8.FMT.get(fmt, f"0x{fmt:02X}")
        ar = W / H if H else 0
        print(f"\n  {W}x{H}  {name}  宽高比 {ar:.2f}  -- {len(v)} 张")
        lim = len(v) if a.full else 12
        for f, n, off, size in v[:lim]:
            rel = str(f).replace("\\", "/")
            # 只保留 out_ui 之后的路径，便于阅读
            for marker in ("out_ui/", "wev/", "sh_", "Gamefiles/"):
                i = rel.find(marker)
                if i >= 0:
                    rel = rel[i:]
                    break
            print(f"      {size:9,} B  {rel}[{n}]")
        if len(v) > lim:
            print(f"      ... 另有 {len(v)-lim} 张（加 --full 全部列出）")

        if a.export:
            ed = Path(a.export)
            ed.mkdir(parents=True, exist_ok=True)
            done = 0
            for f, n, off, size in v:
                raw = f.read_bytes()
                try:
                    rgba, w2, h2, _s, _f, _e = jt8.decode(raw[off:off + size])
                except Exception as ex:
                    print(f"      导出失败 {f.name}: {ex}")
                    continue
                tag = "" if len(v) == 1 else f"__{n}"
                write_png(ed / f"{f.stem}{tag}.png", w2, h2, rgba)
                done += 1
            print(f"      已单独导出 {done} 张 -> {ed}")

    # 导出缩略表
    if a.sheet:
        outdir = Path(a.sheet)
        for gi, (k, v) in enumerate(sel[:a.top]):
            W, H, fmt = k
            name = jt8.FMT.get(fmt, f"0x{fmt:02X}")
            cols = min(a.cols, len(v))
            rows = (len(v) + cols - 1) // cols
            SW = cols * (a.cw + 2)
            SH = rows * (a.ch + 2)
            canvas = bytearray(b"\x10\x10\x10\xff" * (SW * SH))
            for i, (f, n, off, size) in enumerate(v):
                raw = f.read_bytes()
                try:
                    rgba, w2, h2, _s, _f, _e = jt8.decode(raw[off:off + size])
                except Exception:
                    continue
                th = thumb(rgba, w2, h2, a.cw, a.ch)
                r, c = divmod(i, cols)
                x0, y0 = c * (a.cw + 2) + 1, r * (a.ch + 2) + 1
                for yy in range(a.ch):
                    d = ((y0 + yy) * SW + x0) * 4
                    canvas[d:d + a.cw * 4] = th[yy * a.cw * 4:(yy + 1) * a.cw * 4]
            fn = outdir / f"g{gi:02d}_{W}x{H}_{name}_{len(v)}张.png"
            write_png(fn, SW, SH, canvas)
            print(f"  缩略表 -> {fn}")
        print(f"\n# 缩略表目录: {outdir.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
