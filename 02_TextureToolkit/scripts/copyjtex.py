#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
copyjtex.py - 把一个 jtex 的像素数据复制成另一个文件名的 jtex（修正双 CRC）

典型用途:
  tml_stamp_pic001_us_frn.jtex（内容 OK）  ->  tml_stamp_pic001_us_eng.jtex
  法语/西语的 "OK" 印章在中文语境同样可懂，直接复用原厂素材，
  比手绘更接近原作风味。

为什么不能直接改名:
  jIMG 0x20 处存的是 u32 CRC32(文件名去后缀)
  改名后该字段失效，这里按目标名重算。

jIMG 头（已实测验证）:
  0x00 'jIMG'   0x04 u32 文件总长   0x08 u16 宽   0x0A u16 高
  0x10 u32 GTX偏移   0x14 u32 GTX长度
  0x1C u32 CRC32(整个文件，算前把 0x1C..0x24 置零)
  0x20 u32 CRC32(文件名去后缀)

用法:
  py copyjtex.py --src out_ui\ui_fb_tex\tml_tex\tml_stamp_pic001_us_frn.jtex ^
                 --dst-name tml_stamp_pic001_us_eng.jtex --out work_stamp

  批量（同一内容铺给多个目标名）:
  py copyjtex.py --src ... ^
                 --dst-name tml_stamp_pic001_us_eng.jtex ^
                 --dst-name tml_stamp_pic001_eu_eng.jtex --out work_stamp

  只对比结构不写文件:
  py copyjtex.py --src A.jtex --dst-name B.jtex --report
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


def name_crc(name):
    """文件名去后缀的 CRC32"""
    stem = Path(name).stem
    return zlib.crc32(stem.encode("utf-8")) & 0xFFFFFFFF


def fix_crcs(d, new_name):
    """重算 0x1C 主 CRC 和 0x20 文件名 CRC"""
    d = bytearray(d)
    if d[:4] != b"jIMG":
        raise ValueError(f"不是 jIMG（{d[:4].hex(' ').upper()}）")
    size = u32(d, 4)
    if size != len(d):
        print(f"  [!] 0x04 声明 {size:,} 但实际 {len(d):,}")

    old_name = u32(d, 0x20)
    struct.pack_into(">I", d, 0x20, name_crc(new_name))

    # 主 CRC：先把 0x1C..0x24 八个字节置零
    d[0x1C:0x24] = b"\0" * 8
    struct.pack_into(">I", d, 0x1C, zlib.crc32(bytes(d[:size])) & 0xFFFFFFFF)
    # 再把名字 CRC 写回（它不参与主 CRC 计算）
    struct.pack_into(">I", d, 0x20, name_crc(new_name))
    return bytes(d), old_name, name_crc(new_name)


def describe(d, tag, fname=None):
    if d[:4] != b"jIMG":
        print(f"  {tag}: 不是 jIMG（{d[:4].hex(' ').upper()}）")
        return
    W, H = struct.unpack_from(">HH", d, 8)
    go = u32(d, 0x10) & 0xFFFF
    gl = u32(d, 0x14)
    print(f"  {tag}: {u32(d,4):,} B  {W}x{H}  "
          f"GTX@0x{go:X}({gl:,} B)  "
          f"crc_main=0x{u32(d,0x1C):08X}  crc_name=0x{u32(d,0x20):08X}")
    if fname:
        want = name_crc(fname)
        got = u32(d, 0x20)
        print(f"        crc_name 校验: 字段 0x{got:08X} vs "
              f"CRC32('{Path(fname).stem}')=0x{want:08X}  "
              + ("一致 ✓" if got == want else "不一致 ✗"))


def verify_main_crc(d):
    """验证 0x1C 主 CRC 是否确实成立"""
    d = bytearray(d)
    if d[:4] != b"jIMG":
        return None
    stored = u32(d, 0x1C)
    keep = bytes(d[0x20:0x24])
    d[0x1C:0x24] = b"\0" * 8
    calc = zlib.crc32(bytes(d[:u32(d, 4)])) & 0xFFFFFFFF
    return stored, calc, keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="源 jtex（借它的像素数据）")
    ap.add_argument("--dst-name", action="append", default=[],
                    help="目标文件名（可多次指定）")
    ap.add_argument("--out", default="work_stamp")
    ap.add_argument("--report", action="store_true", help="只对比结构不写文件")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_file():
        print(f"[错误] 找不到源文件 {src}")
        return 1
    sdata = src.read_bytes()
    print(f"# 源 {src.name}")
    describe(sdata, "src", src.name)
    vm = verify_main_crc(sdata)
    if vm:
        st, ca, _ = vm
        print(f"        主CRC 校验: 字段 0x{st:08X} vs 计算 0x{ca:08X}  "
              + ("成立 ✓" if st == ca else "不成立 ✗（结构假设有误，先停）"))
        if st != ca:
            print("        [!] 主 CRC 规则不对，改名会破坏文件，请把此输出发我")
            return 1
    if not a.dst_name:
        print("\n[提示] 用 --dst-name 指定目标文件名")
        return 0

    outdir = Path(a.out)
    ok = 0
    for tgt in a.dst_name:
        print(f"\n--- 目标 {tgt} ---")
        # 若目标文件已存在，先对比尺寸/GTX 是否一致
        existing = None
        for base in (Path("out_ui") / "ui_fb_tex" / "tml_tex", Path("png_stamp")):
            p = base / tgt
            if p.is_file():
                existing = p
                break
        if existing:
            ed = existing.read_bytes()
            describe(ed, "现有", tgt)
            if len(ed) != len(sdata):
                print(f"  [!] 长度不同: 现有 {len(ed):,} vs 源 {len(sdata):,}")
            tw, th = struct.unpack_from(">HH", ed, 8)
            sw, sh = struct.unpack_from(">HH", sdata, 8)
            if (tw, th) != (sw, sh):
                print(f"  [!] 尺寸不同: 现有 {tw}x{th} vs 源 {sw}x{sh}")
            if u32(ed, 0x14) != u32(sdata, 0x14):
                print(f"  [!] GTX 长度不同: 现有 {u32(ed,0x14):,} "
                      f"vs 源 {u32(sdata,0x14):,}")
            print(f"  现有文件位置: {existing}")

        if a.report:
            continue

        try:
            newdata, old_c, new_c = fix_crcs(sdata, tgt)
        except Exception as e:
            print(f"  [失败] {e}")
            continue
        print(f"  crc_name 0x{old_c:08X} -> 0x{new_c:08X}  "
              f"（CRC32('{Path(tgt).stem}')）")
        describe(newdata, "新", tgt)

        outdir.mkdir(parents=True, exist_ok=True)
        dst = outdir / tgt
        dst.write_bytes(newdata)
        print(f"  已写入 {len(newdata):,} B -> {dst}")
        ok += 1

    print(f"\n# 生成 {ok} 个")
    if ok:
        print(f"# 下一步: 用 nestput.py 把 {outdir} 回封进 ui_fb_tex.jarc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
