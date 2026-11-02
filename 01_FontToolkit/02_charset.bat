@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 2/4  build charset directory
echo  out: %CHARS%
echo ============================================================
if not exist "%CHARS%" mkdir "%CHARS%"

echo [2a] copy the verified charsets shipped with this toolkit
copy /y "%TOOLKIT%charsets\*.txt" "%CHARS%\" >nul
if errorlevel 1 (
    echo [ERROR] cannot copy charsets\
    pause & exit /b 1
)

echo [2b] regenerate the full charset for the body fonts from the translation
if not exist "%MSG_TXT%" (
    echo [ERROR] no qualifying .txt ^>900 KB found in message\
    echo         Put your wbmgt export there; see samples\sample_health_message.txt
    pause & exit /b 1
)
"%PY%" "%SCRIPTS%\mkchars.py" --txt "%MSG_TXT%" --out "%CHARS%" ^
    --fonts style_font_DB_3.jfnt,style_font_DB_3_S.jfnt
if errorlevel 1 (
    echo [WARN] mkchars failed - the shipped charset is still in place
)

echo.
echo --- charset directory ---
for %%f in ("%CHARS%\*.txt") do (
    echo    %%~nxf
)
echo.
echo  DB_3 / DB_3_S : full charset (~2032 chars = 1990 CJK + 42 punct)
echo  DB_3_I        : 72 chars, exercise-name glyphs only (do NOT enlarge)
echo  DB_L_3        : ~1271 chars, short texts only (title / big button)
echo  EB_L_3        : ~1269 chars, short texts only (title / timer)
echo.
echo Optional: regenerate the two big-font charsets with shortset.py
echo    py scripts\shortset.py --txt "%MSG_TXT%" --fonts "%FONTS_ORIG%" --out "%CHARS%" --write
echo  (default thresholds give ~1376 chars, slightly more than the shipped 1271)
echo.
echo Done. Next: 03_build.bat
pause
