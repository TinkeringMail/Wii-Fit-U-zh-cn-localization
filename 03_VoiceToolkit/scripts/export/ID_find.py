#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_fill_id_from_txt.py —— 批量：用 id 文本汇总(txt) 为多个 csv 回填 id

txt：段落以单独成行的 “---” 分界（无例外），正文有断行，段末空行有无均可；
正文按空格拼回整句后与 csv 的 B 列(English)比对。

匹配策略（两边做同样规范化后比较）：
  精确 —— 撇号/零宽字符删除、NFKC、lower、标点(含全角)->空格、压空白，再完全去空格。
          断行（含断在连字符处）、空格多少、有无标点、大小写均不影响命中。
  模糊 —— 精确不中时 difflib 最近邻兜底（阈值 --threshold；句长<12字符不做模糊，
          短句阈值自动收紧防误配；--no-fuzzy 全部关闭）。

多 id 共文：同一句在 txt 中对应多个 id 时，全部 id 横向展开写入 D、E、F…（ID/ID_2/…），
Note 列注明数量与匹配方式，全部列出供人工筛选。

批量：缺省扫描当前目录全部 .csv（跳过 *_with_id.csv 结果文件与 ~$ 临时文件），
每个 csv 独立完整比对一遍 txt，各输出 <原名>_with_id.csv（不覆盖原文件）与复核报告，
最后打印汇总。仅使用标准库。

用法：
  python ID_find.py                                  # 当前目录全部 csv，自动定位 txt
  python ID_find.py 1.csv 2.csv -t 文本汇总.txt      # 指定 csv（可多个/目录）与 txt
  python ID_find.py --threshold 0.85 / --no-fuzzy
"""

import argparse
import csv
import difflib
import io
import re
import sys
import unicodedata
from pathlib import Path

# ---------------- 常量 ----------------
SEP_RE = re.compile(r"^-{3,}\s*$")                       # 单独成行的 “---”
ID_RE = re.compile(r"^id\s*:\s*(\d+)\s*$", re.IGNORECASE)
ATTR_RE = re.compile(r"^attribute\s*:\s*(\S+)\s*$", re.IGNORECASE)

APOSTROPHES = "'\u2018\u2019\u02bc\u2032\uff07"          # 各类撇号/单引号
ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"                  # 零宽字符、BOM 残留

TXT_DEFAULT_NAMES = ("文本汇总.txt", "summary.txt", "ids.txt", "text_summary.txt")
RESULT_SUFFIX = "_with_id"
REPORT_SUFFIX = "_match_report"


def read_text_auto(path: Path) -> str:
    """多编码尝试读取（utf-8 带/不带 BOM -> utf-16 -> gb18030 -> latin-1 兜底）。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def normalize(text: str):
    """返回 (norm, loose)：norm=压空白的小写词序列；loose=norm 去掉全部空格（用于精确匹配）。"""
    text = unicodedata.normalize("NFKC", text)
    for ch in ZERO_WIDTH + APOSTROPHES:
        text = text.replace(ch, "")
    text = text.lower()
    text = re.sub(r"[\W_]+", " ", text)                  # 所有标点(含全角)与下划线 -> 空格
    norm = re.sub(r"\s+", " ", text).strip()
    return norm, norm.replace(" ", "")


def parse_txt(text: str):
    """按 “---” 扫描 txt，返回段落列表；分隔行既是上段的结束也是下段的开始。"""
    entries, cur, section = [], None, None               # section: None -> meta -> body -> ...

    def flush():
        nonlocal cur
        if cur and cur["id"] and cur["lines"]:
            cur["body"] = re.sub(r"\s+", " ", " ".join(cur["lines"])).strip()
            cur["norm"], cur["loose"] = normalize(cur["body"])
            entries.append(cur)
        cur = None

    for raw in text.splitlines():
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
                cur = {"id": m.group(1), "attribute": "", "lines": []}
            else:
                m = ATTR_RE.match(line)
                if m and cur:
                    cur["attribute"] = m.group(1)
        elif section == "body" and cur is not None:
            if line:
                cur["lines"].append(line)
    flush()                                              # 文件结尾没有 “---” 时兜底
    return entries


def cutoff_for(n_chars: int, base: float):
    """按句长收紧模糊阈值，防止短句误配。
    返回 None 表示该句过短、不参与模糊匹配（difflib 要求 cutoff∈[0,1]，
    此前用 1.01 当‘关闭’哨兵值会在 get_close_matches 里直接抛 ValueError——已修复）。"""
    if n_chars >= 40:
        return base
    if n_chars >= 25:
        return max(base, 0.92)
    if n_chars >= 12:
        return max(base, 0.95)
    return None                                          # ← 修复点：太短不做模糊匹配


def col_letter(idx0: int) -> str:
    """0 基列号 -> Excel 列字母（3 -> D）。"""
    letters, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


class Index:
    """txt 索引：解析一次，供所有 csv 复用。"""

    def __init__(self, entries, fuzzy: bool, threshold: float):
        self.fuzzy = fuzzy
        self.threshold = threshold
        self.by_loose = {}                               # loose -> [entry,...]（保持 txt 出现顺序）
        for e in entries:
            self.by_loose.setdefault(e["loose"], []).append(e)
        self.uniq_norms, self.norm2loose, seen = [], {}, set()
        for e in entries:
            if e["loose"] in seen:
                continue
            seen.add(e["loose"])
            self.uniq_norms.append(e["norm"])
            self.norm2loose[e["norm"]] = e["loose"]
        self.fuzzy_cache = {}                            # 模糊结果按 norm 缓存（多 csv 间复用）

    @staticmethod
    def _ids(cand):
        out, seen = [], set()
        for e in cand:
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append(e["id"])
        return out

    def _nearest_hint(self, norm: str) -> str:
        """未匹配时给出最接近候选的提示信息（cutoff=0.0 合法，仅用于报告，不会回填）。"""
        if not norm or not self.uniq_norms:
            return ""
        near = difflib.get_close_matches(norm, self.uniq_norms, n=1, cutoff=0.0)
        if not near:
            return ""
        r = difflib.SequenceMatcher(None, norm, near[0]).ratio()
        near_ids = self._ids(self.by_loose[self.norm2loose[near[0]]])
        return f"，最接近 sim={r:.3f} id={','.join(near_ids)}: {near[0][:48]}"

    def match(self, english: str):
        """返回 (ids, note)；ids 为全部命中 id，按 txt 出现顺序完整列出。"""
        norm, loose = normalize(english)
        if loose in self.by_loose:                       # 精确匹配
            ids = self._ids(self.by_loose[loose])
            if len(ids) > 1:
                return ids, f"精确匹配（同文多id，共{len(ids)}个）"
            return ids, "精确匹配"
        if not norm:                                     # B 列全是符号/空白，无有效文字
            return [], "未匹配(无有效文字)"
        if not self.fuzzy:                               # 模糊匹配已禁用
            return [], "未匹配(模糊匹配已禁用)"
        cached = self.fuzzy_cache.get(norm)              # 模糊匹配兜底（按 norm 缓存）
        if cached is None:
            cut = cutoff_for(len(norm), self.threshold)
            if cut is None:                              # ← 修复点：短句直接判未匹配+提示
                cached = ([], "未匹配(短句，不参与模糊)" + self._nearest_hint(norm), None)
            else:
                best = difflib.get_close_matches(norm, self.uniq_norms, n=1, cutoff=cut)
                if best:
                    b = best[0]
                    ratio = difflib.SequenceMatcher(None, norm, b).ratio()
                    ids = self._ids(self.by_loose[self.norm2loose[b]])
                    note = f"模糊匹配 sim={ratio:.3f}"
                    if len(ids) > 1:
                        note += f"（同文多id，共{len(ids)}个）"
                    cached = (ids, note, b)
                else:
                    cached = ([], "未匹配" + self._nearest_hint(norm), None)
            self.fuzzy_cache[norm] = cached
        ids, note, _ = cached
        return list(ids), note


def scan_csv_dir(d: Path):
    out = []
    for p in sorted(d.iterdir()):
        if not (p.is_file() and p.suffix.lower() == ".csv" and not p.name.startswith("~$")):
            continue
        if p.stem.endswith(RESULT_SUFFIX):                # 自动扫描时跳过本脚本的结果文件
            print(f"[跳过] 结果文件：{p.name}")
            continue
        out.append(p)
    return out


def collect_csvs(csv_args):
    if csv_args:
        paths = []
        for a in csv_args:
            p = Path(a)
            if p.is_dir():
                paths.extend(scan_csv_dir(p))
            elif p.exists():
                paths.append(p)                          # 显式指定时不做 _with_id 过滤
            else:
                print(f"[跳过] 不存在：{a}")
        seen, out = set(), []
        for p in paths:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out
    return scan_csv_dir(Path.cwd())                      # 默认：当前目录全部 csv


def find_txt(arg):
    if arg:
        p = Path(arg)
        if not p.exists():
            sys.exit(f"找不到 txt：{p}")
        return p
    for name in TXT_DEFAULT_NAMES:
        p = Path(name)
        if p.exists():
            return p
    cands = [p for p in sorted(Path.cwd().glob("*.txt"))
             if not p.stem.endswith(REPORT_SUFFIX)]      # 排除本脚本生成的报告
    if len(cands) == 1:
        print(f"[提示] 未用 -t 指定，自动选用目录中唯一的 txt：{cands[0].name}")
        return cands[0]
    if cands:
        sys.exit("当前目录下有多个 txt，无法自动确定文本汇总文件，请用 -t 指定：\n  "
                 + "\n  ".join(p.name for p in cands))
    sys.exit("未找到文本汇总 txt，请用 -t 指定文件路径")


def process_csv(csv_path: Path, idx: Index):
    rows = list(csv.reader(io.StringIO(read_text_auto(csv_path), newline="")))
    if len(rows) <= 1:
        print(f"[跳过] 空 csv 或只有表头：{csv_path.name}")
        return None

    base = max(len(r) for r in rows)                     # 原 csv 的列数（ID 从下一列开始，即通常的 D 列）
    data, stats = [], {"精确匹配": 0, "模糊匹配": 0, "未匹配": 0, "空": 0}
    multi_rows, fuzzy_rows, none_rows = [], [], []

    for i, row in enumerate(rows[1:], start=2):          # 行号从数据行起（表头为第1行）
        cells = list(row) + [""] * (base - len(row))
        english = cells[1].strip()
        if english:
            ids, note = idx.match(english)
            if len(ids) > 1:
                multi_rows.append((i, english, ids, note))
            if ids:
                if note.startswith("精确匹配"):
                    stats["精确匹配"] += 1
                else:
                    stats["模糊匹配"] += 1
                    fuzzy_rows.append((i, english, ids, note))
            else:
                stats["未匹配"] += 1
                none_rows.append((i, english, note))
        else:
            stats["空"] += 1
            ids, note = [], ""
        data.append((cells, ids, note))

    max_ids = max(1, max(len(ids) for _, ids, _ in data))
    id_cols = ["ID"] + [f"ID_{k}" for k in range(2, max_ids + 1)]

    # 同文多 id：全部 id 各占一列，横向展开（D、E、F…），最后一列为 Note
    out_rows = [list(rows[0]) + [""] * (base - len(rows[0])) + id_cols + ["Note"]]
    for cells, ids, note in data:
        out_rows.append(list(cells) + ids + [""] * (max_ids - len(ids)) + [note])

    out_path = csv_path.with_name(csv_path.stem + RESULT_SUFFIX + ".csv")    # 不覆盖原文件
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:         # 带 BOM，Excel 打开不乱码
        csv.writer(f).writerows(out_rows)

    report_path = csv_path.with_name(csv_path.stem + REPORT_SUFFIX + ".txt")
    rep = [f"文件：{csv_path.name}",
           f"数据行 {len(data)} | 精确 {stats['精确匹配']} | 模糊 {stats['模糊匹配']} | "
           f"未匹配 {stats['未匹配']} | 空English {stats['空']} | ID起始列 {col_letter(base)}",
           ""]
    rep.append(f"== 同文多 id（{len(multi_rows)} 行，全部候选已按列展开，请人工确认）==")
    rep += [f"行{i}\tid={','.join(ids)}\t{note}\t{en[:80]}" for i, en, ids, note in multi_rows]
    rep += ["", f"== 模糊匹配（{len(fuzzy_rows)} 条，请重点复核）=="]
    rep += [f"行{i}\t{note}\tid={','.join(ids)}\t{en[:80]}" for i, en, ids, note in fuzzy_rows]
    rep += ["", f"== 未匹配（{len(none_rows)} 条）=="]
    rep += [f"行{i}\t{note}\t{en[:80]}" for i, en, note in none_rows]
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(rep) + "\n")

    summary = {"name": csv_path.name, "rows": len(data), "stats": stats,
               "out": out_path.name, "report": report_path.name,
               "multi": len(multi_rows), "fuzzy": len(fuzzy_rows),
               "none": len(none_rows), "id_col": col_letter(base)}
    print(f"  精确 {stats['精确匹配']} | 模糊 {stats['模糊匹配']} | 未匹配 {stats['未匹配']} | 空English {stats['空']}")
    if summary["multi"]:
        print(f"  同文多 id：{summary['multi']} 行（每个候选各占一格，已横向全部列出）")
    if summary["fuzzy"]:
        print(f"  模糊匹配：{summary['fuzzy']} 行（见报告复核）")
    if summary["none"]:
        print(f"  未匹配：{summary['none']} 行（报告中有最接近的候选提示）")
    print(f"  输出：{summary['out']}（ID 从 {summary['id_col']} 列开始） | 报告：{summary['report']}")
    return summary


def main():
    ap = argparse.ArgumentParser(
        description="批量用 txt 中的 id 回填多个 csv（各输出新文件，不覆盖原文件）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("csvs", nargs="*",
                    help="要处理的 csv：可给文件、多个文件或目录；缺省=当前目录全部 .csv")
    ap.add_argument("-t", "--txt", default=None,
                    help="id 文本汇总 txt；缺省按常见文件名自动找，找不到且目录中仅一个 txt 时直接用它")
    ap.add_argument("--threshold", type=float, default=0.90, help="模糊匹配相似度阈值 0~1")
    ap.add_argument("--no-fuzzy", action="store_true", help="关闭模糊匹配，只允许精确匹配")
    args = ap.parse_args()

    if not 0.0 <= args.threshold <= 1.0:                 # ← 修复点：防止阈值越界再触发同类错误
        ap.error("--threshold 必须在 0.0 ~ 1.0 之间（示例：--threshold 0.85）")

    txt_path = find_txt(args.txt)
    entries = parse_txt(read_text_auto(txt_path))
    if not entries:
        sys.exit(f"{txt_path} 中未解析到任何 id 段落，请检查“---”分隔行格式")
    idx = Index(entries, fuzzy=not args.no_fuzzy, threshold=args.threshold)
    print(f"txt：{txt_path.name} | 解析到 id 段落 {len(entries)} 个"
          f"（去重后 {len(idx.by_loose)} 种不同文本）")

    csv_paths = collect_csvs(args.csvs)
    if not csv_paths:
        sys.exit("没有找到任何待处理的 csv（当前目录无 .csv，或全是结果文件）")

    summaries = []
    for p in csv_paths:
        print(f"\n--- 处理 {p.name} ---")
        s = process_csv(p, idx)
        if s:
            summaries.append(s)

    print("\n==== 汇总 ====")
    for s in summaries:
        print(f"{s['name']:<40} 行{s['rows']:<6}精确{s['stats']['精确匹配']:<6}"
              f"模糊{s['stats']['模糊匹配']:<6}未匹配{s['stats']['未匹配']:<6}→ {s['out']}")
    print(f"共处理 {len(summaries)} 个 csv：每个都各自对 {txt_path.name} 完整比对一遍并单独输出。")


if __name__ == "__main__":
    main()