#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
stampwear.py - 给印章文字加「磨损油墨」效果（可复用原图真实斑纹）

你只需要:
  在 Paint.NET / GIMP 里画一层【纯色文字】——256x256 透明底，
  橙色字 + 深橙描边，不用管任何磨损效果。
  
脚本负责:
  1. 从原印章测出 油墨色 / 磨损色 / 磨损密度 / 圆心半径
  2. 按半径抹掉原印章内圈的旧文字（圆环完全保留）
  3. 贴上你的文字
  4. 加磨损：blobs(程序化斑点) 或 orig(直接复用原图真实斑纹)

用法:
  :: 只看分析结果
  py stampwear.py --orig png_stamp\aot_stamp001_us_eng__0.png --analyze

  :: 复用原图斑纹（推荐，最像）
  py stampwear.py --orig png_stamp\aot_stamp001_us_eng__0.png ^
      --text edited\found.png --mode orig ^
      --out out_stamp\aot_stamp001_us_eng.png

  :: 程序化斑点 + 指定密度/种子
  py stampwear.py --orig ... --text ... --mode blobs --density 0.15 --seed 7

  :: 转成透明孔（而不是白斑）
  py stampwear.py ... --holes

注意:
  --orig 可以直接给 .jtex，脚本会自动解码；给 PNG 更快。
"""
import argparse
import math
import random
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    sys.exit("需要 Pillow:  pip install pillow")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def sat(c):
    mx, mn = max(c), min(c)
    return (mx - mn) / mx if mx else 0.0


def load_img(p):
    """支持 PNG 和 jtex/jmdl（自动找第一个 jIMG 解码）"""
    p = Path(p)
    if p.suffix.lower() in (".jtex", ".jmdl", ".jarc"):
        import bctext
        raw = p.read_bytes()
        spots = bctext.find_jimgs(raw)
        if not spots:
            raise ValueError(f"{p.name} 里没有 jIMG")
        off, size = spots[0]
        img, meta = bctext.decode(raw[off:off + size])
        return img.convert("RGBA"), meta
    im = Image.open(p)
    im.load()
    return im.convert("RGBA"), None


def geometric(img):
    """求印章圆心和半径（按不透明像素的包围盒）"""
    W, H = img.size
    px = img.load()
    x0, y0, x1, y1 = W, H, -1, -1
    for y in range(H):
        for x in range(W):
            if px[x, y][3] > 32:
                x0 = min(x0, x); x1 = max(x1, x)
                y0 = min(y0, y); y1 = max(y1, y)
    if x1 < 0:
        return W / 2, H / 2, min(W, H) / 2
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0,
            max(x1 - x0, y1 - y0) / 2.0)


def analyze(img):
    """测 油墨色 / 磨损色 / 圆环磨损密度"""
    W, H = img.size
    cx, cy, R = geometric(img)
    px = img.load()

    inks, lights, inked = {}, {}, 0
    ring_tot = ring_light = 0
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            c = (r, g, b)
            l = lum(c)
            rad = math.hypot(x - cx, y - cy)
            in_ring = 0.70 * R <= rad <= 0.97 * R
            if sat(c) > 0.40 and l < 210:
                k = (r >> 3 << 3, g >> 3 << 3, b >> 3 << 3)
                inks[k] = inks.get(k, 0) + 1
                inked += 1
            elif l > 205:
                k = (r >> 4 << 4, g >> 4 << 4, b >> 4 << 4)
                lights[k] = lights.get(k, 0) + 1
                if in_ring:
                    ring_light += 1
            if in_ring:
                ring_tot += 1

    ink = max(inks.items(), key=lambda kv: kv[1])[0] if inks else (236, 96, 20)
    wear = max(lights.items(), key=lambda kv: kv[1])[0] if lights else (250, 248, 244)
    dens = (ring_light / ring_tot) if ring_tot else 0.12
    return dict(cx=cx, cy=cy, R=R, ink=ink, wear=wear,
                density=dens, ink_px=inked,
                ink_ratio=(inked / (W * H)) if W * H else 0)


def wear_mask_from(img, info):
    """从原图提取真实斑纹（布尔表，True=磨损）"""
    W, H = img.size
    px = img.load()
    thr = lum(info["ink"]) + 45
    mask = bytearray(W * H)
    n = 0
    for y in range(H):
        base = y * W
        for x in range(W):
            r, g, b, a = px[x, y]
            if a > 128 and lum((r, g, b)) > thr:
                mask[base + x] = 1
                n += 1
    return mask, n, W, H


def wear_field_blobs(W, H, density, seed, octaves=3, base=8):
    """程序化多尺度斑点，阈值化后恰好命中 density 覆盖率"""
    rnd = random.Random(seed)
    acc = [0.0] * (W * H)
    tot_w = 0.0
    for o in range(octaves):
        k = max(2, base >> o if o < 3 else 1)
        sw, sh = max(1, W // k), max(1, H // k)
        layer = Image.new("L", (sw, sh), 0)
        d = ImageDraw.Draw(layer)
        cnt = max(3, int(sw * sh * 0.08))
        for _ in range(cnt):
            cxx = rnd.randrange(sw)
            cyy = rnd.randrange(sh)
            rx = rnd.randint(1, max(1, sw // 5))
            ry = rnd.randint(1, max(1, sh // 5))
            d.ellipse([cxx - rx, cyy - ry, cxx + rx, cyy + ry], fill=255)
        layer = layer.filter(ImageFilter.GaussianBlur(max(0.4, k / 3.0)))
        big = layer.resize((W, H), Image.BILINEAR).load()
        w = 1.0 / (o + 1)
        tot_w += w
        for y in range(H):
            b0 = y * W
            for x in range(W):
                acc[b0 + x] += big[x, y] / 255.0 * w
    for i in range(W * H):
        acc[i] /= tot_w
    order = sorted(range(W * H), key=lambda i: -acc[i])
    take = int(W * H * max(0.001, min(0.95, density)))
    field = bytearray(W * H)
    for i in order[:take]:
        field[i] = 1
    return field


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", required=True, help="原印章 jtex 或 PNG（取配色和斑纹）")
    ap.add_argument("--text", default=None, help="你画的纯色文字 PNG（透明底）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--mode", default="orig", choices=["orig", "blobs"])
    ap.add_argument("--density", type=float, default=None,
                    help="磨损覆盖率，省略=用原图实测值")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--erase-radius", type=float, default=0.72,
                    help="抹旧文字的内圆比例，0.72=半径的72%%以内")
    ap.add_argument("--holes", action="store_true", help="磨损处变透明孔")
    ap.add_argument("--keep-orig-text", action="store_true",
                    help="不抹原文字（只叠加新字时用）")
    ap.add_argument("--analyze", action="store_true", help="只打印分析结果")
    a = ap.parse_args()

    orig, meta = load_img(a.orig)
    W, H = orig.size
    info = analyze(orig)
    print(f"# 原图 {Path(a.orig).name}  {W}x{H}"
          + (f"  fmt=0x{meta['fmt']:02X}" if meta else ""))
    print(f"  圆心 ({info['cx']:.1f}, {info['cy']:.1f})  半径 {info['R']:.1f}")
    print(f"  油墨色 RGB{info['ink']}   磨损色 RGB{info['wear']}")
    print(f"  圆环磨损密度 {info['density']*100:.1f}%   "
          f"着墨像素 {info['ink_px']:,}")

    if a.analyze:
        return 0
    if not a.text or not a.out:
        print("\n[提示] 需要 --text 和 --out 才能生成（--analyze 只看不写）")
        return 1

    # ---- 1. 抹掉旧文字（只动内圆，圆环保留）----
    if a.keep_orig_text:
        base = orig.copy()
        print("\n  保留原文字，直接叠加")
    else:
        base = orig.copy()
        bp = base.load()
        px0 = orig.load()
        r_lim = a.erase_radius * info["R"]
        # 内圆背景色 = 半径 0.75~0.85R 之间的主色
        from collections import Counter
        cnt = Counter()
        for y in range(H):
            for x in range(W):
                d = math.hypot(x - info["cx"], y - info["cy"])
                if 0.75 * info["R"] <= d <= 0.85 * info["R"]:
                    r, g, b, al = px0[x, y]
                    if al > 128:
                        cnt[(r >> 3 << 3, g >> 3 << 3, b >> 3 << 3)] += 1
        bg = cnt.most_common(1)[0][0] if cnt else info["wear"]
        n = 0
        for y in range(H):
            for x in range(W):
                if math.hypot(x - info["cx"], y - info["cy"]) < r_lim:
                    bp[x, y] = (bg[0], bg[1], bg[2], 255)
                    n += 1
        print(f"  抹掉内圆旧文字: 半径<{r_lim:.1f}px 共 {n:,} 像素, "
              f"填入背景 RGB{bg}")

    # ---- 2. 贴你的文字 ----
    txt = Image.open(a.text)
    txt.load()
    txt = txt.convert("RGBA")
    if txt.size != (W, H):
        sq = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sq.paste(txt, ((W - txt.width) // 2, (H - txt.height) // 2), txt)
        txt = sq
        print(f"  文字层 {Image.open(a.text).size} -> 居中到 {W}x{H}")
    base.alpha_composite(txt)
    print(f"  已贴上 {Path(a.text).name}")

    # ---- 3. 加磨损 ----
    dens = a.density if a.density is not None else info["density"]
    if a.mode == "blobs":
        field = wear_field_blobs(W, H, dens, a.seed)
        src_desc = f"程序化斑点 seed={a.seed}"
    else:
        m, n, mw, mh = wear_mask_from(orig, info)
        if n == 0:
            print("  [!] 原图没提取到斑纹，退回 blobs")
            field = wear_field_blobs(W, H, dens, a.seed)
            src_desc = "程序化斑点（原图无斑纹）"
        else:
            # 随机偏移采样，避免和原图位置完全重合显得刻意
            rnd = random.Random(a.seed)
            dx, dy = rnd.randrange(mw), rnd.randrange(mh)
            field = bytearray(W * H)
            for y in range(H):
                sy = (y + dy) % mh
                b0 = y * W
                s0 = sy * mw
                for x in range(W):
                    if m[s0 + (x + dx) % mw]:
                        field[b0 + x] = 1
            d0 = n / float(mw * mh)
            if dens > d0:            # 需要更密就往四周扩散
                extra = wear_field_blobs(W, H, (dens - d0) / max(dens, 1e-6),
                                         a.seed + 99)
                for i in range(W * H):
                    if extra[i]:
                        field[i] = 1
            src_desc = (f"复用原图斑纹(偏移 {dx},{dy})，"
                        f"原密度 {d0*100:.1f}% -> 目标 {dens*100:.1f}%")

    print(f"  磨损来源: {src_desc}")

    out = base.copy()
    op = out.load()
    wear_c = info["wear"]
    hit = 0
    for y in range(H):
        b0 = y * W
        for x in range(W):
            if not field[b0 + x]:
                continue
            r, g, b, al = op[x, y]
            if al < 32:
                continue
            # 只磨损"着墨"的像素，放过已经接近背景的
            if lum((r, g, b)) > lum(info["ink"]) - 15 and \
                    lum((r, g, b)) < lum(info["ink"]) + 120:
                if a.holes:
                    op[x, y] = (r, g, b, 0)
                else:
                    op[x, y] = (wear_c[0], wear_c[1], wear_c[2], al)
                hit += 1
    print(f"  实际磨损 {hit:,} 像素 ({hit/(W*H)*100:.1f}%)")

    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    out.save(outp)
    print(f"\n  输出 -> {outp}")

    # 放大两倍便于肉眼看细节
    prev = outp.with_name(outp.stem + "_x2.png")
    out.resize((W * 2, H * 2), Image.NEAREST).save(prev)
    print(f"  2倍放大 -> {prev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
