@echo off
REM =====================================================================
REM  config.bat  -  shared settings for 01..06 batch files
REM
REM  PORTABLE EDITION: every path is relative to this toolkit folder.
REM
REM  Before running, put the game tree into the "game" sub-folder so
REM  that it contains (at minimum):
REM      game\ui\*.jarc                          (base-game ui archives)
REM      game\update\content\ui\mme_us_eng_tex.jarc
REM      game\update\content\ui\mme_eu_eng_tex.jarc
REM      game\update\content\ui\models_data.jarc
REM      game\update\content\ui\ui_fb_tex.jarc
REM  Note: aot_tex.jarc exists only in the base game (no update copy).
REM =====================================================================

set TOOLKIT=%~dp0

REM --- game working directory ---
if not defined GAME set GAME=%TOOLKIT%game

REM --- TTF used for drawing Chinese text ---
if not defined TTF set TTF=%TOOLKIT%fonts\AlibabaPuHuiTi-3-75-SemiBold.ttf

REM --- jarc source tree (base-game ui folder) ---
if not defined SRC set SRC=%GAME%\ui

REM --- unpacked tree ---
if not defined TGT_UNPACK set TGT_UNPACK=%GAME%\out_ui

REM --- update archives (repack baselines) ---
if not defined TGT_JARC set TGT_JARC=%GAME%\update\content\ui\mme_us_eng_tex.jarc
if not defined TGT_JARC_EU set TGT_JARC_EU=%GAME%\update\content\ui\mme_eu_eng_tex.jarc
if not defined TGT_JARC_NEST set TGT_JARC_NEST=%GAME%\update\content\ui\models_data.jarc
if not defined TGT_JARC_FB set TGT_JARC_FB=%GAME%\update\content\ui\ui_fb_tex.jarc
if not defined TGT_JARC_AOT set TGT_JARC_AOT=%GAME%\ui\aot_tex.jarc

REM --- scratch dirs ---
if not defined WORK_UPD set WORK_UPD=%GAME%\work_upd
if not defined WORK_PANEL set WORK_PANEL=%GAME%\work_panel
if not defined WORK_STAMP set WORK_STAMP=%GAME%\work_stamp

REM --- final repack output ---
if not defined OUT_SD set OUT_SD=%GAME%\out_sd\ui

REM --- interpreter and script dir ---
if not defined PY set PY=py
set SCRIPTS=%TOOLKIT%scripts

REM --- GAME_DIR lets scripts (rebuild.py etc.) find the game tree ---
set GAME_DIR=%GAME%
