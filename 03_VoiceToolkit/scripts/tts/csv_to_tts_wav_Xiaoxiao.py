#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
csv_to_tts_wav.py —— 批量：目录下全部 .csv 的 C 列(Chinese) 经 edge-tts 合成 wav，
文件名取 A 列(Filename)，输出到与 csv 同名的子目录。
每行：Chinese -> 临时txt -> edge-tts --file 合成 mp3 -> sox 转换 + 效果链 -> 删临时文件

★本次修改（对齐原游戏导出参数、缩减体积）：
  输出采样率 48000Hz -> 22050Hz（--samplerate 可覆盖）。WAV 总码率 = 采样率×位深×声道，
  48000×16×1=768kbps（约370KB/4秒），22050×16×1=353kbps（约170KB/4秒），与游戏原文件一致。
  edge-tts 的 mp3 源即 24kHz，降到 22050 无明显损失；此前 48k 反而是无谓上采样。
  另可用 --trim 修剪 edge-tts 首尾静音垫，使时长更接近原文件（默认关，防切柔音头）。

依赖（Windows / Linux 通用，脚本只用标准库，靠外部命令 edge-tts 与 sox）：
  * Python 3.8+            Windows: python.org 安装包（勾选 Add python.exe to PATH）
  * edge-tts  pip install edge-tts   （或用 pipx；会自动装到 PATH）
  * sox        Windows 无官方安装包，需手动放好 sox.exe 并加入 PATH：
               1) 下载 sox-14.4.2-win32.zip（SourceForge: sox / files）
               2) 解压到任意目录（例如 tools\sox\），把该目录加入系统 PATH
                  （或直接把 sox.exe 放到本脚本同目录 / 同目录下的 tools\ 里，脚本会自动找到）
               Linux: sudo apt install sox libsox-fmt-all
  * 未装 Python 的机器：先装 Python，再 `py -m pip install --upgrade edge-tts`

用法：
  python csv_to_tts_wav.py                      # 当前目录全部 .csv（先自检再批量）
  python csv_to_tts_wav.py xxx.csv --rate "+8%" --pitch "+3Hz"
  python csv_to_tts_wav.py --overwrite --trim   # 已有 wav 重做；并首尾静音
  python csv_to_tts_wav.py --proxy http://127.0.0.1:7890   # 需要代理时（大陆网络常见）
  python csv_to_tts_wav.py --dir path/to/csv_dir   # 指定 csv 所在目录
"""

import argparse
import csv
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Windows 控制台默认 cp936，打印 ✅/★ 之类会 UnicodeEncodeError —— 统一切 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------- 默认参数 ----------------
VOICE_DEFAULT   = "zh-CN-XiaoxiaoNeural"
RATE_DEFAULT    = "+0%"
PITCH_DEFAULT   = "+5Hz"
VOLUME_DEFAULT  = "+30%"
SAMPLERATE_DEF  = 48000          # ★原游戏导出参数：22050Hz/16bit/mono（48000×16≈768kbps -> 22050×16≈353kbps）
BITRATE_BITS    = "16"
CHANNELS        = "1"
SLEEP_SEC       = 0.5
RETRIES         = 3

SOX_EFFECTS = [
    "remix", "1",
    "compand", "0.02,0.2", "-60,-60,-30,-18,-12,-4,0,-1", "-3", "-12", "0.05",
    "treble", "+3",
    "norm", "-1",
]
SOX_TRIM = ["silence", "1", "0.02", "0.5%", "reverse",
            "silence", "1", "0.02", "0.5%", "reverse"]

def _tool_candidates(name):
    """外部命令的候选路径：脚本目录、tools 子目录、Windows 常见安装位置。"""
    exe = name + (".exe" if os.name == "nt" else "")
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, exe), os.path.join(here, "tools", exe)]
    if os.name == "nt":
        cands += sorted(glob.glob(r"C://Program Files*//sox*//sox.exe"))
        cands += sorted(glob.glob(r"C://Program Files*//SoX*//sox.exe"))
        cands.append(r"C://ProgramData//chocolatey//bin//sox.exe")
    return cands


def find_tool(name):
    """返回可直接执行的 argv 前缀；找不到返回 None。"""
    p = shutil.which(name)
    if p:
        return [p]
    for c in _tool_candidates(name):
        if os.path.isfile(c):
            return [c]
    return None


def _edge_module_ok():
    """没有 edge-tts 命令时，退而用 `python -m edge_tts`：这里判断模块是否装了。"""
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


EDGE_BIN = find_tool("edge-tts") or [sys.executable, "-m", "edge_tts"]
SOX_BIN = find_tool("sox")

FILENAME_HEADERS = {"filename", "文件名"}
CHINESE_HEADERS = {"chinese", "中文", "zh", "cn"}
SKIP_FILES = {"duplicate_sentences_summary.csv"}
MIN_WAV_BYTES = 1024


def read_text_auto(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def col_index(headers, wanted, fallback, csv_name, label):
    lowered = [(h or "").strip().lower() for h in headers]
    for i, h in enumerate(lowered):
        if h in wanted:
            return i
    if fallback < len(headers):
        print(f"  [提示] {csv_name}：未找到「{label}」表头，退回用第 {fallback + 1} 列")
        return fallback
    return None


# ---------------- 诊断相关 ----------------
def summarize_err(stderr, stdout="", keep=3):
    lines = [l.strip() for l in ((stderr or "") + "\n" + (stdout or "")).splitlines() if l.strip()]
    key = [l for l in lines if re.search(
        r"(Error|Exception|error:|403|forbidden|denied|connect|SSL|cert|timeout|"
        r"unreachable|resolve|audio|gec|token)", l, re.I)]
    pool = key if key else lines
    out, seen = [], set()
    for l in pool:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return " | ".join(out[-keep:])[:400]


def failure_hint(msg):
    m = (msg or "").lower()
    if any(k in m for k in ("noaudioreceived", "403", "forbidden", "gec", "token", "unauthorized")):
        return ("多为 edge-tts 版本过旧被微软接口拒绝 → pipx upgrade edge-tts（或 reinstall）后重试；"
                "若仍 403 请校准系统时间（timedatectl 开 NTP）。")
    if any(k in m for k in ("ssl", "certificate", "cert")):
        return ("SSL/证书问题 → sudo apt install --reinstall ca-certificates，并校准系统时间。")
    if any(k in m for k in ("connect", "network", "unreachable", "timeout", "resolve",
                            "temporary failure", "getaddrinfo")):
        return ("连不上微软语音服务 → 检查网络/防火墙；需要代理时加：--proxy http://127.0.0.1:7890。")
    return "手动测试：edge-tts --text 你好 --voice zh-CN-XiaoxiaoNeural --write-media /tmp/t.mp3"


def cli_version():
    try:
        r = subprocess.run(EDGE_BIN + ["--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return (r.stdout or r.stderr or "").strip() or "未知"
    except Exception:
        return "未知"


def cli_caps():
    try:
        r = subprocess.run(EDGE_BIN + ["--help"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        h = (r.stdout or "") + (r.stderr or "")
        return {"proxy": "--proxy" in h, "connect_timeout": "--connect-timeout" in h}
    except Exception:
        return {"proxy": False, "connect_timeout": False}


def edge_cmd(out_mp3, tmp_txt, args):
    cmd = EDGE_BIN + ["--file", tmp_txt,
           "--voice", args.voice,
           f"--rate={args.rate}", f"--pitch={args.pitch}", f"--volume={args.volume}",
           "--write-media", out_mp3]
    caps = getattr(args, "_caps", {})
    if args.proxy and caps.get("proxy"):
        cmd += ["--proxy", args.proxy]
    if caps.get("connect_timeout"):
        cmd += ["--connect-timeout", str(args.connect_timeout)]
    return cmd


# ---------------- 单条合成 ----------------
def edge_synth(text, out_mp3, args):
    tmp_txt = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                         delete=False) as tf:
            tf.write(text)
            tmp_txt = tf.name
        r = subprocess.run(edge_cmd(out_mp3, tmp_txt, args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return summarize_err(r.stderr, r.stdout), (r.stderr or "")
        if not (os.path.isfile(out_mp3) and os.path.getsize(out_mp3) > 0):
            return "edge-tts 退出码 0 但 mp3 为空/缺失（多半被服务拒绝返回空音频）", ""
        return None, ""
    except FileNotFoundError:
        return "找不到 edge-tts 命令（pipx install edge-tts）", ""
    finally:
        if tmp_txt and os.path.exists(tmp_txt):
            try:
                os.unlink(tmp_txt)
            except OSError:
                pass


def sox_to_wav(mp3, wav, args, trim=False):
    """输出格式对齐原游戏文件：16bit/单声道/22050Hz（PCM 码率=采样率×位深×声道）。"""
    out_opts = ["-b", str(args.bits), "-c", str(args.channels), "-r", str(args.samplerate)]
    try:
        subprocess.run(SOX_BIN + [mp3] + out_opts + [wav] + SOX_EFFECTS,
                       check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "找不到 sox 命令（Ubuntu/Debian: sudo apt install sox libsox-fmt-all）"
    except subprocess.CalledProcessError as e:
        return f"sox 失败: {summarize_err(e.stderr)}"
    if trim:
        tmp = wav + ".trim.wav"
        subprocess.run(SOX_BIN + [wav, tmp] + SOX_TRIM,
                       check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
        os.replace(tmp, wav)
    return None


def wav_done(path):
    try:
        return os.path.getsize(path) > MIN_WAV_BYTES
    except OSError:
        return False


def clean_file(path):
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def selftest(args):
    with tempfile.TemporaryDirectory() as td:
        mp3 = os.path.join(td, "selftest.mp3")
        wav = os.path.join(td, "selftest.wav")
        err, full_stderr = edge_synth("你好，这是一次连接测试。", mp3, args)
        if err:
            print(f"❌ 自检失败（edge-tts）：{err}")
            if full_stderr:
                tail = [l for l in full_stderr.splitlines() if l.strip()][-15:]
                print("--- edge-tts 完整错误（尾部15行）---")
                print("\n".join(tail))
                print("------------------------------------")
            print(f"💡 建议：{failure_hint(err)}")
            return False
        err = sox_to_wav(mp3, wav, args)
        if err:
            print(f"❌ 自检失败（sox）：{err}")
            return False
    print("✅ 自检通过：edge-tts 与 sox 均可用，开始批量处理")
    return True


def synth_row(text, out_dir, base_name, args):
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, base_name + ".mp3")
    wav = os.path.join(out_dir, base_name + ".wav")
    err = None
    for attempt in range(1, args.retries + 1):
        err, _full = edge_synth(text, mp3, args)
        if err is None:
            break
        print(f"    [WARN] {base_name} 第{attempt}次合成失败: {err}")
        if attempt < args.retries:
            time.sleep(1.0 * attempt)
    if err is not None:
        clean_file(mp3)
        return False
    err = sox_to_wav(mp3, wav, args, trim=args.trim)
    if err:
        print(f"    [WARN] {base_name} sox 处理失败: {err}")
        clean_file(mp3)
        return False
    if not args.keep_mp3:
        clean_file(mp3)
    return True


def collect_csvs(names, base_dir):
    if not names:
        out = []
        for n in sorted(os.listdir(base_dir)):
            if not n.lower().endswith(".csv") or n.startswith("~$") or n.startswith("."):
                continue
            if n in SKIP_FILES:
                print(f"[跳过] 汇总文件：{n}")
                continue
            out.append(os.path.join(base_dir, n))
        return out
    out, seen = [], set()
    for n in names:
        p = n if os.path.isabs(n) or os.path.dirname(n) else os.path.join(base_dir, n)
        if os.path.isdir(p):
            out.extend(collect_csvs([], p))
        elif os.path.isfile(p):
            key = os.path.realpath(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
        else:
            print(f"[跳过] 不存在：{n}")
    return out


def process_csv(csv_path, out_dir, args, targets):
    reader = csv.DictReader(io.StringIO(read_text_auto(csv_path), newline=""))
    if not reader.fieldnames:
        print("  [跳过] 无表头")
        return None
    fi = col_index(reader.fieldnames, FILENAME_HEADERS, 0, csv_path, "Filename(A列)")
    ci = col_index(reader.fieldnames, CHINESE_HEADERS, 2, csv_path, "Chinese(C列)")
    if fi is None or ci is None:
        print(f"  [跳过] 列数不足无法定位 Filename/Chinese：{reader.fieldnames}")
        return None
    name_col, cn_col = reader.fieldnames[fi], reader.fieldnames[ci]

    made = failed = skip_done = skip_empty = 0
    for row in reader:
        name = (row.get(name_col) or "").strip()
        text = (row.get(cn_col) or "").strip()
        if not name or not text:
            skip_empty += 1
            continue
        base_name = os.path.splitext(name)[0].strip().replace("/", "_").replace("\\", "_")
        if not base_name:
            skip_empty += 1
            continue
        wav = os.path.join(out_dir, base_name + ".wav")

        if not args.overwrite and wav_done(wav):
            skip_done += 1
            continue

        key = os.path.realpath(wav)
        if key in targets and targets[key] != text:
            print(f"    [WARN] 文件名冲突（同名但中文不同），将重新合成覆盖: {base_name}.wav")
        targets[key] = text

        if synth_row(text, out_dir, base_name, args):
            made += 1
            time.sleep(args.sleep)
        else:
            failed += 1

    return {"made": made, "failed": failed,
            "skip_done": skip_done, "skip_empty": skip_empty}


def main():
    ap = argparse.ArgumentParser(
        description="批量用 .csv 的 C 列(Chinese) 经 edge-tts 生成 wav（wav 名取 A 列 Filename）")
    ap.add_argument("csvs", nargs="*", help="csv 文件（可多个）；缺省=目录下全部 .csv")
    ap.add_argument("--dir", default=".", help="csv 所在目录（默认当前目录）")
    ap.add_argument("--overwrite", action="store_true", help="已存在的 wav 也重新生成（默认跳过）")
    ap.add_argument("--trim", action="store_true", help="修剪首尾静音（默认关）")
    ap.add_argument("--keep-mp3", action="store_true", help="保留中间 mp3 文件")
    ap.add_argument("--voice", default=VOICE_DEFAULT)
    ap.add_argument("--rate", default=RATE_DEFAULT, help='如 "+8%%"')
    ap.add_argument("--pitch", default=PITCH_DEFAULT, help='如 "+3Hz"')
    ap.add_argument("--volume", default=VOLUME_DEFAULT, help='如 "+30%%"')
    ap.add_argument("--samplerate", type=int, default=SAMPLERATE_DEF,
                    help="wav 输出采样率（默认 22050，对齐游戏原文件）")
    ap.add_argument("--bits", type=str, default=BITRATE_BITS, help="wav 位深（默认 16）")
    ap.add_argument("--channels", type=str, default=CHANNELS, help="声道数（默认 1 单声道）")
    ap.add_argument("--sleep", type=float, default=SLEEP_SEC)
    ap.add_argument("--retries", type=int, default=RETRIES)
    ap.add_argument("--proxy", default=None, help="http 代理，如 http://127.0.0.1:7890")
    ap.add_argument("--connect-timeout", type=int, default=15)
    ap.add_argument("--no-selftest", action="store_true", help="跳过启动自检")
    args = ap.parse_args()

    if not (1 <= int(args.samplerate) <= 384000) or int(args.samplerate) < 1000:
        ap.error("--samplerate 取值须在 1000 ~ 384000 之间（游戏原文件为 22050）")
    if not (args.channels in ("1", "2")):
        ap.error("--channels 仅支持 1（单声道）或 2（立体声）")

    hints = []
    if not (shutil.which("edge-tts") or _edge_module_ok()):
        hints.append("edge-tts")
    if SOX_BIN is None:
        hints.append("sox")
    if hints:
        msg = ["缺少外部命令：%s" % ", ".join(hints), ""]
        if "edge-tts" in hints:
            msg.append("  edge-tts：  py -m pip install --upgrade edge-tts      (Linux: pipx install edge-tts)")
        if "sox" in hints:
            msg.append("  sox(Windows)：下载 sox-14.4.2-win32.zip 解压后把 sox.exe 所在目录加入 PATH，")
            msg.append("                 或把 sox.exe 放到本脚本同目录 / 同目录下的 tools\\ 里")
            msg.append("  sox(Linux)：  sudo apt install sox libsox-fmt-all")
        msg.append("")
        msg.append("提示：装完需重开一个命令行窗口，PATH 才会生效。")
        sys.exit("\n".join(msg))
    if not os.path.isdir(args.dir):
        sys.exit(f"目录不存在：{args.dir}")

    args._caps = cli_caps()
    print(f"参数：voice={args.voice} rate={args.rate} pitch={args.pitch} volume={args.volume} "
          f"trim={'开' if args.trim else '关'} proxy={args.proxy or '无'}")
    print(f"输出格式：{args.samplerate}Hz / {args.bits}bit / "
          f"{'单' if args.channels == '1' else '双'}声道"
          f"（≈{int(args.samplerate) * int(args.bits) * int(args.channels) // 1000} kbps，对齐游戏原文件 22050/16/1≈353kbps）")
    print(f"edge-tts 版本：{cli_version()}（旧版本会被服务拒绝，建议 pipx upgrade edge-tts）")

    if not args.no_selftest:
        if not selftest(args):
            sys.exit(1)

    csv_paths = collect_csvs(args.csvs, args.dir)
    if not csv_paths:
        sys.exit(f"在 {os.path.abspath(args.dir)} 下没有找到可处理的 .csv")

    t0, targets, ok_files = time.time(), {}, 0
    for p in csv_paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(p)), stem)
        print(f"\n===== 处理CSV: {os.path.basename(p)} -> 输出目录 {out_dir} =====")
        try:
            st = process_csv(p, out_dir, args, targets)
        except Exception as e:
            print(f"  [WARN] 该文件处理出错，已跳过: {e}")
            continue
        if st:
            ok_files += 1
            print(f"  合成 {st['made']} | 失败 {st['failed']} | "
                  f"跳过(已生成) {st['skip_done']} | 跳过(空行) {st['skip_empty']}")

    print(f"\n✅ 全部完成：{ok_files}/{len(csv_paths)} 个 csv，"
          f"wav 输出在各自同名子目录，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()