#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r'''
shortset.py - 只统计"短文本"用到的汉字，生成大字体（标题/大按钮）的字符子集

思路:
  DB_L_3（71x106）和 EB_L_3（71x108）这类大字号字体，按推测只用于标题和
  大按钮文字，字形数量很少。给它们塞全部译文字符（1800+ 字）纯属浪费。
  本脚本只收集"短文本"里出现的汉字，作为这两个字体的字符集，
  从而把追加量压到最小。

长度计算规则（按你的要求）:
  1) 先剥离占位符 {{...}}（如 {{255:0 arg="0x0100"}}），
     占位符内部的数字和引号不计入长度
  2) 剥离后剩下的 {{ 与 }} 视为字面大括号，也不计入
  3) 换行不计入字符数；但会统计"行数"，可用 --max-lines 限制
     （多行长段落不算短文本）
  4) 统计两个量:
       cjk   汉字个数（U+3400-4DBF / U+4E00-9FFF / U+F900-FAFF）
       vis   可见字符数（去掉空白后的全部字符，含数字/标点/拉丁）
  5) 入选条件: min-cjk <= cjk <= max-cjk 且 vis <= max-total 且 行数 <= max-lines

输出:
  font_chars/<字体名>.txt   该字体应包含的字符（覆盖式写入，原文件备份为 .full.txt）
  <out>/_short_report.txt   统计、样本、体积账

用法:
  py shortset.py                          # 只看统计，不写文件
  py shortset.py --write                  # 写入 font_chars/
  py shortset.py --max-cjk 12 --list 30   # 放宽阈值并预览
  py shortset.py --max-cjk 6 --write --out font_chars_tight
'''

import sys
import re
import collections
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from jfntbuild import JFNT

CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
PUA_LO, PUA_HI = 0xE000, 0xF8FF

RE_TAG2 = re.compile(r"\{\{[^{}]*\}\}")     # {{255:0 arg="0x0100"}}
RE_TAG1 = re.compile(r"\{[^{}]*\}")         # {6400xxxxxx} 形式，兜底


def is_cjk(cp):
    for lo, hi in CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def find_message_txt(script_file=None):
    """自动查找 message 目录下大于 900KB、含 --- 块标记的 .txt，取最大者。

    与 config.bat 的判定规则一致；未找到时返回 None。
    """
    toolkit = Path(script_file or __file__).resolve().parent.parent
    msg_dir = toolkit / "message"
    if not msg_dir.is_dir():
        return None
    cands = []
    for p in msg_dir.glob("*.txt"):
        try:
            st = p.stat()
            if st.st_size <= 900 * 1024:
                continue
            with p.open("rb") as fh:
                head = fh.read(8192)
            if b"---" not in head:
                continue
        except OSError:
            continue
        cands.append((st.st_size, p))
    return max(cands)[1] if cands else None


def strip_tags(t):
    """剥离控制标签与字面大括号"""
    s = RE_TAG2.sub("", t)
    s = RE_TAG1.sub("", s)
    s = s.replace("{{", "").replace("}}", "")
    return s


def count_cjk(s):
    return sum(1 for ch in s if is_cjk(ord(ch)))


def count_vis(s):
    return sum(1 for ch in s if not ch.isspace())


def count_lines(s):
    return sum(1 for ln in s.splitlines() if ln.strip())


def need_chars(s, include_non_cjk=False):
    """该文本需要用字体重画出来的字符。
       默认只要汉字（ASCII/数字/标点保留原版字形）。"""
    out = set()
    for ch in s:
        o = ord(ch)
        if ch.isspace() or o < 0xA0:
            continue
        if PUA_LO <= o <= PUA_HI:
            continue
        if is_cjk(o):
            out.add(o)
        elif include_non_cjk:
            out.add(o)
    return out


# ---------------------------------------------------------------- txt 解析
def parse_txt(path):
    """解析 wbmgt 导出格式 -> [(id, attr, text)]"""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.splitlines()
    n = len(lines)
    msgs = []
    i = 0

    def next_nonempty(k):
        while k < n and not lines[k].strip():
            k += 1
        return k

    while i < n:
        if lines[i].strip() != "---":
            i += 1
            continue
        j = i + 1
        mid = None
        attr = None
        ok = True
        while j < n and lines[j].strip() != "---":
            s = lines[j].strip()
            if s.startswith("id:"):
                try:
                    mid = int(s[3:].strip())
                except ValueError:
                    ok = False
            elif s.startswith("attribute:"):
                try:
                    attr = int(s.split(":", 1)[1].strip(), 16)
                except ValueError:
                    attr = None
            j += 1
        if not ok or mid is None:
            i = j + 1
            continue
        k = j + 1
        buf = []
        while k < n:
            if lines[k].strip() == "---":
                m = next_nonempty(k + 1)
                if m < n and lines[m].strip().startswith("id:"):
                    break
            buf.append(lines[k])
            k += 1
        msgs.append((mid, attr, "\n".join(buf).strip("\n")))
        i = k
    return msgs


# ---------------------------------------------------------------- 主
def main():
    argv = sys.argv[1:]
    _auto_txt = find_message_txt()
    txt = str(_auto_txt) if _auto_txt else ""
    fonts_dir = r"update\content\fonts"
    out_dir = "font_chars"
    targets_s = "style_font_DB_L_3.jfnt,style_font_EB_L_3.jfnt,style_font_DB_3_I.jfnt"
    max_cjk = 10
    min_cjk = 1
    max_total = 24
    max_lines = 2
    write = False
    backup = True
    include_non_cjk = False
    show = 0
    extra = ""

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--txt":
            txt = argv[i + 1]; i += 2
        elif a == "--fonts":
            fonts_dir = argv[i + 1]; i += 2
        elif a == "--out":
            out_dir = argv[i + 1]; i += 2
        elif a == "--targets":
            targets_s = argv[i + 1]; i += 2
        elif a == "--max-cjk":
            max_cjk = int(argv[i + 1]); i += 2
        elif a == "--min-cjk":
            min_cjk = int(argv[i + 1]); i += 2
        elif a == "--max-total":
            max_total = int(argv[i + 1]); i += 2
        elif a == "--max-lines":
            max_lines = int(argv[i + 1]); i += 2
        elif a == "--write":
            write = True; i += 1
        elif a == "--no-backup":
            backup = False; i += 1
        elif a == "--include-non-cjk":
            include_non_cjk = True; i += 1
        elif a == "--list":
            show = int(argv[i + 1]); i += 2
        elif a == "--extra":
            extra = argv[i + 1]; i += 2
        else:
            i += 1

    targets = [t.strip() for t in targets_s.split(",") if t.strip()]

    # ---------- 读译文 ----------
    if not Path(txt).exists():
        print("[错误] 译文不存在: %s" % txt)
        return 1
    msgs = parse_txt(txt)
    if not msgs:
        print("[错误] 解析到 0 条消息")
        return 1
    print("=" * 96)
    print("译文 %s" % txt)
    print("  消息 %d 条" % len(msgs))

    # ---------- 逐条统计 ----------
    rows = []
    no_cjk = 0
    for mid, attr, raw in msgs:
        clean = strip_tags(raw)
        n_cjk = count_cjk(clean)
        if n_cjk == 0:
            no_cjk += 1
            continue
        rows.append((mid, n_cjk, count_vis(clean), count_lines(clean), clean))

    print("  剥离占位符后无汉字 %d 条，有汉字 %d 条" % (no_cjk, len(rows)))

    # ---------- 长度分布 ----------
    print("\n【1】汉字数分布")
    hist = collections.Counter(r[1] for r in rows)
    for th in (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30):
        c = sum(1 for r in rows if r[1] <= th)
        print("    <=%2d 字: %5d 条 (%5.1f%%)"
              % (th, c, c * 100.0 / len(rows)))
    print("  汉字数上限 %d" % max(r[1] for r in rows))

    # ---------- 阈值扫描（决定 max-cjk 用多少）----------
    print("\n【1b】阈值扫描: 放宽/收紧 max-cjk 会收录多少字")
    print("    %-8s%10s%10s%12s" % ("max-cjk", "入选条目", "字符种数", "相对全集"))
    full_n = None
    for th in (3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20):
        sel_th = [r for r in rows
                  if min_cjk <= r[1] <= th
                  and r[2] <= max_total
                  and r[3] <= max_lines]
        cs = set()
        for _m, _n, _v, _l, clean in sel_th:
            cs |= need_chars(clean, include_non_cjk)
        if full_n is None:
            allc = set()
            for _m, _n, _v, _l, clean in rows:
                allc |= need_chars(clean, include_non_cjk)
            full_n = len(allc)
        print("    <=%-6d%10d%10d%11.1f%%"
              % (th, len(sel_th), len(cs), len(cs) * 100.0 / full_n))

    # ---------- 入选 ----------
    sel = [r for r in rows
           if min_cjk <= r[1] <= max_cjk
           and r[2] <= max_total
           and r[3] <= max_lines]
    print("\n【2】入选条件: 汉字 %d~%d 个，可见字符 <=%d，行数 <=%d"
          % (min_cjk, max_cjk, max_total, max_lines))
    print("  入选 %d 条（占含汉字条目 %.1f%%）"
          % (len(sel), len(sel) * 100.0 / len(rows)))

    short_chars = set()
    for _mid, _n, _v, _l, clean in sel:
        short_chars |= need_chars(clean, include_non_cjk)
    for ch in extra:
        o = ord(ch)
        if not ch.isspace() and not (PUA_LO <= o <= PUA_HI):
            short_chars.add(o)

    full_chars = set()
    for _mid, _n, _v, _l, clean in rows:
        full_chars |= need_chars(clean, include_non_cjk)

    print("  需要渲染的字符 %d 种（全集 %d 种，省下 %d 种）"
          % (len(short_chars), len(full_chars),
             len(full_chars - short_chars)))

    # ---------- 样本 ----------
    if show:
        print("\n【3】入选样本（前 %d 条）" % show)
        for mid, n_cjk, vis, ln, clean in sel[:show]:
            one = clean.replace("\n", "⏎")
            if len(one) > 60:
                one = one[:60] + "…"
            print("    id %-8d %2d字/%2d可见/%d行  %s"
                  % (mid, n_cjk, vis, ln, one))

    # ---------- 字体覆盖 ----------
    fdir = Path(fonts_dir)
    fonts = {}
    for t in targets:
        p = fdir / t
        if not p.exists():
            print("\n[跳过] 字体不存在: %s" % p)
            continue
        try:
            fonts[t] = JFNT(p.read_bytes())
        except Exception as e:
            print("\n[跳过] %s 解析失败: %s" % (t, e))

    print("\n【4】各字体需求与体积")
    print("%-32s%8s%8s%8s%9s%10s%12s"
          % ("字体", "需用字", "已有", "需新增", "B/字形", "增量MB", "字形行数"))
    print("-" * 96)
    total_mb = 0.0
    plans = {}
    for t, jf in fonts.items():
        cs = {c for c in short_chars}
        existing = {c for c in cs if c in jf.cmap}
        add = sorted(cs - existing)
        px = jf.cell_w * jf.cell_h
        mb = len(add) * px / 1048576.0
        total_mb += mb
        old_rows = (jf.count + jf.cols - 1) // jf.cols
        new_rows = (jf.count + len(add) + jf.cols - 1) // jf.cols
        print("%-32s%8d%8d%8d%9d%10.2f%12s"
              % (t, len(cs), len(existing), len(add), px, mb,
                 "%d->%d" % (old_rows, new_rows)))
        plans[t] = cs
    print("-" * 96)
    print("%-32s%8s%8s%8s%9s%10.2f" % ("合计", "", "", "", "", total_mb))

    # ---------- 风险提示 ----------
    lost = full_chars - short_chars
    if lost:
        print("\n【5】注意: 这些字只出现在长文本里，本次不给大字体收录 %d 种"
              % len(lost))
        s = "".join(chr(c) for c in sorted(lost))
        for k in range(0, min(len(s), 240), 60):
            print("    %s" % s[k:k + 60])
        print("    如果某个大字号界面显示了长文本，会出现缺字。")
        print("    用 --max-cjk / --max-total / --max-lines 放宽可减小风险。")

    # ---------- 写出 ----------
    if not write:
        print("\n[dry] 未写文件。确认无误后加 --write")
        return 0

    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    rep = []
    for t, cs in plans.items():
        p = od / (t + ".txt")
        if p.exists() and backup:
            b = od / (t + ".full.txt")
            if not b.exists():
                b.write_bytes(p.read_bytes())
                print("  备份 %s -> %s" % (p.name, b.name))
        p.write_text("".join(chr(c) for c in sorted(cs)), encoding="utf-8")
        print("  写入 %-40s %d 字" % (p, len(cs)))
        rep.append("%s\t%d\t%.2f" % (t, len(cs), 0.0))

    (od / "_short_report.txt").write_text(
        "阈值: 汉字 %d~%d, 可见<=%d, 行数<=%d\n"
        "入选 %d 条, 字符 %d 种\n\n"
        % (min_cjk, max_cjk, max_total, max_lines,
           len(sel), len(short_chars))
        + "\n".join(rep), encoding="utf-8")

    print("\n下一步:")
    print("  py jfntbuild.py --fonts %s --chars %s ^" % (fonts_dir, out_dir))
    print("     --ttf <字体文件> --mode both --cjk-only --verify ^")
    print("     -o build\\big --jarc update\\content\\fonts\\font_data.jarc ^")
    print("     --font %s" % ",".join(plans))
    return 0


if __name__ == "__main__":
    sys.exit(main())
