"""mergetxt.py —— 把每个 Sound Set 文件夹下的转写 txt 汇总成 <Set 名>.csv

输入（当前目录下的二级文件夹）：
    <Set 名>/*.wav + <Set 名>/*.txt   （每个 wav 一个同名 txt，正文=转写文本）
输出（写回当前目录）：
    <Set 名>.csv，表头 Filename,English,Chinese（Chinese 留空，后续补 ID/译文）

规则：跳过 <Set 名>.txt（导出脚本生成的清单）；
      文件名去掉 " (transcribed on ...)" 与 .txt/.wav 等扩展名，只留音频主名；
      文件夹里没有任何 .txt 时不生成空 csv。

用法：cd 到放这些 Set 文件夹的目录，python mergetxt.py
"""
import csv
import os
import sys

# Windows 控制台默认 cp936，打印 ✅/▸ 之类会 UnicodeEncodeError —— 统一切 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --- SETTINGS ---
# 设置【一级文件夹】路径，可以写绝对路径，或者写 "." 代表脚本所在文件夹作为一级文件夹
ROOT_FOLDER = "."
# ----------------

def merge_to_csv():
    # 把ROOT_FOLDER转成绝对路径
    root_dir = os.path.abspath(ROOT_FOLDER)

    # 遍历一级目录下所有子文件夹（二级文件夹）
    for entry in os.scandir(root_dir):
        # 只处理一级目录下的文件夹（二级文件夹），跳过一级目录里的文件
        if not entry.is_dir():
            continue
        subfolder_name = entry.name
        subfolder_path = entry.path

        print(f"正在处理二级文件夹：{subfolder_name}")
        all_rows = []

        # 遍历二级文件夹内所有文件
        for filename in os.listdir(subfolder_path):
            if not filename.lower().endswith(".txt"):
                continue

            file_fullpath = os.path.join(subfolder_path, filename)
            if not os.path.isfile(file_fullpath):
                continue

            # 规则1：忽略【二级文件夹名.txt】这个文件
            if filename == f"{subfolder_name}.txt":
                print(f"跳过排除文件: {filename}")
                continue

            # 读取txt内容
            with open(file_fullpath, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            # 规则2：去除文件名末尾 " (transcribed on ...)" 这一段
            display_name = filename
            if " (transcribed on " in display_name:
                display_name = display_name.split(" (transcribed on ")[0]

            # 规则3：去掉扩展名，只留音频主名。
            # 转写工具可能导出 foo.txt / foo.wav.txt / foo.mp3.txt，
            # CSV 的 Filename 必须是 foo，TTS 侧才会拼回 foo.wav
            #（导入时按 <sound 名>.wav 与 BFSAR 里的 sound 名对上）
            low = display_name.lower()
            for ext in (".txt", ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"):
                if low.endswith(ext):
                    display_name = display_name[: -len(ext)]
                    low = display_name.lower()
            if low.endswith(".wav"):        # 形如 foo.wav.txt → 去 .txt 后再去 .wav
                display_name = display_name[:-4]

            all_rows.append([display_name, content, ""])

        # 该文件夹里没有任何 .txt（可能不是音频目录）→ 不生成空 csv
        if not all_rows:
            print(f"跳过（无 .txt）：{subfolder_name}\n")
            continue

        # 当前二级文件夹生成独立csv，名字 = 二级文件夹名称.csv
        csv_name = f"{subfolder_name}.csv"
        csv_path = os.path.join(root_dir, csv_name)

        # 写入CSV，utf-8-sig保证Excel不乱码
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Filename", "English", "Chinese"])
            writer.writerows(all_rows)

        print(f"✅ 生成文件：{csv_name}，共 {len(all_rows)} 条\n")

    print("全部二级文件夹处理完毕！")

if __name__ == "__main__":
    merge_to_csv()
