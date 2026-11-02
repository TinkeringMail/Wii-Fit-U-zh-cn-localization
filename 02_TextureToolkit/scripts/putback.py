#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
putback.py v2 - 把改好的贴图/模型塞回 .jarc（自动处理 jCMP 外层）

结构（已用真实文件头核对）:
  .jarc 文件 = jCMP(20B头+zlib)  ->  解开后是 jARC
     mme_us_eng_tex.jarc : size=688,692  zsize=688,672  raw=2,447,104
     models_data.jarc    : size=9,959,292 zsize=9,959,272 raw=22,577,404
  jARC = 'jARC' | u32 总长 | u32 unknown(0x01402300) | u32 条目数
         条目表 16B x N: u32 off, u32 size, u32 name_off, u32 f4(=CRC32(名字))
         名字块(顺序写入) | 按 align 填充 | 数据块(每条按 align 填充)
  条目内容可能是 jCMP 包着的 jIMG，也可能就是裸 jIMG

策略:
  [A] 原地覆盖: 新条目字节数与原条目完全相同时，只替换那一段，其余不动
  [B] 完整重封: 大小变了才走。对齐值用「原始数据重建 + 与磁盘逐字节比对」反推
      每个条目的 f4 原样保留；jCMP 条目用原 flags 重新压缩

用法:
  py putback.py ui\mme_us_eng_tex.jarc --report
  py putback.py ui\mme_us_eng_tex.jarc -n new\mme_us_eng_tex -o patched\ui\mme_us_eng_tex.jarc
  py putback.py ui -e jarc -n new -o patched\ui --auto
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

JARC = b"jARC"
JCMP = b"jCMP"
JIMG = b"jIMG"


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ---------------------------------------------------------------- jCMP
def jcmp_unwrap(data):
    """-> (inner, info) ; 不是 jCMP 时 info=None"""
    if len(data) < 20 or data[:4] != JCMP:
        return data, None
    size, flags, zsize, rawsize = struct.unpack_from(">IIII", data, 4)
    body = data[20:20 + zsize]
    inner = zlib.decompress(body)
    return inner, dict(flags=flags, rawsize=rawsize, declared=size,
                       zsize=zsize, ok=(len(inner) == rawsize))


def jcmp_wrap(inner, info, level=9):
    """jCMP 头: 'jCMP' | u32 文件总长 | u32 flags | u32 zsize | u32 rawsize | zlib"""
    if info is None:
        return inner
    comp = zlib.compress(inner, level)
    total = 20 + len(comp)
    out = (JCMP + struct.pack(">IIII", total, info["flags"],
                              len(comp), len(inner)) + comp)
    # 自检：头声明的总长必须等于实际长度，解压回去必须一致
    assert len(out) == total, f"jCMP 总长不符 {len(out)} != {total}"
    chk, _ci = jcmp_unwrap(out)
    assert chk == inner, "jCMP 回读不一致"
    return out


# ---------------------------------------------------------------- jARC
class Entry:
    __slots__ = ("idx", "off", "size", "noff", "f4", "name")

    def __init__(self, idx, off, size, noff, f4, name):
        self.idx, self.off, self.size = idx, off, size
        self.noff, self.f4, self.name = noff, f4, name


def jarc_parse(data):
    if data[:4] != JARC:
        raise ValueError(f"不是 jARC（{data[:4].hex(' ').upper()}）")
    total, unknown, count = struct.unpack_from(">III", data, 4)
    if count > 500000:
        raise ValueError(f"条目数异常 {count}")
    ents = []
    for i in range(count):
        o = 16 + i * 16
        off, size, noff, f4 = struct.unpack_from(">IIII", data, o)
        e = data.find(b"\x00", noff) if noff < len(data) else -1
        if e < 0:
            e = len(data)
        name = data[noff:e].decode("utf-8", "replace")
        ents.append(Entry(i, off, size, noff, f4, name))
    return ents, unknown, total, count


def jarc_build(ents, blobs, unknown, align, data_start=None, names=None):
    """重建 jARC。

    关键修正（2026-10 实测）:
      条目之间按 align 补齐，但【最后一个条目后面不补】——原文件末尾紧接 EOF。
      之前给最后一条也补，导致总长多出 0~31 字节，整包重建被判不一致。

    data_start/names 可传入原文件的原始值，这样名字块一字不动，
    noff 字段全部沿用，重建结果与原文件逐字节相同。
    """
    n = len(ents)
    tbl_end = 16 + n * 16

    if names is None:
        names = bytearray()
        for e in ents:
            names += e.name.encode("utf-8") + b"\x00"
        names = bytes(names)
    if data_start is None:
        data_start = (tbl_end + len(names) + align - 1) & ~(align - 1)

    table = bytearray()
    body = bytearray()
    for i, (e, blob) in enumerate(zip(ents, blobs)):
        table += struct.pack(">IIII", data_start + len(body), len(blob),
                             e.noff, e.f4)
        body += blob
        if i != n - 1:                      # 最后一条不补
            body += b"\x00" * ((-len(body)) % align)

    total = data_start + len(body)
    out = bytearray(JARC)
    out += struct.pack(">III", total, unknown, n)
    out += table
    out += names
    if len(out) < data_start:
        out += b"\x00" * (data_start - len(out))
    out += body
    return bytes(out)


def jarc_layout(ents, data):
    """从原文件取出 (data_start, names_block_bytes)"""
    n = len(ents)
    tbl_end = 16 + n * 16
    ds = min(e.off for e in ents) if ents else tbl_end
    return ds, bytes(data[tbl_end:ds])


def jarc_fit_align(ents, unknown, data):
    """反推对齐值 -> (align, len_diff, byte_diff)

    分开报告两种差异：
      len_diff  重建长度 - 原长度
      byte_diff 等长前缀内不同的字节数
    """
    blobs = [data[e.off:min(e.off + e.size, len(data))] for e in ents]
    short = [(e.name, e.size, len(b)) for e, b in zip(ents, blobs)
             if len(b) != e.size]
    if short:
        print(f"  [!] {len(short)} 个条目 off+size 越界，切片被截断：")
        for nm, want, got in short[:8]:
            print(f"      {nm}: 声明 {want:,} 实际只有 {got:,}")
        if len(short) > 8:
            print(f"      ... 另有 {len(short)-8} 个")

    ds, names = jarc_layout(ents, data)
    best = None
    for align in (0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800, 0x1000):
        buf = jarc_build(ents, blobs, unknown, align, ds, names)
        ld = len(buf) - len(data)
        n = min(len(buf), len(data))
        bd = sum(1 for a, b in zip(buf[:n], data[:n]) if a != b)
        score = bd + abs(ld)
        if best is None or score < best[1]:
            best = (align, ld, bd)
        if score == 0:
            return align, 0, 0
    return best


# ---------------------------------------------------------------- 主流程
def load_any(path):
    """读文件 -> (inner_jarc_bytes, jcmp_info, outer_len)"""
    data = path.read_bytes()
    inner, info = jcmp_unwrap(data)
    return data, inner, info


def do_one(jarc_path, new_dir, out_path, report_only=False, level=9):
    outer, inner, cinfo = load_any(jarc_path)
    print(f"\n=== {jarc_path.name}  文件 {len(outer):,} B")
    if cinfo:
        print(f"  外层 jCMP: flags=0x{cinfo['flags']:08X}  "
              f"zsize={cinfo['zsize']:,}  raw={cinfo['rawsize']:,}  "
              f"{'解压长度吻合 ✓' if cinfo['ok'] else '解压长度不符 !'}")
    else:
        print("  外层: 裸 jARC（无 jCMP）")

    if inner[:4] != JARC:
        print(f"  [错误] 内层不是 jARC（{inner[:4].hex(' ').upper()}）")
        return False

    ents, unknown, total, count = jarc_parse(inner)
    print(f"  内层 jARC: {len(inner):,} B  条目 {count}  unknown=0x{unknown:08X}  "
          f"声明总长 {total:,} {'✓' if total == len(inner) else '!'}")

    align, ld, bd = jarc_fit_align(ents, unknown, inner)
    if ld == 0 and bd == 0:
        print(f"  对齐推断: 0x{align:X}  重建与原件完全一致 ✓")
    else:
        print(f"  对齐推断: 0x{align:X}  不完全一致  "
              f"(长度差 {ld:+,} 字节, 内容差 {bd:,} 字节)")
        print(f"  [!] 完整重封不可靠 —— 但若所有条目长度不变，仍会走 [A] 原地覆盖")

    # 条目类型统计
    kinds = {}
    for e in ents:
        d = inner[e.off:e.off + 4]
        k = "jCMP" if d == JCMP else ("jIMG" if d == JIMG else d.hex(" ").upper())
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  条目类型: {kinds}")

    if report_only:
        for e in ents[:24]:
            d = inner[e.off:e.off + 4]
            k = "jCMP" if d == JCMP else ("jIMG" if d == JIMG else
                                          d.hex(" ").upper())
            extra = ""
            if d == JCMP:
                try:
                    sub, si = jcmp_unwrap(inner[e.off:e.off + e.size])
                    extra = f" -> {sub[:4].decode('ascii', 'replace')}({len(sub):,})"
                except Exception:
                    extra = " -> 解压失败"
            print(f"    [{e.idx:3d}] off=0x{e.off:08X} size={e.size:9,} "
                  f"f4=0x{e.f4:08X} {k:6s}{extra}  {e.name}")
        if count > 24:
            print(f"    ... 另有 {count-24} 条")
        return True

    if not new_dir:
        print("  [跳过] 未指定 -n（新文件目录）")
        return False

    nd = Path(new_dir)
    if not nd.is_dir():
        print(f"  [跳过] 目录不存在: {nd}")
        return False

    # 收集新文件：按条目名匹配（条目名可能不含扩展名，做模糊匹配）
    files = {}
    for p in nd.rglob("*"):
        if p.is_file():
            files[p.name] = p
            files.setdefault(p.stem, p)

    repl, matched, missing = {}, [], []
    for e in ents:
        cand = files.get(e.name) or files.get(e.name + ".jtex") \
            or files.get(Path(e.name).stem)
        if cand:
            repl[e.name] = cand.read_bytes()
            matched.append(e.name)
        else:
            missing.append(e.name)

    print(f"  新文件目录 {nd}: 匹配 {len(matched)} / {count}")
    if not matched:
        print(f"  [跳过] 无匹配。目录内示例: {sorted(files)[:6]}")
        return False
    if missing and len(missing) <= 8:
        print(f"    未匹配: {missing}")

    # 逐条构造新 blob
    new_blobs = {}
    for e in ents:
        if e.name not in repl:
            continue
        nb = repl[e.name]
        orig = inner[e.off:e.off + e.size]
        if orig[:4] == JCMP:
            try:
                sub, si = jcmp_unwrap(orig)
            except Exception as ex:
                print(f"    [!] {e.name} 原 jCMP 解压失败: {ex}")
                continue
            if nb[:4] != JIMG and nb[:4] != JCMP:
                print(f"    [!] {e.name} 新文件不是 jIMG，跳过")
                continue
            blob = jcmp_wrap(nb, si, level)
            new_blobs[e.name] = blob
            print(f"    {e.name}: {len(sub):,} 解包 -> 重压 {len(blob):,} B "
                  f"(原 {e.size:,})")
        else:
            new_blobs[e.name] = nb
            print(f"    {e.name}: 裸数据 {e.size:,} -> {len(nb):,}")

    if not new_blobs:
        print("  [跳过] 没有可替换内容")
        return False

    # --- 方案 A：jARC 内原地覆盖 ---
    same = all(len(new_blobs[e.name]) == e.size
               for e in ents if e.name in new_blobs)
    if same:
        buf = bytearray(inner)
        for e in ents:
            if e.name in new_blobs:
                buf[e.off:e.off + e.size] = new_blobs[e.name]
        new_inner = bytes(buf)
        print(f"  [A] jARC 内原地覆盖（{len(new_blobs)} 条，长度不变）")
    else:
        blobs = [new_blobs.get(e.name, inner[e.off:e.off + e.size]) for e in ents]
        new_inner = jarc_build(ents, blobs, unknown, align)
        print(f"  [B] jARC 完整重封: {len(inner):,} -> {len(new_inner):,} B "
              f"(对齐 0x{align:X})")

    # --- 重新包 jCMP ---
    new_outer = jcmp_wrap(new_inner, cinfo, level) if cinfo else new_inner
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(new_outer)
    print(f"  输出 {len(outer):,} -> {len(new_outer):,} B  ->  {out_path}")

    # --- 自检：重新解析输出 ---
    chk_outer, chk_inner, chk_cinfo = load_any(out_path)
    chk_ents, chk_unk, chk_tot, chk_cnt = jarc_parse(chk_inner)
    bad = 0
    if chk_cnt != count:
        bad += 1
    for e1, e2 in zip(ents, chk_ents):
        if e1.name != e2.name or e1.f4 != e2.f4:
            bad += 1
            continue
        want = new_blobs.get(e1.name, inner[e1.off:e1.off + e1.size])
        if chk_inner[e2.off:e2.off + e2.size] != want:
            bad += 1
    print(f"  自检: 条目数 {chk_cnt}/{count}, 内容核对 "
          + ("全部通过 ✓" if bad == 0 else f"{bad} 处不符 !"))
    return bad == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jarc", nargs="+")
    ap.add_argument("-n", "--new", default=None)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("-e", "--ext", default="jarc")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--level", type=int, default=9, help="zlib 级别 (默认 9)")
    a = ap.parse_args()

    exts = tuple("." + s.strip().lower() for s in a.ext.split(",") if s.strip())
    targets = []
    for p in a.jarc:
        q = Path(p)
        if q.is_file():
            targets.append(q)
        elif q.is_dir():
            targets += sorted(f for f in q.glob("*")
                              if f.is_file() and f.suffix.lower() in exts)
        else:
            print(f"[跳过] 不存在: {p}")

    print(f"# 目标 {len(targets)} 个")
    if not targets:
        print("# [警告] 没有目标文件")
        return 1

    ok = 0
    for t in targets:
        nd = None
        if a.new:
            nd = Path(a.new) / t.stem if a.auto else Path(a.new)
        if a.out:
            op = Path(a.out)
            out = op / t.name if (op.is_dir() or op.suffix.lower() != ".jarc"
                                  or len(targets) > 1) else op
        else:
            out = t.with_name(t.stem + "_patched.jarc")
        try:
            if do_one(t, nd, out, a.report, a.level):
                ok += 1
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")
    print(f"\n# 完成 {ok}/{len(targets)}")
    if ok == 0:
        print("# [警告] 全部未成，先跑 --report 看结构")
    return 0


if __name__ == "__main__":
    sys.exit(main())
