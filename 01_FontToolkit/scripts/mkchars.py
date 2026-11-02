#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r'''mkchars.py - 从译文本里抽出"全量字符集"，给正文类字体用

与 shortset.py 的分工：
  shortset.py   只收"短文本"（标题/大按钮）用到的汉字 -> 给 DB_L_3 / EB_L_3 这类大字号字体
  mkchars.py    收全部译文里出现的字符              -> 给 DB_3 / DB_3_S / debug 这类正文字体

输入是 wbmgt 导出格式（%%% 头 + --- 分隔的 id/attribute/正文 块），
与 shortset.py 共用同一个解析器，所以两者对同一份 txt 的理解完全一致。

用法:
  py mkchars.py                          # 自动查找 message\ 下大于 900KB 的合格 .txt
  py mkchars.py --txt msg.txt --out charset_full --fonts style_font_DB_3.jfnt,style_font_DB_3_S.jfnt
  py mkchars.py --txt msg.txt --out charset_full --cjk-only      # 只写汉字，不含标点

输出文件名规则与 jfntbuild.py 的 --chars 目录约定一致：
  <字体名>.jfnt.txt ，例如 style_font_DB_3_S.jfnt.txt
（jfntbuild 用 Path.stem 做匹配，所以文件名必须带 .jfnt）
'''
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from shortset import parse_txt, is_cjk, find_message_txt, PUA_LO, PUA_HI

_auto = find_message_txt()
DEF_TXT = str(_auto) if _auto else ""
DEF_FONTS = ("style_font_DB_3.jfnt,style_font_DB_3_S.jfnt,"
             "style_font_DB_3_S_fmb.jfnt,style_font_DB_L_3_fmb.jfnt,"
             "style_font_debug.jfnt")


def chars_of(text, cjk_only=False):
    """一条文本要用字体重画的字符集合"""
    out = set()
    for ch in text:
        o = ord(ch)
        if ch.isspace() or o < 0xA0:
            continue
        if PUA_LO <= o <= PUA_HI:      # 私用区图标沿用原版字形
            continue
        if is_cjk(o):
            out.add(o)
        elif not cjk_only:
            out.add(o)
    return out


def main():
    argv = sys.argv[1:]
    txt = DEF_TXT
    out_dir = "charset_full"
    fonts_s = DEF_FONTS
    cjk_only = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--txt":
            txt = argv[i + 1]; i += 2
        elif a == "--out":
            out_dir = argv[i + 1]; i += 2
        elif a == "--fonts":
            fonts_s = argv[i + 1]; i += 2
        elif a == "--cjk-only":
            cjk_only = True; i += 1
        else:
            i += 1

    p = Path(txt)
    if not txt or not p.exists():
        print("[错误] 未在 message\\ 下找到大于 900KB 的合格 .txt，请用 --txt 指定: %s" % txt)
        return 1
    msgs = parse_txt(str(p))
    if not msgs:
        print("[错误] 解析到 0 条消息，检查是否为 wbmgt 导出格式（--- 分隔 + id: 行）")
        return 1

    allc = set()
    for _mid, _attr, body in msgs:
        allc |= chars_of(body, cjk_only)

    cjk = {c for c in allc if is_cjk(c)}
    other = allc - cjk
    print("译文 %s" % p)
    print("  消息 %d 条   字符 %d 个（汉字 %d，其他 %d）"
          % (len(msgs), len(allc), len(cjk), len(other)))
    if other:
        s = "".join(chr(c) for c in sorted(other))
        print("  其他字符: %s" % (s[:120] + ("..." if len(s) > 120 else "")))

    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    buf = "".join(chr(c) for c in sorted(allc))
    fonts = [t.strip() for t in fonts_s.split(",") if t.strip()]
    for f in fonts:
        name = f if f.lower().endswith(".jfnt") else f + ".jfnt"
        tgt = od / (name + ".txt")
        tgt.write_text(buf, encoding="utf-8")
        print("  写出 %-40s %d 字" % (tgt, len(allc)))
    print("\n提示: 大字号字体(DB_L_3/EB_L_3)不要用这份全量集，"
          "用 shortset.py 生成的短文本集，否则体积会翻三倍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
