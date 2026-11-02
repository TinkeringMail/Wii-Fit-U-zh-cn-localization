@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 3a  draw Chinese on the banner textures and jmdl
echo  scratch: %WORK_UPD%
echo ============================================================
if not exist "%TGT_UNPACK%\mme_us_eng_tex" (
    echo [ERROR] run 02_unpack.bat first
    pause & exit /b 1
)
if not exist "%WORK_UPD%" mkdir "%WORK_UPD%"

echo.
echo [1/4] DRY RUN - US banners (no file is written)
"%PY%" "%SCRIPTS%\bctext.py" "%TGT_UNPACK%\mme_us_eng_tex" -e jtex ^
    -d "%WORK_UPD%\mme_us_eng_tex" --dry --preview "%GAME%\pv_upd" --stroke 3 --size 49
if errorlevel 1 (
    echo [ERROR] dry run failed
    pause & exit /b 1
)
echo.
choice /c YN /m "Write the US banners now"
if errorlevel 2 goto :eof

echo.
echo [2/4] US banners -^> 身体测试 / 宝宝数据 / 宠物数据
"%PY%" "%SCRIPTS%\bctext.py" "%TGT_UNPACK%\mme_us_eng_tex" -e jtex ^
    -d "%WORK_UPD%\mme_us_eng_tex" --stroke 3 --size 49
if errorlevel 1 (
    echo [ERROR] US build failed
    pause & exit /b 1
)

echo.
echo [3/4] EU banners
"%PY%" "%SCRIPTS%\bctext.py" "%TGT_UNPACK%\mme_eu_eng_tex" -e jtex ^
    -d "%WORK_UPD%\mme_eu_eng_tex" --stroke 3 --size 49
if errorlevel 1 (
    echo [ERROR] EU build failed
    pause & exit /b 1
)

echo.
echo [4/4] jmdl embedded text -^> 锻炼
"%PY%" "%SCRIPTS%\bctext.py" "%TGT_UNPACK%\models_data\training_obj001_txt_us_eng.jmdl" ^
    "%TGT_UNPACK%\models_data\training_obj001_txt_eu_eng.jmdl" ^
    -d "%WORK_UPD%\models_data" --text 锻炼 --stroke 3
if errorlevel 1 (
    echo [ERROR] jmdl build failed
    pause & exit /b 1
)

echo.
echo Preview PNGs are in %GAME%\pv_upd. Open them and confirm the text
echo is centered, fully inside the banner and has no ghosting.
pause
