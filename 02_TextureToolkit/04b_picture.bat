@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 3b  whole-image replacement for the 6 panel screenshots
echo  target entries live inside mme_eu_eng_tex (548x308 BC1)
echo ============================================================
echo.
echo How to prepare the new pictures:
echo   1. In the game, open the six panels one by one and take 1280x720
echo      screenshots with FPS / battery overlays turned off.
echo   2. Edit each screenshot, replacing the English panel with Chinese,
echo      and save them under game\shots using the SAME file names as the
echo      target jtex entries (the .png extension is fine).
echo.
echo Target names:
echo   mme_panel001_pic_tst_us_eng   mme_panel001_pic_tpt_us_eng
echo   mme_panel001_pic_fav_us_eng   mme_panel001_pic_tad_us_eng
echo   mme_panel001_pic_tat_us_eng   mme_panel001_pic_tsl_us_eng
echo.

if not exist "%GAME%\shots" (
    echo [ERROR] folder not found: %GAME%\shots
    pause & exit /b 1
)

echo [1/2] DRY RUN
"%PY%" "%SCRIPTS%\replacepic.py" "%TGT_UNPACK%\mme_eu_eng_tex" -e jtex ^
    -s "%GAME%\shots" -d "%WORK_PANEL%" --dry --preview "%GAME%\pv_panel"
if errorlevel 1 (
    echo [ERROR] dry run failed
    pause & exit /b 1
)
echo.
choice /c YN /m "Write the panel textures now"
if errorlevel 2 goto :eof

echo.
echo [2/2] WRITE
"%PY%" "%SCRIPTS%\replacepic.py" "%TGT_UNPACK%\mme_eu_eng_tex" -e jtex ^
    -s "%GAME%\shots" -d "%WORK_PANEL%" --fit contain
if errorlevel 1 (
    echo [ERROR] replace failed
    pause & exit /b 1
)

echo.
echo BC1 is fixed-size, so replacing pixels never changes entry length:
echo repacking always uses in-place overwrite and the index is untouched.
pause
