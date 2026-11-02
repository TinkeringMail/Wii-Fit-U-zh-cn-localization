#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
replacepic.py - 用整张新图替换 jtex 里的贴图（适合整屏截图类面板）

适用场景:
  mme_panel001_pic_*.jtex（548x308 BC1）这种「游戏内截图的缩略图」。
  直接拿本地化后的游戏截图，缩放到同尺寸，整张替换。

为什么体积不用担心:
  BC1(DXT1) 是定长压缩 —— 548x308 恒为 137x77 块 x 8 字节 = 84,392 字节(mip0)。
  任何图片编码后字节数完全相同，所以 jtex 长度不变、jarc 偏移不用动。
  BC3 同理（16 字节/块）。

与 bctext.py 的区别:
  bctext.py     新建透明画布只画文字 -> 会把圆环/背景全毁（只适合纯文字图）
  replacepic.py 整张图替换 -> 保留图片全部内容，只换掉画面

BC1 编码质量:
  不用简单包围盒取色，改用「包围盒初值 + 2 轮最小二乘重估端点」，
  对渐变和细文字明显更准。

用法:
  :: 1) 看目标尺寸/格式
  py replacepic.py out_ui\mme_us_eng_tex -e jtex --info

  :: 2) 用一张图试（--fit cover 保证填满不变形）
  py replacepic.py out_ui\mme_us_eng_tex -e jtex -s shots -d work_panel --dry --preview pv

  :: 3) 正式写入
  py replacepic.py out_ui\mme_us_eng_tex -e jtex -s shots -d work_panel
"""
import argparse
import re
import struct
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow:  pip install pillow")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bctext
import jt8


# ---------------------------------------------------------------- BC1 编码
def _q565(r, g, b):
    r = 0 if r < 0 else (255 if r > 255 else r)
    g = 0 if g < 0 else (255 if g > 255 else g)
    b = 0 if b < 0 else (255 if b > 255 else b)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _expand(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _palette(c0, c1):
    a, b = _expand(c0), _expand(c1)
    return [a, b,
            tuple((2 * x + y) // 3 for x, y in zip(a, b)),
            tuple((x + 2 * y) // 3 for x, y in zip(a, b))]


def _assign(px, pal):
    """每个像素最近的调色板索引 + 总误差"""
    bits, err = 0, 0
    for i, p in enumerate(px):
        best, bd = 0, 1 << 30
        for k, q in enumerate(pal):
            d = (p[0]-q[0])**2 + (p[1]-q[1])**2 + (p[2]-q[2])**2
            if d < bd:
                bd, best = d, k
        bits |= best << (2 * i)
        err += bd
    return bits, err


def bc1_encode_block(px):
    """px: 16 个 (r,g,b)。返回 8 字节。端点初值取包围盒，再迭代重估。"""
    rs = [p[0] for p in px]
    gs = [p[1] for p in px]
    bs = [p[2] for p in px]

    c0 = _q565(max(rs), max(gs), max(bs))
    c1 = _q565(min(rs), min(gs), min(bs))
    if c0 == c1:
        if c0 == 0:
            c1 = 1
        else:
            c0 = max(0, c0 - 1)
    if c0 < c1:
        c0, c1 = c1, c0

    best_bits, best_err = _assign(px, _palette(c0, c1))
    best = (c0, c1, best_bits, best_err)

    # 2 轮：用索引 0/1 两个簇的质心重估端点
    for _ in range(2):
        pal = _palette(c0, c1)
        s0 = [0, 0, 0, 0]
        s1 = [0, 0, 0, 0]
        for i, p in enumerate(px):
            idx = (best_bits >> (2 * i)) & 3
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
        bits, err = _assign(px, _palette(nc0, nc1))
        if err < best_err:
            best = (nc0, nc1, bits, err)
            c0, c1 = nc0, nc1
            best_bits = bits
        else:
            break

    c0, c1, bits, _e = best
    return struct.pack("<HHI", c0, c1, bits)


# ---------------------------------------------------------------- 编码整面
def encode_surface(img, meta):
    """按 meta 的尺寸/格式重新编码整张像素数据（长度恒定）"""
    W, H, pitch, bpb = meta["W"], meta["H"], meta["pitch"], meta["bpb"]
    fmt, rows = meta["fmt"], meta["rows"]
    if fmt not in (0x1A, 0x31, 0x33):
        raise ValueError(f"格式 0x{fmt:02X} 不支持")

    out = bytearray(rows * pitch * bpb)
    px = img.load()
    for by in range(rows):
        for bx in range(pitch):
            vals = []
            for i in range(16):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                vals.append(px[x, y] if (x < W and y < H) else (0, 0, 0, 0))
            if fmt == 0x1A:
                blk = b"".join(bytes(v) for v in vals)
            elif fmt == 0x31:
                blk = bctext.bc1_enc([v[:3] for v in vals])   # 已升级端点优化版
            else:                                    # BC3: alpha(BC4) + color(BC1)
                blk = bctext.bc4_enc([v[3] for v in vals]) + \
                    bctext.bc1_enc([v[:3] for v in vals])
            s = bctext.tm4(bx, by, pitch, bpb) * bpb
            out[s:s + bpb] = blk
    return bytes(out)


# ---------------------------------------------------------------- 缩放
def fit_image(src, W, H, mode="cover"):
    """把 src 变成 W x H。cover=填满后居中裁切（不变形）"""
    sw, sh = src.size
    if mode == "stretch":
        return src.resize((W, H), Image.LANCZOS)
    if mode == "contain":
        out = Image.new("RGB", (W, H), (0, 0, 0))
        sc = min(W / sw, H / sh)
        nw, nh = max(1, int(sw * sc)), max(1, int(sh * sc))
        out.paste(src.resize((nw, nh), Image.LANCZOS),
                  ((W - nw) // 2, (H - nh) // 2))
        return out
    # cover
    sc = max(W / sw, H / sh)
    nw, nh = max(W, int(round(sw * sc))), max(H, int(round(sh * sc)))
    tmp = src.resize((nw, nh), Image.LANCZOS)
    x0 = (nw - W) // 2
    y0 = (nh - H) // 2
    return tmp.crop((x0, y0, x0 + W, y0 + H))


# ---------------------------------------------------------------- 主流程
def rewrite(path, new_img, dry, preview, outdir, mode):
    raw = bytearray(path.read_bytes())        # 必须可变：后面要回写像素
    spots = bctext.find_jimgs(raw)
    if not spots:
        print(f"  [跳过] {path.name}: 无 jIMG")
        return False
    off, size = spots[0]
    old_img, meta = bctext.decode(raw[off:off + size])
    W, H = meta["W"], meta["H"]
    fmt = meta["fmt"]
    print(f"  {path.name}: {W}x{H} fmt=0x{fmt:02X} pitch={meta['pitch']} "
          f"bpb={meta['bpb']} 源图 {new_img.size[0]}x{new_img.size[1]}")

    img = fit_image(new_img.convert("RGB"), W, H, mode)
    if preview:
        pv = Path(preview) / (path.stem + ".png")
        pv.parent.mkdir(parents=True, exist_ok=True)
        img.save(pv)
        print(f"    预览 -> {pv}")
    if dry:
        return False

    data = encode_surface(img, meta)
    need = meta["rows"] * meta["pitch"] * meta["bpb"]
    if len(data) != need:
        print(f"    [错误] 编码长度 {len(data)} != {need}")
        return False

    g = bytearray(raw[off:off + size])
    go, _gl = bctext.jimg_range(bytes(g))
    pos = go + bctext.u32(g, go + 4)
    tgt = None
    while pos + 0x20 <= len(g):
        mg, bhs, _a, _b, typ, dsz, _c, _d = struct.unpack(">4s7I", g[pos:pos + 0x20])
        if mg != b"BLK{":
            break
        if typ == 0x0C:
            tgt = pos + bhs
            break
        pos += bhs + dsz
    if tgt is None:
        print("    [错误] 未找到像素块")
        return False

    old = len(raw)
    g[tgt:tgt + len(data)] = data
    g = bctext.jimg_fix_crc(bytes(g))
    raw[off:off + size] = g
    assert len(raw) == old, f"长度从 {old} 变成 {len(raw)}"

    outdir.mkdir(parents=True, exist_ok=True)
    dst = outdir / path.name
    dst.write_bytes(bytes(raw))
    print(f"    已写入（{len(raw):,} B 不变，CRC 已修正）-> {dst}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="jtex 所在目录")
    ap.add_argument("-e", "--ext", default="jtex")
    ap.add_argument("-s", "--shots", default=None, help="新图目录或单张图片")
    ap.add_argument("-d", "--out-dir", default="work_pic")
    ap.add_argument("--preview", default=None)
    ap.add_argument("--fit", default="cover", choices=["cover", "contain", "stretch"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--info", action="store_true", help="只打印目标图的尺寸/格式")
    a = ap.parse_args()

    targets = {}
    for p in a.inputs:
        q = Path(p)
        if q.is_file():
            targets[q.name] = q
        elif q.is_dir():
            for f in sorted(q.glob("*")):
                if f.is_file() and f.suffix.lower().lstrip(".") == a.ext:
                    targets[f.name] = f

    print(f"# 目标 {len(targets)} 个 jtex")

    if a.info:
        for name, f in targets.items():
            raw = f.read_bytes()
            for n, (off, size) in enumerate(bctext.find_jimgs(raw)):
                try:
                    _img, meta = bctext.decode(raw[off:off + size])
                except Exception as e:
                    print(f"  {name}[{n}] 解码失败 {e}")
                    continue
                print(f"  {name:44s} [{n}] {meta['W']}x{meta['H']} "
                      f"fmt=0x{meta['fmt']:02X} pitch={meta['pitch']} "
                      f"bpb={meta['bpb']} 文件 {len(raw):,} B")
        return 0

    if not a.shots:
        print("[错误] 需要 -s 指定新图目录（或用 --info 只看尺寸）")
        return 1

    sp = Path(a.shots)

    IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}

    def index_shots(root):
        """建立 {键: 路径} 索引。
        兼容 group.py --export 产生的 __0 / __1 后缀，
        例如 mme_panel001_pic_fav_us_eng__0.png 也能匹配到
        mme_panel001_pic_fav_us_eng.jtex。"""
        idx = {}
        if root.is_file():
            idx[root.stem] = root
            idx[re.sub(r"__\d+$", "", root.stem)] = root
            return idx
        for p in sorted(root.rglob("*")):
            if not (p.is_file() and p.suffix.lower() in IMG_EXT):
                continue
            idx.setdefault(p.stem, p)
            base = re.sub(r"__\d+$", "", p.stem)
            if base != p.stem:
                idx.setdefault(base, p)
        return idx

    shots_idx = index_shots(sp) if sp.exists() else {}
    print(f"# 新图目录 {sp}: 索引到 {len(shots_idx)} 个键"
          + ("" if shots_idx else "  [目录不存在或为空]"))

    def find_shot(name):
        stem = Path(name).stem
        if stem in shots_idx:
            return shots_idx[stem]
        for pat in (f"{stem}.png", f"{stem}.jpg", f"{stem}__0.png"):
            q = sp / pat if sp.is_dir() else sp.with_name(pat)
            if q.is_file():
                return q
        return None

    ok = miss = 0
    missing_names = []
    for name, f in targets.items():
        print(f"\n=== {name} ===")
        shot = find_shot(name)
        if not shot:
            print(f"  [未找到新图] 需要: {Path(name).stem}.png"
                  f"（或 {Path(name).stem}__0.png）")
            missing_names.append(Path(name).stem)
            miss += 1
            continue
        try:
            im = Image.open(shot)
            im.load()
        except Exception as e:
            print(f"  [打开失败] {shot}: {e}")
            miss += 1
            continue
        try:
            if rewrite(f, im, a.dry, a.preview, Path(a.out_dir), a.fit):
                ok += 1
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")

    print(f"\n# 写入 {ok} 个，未处理 {miss} 个，共 {len(targets)} 个")
    if missing_names:
        print("# 缺图清单:")
        for nm in missing_names:
            print(f"    {nm}.png")
        if shots_idx:
            print("# 目录里现有的键（前 20 个）:")
            for k in sorted(shots_idx)[:20]:
                print(f"    {k}")
    if a.dry and ok:
        print(f"\n# 试运行：预览图在 {Path(a.preview).resolve() if a.preview else '(未指定)'}")
        print("# 确认画质后去掉 --dry 再跑一次")
    elif not a.dry and ok:
        print(f"# 输出目录: {Path(a.out_dir).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
