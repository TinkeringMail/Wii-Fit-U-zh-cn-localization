@echo off
REM =====================================================================
REM  config.bat  -  shared settings for 01..04 batch files
REM
REM  PORTABLE EDITION: every path is relative to this toolkit folder.
REM  No absolute paths, no machine-specific folders.
REM
REM  Before running, put your game tree into the "game" sub-folder so
REM  that it contains:
REM      game\update\content\fonts\font_data.jarc   (original archive)
REM
REM  The TTF fonts (fonts\) and the translated text (message\) are NOT
REM  shipped in this public repo; add them yourself, see README.md.
REM
REM  Every value uses "if not defined", so a variable already set in the
REM  environment still wins.
REM =====================================================================

set TOOLKIT=%~dp0

REM --- 1. game working directory (contains update\content\fonts) ---
if not defined GAME set GAME=%TOOLKIT%game

REM --- 2. TTF used for every font, and the exception for EB_L_3 ---
if not defined TTF_MAIN set TTF_MAIN=%TOOLKIT%fonts\AlibabaPuHuiTi-3-75-SemiBold.ttf
if not defined TTF_EB set TTF_EB=%TOOLKIT%fonts\AlibabaPuHuiTi-3-115-Black.ttf

REM --- 3. translated text (wbmgt export format, --- separated blocks) ---
REM     Auto-detect: any .txt under message\ larger than 900 KB that
REM     contains the --- block marker; largest qualifying file wins.
if not defined MSG_TXT (
    set "MSG_TXT="
    for /f "delims=" %%F in ('dir /b /o-s "%TOOLKIT%message\*.txt" 2^>nul') do (
        for %%G in ("%TOOLKIT%message\%%F") do (
            if not defined MSG_TXT (
                if %%~zG GTR 921600 (
                    "%SystemRoot%\System32\findstr.exe" /c:"---" "%%~fG" >nul 2>&1
                    if not errorlevel 1 set "MSG_TXT=%%~fG"
                )
            )
        )
    )
)
if defined MSG_TXT (
    echo [config] message text: %MSG_TXT%
) else (
    echo [config][WARN] no qualifying .txt ^>900 KB found in message\
)

REM --- 4. original font archive (INPUT; never point this at a build) ---
if not defined SRC_JARC set SRC_JARC=%GAME%\update\content\fonts\font_data.jarc

REM --- 5. scratch / output (all created inside the game folder) ---
if not defined WORK set WORK=%GAME%\_fontwork
if not defined FONTS_ORIG set FONTS_ORIG=%WORK%\fonts_orig\font_data
if not defined CHARS set CHARS=%WORK%\chars
if not defined BUILD_OUT set BUILD_OUT=%GAME%\build\v5

REM --- 6. fonts to rebuild (the other 4 entries are copied untouched) ---
if not defined FONTS set FONTS=style_font_DB_3.jfnt,style_font_DB_3_S.jfnt,style_font_DB_3_I.jfnt,style_font_DB_L_3.jfnt,style_font_EB_L_3.jfnt

REM --- 7. interpreter and script dir ---
if not defined PY set PY=py
set SCRIPTS=%TOOLKIT%scripts
