#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jarcx.py —— jARC / jCMP 批量递归解包（UI 目录）

已用真实数据验证：
  jARC  "jARC" | u32 archive_size | u32 unknown(0x01402300) | u32 count
        16 + i*16 : u32 offset | u32 size | u32 name_off | u32 f4
        offset/size 相对文件起点；name_off 是绝对偏移，NUL 结尾
        ★ 条目间有 0x18~0x2C 不等的对齐填充，必须读 offset，禁止累加 size
  jCMP  "jCMP" | u32 file_size(=20+zsize) | u32 flags(0x01010100)
        | u32 zsize | u32 raw_size | zlib 流(78 01 …)

递归规则：
  Yaz0 / jCMP    纯包装 —— 原地解开，不建目录
  jARC / U8      容器   —— 展开成同名目录，继续递归
  其它           叶子   —— 原样写出，并统计 magic

用法:
  py jarcx.py ui -e jarc -d out            # 递归解到底
  py jarcx.py ui -e jarc --list            # 只列归档内容
  py jarcx.py ui -e jarc --dry-run         # 只看层级不写盘
  py jarcx.py ui\abs_data.jarc --diag      # 单文件详细诊断
  py jarcx.py --selftest
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

JARC_MAGIC = b"jARC"
JCMP_MAGIC = b"jCMP"
YAZ0_MAGIC = b"Yaz0"
U8_MAGIC = b"\x55\xAA\x38\x2D"
YAZ0_HEADER = 0x10
MAX_EXPANSION = 91
JCMP_HEADER = 20
MAX_DEPTH = 16

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class PackError(Exception):
    pass


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def detect(data: bytes) -> str:
    m = data[:4]
    if m == YAZ0_MAGIC:
        return "yaz0"
    if m == JCMP_MAGIC:
        return "jcmp"
    if m == JARC_MAGIC:
        return "jarc"
    if m == U8_MAGIC:
        return "u8"
    return "raw"


def magic_label(data: bytes) -> str:
    m = data[:4]
    if len(m) < 4:
        return "short"
    if all(32 <= c < 127 for c in m):
        return m.decode("ascii")
    return m.hex(" ").upper()


# --------------------------------------------------------------------------- #
# Yaz0
# --------------------------------------------------------------------------- #
def yaz0_decompress(data: bytes) -> bytes:
    if data[:4] != YAZ0_MAGIC:
        raise PackError("不是 Yaz0")
    n_in = len(data)
    if n_in < YAZ0_HEADER:
        raise PackError("Yaz0 头不完整")
    dst_size = u32(data, 4)
    payload = n_in - YAZ0_HEADER
    if dst_size > payload * MAX_EXPANSION + 8:
        raise PackError(f"Yaz0 声明 {dst_size:,} 超理论上限")

    dst = bytearray(dst_size)
    sp, dp = YAZ0_HEADER, 0
    while dp < dst_size:
        if sp >= n_in:
            raise PackError(f"Yaz0 流截断（{dp:,}/{dst_size:,}）")
        code = data[sp]; sp += 1
        if code == 0xFF:
            step = min(8, dst_size - dp)
            if sp + step > n_in:
                raise PackError("Yaz0 字面量区截断")
            dst[dp:dp + step] = data[sp:sp + step]
            sp += step; dp += step
            continue
        for _ in range(8):
            if dp >= dst_size:
                break
            if code & 0x80:
                if sp >= n_in:
                    raise PackError("Yaz0 字面量处截断")
                dst[dp] = data[sp]; sp += 1; dp += 1
            else:
                if sp + 1 >= n_in:
                    raise PackError("Yaz0 引用处截断")
                b1, b2 = data[sp], data[sp + 1]; sp += 2
                dist = ((b1 & 0x0F) << 8) | b2
                length = b1 >> 4
                if length == 0:
                    if sp >= n_in:
                        raise PackError("Yaz0 扩展长度处截断")
                    length = data[sp] + 0x12; sp += 1
                else:
                    length += 2
                start = dp - dist - 1
                if start < 0:
                    raise PackError(f"Yaz0 非法引用 dist={dist} @ {dp}")
                if dp + length > dst_size:
                    length = dst_size - dp
                avail = dp - start
                if length <= avail:
                    dst[dp:dp + length] = dst[start:start + length]
                else:
                    pat = bytes(dst[start:dp])
                    dst[dp:dp + length] = (pat * (length // avail + 1))[:length]
                dp += length
            code = (code << 1) & 0xFF
    return bytes(dst)


# --------------------------------------------------------------------------- #
# jCMP
# --------------------------------------------------------------------------- #
def parse_jcmp(data: bytes, *, lenient=False, info=None):
    if len(data) < JCMP_HEADER:
        raise PackError(f"jCMP 头不完整（{len(data)} 字节）")
    if data[:4] != JCMP_MAGIC:
        raise PackError(f"magic 不是 jCMP（{data[:4].hex(' ').upper()}）")
    file_size, flags, zsize, raw_size = struct.unpack_from(">IIII", data, 4)
    if info is not None:
        info.update(file_size=file_size, flags=flags, zsize=zsize,
                    raw_size=raw_size, actual=len(data),
                    size_consistent=(file_size == JCMP_HEADER + zsize))

    end = JCMP_HEADER + zsize
    payload = data[JCMP_HEADER:end] if (zsize and end <= len(data)) \
        else data[JCMP_HEADER:]
    if not payload:
        raise PackError(f"jCMP 无载荷（zsize={zsize}, raw_size={raw_size}）")

    last = None
    for wbits, tag in ((15, "zlib"), (-15, "raw-deflate"), (47, "auto")):
        try:
            obj = zlib.decompressobj(wbits)
            out = obj.decompress(payload) + obj.flush()
        except zlib.error as e:
            last = f"{tag}: {e}"
            continue
        if not out:
            last = f"{tag}: 输出 0 字节"
            continue
        if len(out) < raw_size and not lenient:
            last = (f"{tag}: 输出 {len(out):,} < 声明 {raw_size:,}"
                    f"（数据截断，--lenient 可接受部分结果）")
            continue
        return out, {"raw_size": raw_size, "got": len(out), "zsize": zsize,
                     "flags": flags, "file_size": file_size}
    raise PackError(f"jCMP 解压失败（zsize={zsize}）：{last}")


def build_jcmp(raw: bytes, flags=0x01010100) -> bytes:
    comp = zlib.compress(raw, 9)
    return (JCMP_MAGIC + struct.pack(">IIII", JCMP_HEADER + len(comp), flags,
                                     len(comp), len(raw)) + comp)


# --------------------------------------------------------------------------- #
# jARC
# --------------------------------------------------------------------------- #
class Entry:
    __slots__ = ("idx", "offset", "size", "name_off", "f4", "name")

    def __init__(self, idx, offset, size, name_off, f4, name):
        self.idx, self.offset, self.size = idx, offset, size
        self.name_off, self.f4, self.name = name_off, f4, name


def parse_jarc(data: bytes, info=None):
    n = len(data)
    if n < 16:
        raise PackError(f"jARC 头不完整（{n} 字节）")
    if data[:4] != JARC_MAGIC:
        raise PackError(f"magic 不是 jARC（{data[:4].hex(' ').upper()}）")
    archive_size, unknown, count = struct.unpack_from(">III", data, 4)
    if count > 200000:
        raise PackError(f"条目数异常：{count}")
    tbl_end = 16 + count * 16
    if tbl_end > n:
        raise PackError(f"条目表越界（{count} 项需 {tbl_end} 字节，文件 {n}）")

    entries = []
    for i in range(count):
        o = 16 + i * 16
        off, size, noff, f4 = struct.unpack_from(">IIII", data, o)
        if noff >= n:
            raise PackError(f"条目 {i} name_off {noff} 越界（文件 {n}）")
        e = data.find(b"\x00", noff, n)
        if e < 0:
            e = n
        name = data[noff:e].decode("utf-8", "replace")
        if off > n:
            raise PackError(f"条目 {i}（{name}）offset 0x{off:X} 越界")
        if off + size > n:
            # 允许 size 略超出（末尾对齐/截断），按可用长度裁剪并记录
            if off < n:
                size = n - off
            else:
                raise PackError(f"条目 {i}（{name}）数据越界")
        entries.append(Entry(i, off, size, noff, f4, name))

    if info is not None:
        info.update(archive_size=archive_size, unknown=unknown, count=count,
                    actual=n, size_consistent=(archive_size == n))
    return entries


def build_jarc(files, unknown=0x01402300) -> bytes:
    count = len(files)
    names = bytearray()
    name_offs = []
    for nm, _ in files:
        name_offs.append(16 + count * 16 + len(names))
        names += nm.encode("utf-8") + b"\x00"
    data_start = (16 + count * 16 + len(names) + 0x1F) & ~0x1F

    entries, body = bytearray(), bytearray()
    cur = data_start
    for i, (nm, blob) in enumerate(files):
        entries += struct.pack(">IIII", cur, len(blob), name_offs[i], 0)
        body += blob
        pad = (-len(body)) % 32
        body += b"\x00" * pad
        cur += len(blob) + pad

    total = data_start + len(body)
    out = bytearray(JARC_MAGIC + struct.pack(">III", total, unknown, count))
    out += entries + names
    out += b"\x00" * (data_start - len(out))
    out += body
    return bytes(out)


def safe_rel(name: str, fallback: str) -> Path:
    parts = []
    for piece in name.replace("\\", "/").split("/"):
        piece = piece.strip()
        if piece in ("", ".", ".."):
            continue
        piece = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in piece)
        parts.append(piece)
    return Path(*parts) if parts else Path(fallback)


# --------------------------------------------------------------------------- #
# 递归解包
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, dry_run=False, keep_container=False, lenient=False,
                 max_depth=MAX_DEPTH, log=None, quiet=False):
        self.dry_run = dry_run
        self.keep_container = keep_container
        self.lenient = lenient
        self.max_depth = max_depth
        self.log = log or (lambda s: None)
        self.quiet = quiet
        self.files = 0
        self.bytes = 0
        self.empty = 0
        self.layers = Counter()
        self.magics = Counter()
        self.magic_bytes = Counter()
        self.exts = Counter()
        self.errors = []

    def warn(self, msg):
        self.errors.append(msg)


def write_leaf(target: Path, blob: bytes, ctx: Ctx):
    ctx.magics[magic_label(blob)] += 1
    ctx.magic_bytes[magic_label(blob)] += len(blob)
    ctx.exts[target.suffix.lower() or "(无后缀)"] += 1
    ctx.files += 1
    ctx.bytes += len(blob)
    if not ctx.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(blob)
        tmp.replace(target)


def unpack(data: bytes, target: Path, ctx: Ctx, depth=0, tag=""):
    if depth > ctx.max_depth:
        ctx.warn(f"{target}: 超过深度上限 {ctx.max_depth}")
        return

    guard, kind = 0, detect(data)
    while kind in ("yaz0", "jcmp"):
        guard += 1
        if guard > 8:
            ctx.warn(f"{target}: 包装层超过 8 层，疑似循环")
            return
        try:
            if kind == "yaz0":
                data = yaz0_decompress(data)
                ctx.layers["yaz0"] += 1
            else:
                data, _ = parse_jcmp(data, lenient=ctx.lenient)
                ctx.layers["jcmp"] += 1
        except PackError as e:
            ctx.warn(f"{target} [{kind}]: {e}")
            return
        kind = detect(data)

    if kind in ("jarc", "u8"):
        if len(data) == 0:
            ctx.empty += 1
            return
        if ctx.keep_container:
            write_leaf(target, data, ctx)
        sub = target.with_suffix("") if target.suffix else target
        ctx.layers[kind] += 1
        try:
            if kind == "jarc":
                unpack_jarc(data, sub, ctx, depth + 1)
            else:
                unpack_u8(data, sub, ctx, depth + 1)
        except PackError as e:
            ctx.warn(f"{target} [{kind}]: {e}")
        return

    if len(data) == 0:
        ctx.empty += 1
        if not ctx.quiet and tag:
            ctx.log(f"{'  ' * (depth + 1)}{tag}  [空条目 0 B，跳过]")
        return

    write_leaf(target, data, ctx)
    if not ctx.quiet and tag:
        ctx.log(f"{'  ' * (depth + 1)}{target.name}  "
                f"[{magic_label(data)}]  {len(data):,} B")


def unpack_jarc(data: bytes, out_dir: Path, ctx: Ctx, depth: int):
    entries = parse_jarc(data)
    if not entries:
        if not ctx.quiet:
            ctx.log(f"{'  ' * depth}{out_dir.name}/  (空归档)")
        return
    for e in entries:
        blob = data[e.offset:e.offset + e.size]
        unpack(blob, out_dir / safe_rel(e.name, f"entry{e.idx}"),
               ctx, depth, tag=e.name)


def _u8_names(data, root_off, count, str_base, limit):
    out = []
    for i in range(count):
        if i == 0:
            out.append("")
            continue
        noff = int.from_bytes(data[root_off + i * 12 + 1: root_off + i * 12 + 4], "big")
        s = str_base + noff
        if s >= limit:
            raise PackError("nameOff 越界")
        e = data.find(b"\x00", s, limit)
        if e < 0:
            raise PackError("名字未终止")
        out.append(data[s:e].decode("utf-8", "replace"))
    return out


def unpack_u8(data: bytes, out_dir: Path, ctx: Ctx, depth: int):
    n = len(data)
    if n < 0x20:
        raise PackError(f"U8 太小（{n} 字节）")
    root_off, tbl_size, data_off = u32(data, 4), u32(data, 8), u32(data, 12)
    if not (0x10 <= root_off < n):
        raise PackError(f"U8 rootOffset 0x{root_off:X} 越界")
    count = u32(data, root_off + 8) or 1
    str_base = root_off + count * 12
    if str_base > n:
        raise PackError(f"U8 节点表越界（{count} 项需 {str_base}）")

    cands = sorted({c for c in (root_off + tbl_size, str_base + tbl_size,
                                data_off, n) if str_base < c <= n})
    names = None
    for lim in cands:
        try:
            names = _u8_names(data, root_off, count, str_base, lim)
            break
        except PackError:
            continue
    if names is None:
        raise PackError("U8 名字表解析失败")

    stack = []
    for i in range(1, count):
        o = root_off + i * 12
        is_dir = data[o] != 0
        doff, size = u32(data, o + 4), u32(data, o + 8)
        if is_dir:
            stack.append((safe_rel(names[i], f"dir{i}"), size))
            continue
        if doff + size > n:
            ctx.warn(f"{out_dir}/{names[i]}: U8 条目越界，跳过")
            continue
        rel = Path(*(p for d, _ in stack for p in d.parts)) / safe_rel(names[i], f"file{i}")
        unpack(data[doff:doff + size], out_dir / rel, ctx, depth, tag=names[i])
        while stack and i + 1 >= stack[-1][1]:
            stack.pop()


# --------------------------------------------------------------------------- #
# IO / CLI
# --------------------------------------------------------------------------- #
def iter_files(paths, exts, recursive=True):
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in (p.rglob("*") if recursive else p.glob("*")):
                if f.is_file() and not f.name.endswith(".part"):
                    if "*" in exts or f.name.lower().endswith(exts):
                        yield f
        else:
            print(f"[警告] 路径不存在：{p}", file=sys.stderr)


def _norm_exts(raw):
    out = []
    for item in raw:
        for piece in str(item).replace(";", ",").split(","):
            piece = piece.strip().lower()
            if piece:
                out.append("*" if piece in ("*", ".*", "all")
                           else (piece if piece.startswith(".") else "." + piece))
    return tuple(dict.fromkeys(out))


def _unwrap(data: bytes, lenient=False, max_layers=8):
    kinds, d = [], data
    while detect(d) in ("yaz0", "jcmp") and len(kinds) < max_layers:
        k = detect(d)
        d = (yaz0_decompress(d) if k == "yaz0"
             else parse_jcmp(d, lenient=lenient)[0])
        kinds.append(k)
    return d, kinds


def diag(path: Path):
    print("=" * 72)
    print(f"文件: {path}")
    raw = path.read_bytes()
    print(f"大小: {len(raw):,} B   magic: {raw[:4].hex(' ').upper()}   类型: {detect(raw)}")
    info = {}
    if raw[:4] == JCMP_MAGIC:
        try:
            parse_jcmp(raw, info=info)
        except PackError as e:
            print(f"  jCMP 解析失败: {e}")
        else:
            print(f"  jCMP: fileSize={info['file_size']:,} "
                  f"({'✓' if info['size_consistent'] else '✗ 20+zsize'})  "
                  f"flags=0x{info['flags']:08X}  zsize={info['zsize']:,}  "
                  f"rawSize={info['raw_size']:,}")
    try:
        data, kinds = _unwrap(raw, lenient=True)
    except PackError as e:
        print(f"  解包装失败: {e}")
        return
    if kinds:
        print(f"  包装层: {' -> '.join(kinds)}")
    print(f"  当前: {detect(data)}  magic={data[:4].hex(' ').upper()}  {len(data):,} B")
    if detect(data) != "jarc":
        return
    info2 = {}
    entries = parse_jarc(data, info=info2)
    print(f"  jARC: archiveSize={info2['archive_size']:,} "
          f"({'✓' if info2['size_consistent'] else '与文件大小不符'})  "
          f"unknown=0x{info2['unknown']:08X}  count={info2['count']}")
    for e in entries:
        head = data[e.offset:e.offset + 4]
        print(f"    [{e.idx:>3}] 0x{e.offset:<8X} {e.size:>10,} B  "
              f"name_off={e.name_off:<6} f4=0x{e.f4:08X}  {e.name!r}  "
              f"head={head.hex(' ').upper()}")


def list_jarc(path: Path, lenient=True):
    print("=" * 72)
    print(f"{path}")
    data = path.read_bytes()
    try:
        data, kinds = _unwrap(data, lenient=lenient)
    except PackError as e:
        print(f"  [失败] {e}")
        return
    if kinds:
        print(f"  (包装层: {' -> '.join(kinds)})")
    if detect(data) != "jarc":
        print(f"  ({detect(data)}, {len(data):,} B, magic {data[:4].hex(' ').upper()})")
        return
    info = {}
    for e in parse_jarc(data, info=info):
        print(f"  {e.size:>10,} B  0x{e.offset:<8X}  {e.name}")
    print(f"  -- {info['count']} 条目  archiveSize={info['archive_size']:,}")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    ok = True

    raw = b"jLYT" + bytes(range(256)) * 8
    blob = build_jcmp(raw)
    back, hdr = parse_jcmp(blob)
    good = back == raw and hdr["file_size"] == len(blob)
    print(f"[{'PASS' if good else 'FAIL'}] jCMP 往返 ({len(raw)} -> {len(blob)} B)")
    ok &= good

    files = [("layout.jlyt", b"jLYT" + b"A" * 100),
             ("a.jlym", b"jLYM" + b"B" * 50),
             ("sub/b.jlym", b"jLYM" + b"C" * 20)]
    arc = build_jarc(files)
    got = [(e.name, arc[e.offset:e.offset + e.size]) for e in parse_jarc(arc)]
    good = got == [(n, b) for n, b in files]
    print(f"[{'PASS' if good else 'FAIL'}] jARC 往返 {[n for n, _ in got]}")
    ok &= good

    # 零字节条目（真实 gdv_data.jarc 的情况）
    z = build_jarc([("zero.jlym", b""), ("real.jlym", b"jLYM" + b"D" * 30)])
    ents = parse_jarc(z)
    good = len(ents) == 2 and ents[0].size == 0
    print(f"[{'PASS' if good else 'FAIL'}] 零字节条目可解析")
    ok &= good

    # 空归档（真实 42 B 那三个的猜想结构）
    empty_arc = build_jarc([])
    good = parse_jarc(empty_arc) == []
    print(f"[{'PASS' if good else 'FAIL'}] 空 jARC（count=0）")
    ok &= good

    # 多层递归
    inner = build_jarc([("x.jlyt", build_jcmp(b"jLYT" + b"Z" * 40))])
    outer = build_jarc([("pkg.jarc", build_jcmp(inner))])
    ctx = Ctx(quiet=True)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o"
        unpack(outer, out, ctx)
        leaf = out / "pkg" / "x.jlyt"
        data = leaf.read_bytes() if leaf.exists() else None
    good = data == b"jLYT" + b"Z" * 40 and ctx.layers["jcmp"] == 2 and ctx.layers["jarc"] == 2
    print(f"[{'PASS' if good else 'FAIL'}] jARC→jCMP→jARC→jCMP  {dict(ctx.layers)}")
    ok &= good

    try:
        parse_jcmp(blob[:len(blob) - 30])
        print("[FAIL] 截断 jCMP 未拦截"); ok = False
    except PackError:
        print("[PASS] 截断 jCMP 正确拦截")

    print("\n自检结果:", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="jARC / jCMP 批量递归解包",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*")
    ap.add_argument("-e", "--ext", action="append", default=None,
                    help="输入后缀，默认 jarc（* 表示全部）")
    ap.add_argument("-d", "--out-dir", default=None)
    ap.add_argument("-R", "--no-recursive", action="store_true")
    ap.add_argument("--list", action="store_true", help="只列归档内容")
    ap.add_argument("--diag", action="store_true", help="单文件详细诊断")
    ap.add_argument("--dry-run", action="store_true", help="只遍历不写盘")
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument("--lenient", action="store_true", help="jCMP 截断时接受部分结果")
    ap.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.inputs:
        ap.print_help()
        return 2

    exts = _norm_exts(args.ext if args.ext else ["jarc"])

    if args.diag:
        for f in iter_files(args.inputs, exts, not args.no_recursive):
            try:
                diag(f)
            except (PackError, OSError) as e:
                print(f"[FAIL] {f}: {e}")
        return 0

    if args.list:
        for f in iter_files(args.inputs, exts, not args.no_recursive):
            try:
                list_jarc(f, lenient=args.lenient)
            except (PackError, OSError) as e:
                print(f"[FAIL] {f}: {e}")
        return 0

    files = list(iter_files(args.inputs, exts, not args.no_recursive))
    if not files:
        print(f"未找到匹配 {exts} 的文件。")
        return 1

    print(f"输入后缀 {exts}   文件数 {len(files)}"
          f"{'   [DRY-RUN]' if args.dry_run else ''}")
    t0 = time.perf_counter()
    ctx = Ctx(dry_run=args.dry_run, keep_container=args.keep_container,
              lenient=args.lenient, max_depth=args.max_depth,
              log=(lambda s: None) if args.quiet else (lambda s: print(s)),
              quiet=args.quiet)

    roots = {}
    for p in args.inputs:
        pp = Path(p)
        roots[str(pp)] = pp if pp.is_dir() else pp.parent

    for f in files:
        root = next((r for k, r in roots.items()
                     if str(f).lower().startswith(str(r).lower())), f.parent)
        try:
            rel_parent = f.parent.relative_to(root)
        except ValueError:
            rel_parent = Path()
        base = Path(args.out_dir) if args.out_dir else root
        target = base / rel_parent / (f.stem if f.suffix else f.name + "_x")
        if not args.quiet and not args.dry_run:
            print(f"-- {f}")
        try:
            unpack(f.read_bytes(), target, ctx)
        except (PackError, OSError) as e:
            ctx.warn(f"{f}: {e}")

    print("\n" + "=" * 72)
    print("层数统计：" + "  ".join(f"{k}×{v}" for k, v in sorted(ctx.layers.items())))
    print(f"写出文件 {ctx.files:,} 个，共 {ctx.bytes:,} B"
          f"（{ctx.bytes / 1048576:.1f} MiB）   空条目 {ctx.empty}")
    if ctx.exts:
        print("后缀分布：" + "  ".join(f"{k}×{v}" for k, v in
                                     ctx.exts.most_common(12)))
    if ctx.magics:
        print("叶子 magic（决定最内层真实格式）：")
        for m, n in ctx.magics.most_common(15):
            print(f"    {m:<12} ×{n:<6} {ctx.magic_bytes[m]:>12,} B")
    print(f"问题 {len(ctx.errors)}   耗时 {time.perf_counter() - t0:.2f}s")
    for m in ctx.errors[:25]:
        print(f"  ! {m}")
    if len(ctx.errors) > 25:
        print(f"  ... 另有 {len(ctx.errors) - 25} 条")
    print("=" * 72)
    return 0 if not ctx.errors else 1


if __name__ == "__main__":
    sys.exit(main())