#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_chinese_from_txt.py —— 批量：从中文译文汇总 txt 按 id 回填 *_with_id.csv 的 C 列(Chinese)

输入：上一脚本(ID_find.py / batch_fill_id_from_txt.py)生成的 *_with_id.csv
      列结构 = 原csv各列 + ID(+ID_2/ID_3...同文多id时) + Note
条件：取 id 的列(表头为 “ID” 的第一列，通常即 D 列)非空，且 Note 以 “精确匹配” 开头
      ——注意“精确匹配（同文多id，共N个）”同样以“精确匹配”开头，符合条件；
        此时无论还展开了几列 id，也只取第一个 id（D 列）——多id行会在报告中单独列出供人工复核。
      模糊匹配、未匹配、无 id 的行一律忽略，C 列保持原样。
译文：health_message_v1.3_COMPLETE.txt 结构与英文汇总相同（“---”分界、正文断行；
      段末空行有无均可）。中文断行直接相连；仅当拼接处两侧均为 ASCII 字母/数字时补空格，
      英文断词连字符处不补，避免中文句子里混入多余空格。
输出：<原名去掉_with_id>_with_cn.csv（新文件，不覆盖输入）；另附 <...>_cn_report.txt。
      C 列已有译文时默认不覆盖（--overwrite 可强制覆盖）。
批量：缺省扫描当前目录全部 .csv（跳过本脚本输出 *_with_cn.csv、~$ 临时文件、
      以及没有 ID/Note 列的普通 csv），每个文件独立处理，最后打印汇总。仅用标准库。

用法：
  python fill_chinese_from_txt.py
  python fill_chinese_from_txt.py a_with_id.csv b_with_id.csv -t health_message_v1.3_COMPLETE.txt
  python fill_chinese_from_txt.py ./某个目录 -t health_message_v1.3_COMPLETE.txt --overwrite
  python fill_chinese_from_txt.py --ignore-note    # 只看 ID 列，不再要求 Note 为“精确匹配”
"""

import argparse
import csv
import io
import re
import sys
from pathlib import Path

# ---------------- 常量 ----------------
SEP_RE = re.compile(r"^-{3,}\s*$")                    # 单独成行的 “---”
ID_RE = re.compile(r"^id\s*:\s*(\d+)\s*$", re.IGNORECASE)

RESULT_SUFFIX = "_with_cn"        # 本脚本输出后缀
INPUT_SUFFIX = "_with_id"         # 上一脚本输出 = 本脚本输入
REPORT_SUFFIX = "_cn_report"      # 本脚本报告后缀
EXACT_NOTE_PREFIX = "精确匹配"     # Note 以此开头才回填中文
TXT_PRIORITY_NAMES = ("health_message_v1.3_COMPLETE.txt",
                      "文本汇总.txt", "summary.txt")


def read_text_auto(path: Path) -> str:
    """多编码尝试读取（utf-8 带/不带 BOM -> utf-16 -> gb18030 -> latin-1 兜底）。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _join_space_needed(a: str, b: str) -> bool:
    """行拼接处是否补空格：两侧都是 ASCII 字母/数字才补（英文断行/译文夹英文）；
    行尾为连字符或斜杠（英文断词）不补；纯中文（含全角标点）直接相连。"""
    if a in "-/":
        return False
    return a.isascii() and a.isalnum() and b.isascii() and b.isalnum()


def smart_join(lines) -> str:
    """把断行正文拼回一句；行内/行间空行忽略。"""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and _join_space_needed(out[-1], ln[0]):
            out += " "
        out += ln
    return out


def parse_cn_txt(path: Path):
    """按 “---” 扫描中文汇总，返回 (id->译文 dict, 重复id列表)；id 保持首次出现。"""
    cn_map, dup_ids, cur, section = {}, [], None, None   # section: None -> meta -> body -> ...

    def flush():
        nonlocal cur
        if cur and cur["id"] and cur["lines"]:
            if cur["id"] in cn_map:
                dup_ids.append(cur["id"])                # 同 id 重复出现：保留首次
            else:
                cn_map[cur["id"]] = smart_join(cur["lines"])
        cur = None

    for raw in read_text_auto(path).splitlines():
        line = raw.strip()
        if SEP_RE.match(line):
            if section == "body":
                flush()
                section = "meta"
            elif section == "meta":
                section = "body"
            else:
                section = "meta"
            continue
        if section == "meta":
            m = ID_RE.match(line)
            if m:
                cur = {"id": m.group(1), "lines": []}
        elif section == "body" and cur is not None and line:
            cur["lines"].append(line)
    flush()                                              # 文件结尾没有 “---” 时兜底
    return cn_map, dup_ids


def find_txt(arg):
    """定位中文译文汇总 txt：-t > 精确名 > 含 COMPLETE > 常见名 > 目录唯一 txt。"""
    if arg:
        p = Path(arg)
        if not p.exists():
            sys.exit(f"找不到 txt：{p}")
        return p
    for name in TXT_PRIORITY_NAMES:
        p = Path(name)
        if p.exists():
            return p
    all_txt = [p for p in sorted(Path.cwd().glob("*.txt"))
               if not p.stem.endswith((REPORT_SUFFIX, "_match_report"))]
    complete = [p for p in all_txt if "complete" in p.name.lower()]
    if len(complete) == 1:
        print(f"[提示] 未用 -t 指定，自动选用含 COMPLETE 的 txt：{complete[0].name}")
        return complete[0]
    if len(all_txt) == 1:
        print(f"[提示] 未用 -t 指定，自动选用目录中唯一的 txt：{all_txt[0].name}")
        return all_txt[0]
    if all_txt:
        sys.exit("当前目录下有多个 txt（可能混有英文汇总），无法自动确定中文译文文件，"
                 "请用 -t 指定：\n  " + "\n  ".join(p.name for p in all_txt))
    sys.exit("未找到中文译文汇总 txt，请用 -t 指定文件路径")


def scan_dir(d: Path):
    out = []
    for p in sorted(d.iterdir()):
        if not (p.is_file() and p.suffix.lower() == ".csv" and not p.name.startswith("~$")):
            continue
        if p.stem.endswith(RESULT_SUFFIX):               # 跳过本脚本自己的输出
            print(f"[跳过] 本脚本输出：{p.name}")
            continue
        out.append(p)
    return out


def collect_inputs(files_args):
    if files_args:
        paths, seen, out = [], set(), []
        for a in files_args:
            p = Path(a)
            if p.is_dir():
                paths.extend(scan_dir(p))
            elif p.exists():
                paths.append(p)                          # 显式指定时不做后缀过滤
            else:
                print(f"[跳过] 不存在：{a}")
        for p in paths:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out
    return scan_dir(Path.cwd())                          # 默认：当前目录全部 csv


def process_one(csv_path: Path, cn_map: dict, overwrite: bool, ignore_note: bool):
    rows = list(csv.reader(io.StringIO(read_text_auto(csv_path), newline="")))
    if len(rows) <= 1:
        print(f"[跳过] 空 csv 或只有表头：{csv_path.name}")
        return None
    header = rows[0]

    def hcol(name, default):
        for j, h in enumerate(header):
            if h.strip().lower() == name.lower():
                return j
        return default

    id_col, note_col = hcol("ID", None), hcol("Note", None)
    cn_col = hcol("Chinese", 2 if len(header) > 2 else None)
    if id_col is None or note_col is None or cn_col is None:
        print(f"[跳过] 缺少 ID / Note / Chinese 列（可能不是 *_with_id.csv）：{csv_path.name}")
        return None
    extra_id_col = 0                                     # 检测“同文多id”行
    for j, h in enumerate(header):
        lh = h.strip().lower()
        if lh.startswith("id") and lh != "id":
            extra_id_col = j
            break

    maxw = max(len(r) for r in rows)
    stats = {"填入": 0, "忽略-无id": 0, "忽略-非精确匹配": 0,
             "已有未覆盖": 0, "txt无此id": 0, "多id行": 0}
    filled, multi_rows, ign_note, ign_noid, kept, missing = [], [], [], [], [], []
    out_rows = []

    for i, row in enumerate(rows):
        cells = list(row) + [""] * (maxw - len(row))
        if i == 0:
            out_rows.append(cells)
            continue
        lineno = i + 1
        id_val = cells[id_col].strip()
        note = cells[note_col].strip() if note_col is not None else ""
        english = cells[1].strip() if len(cells) > 1 else ""
        if not id_val:
            stats["忽略-无id"] += 1
            ign_noid.append((lineno, note, english))
        elif not ignore_note and not note.startswith(EXACT_NOTE_PREFIX):
            stats["忽略-非精确匹配"] += 1                  # 模糊匹配/未匹配 → 直接忽略
            ign_note.append((lineno, note, english))
        elif id_val not in cn_map:
            stats["txt无此id"] += 1
            missing.append((lineno, id_val, english))
        elif any(cells[j].strip() for j in range(extra_id_col, extra_id_col + 1)) if False else False:
            pass
        else:
            if extra_id_col and extra_id_col < len(cells) and cells[extra_id_col].strip():
                stats["多id行"] += 1                     # 同文多id：只取第一个 id（id_col）
                multi_rows.append((lineno, id_val, english))
            if cells[cn_col].strip():
                if not overwrite:
                    stats["已有未覆盖"] += 1
                    kept.append((lineno, id_val, english))
                    out_rows.append(cells)
                    continue
            cells[cn_col] = cn_map[id_val]
            stats["填入"] += 1
            filled.append((lineno, id_val, english))
        out_rows.append(cells)

    # 输出新文件（去掉 _with_id 后缀，命名干净），绝不覆盖输入
    stem = csv_path.stem
    if stem.endswith(INPUT_SUFFIX):
        stem = stem[: -len(INPUT_SUFFIX)]
    out_path = csv_path.with_name(stem + RESULT_SUFFIX + ".csv")
    if out_path.resolve() == csv_path.resolve():
        out_path = csv_path.with_name(csv_path.stem + "_2.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:   # 带 BOM，Excel 不乱码
        csv.writer(f).writerows(out_rows)

    # 复核报告
    report_path = csv_path.with_name(stem + REPORT_SUFFIX + ".txt")
    rep = [f"输入：{csv_path.name} -> 输出：{out_path.name}",
           f"数据行 {len(rows) - 1} | 填入中文 {stats['填入']} | 多id行(取首个) {stats['多id行']} | "
           f"忽略-非精确匹配 {stats['忽略-非精确匹配']} | 忽略-无id {stats['忽略-无id']} | "
           f"C列已有未覆盖 {stats['已有未覆盖']} | txt无此id {stats['txt无此id']}",
           ""]
    rep.append(f"== 已填入中文（{len(filled)} 行）==")
    rep += [f"行{n}\tid={aid}\t{en[:80]}" for n, aid, en in filled]
    rep += ["", f"== 多 id 行，仅取第一个 id（{len(multi_rows)} 行，请人工复核）=="]
    rep += [f"行{n}\t取id={aid}\t{en[:80]}" for n, aid, en in multi_rows]
    rep += ["", f"== 忽略：Note 非精确匹配（{len(ign_note)} 行，含模糊匹配/未匹配）=="]
    rep += [f"行{n}\tnote={nt}\t{en[:80]}" for n, nt, en in ign_note]
    rep += ["", f"== 忽略：D列无 id（{len(ign_noid)} 行）=="]
    rep += [f"行{n}\tnote={nt}\t{en[:80]}" for n, nt, en in ign_noid]
    rep += ["", f"== 中文 txt 中没有该 id（{len(missing)} 行）=="]
    rep += [f"行{n}\tid={aid}\t{en[:80]}" for n, aid, en in missing]
    rep += ["", f"== C 列已有译文未覆盖（{len(kept)} 行，--overwrite 可强制覆盖）=="]
    rep += [f"行{n}\tid={aid}\t{en[:80]}" for n, aid, en in kept]
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(rep) + "\n")

    print(f"  填入 {stats['填入']} | 多id行取首个 {stats['多id行']} | "
          f"忽略 {stats['忽略-非精确匹配'] + stats['忽略-无id']} | "
          f"已有未覆盖 {stats['已有未覆盖']} | txt无此id {stats['txt无此id']}")
    print(f"  输出：{out_path.name} | 报告：{report_path.name}")
    return {"name": csv_path.name, "rows": len(rows) - 1, "stats": stats,
            "out": out_path.name}


def main():
    ap = argparse.ArgumentParser(
        description="按 id 从中文译文汇总 txt 回填 *_with_id.csv 的 Chinese 列"
                    "（输出新文件，不覆盖原文件）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("csvs", nargs="*",
                    help="要处理的 csv（_*_with_id.csv）：可给文件、多个文件或目录；"
                         "缺省=当前目录全部 .csv（自动跳过 *_with_cn.csv 等）")
    ap.add_argument("-t", "--txt", default=None, help="中文译文汇总 txt（缺省自动定位，见文档）")
    ap.add_argument("--overwrite", action="store_true",
                    help="C 列已有译文时也覆盖（默认保留已有内容）")
    ap.add_argument("--ignore-note", action="store_true",
                    help="不检查 Note 列，凡 ID 列有 id 就填（默认必须“精确匹配”）")
    args = ap.parse_args()

    txt_path = find_txt(args.txt)
    cn_map, dup_ids = parse_cn_txt(txt_path)
    if not cn_map:
        sys.exit(f"{txt_path.name} 中未解析到任何 id 段落，请检查“---”分隔行格式")
    print(f"中文译文：{txt_path.name} | 解析到 id {len(cn_map)} 个"
          + (f"（重复 id {len(dup_ids)} 个，已保留首次出现）" if dup_ids else ""))

    csv_paths = collect_inputs(args.csvs)
    if not csv_paths:
        sys.exit("没有找到任何待处理的 csv")

    summaries = []
    for p in csv_paths:
        print(f"\n--- 处理 {p.name} ---")
        s = process_one(p, cn_map, overwrite=args.overwrite, ignore_note=args.ignore_note)
        if s:
            summaries.append(s)

    print("\n==== 汇总 ====")
    for s in summaries:
        st = s["stats"]
        print(f"{s['name']:<44} 行{s['rows']:<6}填入{st['填入']:<6}"
              f"忽略{st['忽略-非精确匹配'] + st['忽略-无id']:<6}→ {s['out']}")
    print(f"共处理 {len(summaries)} 个 csv：均只对 Note=精确匹配 的行、取第一个 id 填入中文译文。")


if __name__ == "__main__":
    main()