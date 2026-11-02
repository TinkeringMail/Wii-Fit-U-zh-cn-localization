#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unpack.py —— 递归通用解包器（Yaz0 / U8 多层嵌套，按 magic 判别，不依赖后缀）

自动识别并逐层展开：
  Yaz0      59 61 7A 30   -> 解压后在同一目标继续判别（纯包装，不产生新目录）
  U8/ARC    55 AA 38 2D   -> 解包成同名目录（去掉后缀）
  叶子      BRLYT/BRLAN/BRFNT/TPL/其他 -> 原样写出

深度上限默认 12，防止畸形文件导致无限递归。

用法:
  py unpack.py LayoutCommon -e dec -d out            # 递归解到底
  py unpack.py LayoutCommon -e dec --dry-run         # 只看层级不解
  py unpack.py 某文件.dec --diag                     # 打印头部与全部节点
  py unpack.py LayoutCommon -e dec --keep-container  # 容器文件也保留一份
  py unpack.py --selftest
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

U8_MAGIC = b"\x55\xAA\x38\x2D"
YAZ0_MAGIC = b"Yaz0"
YAZ0_HEADER = 0x10
MAX_EXPANSION = 91
MAX_OUTPUT = 512 * 1024 * 1024

# 叶子格式的特征 magic（只用于分类统计/命名，不影响数据）
LEAF_MAGICS = [
    (b"BRLYT", "BRLYT"),
    (b"BRLAN", "BRLAN"),
    (b"BRFNT", "BRFNT"),
    (b"BRPA", "BRPA"),
    (b"BRVC", "BRVC"),
    (b"BRMC", "BRMC"),
    (b"\x00\x20\xAF\x30", "TPL"),
    (b"RARC", "RARC"),
    (b"SARC", "SARC"),
]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class PackError(Exception):
    pass


def u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def u24(b, o):
    return int.from_bytes(b[o:o + 3], "big")


def detect(data: bytes) -> str:
    if data[:4] == YAZ0_MAGIC:
        return "yaz0"
    if data[:4] == U8_MAGIC:
        return "u8"
    return "raw"


def classify_leaf(data: bytes) -> str:
    for magic, name in LEAF_MAGICS:
        if data.startswith(magic):
            return name
    return "raw"


# --------------------------------------------------------------------------- #
# Yaz0（与 QuickBMS src/unz.c decodeYaz0 对齐）
# --------------------------------------------------------------------------- #
def yaz0_decompress(data: bytes) -> bytes:
    if data[:4] != YAZ0_MAGIC:
        raise PackError("不是 Yaz0")
    n_in = len(data)
    if n_in < YAZ0_HEADER:
        raise PackError("Yaz0 头不完整")
    dst_size = int.from_bytes(data[4:8], "big")
    if dst_size > MAX_OUTPUT:
        raise PackError(f"Yaz0 声明 {dst_size:,} 字节，超过安全上限")
    payload = n_in - YAZ0_HEADER
    if dst_size > payload * MAX_EXPANSION + 8:
        raise PackError(f"Yaz0 声明 {dst_size:,} 超出理论上限 {payload * MAX_EXPANSION:,}")

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


def yaz0_stub_compress(src: bytes) -> bytes:
    """全字面量编码（等价 QuickBMS encodeYaz0_fake），仅用于自检。"""
    out = bytearray(YAZ0_MAGIC + len(src).to_bytes(4, "big") + b"\x00" * 8)
    for i in range(0, len(src), 8):
        out.append(0xFF)
        out += src[i:i + 8]
    return bytes(out)


# --------------------------------------------------------------------------- #
# U8 解析
# --------------------------------------------------------------------------- #
class Node:
    __slots__ = ("idx", "is_dir", "name", "name_off", "data_off", "size")

    def __init__(self, idx, is_dir, name, name_off, data_off, size):
        self.idx, self.is_dir, self.name = idx, is_dir, name
        self.name_off, self.data_off, self.size = name_off, data_off, size


def _read_names(data, root_off, count, str_base, limit):
    """在 [str_base, limit) 内读全部名字；root 恒为空，其余（含目录）都读。"""
    names = []
    for i in range(count):
        o = root_off + i * 12
        noff = u24(data, o + 1)
        if i == 0:
            names.append("")
            continue
        s = str_base + noff
        if s >= limit:
            return None
        e = data.find(b"\x00", s, limit)
        if e < 0:
            return None
        raw = data[s:e]
        if any(c < 0x20 and c not in (0x09,) for c in raw):
            return None
        names.append(raw.decode("utf-8", "replace"))
    return names


def parse_u8(data: bytes, diag=None):
    n = len(data)
    if n < 0x20:
        raise PackError(f"文件太小（{n} 字节）")
    if data[:4] != U8_MAGIC:
        raise PackError(f"magic 不是 55AA382D（{data[:4].hex(' ').upper()}）")

    root_off = u32(data, 4)
    tbl_size = u32(data, 8)
    data_off = u32(data, 12)
    if not (0x10 <= root_off < n) or root_off + 12 > n:
        raise PackError(f"rootOffset 0x{root_off:X} 越界")

    count = u32(data, root_off + 8) or 1
    if count > 200000:
        raise PackError(f"节点数异常：{count}")
    str_base = root_off + count * 12
    if str_base > n:
        raise PackError(f"节点表越界（{count} 项需 {str_base} 字节，文件 {n}）")

    cands, seen = [], set()
    for c in (root_off + tbl_size, str_base + tbl_size, data_off, n):
        if str_base < c <= n and c not in seen:
            seen.add(c); cands.append(c)
    cands.sort()

    names = used = None
    for lim in cands:
        names = _read_names(data, root_off, count, str_base, lim)
        if names is not None:
            used = lim
            break

    if diag is not None:
        diag.update(size=n, root_off=root_off, tbl_size=tbl_size, data_off=data_off,
                    count=count, str_base=str_base, candidates=cands, name_limit=used)

    if names is None:
        raise PackError(
            f"字符串表解析失败（起点 0x{str_base:X}，候选上限 "
            f"{['0x%X' % c for c in cands]}）")

    nodes = []
    for i in range(count):
        o = root_off + i * 12
        nodes.append(Node(i, data[o] != 0, names[i], u24(data, o + 1),
                          u32(data, o + 4), u32(data, o + 8)))

    for nd in nodes:
        if nd.is_dir:
            continue
        if nd.data_off + nd.size > n:
            raise PackError(f"节点 {nd.idx}（{nd.name}）数据越界："
                            f"0x{nd.data_off:X}+{nd.size:,} > {n:,}")
        if nd.size and nd.data_off < 0x20:
            raise PackError(f"节点 {nd.idx}（{nd.name}）dataOff 0x{nd.data_off:X} 非法")
    return nodes


_BAD = set('<>:"/\\|?*')


def safe_name(name: str, fallback: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    name = "".join("_" if c in _BAD or ord(c) < 32 else c for c in name)
    return fallback if name in ("", ".", "..") else name


def walk_tree(nodes):
    """目录的 size = 子树之后第一个节点索引。"""
    if not nodes:
        return []
    stack, out = [], []
    i = 1
    while i < len(nodes):
        nd = nodes[i]
        if nd.is_dir:
            stack.append((nd, nd.size))
            i += 1
            while stack and i >= stack[-1][1]:
                stack.pop()
            continue
        rel = [safe_name(d.name, f"dir{d.idx}") for d, _ in stack]
        rel.append(safe_name(nd.name, f"file{nd.idx}"))
        out.append((Path(*rel), nd))
        i += 1
        while stack and i >= stack[-1][1]:
            stack.pop()
    return out


# --------------------------------------------------------------------------- #
# 递归解包
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, dry_run=False, keep_container=False, max_depth=12, log=None):
        self.dry_run = dry_run
        self.keep_container = keep_container
        self.max_depth = max_depth
        self.log = log or (lambda s: None)
        self.files = 0
        self.bytes = 0
        self.layers = Counter()
        self.errors = []
        self.dirs = set()

    def emit(self, path: Path):
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)


def write_leaf(target: Path, blob: bytes, ctx: Ctx):
    ctx.emit(target)
    if not ctx.dry_run:
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(blob)
        tmp.replace(target)
    ctx.files += 1
    ctx.bytes += len(blob)


def unpack(data: bytes, target: Path, ctx: Ctx, depth=0, label=""):
    kind = detect(data)

    # Yaz0 是纯包装：解压后落到同一目标，不建目录
    while kind == "yaz0":
        try:
            data = yaz0_decompress(data)
        except PackError as e:
            ctx.errors.append((str(target), f"Yaz0: {e}"))
            return
        ctx.layers["yaz0"] += 1
        kind = detect(data)

    if kind == "u8":
        if depth >= ctx.max_depth:
            ctx.errors.append((str(target), f"超过深度上限 {ctx.max_depth}"))
            write_leaf(target, data, ctx)
            return
        if ctx.keep_container:
            write_leaf(target, data, ctx)
        sub = target.with_suffix("") if target.suffix else target
        ctx.layers["u8"] += 1
        unpack_u8(data, sub, ctx, depth + 1)
        return

    leaf = classify_leaf(data)
    ctx.layers[leaf] += 1
    write_leaf(target, data, ctx)


def unpack_u8(data: bytes, out_dir: Path, ctx: Ctx, depth: int):
    try:
        nodes = parse_u8(data)
    except PackError as e:
        ctx.errors.append((str(out_dir), f"U8: {e}"))
        return
    entries = walk_tree(nodes)
    if ctx.dry_run:
        for rel, nd in entries:
            ctx.log(f"{'    ' * depth}{rel}  {nd.size:,} B")
    for rel, nd in entries:
        blob = data[nd.data_off:nd.data_off + nd.size]
        unpack(blob, out_dir / rel, ctx, depth, nd.name)


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #
def iter_files(paths, exts, recursive=True):
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in (p.rglob("*") if recursive else p.glob("*")):
                if not f.is_file():
                    continue
                if "*" in exts or f.name.lower().endswith(exts):
                    if f.name.endswith(".part"):
                        continue
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


def diag(path: Path):
    print("=" * 72)
    print(f"文件: {path}")
    raw = path.read_bytes()
    print(f"大小: {len(raw):,} B   magic: {raw[:4].hex(' ').upper()}  "
          f"分类: {detect(raw)}")
    data = raw
    if data[:4] == YAZ0_MAGIC:
        data = yaz0_decompress(data)
        print(f"Yaz0 解出: {len(data):,} B  magic: {data[:4].hex(' ').upper()}")
    if data[:4] != U8_MAGIC:
        print(f"非 U8，叶子类型: {classify_leaf(data)}")
        return
    d = {}
    nodes = parse_u8(data, diag=d)
    print(f"rootOffset=0x{d['root_off']:X}  tbl_size=0x{d['tbl_size']:X}  "
          f"dataOffset=0x{d['data_off']:X}  节点数={d['count']}")
    print(f"串表起点=0x{d['str_base']:X}  上限候选="
          f"{['0x%X' % c for c in d['candidates']]}  采用=0x{d['name_limit']:X}")
    for nd in nodes:
        print(f"  [{nd.idx:>3}] {'DIR ' if nd.is_dir else 'FILE'} "
              f"nameOff={nd.name_off:<6} dataOff=0x{nd.data_off:<9X} "
              f"size={nd.size:<10,} {nd.name!r}")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def build_u8(files):
    names = [""] + [n for n, _ in files]
    strtab = bytearray(b"\x00")
    offs = [0]
    for nm in names[1:]:
        offs.append(len(strtab)); strtab += nm.encode() + b"\x00"
    while len(strtab) % 4:
        strtab += b"\x00"
    count = len(names)
    abs_end = 0x20 + count * 12 + len(strtab)
    data_off = (abs_end + 0x1F) & ~0x1F

    nodes = bytearray(bytes([1]) + (0).to_bytes(3, "big")
                      + (0).to_bytes(4, "big") + count.to_bytes(4, "big"))
    cur = data_off
    for i, (nm, blob) in enumerate(files):
        nodes += bytes([0]) + offs[i + 1].to_bytes(3, "big") \
                 + cur.to_bytes(4, "big") + len(blob).to_bytes(4, "big")
        cur += len(blob)

    out = bytearray(U8_MAGIC + (0x20).to_bytes(4, "big")
                    + (abs_end - 0x20).to_bytes(4, "big")
                    + data_off.to_bytes(4, "big") + b"\x00" * 16)
    out += nodes + strtab
    out += b"\x00" * (data_off - len(out))
    for _, blob in files:
        out += blob
    return bytes(out)


def _selftest() -> int:
    ok = True

    # 1) 单层 U8
    arc = build_u8([("a.brlan", b"BRLAN\xfe\xff" + b"A" * 20)])
    ctx = Ctx()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x"
        unpack(arc, out, ctx)
        got = (out / "a.brlan").read_bytes()
    good = got == b"BRLAN\xfe\xff" + b"A" * 20 and ctx.files == 1
    print(f"[{'PASS' if good else 'FAIL'}] 单层 U8")
    ok &= good

    # 2) Yaz0 + U8 嵌套（模拟真实三层：Yaz0(U8(U8(file)))）
    inner = build_u8([("inner.brlyt", b"BRLYT\xfe\xff" + b"B" * 30)])
    outer = build_u8([("FPTrainingMenu.arc", inner)])
    wrapped = yaz0_stub_compress(outer)
    ctx = Ctx()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x"
        unpack(wrapped, out, ctx)
        leaf = out / "FPTrainingMenu" / "inner.brlyt"
        got = leaf.read_bytes() if leaf.exists() else None
    good = got == b"BRLYT\xfe\xff" + b"B" * 30 and ctx.layers["yaz0"] == 1 \
           and ctx.layers["u8"] == 2 and ctx.layers["BRLYT"] == 1
    print(f"[{'PASS' if good else 'FAIL'}] Yaz0→U8→U8 三层递归  {dict(ctx.layers)}")
    ok &= good

    # 3) 目录节点命名（root 空、非 root 目录读 nameOff）
    okd = True
    try:
        names = _read_names(arc, 0x20, 2, 0x20 + 24, len(arc))
        okd = names[0] == "" and names[1] != ""
    except Exception:
        okd = False
    print(f"[{'PASS' if okd else 'FAIL'}] 名字表解析（root 为空）")
    ok &= okd

    # 4) 错误 magic 不崩
    ctx = Ctx()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "y"
        unpack(b"NOPE" + b"\x00" * 30, out, ctx)
        got = (out).read_bytes()
    print(f"[{'PASS' if got.startswith(b'NOPE') else 'FAIL'}] 未知格式原样写出")
    ok &= got.startswith(b"NOPE")

    print("\n自检结果:", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="递归通用解包器（Yaz0 / U8 多层，按 magic 判别）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  py unpack.py LayoutCommon -e dec -d out\n"
               "  py unpack.py LayoutCommon -e dec --dry-run\n"
               "  py unpack.py 某文件.dec --diag\n")
    ap.add_argument("inputs", nargs="*")
    ap.add_argument("-e", "--ext", action="append", default=None,
                    help="输入后缀，逗号分隔，默认 *（全部）")
    ap.add_argument("-d", "--out-dir", default=None, help="输出根目录")
    ap.add_argument("-R", "--no-recursive", action="store_true", help="不递归子目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印层级不写盘")
    ap.add_argument("--keep-container", action="store_true",
                    help="容器文件（.arc/.dec）也保留一份")
    ap.add_argument("--max-depth", type=int, default=12)
    ap.add_argument("--diag", action="store_true", help="打印头部与全部节点")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.inputs:
        ap.print_help()
        return 2

    exts = _norm_exts(args.ext if args.ext else ["*"])

    if args.diag:
        for f in iter_files(args.inputs, exts, not args.no_recursive):
            try:
                diag(f)
            except PackError as e:
                print(f"[FAIL] {f}: {e}")
        return 0

    files = list(iter_files(args.inputs, exts, not args.no_recursive))
    if not files:
        print(f"未找到匹配 {exts} 的文件。")
        return 1

    print(f"输入后缀 {exts}   文件数 {len(files)}"
          f"{'   [DRY-RUN]' if args.dry_run else ''}")
    t0 = time.perf_counter()
    all_ctx = Ctx(dry_run=args.dry_run, keep_container=args.keep_container,
                  max_depth=args.max_depth,
                  log=(None if args.quiet else (lambda s: print(s))))
    for f in files:
        if args.out_dir:
            target = Path(args.out_dir) / f.parent.name / f.stem
        else:
            target = f.with_suffix("") if f.suffix else Path(str(f) + "_x")
        if args.dry_run and not args.quiet:
            print(f"-- {f}")
        try:
            unpack(f.read_bytes(), target, all_ctx)
        except PackError as e:
            all_ctx.errors.append((str(f), str(e)))
        except OSError as e:
            all_ctx.errors.append((str(f), f"I/O: {e}"))

    print("\n" + "=" * 70)
    print("层数统计：" + "  ".join(f"{k}×{v}" for k, v in
                                  sorted(all_ctx.layers.items())))
    print(f"写出文件 {all_ctx.files:,} 个，共 {all_ctx.bytes:,} B")
    print(f"错误 {len(all_ctx.errors)}   耗时 {time.perf_counter() - t0:.2f}s")
    for p, msg in all_ctx.errors[:25]:
        print(f"  ! {p}  ->  {msg}")
    if len(all_ctx.errors) > 25:
        print(f"  ... 另有 {len(all_ctx.errors) - 25} 条")
    print("=" * 70)
    return 0 if not all_ctx.errors else 1


if __name__ == "__main__":
    sys.exit(main())