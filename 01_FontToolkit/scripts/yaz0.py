#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yaz0.py —— Yaz0 批量解压工具（.carc / .szs / .bfres ... 后缀可自定义）

格式：Yaz0 = "Yaz0"(4) + 解压长度 u32be(4) + 8 字节保留 + 压缩流(从 0x10 起)
算法：严格对齐 QuickBMS src/unz.c:3869 decodeYaz0()（by thakis）
      —— 唯一有意偏离：QuickBMS 不含任何边界检查，本脚本保留检查以免静默产出坏文件。

用法:
  py yaz0.py LayoutCommon                          # 递归解压所有 .carc -> .dec
  py yaz0.py Gamefiles -e carc,szs -o .arc -d out -j 8
  py yaz0.py LayoutCommon -e carc --keep-name -o .dec
  py yaz0.py LayoutCommon --dry-run -v             # 只校验不写盘
  py yaz0.py --selftest                            # 内置自检（含膨胀比回归测试）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

MAGIC = b"Yaz0"
DEFAULT_HEADER = 0x10      # magic(4) + 解压长度(4) + 保留(8)

# 理论上界：最省的 token 是扩展长度引用，3 字节输入 -> 0xFF+0x12 = 273 字节输出。
# 273/3 = 91 字节输出 / 1 字节输入。注意这是"上界"而非"典型值"：
# 正常 Yaz0 文件膨胀 3~11 倍极其常见，绝不能按字面量的 8/9 来卡。
MAX_EXPANSION = 91
# 单文件解压结果上限，防止头部损坏导致巨型分配（可在命令行调整）
MAX_OUTPUT = 512 * 1024 * 1024

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class Yaz0Error(Exception):
    """Yaz0 数据损坏 / 截断 / 非法引用"""


class NotYaz0Error(Yaz0Error):
    """不是 Yaz0 文件（magic 不匹配）"""


# --------------------------------------------------------------------------- #
# 核心解压
# --------------------------------------------------------------------------- #
def yaz0_decompress(data: bytes, *, header_size: int = DEFAULT_HEADER,
                    search_magic: bool = False, strict: bool = True,
                    max_output: int = MAX_OUTPUT) -> bytes:
    """
    解压一段 Yaz0 数据，返回解压后的 bytes。

    header_size  : 压缩流相对 magic 的起始偏移，标准 0x10
    search_magic : magic 不在偏移 0 时，在前 64KB 内搜索
    strict       : 做"数学上不可能"的合理性校验（不会误伤正常文件）
    max_output   : 单次解压结果字节上限
    """
    n_in = len(data)
    base = 0
    if data[0:4] != MAGIC:
        if not search_magic:
            raise NotYaz0Error(f"magic 不匹配：{data[0:4]!r}")
        base = data.find(MAGIC, 0, 0x10000)
        if base < 0:
            raise NotYaz0Error("前 64KB 内未找到 'Yaz0'")
    if n_in < base + header_size:
        raise Yaz0Error(f"文件太短（{n_in} 字节），不足 {header_size} 字节头部")

    dst_size = int.from_bytes(data[base + 4: base + 8], "big")
    payload = n_in - (base + header_size)

    if dst_size > max_output:
        raise Yaz0Error(
            f"头部声明解压长度 {dst_size:,} 超过上限 {max_output:,}"
            f"（可用 --max-output 调整）")

    # 正确上界：payload * 91。这是"数学上不可能"的判据，不是经验值。
    max_possible = payload * MAX_EXPANSION + 8
    if strict and dst_size > max_possible:
        raise Yaz0Error(
            f"头部声明解压长度 {dst_size:,} 超过理论上限 {max_possible:,}"
            f"（压缩流仅 {payload:,} 字节，Yaz0 最大膨胀 91x），文件确实损坏"
            f"（确认为正常文件可加 --force）")

    dst = bytearray(dst_size)
    src_pos = base + header_size
    dst_pos = 0

    while dst_pos < dst_size:
        if src_pos >= n_in:
            raise Yaz0Error(f"压缩流被截断（已输出 {dst_pos:,}/{dst_size:,} 字节）")
        code = data[src_pos]
        src_pos += 1

        # 快路径：8 bit 全 1 = 连续 8 个字面量
        if code == 0xFF:
            n = 8 if dst_pos + 8 <= dst_size else dst_size - dst_pos
            if src_pos + n > n_in:
                raise Yaz0Error("压缩流在字面量区被截断")
            dst[dst_pos: dst_pos + n] = data[src_pos: src_pos + n]
            src_pos += n
            dst_pos += n
            continue

        for _ in range(8):
            if dst_pos >= dst_size:
                break

            if code & 0x80:                                 # 字面量
                if src_pos >= n_in:
                    raise Yaz0Error("压缩流在字面量处被截断")
                dst[dst_pos] = data[src_pos]
                src_pos += 1
                dst_pos += 1
            else:                                           # 后向引用
                if src_pos + 1 >= n_in:
                    raise Yaz0Error("压缩流在引用处被截断")
                b1 = data[src_pos]
                b2 = data[src_pos + 1]
                src_pos += 2

                dist = ((b1 & 0x0F) << 8) | b2
                length = b1 >> 4
                if length == 0:                             # 扩展长度，+0x12
                    if src_pos >= n_in:
                        raise Yaz0Error("压缩流在扩展长度处被截断")
                    length = data[src_pos] + 0x12
                    src_pos += 1
                else:
                    length += 2

                start = dst_pos - dist - 1
                if start < 0:
                    raise Yaz0Error(
                        f"非法后向引用：偏移 {dist} 越过输出起点"
                        f"（位置 dst={dst_pos}）")

                # QuickBMS 允许最后一次 run 越过 dst_size；这里截断，输出内容相同
                if dst_pos + length > dst_size:
                    length = dst_size - dst_pos

                avail = dst_pos - start                     # 已有的非重叠字节数
                if length <= avail:
                    dst[dst_pos: dst_pos + length] = dst[start: start + length]
                else:                                       # 重叠（RLE 式），周期展开
                    pattern = bytes(dst[start: dst_pos])
                    dst[dst_pos: dst_pos + length] = (
                        pattern * (length // avail + 1))[:length]
                dst_pos += length

            code = (code << 1) & 0xFF

    return bytes(dst)


# --------------------------------------------------------------------------- #
# 单文件任务（进程池 worker，必须模块级）
# --------------------------------------------------------------------------- #
def _process_job(job):
    src_s, dst_s, opt = job
    src, dst = Path(src_s), Path(dst_s)
    t0 = time.perf_counter()
    res = {"src": str(src), "dst": str(dst), "status": "error",
           "src_size": 0, "out_size": 0, "seconds": 0.0, "message": ""}
    try:
        if src.resolve() == dst.resolve():
            raise Yaz0Error("输出路径与输入相同，请修改 --out-ext / --keep-name")
        data = src.read_bytes()
        res["src_size"] = len(data)

        if dst.exists() and not opt["overwrite"]:
            res["status"] = "skipped"
            res["message"] = "输出已存在（--overwrite 可覆盖）"
            res["seconds"] = time.perf_counter() - t0
            return res

        out = yaz0_decompress(data,
                              header_size=opt["header_size"],
                              search_magic=opt["search_magic"],
                              strict=opt["strict"],
                              max_output=opt["max_output"])
        res["out_size"] = len(out)

        if opt["dry_run"]:
            res["status"] = "dry"
            return res

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        tmp.write_bytes(out)
        os.replace(tmp, dst)
        res["status"] = "ok"
    except NotYaz0Error as e:
        res["status"] = "not_yaz0"
        res["message"] = str(e)
    except Yaz0Error as e:
        res["status"] = "error"
        res["message"] = str(e)
    except MemoryError:
        res["status"] = "error"
        res["message"] = "内存不足（文件头声明的解压长度过大）"
    except OSError as e:
        res["status"] = "error"
        res["message"] = f"I/O 错误: {e}"
    except Exception as e:
        res["status"] = "error"
        res["message"] = f"{type(e).__name__}: {e}"
    finally:
        res["seconds"] = time.perf_counter() - t0
    return res


# --------------------------------------------------------------------------- #
# 路径处理
# --------------------------------------------------------------------------- #
def _norm_exts(raw):
    exts = []
    for item in raw:
        for piece in str(item).replace(";", ",").split(","):
            piece = piece.strip().lower()
            if not piece:
                continue
            exts.append("*" if piece in ("*", ".*", "all") else
                        (piece if piece.startswith(".") else "." + piece))
    return tuple(dict.fromkeys(exts))


def iter_inputs(paths, exts, recursive):
    seen = set()
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
            continue
        if not p.is_dir():
            print(f"[警告] 路径不存在：{p}", file=sys.stderr)
            continue
        it = p.rglob("*") if recursive else p.glob("*")
        for f in it:
            if not f.is_file():
                continue
            if "*" in exts or f.name.lower().endswith(exts):
                key = str(f.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    yield f


def build_dst(src: Path, root: Path, out_dir, out_ext: str,
              keep_name: bool, flat: bool) -> Path:
    if keep_name:
        name = src.name + out_ext
    else:
        name = (src.name[: -len(src.suffix)] if src.suffix else src.name) + out_ext
    if out_dir is None:
        return src.with_name(name)
    if flat:
        return Path(out_dir) / name
    try:
        rel = src.parent.relative_to(root)
    except ValueError:
        rel = Path()
    return Path(out_dir) / rel / name


# --------------------------------------------------------------------------- #
# 自检（含本次 bug 的回归测试）
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    def wrap(stream: bytes, out_size: int) -> bytes:
        return MAGIC + out_size.to_bytes(4, "big") + b"\x00" * 8 + stream

    ok = True

    def check(name, blob, expect, must_pass_strict=True):
        nonlocal ok
        try:
            got = yaz0_decompress(blob)
            good = got == expect
            detail = f"len={len(got)}" if not good else f"{len(got)} B"
        except Exception as e:
            got, good, detail = None, (not must_pass_strict), f"{type(e).__name__}: {e}"
        print(f"[{'PASS' if good else 'FAIL'}] {name}: {detail}")
        ok &= good

    # 1) 纯字面量：code=0xFF + 8 字节
    lit = b"01234567"
    check("8 个字面量", wrap(b"\xFF" + lit, 8), lit)

    # 2) 短引用：ABC + (len=6, dist=2) -> ABCABCABC
    check("短后向引用", wrap(b"\xE0ABC\x40\x02", 9), b"ABCABCABC")

    # 3) 扩展长度 + 重叠复制：'A' + (len=0xFF+0x12=273, dist=0)
    check("扩展长度/重叠", wrap(b"\x80A\x00\x00\xFF", 274), b"A" * 274)

    # 4) 【回归测试】高膨胀比文件必须通过 strict —— 上一版 (payload//9)*8 的 bug
    #    9 B -> 8 B 字面量，再 25 B -> 8*273 B，合计 34 B in / 2192 B out（64x）
    stream = b"\xFF12345678" + b"\x00" + (b"\x00\x00\xFF" * 8)
    expect = b"12345678" + b"8" * (273 * 8)
    check("高膨胀比(64x)通过 strict", wrap(stream, len(expect)), expect)
    print(f"        └ 上一版校验会误报上限 = {(34//9)*8 + (34%9-1)}"
          f" < {len(expect)}，本版上限 = {34*MAX_EXPANSION+8}")

    # 5) 损坏头部：声称 6 MB，但压缩流只有 20 字节 —— 必须被拦住
    check("损坏头部被拦截", wrap(b"\xFFABCDEFGH", 6 * 1024 * 1024),
          b"", must_pass_strict=False)

    # 6) 空输出
    check("空输出", wrap(b"", 0), b"")

    # 7) 非 Yaz0
    try:
        yaz0_decompress(b"NOPE" + b"\x00" * 20)
        print("[FAIL] 非 Yaz0 未报错")
        ok = False
    except NotYaz0Error:
        print("[PASS] 非 Yaz0 正确报错")

    print("\n自检结果:", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Yaz0 批量解压（严格对齐 QuickBMS src/unz.c decodeYaz0）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  py yaz0.py LayoutCommon\n"
               "  py yaz0.py Gamefiles -e carc,szs -o .arc -d out -j 8\n"
               "  py yaz0.py LayoutCommon -e carc --keep-name -o .dec\n"
               "  py yaz0.py LayoutCommon --dry-run -v\n")
    ap.add_argument("inputs", nargs="*", help="文件或目录（可多个）")
    ap.add_argument("-e", "--ext", action="append", default=None,
                    help="源后缀，逗号分隔，默认 carc（* 表示全部）")
    ap.add_argument("-o", "--out-ext", default=".dec", help="输出后缀，默认 .dec")
    ap.add_argument("-k", "--keep-name", action="store_true",
                    help="保留原名（x.carc -> x.carc.dec）")
    ap.add_argument("-d", "--out-dir", default=None, help="输出根目录（镜像目录结构）")
    ap.add_argument("--flat", action="store_true", help="配合 -d：平铺不镜像")
    ap.add_argument("-R", "--no-recursive", action="store_true", help="不递归子目录")
    ap.add_argument("-j", "--jobs", type=int, default=0,
                    help="并行进程数，0=CPU 核数，1=串行")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已存在输出")
    ap.add_argument("--dry-run", action="store_true", help="只解压不写盘")
    ap.add_argument("--header-size", default="0x10", help="压缩流起点，默认 0x10")
    ap.add_argument("--search-magic", action="store_true",
                    help="magic 不在偏移 0 时，前 64KB 内搜索")
    ap.add_argument("--max-output", default=None,
                    help=f"单文件解压结果上限（字节，支持 512M 写法），"
                         f"默认 {MAX_OUTPUT}")
    ap.add_argument("--force", action="store_true", help="跳过合理性校验")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐文件输出")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出错误和汇总")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.inputs:
        ap.print_help()
        return 2

    exts = _norm_exts(args.ext if args.ext else ["carc"])
    out_ext = (args.out_ext if args.out_ext.startswith(".")
               else "." + args.out_ext)
    header_size = int(str(args.header_size), 0)

    if args.max_output is None:
        max_output = MAX_OUTPUT
    else:
        s = str(args.max_output).strip().upper().replace("B", "")
        mult = 1
        if s.endswith("K"):
            mult, s = 1024, s[:-1]
        elif s.endswith("M"):
            mult, s = 1024 ** 2, s[:-1]
        elif s.endswith("G"):
            mult, s = 1024 ** 3, s[:-1]
        max_output = int(float(s) * mult)

    jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 4)

    opt = {"header_size": header_size, "search_magic": args.search_magic,
           "strict": not args.force, "overwrite": args.overwrite,
           "dry_run": args.dry_run, "max_output": max_output}

    jobs_list, n_found = [], 0
    for p in args.inputs:
        pp = Path(p)
        root = pp if pp.is_dir() else pp.parent
        src_files = list(iter_inputs([p], exts, not args.no_recursive))
        n_found += len(src_files)
        for s in src_files:
            jobs_list.append((str(s),
                              str(build_dst(s, root, args.out_dir, out_ext,
                                            args.keep_name, args.flat)),
                              opt))

    if not n_found:
        print(f"未找到匹配 {exts} 的文件。")
        return 1

    print(f"源后缀: {exts}   输出后缀: {out_ext!r}   "
          f"文件数: {len(jobs_list)}   并行: {jobs}"
          f"{'   [DRY-RUN]' if args.dry_run else ''}")

    t0 = time.perf_counter()
    results = []

    if jobs <= 1 or len(jobs_list) == 1:
        for i, job in enumerate(jobs_list, 1):
            r = _process_job(job)
            results.append(r)
            if not args.quiet and (args.verbose or r["status"] == "error"
                                   or r["status"] == "not_yaz0"):
                _print_result(r, i, len(jobs_list))
            elif not args.quiet and i % 20 == 0:
                print(f"  ... {i}/{len(jobs_list)}")
    else:
        done = 0
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(_process_job, j): j for j in jobs_list}
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                done += 1
                if not args.quiet and (args.verbose or r["status"] == "error"
                                       or r["status"] == "not_yaz0"):
                    _print_result(r, done, len(jobs_list))
                elif not args.quiet and done % 50 == 0:
                    print(f"  ... {done}/{len(jobs_list)}")

    _print_summary(results, time.perf_counter() - t0)
    return 0 if all(r["status"] in ("ok", "skipped", "dry")
                    for r in results) else 1


def _print_result(r, idx, total):
    tag = {"ok": "OK  ", "dry": "DRY ", "skipped": "SKIP",
           "not_yaz0": "NOYZ", "error": "ERR "}[r["status"]]
    line = f"[{tag}] {idx}/{total} {r['src']}"
    if r["status"] in ("ok", "dry") and r["src_size"]:
        line += (f"  {r['src_size']:,} -> {r['out_size']:,} B"
                 f"  ({r['out_size'] / r['src_size']:.2f}x, {r['seconds']:.2f}s)")
    if r["message"]:
        line += f"  // {r['message']}"
    print(line)


def _print_summary(results, elapsed):
    cnt, tin, tout = {}, 0, 0
    for r in results:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
        tin += r["src_size"]
        tout += r["out_size"]
    print("\n" + "=" * 68)
    print(f"成功 {cnt.get('ok', 0)}  试运行 {cnt.get('dry', 0)}  "
          f"跳过 {cnt.get('skipped', 0)}  非Yaz0 {cnt.get('not_yaz0', 0)}  "
          f"失败 {cnt.get('error', 0)}")
    if tout:
        print(f"输入 {tin:,} B  ->  输出 {tout:,} B   (压缩比 {tin / tout:.3f})")
    else:
        print(f"输入 {tin:,} B")
    print(f"耗时 {elapsed:.2f}s")
    errs = [r for r in results if r["status"] == "error"]
    if errs:
        print("\n失败明细：")
        for r in errs[:30]:
            print(f"  {r['src']}  ->  {r['message']}")
        if len(errs) > 30:
            print(f"  ... 另有 {len(errs) - 30} 条")
    print("=" * 68)


if __name__ == "__main__":
    sys.exit(main())