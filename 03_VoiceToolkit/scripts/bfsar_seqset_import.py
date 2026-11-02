#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TuneBloom batch Sequence Sound Set importer (BFSAR container)
— bfsar_seqset_export.py 的配套批量导入脚本.

逆向映射 (与导出脚本严格互逆):
  导出: Sound → FSEQ/MML → bank → (WaveArchiveId, WaveIndex) → FWAR 内 BFWAV → RIFF WAV
  导入: _manifest.csv 行 → 导出目录内 WAV → (WaveArchiveId, WaveIndex) → FWAR 内 BFWAV(重编码) → BFSAR 重打包

波形写回路径与 bfsar_import.py (Wave 批量导入, 已在游戏内验证通过) 完全一致:
  read_riff_wav → convertChannel_ → build_fwav_image(WaveFile::doWrite 移植)
  → rebuild_fwar(BfwarFile::doWrite 移植) → rebuild_bfsar(Bfsar::save_ 移植 + 自检)

清单规则 (用户指定, 与导出脚本的 _manifest.csv 对应):
  * 以 <导出目录>/_manifest.csv 为准; 文件夹里存在但清单没有记录的文件 → 忽略;
  * 清单有记录但文件不存在的行 → 忽略;
  * 导入 wav 比"原导出 wav"体积大 → 警告 + 超出百分比, 仍然执行导入
    (原导出 wav 体积 = 44 字节 RIFF 头 + 采样数 × 每样本字节 × 通道数, 由清单列推算);
  * 第 22 列「导出MD5」= 导出时写出的 wav 内容摘要。若磁盘上的文件与之一致
    (说明导出后没动过) → 该目标整组跳过、不写回。这样"整库导入"不会把没本地化
    的波形重新编码, 也不会把原本带 loop 的波形(环境音/BGM 类)改写成不循环。
    旧清单(无该列)或 --force-all 时退化为旧行为: 清单内有文件就写回.

共享波形语义 (多个声音/多个变体命中同一 (WaveArchiveId, WaveIndex) 时):
  * 同目标的多份文件内容相同 → 只导入 1 份, 其余视为重复跳过;
  * 内容不同 → 只在"导出后被动过"的文件里挑, 再以修改时间最新者为准,
    其余忽略并逐条警告 (避免未修改的副本覆盖用户已修改的文件).

随机变体机制 (同一声音多次播放命中不同波形):
  播放器里能改变"最终命中哪个 BFWAV"的机制共 4 类 (见 MmlParser.cpp):
    ① RANDOM + PRG 随机乐器号 ② RANDOM + BANK_SELECT 随机 bank
    ③ RANDOM/VARIABLE + TRANSPOSE 随机移调 (含 EX SETVAR/RANDVAR)
    ④ IF 条件分支 (cmpFlag 由游戏状态决定)
  bfsar_seqset_export.py 已按这 4 类导出全部可达波形 (基础 + _v1/_v2…)。
  * 导入前自动做"变体覆盖检查": 扫描清单涉及的 seq 声音的全部可达波形,
    若清单未覆盖其中某些波形 → 逐条警告 (这些波形未导入, 游戏内反复播放
    仍会随机命中未本地化音频, 即"中英混杂"的直接成因);
  * 本脚本对变体行 (不同 WaveIndex) 天然逐波形处理, 无需额外修改.

用法: python bfsar_seqset_import.py <file.bfsar> [导出目录] [输出.bfsar]
          [-e pcm8|pcm16|dspadpcm|keep] [-o 输出.bfsar] [-y] [--force-all]
  导出目录默认 <档案名>_seqsets/; 输出默认 <档案名>.seqimported.bfsar。
  --force-all: 忽略「导出MD5」, 清单内有文件的波形全部写回 (旧行为)。
仅依赖 Python 标准库 (复用同目录 bfsar_import.py 的解析/编码/重建实现)。
"""

import csv
import hashlib
import os
import sys

import bfsar_import as W    # 复用已游戏内验证的 BFSAR 解析 / DSP 编码 / FWAV / FWAR / BFSAR 重建

MANIFEST_HEADER = ["Set", "SetIndex", "SoundIndex", "SoundName", "类型", "SeqRaw",
                   "BankId", "BankName", "WaveArchiveId", "WaveIndex", "WARC名",
                   "编码", "采样率", "通道数", "采样数", "循环起", "循环止",
                   "输出/状态", "SeqStartOffset", "SeqLabel", "变体来源", "导出MD5"]
MANIFEST_COL_EXPORT_MD5 = 21    # 「导出MD5」列: 导出时写入的 wav 内容摘要, 用于判断文件是否被改动


def human(n):
    return "{:,}".format(n)


def expected_wav_size(row):
    """原导出 wav 体积: 44 字节 RIFF 头 + 采样数 × 每样本字节 × 通道数.
    返回 None 表示清单列缺失/非法 (不做体积警告)."""
    try:
        nch = int(row[13])
        samples = int(row[14])
    except (IndexError, ValueError):
        return None
    enc = (row[11] or "").upper()
    sw = 1 if enc == "PCM8" else 2
    return 44 + samples * sw * nch


def load_manifest(manifest_path):
    """读取 _manifest.csv, 返回 (rows, header). rows: 原始 19 列列表 (跳过表头)."""
    rows = []
    header = None
    with open(manifest_path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        for r in rd:
            if header is None:
                header = r
                continue
            if len(r) >= 18:
                rows.append(r)
    return rows, header


def resolve_target(b, warc_id_str, wave_idx_str):
    """(WaveArchiveId, WaveIndex) → (fileId, waveIdx) — 与导出脚本 warc_id&0xFFFFFF 一致."""
    try:
        wa = int(warc_id_str, 16)
        wi = int(wave_idx_str)
    except ValueError:
        return None
    aa = wa & 0xFFFFFF
    if aa >= len(b["archives"]):
        return None
    fileId = b["archives"][aa]["fileId"]
    if fileId >= len(b["files"]):
        return None
    fe = b["files"][fileId]
    if not (fe["internal"] and fe["abs"] is not None and
            fe["size"] not in (None, 0xFFFFFFFF)):
        return None
    return fileId, wi


def check_variant_coverage(bfsar_path, rows, set_filter=None, log=print):
    """随机变体覆盖检查（通盘处理"同一声音反复播放出现不同内容"机制）。

    对清单涉及/可能涉及的每个 Sequence Sound，用 bfsar_seqset_export 的变体
    扫描器（随机乐器号 RANDOM+PRG、随机 bank、随机移调（含 RANDOM+SETVAR/RANDVAR
    写入的变量）、IF 条件分支）求其全部可达波形；若清单未覆盖其中某些波形 → 警告（游戏内反复播放仍会
    命中未本地化波形，即"中英混杂"的直接成因）。

    返回 (带变体声音数, 存在覆盖缺口声音数)。检查失败时静默返回 (0,0)，
    不影响导入本身。"""
    try:
        import bfsar_seqset_export as E
    except Exception:
        return 0, 0
    try:
        arc = E.Archive(E.Path(bfsar_path))
    except Exception:
        return 0, 0
    rw = arc.rw
    covered = set()
    for row in rows:
        try:
            covered.add((int(row[8], 16) & 0xFFFFFF, int(row[9])))
        except (IndexError, ValueError):
            pass
    n_var = n_gap = 0
    for si, (_tid, gof) in enumerate(arc.rt_grp):
        if gof is None:
            continue
        rs, re_ = rw.u32(gof), rw.u32(gof + 4)
        stid, detail = E.deref(rw, gof + 0x10, gof, "set")
        if stid == E.EI_WSNDGROUP and detail is not None:      # 仅检查 Seq 集
            continue
        set_name = arc.name_at(gof + 0x18, f"SEQSET_{si}")
        if set_filter and set_filter.lower() not in set_name.lower():
            continue
        if rs == E.INVALID and re_ == E.INVALID:
            continue
        sstart = min(rs & 0xFFFFFF, len(arc.rt_sound) - 1)
        send = min(max(re_ & 0xFFFFFF, sstart), len(arc.rt_sound) - 1)
        for snd in range(sstart, send + 1):
            if snd >= len(arc.rt_sound):
                break
            _t2, soff = arc.rt_sound[snd]
            if soff is None:
                continue
            try:
                file_id = rw.u32(soff)
                stype = rw.u16(soff + 0x0C)
                sname = arc.name_at(soff + 0x14, f"SOUND_{snd}")
                _d2, det = E.deref(rw, soff + 0x0C, soff, "d")
                if stype != E.EI_SEQ or det is None:
                    continue
                seqf = arc.fdata(file_id)
                if not seqf:
                    continue
                btab = det + rw.s32(det + 4)
                start_off = E.flagval(rw, det + 0xC, 0) or 0
                banks_ids = E.u32tab(rw, btab, cap=4) if btab > 0 else []
                bank_datas = []
                for bk in banks_ids:
                    if bk == E.INVALID:
                        bank_datas.append(None)
                        continue
                    bidx = bk & 0xFFFFFF
                    info = None
                    if 0 <= bidx < len(arc.rt_bank) and arc.rt_bank[bidx][1] is not None:
                        boff = arc.rt_bank[bidx][1]
                        bnk_fid = rw.u32(boff)
                        bf = arc.fdata(bnk_fid)
                        info = (bf[0], arc.name_at(boff + 0xC, f"BNK_{bidx}"), bnk_fid) if bf else None
                    bank_datas.append(info)
                _first, _used, variants, has_var, _misses, _lab = E.seq_used_waves_variants(
                    seqf[0], bank_datas, start_off)
                if not has_var:
                    continue
                n_var += 1
                reach = set((warc & 0xFFFFFF, widx) for (_v, _bn, warc, widx) in variants)
                missing = sorted((reach - covered), key=lambda t: (t[0], t[1]))
                if missing:
                    n_gap += 1
                    waves = ", ".join("w%d" % w for w in sorted({w for _a, w in missing}))
                    log("警告: %s s%d %s 含随机变体, 但清单未覆盖可达波形 {%s} — "
                        "这些波形未被导入, 游戏内反复播放仍会随机命中(未本地化音频)" %
                        (set_name, snd, sname, waves))
            except Exception:
                continue
    if n_var:
        log("变体覆盖检查: %d 个随机变体声音, %d 个存在覆盖缺口%s"
            % (n_var, n_gap, "（重新用变体感知版导出脚本导出即可补齐 _v1/_v2…）" if n_gap else ""))
    return n_var, n_gap


def main():
    args = sys.argv[1:]
    pos = []
    enc_cli = None
    outfile = None
    assume_yes = False
    force_all = False
    set_filter = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return
        if a in ("-e", "--encoding"):
            i += 1
            if i >= len(args):
                print("-e 缺少参数")
                return
            enc_cli = args[i].lower()
            if enc_cli not in W.ENC_CLI:
                print("--encoding 仅支持 pcm8|pcm16|dspadpcm|keep")
                return
        elif a in ("-o", "--output"):
            i += 1
            if i >= len(args):
                print("-o 缺少参数")
                return
            outfile = args[i]
        elif a == "--set-filter":
            i += 1
            if i >= len(args):
                print("--set-filter 缺少参数")
                return
            set_filter = args[i]
        elif a in ("-y", "--yes"):
            assume_yes = True
        elif a == "--force-all":
            force_all = True          # 忽略「导出MD5」, 清单内所有文件都写回
        elif a.startswith("-"):
            print("未知参数: %s" % a)
            return
        else:
            pos.append(a)
        i += 1

    if not pos:
        print("usage: python bfsar_seqset_import.py <file.bfsar> [导出目录] [输出.bfsar]"
              " [-e pcm8|pcm16|dspadpcm|keep] [-o 输出.bfsar] [--set-filter 子串] [-y] [--force-all]")
        return
    bfsar_path = pos[0]
    base, ext = os.path.splitext(bfsar_path)
    exportdir = pos[1] if len(pos) > 1 else base + "_seqsets"
    if outfile is None and len(pos) > 2:
        outfile = pos[2]
    if outfile is None:
        outfile = base + ".seqimported" + (ext if ext else ".bfsar")

    # ---- 读取 BFSAR ----
    if not os.path.isfile(bfsar_path):
        print("找不到 BFSAR: %s" % bfsar_path)
        return
    with open(bfsar_path, "rb") as f:
        data = f.read()
    b = W.load_bfsar(data)
    r = b["r"]
    fsarVersion = r.u32(8)
    fwavVersion = (0x00010200 if fsarVersion >= 0x00020200 else
                   (0x00010100 if fsarVersion >= 0x00020000 else 0x00010000))
    print("BFSAR: %s  (version 0x%08X, %s 字节序, BFSAR→FWAV 版本 0x%08X)"
          % (bfsar_path, fsarVersion, "大端" if r.e == W.EB else "小端", fwavVersion))

    # ---- 导出目录 / 清单 ----
    if not os.path.isdir(exportdir):
        print("错误: 导出目录不存在: %s" % exportdir)
        return
    manifest_path = os.path.join(exportdir, "_manifest.csv")
    if not os.path.isfile(manifest_path):
        print("错误: %s 下没有 _manifest.csv (必须由 bfsar_seqset_export.py 生成)" % exportdir)
        return
    rows, _hdr = load_manifest(manifest_path)
    print("清单: %s  (%d 行)%s" % (manifest_path, len(rows),
                                   (" | 仅导入 Set 名含 '%s'" % set_filter) if set_filter else ""))

    # ---- 随机变体覆盖检查 (同一声音反复播放命中不同波形 → 防"中英混杂"缺口) ----
    check_variant_coverage(bfsar_path, rows, set_filter)

    # ---- 编码 ----
    enc_choice = W.ask_encoding(enc_cli)

    # ---- 确认输出文件 ----
    if os.path.exists(outfile) and not assume_yes:
        tip = " (将覆盖导入源 BFSAR 本体!)" if os.path.abspath(outfile) == os.path.abspath(bfsar_path) else ""
        yn = input('输出 "%s" 已存在%s, 覆盖? [y/N]: ' % (outfile, tip))
        if yn.strip().lower() != "y":
            print("已取消")
            return

    # ---- 按目标波形分组 (共享目标去重) ----
    manifest_paths = set()       # 清单内 wav 相对路径 (正斜杠规范化)
    groups = {}                  # (fileId, waveIdx) -> [row, ...]
    for row in rows:
        status = row[17]
        if status.endswith(".wav"):
            manifest_paths.add(status.replace("\\", "/"))
            if set_filter and set_filter.lower() not in row[0].lower():
                continue
            tgt = resolve_target(b, row[8], row[9])
            if tgt is None:
                print("忽略 %s: 清单目标 (warc=%s, wave=%s) 无法定位" % (status, row[8], row[9]))
                continue
            groups.setdefault(tgt, []).append(row)
    n_no_wav = len(rows) - sum(1 for row in rows if row[17].endswith(".wav"))

    # 清单外文件 → 忽略
    ignored_disk = 0
    for dirpath, _dirs, files in os.walk(exportdir):
        for fn in files:
            if not fn.lower().endswith(".wav"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), exportdir).replace("\\", "/")
            if rel not in manifest_paths:
                print("忽略: %s 不在 _manifest.csv 清单中, 不处理" % rel)
                ignored_disk += 1

    # ---- 逐目标导入 ----
    replacements = {}            # (fileId, waveIdx) -> meta
    total_imported = 0
    total_missing = 0
    total_dup_same = 0
    total_conflict = 0
    total_untouched = 0          # 导出后未改动(与清单 导出MD5 一致) → 不写回

    group_order = sorted(groups.keys())
    for gn, (fileId, waveIdx) in enumerate(group_order, 1):
        rows_g = groups[(fileId, waveIdx)]
        fe = b["files"][fileId]
        base_abs = fe["abs"]
        fwar_waves = W.parse_fwar(r, base_abs)
        if waveIdx >= len(fwar_waves):
            for row in rows_g:
                print("跳过 %s: wave 索引越界 (waveIdx=%d, 该 WARC 共 %d)" % (row[17], waveIdx, len(fwar_waves)))
                total_missing += 1
            continue

        # 收集存在文件, 同时用清单的"导出MD5"(第 22 列, 下标 21) 判断该文件
        # 导出后有没有被改动过 —— 未改动的文件不写回, 避免无谓重编码,
        # 更重要的是避免把原本带 loop 的波形改写成不循环(见 --force-all)。
        exist = []
        for row in rows_g:
            path = os.path.join(exportdir, row[17])
            if not os.path.isfile(path):
                print("忽略 %s: 清单有记录但文件不存在" % row[17])
                total_missing += 1
                continue
            with open(path, "rb") as f:
                content = f.read()
            exp_md5 = (row[21].strip().lower() if len(row) > 21 else "")
            untouched = (not force_all) and bool(exp_md5) and \
                        hashlib.md5(content).hexdigest() == exp_md5
            exist.append((row, path, os.path.getmtime(path),
                          hashlib.sha1(content).hexdigest(), content, untouched))

        if not exist:
            continue

        # 整个目标下所有文件都"未改动" → 整组跳过(不写回)
        if all(e[5] for e in exist):
            total_untouched += len(exist)
            continue

        # 同目标多文件: 只在"已改动"的文件里挑;
        # 内容相同 → 1 份; 仍不同 → 最新修改为准
        cand = [e for e in exist if not e[5]] or exist
        total_untouched += len(exist) - len(cand)
        chosen = cand[0]
        if len(cand) > 1:
            chash = chosen[3]
            if all(e[3] == chash for e in cand):
                total_dup_same += len(cand) - 1
                print("共享目标 warc#%d wave#%d: %d 个文件内容相同, 导入 1 份 (其余 %d 份跳过)"
                      % (fileId, waveIdx, len(cand), len(cand) - 1))
            else:
                chosen = max(cand, key=lambda e: e[2])      # mtime 最新
                for e in cand:
                    if e[3] != chosen[3]:
                        print("警告: 目标 warc#%d wave#%d 的多个文件内容不同 — 采用最新修改的 %s, 忽略 %s"
                              % (fileId, waveIdx, chosen[0][17], e[0][17]))
                        total_conflict += 1
                    else:
                        total_dup_same += 1

        row, path, _mtime, _h, content, _u = chosen
        fn = row[17]
        tag = "[目标 %d/%d]" % (gn, len(group_order))

        # 体积警告 (用户规则: 比原导出大 → 警告 + 百分比, 仍导入)
        old_size = expected_wav_size(row)
        cur_size = len(content)
        if old_size is not None and cur_size > old_size:
            pct = (cur_size - old_size) / old_size * 100.0
            print("%s 警告: %s 比原导出体积大 %.2f%% (%s → %s bytes), 按要求仍继续导入"
                  % (tag, fn, pct, human(old_size), human(cur_size)))

        # WAV 解析 (WaveFile::readRiffWavInfo)
        channels, info = W.read_riff_wav(path)
        if channels is None:
            print("%s 跳过 %s: %s" % (tag, fn, info))
            total_missing += 1
            continue

        # 原 BFWAV (提示 + keep 编码)
        orig_abs, _orig_size = fwar_waves[waveIdx]
        try:
            orig = W.parse_bfwav_lite(r, orig_abs)
        except Exception as ex:
            print("%s 跳过 %s: 原始 BFWAV 解析失败 (%s)" % (tag, fn, ex))
            total_missing += 1
            continue

        # 信息性提示 (不阻断; 与 TuneBloom Replace 语义一致)
        if info["sampleRate"] != orig["sr"]:
            print("%s 注意: %s 采样率 %d → %d (与 TuneBloom 一致, 以 wav 为准)" % (tag, fn, orig["sr"], info["sampleRate"]))
        if info["numChannels"] != len(orig["channels"]):
            print("%s 注意: %s 声道数 %d → %d" % (tag, fn, len(orig["channels"]), info["numChannels"]))
        to_enc = enc_choice if enc_choice is not None else orig["enc"]
        if to_enc != orig["enc"]:
            print("%s 注意: %s 编码 %s → %s (改变编码可能损失音质)"
                  % (tag, fn, W.ENC_NAMES.get(orig["enc"], "?"), W.ENC_NAMES.get(to_enc, "?")))
        if orig["isLoop"]:
            print("%s 注意: %s 原波形有 loop, 按 TuneBloom 导入语义 loop 将被重置为不循环" % (tag, fn))

        # readWavFile + convertChannel_ (isLoop=False)
        wstate = W.WaveFileState(to_enc, False, info["sampleRate"], 0, info["sampleCount"])
        ch_datas = []
        params = [] if to_enc == W.WAVE_DSP else None
        ok = True
        for ch in channels:
            try:
                cb, par = W.convert_channel_import(ch, wstate, r.e)
            except Exception as ex:
                print("%s 跳过 %s: 编码失败 (%s)" % (tag, fn, ex))
                ok = False
                break
            ch_datas.append(cb)
            if to_enc == W.WAVE_DSP:
                params.append(par)
        if not ok:
            total_missing += 1
            continue
        img = W.build_fwav_image(r.e, fwavVersion, wstate, ch_datas, params)
        # 编码后体积 vs 原 BFWAV 体积（BFSAR 整包进内存，累积变大可能致游戏崩溃）
        if _orig_size:
            delta = len(img) - _orig_size
            if delta > 0:
                print("%s 警告: %s 编码后体积 %s → %s (+%.1f%%, 对比原 BFWAV) — "
                      "单条变大将推高 BFSAR 体积"
                      % (tag, fn, W.human(_orig_size), W.human(len(img)),
                         delta * 100.0 / _orig_size))
        key = (fileId, waveIdx)
        if key in replacements:
            print("%s 警告: wave (fileId=%d, waveIdx=%d) 已被 %s 导入, 现被 %s 覆盖"
                  % (tag, fn, fileId, waveIdx, replacements[key]["name"], fn))
        replacements[key] = {"img": img, "name": fn, "enc": to_enc,
                             "sr": info["sampleRate"], "count": info["sampleCount"]}
        total_imported += 1
        print("%s 导入 %s → warc(fileId=%d) wave#%d  [%s / %d Hz / %d 样本 / %s bytes]"
              % (tag, fn, fileId, waveIdx, W.ENC_NAMES.get(to_enc, "?"),
                 info["sampleRate"], info["sampleCount"], human(len(img))))

    print("\n汇总: 导入 %d 个目标波形 | 未改动跳过 %d | 清单无文件忽略 %d | 同内容去重跳过 %d | 内容冲突忽略 %d | 无波形记录 %d"
          % (total_imported, total_untouched, total_missing, total_dup_same,
             total_conflict, n_no_wav))
    if not replacements:
        print("没有任何波形被导入, 未写出文件。")
        return

    # ---- 重建受影响的 FWAR 镜像 ----
    grouped = {}
    for (fid, wi), meta in replacements.items():
        grouped.setdefault(fid, {})[wi] = meta
    replacement_images = {}
    orig_waves = {}
    for fileId in sorted(grouped.keys()):
        by_wave = grouped[fileId]
        img, wave_count, waves0 = W.rebuild_fwar(data, b, fileId, by_wave, print)
        replacement_images[fileId] = img
        orig_waves[fileId] = waves0
        print("重建 FWAR (fileId=%d): %d 条 wave, 其中 %d 条已替换, 镜像 %s → %s bytes"
              % (fileId, wave_count, len(by_wave),
                 human(b["files"][fileId]["size"] or 0), human(len(img))))

    # ---- 重组 BFSAR ----
    try:
        out, st = W.rebuild_bfsar(data, b, replacement_images)
    except (ValueError, AssertionError) as ex:
        print("\n错误: %s" % ex)
        print("安全中止, 未写出文件。")
        return

    # ---- 自检: 重新解析输出, 用补丁后的"新"位置做比对 ----
    try:
        b2 = W.load_bfsar(out)
        r2 = b2["r"]
        for fileId, img in replacement_images.items():
            fe2 = b2["files"][fileId]
            if fe2["abs"] is None or fe2["size"] != len(img):
                raise AssertionError("fileId=%d 重新解析的尺寸/偏移不一致" % fileId)
            waves2 = W.parse_fwar(r2, fe2["abs"])
            if len(waves2) != len(orig_waves[fileId]):
                raise AssertionError("fileId=%d wave 数量不一致" % fileId)
            for wi, (abs2, size2) in enumerate(waves2):
                meta = grouped[fileId].get(wi)
                if meta is not None:
                    nf = W.parse_bfwav_lite(r2, abs2)
                    if (nf["enc"] != meta["enc"] or nf["sr"] != meta["sr"]
                            or nf["storedLoopEnd"] != meta["count"] or nf["isLoop"]):
                        raise AssertionError("wave(fileId=%d,#%d) 元数据不一致" % (fileId, wi))
                else:
                    oabs, osize = orig_waves[fileId][wi]
                    if bytes(out[abs2:abs2 + size2]) != bytes(data[oabs:oabs + osize]):
                        raise AssertionError("wave(fileId=%d,#%d) 未修改却被改动" % (fileId, wi))
        for fi, fe in enumerate(b["files"]):
            if not (fe["internal"] and fe["abs"] is not None and
                    fe["size"] not in (None, 0xFFFFFFFF)):
                continue
            if fi in replacement_images:
                continue
            fe2 = b2["files"][fi]
            if fe2["abs"] is None or fe2["size"] != fe["size"]:
                raise AssertionError("fileId=%d 重新解析的尺寸/偏移不一致" % fi)
            if bytes(out[fe2["abs"]:fe2["abs"] + fe2["size"]]) != \
               bytes(data[fe["abs"]:fe["abs"] + fe["size"]]):
                raise AssertionError("fileId=%d 未修改却被改动" % fi)
        print("自检: 通过 (受影响 FWAR / 替换波形 / 未修改文件均与预期一致)")
    except Exception as ex:
        print("警告: 自检未通过 (%s) — 请勿直接使用输出文件!" % ex)
        return

    with open(outfile, "wb") as f:
        f.write(out)
    md5 = hashlib.md5(out).hexdigest()
    print("\n==================================================")
    print("导入完成: 替换 %d 条 wave, 涉及 %d 个 FWAR"
          % (len(replacements), len(replacement_images)))
    print("  BFSAR: %s → %s (%s → %s bytes)"
          % (bfsar_path, outfile, human(st["oldFileSize"]), human(st["newFileSize"])))
    print("  MD5: %s" % md5)
    print("  (可用 bfsar_seqset_export.py 对输出文件重新导出, 与本目录 wav 做听感/交叉验证)")
    print("==================================================")


if __name__ == "__main__":
    main()
