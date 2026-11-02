#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TuneBloom batch WAV importer (BFSAR container) — bfsar_export.py 的配套批量导入脚本.

严格复刻 TuneBloom 的导入/写出路径, 未使用其他方法:
  * RIFF WAV 读取         WaveFile::readRiffWavInfo / readWavFile   (src/bfsar/WaveFile.cpp)
  * PCM spool + 重编码    WaveFile::convertChannel_ + src/dsp/encode.cpp (correlateCoefs /
                          DSPEncodeFrame) + src/dsp/decode.cpp getLoopContext
  * BFWAV 镜像写出        WaveFile::doWrite
  * FWAR 打包             BfwarFile::doWrite
  * BFSAR FILE 块重组     Bfsar::save_ 的 File Block 规则 + FileWriter (src/bfsar/FileWriter.cpp)
  * BFSAR 解析            与 bfsar_export.py 相同的解析代码 (导出已验证 hash 一致)

与 TuneBloom GUI "Replace" 相同的行为: 采样率/采样数/编码取自导入 wav(编码可选,
默认 DspAdpcm = GUI 默认值); 按 TuneBloom 的 RiffWaveInfo(TODO: Loop info?)约定,
导入后波形不再循环: isLoop=0, loopStart=0, loopEnd=采样总数。

输入目录结构 = 导出脚本输出:
    <export目录>/<wave sound set 名>/<sound 名>.wav ...  +  <wave sound set 名>.txt
导入时以 .txt 清单为准: 只导入"文件名与 .txt 记录完全一致"的 wav;
文件夹里存在但 .txt 没有记录的文件不导入(打印提示后忽略)。

用法: python bfsar_import.py <file.bfsar> [export目录] [输出.bfsar]
          [-e pcm8|pcm16|dspadpcm|keep] [-o 输出.bfsar] [-y]
仅依赖 Python 标准库。
"""

import hashlib
import math
import os
import struct
import sys

# ============================================================
# Part 1 — BFSAR 解析 (与 bfsar_export.py 完全一致, 仅少量扩展)
# ============================================================

EB = ">"   # big endian
EL = "<"   # little endian


class R:
    def __init__(self, data):
        self.d = data
        self.e = EB if struct.unpack_from(">H", data, 4)[0] == 0xFEFF else EL

    def u16(self, o): return struct.unpack_from(self.e + "H", self.d, o)[0]
    def u32(self, o): return struct.unpack_from(self.e + "I", self.d, o)[0]
    def s32(self, o): return struct.unpack_from(self.e + "i", self.d, o)[0]
    def s16(self, o): return struct.unpack_from(self.e + "h", self.d, o)[0]

    def ref(self, o):
        # Util::Reference {typeId u16, padding u16, offset s32}
        return (self.u16(o), self.u16(o + 2), self.s32(o + 4))

    def refws(self, o):
        # Util::ReferenceWithSize {typeId u16, padding u16, offset s32, size u32}
        return (self.u16(o), self.u16(o + 2), self.s32(o + 4), self.u32(o + 8))

    def cstr(self, o):
        e = self.d.index(b"\x00", o)
        return self.d[o:e].decode("latin-1", "replace")


# element type ids (snd_ElementType.h)
ET_SAR_STR = 0x2000; ET_SAR_IFO = 0x2001; ET_SAR_FLE = 0x2002
EI_WAVE = 0x2202; EI_SEQ = 0x2203; EI_WSNDGROUP = 0x2205; EI_INTFILE = 0x220C
ET_WARC_IFO = 0x6800; ET_WARC_FLE = 0x6801
ET_WFILE_IFO = 0x7000; ET_WFILE_DAT = 0x7001
ET_WFILE_CHN = 0x7100          # ElementType_WaveFile_ChannelInfo
ET_CODEC_DSPADPCM_INFO = 0x0300  # ElementType_Codec_DspAdpcmInfo
ET_GENERAL_BYTESTREAM = 0x1F00   # ElementType_General_ByteStream
ET_WSND_IFO = 0x6800           # BFWSD INFO block (== 0x6800, 同 wave archive 类)
WAVE_PCM8 = 0; WAVE_PCM16 = 1; WAVE_DSP = 2
SPF = 14                       # SAMPLES_PER_FRAME
FSAR_BFWAR_VERSION = 0x00010000   # Bfsar::getVersionForBfwar() (固定值)


def parse_fwar(r, base):
    """FWAR → [(wave镜像绝对位置, wave镜像大小), ...] (导出脚本逻辑 + size)."""
    nb = r.u16(base + 0x10); ifo = fle = None
    for k in range(nb):
        tid, pad, off, size = r.refws(base + 0x14 + k * 12)
        if tid == ET_WARC_IFO: ifo = base + off
        elif tid == ET_WARC_FLE: fle = base + off
    if ifo is None or fle is None:
        raise ValueError("FWAR block missing")
    tab = ifo + 8; cnt = r.u32(tab); waves = []
    for i in range(cnt):
        tid, pad, off, size = r.refws(tab + 4 + i * 12)
        waves.append((fle + 8 + off, size))
    return waves


def wsnd_wave_id(r, base, dataIdx):
    """BFWSD wave-sound data index → (waveArchiveId, waveIndex). (导出脚本原样)"""
    nb = r.u16(base + 0x10); info = None
    for k in range(nb):
        tid, pad, off, size = r.refws(base + 0x14 + k * 12)
        if tid == ET_WSND_IFO: info = base + off
    if info is None:
        raise ValueError("BFWSD INFO missing")
    body = info + 8
    tid, pad, offW = r.ref(body)        # toWaveIdTable
    tid, pad, offD = r.ref(body + 8)    # toWaveSoundDataReferenceTable
    waveIdTab = body + offW; dataTab = body + offD
    if dataIdx >= r.u32(dataTab):
        return None
    tid, pad, off = r.ref(dataTab + 4 + dataIdx * 8)
    meta = dataTab + off
    tid, pad, offN = r.ref(meta + 16)   # toNoteInfoReferenceTable
    noteTab = meta + offN
    if r.u32(noteTab) == 0:
        return None
    tid, pad, noff = r.ref(noteTab + 4)  # note 0
    wIdx = r.u32(noteTab + noff)         # NoteInfo.waveIdTableIndex
    wid = waveIdTab + 4 + wIdx * 8
    return (r.u32(wid), r.u32(wid + 4))


def load_bfsar(data):
    """BFSAR 解析 — 与导出脚本一致, 另为每个 file 捕获 FileInfo/sized-ref 位置(重打包用)."""
    r = R(data)
    if data[:4] != b"FSAR":
        raise ValueError("Not a BFSAR")
    s_off = i_off = f_off = None
    for k in range(3):
        tid, pad, off, size = r.refws(0x14 + k * 12)
        if tid == ET_SAR_STR: s_off = off
        elif tid == ET_SAR_IFO: i_off = off
        elif tid == ET_SAR_FLE: f_off = off
    if s_off is None or i_off is None or f_off is None:
        raise ValueError("blocks")
    sb = s_off + 8
    tid, pad, stOff = r.ref(sb)                 # toSection[0] = StringTable
    stab = sb + stOff; stcnt = r.u32(stab)

    def get_str(sid):
        if sid == 0xFFFFFFFF or sid >= stcnt:
            return None
        return r.cstr(stab + r.s32(stab + 4 + sid * 12 + 4))

    ib = i_off + 8
    tid, pad, sgOff = r.ref(ib + 8);  sgTab = ib + sgOff;  sgcnt = r.u32(sgTab)
    tid, pad, fiOff = r.ref(ib + 48); fiTab = ib + fiOff;  ficnt = r.u32(fiTab)
    tid, pad, waOff = r.ref(ib + 24); waTab = ib + waOff;  wacnt = r.u32(waTab)
    tid, pad, sdOff = r.ref(ib);      sndTab = ib + sdOff; sndcnt = r.u32(sndTab)
    sets = []
    for g in range(sgcnt):
        tid, pad, off = r.ref(sgTab + 4 + g * 8); go = sgTab + off
        start = r.u32(go) & 0xFFFFFF; end = r.u32(go + 4) & 0xFFFFFF
        dtid, dpad, doff = r.ref(go + 16); isWave = (dtid == EI_WSNDGROUP)
        opt = r.u32(go + 24); name = None
        if opt & 1: name = get_str(r.u32(go + 28))
        if not name: name = ("WSDSET_%u" % g) if isWave else ("SEQSET_%u" % g)
        sets.append({"idx": g, "name": name, "wave": isWave, "start": start, "end": end})
    sounds = []
    for si in range(sndcnt):
        tid, pad, off = r.ref(sndTab + 4 + si * 8); so = sndTab + off
        fileId = r.u32(so)
        dtid, dpad, doff = r.ref(so + 12)
        typ = ("seq" if dtid == EI_SEQ else "wave" if dtid == EI_WAVE else "strm")
        wsIdx = 0
        if dtid == EI_WAVE:
            wsIdx = r.u32(so + doff)   # SoundArchive::WaveSoundInfo.index
        opt = r.u32(so + 20); nm = None
        if opt & 1: nm = get_str(r.u32(so + 24))
        if not nm: nm = "SOUND_%u" % si
        sounds.append({"idx": si, "fileId": fileId, "type": typ, "name": nm, "waveIdx": wsIdx})
    files = []
    for fi in range(ficnt):
        tid, pad, off = r.ref(fiTab + 4 + fi * 8); fo = fiTab + off
        ltid, lpad, loff = r.ref(fo)
        # FileInfo.toFileLocation 在 fo; InternalFileInfo(=sized ref) 在 fo+loff
        entry = {"idx": fi, "fo": fo, "loff": loff, "ltid": ltid}
        if ltid == EI_INTFILE:
            off2 = r.s32(fo + loff + 4); size = r.u32(fo + loff + 8)
            entry["internal"] = True
            entry["abs"] = f_off + 8 + off2 if off2 != -1 else None
            entry["size"] = size
        else:
            entry["internal"] = False
            entry["abs"] = None; entry["size"] = None
        files.append(entry)
    archives = []
    for a in range(wacnt):
        tid, pad, off = r.ref(waTab + 4 + a * 8); ao = waTab + off
        aid = r.u32(ao)
        img = files[aid]["abs"] if aid < len(files) and files[aid]["internal"] else None
        archives.append({"fileId": aid, "abs": img})
    return {"r": r, "sounds": sounds, "sets": sets, "files": files,
            "archives": archives, "fiTab": fiTab, "fileOff": f_off}


def build_flat_wave_map(b):
    """导出脚本 flat 列表的等价物, 额外跟踪每个 flat 项所在的 archive."""
    r = b["r"]
    aw = [parse_fwar(r, a["abs"]) if a["abs"] is not None else []
          for a in b["archives"]]
    flat = []   # [(archiveIdx, waveLocalIdx, abs, size)] — 拼接顺序与导出脚本一致
    for archIdx, lst in enumerate(aw):
        for wi, (abspos, size) in enumerate(lst):
            flat.append((archIdx, wi, abspos, size))
    return aw, flat


def resolve_wave_target(b, so, flat, aw):
    """复刻导出脚本 main() 中 wave 镜像的定位逻辑, 返回 (fileId, waveIdx) 或 None."""
    r = b["r"]
    f = b["files"][so["fileId"]] if so["fileId"] < len(b["files"]) else None
    if not (f and f["internal"] and f["abs"] is not None):
        return None
    try:
        wid = wsnd_wave_id(r, f["abs"], so["waveIdx"])
    except Exception:
        return None
    if not wid:
        return None
    waId, wIndex = wid
    if waId == 0:                              # 导出脚本: waId==0 → flat[wIndex]
        if wIndex >= len(flat):
            return None
        archIdx, wi, _abs, _sz = flat[wIndex]
        return (b["archives"][archIdx]["fileId"], wi)
    aa = waId & 0xFFFFFF
    if aa >= len(aw) or wIndex >= len(aw[aa]):
        return None
    return (b["archives"][aa]["fileId"], wIndex)


def parse_bfwav_lite(r, base):
    """读取 WaveFile::doRead(snd_WaveFileReader::ReadWaveInfo) 所需字段, 不解码采样."""
    if bytes(r.d[base:base + 4]) != b"FWAV":
        raise ValueError("not a BFWAV")
    version = r.u32(base + 8)
    nb = r.u16(base + 0x10); ifo = dat = None; datSize = 0
    for k in range(nb):
        tid, pad, off, size = r.refws(base + 0x14 + k * 12)
        if tid == ET_WFILE_IFO: ifo = base + off
        elif tid == ET_WFILE_DAT: dat = base + off; datSize = size
    if ifo is None or dat is None:
        raise ValueError("BFWAV block missing")
    b = ifo + 8
    enc = r.d[b]; isLoop = r.d[b + 1]
    sr = r.u32(b + 4)
    storedLoopStart = r.u32(b + 8)
    storedLoopEnd = r.u32(b + 12)
    # snd_WaveFileReader::IsOriginalLoopAvailable(): version >= 0x00010200
    origLoopStart = r.u32(b + 16) if version >= 0x00010200 else storedLoopStart
    chnTab = b + 20
    nchn = r.u32(chnTab)
    chans = []
    for c in range(nchn):
        tid, pad, off = r.ref(chnTab + 4 + c * 8)
        chn = chnTab + off
        tid, pad, soff = r.ref(chn)          # referToSamples (DATA 块体相对)
        tid, pad, aoff = r.ref(chn + 8)      # referToAdpcmInfo (chn 相对)
        ch = {"samplesOffset": soff, "adpcm": None, "samplesAbs": dat + 8 + soff}
        if enc == WAVE_DSP and aoff:
            ada = chn + aoff
            ch["adpcm"] = {
                "coef": [r.s16(ada + i * 2) for i in range(16)],
                "predScale": r.s16(ada + 32),
                "yn1": r.s16(ada + 34),
                "yn2": r.s16(ada + 36),
                "loopPredScale": r.s16(ada + 38),
                "loopYn1": r.s16(ada + 40),
                "loopYn2": r.s16(ada + 42),
            }
        chans.append(ch)
    return {"version": version, "enc": enc, "isLoop": isLoop, "sr": sr,
            "storedLoopStart": storedLoopStart, "storedLoopEnd": storedLoopEnd,
            "origLoopStart": origLoopStart, "channels": chans,
            "dataAbs": dat, "dataSize": datSize}

# ============================================================
# Part 2 — DSP-ADPCM 编码器 (src/dsp/{encode,decode,math}.cpp 逐行移植)
# ============================================================

EPS = struct.unpack("f", struct.pack("f", 0.4999999))[0]   # C 的 0.4999999f
DBL_EPSILON = 2.2204460492503131e-16
DBL_MAX = 1.7976931348623157e308


def cdiv(a, b):
    """C 整数除法: 向零截断."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def lround(d):
    """C lround: 四舍五入, 半数远离零."""
    return math.floor(d + 0.5) if d >= 0 else math.ceil(d - 0.5)


def clamp16(v):
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def bytes_for_adpcm_buffer(samples):
    frames = samples // SPF
    if samples % SPF:
        frames += 1
    return frames * 8


def bytes_for_adpcm_samples(samples):
    extraBytes = 0
    frames = samples // SPF
    extraSamples = samples % SPF
    if extraSamples:
        extraBytes = (extraSamples // 2) + (extraSamples % 2) + 1
    return 8 * frames + extraBytes


def _pcm(rows, i):
    """pcmBuf[x] 的 C 指针语义: 行 1 的第 x 个; 负索引落入行 0 (pcmHistBuffer[2][14] 连续)."""
    return rows[1][i] if i >= 0 else rows[0][14 + i]


def inner_product_merge(rows):
    vecOut = [0.0, 0.0, 0.0]
    for i in range(3):
        acc = 0.0
        for x in range(14):
            acc -= _pcm(rows, x - i) * rows[1][x]
        vecOut[i] = acc
    return vecOut


def outer_product_merge(rows):
    mtx = [[0.0] * 3 for _ in range(3)]
    for x in range(1, 3):
        for y in range(1, 3):
            acc = 0.0
            for z in range(14):
                acc += _pcm(rows, z - x) * _pcm(rows, z - y)
            mtx[x][y] = acc
    return mtx


def analyze_ranges(mtx, vecIdxsOut):
    recips = [0.0, 0.0, 0.0]
    for x in range(1, 3):
        val = max(abs(mtx[x][1]), abs(mtx[x][2]))
        if val < DBL_EPSILON:
            return True
        recips[x] = 1.0 / val
    maxIndex = 0
    for i in range(1, 3):
        for x in range(1, i):
            tmp = mtx[x][i]
            for y in range(1, x):            # C: for (y = 1; y < x; y++)
                tmp -= mtx[x][y] * mtx[y][i]
            mtx[x][i] = tmp
        val = 0.0
        for x in range(i, 3):
            tmp = mtx[x][i]
            for y in range(1, i):            # C: for (y = 1; y < i; y++)  ← 修复(原 range(x))
                tmp -= mtx[x][y] * mtx[y][i]
            mtx[x][i] = tmp
            tmp = abs(tmp) * recips[x]
            if tmp >= val:
                val = tmp
                maxIndex = x
        if maxIndex != i:
            for y in range(1, 3):
                mtx[maxIndex][y], mtx[i][y] = mtx[i][y], mtx[maxIndex][y]
            recips[maxIndex] = recips[i]
        vecIdxsOut[i] = maxIndex
        if mtx[i][i] == 0.0:
            return True
        if i != 2:
            t = 1.0 / mtx[i][i]
            for x in range(i + 1, 3):
                mtx[x][i] *= t
    # "Get range"
    mn = 1.0e10; mx = 0.0
    for i in range(1, 3):
        tmp = abs(mtx[i][i])
        if tmp < mn: mn = tmp
        if tmp > mx: mx = tmp
    return mn / mx < 1.0e-10


def ieee_div(a, b):
    """C 语义的 double 除法: 0.0 除数不抛异常 (x/0.0→±inf, 0.0/0.0→NaN).
    与 C 编译器默认 IEEE 行为一致; 下游 finish_record 的 clamp 会吸收 inf/NaN."""
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0.0:
            return float("nan")
        return float("inf") if a > 0.0 else float("-inf")


def bidirectional_filter(mtx, vecIdxs, vecOut):
    x = 0
    for i in range(1, 3):
        index = vecIdxs[i]
        tmp = vecOut[index]
        vecOut[index] = vecOut[i]
        if x != 0:
            for y in range(x, i):
                tmp -= vecOut[y] * mtx[i][y]
        elif tmp != 0.0:
            x = i
        vecOut[i] = tmp
    for i in range(2, 0, -1):
        tmp = vecOut[i]
        for y in range(i + 1, 3):
            tmp -= vecOut[y] * mtx[i][y]
        vecOut[i] = ieee_div(tmp, mtx[i][i])
    vecOut[0] = 1.0


def quadratic_merge(v):
    v2 = v[2]
    tmp = 1.0 - v2 * v2
    if tmp == 0.0:
        return True
    v0 = (v[0] - v2 * v2) / tmp
    v1 = (v[1] - v[1] * v2) / tmp
    v[0] = v0
    v[1] = v1
    return abs(v1) > 1.0


def finish_record(inv, outv):
    """注意: C 的 tvec 参数退化指针, inv 被就地修改 (调用方的 vec1 会变化)."""
    for z in range(1, 3):
        if inv[z] >= 1.0:
            inv[z] = 0.9999999999
        elif inv[z] <= -1.0:
            inv[z] = -0.9999999999
    outv[0] = 1.0
    outv[1] = (inv[2] * inv[1]) + inv[1]
    outv[2] = inv[2]


def matrix_filter(srcv, dstv):
    mtx = [[0.0] * 3 for _ in range(3)]
    mtx[2][0] = 1.0
    for i in range(1, 3):
        mtx[2][i] = -srcv[i]
    for i in range(2, 0, -1):
        val = 1.0 - (mtx[i][i] * mtx[i][i])
        for y in range(1, i + 1):
            mtx[i - 1][y] = ieee_div((mtx[i][i] * mtx[i][y]) + mtx[i][y], val)
    dstv[0] = 1.0
    for i in range(1, 3):
        dstv[i] = 0.0
        for y in range(1, i + 1):
            dstv[i] += mtx[i][y] * dstv[i - y]


def merge_finish_record(srcv, dstv):
    val = srcv[0]
    dstv[0] = 1.0
    tmp = [0.0, 0.0, 0.0]
    for i in range(1, 3):
        v2 = 0.0
        for y in range(1, i):
            v2 += dstv[y] * srcv[i - y]
        if val > 0.0:
            dstv[i] = -(v2 + srcv[i]) / val
        else:
            dstv[i] = 0.0
        tmp[i] = dstv[i]
        for y in range(1, i):
            dstv[y] += dstv[i] * dstv[i - y]
        val *= 1.0 - (dstv[i] * dstv[i])
    finish_record(tmp, dstv)


def contrast_vectors(s1, s2):
    val = (s2[2] * s2[1] + -s2[1]) / (1.0 - s2[2] * s2[2])
    val1 = s1[0] * s1[0] + s1[1] * s1[1] + s1[2] * s1[2]
    val2 = s1[0] * s1[1] + s1[1] * s1[2]
    val3 = s1[0] * s1[2]
    return val1 + (2.0 * val * val2) + (2.0 * (-s2[1] * val + -s2[2]) * val3)


def filter_records(vecBest, exp, records, recordCount):
    buffer1 = [0] * 8
    bufferList = [[0.0] * 3 for _ in range(8)]
    for _x in range(2):
        for y in range(exp):
            buffer1[y] = 0
            for i in range(3):
                bufferList[y][i] = 0.0
        for z in range(recordCount):
            index = 0
            value = 1.0e30
            for i in range(exp):
                tempVal = contrast_vectors(vecBest[i], records[z])
                if tempVal < value:
                    value = tempVal
                    index = i
            buffer1[index] += 1
            buf2 = [0.0] * 3
            matrix_filter(records[z], buf2)
            for i in range(3):
                bufferList[index][i] += buf2[i]
        for i in range(exp):
            if buffer1[i] > 0:
                for y in range(3):
                    bufferList[i][y] /= buffer1[i]
        for i in range(exp):
            merge_finish_record(bufferList[i], vecBest[i])


def correlate_coefs(src, samples):
    """dsp/encode.cpp correlateCoefs() 的移植. 返回 16 个系数 (list[int])."""
    blockBuffer = [0] * 0x3800
    rows = [[0] * 14, [0] * 14]          # pcmHistBuffer[2][14] (连续内存语义)
    records = []
    vecBest = [[0.0, 0.0, 0.0] for _ in range(8)]
    vecIdxs = [0, 0, 0]

    sourceOff = 0
    x = samples
    while x > 0:
        if x > 0x3800:
            frameSamples = 0x3800
            x -= 0x3800
        else:
            frameSamples = x
            z = 0
            while z < 14 and z + frameSamples < 0x3800:
                blockBuffer[frameSamples + z] = 0
                z += 1
            x = 0
        blockBuffer[0:frameSamples] = src[sourceOff:sourceOff + frameSamples]
        sourceOff += frameSamples

        i = 0
        while i < frameSamples:
            rows[0] = rows[1][:]
            rows[1][:] = blockBuffer[i:i + 14]
            i += 14

            vec1 = inner_product_merge(rows)
            if abs(vec1[0]) > 10.0:
                mtx = outer_product_merge(rows)
                if not analyze_ranges(mtx, vecIdxs):
                    bidirectional_filter(mtx, vecIdxs, vec1)
                    if not quadratic_merge(vec1):
                        rec = [0.0, 0.0, 0.0]
                        finish_record(vec1, rec)
                        records.append(rec)

    vec1 = [1.0, 0.0, 0.0]
    if records:
        for z in range(len(records)):
            matrix_filter(records[z], vecBest[0])
            for y in range(1, 3):
                vec1[y] += vecBest[0][y]
        for y in range(1, 3):
            vec1[y] /= len(records)
    # (recordCount==0 → C 中为 0/0=NaN 后果未定义; 完全静音才触发, 这里保持 0)
    merge_finish_record(vec1, vecBest[0])

    exp = 1
    w = 0
    while w < 3:
        vec2 = [0.0, -1.0, 0.0]
        for i in range(exp):
            for y in range(3):
                vecBest[exp + i][y] = (0.01 * vec2[y]) + vecBest[i][y]
        w += 1
        exp = 1 << w
        filter_records(vecBest, exp, records, len(records))

    coefsOut = [0] * 16
    for z in range(8):
        d = -vecBest[z][1] * 2048.0
        coefsOut[z * 2] = 32767 if d > 32767.0 else (-32768 if d < -32768.0 else lround(d))
        d = -vecBest[z][2] * 2048.0
        coefsOut[z * 2 + 1] = 32767 if d > 32767.0 else (-32768 if d < -32768.0 else lround(d))
    return coefsOut


def dsp_encode_frame(pcmInOut, coefs):
    """dsp/encode.cpp DSPEncodeFrame(). coefs: 平铺 16 个系数 ([2i]=[i][0], [2i+1]=[i][1]).
    pcmInOut: 16 个样本(2 历史 + 14 当前), 就地更新. 返回 8 字节 adpcmOut."""
    MAXC = 8
    inSamples = [[0] * 16 for _ in range(MAXC)]
    outSamples = [[0] * 14 for _ in range(MAXC)]
    scale = [0] * MAXC
    distAccum = [0.0] * MAXC
    sampleCount = SPF

    for i in range(MAXC):
        inSamples[i][0] = pcmInOut[0]
        inSamples[i][1] = pcmInOut[1]

        distance = 0
        for s in range(sampleCount):
            v1 = cdiv((pcmInOut[s] * coefs[2 * i + 1]) + (pcmInOut[s + 1] * coefs[2 * i]), 2048)
            inSamples[i][s + 2] = v1
            v2 = pcmInOut[s + 2] - v1
            v3 = clamp16(v2)
            if abs(v3) > abs(distance):
                distance = v3

        si = 0
        while si <= 12 and (distance > 7 or distance < -8):
            si += 1
            distance = cdiv(distance, 2)
        scale[i] = -1 if si <= 1 else si - 2

        while True:
            scale[i] += 1
            distAccum[i] = 0.0
            index = 0
            for s in range(sampleCount):
                v1 = (inSamples[i][s] * coefs[2 * i + 1]) + (inSamples[i][s + 1] * coefs[2 * i])
                v2 = (pcmInOut[s + 2] << 11) - v1
                if v2 > 0:
                    v3 = int(float(v2) / (1 << scale[i]) / 2048 + EPS)
                else:
                    v3 = int(float(v2) / (1 << scale[i]) / 2048 - EPS)
                if v3 < -8:
                    v3 = -8 - v3            # if (index < (v3 = -8 - v3)) index = v3;
                    if index < v3:
                        index = v3
                    v3 = -8
                elif v3 > 7:
                    v3 = v3 - 7             # if (index < (v3 -= 7)) index = v3;
                    if index < v3:
                        index = v3
                    v3 = 7
                outSamples[i][s] = v3
                v1 = (v1 + ((v3 * (1 << scale[i])) << 11) + 1024) >> 11
                inSamples[i][s + 2] = clamp16(v1)
                v3 = pcmInOut[s + 2] - inSamples[i][s + 2]
                distAccum[i] += v3 * float(v3)
            x = index + 8
            while x > 256:
                scale[i] += 1
                if scale[i] >= 12:
                    scale[i] = 11
                x >>= 1
            if not (scale[i] < 12 and index > 1):
                break

    bestIndex = 0
    mn = DBL_MAX
    for i in range(MAXC):
        if distAccum[i] < mn:
            mn = distAccum[i]
            bestIndex = i

    for s in range(sampleCount):
        pcmInOut[s + 2] = inSamples[bestIndex][s + 2]

    adpcmOut = bytearray(8)
    adpcmOut[0] = ((bestIndex << 4) | (scale[bestIndex] & 0xF)) & 0xFF
    for s in range(sampleCount, 14):
        outSamples[bestIndex][s] = 0
    for y in range(7):
        adpcmOut[y + 1] = ((outSamples[bestIndex][y * 2] << 4) |
                           (outSamples[bestIndex][y * 2 + 1] & 0xF)) & 0xFF
    return adpcmOut


def dsp_encode(spooled, samples):
    """dsp/encode.cpp encode(). 返回 (dst bytearray, adpcmInfo dict)."""
    coefs = correlate_coefs(spooled, samples)
    frameCount = samples // SPF + (1 if samples % SPF else 0)
    dst = bytearray(bytes_for_adpcm_buffer(samples))   # 末尾 0 填充
    history = [0, 0]                                   # pcmFrame[0], pcmFrame[1]
    adpcmPos = 0
    pcmPos = 0
    for i in range(frameCount):
        sampleCount = min(samples - i * SPF, SPF)
        pcmFrame = [0] * (SPF + 2)
        pcmFrame[0], pcmFrame[1] = history[0], history[1]
        for k in range(sampleCount):
            pcmFrame[2 + k] = spooled[pcmPos + k]
        adpcmFrame = dsp_encode_frame(pcmFrame, coefs)
        n = bytes_for_adpcm_samples(sampleCount)
        dst[adpcmPos:adpcmPos + n] = adpcmFrame[0:n]
        history[0], history[1] = pcmFrame[14], pcmFrame[15]
        pcmPos += SPF
        adpcmPos += 8
    adpcmInfo = {"coef": coefs, "gain": 0,
                 "pred_scale": dst[0],          # cxt->pred_scale = *dst (u8, 不符号扩展)
                 "yn1": 0, "yn2": 0,
                 "loopPredScale": 0, "loopYn1": 0, "loopYn2": 0}
    return dst, adpcmInfo


def get_loop_context(src, cxt, samples):
    """dsp/decode.cpp getLoopContext() 的移植. 只更新 cxt 的 loop 字段."""
    hist1 = cxt["yn1"]
    hist2 = cxt["yn2"]
    coefs = cxt["coef"]
    ps = src[0]                                    # char ps — 符号!
    frameCount = (samples + SPF - 1) // SPF
    remaining = samples
    idx = 0
    for _ in range(frameCount):
        ps = src[idx]
        predictor = (src[idx] >> 4) & 0xF
        sc = 1 << (src[idx] & 0xF)
        idx += 1
        c1 = coefs[predictor * 2]
        c2 = coefs[predictor * 2 + 1]
        n = min(SPF, remaining)
        for s in range(n):
            b = src[idx]
            nib = (b >> 4) & 0xF if s % 2 == 0 else b & 0xF
            if s % 2 == 1:
                idx += 1
            nib = nib - 16 if nib >= 8 else nib
            v = (((sc * nib) << 11) + 1024 + (c1 * hist1 + c2 * hist2)) >> 11
            v = clamp16(v)
            hist2 = hist1
            hist1 = v
        remaining -= n
    cxt["loopPredScale"] = (ps - 256 if ps >= 128 else ps) & 0xFFFF   # char → 符号扩展
    cxt["loopYn1"] = hist1
    cxt["loopYn2"] = hist2


def is_original_loop_available(version):
    return version >= 0x00010200                   # WaveFile::isOriginalLoopAvailable

# ============================================================
# Part 3 — WaveFile 语义镜像 + RIFF WAV 读取
# ============================================================


class WaveFileState:
    """include/bfsar/WaveFile.h 中 loop/spool 访问器的逐一镜像."""

    cFramesAfterLoopEnd = 140
    cStreamLoopStartAlignmentFramesAdpcm = 0x3800
    cStreamMinimumLoopFrames = 0x480
    cSamplesPerFrame = SPF

    def __init__(self, enc, mIsLoop, mSampleRate, mLoopStartFrame, mLoopEndFrame):
        self.mEncoding = enc
        self.mIsLoop = mIsLoop
        self.mSampleRate = mSampleRate
        self.mLoopStartFrame = mLoopStartFrame
        self.mLoopEndFrame = mLoopEndFrame
        self.mSampleCount = mLoopEndFrame           # doRead: mSampleCount = mLoopEndFrame

    def getOriginalLoopStartFrame(self):
        return self.mLoopStartFrame if self.mIsLoop else 0

    def getOriginalLoopEndFrame(self):
        return self.mLoopEndFrame

    def getLoopFrames(self):
        return self.getOriginalLoopEndFrame() - self.getOriginalLoopStartFrame()

    def getLoopStartAlignmentFrames(self, forStream):
        if self.mEncoding == WAVE_DSP:
            return self.cStreamLoopStartAlignmentFramesAdpcm if forStream else self.cSamplesPerFrame
        if self.mEncoding == WAVE_PCM16:
            return 8192 // 2 if forStream else 0
        if self.mEncoding == WAVE_PCM8:
            return 8192 // 1 if forStream else 0
        return 0

    def getSpoolFrames(self, loopStartAlignmentFrames):
        if loopStartAlignmentFrames > 0:
            skipped = self.getOriginalLoopStartFrame() % loopStartAlignmentFrames
            if skipped > 0:
                return loopStartAlignmentFrames - skipped
        return 0

    def getLoopStartFrame(self, forStream):
        return self.getOriginalLoopStartFrame() + self.getSpoolFrames(self.getLoopStartAlignmentFrames(forStream))

    def getLoopEndFrame(self, forStream):
        loopFrames = self.getLoopFrames()
        endFrame = self.getLoopStartFrame(forStream) + loopFrames
        if forStream and self.cStreamMinimumLoopFrames > loopFrames and loopFrames != 0:
            endFrame += loopFrames * (self.cStreamMinimumLoopFrames // loopFrames)
        return endFrame


def read_riff_wav(path):
    """WaveFile::readRiffWavInfo + readWavFile 的载入部分(逐行对照).
    返回 (channels, info) 或 (None, 错误消息)."""
    try:
        with open(path, "rb") as f:
            d = f.read()
    except OSError as ex:
        return None, "Couldn't open file: %s" % ex
    if d[0:4] != b"RIFF":
        return None, "Invalid 'RIFF' header"
    if d[8:12] != b"WAVE":
        return None, "Invalid 'WAVE' header"
    if d[12:16] != b"fmt ":
        return None, "Invalid 'fmt ' header"
    u16le = lambda o: struct.unpack_from("<H", d, o)[0]
    u32le = lambda o: struct.unpack_from("<I", d, o)[0]
    if u16le(20) != 1:
        return None, "Only Pcm .wav files supported"
    numChannels = u16le(22)
    if numChannels > 2:
        return None, "Only mono and stereo .wav files supported"
    sampleRate = u32le(24)
    bits = u16le(34)
    if bits != 8 and bits != 16:
        return None, "Only Pcm8 and Pcm16 .wav files supported"
    # 块扫描: 与 TuneBloom 相同, 从 fmt 16 字节字段后(offset 36)开始, skip 不做奇数补齐
    pos = 36
    sampleBytes = 0
    dataStart = 0
    while pos + 8 <= len(d):
        tag = d[pos:pos + 4]
        csize = u32le(pos + 4)
        if tag == b"data":
            sampleBytes = csize
            dataStart = pos + 8
            break
        pos += 8 + csize
    if sampleBytes == 0:
        return None, "No 'data' block found"
    sampleCount = sampleBytes // numChannels // (bits // 8)
    raw = d[dataStart:dataStart + sampleBytes]
    if len(raw) < sampleBytes:
        return None, "data chunk 被截断"
    bsz = bits // 8
    channels = []
    for ch in range(numChannels):
        vals = []
        for s in range(sampleCount):
            off = (s * numChannels + ch) * bsz
            if bits == 8:
                v = raw[off] - 128                  # 无符号 PCM8 → 符号 s8
            else:
                v = struct.unpack_from("<h", raw, off)[0]
            vals.append(v)
        channels.append(vals)
    info = {"numChannels": numChannels, "sampleRate": sampleRate,
            "bitsPerSample": bits, "sampleBytes": sampleBytes,
            "sampleCount": sampleCount}
    return channels, info

# ============================================================
# Part 4 — convertChannel_ 导入路径 + FileWriter 镜像 + FWAV/FWAR 写出
# ============================================================


def convert_channel_import(ch, wstate, e_fmt):
    """WaveFile::convertChannel_ (from=PCM, isLoop=False) 导入路径的移植.
    ch: 该通道 s16 数值 list. e_fmt: '>' 或 '<' (BFSAR 字节序).
    返回 (该通道写入 BFWAV 的 mDataSizeMin 字节, adpcm_params 或 None)."""
    to = wstate.mEncoding
    # --- spool ---
    # framesAfterLoopEnd = (isLoop && to==DspAdpcm) ? 140 : 0 → 0 (isLoop=False)
    totalSamples = wstate.getLoopEndFrame(True)      # loopEndFrameStream
    totalSamplesMin = wstate.getLoopEndFrame(False)  # loopEndFrame (写入 BFWAV 的大小)
    copyCount = min(wstate.mSampleCount, wstate.getOriginalLoopEndFrame())
    spooled = ch[:copyCount]
    if copyCount < totalSamples:                     # else 分支: fillZero 补 0
        spooled = spooled + [0] * (totalSamples - copyCount)
    # --- encode ---
    if to == WAVE_PCM16:
        # dstData = spooledPcm; doWrite 时若 mDataEndian != mEndian 逐样本换字节序,
        # 净效果 = 以 BFSAR 字节序写出 (读取侧 RIFF 恒为小端)
        vals = spooled[:totalSamplesMin]
        data = struct.pack(e_fmt + "%dh" % len(vals), *vals)
        return data, None
    if to == WAVE_PCM8:
        vals = spooled[:totalSamplesMin]
        data = bytes((v >> 8) & 0xFF for v in vals)
        return data, None
    # --- DspAdpcm ---
    dst, ainfo = dsp_encode(spooled, totalSamples)
    get_loop_context(bytes(dst), ainfo, wstate.getLoopStartFrame(False))
    ainfoStream = dict(ainfo)
    get_loop_context(bytes(dst), ainfoStream, wstate.getLoopStartFrame(True))
    bufSizeMin = bytes_for_adpcm_buffer(totalSamplesMin)
    param = {"coef": ainfo["coef"], "predScale": ainfo["pred_scale"],
             "yn1": ainfo["yn1"], "yn2": ainfo["yn2"]}
    loopParam = {"loopPredScale": ainfoStream["loopPredScale"],
                 "loopYn1": ainfoStream["loopYn1"],
                 "loopYn2": ainfoStream["loopYn2"]}
    return bytes(dst[:bufSizeMin]), (param, loopParam)


class W:
    """src/bfsar/FileWriter.cpp 的镜像 (本脚本用到的写入路径)."""

    def __init__(self, e):
        self.d = bytearray()
        self.e = e
        self._hdrPos = None
        self._hdrSize = None
        self._blocks = []
        self._blockPos = None
        self._offsetBase = 0
        self._offsetBases = []
        self._refTables = {}
        self._sizedRefTables = {}
        self._refs = {}

    def tell(self):
        return len(self.d)

    def u8(self, v): self.d.append(v & 0xFF)
    def u16(self, v): self.d += struct.pack(self.e + "H", v & 0xFFFF)
    def u32(self, v): self.d += struct.pack(self.e + "I", v & 0xFFFFFFFF)
    def s32(self, v): self.d += struct.pack(self.e + "i", v)
    def write(self, b): self.d += b

    def openFile(self, magic, numBlocks, version):
        self._hdrPos = self.tell()
        self._blocks = []
        hdrSize = 0x14 + numBlocks * 12               # BinaryFileHeader + n*ReferenceWithSize
        hdrSize = (hdrSize + 0x1F) & ~0x1F            # roundUpPow2(x, 0x20)
        self._hdrSize = hdrSize
        self.write(magic.encode("ascii"))
        self.u16(0xFEFF)                              # Endian (按流序写出)
        self.u16(hdrSize)
        self.u32(version)
        self.u32(0)                                   # File Size, 最后回补
        self.u16(numBlocks)
        self.u16(0)                                   # Reserved
        for _ in range(numBlocks):
            self.u16(0); self.u16(0); self.s32(0); self.u32(0)
        self.write(bytes(self._hdrSize - self.tell()))

    def closeFile(self):
        size = self._hdrSize + sum(bk["size"] for bk in self._blocks)
        p = self._hdrPos + 0x0C                       # offsetof(BinaryFileHeader, fileSize)
        self.d[p:p + 4] = struct.pack(self.e + "I", size)
        p = self._hdrPos + 0x14                       # 块表
        offset = self._hdrSize
        for bk in self._blocks:
            self.d[p:p + 2] = struct.pack(self.e + "H", bk["type"]); p += 2
            self.d[p:p + 2] = struct.pack(self.e + "H", 0); p += 2
            if bk["size"] != 0:
                self.d[p:p + 4] = struct.pack(self.e + "i", offset); p += 4
                self.d[p:p + 4] = struct.pack(self.e + "I", bk["size"]); p += 4
            else:
                self.d[p:p + 4] = struct.pack(self.e + "i", -1); p += 4
                self.d[p:p + 4] = struct.pack(self.e + "I", 0xFFFFFFFF); p += 4
            offset += bk["size"]

    def openBlock(self, typeId, magic):
        self._blocks.append({"type": typeId, "size": 0})
        self._blockPos = self.tell()
        self._offsetBase = self._blockPos + 8         # BinaryBlockHeader 之后
        self.write(magic.encode("ascii"))
        self.u32(0)

    def closeBlock(self):
        blockSize = self.tell() - self._blockPos
        self.d[self._blockPos + 4:self._blockPos + 8] = struct.pack(self.e + "I", blockSize)
        self._blocks[-1]["size"] = blockSize
        self._blockPos = None

    def align(self, a):
        # 注意: 相对块头起始对齐 (与 FileWriter::align 一致)
        while (self.tell() - self._blockPos) % a != 0:
            self.u8(0)

    def pushOffsetBase(self):
        self._offsetBases.append(self._offsetBase)
        self._offsetBase = self.tell()

    def popOffsetBase(self):
        self._offsetBase = self._offsetBases.pop()

    def openReference(self, name):
        pos = self.tell()
        self.u16(0); self.u16(0); self.s32(0)
        self._refs[name] = (pos, self._offsetBase - self._blockPos)

    def closeReference(self, name, typeId, offset=None):
        pos, base = self._refs.pop(name)
        if offset is None:
            offset = self.tell() - base - self._blockPos
        self.d[pos:pos + 8] = struct.pack(self.e + "HHi", typeId & 0xFFFF, 0, offset)

    def closeNullReference(self, name):
        pos, _base = self._refs.pop(name)
        self.d[pos:pos + 8] = struct.pack(self.e + "HHi", 0, 0, -1)

    def openReferenceTable(self, name, count):
        pos = self.tell()
        self.u32(count)
        for _ in range(count):
            self.u16(0); self.u16(0); self.s32(0)
        self._refTables[name] = {"pos": pos,
                                 "offset": self._offsetBase - self._blockPos, "refs": []}

    def addReferenceTableReference(self, name, typeId):
        t = self._refTables[name]
        t["refs"].append((typeId, self.tell() - t["offset"] - self._blockPos))

    def closeReferenceTable(self, name):
        t = self._refTables.pop(name)
        p = t["pos"] + 4                              # offsetof(ReferenceTable, item)
        for typeId, off in t["refs"]:
            self.d[p:p + 8] = struct.pack(self.e + "HHi", typeId & 0xFFFF, 0, off); p += 8

    def openSizedReferenceTable(self, name, count):
        pos = self.tell()
        self.u32(count)
        for _ in range(count):
            self.u16(0); self.u16(0); self.s32(0); self.u32(0)
        self._sizedRefTables[name] = {"pos": pos,
                                      "offset": self._offsetBase - self._blockPos, "refs": []}

    def addSizedReferenceTableReference(self, name, typeId, size):
        t = self._sizedRefTables[name]
        t["refs"].append([typeId, self.tell() - t["offset"] - self._blockPos, size])

    def setSizedReferenceTableReferenceSize(self, name, size):
        t = self._sizedRefTables[name]
        t["refs"][-1][2] = size

    def closeSizedReferenceTable(self, name):
        t = self._sizedRefTables.pop(name)
        p = t["pos"] + 4                              # offsetof(ReferenceWithSizeTable, item)
        for typeId, off, size in t["refs"]:
            self.d[p:p + 12] = struct.pack(self.e + "HHiI", typeId & 0xFFFF, 0, off, size); p += 12


def build_fwav_image(e, version, wstate, ch_datas, adpcm_params):
    """WaveFile::doWrite 的忠实移植 (mUseOriginalData=false 分支).
    ch_datas: 每通道 mDataSizeMin 字节; adpcm_params: 仅 DspAdpcm — [(param,loopParam),...]."""
    enc = wstate.mEncoding
    nch = len(ch_datas)
    w = W(e)
    w.openFile("FWAV", 2, version)
    #? Info Block
    w.openBlock(ET_WFILE_IFO, "INFO")
    w.u8(enc)
    w.u8(1 if wstate.mIsLoop else 0)
    w.u16(0)                                          # Padding
    w.u32(wstate.mSampleRate)
    w.u32(wstate.getLoopStartFrame(False))
    w.u32(wstate.getLoopEndFrame(False))
    w.u32(wstate.getOriginalLoopStartFrame() if is_original_loop_available(version) else 0)
    w.pushOffsetBase()
    w.openReferenceTable("ChannelInfoTable", nch)
    for i in range(nch):
        w.addReferenceTableReference("ChannelInfoTable", ET_WFILE_CHN)
        w.pushOffsetBase()
        w.openReference("Samples%u" % i)
        w.openReference("AdpcmInfo%u" % i)
        w.u32(0)                                      # Reserved
        w.popOffsetBase()
    for i in range(nch):
        if enc == WAVE_DSP:
            w.closeReference("AdpcmInfo%u" % i, ET_CODEC_DSPADPCM_INFO)
            par, loop = adpcm_params[i]
            for j in range(8):
                w.u16(par["coef"][j * 2] & 0xFFFF)
                w.u16(par["coef"][j * 2 + 1] & 0xFFFF)
            w.u16(par["predScale"] & 0xFFFF)
            w.u16(par["yn1"] & 0xFFFF)
            w.u16(par["yn2"] & 0xFFFF)
            w.u16(loop["loopPredScale"] & 0xFFFF)
            w.u16(loop["loopYn1"] & 0xFFFF)
            w.u16(loop["loopYn2"] & 0xFFFF)
            w.u16(0)                                  # Padding
        else:
            w.closeNullReference("AdpcmInfo%u" % i)
    w.closeReferenceTable("ChannelInfoTable")
    w.popOffsetBase()
    w.align(0x20)
    w.closeBlock()
    #? Data Block
    w.openBlock(ET_WFILE_DAT, "DATA")
    dataBlockBodyPos = w.tell()
    w.align(0x20)
    for i in range(nch):
        w.align(0x20)
        w.closeReference("Samples%u" % i, ET_WFILE_DAT and ET_GENERAL_BYTESTREAM,
                         w.tell() - dataBlockBodyPos)
        w.write(ch_datas[i])
    w.closeBlock()
    w.closeFile()
    return bytes(w.d)


def build_fwar_image(e, version, wave_images):
    """BfwarFile::doWrite 的忠实移植. wave_images 顺序 = 原 FWAR WaveTable 顺序."""
    w = W(e)
    w.openFile("FWAR", 2, version)
    #? Info Block
    w.openBlock(ET_WARC_IFO, "INFO")
    w.openSizedReferenceTable("WaveTable", len(wave_images))
    w.align(0x20)
    w.closeBlock()
    #? File Block
    w.openBlock(ET_WARC_FLE, "FILE")
    w.align(0x20)
    for img in wave_images:
        w.align(0x20)
        w.addSizedReferenceTableReference("WaveTable", ET_GENERAL_BYTESTREAM, 0)
        pos = w.tell()
        w.write(img)
        w.setSizedReferenceTableReferenceSize("WaveTable", w.tell() - pos)
    w.closeSizedReferenceTable("WaveTable")
    w.closeBlock()
    w.closeFile()
    return bytes(w.d)

# ============================================================
# Part 5 — 版本清单(.txt) 与 BFSAR 重组
# ============================================================


def load_manifest(txt_path):
    """解析 bfsar_export.py 的 <set>.txt: file<TAB>size_bytes<TAB>src_data_bytes<TAB>wave_id.
    返回 (dict: 文件名 → (size_bytes, src_data_bytes, wave_id), 重复名列表)."""
    out = {}
    dups = []
    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            if name == "file":
                continue
            try:
                size = int(parts[1])
            except ValueError:
                continue
            srcb = None
            if len(parts) > 2:
                try:
                    srcb = int(parts[2])
                except ValueError:
                    pass
            wid = parts[3].strip() if len(parts) > 3 else None
            if name in out:
                dups.append(name)
                continue                       # 重复名保留第一条
            out[name] = (size, srcb, wid)
    return out, dups


def rebuild_fwar(data, b, fileId, repl_by_wave, log):
    """重打包一个被修改的 FWAR: BfwarFile::doWrite 规则 + 未修改波形按原始镜像原样复制.
    返回 (新FWAR镜像, wave数, 原始wave布局[(abs,size),...])."""
    r = b["r"]
    fentry = b["files"][fileId]
    if not (fentry["internal"] and fentry["abs"] is not None and
            fentry["size"] not in (None, 0xFFFFFFFF)):
        raise ValueError("wave archive fileId=%d 不是有效的内部文件" % fileId)
    base = fentry["abs"]
    origVersion = r.u32(base + 8)
    waves = parse_fwar(r, base)
    images = []
    for wi, (abspos, size) in enumerate(waves):
        if wi in repl_by_wave:
            images.append(repl_by_wave[wi]["img"])
        else:
            images.append(bytes(data[abspos:abspos + size]))
    if origVersion != FSAR_BFWAR_VERSION:
        log("  注意: FWAR(fileId=%d) 原头版本 0x%08X → 按 Bfsar::getVersionForBfwar() 写为 0x%08X"
            % (fileId, origVersion, FSAR_BFWAR_VERSION))
    img = build_fwar_image(r.e, FSAR_BFWAR_VERSION, images)
    return img, len(waves), waves


def rebuild_bfsar(data, b, replacement_images):
    """Bfsar::save_ 的 File Block 规则重组 BFSAR.
    replacement_images: {fileId: 新 FWAR 镜像}.
    返回 (新 BFSAR bytes, 统计 dict)."""
    r = b["r"]
    e = r.e
    hdrSize = r.u16(6)
    numBlocks = r.u16(0x10)
    if numBlocks != 3:
        raise ValueError("FSAR 头 dataBlocks=%d != 3, 与 TuneBloom 的 BLOCK_SIZE 不符" % numBlocks)
    if hdrSize != 0x40:
        raise ValueError("FSAR 头 headerSize=0x%X, 非标准布局" % hdrSize)
    t0 = r.refws(0x14)
    t1 = r.refws(0x14 + 12)
    t2 = r.refws(0x14 + 24)
    if not (t0[0] == ET_SAR_STR and t1[0] == ET_SAR_IFO and t2[0] == ET_SAR_FLE):
        raise ValueError("FSAR 块表顺序不是 STRG/INFO/FILE")
    strOff, strSize = t0[2], t0[3]
    infoOff, infoSize = t1[2], t1[3]
    fileOff, fileSize = t2[2], t2[3]
    if not (strOff == hdrSize and infoOff == strOff + strSize and fileOff == infoOff + infoSize):
        raise ValueError("FSAR 块不是顺序紧凑排列 (非 TuneBloom/标准打包)")
    if fileOff + fileSize > len(data):
        raise ValueError("FSAR 文件被截断")

    # 参与 FILE 块布局的内部文件
    internal_entries = [fe for fe in b["files"]
                        if fe["internal"] and fe["fo"] is not None
                        and fe["abs"] is not None
                        and fe["size"] not in (None, 0xFFFFFFFF)]

    # 嵌入拷贝检测: 其镜像区域完整落在另一内部文件镜像内
    # (对应 TuneBloom 保存时 includeInBfsar=false、bytes 被写进 group 文件镜像的文件;
    #  该类文件不单独重写, 其 INFO 引用 patch 到 "宿主新位置 + 原内部偏移")
    containers = {}
    for fe in internal_entries:
        for other in internal_entries:
            if other["idx"] == fe["idx"]:
                continue
            if (other["abs"] <= fe["abs"] and
                    fe["abs"] + fe["size"] <= other["abs"] + other["size"]):
                containers[fe["idx"]] = other["idx"]
                break

    # ---- FILE 块重建 ----
    body = bytearray()
    body += b"FILE"
    body += struct.pack(e + "I", 0)               # size 稍后回补
    cur = 8
    while cur % 0x20:
        body += b"\x00"; cur += 1                 # writer.align(0x20)
    placed = {}                                   # fi -> (bodyRel, size) 相对 FILE 块体
    for fe in internal_entries:
        fi = fe["idx"]
        # 嵌入且未被替换 → 不独立写出 (随宿主镜像整体移动)
        # 嵌入但被替换   → 独立写出 (group 镜像内残留旧拷贝不再被 FILE 引用, 无害)
        if fi in containers and fi not in replacement_images:
            continue
        img = replacement_images.get(fi)
        if img is None:
            img = bytes(data[fe["abs"]:fe["abs"] + fe["size"]])
        origPos = cur
        while cur % 0x20:
            body += b"\x00"; cur += 1             # 每个文件前 writer.align(0x20)
        dummy = origPos                           # (对齐可能已前移, 不影响)
        bodyRel = cur - 8
        body += img
        cur += len(img)
        placed[fi] = (bodyRel, len(img))

    # 嵌入文件的引用目标 = 宿主新位置 + 原内部偏移
    ref_patch = {}                                # fi -> (bodyRel, size)
    for fi, (rel, size) in placed.items():
        ref_patch[fi] = (rel, size)
    for fe in internal_entries:
        fi = fe["idx"]
        if fi in placed:
            continue
        cont = containers.get(fi)
        if cont is None or cont not in placed:
            # 无有效宿主 (理论上不应发生) → 保守起见独立写出
            img = replacement_images.get(fi) or bytes(data[fe["abs"]:fe["abs"] + fe["size"]])
            while cur % 0x20:
                body += b"\x00"; cur += 1
            rel = cur - 8
            body += img
            cur += len(img)
            placed[fi] = (rel, len(img))
            ref_patch[fi] = (rel, len(img))
        else:
            inner = fe["abs"] - b["files"][cont]["abs"]
            rel = placed[cont][0] + inner
            ref_patch[fi] = (rel, fe["size"])
    struct.pack_into(e + "I", body, 4, cur)       # FILE 块大小 (含 8 字节块头)

    # ---- 输出拼装 ----
    out = bytearray(data[0:hdrSize])
    newFileTotal = fileOff + cur
    struct.pack_into(e + "I", out, 0x0C, newFileTotal)      # 补丁 1: fileSize @0x0C
    struct.pack_into(e + "I", out, 0x14 + 2 * 12 + 8, cur)  # 补丁 2: 块表中 FILE 的 size
    infoRegion = bytearray(data[hdrSize:fileOff])
    for fi, (rel, size) in ref_patch.items():
        fe = b["files"][fi]
        p = (fe["fo"] + fe["loff"]) - hdrSize     # InternalFileInfo sized-ref 绝对位置
        struct.pack_into(e + "i", infoRegion, p + 4, rel)
        struct.pack_into(e + "I", infoRegion, p + 8, size)
    out += infoRegion
    out += body
    if len(out) != newFileTotal:
        raise AssertionError("重组长度不一致")
    return bytes(out), {"oldFileSize": r.u32(0x0C), "newFileSize": newFileTotal,
                        "fileOff": fileOff, "oldFileBlockSize": fileSize,
                        "newFileBlockSize": cur}

# ============================================================
# Part 6 — 主流程
# ============================================================


def human(n):
    return "{:,}".format(n)


def parse_selection(sel, count):
    """与 bfsar_export.py 相同的选择语法: '1,2,3' / '1-5' / 'all'."""
    picked = []
    if sel.lower() == "all":
        return list(range(1, count + 1))
    for p in sel.split(","):
        p = p.strip()
        try:
            if "-" in p:
                a, c = p.split("-")
                picked += list(range(int(a), int(c) + 1))
            elif p:
                picked.append(int(p))
        except ValueError:
            print("skip: 无法解析的选择项 '%s'" % p)
    return picked


ENC_NAMES = {0: "Pcm8", 1: "Pcm16", 2: "DspAdpcm"}
ENC_CLI = {"pcm8": 0, "pcm16": 1, "dspadpcm": 2, "keep": None}


def ask_encoding(cli_val):
    if cli_val is not None or cli_val == "keep":
        return ENC_CLI.get(cli_val)
    print()
    print("Target Encoding (与 TuneBloom Replace 对话框一致):")
    print("  [0] Pcm8   [1] Pcm16   [2] DspAdpcm   [3] 保持每条 wave 原编码")
    v = input("Select (Enter = 2/DspAdpcm): ").strip()
    if v == "":
        return 2
    if v == "3":
        return None
    if v in ("0", "1", "2"):
        return int(v)
    print("无效选择, 使用默认 DspAdpcm")
    return 2


def main():
    args = sys.argv[1:]
    pos = []
    enc_cli = None
    outfile = None
    assume_yes = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__); return
        if a in ("-e", "--encoding"):
            i += 1
            if i >= len(args):
                print("-e 缺少参数"); return
            enc_cli = args[i].lower()
            if enc_cli not in ENC_CLI:
                print("--encoding 仅支持 pcm8|pcm16|dspadpcm|keep"); return
        elif a in ("-o", "--output"):
            i += 1
            if i >= len(args):
                print("-o 缺少参数"); return
            outfile = args[i]
        elif a in ("-y", "--yes"):
            assume_yes = True
        elif a.startswith("-"):
            print("未知参数: %s" % a); return
        else:
            pos.append(a)
        i += 1

    if not pos:
        print("usage: python bfsar_import.py <file.bfsar> [export目录] [输出.bfsar]"
              " [-e pcm8|pcm16|dspadpcm|keep] [-o 输出.bfsar] [-y]")
        return
    bfsar_path = pos[0]
    exportdir = pos[1] if len(pos) > 1 else "export"
    if outfile is None and len(pos) > 2:
        outfile = pos[2]
    if outfile is None:
        base, ext = os.path.splitext(bfsar_path)
        outfile = base + ".imported" + (ext if ext else ".bfsar")

    # ---- 读取 BFSAR ----
    if not os.path.isfile(bfsar_path):
        print("找不到 BFSAR: %s" % bfsar_path); return
    with open(bfsar_path, "rb") as f:
        data = f.read()
    b = load_bfsar(data)
    r = b["r"]
    fsarVersion = r.u32(8)
    fwavVersion = 0x00010200 if fsarVersion >= 0x00020200 else (
        0x00010100 if fsarVersion >= 0x00020000 else 0x00010000)   # Bfsar::getVersionForBfwav()
    aw, flat = build_flat_wave_map(b)
    print("BFSAR: %s  (version 0x%08X, %s 字节序, BFSAR→FWAV 版本 0x%08X)"
          % (bfsar_path, fsarVersion, "大端" if r.e == EB else "小端", fwavVersion))

    # ---- export 目录 / wave set 文件夹 (先检测目录是否存在) ----
    if not os.path.isdir(exportdir):
        print("错误: 导入目录不存在: %s" % exportdir); return
    set_folders = sorted(dname for dname in os.listdir(exportdir)
                         if os.path.isdir(os.path.join(exportdir, dname))
                         and os.path.isfile(os.path.join(exportdir, dname, dname + ".txt")))
    if not set_folders:
        print("错误: %s 下没有包含 <set 名>.txt 清单的 wave set 文件夹" % exportdir)
        print("      (目录结构应为 bfsar_export.py 的输出: export/<set 名>/*.wav + <set 名>.txt)")
        return
    wave_sets = {s["name"]: s for s in b["sets"] if s["wave"]}
    print("\n-- 检测到 Wave Sound Set 文件夹 --")
    for i2, name in enumerate(set_folders, 1):
        mark = "" if name in wave_sets else "  (不在当前 BFSAR 中!)"
        print("  [%d] %s%s" % (i2, name, mark))

    if len(set_folders) == 1:
        chosen_names = [set_folders[0]]
        print("只有一个 wave set, 自动选择: %s" % chosen_names[0])
    else:
        sel = input("\nSelect Wave Sound Sets (e.g. 1,2,3 / 1-5 / 'all'): ").strip()
        chosen = parse_selection(sel, len(set_folders))
        if not chosen:
            print("未选择任何 wave set"); return
        chosen_names = []
        for c in chosen:
            if 1 <= c <= len(set_folders):
                chosen_names.append(set_folders[c - 1])
            else:
                print("skip: 无效选择 %d" % c)

    # ---- 编码 ----
    enc_choice = ask_encoding(enc_cli)

    # ---- 确认输出文件 ----
    if os.path.exists(outfile) and not assume_yes:
        tip = " (将覆盖导入源 BFSAR 本体!)" if os.path.abspath(outfile) == os.path.abspath(bfsar_path) else ""
        yn = input('输出 "%s" 已存在%s, 覆盖? [y/N]: ' % (outfile, tip))
        if yn.strip().lower() != "y":
            print("已取消"); return

    # ---- 逐 set 导入 (以 .txt 清单为准) ----
    replacements = {}       # (fileId, waveIdx) -> meta
    total_imported = 0
    total_skipped = 0

    for setNo, name in enumerate(chosen_names, 1):
        folder = os.path.join(exportdir, name)
        if name not in wave_sets:
            print("\n错误: 文件夹 '%s' 不是当前 BFSAR 的 wave sound set 名称" % name)
            total_skipped += 1
            continue
        if not os.path.isdir(folder):
            print("\n错误: wave set 文件夹不存在: %s" % folder)
            total_skipped += 1
            continue
        s = wave_sets[name]
        txt_path = os.path.join(folder, name + ".txt")
        if not os.path.isfile(txt_path):
            print("\n错误: '%s' 缺少 %s.txt (大小清单必须由 bfsar_export.py 生成)" % (folder, name))
            total_skipped += 1
            continue
        manifest, dups = load_manifest(txt_path)
        if dups:
            print("\n注意: %s.txt 中存在重复文件名: %s (仅保留首次记录)" % (name, ", ".join(sorted(set(dups)))))
        if not manifest:
            print("\n'%s'.txt 中没有任何记录, 跳过该 set" % name)
            total_skipped += 1
            continue

        # 与导出脚本一致的 sound 枚举
        snds = [so for si, so in enumerate(b["sounds"])
                if s["start"] <= si <= s["end"] and so["type"] == "wave"]
        expect = {}
        for so in snds:
            expect.setdefault(so["name"] + ".wav", []).append(so)

        files_present = set(fn for fn in os.listdir(folder) if fn.lower().endswith(".wav"))
        # (规则) 文件夹里有、但 .txt 没记录的 → 提示后忽略, 不导入
        ignored = sorted(files_present - set(manifest.keys()))
        for fn in ignored:
            print("忽略: %s 不在 %s.txt 清单中, 按要求不处理" % (fn, name))

        todo_names = [fn for fn in manifest.keys() if fn not in ignored]
        print("\n-- Importing wave set [%d/%d] '%s' (%d 个 .txt 记录, %d 个文件夹 wav 将被忽略) --"
              % (setNo, len(chosen_names), name, len(todo_names), len(ignored)))

        done = 0
        for n, fn in enumerate(todo_names, 1):
            old_size, src_bytes, manifest_wid = manifest[fn]
            path = os.path.join(folder, fn)
            tag = "[set %d/%d | %d/%d]" % (setNo, len(chosen_names), n, max(1, len(todo_names)))
            # 1) .txt 记录的文件必须在文件夹中真实存在
            if not os.path.isfile(path):
                print("%s 跳过 %s: .txt 有记录但文件夹中缺少该文件" % (tag, fn))
                done += 1; total_skipped += 1
                continue
            # 2) .txt 记录必须能对上 set 中的 sound
            so_list = expect.get(fn, [])
            if not so_list:
                print("%s 跳过 %s: .txt 有记录但当前 BFSAR 的 set '%s' 中不存在同名 sound"
                      % (tag, fn, name))
                done += 1; total_skipped += 1
                continue
            # 3) 大小核对 (.txt size_bytes)
            cur_size = os.path.getsize(path)
            if old_size > 0 and cur_size > old_size:
                pct = (cur_size - old_size) / old_size * 100.0
                print("%s 警告: %s 大小超过原始导出 %.2f%% (%s → %s bytes), 按要求仍继续导入"
                      % (tag, fn, pct, human(old_size), human(cur_size)))
            # 4) WAV 解析 (WaveFile::readRiffWavInfo)
            channels, info = read_riff_wav(path)
            if channels is None:
                print("%s 跳过 %s: %s" % (tag, fn, info))
                done += 1; total_skipped += 1
                continue
            imported_any = False
            # 5) 逐个同名 sound 导入 (通常 1 个; 同名 sound 共享同一 wav)
            for so in so_list:
                target = resolve_wave_target(b, so, flat, aw)
                if target is None:
                    print("%s 跳过 %s (sound '%s'): 无法定位 wave" % (tag, fn, so["name"]))
                    total_skipped += 1
                    continue
                fileId, waveIdx = target
                if not (b["files"][fileId]["internal"] and b["files"][fileId]["abs"] is not None):
                    print("%s 跳过 %s: 目标 wave archive 是外部文件" % (tag, fn))
                    total_skipped += 1
                    continue
                base = b["files"][fileId]["abs"]
                fwar_waves = parse_fwar(r, base)
                if waveIdx >= len(fwar_waves):
                    print("%s 跳过 %s: wave 索引越界" % (tag, fn))
                    total_skipped += 1
                    continue
                orig_abs, orig_size = fwar_waves[waveIdx]
                try:
                    orig = parse_bfwav_lite(r, orig_abs)
                except Exception as ex:
                    print("%s 跳过 %s: 原始 BFWAV 解析失败 (%s)" % (tag, fn, ex))
                    total_skipped += 1
                    continue
                # 6) 信息性提示 (不阻断; 与 TuneBloom Replace 语义一致)
                if info["sampleRate"] != orig["sr"]:
                    print("%s 注意: %s 采样率 %d → %d (与 TuneBloom 一致, 以 wav 为准)"
                          % (tag, fn, orig["sr"], info["sampleRate"]))
                if info["numChannels"] != len(orig["channels"]):
                    print("%s 注意: %s 声道数 %d → %d"
                          % (tag, fn, len(orig["channels"]), info["numChannels"]))
                to_enc = enc_choice if enc_choice is not None else orig["enc"]
                if to_enc != orig["enc"]:
                    print("%s 注意: %s 编码 %s → %s (改变编码可能损失音质)"
                          % (tag, fn, ENC_NAMES.get(orig["enc"], "?"), ENC_NAMES.get(to_enc, "?")))
                if orig["isLoop"]:
                    print("%s 注意: %s 原波形有 loop, 按 TuneBloom 导入语义 loop 将被重置为不循环"
                          % (tag, fn))
                # 7) readWavFile + convertChannel_ (isLoop=False)
                wstate = WaveFileState(to_enc, False, info["sampleRate"], 0, info["sampleCount"])
                ch_datas = []
                params = [] if to_enc == WAVE_DSP else None
                ok = True
                for ch in channels:
                    try:
                        cb, par = convert_channel_import(ch, wstate, r.e)
                    except Exception as ex:
                        print("%s 跳过 %s: 编码失败 (%s)" % (tag, fn, ex))
                        ok = False; break
                    ch_datas.append(cb)
                    if to_enc == WAVE_DSP:
                        params.append(par)
                if not ok:
                    total_skipped += 1
                    continue
                img = build_fwav_image(r.e, fwavVersion, wstate, ch_datas, params)
                # 编码后体积对比原始编码体积: BFSAR 是整包进内存的, 单条明显变大
                # (例如 TTS 时长比原音长) 会推高整包体积, 累积过多可能导致游戏崩溃
                if src_bytes:
                    delta = len(img) - src_bytes
                    if delta > 0:
                        print("%s 警告: %s 编码后体积 %s → %s (+%.1f%%, 原始编码体积来自 .txt 的 "
                              "src_data_bytes) — 单条变大将推高 BFSAR 体积"
                              % (tag, fn, human(src_bytes), human(len(img)),
                                 delta * 100.0 / src_bytes))
                key = (fileId, waveIdx)
                if key in replacements:
                    print("%s 警告: wave (fileId=%d, waveIdx=%d) 已被 '%s' 导入过, 现被 '%s' 覆盖"
                          % (tag, fn, fileId, waveIdx, replacements[key]["name"], fn))
                replacements[key] = {"img": img, "name": fn, "enc": to_enc,
                                     "sr": info["sampleRate"], "count": info["sampleCount"]}
                print("%s 导入 %s → warc(fileId=%d) wave#%d  [%s / %d Hz / %d 样本 / %s bytes]"
                      % (tag, fn, fileId, waveIdx, ENC_NAMES.get(to_enc, "?"),
                         info["sampleRate"], info["sampleCount"], human(len(img))))
                imported_any = True
            if imported_any:
                total_imported += 1
            done += 1
        print("Wave set '%s' 完成: 文件处理 %d, 累计导入 %d, 累计跳过 %d"
              % (name, done, total_imported, total_skipped))

    if not replacements:
        print("\n没有任何 wave 被导入, 未写出文件。")
        return

     # ---- 重建受影响的 FWAR 镜像 ----
    grouped = {}
    for (fid, wi), meta in replacements.items():
        grouped.setdefault(fid, {})[wi] = meta
    replacement_images = {}
    orig_waves = {}      # fileId -> 原始 [(abs,size),...]
    for fileId in sorted(grouped.keys()):
        by_wave = grouped[fileId]
        img, wave_count, waves0 = rebuild_fwar(data, b, fileId, by_wave, print)
        replacement_images[fileId] = img
        orig_waves[fileId] = waves0
        print("重建 FWAR (fileId=%d): %d 条 wave, 其中 %d 条已替换, 镜像 %s → %s bytes"
              % (fileId, wave_count, len(by_wave),
                 human(b["files"][fileId]["size"] or 0), human(len(img))))

    # ---- 重组 BFSAR ----
    try:
        out, st = rebuild_bfsar(data, b, replacement_images)
    except (ValueError, AssertionError) as ex:
        print("\n错误: %s" % ex)
        print("安全中止, 未写出文件。")
        return

    # ---- 自检: 重新解析输出, 用补丁后的"新"位置做比对 ----
    try:
        b2 = load_bfsar(out)
        r2 = b2["r"]
        # 1) 被替换的 FWAR: 尺寸/位置来自重新解析, 每条 wave 核对元数据与未替换 wave 的字节
        for fileId, img in replacement_images.items():
            fe2 = b2["files"][fileId]
            if fe2["abs"] is None or fe2["size"] != len(img):
                raise AssertionError("fileId=%d 重新解析的尺寸/偏移不一致" % fileId)
            waves2 = parse_fwar(r2, fe2["abs"])
            if len(waves2) != len(orig_waves[fileId]):
                raise AssertionError("fileId=%d wave 数量不一致" % fileId)
            for wi, (abs2, size2) in enumerate(waves2):
                meta = grouped[fileId].get(wi)
                if meta is not None:
                    nf = parse_bfwav_lite(r2, abs2)
                    if (nf["enc"] != meta["enc"] or nf["sr"] != meta["sr"]
                            or nf["storedLoopEnd"] != meta["count"] or nf["isLoop"]):
                        raise AssertionError("wave(fileId=%d,#%d) 元数据不一致" % (fileId, wi))
                else:
                    oabs, osize = orig_waves[fileId][wi]
                    if bytes(out[abs2:abs2 + size2]) != bytes(data[oabs:oabs + osize]):
                        raise AssertionError("wave(fileId=%d,#%d) 未修改却被改动" % (fileId, wi))
        # 2) 其余全部内部文件: 内容必须与原文件一致 (位置 = 重新解析后的新位置)
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
    print("  (可用 bfsar_export.py 对输出文件重新导出, 与本目录 wav 做听感/交叉验证)")
    print("==================================================")


if __name__ == "__main__":
    main()