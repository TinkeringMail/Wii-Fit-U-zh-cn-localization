#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
bctext.py - 重绘烘死在 BC1/BC3 贴图里的文字（jtex / jmdl 均可）

特点:
  * 默认「保留原配色」：从原图统计出填充色和描边色，新字沿用 -> 不会撞色
  * 按文件名自动选译文（--auto-text），也可 --text 强制指定
  * 编码后字节数不变（BC3/BC1 固定），改完可原地覆盖进 jarc
  * 自动重算 jIMG 0x1C 主 CRC（0x20 文件名 CRC 保持不变）

依赖: Pillow

用法:
  py bctext.py out_ui\mme_us_eng_tex -e jtex -d new\mme_us_eng_tex --auto-text --preview
  py bctext.py out_ui\models_data\training_obj001_txt_us_eng.jmdl -d new\models_data
  py bctext.py 某.jtex --text 身体测试 --color 255,255,255 --stroke-color 60,160,40
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("需要 Pillow:  pip install pillow")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ------------------------------------------------------------ jIMG / GTX
def jimg_range(d):
    return u32(d, 0x10) & 0xFFFF, u32(d, 0x14)


def jimg_fix_crc(d):
    d = bytearray(d)
    size = u32(d, 4)
    keep = bytes(d[0x20:0x24])
    d[0x1C:0x24] = b"\0" * 8
    struct.pack_into(">I", d, 0x1C, zlib.crc32(bytes(d[:size])) & 0xFFFFFFFF)
    d[0x20:0x24] = keep
    return bytes(d)


SF = ["dim", "width", "height", "depth", "numMips", "format", "aa", "use",
      "imageSize", "imagePtr", "mipSize", "mipPtr", "tileMode", "swizzle",
      "alignment", "pitch"]


def gtx_parse(g):
    pos, blocks = u32(g, 4), []
    while pos + 0x20 <= len(g):
        mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I",
                                                         g[pos:pos + 0x20])
        if mg != b"BLK{":
            break
        blocks.append(dict(type=typ, data=pos + bhs, size=dsz))
        pos += bhs + dsz
    sb = next((b for b in blocks if b["type"] == 0x0B), None)
    ib = next((b for b in blocks if b["type"] == 0x0C), None)
    if not sb or not ib:
        raise ValueError("缺 BLK 块")
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


BPB = {0x1A: 4, 0x31: 8, 0x33: 16}


# ------------------------------------------------------------ BC 编解码
def _c565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def bc1_dec(blk):
    c0, c1 = blk[0] | (blk[1] << 8), blk[2] | (blk[3] << 8)
    p0, p1 = _c565(c0), _c565(c1)
    pal = [p0, p1,
           tuple((2*a+b)//3 for a, b in zip(p0, p1)),
           tuple((a+2*b)//3 for a, b in zip(p0, p1))]
    bits = blk[4] | (blk[5]<<8) | (blk[6]<<16) | (blk[7]<<24)
    return [pal[(bits >> (2*i)) & 3] for i in range(16)]


def bc4_dec(blk):
    a0, a1 = blk[0], blk[1]
    if a0 > a1:
        pal = [a0, a1] + [((7-i)*a0 + i*a1)//7 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [((5-i)*a0 + i*a1)//5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(blk[2:8], "little")
    return [pal[(bits >> (3*i)) & 7] for i in range(16)]


def _q565(r, g, b):
    r = 0 if r < 0 else (255 if r > 255 else r)
    g = 0 if g < 0 else (255 if g > 255 else g)
    b = 0 if b < 0 else (255 if b > 255 else b)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _bc1_assign(px, pal):
    bits, err = 0, 0
    for i, p in enumerate(px):
        best, bd = 0, 1 << 30
        for k, q in enumerate(pal):
            d = (p[0]-q[0])**2 + (p[1]-q[1])**2 + (p[2]-q[2])**2
            if d < bd:
                bd, best = d, k
        bits |= best << (2*i)
        err += bd
    return bits, err


def bc1_enc(px):
    """BC1 块编码：包围盒取初值，再用两个簇的质心迭代重估端点。
    比单纯包围盒明显更准，尤其是渐变和细文字。"""
    rf = [p[0] for p in px]; gf = [p[1] for p in px]; bf = [p[2] for p in px]
    c0 = _q565(max(rf), max(gf), max(bf))
    c1 = _q565(min(rf), min(gf), min(bf))
    if c0 == c1:
        if c0 == 0:
            c1 = 1
        else:
            c0 = max(0, c0 - 1)
    if c0 < c1:
        c0, c1 = c1, c0

    pal = [a for a in _bc1_palette(c0, c1)]
    bits, err = _bc1_assign(px, pal)
    best = (c0, c1, bits, err)

    for _ in range(2):
        s0 = [0, 0, 0, 0]
        s1 = [0, 0, 0, 0]
        for i, p in enumerate(px):
            idx = (bits >> (2*i)) & 3
            if idx == 0:
                for k in range(3):
                    s0[k] += p[k]
                s0[3] += 1
            elif idx == 1:
                for k in range(3):
                    s1[k] += p[k]
                s1[3] += 1
        if s0[3] == 0 or s1[3] == 0:
            break
        n0 = tuple(s0[k] // s0[3] for k in range(3))
        n1 = tuple(s1[k] // s1[3] for k in range(3))
        nc0, nc1 = _q565(*n0), _q565(*n1)
        if nc0 == nc1:
            break
        if nc0 < nc1:
            nc0, nc1 = nc1, nc0
        nbits, nerr = _bc1_assign(px, _bc1_palette(nc0, nc1))
        if nerr < err:
            best = (nc0, nc1, nbits, nerr)
            c0, c1, bits, err = nc0, nc1, nbits, nerr
        else:
            break

    c0, c1, bits, _e = best
    return struct.pack("<HHI", c0, c1, bits)


def _bc1_palette(c0, c1):
    a, b_ = _c565(c0), _c565(c1)
    return [a, b_,
            tuple((2*x+y)//3 for x, y in zip(a, b_)),
            tuple((x+2*y)//3 for x, y in zip(a, b_))]


def bc4_enc(vals):
    a0, a1 = max(vals), min(vals)
    if a0 == a1:
        if a0 == 0:
            a0 = 1
        else:
            a1 = a0 - 1
    if a0 > a1:
        pal = [a0, a1] + [((7-i)*a0 + i*a1)//7 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [((5-i)*a0 + i*a1)//5 for i in range(1, 5)] + [0, 255]
    bits = 0
    for i, v in enumerate(vals):
        best, bd = 0, 1 << 30
        for k, p in enumerate(pal):
            d = abs(v - p)
            if d < bd:
                bd, best = d, k
        bits |= best << (3*i)
    return bytes((a0, a1)) + bits.to_bytes(6, "little")


# ------------------------------------------------------------ 表面解/编
def decode(jimg):
    go, gl = jimg_range(jimg)
    g = jimg[go:go+gl]
    surf, ib = gtx_parse(g)
    fmt = surf["format"] & 0xFF
    if fmt not in BPB:
        raise ValueError(f"格式 0x{fmt:02X} 不支持（只做 BC1/BC3/RGBA8）")
    W, H, pitch, bpb = surf["width"], surf["height"], surf["pitch"], BPB[fmt]
    data = g[ib["data"]:ib["data"] + ib["size"]]
    rows = len(data) // bpb // pitch
    lin = [[None] * pitch for _ in range(rows)]
    for y in range(rows):
        for x in range(pitch):
            s = tm4(x, y, pitch, bpb) * bpb
            lin[y][x] = data[s:s+bpb]

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    if fmt == 0x1A:
        for y in range(min(H, rows)):
            for x in range(min(W, pitch)):
                q = lin[y][x]
                px[x, y] = (q[0], q[1], q[2], q[3])
    elif fmt == 0x31:
        for by in range(rows):
            for bx in range(pitch):
                cs = bc1_dec(lin[by][bx])
                for i in range(16):
                    x, y = bx*4 + (i & 3), by*4 + (i >> 2)
                    if x < W and y < H:
                        px[x, y] = cs[i] + (255,)
    else:                                   # BC3
        for by in range(rows):
            for bx in range(pitch):
                blk = lin[by][bx]
                cs = bc1_dec(blk[8:16])
                av = bc4_dec(blk[0:8])
                for i in range(16):
                    x, y = bx*4 + (i & 3), by*4 + (i >> 2)
                    if x < W and y < H:
                        px[x, y] = cs[i] + (av[i],)
    return img, dict(fmt=fmt, W=W, H=H, pitch=pitch, bpb=bpb, rows=rows)


def encode(img, meta):
    W, H, pitch, bpb = meta["W"], meta["H"], meta["pitch"], meta["bpb"]
    fmt, rows = meta["fmt"], meta["rows"]
    out = bytearray(rows * pitch * bpb)
    ip = img.load()
    for by in range(rows):
        for bx in range(pitch):
            vals = []
            for i in range(16):
                x, y = bx*4 + (i & 3), by*4 + (i >> 2)
                vals.append(ip[x, y] if (x < W and y < H) else (0, 0, 0, 0))
            if fmt == 0x1A:
                blk = b"".join(bytes(v) for v in vals)
            elif fmt == 0x31:
                blk = bc1_enc([v[:3] for v in vals])
            else:
                blk = bc4_enc([v[3] for v in vals]) + bc1_enc([v[:3] for v in vals])
            s = tm4(bx, by, pitch, bpb) * bpb
            out[s:s+bpb] = blk
    return bytes(out)


# ------------------------------------------------------------ 配色分析
def analyze_colors(img, topn=6):
    """统计不透明像素的颜色，返回 [(count, (r,g,b))] 降序"""
    W, H = img.size
    px = img.load()
    cnt = {}
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a > 128:
                key = (r >> 3 << 3, g >> 3 << 3, b >> 3 << 3)
                cnt[key] = cnt.get(key, 0) + 1
    return sorted(((c, k) for k, c in cnt.items()), reverse=True)[:topn]


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def pick_fill_stroke(img, verbose=False):
    """按拓扑判定，而不是按像素数量。

    描边 = 最外层，必然有大量像素紧挨透明区
    填充 = 被包在里面，接触透明的比例低
    256x64 的牌匾上描边比字内还大，用"数量最多"会判反 —— 这里改用边界接触率。
    """
    cols = analyze_colors(img, 10)
    if not cols:
        return (40, 140, 30), (180, 235, 70)

    W, H = img.size
    px = img.load()

    def key(c):
        return (c[0] >> 3 << 3, c[1] >> 3 << 3, c[2] >> 3 << 3)

    # 候选色集合（量化后）
    cand = [k for _n, k in cols[:6]]

    if len(cand) == 1:
        only = cand[0]
        stroke = (255, 255, 255) if _lum(only) < 128 else (40, 40, 40)
        return only, stroke

    stats = []
    for c in cand:
        kc = key(c)
        total = edge = 0
        for y in range(H):
            for x in range(W):
                r, g, b, a = px[x, y]
                if a <= 128:
                    continue
                if key((r, g, b)) != kc:
                    continue
                total += 1
                # 四邻域里只要有透明像素，就算边界像素
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < W and 0 <= ny < H:
                        if px[nx, ny][3] <= 16:
                            edge += 1
                            break
                    else:
                        edge += 1          # 贴图片边缘也算边界
                        break
        if total == 0:
            continue
        stats.append((edge / total, total, tuple(c)))

    if not stats:
        return (40, 140, 30), (180, 235, 70)

    # 边界接触率最高的 = 描边
    stats.sort(key=lambda t: -t[0])
    stroke = stats[0][2]
    # 边界接触率最低的 = 填充
    fill = min(stats, key=lambda t: t[0])[2]
    if fill == stroke and len(stats) > 1:
        fill = stats[-1][2]

    if verbose:
        print("  [配色判定] 边界接触率 / 像素数:")
        for er, n, c in stats:
            tag = "描边" if c == stroke else ("填充" if c == fill else "    ")
            print(f"      {er*100:5.1f}%  {n:6d}  RGB{c}  {tag}")

    return fill, stroke


# ------------------------------------------------------------ 文本映射
def auto_text(name):
    n = name.lower()
    if "baby" in n:
        return "宝宝数据"
    if "pet" in n:
        return "宠物数据"
    if "training_obj" in n or "train" in n:
        return "锻炼"
    if "bck_obj_txt" in n:
        return "身体测试"
    return None


# ------------------------------------------------------------ 字体
FONT_DEFAULT = "AlibabaPuHuiTi-3-75-SemiBold.ttf"
_FONT_CACHE = {}
_USED_FONT = [None]


def load_font(size, font_path=None):
    """按顺序找字体：--font > 工具包 fonts 目录 > 脚本目录 > 当前目录 > 系统字体目录。
    找到的第一个可用字体缓存下来，并在首次使用时打印实际路径。"""
    key = (font_path, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    cands = []
    if font_path:
        cands.append(Path(font_path))
    cands += [HERE / FONT_DEFAULT, Path(FONT_DEFAULT)]
    cands += [HERE.parent / "fonts" / FONT_DEFAULT,
              Path(r"C:\Windows\Fonts") / FONT_DEFAULT,
              Path(r"C:\Windows\Fonts\msyhbd.ttc"),
              Path(r"C:\Windows\Fonts\msyh.ttc"),
              Path(r"C:\Windows\Fonts\simhei.ttf"),
              Path(r"C:\Windows\Fonts\Deng.ttf")]

    tried = []
    for c in cands:
        if not c.is_file():
            continue
        try:
            f = ImageFont.truetype(str(c), size)
        except OSError as e:
            tried.append(f"{c} ({e})")
            continue
        _FONT_CACHE[key] = f
        if _USED_FONT[0] != str(c):
            _USED_FONT[0] = str(c)
            try:
                name = f.getname()
            except Exception:
                name = ("?", "?")
            print(f"  [字体] {name[0]} {name[1]}  <- {c}")
            want = Path(font_path).name if font_path else FONT_DEFAULT
            if Path(c).name != want:
                print(f"  [字体警告] 没找到 {want}，已退回 {Path(c).name}")
                print(f"             把 {want} 放到 {HERE} 或当前目录，"
                      f"或用 --font 指定完整路径")
        return f

    msg = (f"找不到可用字体。已尝试:\n  " + "\n  ".join(tried or [str(x) for x in cands])
           + f"\n请把 {FONT_DEFAULT} 放到 {HERE} 或用 --font 指定完整路径")
    raise FileNotFoundError(msg)


def fit_size(W, H, text, stroke, font_path, max_ratio=0.92, angle=0.0):
    """算出把 text 塞进 W x H（含 stroke）的最大字号；angle 非零时按旋转后的包围盒算"""
    import math
    probe = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(probe)
    rad = math.radians(angle)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    cand = H
    while cand > 6:
        f = load_font(cand, font_path)
        bb = d.textbbox((0, 0), text, font=f, stroke_width=stroke)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        # 旋转后的外接矩形
        bw = tw * ca + th * sa
        bh = tw * sa + th * ca
        if (bw <= W*max_ratio - 2*stroke and bh <= H*max_ratio - 2*stroke):
            return cand
        cand -= 1
    return 6


def render(size_wh, text, size, fill, stroke, stroke_fill, font_path,
           max_ratio=0.92, angle=0.0):
    """angle: 顺时针旋转角度（度）。印章那种斜排文字用。"""
    W, H = size_wh

    if size <= 0:
        size = fit_size(W, H, text, stroke, font_path, max_ratio, angle)
    f = load_font(size, font_path)

    # 先画在足够大的透明层上，旋转后再居中贴回，避免旋转裁边
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bb = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]
    pad = 2 * stroke + 4
    lw, lh = tw + pad * 2, th + pad * 2
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad - bb[0], pad - bb[1]), text, font=f,
                               fill=tuple(fill) + (255,),
                               stroke_width=stroke,
                               stroke_fill=tuple(stroke_fill) + (255,))
    if angle:
        layer = layer.rotate(-angle, resample=Image.BICUBIC, expand=True)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(layer, ((W - layer.width) // 2, (H - layer.height) // 2))
    return canvas, size


# ------------------------------------------------------------ 主处理
def find_jimgs(raw):
    out, i = [], 0
    while True:
        i = raw.find(b"jIMG", i)
        if i < 0:
            return out
        try:
            go, _gl = jimg_range(raw[i:])
            size = u32(raw, i + 4)
            if raw[i+go:i+go+4] == b"Gfx2" and 0x800 < size and i + size <= len(raw):
                out.append((i, size))
                i += size
                continue
        except Exception:
            pass
        i += 4


def collect_targets(files, args):
    """预扫描：找出所有会被处理的 (path, jimg_off, size, W, H, text)"""
    out = []
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        text = args.text or auto_text(f.stem)
        if not text:
            continue
        for n, (off, size) in enumerate(find_jimgs(raw)):
            try:
                img, meta = decode(raw[off:off + size])
            except Exception:
                continue
            W, H = meta["W"], meta["H"]
            if W < 96 or W < 2 * H:
                continue
            out.append((f, n, off, size, W, H, text))
    return out


def resolve_unified_size(files, args):
    """--unify-size：取所有目标图都能容纳的最大字号，保证字号一致"""
    tg = collect_targets(files, args)
    if not tg:
        return 0
    per = []
    for _f, _n, _o, _s, W, H, text in tg:
        per.append(fit_size(W, H, text, args.stroke, args.font))
    uniq = sorted(set(per))
    print(f"# [统一字号] 各图单独可容纳: {uniq}")
    if len(uniq) == 1:
        print(f"# [统一字号] 全部为 {uniq[0]}，无需调整")
    else:
        print(f"# [统一字号] 取最小值 {uniq[0]}（原先 {max(uniq)}，差 {max(uniq)-uniq[0]}）")
    return uniq[0]


def process(path, outdir, args):
    raw = bytearray(path.read_bytes())
    spots = find_jimgs(raw)
    if not spots:
        print(f"  [跳过] {path.name}: 无 jIMG")
        return False

    text = args.text or auto_text(path.stem)
    if not text:
        print(f"  [跳过] {path.name}: 无法判定译文，请用 --text 指定")
        return False

    changed = False
    for n, (off, size) in enumerate(spots):
        try:
            img, meta = decode(bytes(raw[off:off+size]))
        except Exception as e:
            print(f"  [{n}] 跳过: {type(e).__name__}: {e}")
            continue
        W, H = meta["W"], meta["H"]
        # 只处理像文字条的（宽>=2x高 且够大）
        if W < 96 or W < 2 * H:
            print(f"  [{n}] {W}x{H} 非文字条，跳过")
            continue

        fill, stroke_c = pick_fill_stroke(img, verbose=True)
        if args.color:
            fill = tuple(args.color)
        if args.stroke_color:
            stroke_c = tuple(args.stroke_color)

        new, used = render((W, H), text, args.size, fill, args.stroke,
                           stroke_c, args.font, angle=getattr(args, "angle", 0.0))
        cols = analyze_colors(img, 4)
        print(f"  [{n}] {W}x{H} fmt=0x{meta['fmt']:02X}  译文「{text}」 字号 {used}"
              + (f"  旋转 {getattr(args, 'angle', 0.0):g}°"
                 if getattr(args, "angle", 0.0) else ""))
        print(f"      原图前几色 {[(c, k) for c, k in cols[:3]]}")
        print(f"      采用 -> 填充 RGB{fill}   描边 RGB{stroke_c}  ({args.stroke}px)")

        if args.preview:
            try:
                pv = Path(args.preview)
                ext = pv.suffix or ".png"      # 未写扩展名时默认 png
                if len(spots) > 1:
                    pv = pv.with_name(f"{path.stem}__t{n}{ext}")
                else:
                    pv = pv.with_name(f"{path.stem}{ext}")
                pv.parent.mkdir(parents=True, exist_ok=True)
                new.save(pv)
                print(f"      预览 -> {pv}")
            except Exception as e:
                print(f"      [预览失败] {type(e).__name__}: {e}（不影响后续写入）")

        if args.dry:
            continue

        data = encode(new, meta)
        if len(data) != meta["rows"] * meta["pitch"] * meta["bpb"]:
            print("      [错误] 编码长度异常，跳过")
            continue
        # 定位 GTX 内像素块
        # 注意: u32(g, go+4) 是「相对 GTX 起点」的头长度，必须再叠加 go 才是 jIMG 内绝对偏移
        go, gl = jimg_range(bytes(raw[off:off+size]))
        g = raw[off:off+size]
        pos = go + u32(g, go + 4)
        tgt = None
        chain = []
        while pos + 0x20 <= len(g):
            mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I",
                                                             g[pos:pos+0x20])
            if mg != b"BLK{":
                break
            chain.append((hex(pos), hex(typ), dsz))
            if typ == 0x0C:
                tgt = pos + bhs
                break
            pos += bhs + dsz
        if tgt is None:
            print(f"      [错误] 未找到像素块  GTX@{go:#x} 起点 pos={pos:#x} "
                  f"已扫块 {chain}")
            continue
        if tgt + len(data) > len(g):
            print(f"      [错误] 写入越界: tgt={tgt:#x} + {len(data):,} > {len(g):,}")
            continue
        old_len = len(raw)
        g[tgt:tgt+len(data)] = data
        g = jimg_fix_crc(bytes(g))
        raw[off:off+size] = g
        assert len(raw) == old_len
        changed = True
        print(f"      已写入 像素块@{tgt:#x}（长度不变 {len(raw):,} B，CRC 已修正）")
        print(f"      块链 {chain}")

    if not changed:
        return False

    outdir.mkdir(parents=True, exist_ok=True)
    dst = outdir / path.name
    dst.write_bytes(bytes(raw))
    print(f"  -> {dst}  ({len(raw):,} B)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-e", "--ext", default="jtex,jmdl")
    ap.add_argument("-d", "--out-dir", default="new_tex")
    ap.add_argument("--text", default=None, help="强制指定译文")
    ap.add_argument("--auto-text", action="store_true",
                    help="按文件名自动选译文（默认开启）")
    ap.add_argument("--size", type=int, default=0, help="0=自动适配")
    ap.add_argument("--font", default=None,
                    help=f"字体路径或文件名（默认 {FONT_DEFAULT}）")
    ap.add_argument("--color", default=None,
                    type=lambda s: [int(x) for x in s.split(",")])
    ap.add_argument("--stroke", type=int, default=4)
    ap.add_argument("--stroke-color", default=None,
                    type=lambda s: [int(x) for x in s.split(",")])
    ap.add_argument("--preview", default=None)
    ap.add_argument("--angle", type=float, default=0.0,
                    help="文字顺时针旋转角度（度），印章斜排用，如 15")
    ap.add_argument("--unify-size", action="store_true",
                    help="同一批图统一用同一个字号（取都能容纳的最大值）")
    ap.add_argument("--dry", action="store_true", help="只看诊断不写文件")
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

    print(f"# 待处理 {len(files)} 个文件")

    if a.unify_size and a.size <= 0 and files:
        us = resolve_unified_size(files, a)
        if us > 0:
            a.size = us

    ok = skipped = 0
    for f in files:
        print(f"\n=== {f.name} ===")
        try:
            if process(f, Path(a.out_dir), a):
                ok += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")
            skipped += 1

    if a.dry:
        print(f"\n# 试运行结束：处理 {ok} 个，跳过 {skipped} 个（--dry 不写任何文件）")
        print("# 预览图在 --preview 指定目录；确认后去掉 --dry 再跑一次")
    else:
        print(f"\n# 完成 {ok} 个，跳过 {skipped} 个，共 {len(files)} 个")
        if ok == 0:
            print("# [警告] 一个都没改成，检查后缀/路径，或该目录里没有带文字的图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
