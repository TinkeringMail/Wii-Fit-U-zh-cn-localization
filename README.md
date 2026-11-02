# Wii Fit U Localization Toolkit / Wii Fit U 本地化工具包

**EN:** A set of portable Python scripts and batch files for localizing the Wii U game *Wii Fit U* into Simplified Chinese, covering in-game fonts (JFNT), text-bearing textures (jTEX/jMDL), and voice audio (BFSAR).

**中文：** 一组便携的 Python 脚本与批处理文件，用于把 Wii U 游戏《Wii Fit U》本地化为简体中文，覆盖游戏字体（JFNT）、含文字贴图（jTEX/jMDL）与语音（BFSAR）三个部分。

---

## Legal notice / 法律声明

**EN:**

- This is a **non-commercial, fan-made research project**. It is not affiliated with, sponsored by, or endorsed by Nintendo Co., Ltd. or any of its affiliates.
- "Wii", "Wii U", "Wii Fit U" and related names are trademarks of their respective owners. They are used here only to identify the target software (nominative use).
- This repository **does not contain any game files, game-derived binaries, game text dumps, or font files**. Nothing here is a ROM, ROM fragment, decryption key, or console key.
- You must supply all game files yourself, extracted from a copy of the game you legally own. You are responsible for complying with the laws applicable in your jurisdiction.
- The software is provided "as is", without warranty of any kind. See `LICENSE`.

**中文：**

- 本项目为**非商业的同人研究项目**，与任天堂公司及其关联公司无任何隶属、赞助或认可关系。
- “Wii”“Wii U”“Wii Fit U”及相关名称均为各自权利人的商标，在此仅用于标识目标软件（指示性使用）。
- 本仓库**不包含任何游戏文件、游戏衍生二进制文件、游戏文本导出或字体文件**，也不含 ROM、ROM 碎片、解密密钥或主机密钥。
- 所有游戏文件均须由你本人从合法拥有的游戏中提取并自行提供；你有责任遵守所在司法辖区的法律。
- 本软件按“原样”提供，不附任何担保，详见 `LICENSE`。

---

## Repository structure / 仓库结构

```
WiiFitU_Localization_Public/
├─ 01_FontToolkit/        字体本地化：编号 bat + scripts/ + charsets/ + docs/
├─ 02_TextureToolkit/     贴图本地化：编号 bat + scripts/ + docs/
├─ 03_VoiceToolkit/       音频本地化：scripts/（导出/导入/TTS）
├─ samples/               虚构的格式示例（不含任何游戏内容）
├─ LICENSE                MIT License（英文原文 + 中文译文）
├─ NOTICE                 第三方组件与归属说明
└─ .gitignore
```

**EN:** Each toolkit is self-contained. Every path in the batch files is relative to the script's own directory; there are no machine-specific absolute paths.

**中文：** 每个工具包自包含，批处理中的所有路径都基于脚本所在目录，不包含任何机器相关的绝对路径。

---

## Prerequisites / 环境准备

**EN:**

1. Windows + Python 3.10 or newer (install with *Add Python to PATH*; the `py` launcher is used).
2. Pillow: `py -m pip install pillow`
3. For voice work: `py -m pip install --upgrade edge-tts`, plus SoX (place `sox.exe` under `03_VoiceToolkit\scripts\tts\` or `...\tts\tools\`, or add it to PATH).
4. The fonts are **not** included. Download Alibaba PuHuiTi from the official channel (Alibaba fonts site) under its own license and place the TTF files into the `fonts\` folders yourself. Do not modify the font files.

**中文：**

1. Windows + Python 3.10 或更高版本（安装时勾选 *Add Python to PATH*，使用 `py` 启动器）。
2. 安装 Pillow：`py -m pip install pillow`
3. 音频部分需安装 edge-tts：`py -m pip install --upgrade edge-tts`，并准备 SoX（把 `sox.exe` 放到 `03_VoiceToolkit\scripts\tts\` 或 `...\tts\tools\` 下，也可加入系统 PATH）。
4. 字体**不随仓库提供**。请从阿里巴巴字体官方渠道下载普惠体（适用其自有许可证），自行把 TTF 放入各包的 `fonts\` 目录，且不要修改字体文件。

---

## Files you must supply / 你需要自备的文件

**EN:** Place your extracted game tree into each toolkit's `game\` folder, keeping the original relative paths:

**中文：** 把解包后的游戏树放入各工具包的 `game\` 目录，保持原始相对路径：

```
game\update\content\fonts\font_data.jarc
game\update\content\ui\mme_us_eng_tex.jarc
game\update\content\ui\mme_eu_eng_tex.jarc
game\update\content\ui\models_data.jarc
game\update\content\ui\ui_fb_tex.jarc
game\ui\aot_tex.jarc                       (base game only / 仅本体存在)
```

**EN:** For the voice toolkit, place the sound archive at `bfsar\rp_Step_sound.bfsar`. The BMG text exports used for ID matching are also produced by you from your own game files, which can be opened and exported as txt directly using [MSBT Editor](https://msbt-editor.aeonsake.com/) at /storage_usb/usr/title/0005000e/10102300/content/US/Message/message.carc; see `samples\sample_health_message.txt` for the format, or use the file in the zh-cn patch directly.

**中文：** 音频工具包请把声音归档放到 `bfsar\rp_Step_sound.bfsar`。用于匹配 ID 的 BMG 文本导出也由你从自己的游戏文件生成，可以直接用 [MSBT Editor](https://msbt-editor.aeonsake.com/) 直接打开游戏升级补丁安装目录/storage_usb/usr/title/0005000e/10102300/content/US/Message/message.carc，导出为任意名称的.txt文件放到\01_FontToolkit\message，然后进行翻译。格式见 `samples\sample_health_message.txt`，也可以直接使用汉化补丁里的版本。

---

## Workflow / 操作流程

**EN:** Run the numbered batch files in order inside each toolkit. Every step pauses and tells you what to check before continuing.

**中文：** 在各工具包内按编号顺序运行批处理，每步会暂停并提示检查项。

### 1. Fonts / 字体（`01_FontToolkit`）

`01_unpack` → `02_charset` → `03_build` (dry run first, then real build) → `04_deploy`

**EN:** Five text fonts are rebuilt with the PuHuiTi glyphs (default, configurable on config.bat) ; the other four archive entries are copied untouched. Reference output size: 16,454,127 bytes (14–17 MB is normal).

**中文：** 5 个文本字体用普惠体字形重建（默认字体，可在config.bat里配置），其余 4 个条目原样保留。参考输出大小 16,454,127 字节（14–17 MB 均正常）。

### 2. Textures / 贴图（`02_TextureToolkit`）

`01_check` → `02_unpack` → `03_locate` → `04a_draw` → `04b_picture` (optional) → `04c_clone` → `05a_pack` → `05b_pack_nested` → `06_verify`

**EN:** 12 entries across 5 archives: three banner sets, six panel screenshots (whole-image replacement), two embedded jMDL texts, and two stamp textures.

**中文：** 共 5 个归档、12 个条目：三组横幅、六张面板截图（整图替换）、两个 jMDL 内嵌文字和两枚印章贴图。

### 3. Voice / 音频（`03_VoiceToolkit`）

**EN:** Two pipelines — Wave Sound Sets (one sound = one wav) and Sequence Sound Sets (sounds are music sequences mapped through instrument banks). Follow the scripts in this order:

**中文：** 两条流水线——Wave（一条声音对应一个 wav）与 Sequence（声音是经乐器库映射的乐谱序列）。按以下顺序使用脚本：

1. `bfsar_export.py` / `bfsar_seqset_export.py` — export
2. transcribe each wav (manually or a local Whisper tool)
3. `mergetxt.py` — merge transcriptions into CSV
4. `export\ID_find.py` — match entries against your own BMG text export
5. `export\copy_trans.py` — fill translations; review the CSV manually
6. `tts\csv_to_tts_wav_Xiaoxiao.py` — synthesize Chinese wav
7. `bfsar_import.py` / `bfsar_seqset_import.py` — repack; wait for `自检: 通过`

**EN:** Keep the sample rate at 22050 and prefer DSP-ADPCM encoding to reduce size (note some items are 48000 and some played on Wiimote are 8000, adjust as needed). The BFSAR is loaded into memory as a whole, so the repacked archive should not exceed the original size, or at least not growing too much.

**中文：** 采样率保持 22050，编码优先选 DSP-ADPCM 以减小体积（注意有些项目为48000，有些在Wiimote上播放的为8000，按需调整)。BFSAR 整包进内存，回封后的整包体积不应超过原包，或至少不要超过太多。

---

## Important licensing note / 重要许可证提示

**EN:** This project is licensed under MIT. However, if any code was ported or derived from QuickBMS (GPL-licensed) or another GPL component, the affected code must be released under the GPL instead of MIT. Verify the provenance of every file before publishing.

**中文：** 本项目采用 MIT 许可证。但若任何代码移植或衍生自 QuickBMS（GPL 许可证）或其他 GPL 组件，相关代码必须改用 GPL 许可证发布。发布前请逐文件核实代码来源。

---

## Troubleshooting / 故障排查

| Symptom / 症状                     | Fix / 处理                                                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `py` not recognized / 找不到 `py`  | Reinstall Python with PATH enabled, or edit `PY` in `config.bat` / 重装 Python 并勾选 PATH，或改 config.bat 的 `PY` |
| `No module named PIL`              | Run `py -m pip install pillow`                                                                                      |
| Missing-file errors / 提示文件缺失 | Check the `game\` paths in the section above / 对照上文检查 `game\` 路径                                            |
| TTS 403 / NoAudioReceived          | Upgrade edge-tts, check system clock, use a proxy / 升级 edge-tts、校准系统时间、使用代理                           |
| Texture not found / 未找到像素块   | Re-run from the unpack step; GTX offsets are handled by the scripts / 从解包步骤重跑，GTX 偏移由脚本处理            |
| In-game crash / 游戏死机           | Restore the backup and verify only self-tested files are deployed / 恢复备份，确认只部署自检通过的文件              |

---

## Reference documents / 参考文档

- `01_FontToolkit\docs\FORMAT.md` — JFNT/jIMG/GTX format notes (Chinese)
- `02_TextureToolkit\docs\FORMAT_jtex.md` — jTEX/jARC format notes (Chinese)
- `samples\` — invented, copyright-free format examples

## External references / 外部参考

**EN:**

- [arqhive/wii-fit-u-korean-translation](https://github.com/arqhive/wii-fit-u-korean-translation)
  — a sibling fan localization of the same game (Korean); referenced for
  the overall workflow and repository packaging. License: MIT,
  Copyright (c) 2026 arqhive.
- [TuneBloom](https://github.com/stupidestmodder/TuneBloom)
  — open-source BFSAR editor; the import/export behavior of the voice
  scripts follows its documented semantics. License: MIT,
  Copyright (c) 2026 STUPID Modder (see NOTICE).
- [Nintendo File Formats — NW Sound File Overview](https://nintendo-formats.com/libs/nw/sound.html)
  — community documentation of the BFSAR header, block references, item
  IDs and wave tables; used as the technical reference for the sound
  structures.

**中文：**

- [arqhive/wii-fit-u-korean-translation](https://github.com/arqhive/wii-fit-u-korean-translation)
  — 同一游戏的韩国语同人本地化项目，参考其整体流程与仓库组织方式。
  MIT 许可证，版权所有 (c) 2026 arqhive。
- [TuneBloom](https://github.com/stupidestmodder/TuneBloom)
  — 开源 BFSAR 编辑器，音频脚本的导入/导出行为依其文档语义实现。
  MIT 许可证，版权所有 (c) 2026 STUPID Modder（见 NOTICE）。
- [Nintendo File Formats：NW 声音文件概览](https://nintendo-formats.com/libs/nw/sound.html)
  — 社区维护的格式文档，涵盖 BFSAR 头部、块引用、Item ID 与波形表，
  作为声音结构的技术参考。

