#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
cmp.py - 对比本体与补丁（update）同名文件，找出必须改的那一份

背景:
  Wii U 更新包会覆盖本体。同路径下两份都存在时，游戏读【补丁】那份。
  所以改动必须打在 update 版本上，否则白改。

用法:
  py cmp.py                     # 对比 ui\ 和 fonts\ 下的 jarc
  py cmp.py -r                  # 递归对比所有同名文件
  py cmp.py --list-ui           # 顺便列出各 jarc 的条目数
"""
import argparse
import hashlib
import os
import struct
import sys
import zlib
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(os.environ.get("GAME_DIR") or Path(__file__).resolve().parent)
BASE = ROOT
UPD = ROOT / "update" / "content"


def sha(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unwrap(d):
    if len(d) >= 20 and d[:4] == b"jCMP":
        _s, _f, zs, _r = struct.unpack_from(">IIII", d, 4)
        return zlib.decompress(d[20:20 + zs])
    return d


def jarc_count(p):
    try:
        d = unwrap(p.read_bytes())
        if d[:4] != b"jARC":
            return None
        return struct.unpack_from(">I", d, 12)[0]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--recursive", action="store_true")
    ap.add_argument("--list-ui", action="store_true")
    a = ap.parse_args()

    if not UPD.is_dir():
        print(f"[错误] 找不到补丁目录: {UPD}")
        return 1

    pats = ["**/*"] if a.recursive else ["ui/*.jarc", "fonts/*.jarc",
                                        "US/ui/*.jarc", "SU/ui/*.jarc",
                                        "FU/ui/*.jarc", "US/Message/*.carc"]
    files = []
    for pat in pats:
        files += sorted(UPD.glob(pat))
    files = [f for f in files if f.is_file()]
    if not files:
        print(f"[警告] 补丁目录里没匹配到文件: {UPD}")
        return 1

    same = diff = only_upd = 0
    diff_list = []
    for u in files:
        rel = u.relative_to(UPD)
        b = BASE / rel
        us, bs = u.stat().st_size, (b.stat().st_size if b.is_file() else None)
        if bs is None:
            print(f"[仅补丁] {rel}  {us:,} B")
            only_upd += 1
            continue
        hs, hb = sha(u), sha(b)
        if hs == hb:
            same += 1
            continue
        diff += 1
        diff_list.append(rel)
        flag = "大小不同" if us != bs else "内容不同"
        print(f"[不同] {rel}")
        print(f"        本体 {bs:,} B  sha1 {hb[:16]}")
        print(f"        补丁 {us:,} B  sha1 {hs[:16]}   <-- 游戏读这份 ({flag})")

    print(f"\n# 补丁独有 {only_upd} 个，与本体重名同内容 {same} 个，"
          f"同名但不同 {diff} 个")

    if diff_list:
        print("\n# 必须改的是补丁版本：")
        for rel in diff_list:
            print(f"    update\\content\\{rel}")

    if a.list_ui:
        print("\n# 补丁内 ui jarc 条目数：")
        for u in sorted((UPD / "ui").glob("*.jarc")):
            n = jarc_count(u)
            print(f"    {u.name:32s} {u.stat().st_size:12,} B  "
                  f"条目 {n if n is not None else '?'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
