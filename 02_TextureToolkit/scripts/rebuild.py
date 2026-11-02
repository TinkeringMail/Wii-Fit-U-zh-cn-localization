#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
rebuild.py - 从【补丁版】jarc 重做全部文字贴图并回封

为什么必须用补丁版:
  Wii U 更新包会覆盖本体，同路径下游戏读补丁那份。
  cmp.py 实测：update\content 下 24 个文件与本体的 sha1 全都不同。
  所以改动必须基于 update\content\... 的版本。

流程（每个目标 jarc 独立）:
  1. 读 update 版 jarc -> 外层 jCMP 解压 -> jARC 解析
  2. 按条目名取出 jIMG / jMDL 原始字节，写到临时目录
  3. 调 bctext.process() 改字
  4. 调 putback.do_one() 回封（长度不变走原地覆盖）
  5. 从输出 jarc 重新取出条目，解码成 PNG 存到 verify\ 供肉眼确认

用法:
  py rebuild.py                # 正式执行全部
  py rebuild.py --dry          # 只解包+出预览，不写 jarc
  py rebuild.py --only mme_us_eng_tex
"""
import argparse
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import bctext
import putback

UPD = Path(os.environ.get("GAME_DIR") or HERE) / "update" / "content"

# jarc 相对 update\content 的路径 -> 要处理的条目名
TARGETS = {
    r"ui\mme_us_eng_tex.jarc": [
        "bck_obj_txt_us_eng.jtex",
        "bck_obj_txt_baby_us_eng.jtex",
        "bck_obj_txt_pet_us_eng.jtex",
    ],
    r"ui\mme_eu_eng_tex.jarc": [
        "bck_obj_txt_eu_eng.jtex",
        "bck_obj_txt_baby_eu_eng.jtex",
        "bck_obj_txt_pet_eu_eng.jtex",
    ],
    r"ui\models_data.jarc": [
        "training_obj001_txt_us_eng.jmdl",
        "training_obj001_txt_eu_eng.jmdl",
    ],
}


def extract(inner, ents, names, outdir):
    """从 jARC 里按名字取出条目字节，写到 outdir"""
    outdir.mkdir(parents=True, exist_ok=True)
    got = {}
    for e in ents:
        if e.name in names:
            blob = inner[e.off:e.off + e.size]
            assert len(blob) == e.size, f"{e.name} 切片越界"
            p = outdir / e.name
            p.write_bytes(blob)
            got[e.name] = len(blob)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只出预览，不写 jarc")
    ap.add_argument("--only", default=None, help="只处理指定 jarc（去后缀）")
    ap.add_argument("--size", type=int, default=49,
                    help="小图字号（big 牌匾自动适配）")
    ap.add_argument("--stroke", type=int, default=3)
    ap.add_argument("--level", type=int, default=9)
    a = ap.parse_args()

    if not UPD.is_dir():
        print(f"[错误] 补丁目录不存在: {UPD}")
        return 1

    work = HERE / "work_upd"
    outdir = HERE / "out_sd" / "ui"
    verify = HERE / "verify"

    print(f"# 补丁根目录 {UPD}")
    print(f"# 工作目录   {work}")
    print(f"# 输出目录   {outdir}")

    total_ok = 0
    for rel, names in TARGETS.items():
        jarc_path = UPD / rel
        stem = jarc_path.stem
        if a.only and a.only != stem:
            continue
        print(f"\n{'='*66}")
        print(f"=== {rel}")
        print(f"{'='*66}")
        if not jarc_path.is_file():
            print(f"  [跳过] 补丁里没有这个文件")
            continue

        outer = jarc_path.read_bytes()
        inner, cinfo = putback.jcmp_unwrap(outer)
        if inner[:4] != b"jARC":
            print(f"  [错误] 内层不是 jARC（{inner[:4].hex(' ').upper()}）")
            continue
        ents, unknown, tot, cnt = putback.jarc_parse(inner)
        print(f"  jarc {len(outer):,} B -> 内层 {len(inner):,} B  条目 {cnt}  "
              f"unknown=0x{unknown:08X}")
        if cinfo:
            print(f"  外层 jCMP flags=0x{cinfo['flags']:08X} "
                  f"raw={cinfo['rawsize']:,} "
                  f"{'OK' if cinfo['ok'] else '长度不符!'}")

        src = work / stem
        got = extract(inner, ents, names, src)
        print(f"  取出 {len(got)} 个目标条目: "
              f"{ {k: f'{v:,}' for k, v in got.items()} }")
        if not got:
            print("  [跳过] 一个目标条目都没找到，检查名字")
            continue

        # 改字
        args = types.SimpleNamespace(
            text=None, size=a.size, font=None, color=None,
            stroke=a.stroke, stroke_color=None,
            preview=str(HERE / "pv_upd" / stem), dry=a.dry,
        )
        made = 0
        for f in sorted(src.iterdir()):
            print(f"\n  --- {f.name} ---")
            try:
                if bctext.process(f, src, args):
                    made += 1
            except Exception as ex:
                print(f"    [异常] {type(ex).__name__}: {ex}")
        print(f"\n  改动完成 {made}/{len(got)}")
        if made == 0:
            print("  [跳过] 没有成功改动的条目")
            continue

        if a.dry:
            print("  --dry：不写 jarc")
            total_ok += 1
            continue

        # 回封
        dst = outdir / jarc_path.name
        ok = putback.do_one(jarc_path, src, dst, False, a.level)
        if not ok:
            print(f"  [失败] 回封未通过自检")
            continue

        # 验证：从输出 jarc 重新取出并解码成 PNG
        v_outer = dst.read_bytes()
        v_inner, _ = putback.jcmp_unwrap(v_outer)
        v_ents, _u, _t, _c = putback.jarc_parse(v_inner)
        vdir = verify / stem
        vdir.mkdir(parents=True, exist_ok=True)
        n_ok = 0
        for e in v_ents:
            if e.name not in names:
                continue
            blob = v_inner[e.off:e.off + e.size]
            try:
                spots = bctext.find_jimgs(blob)
                for n, (off, size) in enumerate(spots):
                    img, meta = bctext.decode(blob[off:off + size])
                    if meta["W"] < 96 or meta["W"] < 2 * meta["H"]:
                        continue
                    p = vdir / f"{Path(e.name).stem}__t{n}.png"
                    img.save(p)
                    n_ok += 1
                    print(f"    验证图 {p.name}  {meta['W']}x{meta['H']}")
            except Exception as ex:
                print(f"    [验证失败] {e.name}: {type(ex).__name__}: {ex}")
        print(f"  验证输出 {n_ok} 张 -> {vdir}")
        total_ok += 1

    print(f"\n{'='*66}")
    print(f"# 完成 {total_ok} 个 jarc")
    if total_ok:
        print(f"# 待部署文件在: {outdir}")
        for p in sorted(outdir.glob("*.jarc")):
            print(f"    {p.name:26s} {p.stat().st_size:12,} B")
        if not a.dry:
            print(f"# 目视检查: {verify}\\<jarc名>\\*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
