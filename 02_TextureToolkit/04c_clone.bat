@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 3c  clone the two stamp textures across languages
echo  Completed / Found  -^>  OK
echo ============================================================
if not exist "%WORK_STAMP%" mkdir "%WORK_STAMP%"

echo.
echo [1/4] REPORT - tml stamp (structure comparison only)
"%PY%" "%SCRIPTS%\copyjtex.py" --src "%TGT_UNPACK%\ui_fb_tex\tml_tex\tml_stamp_pic001_us_frn.jtex" ^
    --dst-name tml_stamp_pic001_us_eng.jtex --report
if errorlevel 1 (
    echo [ERROR] tml report failed; check the unpacked ui_fb_tex tree
    pause & exit /b 1
)

echo.
echo [2/4] WRITE - tml stamp (us_eng and eu_eng)
"%PY%" "%SCRIPTS%\copyjtex.py" --src "%TGT_UNPACK%\ui_fb_tex\tml_tex\tml_stamp_pic001_us_frn.jtex" ^
    --dst-name tml_stamp_pic001_us_eng.jtex ^
    --dst-name tml_stamp_pic001_eu_eng.jtex --out "%WORK_STAMP%"
if errorlevel 1 (
    echo [ERROR] tml write failed
    pause & exit /b 1
)

echo.
echo [3/4] REPORT - aot stamp (base game only)
"%PY%" "%SCRIPTS%\copyjtex.py" --src "%TGT_UNPACK%\aot_tex\aot_stamp001_eu_eng.jtex" ^
    --dst-name aot_stamp001_us_eng.jtex --report
if errorlevel 1 (
    echo [ERROR] aot report failed; check the unpacked aot_tex tree
    pause & exit /b 1
)

echo.
echo [4/4] WRITE - aot stamp
"%PY%" "%SCRIPTS%\copyjtex.py" --src "%TGT_UNPACK%\aot_tex\aot_stamp001_eu_eng.jtex" ^
    --dst-name aot_stamp001_us_eng.jtex --out "%WORK_STAMP%\aot"
if errorlevel 1 (
    echo [ERROR] aot write failed
    pause & exit /b 1
)

echo.
echo Done. Next: 05a / 05b to repack.
pause
