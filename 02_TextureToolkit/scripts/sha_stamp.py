#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
sha_stamp.py - 对比一组 jtex 的「像素区指纹」，找出内容真正相同的文件

为什么要忽略 CRC:
  jIMG 里 0x1C(主CRC) 和 0x20(名字CRC) 必然每个文件不同，
  但它们只是校验字段，不影响画面。真正决定画面的是 GTX 之后的数据。
  所以计算指纹时把这两处（0x1C..0x24）排除，才能看出图画是否相同。

用法:
  py sha_stamp.py                              # 默认扫 aot_stamp001
  py sha_stamp.py out_ui\ui_fb_tex\tml_tex --prefix tml_stamp
  py sha_stamp.py 目录 --prefix 前缀
"""
import argparse
import hashlib
import struct
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def pixel_fp(d):
    """像素区指纹：跳过 0x1C..0x24 两处 CRC"""
    if d[:4] != b"jIMG":
        return None
    size = u32(d, 4)
    body = bytearray(d[:size])
    body[0x1C:0x24] = b"\0" * 8
    return hashlib.sha1(bytes(body)).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default="out_ui\\aot_tex")
    ap.add_argument("--prefix", default="aot_stamp001")
    a = ap.parse_args()

    d = Path(a.dir)
    if not d.is_dir():
        print(f"[错误] 目录不存在: {d}")
        return 1
    files = sorted(f for f in d.glob("*.jtex") if a.prefix in f.stem)
    if not files:
        print(f"[错误] {d} 里没有名字含 {a.prefix} 的 .jtex")
        return 1

    rows = []
    for f in files:
        raw = f.read_bytes()
        if raw[:4] != b"jIMG":
            print(f"  {f.name}: 不是 jIMG")
            continue
        W, H = struct.unpack_from(">HH", raw, 8)
        fp = pixel_fp(raw)
        rows.append((f.name, len(raw), W, H, fp))

    print(f"{'文件':38s} {'总长':>9s} {'尺寸':>10s}  像素指纹(前16)")
    print("-" * 84)
    for name, n, W, H, fp in rows:
        print(f"{name:38s} {n:>9,} {'%dx%d' % (W, H):>10s}  {fp[:16]}")

    # 按指纹分组
    groups = {}
    for name, n, W, H, fp in rows:
        groups.setdefault(fp, []).append(name)

    print(f"\n# 按像素指纹分组：共 {len(groups)} 组")
    for i, (fp, names) in enumerate(groups.items(), 1):
        print(f"\n  组 {i}（{len(names)} 个，指纹 {fp[:16]}）：")
        for nm in names:
            print(f"      {nm}")

    if len(groups) == 1:
        print("\n# 全部同图：随便挑一个当源都行")
    else:
        print("\n# 说明：不同组 = 画面不同。"
              "挑一个你确认内容是「OK」的组当源。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
