@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 1/4  unpack original font_data.jarc
echo  in : %SRC_JARC%
echo  out: %WORK%\fonts_orig
echo ============================================================
if not exist "%SRC_JARC%" (
    echo [ERROR] not found: %SRC_JARC%
    echo         Put the original game tree under the "game" folder,
    echo         keeping update\content\fonts\font_data.jarc
    pause & exit /b 1
)

"%PY%" "%SCRIPTS%\jarcx.py" "%SRC_JARC%" -d "%WORK%\fonts_orig"
if errorlevel 1 (
    echo [ERROR] unpack failed
    pause & exit /b 1
)

echo.
echo --- extracted .jfnt (these are the calibration baselines) ---
dir /b "%FONTS_ORIG%\*.jfnt"
echo.
echo Expected sizes (US/EU build, 9 entries):
echo   9400000  style_font_DB_3.jfnt      1580000  style_font_DB_3_I.jfnt
echo  18390000  style_font_DB_3_S.jfnt     418000  style_font_DB_3_S_fmb.jfnt
echo   5250000  style_font_DB_L_3.jfnt    2106000  style_font_DB_L_3_fmb.jfnt
echo   2630000  style_font_DB_L_3_I.jfnt  13640000 style_font_EB_L_3.jfnt
echo   6040000  style_font_debug.jfnt
echo.
echo Done. Next: 02_charset.bat
pause
