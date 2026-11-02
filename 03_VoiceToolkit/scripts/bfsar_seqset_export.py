#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bfsar_seqset_export.py —— BFSAR "Sequence Sound Sets" 音频批量导出脚本
=====================================================================

与 bfsar_export.py（Wave Sound Sets 批量导出）配套：Wave 组导出的是
每个 Wave 声音对应的单个 BFWAV；Sequence 组里每个声音是一段 MML 序列
（.bfseq），本身不是波形，它通过 Bank（.bfbnk）里的乐器映射到 Wave Archive
（.bfwar）中的 BFWAV。本脚本对每个 Sequence 声音：

  序列声音 (SoundInfo → SequenceSoundInfo)
    → 起始偏移（SequenceSoundInfo.optionParameter bit0 = START_OFFSET）
    → 从偏移扫描 BFSEQ 的 MML 数据（语义逐条对齐 src/snd-ply/MmlParser.cpp）
    → 收集可能触发的音符 {(bank号, prg, key, vel)}
    → BFBNK 乐器解析（Direct/Range/Index 键区 + 力度区）
       → VelocityRegion.waveIdTableIndex → WaveIdTable[(warcId, waveIdx)]
    → FWAR 第 waveIdx 个 BFWAV → 解码为 RIFF WAV（PCM8/PCM16/DSP-ADPCM，
      与 src/dsp/decode.cpp 逐位一致，样本数按 TuneBloom WaveFile.cpp 的
      "调整后 loopEnd" 语义）

随机/变体声音（同一声音反复播放会命中不同 BFWAV）：
  播放器里能改变"最终命中哪个波形"的机制有四类，本脚本全部枚举——
    ① RANDOM + PRG        随机乐器号（rand[lo,hi]），如脚步声 rand[12,15]
    ② RANDOM + BANK_SELECT 随机 bank
    ③ RANDOM/VARIABLE + TRANSPOSE（含 EX SETVAR/RANDVAR 写入的变量）
    ④ IF(0xA2) 条件分支（cmpFlag 由游戏状态决定，如地形决定草地/雪地脚步）
  这类声音在默认模式下会导出全部可达波形：
      s<序号>_<名>.wav      代表波形（IF 假路径首个音符，与旧版一致）
      s<序号>_<名>_v1.wav   其余可达波形（_v2/_v3…）
  非随机声音仍只导出 1 个代表波形（保持与旧版完全相同的文件名）。

输出结构（与 bfsar_export.py 一致，便于复用/对照）：
  <out>/<Sequence Sound Set 名>/
      s<声音序号:04d>_<声音名>.wav        # 该声音的代表波形
      s<声音序号:04d>_<声音名>_v1.wav     # 随机变体的其余波形（_v2/_v3…）
      <Set 名>.txt                        # 清单：file / size_bytes / src_data_bytes / wave_id
  <out>/_manifest.csv                     # 全部导出记录
  <out>/_report.txt                       # 汇总

参考依据：
  * TuneBloom 源码  include/snd/snd_SoundArchiveFile.h（BFSAR Info 布局）
                    include/snd/snd_SequenceSoundFile.h（FSEQ DATA/LABL）
                    include/snd/snd_BankFile.h（BFBNK 乐器/区域）
                    include/snd/snd_WaveFile.h（BFWAV INFO/DATA）
                    include/snd/snd_WaveArchiveFile.h（FWAR）
                    include/snd/snd_GroupFile.h（FGRP 分组文件）
                    src/snd-ply/MmlParser.cpp（MML 解析）
                    src/dsp/decode.cpp（DSP-ADPCM 解码）
                    src/bfsar/WaveFile.cpp（WAV 写出、样本数语义）
  * Citric-Composer 格式说明（ntd_formats/*.htm）

仅使用 Python 标准库。用法：
  py bfsar_seqset_export.py rp_Step_sound.bfsar --list
  py bfsar_seqset_export.py rp_Step_sound.bfsar
  py bfsar_seqset_export.py rp_Step_sound.bfsar --set-filter MIZ
  py bfsar_seqset_export.py rp_Step_sound.bfsar -o OUT --all-bank-waves --dump-raw
"""

import argparse
import csv
import hashlib
import os
import re
import sys
from collections import deque
from pathlib import Path

# Windows 控制台默认 cp936，打印 ✅/▸ 之类会 UnicodeEncodeError —— 统一切 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 常量（snd_ElementType.h / snd_ItemId.h）
# ---------------------------------------------------------------------------
INVALID = 0xFFFFFFFF

ET_SAR_STRG = 0x2000          # ElementType_SoundArchiveFile_StringBlock
ET_SAR_INFO = 0x2001          # ElementType_SoundArchiveFile_InfoBlock
ET_SAR_FILE = 0x2002          # ElementType_SoundArchiveFile_FileBlock

EI_SOUND = 0x2200             # SoundInfo
EI_STREAM = 0x2201            # StreamSoundInfo
EI_WAVE = 0x2202              # WaveSoundInfo
EI_SEQ = 0x2203               # SequenceSoundInfo
EI_WSNDGROUP = 0x2205         # WaveSoundGroupInfo（Wave Sound Set 的 detail）
EI_BANK = 0x2206              # BankInfo
EI_WARC = 0x2207              # WaveArchiveInfo
EI_GROUP = 0x2208             # GroupInfo
EI_INTFILE = 0x220C           # InternalFileInfo
EI_EXTFILE = 0x220D           # ExternalFileInfo

ET_SEQ_DATA = 0x5000          # SequenceSoundFile_DataBlock
ET_SEQ_LABEL = 0x5001         # SequenceSoundFile_LabelBlock
ET_SEQ_LABELINFO = 0x5100     # SequenceSoundFile_LabelInfo

ET_BNK_INFO = 0x5800          # BankFile_InfoBlock
ET_BNK_INSTRUMENT = 0x5900
ET_BNK_KEYREGION = 0x5901
ET_BNK_VELOCITY_REGION = 0x5902
ET_BNK_NULL = 0x5903
ET_BNK_DIRECT = 0x6000        # DirectReferenceTable
ET_BNK_RANGE = 0x6001         # RangeReferenceTable
ET_BNK_INDEX = 0x6002         # IndexReferenceTable

ET_WARC_INFO = 0x6800         # WaveArchiveFile_InfoBlock
ET_WARC_FILE = 0x6801         # WaveArchiveFile_FileBlock
ET_WFILE_INFO = 0x7000        # WaveFile_InfoBlock
ET_WFILE_DATA = 0x7001        # WaveFile_DataBlock

ET_GRP_INFO = 0x7800          # GroupFile_InfoBlock
ET_GRP_FILE = 0x7801          # GroupFile_FileBlock

WAVE_PCM8 = 0
WAVE_PCM16 = 1
WAVE_DSP = 2
ENC_NAMES = {0: "PCM8", 1: "PCM16", 2: "DSP_ADPCM"}

# MML 命令（src/snd-ply/MmlParser.cpp / MmlCommand.h）
M_WAIT, M_PRG, M_OPEN, M_JUMP, M_CALL = 0x80, 0x81, 0x88, 0x89, 0x8A
M_RANDOM, M_VARIABLE, M_IF, M_TIME, M_TIME_R, M_TIME_V = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
M_BANK, M_TRANSPOSE = 0xB6, 0xC3
M_EX, M_RET, M_ALLOC, M_FIN = 0xF0, 0xFD, 0xFE, 0xFF

RAW_EXT = {"fseq": ".bfseq", "fbnk": ".bfbnk", "fwar": ".bfwar", "fwsd": ".bfwsd"}
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def fail(msg):
    sys.exit(f"[错误] {msg}")


def clean_name(s, maxlen=100):
    s = ILLEGAL.sub("_", s or "").strip().strip(".")
    return s[:maxlen] or "noname"


def uniq_path(p: Path) -> Path:
    """已存在时追加 _2/_3/…，避免覆盖。"""
    if not p.exists():
        return p
    k = 2
    while True:
        q = p.with_name(f"{p.stem}_{k}{p.suffix}")
        if not q.exists():
            return q
        k += 1


# ---------------------------------------------------------------------------
# 基础读取（大端 Wii U；按文件头字节序自适应）
# ---------------------------------------------------------------------------
class RwError(Exception):
    pass


class Rw:
    __slots__ = ("d",)

    def __init__(self, data):
        self.d = data

    def _g(self, o, n, signed=False):
        if o < 0 or o + n > len(self.d):
            raise RwError(f"读取越界 @{o:#x} 需 {n} 字节（总 {len(self.d):#x}）")
        v = int.from_bytes(self.d[o:o + n], "big")
        return v - (1 << (n * 8)) if signed and v >= (1 << (n * 8 - 1)) else v

    def u8(self, o):  return self._g(o, 1)
    def u16(self, o): return self._g(o, 2)
    def u32(self, o): return self._g(o, 4)
    def s32(self, o): return self._g(o, 4, True)
    def s16(self, o): return self._g(o, 2, True)
    def magic(self, o): return self.d[o:o + 4]

    def cstr(self, o, cap=1024):
        i = self.d.find(b"\0", o, o + cap)
        return self.d[o:(i if i >= 0 else o + cap)].decode("utf-8", "replace")


def deref(rw, ref_pos, base, what, expect=None):
    """Util::Reference{u16 typeId, u16 pad, s32 offset}：offset 相对 ref 所在结构起点。"""
    tid = rw.u16(ref_pos)
    if expect is not None and tid != expect:
        raise RwError(f"{what}: 期望类型 {expect:#06x}，实际 {tid:#06x} @{ref_pos:#x}")
    off = rw.s32(ref_pos + 4)
    if off < 0:
        return tid, None
    tgt = base + off
    if tgt >= len(rw.d):
        raise RwError(f"{what}: 引用目标越界 @{tgt:#x}")
    return tid, tgt


def blocks(rw, n):
    """文件头 0x14 起的块表 → {typeId: (offset, size)}（offset 相对文件头）。"""
    return {rw.u16(0x14 + i * 12): (rw.s32(0x14 + i * 12 + 4), rw.u32(0x14 + i * 12 + 8))
            for i in range(n)}


def reftab(rw, table):
    """Util::ReferenceTable：count@0，条目 = {u16 typeId, u16 pad, s32 off}。"""
    n = rw.u32(table)
    out = []
    for i in range(n):
        p = table + 4 + i * 8
        if p + 8 > len(rw.d):
            raise RwError(f"引用表 @{table:#x} 条目 {i} 越界")
        off = rw.s32(p + 4)
        out.append((rw.u16(p), (table + off) if off >= 0 else None))
    return out


def u32tab(rw, table, cap=4):
    """Util::Table<u32>。"""
    n = min(rw.u32(table), cap)
    return [rw.u32(table + 4 + i * 4) for i in range(n)]


def flagval(rw, base, bit):
    """Util::BitFlag：flags@base；第 bit 位的值 = 后续第 popcount(flags&((2<<bit)-1)) 个 u32。"""
    f = rw.u32(base)
    if not (f >> bit) & 1:
        return None
    return rw.u32(base + 4 * bin(f & ((2 << bit) - 1)).count("1"))


def bin_head(rw):
    return rw.magic(0).decode("ascii", "replace"), rw.u16(4) == 0xFEFF, rw.u32(8)


# ---------------------------------------------------------------------------
# FWAR（Wave Archive）：只装 BFWAV 镜像
# ---------------------------------------------------------------------------
class Fwar:
    def __init__(self, data, name):
        self.data, self.name = data, name
        rw = Rw(data)
        mag, be, ver = bin_head(rw)
        if mag != "FWAR":
            raise ValueError(f"FWAR 魔数 {mag!r}")
        bl = blocks(rw, rw.u16(0x10))
        if ET_WARC_INFO not in bl or ET_WARC_FILE not in bl:
            raise ValueError("缺少 INFO/FILE 块")
        if rw.magic(bl[ET_WARC_INFO][0]) != b"INFO" or rw.magic(bl[ET_WARC_FILE][0]) != b"FILE":
            raise ValueError("INFO/FILE 块魔数不符")
        body = bl[ET_WARC_INFO][0] + 8          # INFO 块体
        file_body = bl[ET_WARC_FILE][0] + 8     # FILE 块体
        n = min(rw.u32(body), 65536)
        self.waves = []                         # (绝对位置, 大小)
        for i in range(n):
            p = body + 4 + i * 12
            off, size = rw.s32(p + 4), rw.u32(p + 8)
            if off < 0 or file_body + off > len(data):
                raise RwError(f"FWAR 波形 {i} 引用越界")
            self.waves.append((file_body + off, size))

    def slice_of(self, i):
        off, size = self.waves[i]
        return bytes(self.data[off:off + size])


# ---------------------------------------------------------------------------
# BFSAR
# ---------------------------------------------------------------------------
class Archive:
    def __init__(self, path: Path, debug=False):
        self.path = path
        self.d = path.read_bytes()
        self.rw = Rw(self.d)
        self.fcache = {}
        self.mag, self.be, self.ver = bin_head(self.rw)
        if self.mag != "FSAR":
            fail(f"{path.name}: 魔数 {self.mag!r}，仅支持 FSAR")
        try:
            self._parse(debug)
        except RwError as e:
            fail(f"{path.name}: 解析失败 — {e}")
        except Exception as e:
            fail(f"{path.name}: 解析失败 — {type(e).__name__}: {e}")

    def _parse(self, debug=False):
        rw, d = self.rw, self.d
        bl = blocks(rw, rw.u16(0x10))
        for et, m4 in ((ET_SAR_STRG, b"STRG"), (ET_SAR_INFO, b"INFO"), (ET_SAR_FILE, b"FILE")):
            if et not in bl or rw.magic(bl[et][0]) != m4:
                fail(f"{self.path.name}: {m4.decode()} 块缺失或魔数不符")
        self.bl = bl
        self.file_body = bl[ET_SAR_FILE][0] + 8     # FILE 块体

        # INFO 块体：8 个引用（snd_SoundArchiveFile.h InfoBlockBody）
        ib = bl[ET_SAR_INFO][0] + 8
        tables = [[] for _ in range(7)]
        for sec in range(7):                        # 0 Sound / 1 SoundGroup / 2 Bank
            _tid, tbl = deref(rw, ib + sec * 8, ib, f"INFO 引用[{sec}]")
            if tbl is not None:
                tables[sec] = reftab(rw, tbl)
        self.rt_sound, self.rt_grp, self.rt_bank = tables[0], tables[1], tables[2]
        self.rt_warc, self.rt_group, self.rt_file = tables[3], tables[4], tables[6]

        # 字符串表（STRG 块体 toSection[0]）
        sb = bl[ET_SAR_STRG][0] + 8
        self.strings = {}
        _t, st = deref(rw, sb, sb, "STRG→StringTable")
        if st is not None:
            for i in range(min(rw.u32(st), 1000000)):
                p = st + 4 + i * 12
                if p + 12 > len(d):
                    break
                off = rw.s32(p + 4)
                if off >= 0:
                    self.strings[i] = rw.cstr(st + off)
        if debug:
            print(f"  [debug] 版本 0x{self.ver:08X} | 大端={self.be} | 大小 {len(d):#x}")
            print(f"  [debug] Sound {len(self.rt_sound)} | Sets {len(self.rt_grp)} | Banks {len(self.rt_bank)}"
                  f" | Warcs {len(self.rt_warc)} | Groups {len(self.rt_group)} | Files {len(self.rt_file)}"
                  f" | 字符串 {len(self.strings)}")

        # FileInfo：内嵌（InternalFileInfo）或外部路径
        n_files = len(self.rt_file)
        self.loc, self.ext = [None] * n_files, {}
        for i, (_tid, fi) in enumerate(self.rt_file):
            if fi is None:
                continue
            try:
                ftid, target = deref(rw, fi, fi, f"FileInfo#{i}→位置")
                if target is None:
                    continue
                if ftid == EI_INTFILE:
                    off, size = rw.s32(target + 4), rw.u32(target + 8)   # ReferenceWithSize
                    # GetOffsetFromFileBlockHead = offset + BinaryBlockHeader(0x8)
                    if off not in (-1, INVALID):
                        self.loc[i] = (self.file_body + off, size)
                elif ftid == EI_EXTFILE:
                    self.ext[i] = rw.cstr(target)
            except RwError:
                continue

        # Group（FGRP）：BFSAR 的 GroupInfo.fileId → .bfgrp 数据；组内按 FileId 索引文件
        self.groups = []
        for gi, (_tid, gof) in enumerate(self.rt_group):
            if gof is None:
                self.groups.append((f"GROUP_{gi}", {}, None, ""))
                continue
            gname = self.name_at(gof + 4, f"GROUP_{gi}")
            gfid = rw.u32(gof)                      # GroupInfo.fileId@0
            fb, note = None, ""
            if gfid != INVALID:
                f = self.fdata_nogroups(gfid)
                if f:
                    fb, note = f[0], f[1]
            else:
                # 外部 .bfgrp（与 TuneBloom Bfsar::read 的兜底一致）
                for cand in (self.path.parent / f"{gname}.bfgrp",):
                    if cand.exists():
                        fb, note = cand.read_bytes(), cand.name
                        break
                else:
                    for cand in sorted(self.path.parent.glob("*.bfgrp")):
                        if cand.read_bytes()[:4] == b"FGRP":
                            fb, note = cand.read_bytes(), cand.name
                            break
            fmap = {}
            if fb and fb[:4] == b"FGRP":
                g = Rw(fb)
                try:
                    gb = blocks(g, g.u16(0x10))
                    if ET_GRP_INFO in gb and ET_GRP_FILE in gb:
                        g_ib, g_fb = gb[ET_GRP_INFO][0] + 8, gb[ET_GRP_FILE][0] + 8
                        for _gt, git in reftab(g, g_ib):
                            if git is None:
                                continue
                            gfid2 = g.u32(git)                  # GroupItemInfo.fileId@0
                            ft2 = g.u16(git + 4)                # embeddedItemInfo.typeId
                            off, size = g.s32(git + 8), g.u32(git + 0xC)
                            if gfid2 != INVALID and off >= 0 and ft2 != 0:
                                fmap[gfid2 & 0xFFFFFF] = (g_fb + off, size)
                except RwError:
                    pass
            self.groups.append((gname, fmap, fb, note))

    def fdata_nogroups(self, fid):
        """FileId → (bytes, 来源说明)，仅内嵌/外部。"""
        if fid == INVALID:
            return None
        i = fid & 0xFFFFFF
        if i < 0 or i >= len(self.rt_file):
            return None
        if i in self.fcache:
            return self.fcache[i], f"缓存#{i}"
        r = None
        if self.loc[i]:
            off, size = self.loc[i]
            r = bytes(self.d[off:off + size])
        elif i in self.ext:
            for cand in (self.path.parent / self.ext[i], self.path.parent / Path(self.ext[i]).name):
                if cand.exists():
                    r = cand.read_bytes()
                    break
        if r is not None:
            self.fcache[i] = r
            return r, (f"内嵌#{i}" if i not in self.ext else f"外部:{self.ext[i]}")
        return None

    def fdata(self, fid):
        """FileId → 数据：内嵌/外部，再兜底查各组的 FGRP。"""
        f = self.fdata_nogroups(fid)
        if f:
            return f
        i = (fid & 0xFFFFFF) if fid != INVALID else -1
        for gname, fmap, fb, note in self.groups:
            if i in fmap and fb:
                off, size = fmap[i]
                return bytes(fb[off:off + size]), f"组:{gname}({note or '内嵌'})"
        return None

    def name_at(self, flags_off, fallback):
        """optionParameter BitFlag 的 bit0 = STRING_ID。"""
        sid = flagval(self.rw, flags_off, 0)
        s = self.strings.get(sid) if sid not in (None, INVALID) else None
        return s or fallback


# ---------------------------------------------------------------------------
# BFBNK：乐器解析
# ---------------------------------------------------------------------------
def bank_parse(data: bytes):
    """FBNK → (WaveIdTable[(warcId, waveIdx)], 乐器表（含 None 空位，prg=表下标）)。

    对照 snd_BankFileReader/InfoBlockBody::GetInstrument：乐器参考表条目顺序即
    programNo（Null 条目同样占位）；TuneBloom BankFile::doRead 以表下标赋 mProgramNo。
    """
    rw = Rw(data)
    wtab, insts = [], []
    try:
        mag, _be, _ver = bin_head(rw)
        if mag != "FBNK":
            return [], []
        bl = blocks(rw, rw.u16(0x10))
        if ET_BNK_INFO not in bl or rw.magic(bl[ET_BNK_INFO][0]) != b"INFO":
            return [], []
        iib = bl[ET_BNK_INFO][0] + 8
        # InfoBlockBody: toWaveIdTable@0, toInstrumentReferenceTable@8
        _t, wt = deref(rw, iib, iib, "BFBNK→WaveIdTable")
        if wt is not None:
            for i in range(min(rw.u32(wt), 65536)):
                p = wt + 4 + i * 8
                wtab.append((rw.u32(p), rw.u32(p + 4)))
        _t2, it = deref(rw, iib + 8, iib, "BFBNK→乐器表")
        if it is not None:
            insts = [None] * rw.u32(it)     # 保留 Null 表位（prg 直接索引）
            for i, (_rt, inst) in enumerate(reftab(rw, it)):
                insts[i] = inst
        return wtab, insts
    except RwError:
        return wtab, insts


def resolve_entries(rw: Rw, chunk_tid: int, chunk: int, sel: int):
    """选择块（Direct 0x6000 / Range 0x6001 / Index 0x6002）按 sel 选一条区域引用。"""
    if chunk_tid == ET_BNK_DIRECT:
        off = rw.s32(chunk + 4)
        return (rw.u16(chunk), chunk + off) if off >= 0 else None
    if chunk_tid == ET_BNK_RANGE:
        n = min(rw.u32(chunk), 128)
        base = chunk + 4 + ((n + 3) & ~3)        # 边界表后按 4 对齐（GetRegionTableAddress 同款）
        for i in range(n):
            if sel <= rw.u8(chunk + 4 + i):      # border = 上界，第一个满足即选中
                p = base + i * 8
                off = rw.s32(p + 4)
                return (rw.u16(p), (chunk + off) if off >= 0 else None)
        return None
    if chunk_tid == ET_BNK_INDEX:
        mn, mx = rw.u8(chunk), rw.u8(chunk + 1)
        if sel < mn or sel > mx:
            return None
        p = chunk + 4 + (sel - mn) * 8
        off = rw.s32(p + 4)
        return (rw.u16(p), (chunk + off) if off >= 0 else None)
    return None


def bank_note_wave(data: bytes, insts, prg, key, vel):
    """prg 号乐器按 key/vel 定位 VelocityRegion → WaveIdTable 下标。
    insts 含 None（Null 乐器空位，prg=表下标）→ 返回 None。"""
    rw = Rw(data)
    if prg < 0 or prg >= len(insts):
        return None
    inst = insts[prg]
    if inst is None:                             # Null 乐器（ET_BNK_NULL 空位）
        return None
    ktid = rw.u16(inst)                          # Instrument.toKeyRegionChunk 引用@0（off@4）
    if ktid not in (ET_BNK_DIRECT, ET_BNK_RANGE, ET_BNK_INDEX):
        return None
    kchunk = inst + rw.s32(inst + 4)
    e = resolve_entries(rw, ktid, kchunk, key)
    if not e or e[0] != ET_BNK_KEYREGION or e[1] is None:
        return None
    vtid = rw.u16(e[1])                          # KeyRegion.toVelocityRegionChunk 引用@0
    if vtid not in (ET_BNK_DIRECT, ET_BNK_RANGE, ET_BNK_INDEX):
        return None
    vchunk = e[1] + rw.s32(e[1] + 4)
    e2 = resolve_entries(rw, vtid, vchunk, vel)
    if not e2 or e2[0] != ET_BNK_VELOCITY_REGION or e2[1] is None:
        return None
    return rw.u32(e2[1])                         # VelocityRegion.waveIdTableIndex@0


# ---------------------------------------------------------------------------
# BFSEQ：数据块 + 标签表 + MML 扫描（对照 src/snd-ply/MmlParser.cpp）
# ---------------------------------------------------------------------------
def seq_open(data: bytes):
    """FSEQ → (序列数据 bytes, [(raw偏移, 解析偏移, 标签名, allocTracks), ...])"""
    rw = Rw(data)
    if rw.magic(0) != b"FSEQ":
        return None, []
    bl = blocks(rw, rw.u16(0x10))
    if ET_SEQ_DATA not in bl:
        return None, []
    doff, dsize = bl[ET_SEQ_DATA]
    seq = data[doff + 8:doff + dsize]           # DataBlock 块体
    labels = []
    if ET_SEQ_LABEL in bl:
        try:
            lb = bl[ET_SEQ_LABEL][0] + 8        # LABL 块体
            n = min(rw.u32(lb), 65536)
            for i in range(n):
                p = lb + 4 + i * 8
                loff = rw.s32(p + 4)
                if loff < 0:
                    continue
                li = lb + loff                  # LabelInfo
                raw = rw.s32(li + 4)            # referToSequenceData.offset（序列数据内偏移）
                slen = rw.u32(li + 8)
                name = rw.cstr(li + 12, cap=max(slen, 1) + 8)
                parsed, alloc = raw, 0
                if 0 <= raw < len(seq) and seq[raw] == M_ALLOC:
                    alloc = (seq[raw + 1] << 8) | seq[raw + 2]
                    parsed = raw + 3            # ParseAllocTrack：段首 ALLOC → 从 +3 起
                labels.append((raw, parsed, name, alloc))
        except RwError:
            pass
    return seq, labels


def seq_scan_notes(data: bytes, start: int, max_steps=500000):
    """
    真实播放器语义的有序扫描（对照 src/snd-ply/MmlParser.cpp::parse）：
    - 主轨（track 0）从 start（parsed 偏移）线性执行；JUMP 跟随目标（不再并集，
      避免泄漏到相邻声音数据产生幻影音符）；CALL 压栈、RET 弹栈；
    - OPEN_TRACK 0x88 打开的新轨按出现顺序在之后扫描（平行轨，同样只取轨内音符）；
    - 0xA0/0xA1 RANDOM/VARIABLE 前缀改写参数宽度；0xA3/4/5 TIME 前缀的第二参数
      （s16/随机/变量），音符不追加该参数；
    - IF 0xA2 后的条件命令：pass-1 按“条件为假”（仅跳过参数）——即首次播放语义
      （变量初值 0，如 wtr_2hit_* 的击打计数保护走顺序主音）；pass-1 无音符时
      pass-2 按“条件为真”（执行）兜底（如由调用方预置变量才发声的声音）。
    - ALLOC 0xFE + u16；FIN 0xFF 停止当前轨；EX 0xF0 按低 4 位组解析参数。
    返回有序音符列表 [(bank, prg, key, vel), ...]（track0 在前，其余轨按打开序）。
    """
    end = len(data)
    start = max(0, min(start, end - 1))
    notes = []

    def read_vmidi(p):
        v = 0
        for _ in range(4):
            if p >= end:
                raise RwError("MML 变长参数越界")
            c = data[p]; p += 1
            v = (v << 7) | (c & 0x7F)
            if not (c & 0x80):
                break
        return v, p

    def read_arg(p, a_type):
        if a_type == "rand":                    # s16 min + s16 max（取下界作确定性代表值）
            if p + 4 > end:
                raise RwError("MML 随机参数越界")
            lo = int.from_bytes(data[p:p + 2], "big", signed=True)
            return lo, p + 4
        if a_type == "var":                     # u8 变量号
            if p + 1 > end:
                raise RwError("MML 变量参数越界")
            return None, p + 1
        if a_type == "s16":
            if p + 2 > end:
                raise RwError("MML s16 参数越界")
            return int.from_bytes(data[p:p + 2], "big", signed=True), p + 2
        if a_type == "vmidi":
            return read_vmidi(p)
        if p + 1 > end:
            raise RwError("MML 参数越界")
        return data[p], p + 1

    def scan_flow(s0, if_take):
        stack = []
        pos, transpose, prg, bank = s0, 0, 0, 0
        visited, steps = set(), 0
        while 0 <= pos < end:
            if pos in visited or steps > max_steps:
                break
            visited.add(pos); steps += 1
            a_t = a_t2 = None
            cond = False                        # 本条命令是否跟在 IF 之后
            cmd = data[pos]; pos += 1
            if cmd == M_IF:
                if pos >= end:
                    break
                cond = True
                cmd = data[pos]; pos += 1
            if cmd in (M_TIME, M_TIME_R, M_TIME_V):
                a_t2 = "s16" if cmd == M_TIME else ("rand" if cmd == M_TIME_R else "var")
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1
            if cmd == M_RANDOM:
                a_t = "rand"
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1
            elif cmd == M_VARIABLE:
                a_t = "var"
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1
            try:
                eff = (not cond) or if_take     # 条件命令是否真正执行
                if cmd < 0x80:                  # 音符：key, vel u8, 时长（前缀宽或 VMIDI）
                    if pos >= end:
                        break
                    vel = data[pos]; pos += 1
                    _, pos = read_arg(pos, a_t if a_t else "vmidi")
                    if eff:
                        notes.append((bank, prg, min(max(cmd + transpose, 0), 127), vel))
                    continue                    # TIME 前缀对音符不追加参数（解析器同款）
                grp = cmd & 0xF0
                if grp == 0x80:
                    if cmd == M_WAIT:
                        _, pos = read_arg(pos, a_t if a_t else "vmidi")
                    elif cmd == M_PRG:          # 乐器号 VMIDI，值生效
                        v, pos = read_arg(pos, a_t if a_t else "vmidi")
                        if eff and v is not None:
                            prg = v & 0xFFFF
                    elif cmd == M_OPEN:         # u8 轨号 + u24 数据偏移（绝对，新轨列队）
                        if pos + 4 > end:
                            break
                        off = (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3]
                        pos += 4
                        if eff:
                            spawn.append(off)
                    elif cmd in (M_JUMP, M_CALL):   # u24 数据偏移（绝对）
                        if pos + 3 > end:
                            break
                        off = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
                        pos += 3
                        if eff:
                            if cmd == M_JUMP:
                                pos = off      # 跟随跳转（真实播放器行为）
                            else:
                                stack.append((pos, transpose, prg, bank))
                                pos = off
                elif grp in (0xB0, 0xC0, 0xD0): # u8（C3 TRANSPOSE 取 s8；TIME 前缀追加第二参数）
                    v, pos = read_arg(pos, a_t if a_t else "u8")
                    if a_t2:
                        _, pos = read_arg(pos, a_t2)
                    if eff and v is not None:
                        if cmd == M_BANK:
                            bank = v
                        elif cmd == M_TRANSPOSE:
                            transpose = v - 256 if v >= 128 else v
                elif grp == 0xE0:
                    _, pos = read_arg(pos, a_t if a_t else "s16")
                elif grp == 0xF0:
                    if cmd == M_ALLOC:          # 与解析器一致：ALLOC 读 u16
                        pos += 2
                    elif cmd == M_FIN:
                        break
                    elif cmd == M_RET:
                        if not eff or not stack:
                            if not eff:
                                continue        # 条件为假的 RET：无参数，跳过
                            break
                        pos, transpose, prg, bank = stack.pop()
                    elif cmd == M_EX:
                        if pos >= end:
                            break
                        ex = data[pos]; pos += 1
                        e = ex & 0xF0
                        if e in (0xA0, 0xB0):   # EX u8：ReadByte（不吃前缀类型）
                            if pos + 1 > end:
                                break
                            pos += 1
                        elif e == 0xE0:         # EX u16：ReadArg(s16 / 前缀宽)
                            _, pos = read_arg(pos, a_t if a_t else "s16")
                        elif e in (0x80, 0x90): # EX u8 + u16
                            if pos + 3 > end:
                                break
                            pos += 1
                            _, pos = read_arg(pos, a_t if a_t else "s16")
                    # 0xFB/0xFC 等其余：无参数
                # 0x90 组：无参数
            except RwError:
                break

    for if_take in (False, True):               # pass-1 首次播放（条件假）→ pass-2 兜底（条件真）
        spawn = []                              # OPEN_TRACK 目标队列（平行轨）
        queue = [start]
        seen_flow = set()
        while queue:
            s0 = queue.pop(0)
            if s0 in seen_flow or not (0 <= s0 < end):
                continue
            seen_flow.add(s0)
            scan_flow(s0, if_take)
            if spawn:                           # OPEN_TRACK 新轨按打开序排入队列
                queue.extend(spawn)
                spawn = []
        if notes:
            break
    return notes


def seq_used_waves(seq_bytes, bank_datas, start_off):
    """
    bank_datas: [(数据, 名, fileId, bankItemId) | None]（对应声音的 bank 表顺序）。
    返回 (首个有效音符波形|None, 全部解析波形[(bank号,warcId,waveIdx)]（按音符序去重）,
          未匹配统计, 起始标签名)。
    """
    try:
        data, labels = seq_open(seq_bytes)
    except RwError:
        data, labels = None, []
    if data is None:
        return None, [], {"非FSEQ": 1}, ""
    lab = next((nm for raw, psd, nm, _a in labels
                if (raw == start_off or psd == start_off) and nm), "")
    if not (0 <= start_off < len(data)):
        return None, [], {"偏移越界": 1}, lab
    notes = seq_scan_notes(data, start_off)      # 有序音符（真实播放顺序）
    parsed = {}
    for bn, bd in enumerate(bank_datas):
        if bd and bd[0]:
            parsed[bn] = (*bank_parse(bd[0]), bd[0])
    resolved, misses = [], {}
    seen_w = set()
    for (bn, prg, key, vel) in notes:            # 按执行顺序，首个成功即代表波形
        wti, reason = None, None
        if bn not in parsed:
            reason = "无bank"
        else:
            wtab, insts, bdata = parsed[bn]
            wti = bank_note_wave(bdata, insts, prg, key, vel)
            if wti is None:                     # 力度区定位失败 → 中值重试（兼容 velocity 越界）
                wti = bank_note_wave(bdata, insts, prg, key, 64)
            if wti is None:
                reason = "无乐器/键区"
            elif wti >= len(wtab):
                wti, reason = None, "wave表越界"
            else:
                warc_id, widx = wtab[wti]
                if warc_id == INVALID or widx == INVALID:
                    reason = "空条目"
                else:
                    key3 = (bn, warc_id, widx)
                    if key3 not in seen_w:
                        seen_w.add(key3)
                        resolved.append(key3)
        if reason:
            k = f"{reason}(bank{bn})"
            misses[k] = misses.get(k, 0) + 1
    return (resolved[0] if resolved else None), resolved, misses, lab


# ---------------------------------------------------------------------------
# 变体感知扫描/解析
#   —— 符号执行：把"静态未知量"（随机区间 / 序列变量 / IF 条件）保留为候选集合，
#      一次遍历枚举全部可达波形。播放器语义逐条对照 src/snd-ply/MmlParser.cpp。
#
# 能改变"最终命中哪个 BFWAV"的机制共 4 类（旧版只覆盖第 ③ 类，所以
# SEQSET_ORIENTEEING 之类只导出 1 个波形、漏掉随机变体）：
#   ① RANDOM + PRG           随机乐器号 rand[lo,hi]（如 ori_startdemo 的
#                            rand[12,15]/rand[66,69] 脚步声）→ 多个乐器 → 多个波形
#   ② RANDOM + BANK_SELECT   随机 bank（0xB6 带 0xA0 前缀）
#   ③ RANDOM/VARIABLE + TRANSPOSE  键移调（含 EX SETVAR/RANDVAR 写入的变量，
#                            如 rgt_vce 的 rand[0,2] → key 36/37/38 → 3 个波形）
#   ④ IF(0xA2) 分支          cmpFlag 静态未知（取决于游戏状态，如地形决定
#                            草地/雪地脚步）→ 两条分支都可达，都要枚举
# 顺序约定：IF 假分支优先（路径权重 prio 小），保证"代表波形"（不带 _v 后缀的
# 那个文件）与旧版一致，不打断已有的本地化命名。
# ---------------------------------------------------------------------------
MAX_CAND = 64           # 单个候选集合上限（随机区间过长时截断，防爆量）
MAX_NOTE_COMBO = 2048   # 单个音符的候选组合上限
MAX_STATES = 30000      # 状态（分叉）数上限


class _AnyOf:
    """"取值由游戏侧决定"的未知量（变量被 VARIABLE 前缀引用，但序列里没赋值）。

    典型：SEQSET_ORIENTEEING 的 ori_vc_atti_male/female —— 序列先
    `VAR+SETVAR var32 = var30`（var30 是游戏写进去的全局变量），
    `DIVVAR var32,10` / `ADDVAR var32,10`，再 `VARIABLE+PRG var32`；
    换一个 var30 就换一个乐器 → 换一个波形，反复播放自然"随机"。
    这类值无从静态求值，只能按 bank 的可选乐器/键区枚举全部可能。
    cur = 序列里此前的取值，枚举时排在首位，保证"代表波形"与旧版一致。
    """
    __slots__ = ("cur",)

    def __init__(self, cur):
        self.cur = cur

    def __eq__(self, o):
        return isinstance(o, _AnyOf) and o.cur == self.cur

    def __hash__(self):
        return hash(("any", _sig(self.cur)))

    def __repr__(self):
        return "ANY(%r)" % (self.cur,)


def _cand(v):
    """候选值 → 升序元组（int/None/_AnyOf 视为单元素）。"""
    if isinstance(v, _AnyOf):
        return (v,)
    return tuple(sorted(v)) if isinstance(v, (set, frozenset)) else (v,)


def _norm(v):
    """单元素集合收敛为 int，便于比较/打印。"""
    if isinstance(v, (set, frozenset)):
        t = sorted(v)
        return t[0] if len(t) == 1 else frozenset(t)
    return v


def _sig(v):
    return _cand(v)


def seq_scan_notes_variants(data: bytes, start: int, max_steps=400000,
                            mode="branch"):
    """
    按"播放顺序"返回音符事件：
      [(prio, bank候选, prg候选, key候选, vel), ...]
    候选为 int 或 frozenset[int]；prio = 该路径上"IF 条件为真"的次数。

    mode:
      "prefer_false"  所有 IF 一律按"条件为假"（= 旧版 pass-1），不分支。
                      代表波形（不带 _v 后缀的那个文件）用它求，保证与旧版一致。
      "prefer_true"   所有 IF 一律按"条件为真"（= 旧版 pass-2 兜底）。
      "branch"        两条分支都枚举（含随机乐器/随机 bank/随机移调的全部候选），
                      用于求"该声音所有可达波形"。
    """
    end = len(data)
    if end == 0:
        return [], False
    start = max(0, min(start, end - 1))
    events = []
    rand_seen = [False]                          # 是否走到过随机指令（RANDOM/RANDVAR）
    # 状态：(pos, transpose, prg, bank, vars, stack, prio, skip_pos)
    #   vars: {变量号: 可能取值(int/frozenset/None=未知)}
    #   skip_pos: 需要"解析但不执行"的条件命令位置（IF 假分支复用同一条命令）
    pending = deque([(start, 0, 0, 0, {}, [], 0, -1)])
    seen = set()
    steps = 0

    def read_vmidi(p):
        v = 0
        for _ in range(4):
            if p >= end:
                raise RwError("MML 变长参数越界")
            c = data[p]; p += 1
            v = (v << 7) | (c & 0x7F)
            if not (c & 0x80):
                break
        return v, p

    def read_arg(p, t, variables):
        """→ (候选值 int/frozenset/None=未知, 新位置)。语义见 MmlParser::ReadArg。"""
        if t == "rand":                          # s16 min + s16 max → 闭区间
            if p + 4 > end:
                raise RwError("MML 随机参数越界")
            lo = int.from_bytes(data[p:p + 2], "big", signed=True)
            hi = int.from_bytes(data[p + 2:p + 4], "big", signed=True)
            if hi < lo:
                lo, hi = hi, lo
            if hi - lo + 1 > MAX_CAND:
                hi = lo + MAX_CAND - 1
            return frozenset(range(lo, hi + 1)), p + 4
        if t == "var":                           # u8 变量号 → 该变量的可能取值
            if p + 1 > end:
                raise RwError("MML 变量参数越界")
            return variables.get(data[p]), p + 1
        if t == "s16":
            if p + 2 > end:
                raise RwError("MML s16 参数越界")
            return int.from_bytes(data[p:p + 2], "big", signed=True), p + 2
        if t == "vmidi":
            return read_vmidi(p)
        if p + 1 > end:
            raise RwError("MML 参数越界")
        return data[p], p + 1

    while pending:
        pos, transpose, prg, bank, variables, stack, prio, skip = pending.popleft()
        while 0 <= pos < end:
            steps += 1
            if steps > max_steps or len(seen) > MAX_STATES:
                break
            sig = (pos, _sig(transpose), _sig(prg), _sig(bank),
                   tuple(sorted((k, _sig(v)) for k, v in variables.items())),
                   tuple(stack), skip)
            if sig in seen:                      # 该状态已探索（含循环保护）
                break
            seen.add(sig)

            cmd_pos = pos
            a_t = a_t2 = None
            cond = False                         # 本条命令是否跟在 IF 之后
            cmd = data[pos]; pos += 1
            if cmd == M_IF:
                if pos >= end:
                    break
                cond = True
                cmd = data[pos]; pos += 1
            if cmd in (M_TIME, M_TIME_R, M_TIME_V):
                a_t2 = "s16" if cmd == M_TIME else ("rand" if cmd == M_TIME_R else "var")
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1
            if cmd == M_RANDOM:
                a_t = "rand"
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1
            elif cmd == M_VARIABLE:
                a_t = "var"
                if pos >= end:
                    break
                cmd = data[pos]; pos += 1

            # IF 条件未知 → 分叉：先把"条件为假（本命令不执行）"排队，
            # 当前继续走"条件为真"路径（顺序：假优先，与旧版 pass-1 一致）。
            # 只有"执行与否会改变波形"的命令才分叉/计入 prio；音量、声像之类
            # 无害命令的两条分支等价，不分叉也不抬高 prio。
            eff = True
            if cond:
                if mode == "branch":
                    if skip == cmd_pos:
                        eff = False
                        skip = -1
                    elif cmd < 0x80 or cmd in (M_PRG, M_BANK, M_TRANSPOSE, M_JUMP,
                                               M_CALL, M_RET, M_OPEN, M_FIN, M_EX):
                        pending.append((cmd_pos, transpose, prg, bank, dict(variables),
                                        list(stack), prio, cmd_pos))
                        prio += 1                # 本路径走了"条件为真"分支
                elif mode == "prefer_false":
                    eff = False                  # 旧版 pass-1：一律按条件不成立
                # mode == "prefer_true"：eff 保持 True（旧版 pass-2）

            try:
                if eff and (a_t == "rand" or a_t2 in ("rand",)):
                    rand_seen[0] = True          # 本声音被走到了"随机"指令
                if cmd < 0x80:                   # 音符：key 就是命令字节（+移调）
                    if pos >= end:
                        break
                    vel = data[pos]; pos += 1
                    _, pos = read_arg(pos, a_t if a_t else "vmidi", variables)  # 时长不影响波形
                    if eff:
                        if isinstance(transpose, _AnyOf):
                            keys = _AnyOf(min(max(cmd, 0), 127))
                        else:
                            keys = frozenset(min(max(cmd + (t or 0), 0), 127)
                                             for t in _cand(transpose))
                        events.append((prio, bank, prg, _norm(keys), vel))
                    continue
                grp = cmd & 0xF0
                if grp == 0x80:
                    if cmd == M_WAIT:
                        _, pos = read_arg(pos, a_t if a_t else "vmidi", variables)
                    elif cmd == M_PRG:           # 乐器号：随机区间 → 候选乐器集合
                        v, pos = read_arg(pos, a_t if a_t else "vmidi", variables)
                        if eff:
                            # 变量由游戏侧赋值(序列里查不到) → 标记"未知"，
                            # 解析时按 bank 全部乐器枚举（见 _resolve_notes）
                            prg = _AnyOf(prg) if v is None else _norm(v)
                    elif cmd == M_OPEN:          # u8 轨号 + u24 段偏移（新轨另起状态）
                        if pos + 4 > end:
                            break
                        off = (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3]
                        pos += 4
                        if eff:
                            # 新轨从自己的段首开始：移调/乐器/bank 复位，
                            # 但玩家/全局变量延续（var 0..15 属 player，
                            # 故一并带入，否则会漏掉"先 SETVAR 再开轨"的变体）
                            pending.append((off, 0, 0, 0, dict(variables), [], prio, -1))
                    elif cmd in (M_JUMP, M_CALL):   # u24 段偏移（绝对）
                        if pos + 3 > end:
                            break
                        off = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
                        pos += 3
                        if eff:
                            if cmd == M_JUMP:
                                pos = off
                            else:
                                # 播放器 callStack 只存返回地址：被调段里改的
                                # 移调/乐器/bank/变量在 RET 之后继续生效
                                stack.append(pos)
                                pos = off
                elif grp in (0xB0, 0xC0, 0xD0):  # u8（TRANSPOSE/PITCH_BEND 取 s8）
                    v, pos = read_arg(pos, a_t if a_t else "u8", variables)
                    if a_t2:
                        _, pos = read_arg(pos, a_t2, variables)
                    if eff:
                        if v is None:                   # 变量未知（游戏侧赋值）
                            if cmd == M_BANK:
                                bank = _AnyOf(bank)
                            elif cmd == M_TRANSPOSE:
                                transpose = _AnyOf(transpose)
                        elif cmd == M_BANK:
                            bank = _norm(v)
                        elif cmd == M_TRANSPOSE:
                            transpose = _norm(frozenset(x - 256 if x >= 128 else x
                                                        for x in _cand(v)))
                elif grp == 0xE0:
                    _, pos = read_arg(pos, a_t if a_t else "s16", variables)
                elif grp == 0xF0:
                    if cmd == M_ALLOC:
                        pos += 2
                    elif cmd == M_FIN:
                        if eff:
                            break
                    elif cmd == M_RET:
                        if not eff:
                            continue
                        if not stack:
                            break
                        pos = stack.pop()
                    elif cmd == M_EX:
                        if pos >= end:
                            break
                        ex = data[pos]; pos += 1
                        e = ex & 0xF0
                        if e in (0xA0, 0xB0):    # EX u8
                            if pos + 1 > end:
                                break
                            pos += 1
                        elif e == 0xE0:          # EX u16
                            _, pos = read_arg(pos, a_t if a_t else "s16", variables)
                        elif e in (0x80, 0x90):  # 变量/比较：u8 变量号 + s16 参数
                            if pos + 1 > end:
                                break
                            var_no = data[pos]; pos += 1
                            arg2, pos = read_arg(pos, a_t if a_t else "s16", variables)
                            if eff:
                                if ex == 0x86:               # RANDVAR：0..arg2 均匀
                                    rand_seen[0] = True
                                    if arg2 is None:
                                        variables[var_no] = None
                                    else:
                                        hi = min(max(_cand(arg2)), MAX_CAND - 1)
                                        variables[var_no] = _norm(
                                            frozenset(range(0, max(hi, 0) + 1)))
                                elif ex == 0x80:             # SETVAR：常量/随机集合
                                    if a_t == "rand":
                                        rand_seen[0] = True
                                    variables[var_no] = arg2
                                elif 0x90 <= ex <= 0x95:
                                    pass                     # 比较：只写 cmpFlag
                                else:                        # 其它运算：值未知
                                    variables[var_no] = None
                # 0x90 组：无参数，且不影响波形
            except RwError:
                break

    events.sort(key=lambda e: e[0])              # 稳定排序：IF 假路径优先
    return events, rand_seen[0]


def bank_key_reps(data: bytes, insts, prg, limit=16):
    """该乐器键区的"每个区间取一个代表键"（用于移调未知时枚举可达波形）。
    Range 的边界值本身就是各区间的合法基准键；Direct 与键无关返回 [0]。"""
    rw = Rw(data)
    if prg < 0 or prg >= len(insts) or insts[prg] is None:
        return []
    inst = insts[prg]
    try:
        ktid = rw.u16(inst)
        kchunk = inst + rw.s32(inst + 4)
        if ktid == ET_BNK_DIRECT:
            return [0]
        if ktid == ET_BNK_RANGE:
            n = min(rw.u32(kchunk), 128)
            return [rw.u8(kchunk + 4 + i) for i in range(min(n, limit))]
        if ktid == ET_BNK_INDEX:
            mn, mx = rw.u8(kchunk), rw.u8(kchunk + 1)
            return list(range(mn, min(mx, mn + limit - 1) + 1))
    except RwError:
        return []
    return []


def _resolve_notes(events, parsed, misses, notes_seen=None):
    """音符事件 → 可达波形 [(标记, bank号, warcId, waveIdx)]（按候选/播放序去重）。

    标记 = (prg, key) 表示该波形是某个随机/变量候选（供 _v1/_v2 命名），
    确定性音符为 None。notes_seen 传入集合可在多趟之间共用去重表。
    """
    resolved = []
    seen_w = set() if notes_seen is None else notes_seen
    has_var = False
    for (prio, bank_c, prg_c, key_c, vel) in events:
        c_bank, c_prg, c_key = _cand(bank_c), _cand(prg_c), _cand(key_c)
        multi = (isinstance(bank_c, _AnyOf) or isinstance(prg_c, _AnyOf)
                 or isinstance(key_c, _AnyOf)
                 or len(c_bank) > 1 or len(c_prg) > 1 or len(c_key) > 1)
        if multi:
            has_var = True
        combos = 0
        banks = ([bank_c.cur] + [x for x in parsed if x != bank_c.cur]
                 if isinstance(bank_c, _AnyOf) else c_bank)
        for bn in banks:
            if bn not in parsed:
                misses["无bank"] = misses.get("无bank", 0) + 1
                continue
            wtab, insts, bdata = parsed[bn]
            if isinstance(prg_c, _AnyOf):
                # 乐器号由游戏侧变量决定：bank 内每个乐器都可能是它
                lim = min(len(insts), MAX_CAND)
                prgs = [prg_c.cur] + [p for p in range(lim) if p != prg_c.cur]
            else:
                prgs = c_prg
            for prg_v in prgs:
                if prg_v is None:
                    continue
                if isinstance(key_c, _AnyOf):
                    # 移调未知 → 该乐器每个键区都可能被命中，各取一个代表键
                    reps = bank_key_reps(bdata, insts, prg_v)
                    keys = [key_c.cur] + [k for k in reps if k != key_c.cur]
                else:
                    keys = c_key
                for key in keys:
                    if key is None:
                        continue
                    combos += 1
                    if combos > MAX_NOTE_COMBO:
                        break
                    wti = bank_note_wave(bdata, insts, prg_v, key, vel)
                    if wti is None:                  # 力度区定位失败 → 中值重试
                        wti = bank_note_wave(bdata, insts, prg_v, key, 64)
                    reason = None
                    if wti is None:
                        reason = "无乐器/键区(bank%d)" % bn
                    elif wti >= len(wtab):
                        reason = "wave表越界(bank%d)" % bn
                    else:
                        warc_id, widx = wtab[wti]
                        if warc_id == INVALID or widx == INVALID:
                            reason = "空条目(bank%d)" % bn
                    if reason:
                        misses[reason] = misses.get(reason, 0) + 1
                        continue
                    key3 = (bn, warc_id, widx)
                    if key3 in seen_w:
                        continue
                    seen_w.add(key3)
                    resolved.append(((prg_v, key) if multi else None,
                                     bn, warc_id, widx))
    return resolved, has_var


def seq_used_waves_variants(seq_bytes, bank_datas, start_off):
    """
    序列驱动 + 变体枚举（两趟）：
      第一趟 mode="prefer_false"（IF 假路径，空则退回 prefer_true）→ 求"代表波形"，
              与旧版 pass-1/pass-2 完全一致，已有导出文件名/本地化成果不受影响；
      第二趟 mode="branch"（IF 两条分支都走）→ 求"全部可达波形"（变体全集）。
    bank_datas: [(数据, 名, fileId) | None]（对应声音的 bank 表顺序）。
    返回 (first, used, variants, has_var, misses, lab)：
      first:   代表波形（该声音最可能的首次发声，导出时无后缀的那个文件）
      used:    全部可达波形 [(bank号, warcId, waveIdx)]（按播放/候选序去重）
      variants: 同上但带变体标记 [(标记, bank号, warcId, waveIdx)]，供 _v1/_v2 命名
      has_var: 是否存在"一次声音多条波形"（随机乐器/随机 bank/随机移调/IF 分支）
      misses:  未匹配统计；lab: 起始标签名
    """
    try:
        data, labels = seq_open(seq_bytes)
    except RwError:
        data, labels = None, []
    if data is None:
        return None, [], [], False, {"非FSEQ": 1}, ""
    lab = next((nm for raw, psd, nm, _a in labels
                if (raw == start_off or psd == start_off) and nm), "")
    if not (0 <= start_off < len(data)):
        return None, [], [], False, {"偏移越界": 1}, lab
    parsed = {}
    for bn, bd in enumerate(bank_datas):
        if bd and bd[0]:
            parsed[bn] = (*bank_parse(bd[0]), bd[0])

    # ① 代表波形：单一路径（IF 假优先，空则 IF 真），与旧版语义一致
    ev, _rs = seq_scan_notes_variants(data, start_off, mode="prefer_false")
    misses_first = {}
    first_list, _hv = _resolve_notes(ev, parsed, misses_first)
    if not first_list:
        ev, _rs = seq_scan_notes_variants(data, start_off, mode="prefer_true")
        misses_first = {}
        first_list, _hv = _resolve_notes(ev, parsed, misses_first)
    first = ((first_list[0][1], first_list[0][2], first_list[0][3])
             if first_list else None)

    # ② 全部可达波形：IF 两条分支 + 随机/变量候选全枚举
    ev_all, rand_seen = seq_scan_notes_variants(data, start_off, mode="branch")
    misses = {}
    resolved, has_var = _resolve_notes(ev_all, parsed, misses)
    # "一声音多波形"且确实走到过随机指令（RANDOM 前缀 / RANDVAR / 随机 SETVAR）
    # 才算随机变体；只有分层演奏（一条序列叠多个音，如 PERFECT）的确定性声音
    # 仍按旧行为只导出代表波形（需要全部分层时用 --all-notes）。
    has_var = bool(has_var or (rand_seen and len(resolved) > 1))
    if not resolved and misses_first:            # 第二趟没结果 → 用第一趟的未匹配统计
        misses = misses_first
    used = [(bn, w, i) for (_t, bn, w, i) in resolved]
    if first is None:
        first = used[0] if used else None
    elif resolved and first != (resolved[0][1], resolved[0][2], resolved[0][3]):
        # 让代表波形排在首位（导出时"无后缀"的那个文件始终是它）
        resolved.sort(key=lambda r: (r[1], r[2], r[3]) != first)
    return first, used, resolved, has_var, misses, lab


# ---------------------------------------------------------------------------
# BFWAV 解析 + DSP 解码 + WAV 写出
# ---------------------------------------------------------------------------
def parse_fwav(data: bytes, note: str):
    """FWAV → {enc, rate, loop, ls, le, samples, chs}。样本数 = TuneBloom 的调整后 loopEnd。"""
    rw = Rw(data)
    mag, big, ver = bin_head(rw)
    if mag != "FWAV":
        raise ValueError(f"{note}: 魔数 {mag!r}")
    if not 0x10000 <= ver <= 0x10200:
        raise ValueError(f"{note}: BFWAV 版本 0x{ver:08X}")
    bl = blocks(rw, rw.u16(0x10))
    if ET_WFILE_INFO not in bl or ET_WFILE_DATA not in bl:
        raise ValueError(f"{note}: 缺 INFO/DATA 块")
    io, do = bl[ET_WFILE_INFO][0], bl[ET_WFILE_DATA][0]
    if rw.magic(io) != b"INFO" or rw.magic(do) != b"DATA":
        raise ValueError(f"{note}: INFO/DATA 魔数不符")
    ib, db = io + 8, do + 8
    enc, loop = rw.u8(ib), rw.u8(ib + 1)
    rate = rw.u32(ib + 4)
    real_ls = rw.u32(ib + 8)                    # loopStartFrame
    nums = rw.u32(ib + 0xC)                     # loopEndFrame
    adj_ls = rw.u32(ib + 0x10) if ver >= 0x10200 else 0   # originalLoopStartFrame (v0.1.2.0)
    ctb = ib + 0x14                             # channelInfoReferenceTable
    nch = min(rw.u32(ctb), 2)
    dlen = rw.u32(do + 4) - 8                   # DATA 块体大小
    chans = []
    for i in range(nch):
        ch = ctb + rw.s32(ctb + 4 + i * 8 + 4)  # ChannelInfo
        samp = rw.s32(ch + 4)                   # referToSamples.offset
        aoff = rw.s32(ch + 0xC)                 # referToAdpcmInfo.offset
        if samp < 0 or db + samp > len(data):
            raise RwError(f"{note}: 通道{i} 数据越界 @{db + samp:#x}")
        raw = data[db + samp:db + dlen]
        if enc == WAVE_DSP:
            if aoff <= 0:
                raise ValueError(f"{note}: ADPCM 通道{i} 缺系数")
            ai = ch + aoff
            coefs = [rw.s16(ai + 2 * k) for k in range(16)]
            # DspAdpcmParam: coef[8][2]@0, predScale@0x20, yn1@0x22, yn2@0x24
            chans.append((raw, coefs, rw.s16(ai + 0x22), rw.s16(ai + 0x24)))
        else:
            chans.append((raw, None, 0, 0))
    if not chans:
        raise ValueError(f"{note}: 无通道")
    raw0 = chans[0][0]
    if enc == WAVE_DSP:
        rem = len(raw0) % 8
        n_data = (len(raw0) // 8) * 14 + (max(rem - 1, 0)) * 2 if rem else (len(raw0) // 8) * 14
    elif enc == WAVE_PCM8:
        n_data = len(raw0)
    else:
        n_data = len(raw0) // 2
    # WaveFile.cpp: mLoopEndFrame = loopEnd - (loopStart - originalLoopStart)
    eff = nums
    if loop and ver >= 0x10200:
        eff -= max(0, real_ls - adj_ls)
    n = min(n_data, max(eff, 0)) if nums else n_data
    return {"enc": enc, "rate": rate, "loop": bool(loop), "ls": real_ls, "le": nums,
            "samples": n, "chs": chans}


def decode_dsp(raw, coefs, yn1, yn2, n):
    """与 src/dsp/decode.cpp 逐位一致：8 字节帧 ↔ 14 样本，高位 nibble 在前。"""
    out = bytearray(2 * n)
    p = i = 0
    while i < n and p < len(raw):
        h = raw[p]; p += 1
        sc, pr = 1 << (h & 0xF), h >> 4
        c1, c2 = coefs[pr * 2], coefs[pr * 2 + 1]
        for s in range(14):
            if i >= n or p >= len(raw):
                break
            nib = (raw[p] >> 4) if (s & 1) == 0 else (raw[p] & 0xF)
            if (s & 1) == 1:
                p += 1
            if nib >= 8:
                nib -= 16
            v = (((sc * nib) << 11) + 1024 + c1 * yn1 + c2 * yn2) >> 11
            v = 32767 if v > 32767 else (-32768 if v < -32768 else v)
            yn2, yn1 = yn1, v
            out[2 * i] = v & 0xFF
            out[2 * i + 1] = (v >> 8) & 0xFF
            i += 1
    return out


def write_wav(path: Path, w):
    nch = len(w["chs"])
    sw = 1 if w["enc"] == WAVE_PCM8 else 2
    chdata = []
    for raw, coefs, yn1, yn2 in w["chs"]:
        if w["enc"] == WAVE_DSP:
            chdata.append(decode_dsp(raw, coefs, yn1, yn2, w["samples"]))
        elif w["enc"] == WAVE_PCM16:
            x = bytearray(2 * w["samples"])
            m = min(w["samples"], len(raw) // 2)
            for i in range(m):                  # 文件大端 → WAV 小端
                x[2 * i], x[2 * i + 1] = raw[2 * i + 1], raw[2 * i]
            chdata.append(x)
        else:
            x = bytearray(w["samples"])
            m = min(w["samples"], len(raw))
            for i in range(m):                  # 有符号 PCM8 → WAV 无符号
                x[i] = (raw[i] + 128) & 0xFF
            chdata.append(x)
    frames = bytearray()
    for i in range(w["samples"]):
        for ch in chdata:
            frames += (ch[2 * i:2 * i + 2] if sw == 2 else ch[i:i + 1])
    hdr = b"RIFF" + (36 + len(frames)).to_bytes(4, "little") + b"WAVE"
    hdr += b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
    hdr += nch.to_bytes(2, "little") + w["rate"].to_bytes(4, "little")
    hdr += (w["rate"] * nch * sw).to_bytes(4, "little")
    hdr += (nch * sw).to_bytes(2, "little") + (sw * 8).to_bytes(2, "little")
    hdr += b"data" + len(frames).to_bytes(4, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = hdr + bytes(frames)
    path.write_bytes(blob)
    return hashlib.md5(blob).hexdigest()          # 供导入端判断"该文件是否被改动过"


# ---------------------------------------------------------------------------
# BFWSD（Wave 声音专用，--all-sets 时使用；逻辑同 bfsar_export.py）
# ---------------------------------------------------------------------------
def fwsd_wave_table(data: bytes):
    rw = Rw(data)
    try:
        if rw.magic(0) != b"FWSD":
            return None
        bl = blocks(rw, rw.u16(0x10))
        if ET_WARC_INFO not in bl:              # BFWSD INFO 块类型 == 0x6800
            return None
        iib = bl[ET_WARC_INFO][0] + 8
        _t, wt = deref(rw, iib, iib, "BFWSD→WaveIdTable")
        if wt is None:
            return None
        return [(rw.u32(wt + 4 + i * 8), rw.u32(wt + 4 + i * 8 + 4))
                for i in range(min(rw.u32(wt), 65536))]
    except RwError:
        return None


def fwsd_note_wti(data: bytes, wsd_index: int):
    """BFWSD wave-sound data index → WaveIdTable 下标。"""
    rw = Rw(data)
    try:
        bl = blocks(rw, rw.u16(0x10))
        if ET_WARC_INFO not in bl:
            return None
        iib = bl[ET_WARC_INFO][0] + 8
        _t1, wt = deref(rw, iib, iib, "BFWSD→WaveIdTable")
        _t2, rt = deref(rw, iib + 8, iib, "BFWSD→WaveSoundData表")
        if rt is None:
            return None
        entries = reftab(rw, rt)
        if wsd_index >= len(entries) or entries[wsd_index][1] is None:
            return None
        wd = entries[wsd_index][1]
        _t3, nt = deref(rw, wd + 0x10, wd, "BFWSD→Note表")
        if nt is None:
            return None
        notes = reftab(rw, nt)
        if not notes or notes[0][1] is None:
            return None
        return rw.u32(notes[0][1])              # NoteInfo.waveIdTableIndex
    except RwError:
        return None


# ---------------------------------------------------------------------------
# 导出主流程
# ---------------------------------------------------------------------------
MANIFEST_HEADER = ["Set", "SetIndex", "SoundIndex", "SoundName", "类型", "SeqRaw",
                   "BankId", "BankName", "WaveArchiveId", "WaveIndex", "WARC名",
                   "编码", "采样率", "通道数", "采样数", "循环起", "循环止",
                   "输出/状态", "SeqStartOffset", "SeqLabel", "变体来源", "导出MD5"]


def mrow(*vals):
    r = list(vals) + [""] * (len(MANIFEST_HEADER) - len(vals))
    return r[:len(MANIFEST_HEADER)]


def why_row(set_name, si, snd, sname, kind, seq_raw, why, soff="", lab=""):
    """原因写入「输出/状态」列（下标 17），与旧版清单一致。"""
    r = mrow(set_name, si, snd, sname, kind, seq_raw)
    r[17] = why
    r[18], r[19] = soff, lab
    return r


def export_sar(arc: Archive, out_root: Path, args, manifest):
    rw = arc.rw

    # 预载全部 Wave Archive
    warcs = {}
    for wi, (_tid, woff) in enumerate(arc.rt_warc):
        if woff is None:
            continue
        try:
            fid = rw.u32(woff)                  # WaveArchiveInfo.fileId@0
            f = arc.fdata(fid)
            if not f:
                print(f"  [警告] WaveArchive {wi} 数据不可用（缺内嵌数据 / .bfgrp / .bfwar？）")
                continue
            warcs[wi] = (Fwar(f[0], arc.name_at(woff + 0x8, f"WARC_{wi}")), f[1], fid)
        except (RwError, ValueError) as e:
            print(f"  [警告] WaveArchive {wi} 解析失败: {e}")

    bp_cache = {}
    def bank_lookup(bk):
        bidx = bk & 0xFFFFFF
        if bidx not in bp_cache:
            info = None
            if 0 <= bidx < len(arc.rt_bank) and arc.rt_bank[bidx][1] is not None:
                boff = arc.rt_bank[bidx][1]
                bnk_fid = rw.u32(boff)          # BankInfo.fileId@0
                bf = arc.fdata(bnk_fid)
                info = (bf[0], arc.name_at(boff + 0xC, f"BNK_{bidx}"), bnk_fid) if bf else None
                if bf is None:
                    print(f"  [警告] bank {bidx} 数据缺失，涉及声音将无法解析")
            bp_cache[bidx] = info
        return bp_cache[bidx]

    raw_cache = {}
    def raw_dump(fid, tag, kind):
        if not args.dump_raw or fid == INVALID:
            return ""
        if fid in raw_cache:
            return raw_cache[fid]
        f = arc.fdata(fid)
        if not f:
            return ""
        rp = uniq_path(out_root / "_raw" / f"{kind}{fid & 0xFFFFFF:04d}_{clean_name(tag, 40)}{RAW_EXT[kind]}")
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_bytes(f[0])
        raw_cache[fid] = str(rp.relative_to(out_root))
        return raw_cache[fid]

    wv_cache = {}
    def fwav_cached(warc_key, widx, fw, note):
        key = (warc_key, widx)
        if key not in wv_cache:
            wv_cache[key] = parse_fwav(fw.slice_of(widx), note)
        return wv_cache[key]

    nsets = nwav = 0
    for si, (_tid, gof) in enumerate(arc.rt_grp):
        if gof is None:
            continue
        rs, re_ = rw.u32(gof), rw.u32(gof + 4)  # SoundGroupInfo.startId/endId
        stid, detail = deref(rw, gof + 0x10, gof, f"SoundSet#{si}→细节")
        is_wave = (stid == EI_WSNDGROUP and detail is not None)
        if is_wave and not args.all_sets:
            continue
        set_name = arc.name_at(gof + 0x18, f"{'WSDSET' if is_wave else 'SEQSET'}_{si}")
        if args.set_filter and args.set_filter.lower() not in set_name.lower():
            continue
        if rs == INVALID and re_ == INVALID:
            print(f"  ▸ {set_name}: 空 Set，跳过")
            continue
        a = min(rs & 0xFFFFFF, len(arc.rt_sound) - 1)
        b = min(max(re_ & 0xFFFFFF, a), len(arc.rt_sound) - 1)
        nsets += 1
        sub = out_root / clean_name(set_name)
        print(f"  ▸ {'Wave' if is_wave else 'Seq  '} Sound Set [{si}] {set_name}: Sound {a}..{b}（{b - a + 1}）")
        cnt = {"wav": 0, "skip": 0, "fail": 0, "empty": 0}
        sv = {"var": 0, "extra": 0, "multi": 0}  # 随机声音 / 额外变体波形 / 分层多波形声音

        for snd in range(a, b + 1):
            if snd >= len(arc.rt_sound):
                break
            _tid2, soff = arc.rt_sound[snd]
            if soff is None:
                continue
            try:
                file_id = rw.u32(soff)          # SoundInfo.fileId@0
                stype = rw.u16(soff + 0x0C)     # toDetailSoundInfo.typeId
                sname = arc.name_at(soff + 0x14, f"SOUND_{snd}")
                _dt, det = deref(rw, soff + 0x0C, soff, f"Sound#{snd}→细节")

                # ---- Wave 声音（--all-sets）----
                if stype == EI_WAVE and det is not None and args.all_sets:
                    widx0 = rw.u32(det)         # WaveSoundInfo.index@0
                    wsdf = arc.fdata(file_id)
                    wtab = fwsd_wave_table(wsdf[0]) if wsdf else None
                    wti = fwsd_note_wti(wsdf[0], widx0) if (wsdf and wtab) else None
                    if wtab and wti is not None and wti < len(wtab):
                        warc_id, widx = wtab[wti]
                        wi_ = warc_id & 0xFFFFFF
                        if wi_ in warcs and widx < len(warcs[wi_][0].waves):
                            fw, _wn, _wf = warcs[wi_]
                            wv = fwav_cached(wi_, widx, fw, f"{set_name}/{sname}")
                            outp = sub / f"s{snd:04d}_{clean_name(sname)}.wav"
                            wmd5 = write_wav(outp, wv)
                            row = mrow(set_name, si, snd, sname, "Wave",
                                       "", "", "", f"{warc_id:08X}", widx, fw.name,
                                       ENC_NAMES.get(wv["enc"], wv["enc"]), wv["rate"],
                                       len(wv["chs"]), wv["samples"], wv["ls"], wv["le"],
                                       str(outp.relative_to(out_root)))
                            row[21] = wmd5
                            manifest.append(row)
                            nwav += 1; cnt["wav"] += 1
                            continue
                    cnt["fail"] += 1
                    manifest.append(why_row(set_name, si, snd, sname, "Wave", "", "无法定位波形"))
                    continue

                # ---- Sequence 声音 ----
                if stype != EI_SEQ or det is None:
                    cnt["skip"] += 1
                    row = mrow(set_name, si, snd, sname,
                               f"非Seq({stype:#06x})" if stype != EI_SEQ else "Seq-detail空", "")
                    row[17] = "跳过"
                    manifest.append(row)
                    continue

                seq_raw = raw_dump(file_id, sname, "fseq")
                seqf = arc.fdata(file_id)
                # SequenceSoundInfo(0x10)：toBankIdTable@0(off@4) allocFlags@8 起始偏移@0xC(bit0)
                btab = det + rw.s32(det + 4)
                start_off = flagval(rw, det + 0xC, 0) or 0
                banks_ids = u32tab(rw, btab, cap=4) if btab > 0 else []
                bank_datas = []
                for bk in banks_ids:
                    if bk == INVALID:
                        bank_datas.append(None)
                        continue
                    info = bank_lookup(bk)
                    bank_datas.append((info[0], info[1], info[2], bk) if info else None)

                if args.all_bank_waves:         # 整 bank 波形（不含序列信息）
                    used, variants, has_var = [], [], False
                    for bn, bd in enumerate(bank_datas):
                        if not (bd and bd[0]):
                            continue
                        for warc_id, widx in bank_parse(bd[0])[0]:
                            if warc_id != INVALID and widx != INVALID:
                                used.append((bn, warc_id, widx))
                    first, misses, lab = (used[0] if used else None), {}, ""
                else:                           # 序列驱动：真实播放顺序 + 随机变体枚举
                    first, used, variants, has_var, misses, lab = ((None, [], [], False, {}, "")
                                                                    if not seqf else
                                                                    seq_used_waves_variants(seqf[0], bank_datas, start_off))

                if not used and not variants:
                    cnt["empty"] += 1
                    if misses.get("非FSEQ"):
                        why = "bfseq 数据不可读"
                    elif misses.get("偏移越界"):
                        why = f"Seq起始偏移越界({start_off:#x})"
                    elif misses.get("无bank") or all(bd is None for bd in bank_datas):
                        why = "bank 数据缺失"
                    elif misses:
                        why = "未解析到演奏波形（" + "，".join(f"{k}×{n}" for k, n in sorted(misses.items())) + "）"
                    else:
                        why = "序列段无音符"
                    manifest.append(why_row(set_name, si, snd, sname, "Seq", seq_raw, why,
                                            f"{start_off:#x}", lab))
                    continue

                # 默认每声音 1 个代表波形（=UI 点击该行最先发声）；
                # --all-notes 保留全部命中波形（普通声音多波形用 _2/_3 后缀，
                #   如 PERFECT 这类一条序列叠多音高的声音）；
                # 随机/变量/分支导致的"一声音多波形"（如 ori_startdemo 的草地/雪地
                #   脚步 rand[12,15]/rand[66,69]）：导出全部可达波形，
                #   代表波形用原名，其余 _v1/_v2… 后缀（与普通声音命名不冲突）。
                is_variant = has_var and len(variants) > 1 and not args.no_variants
                if has_var:
                    sv["var"] += 1
                    if is_variant:
                        sv["extra"] += len(variants) - 1
                elif len(variants) > 1:
                    sv["multi"] += 1            # 确定性分层多波形（需 --all-notes 才全导出）
                if is_variant:
                    ordered = list(variants)             # 已保证代表波形在首位
                    suffix_of = lambda k: "" if k == 0 else f"_v{k}"
                else:
                    if args.all_notes:
                        ordered = [(None, bn, w, i) for (bn, w, i) in used]
                        if first and first not in used:
                            ordered.append((None,) + first)
                    else:
                        ordered = [(None,) + first] if first else []
                    suffix_of = lambda k: "" if k == 0 else f"_{k + 1}"
                for k, (tag, bn, warc_id, widx) in enumerate(ordered):
                    wi_ = warc_id & 0xFFFFFF
                    bd_name = bank_datas[bn][1] if bn < len(bank_datas) and bank_datas[bn] else ""
                    note_v = (f"prg{tag[0]} key{tag[1]}" if isinstance(tag, tuple) else "")
                    if wi_ not in warcs:
                        cnt["fail"] += 1
                        manifest.append(mrow(set_name, si, snd, sname, "Seq", seq_raw, f"bank{bn}",
                                             bd_name, f"{warc_id:08X}", widx, "", "", "", "", "", "",
                                             "warc数据缺失", f"{start_off:#x}", lab, note_v))
                        continue
                    fw, _wn, _wf = warcs[wi_]
                    if widx >= len(fw.waves):
                        continue
                    try:
                        wv = fwav_cached(wi_, widx, fw, f"{set_name}/{sname}")
                        stem = f"s{snd:04d}_{clean_name(sname)}"
                        outp = sub / f"{stem}{suffix_of(k)}.wav"
                        wmd5 = write_wav(outp, wv)
                    except Exception as e:
                        cnt["fail"] += 1
                        manifest.append(mrow(set_name, si, snd, sname, "Seq", seq_raw, f"bank{bn}",
                                             bd_name, f"{warc_id:08X}", widx, "", "", "", "", "", "",
                                             f"失败:{e}", f"{start_off:#x}", lab, note_v))
                        continue
                    nwav += 1; cnt["wav"] += 1
                    row = mrow(set_name, si, snd, sname, "Seq", seq_raw, f"bank{bn}",
                               bd_name, f"{warc_id:08X}", widx, fw.name,
                               ENC_NAMES.get(wv["enc"], wv["enc"]), wv["rate"], len(wv["chs"]),
                               wv["samples"], wv["ls"], wv["le"],
                               str(outp.relative_to(out_root)), f"{start_off:#x}", lab,
                               note_v)
                    row[21] = wmd5
                    manifest.append(row)
            except RwError as e:
                cnt["fail"] += 1
                manifest.append(why_row(set_name, si, snd, f"SOUND_{snd}", "错误", "", f"{e}（跳过）"))
        print(f"    波形 {cnt['wav']} | 无波形 {cnt['empty']} | 跳过 {cnt['skip']} | 失败 {cnt['fail']}"
              + (f" | 随机变体声音 {sv['var']}（额外变体 +{sv['extra']}）" if sv["var"] else "")
              + (f" | 分层多波形声音 {sv['multi']}（--all-notes 可全导出）" if sv["multi"] else ""))
    return nsets, nwav


def export_all_warcs(arc: Archive, out_root: Path, manifest):
    """--warcs：整库导出所有 Wave Archive 的全部 BFWAV（供对照/补全）。"""
    rw = arc.rw
    for wi, (_tid, woff) in enumerate(arc.rt_warc):
        if woff is None:
            continue
        try:
            fid = rw.u32(woff)
            f = arc.fdata(fid)
            if not f:
                continue
            fw = Fwar(f[0], arc.name_at(woff + 0x8, f"WARC_{wi}"))
        except (RwError, ValueError) as e:
            print(f"  [警告] WaveArchive {wi} 整库导出失败: {e}")
            continue
        print(f"  ▸ 整库 {fw.name}: {len(fw.waves)} 个 BFWAV")
        o = out_root / clean_name(fw.name)
        for i in range(len(fw.waves)):
            try:
                wv = parse_fwav(fw.slice_of(i), f"{fw.name}#{i}")
                outp = o / f"w{i:04d}.wav"
                wmd5 = write_wav(outp, wv)
                row = mrow("_warcs", wi, i, "", "WARC", "", "", "", f"{fid:08X}", i, fw.name,
                           ENC_NAMES.get(wv["enc"], wv["enc"]), wv["rate"], len(wv["chs"]),
                           wv["samples"], wv["ls"], wv["le"], str(outp.relative_to(out_root)))
                row[21] = wmd5
                manifest.append(row)
            except Exception as e:
                print(f"    [失败] w{i:04d}: {e}")


def list_structure(arc: Archive):
    rw = arc.rw
    print(f"  Sounds {len(arc.rt_sound)} | SoundSets {len(arc.rt_grp)} | Banks {len(arc.rt_bank)} | "
          f"WaveArchives {len(arc.rt_warc)} | Groups {len(arc.rt_group)} | Files {len(arc.rt_file)}")
    seq_n = wave_n = 0
    for si, (_tid, gof) in enumerate(arc.rt_grp):
        if gof is None:
            continue
        try:
            rs, re_ = rw.u32(gof), rw.u32(gof + 4)
            stid, detail = deref(rw, gof + 0x10, gof, f"SoundSet#{si}→细节")
            is_wave = (stid == EI_WSNDGROUP and detail is not None)
            nm = arc.name_at(gof + 0x18, f"{'WSDSET' if is_wave else 'SEQSET'}_{si}")
            rng = "空" if (rs == INVALID and re_ == INVALID) else f"{rs & 0xFFFFFF}..{re_ & 0xFFFFFF}"
            n = (re_ & 0xFFFFFF) - (rs & 0xFFFFFF) + 1 if rs != INVALID else 0
            print(f"  [{'Wave' if is_wave else 'Seq  '}] Set[{si:3}] {nm}: {rng}（{max(n, 0)} 声音）")
            if is_wave:
                wave_n += 1
            else:
                seq_n += 1
        except RwError as e:
            print(f"  Set[{si}] 解析失败: {e}")
    print(f"  合计：Seq Set {seq_n} 个 | Wave Set {wave_n} 个")


def main():
    ap = argparse.ArgumentParser(
        description="批量导出 BFSAR Sequence Sound Sets 的音频（BFWAV→WAV，仅标准库）")
    ap.add_argument("inputs", nargs="*", help="bfsar 文件或目录（缺省=当前目录全部）")
    ap.add_argument("-o", "--out", default=None, help="输出根目录（默认 <档案名>_seqsets/）")
    ap.add_argument("--set-filter", default=None, help="仅导出名称含该子串的 Set")
    ap.add_argument("--all-sets", action="store_true", help="Wave Sound Set 也导出（默认仅 Sequence）")
    ap.add_argument("--all-bank-waves", action="store_true",
                    help="旧行为：导出声音所属 bank 的全部波形（默认仅序列实际演奏的）")
    ap.add_argument("--all-notes", action="store_true",
                    help="每个声音导出其全部命中波形（_2/_3…；默认仅 1 个代表波形=UI 首音）；"
                         "分层演奏的声音（如 PERFECT 一条序列叠多音高）需要它才能全部导出")
    ap.add_argument("--no-variants", action="store_true",
                    help="不导出随机变体（每个声音只保留代表波形，不生成 _v1/_v2…）；"
                         "默认对随机声音导出全部可达波形，否则游戏内反复播放会命中未本地化波形")
    ap.add_argument("--list", action="store_true", help="只列出结构")
    ap.add_argument("--debug", action="store_true", help="打印版本/计数诊断")
    ap.add_argument("--dump-raw", action="store_true", help="另存 .bfseq/.bfbnk 原始文件")
    ap.add_argument("--warcs", action="store_true", help="额外整库导出全部 BFWAR")
    args = ap.parse_args()

    paths = []
    for a in args.inputs or []:
        p = Path(a)
        if p.is_dir():
            paths += [c for c in sorted(p.iterdir())
                      if c.is_file() and c.suffix.lower() == ".bfsar"
                      and not c.name.startswith("~$")]
        elif p.exists():
            paths.append(p)
        else:
            print(f"[跳过] 不存在：{a}")
    if not paths and not args.inputs:
        paths = [c for c in sorted(Path.cwd().iterdir())
                 if c.is_file() and c.suffix.lower() == ".bfsar"
                 and not c.name.startswith("~$")]
    if not paths:
        fail("未找到 .bfsar")

    for p in paths:
        print(f"\n===== {p.name} =====")
        arc = Archive(p, debug=args.debug)
        if args.list:
            list_structure(arc)
            continue
        out = Path(args.out) if args.out else p.with_name(p.stem + "_seqsets")
        if args.out and len(paths) > 1:
            out = out / p.stem
        out.mkdir(parents=True, exist_ok=True)
        manifest = [MANIFEST_HEADER]
        nsets, nwav = export_sar(arc, out, args, manifest)
        if args.warcs:
            export_all_warcs(arc, out / "_warcs", manifest)
        with open(out / "_manifest.csv", "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(manifest)
        with open(out / "_report.txt", "w", encoding="utf-8-sig") as f:
            f.write(f"{p.name}：导出 Sound Set {nsets} 个，wav {nwav} 个\n")
        print(f"  共 {nsets} 个 Set，导出 {nwav} 个 wav → {out}")


if __name__ == "__main__":
    main()
