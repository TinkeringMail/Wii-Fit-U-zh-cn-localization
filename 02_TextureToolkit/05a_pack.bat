@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 4a  repack the four flat jarc archives
echo  out: %OUT_SD%
echo ============================================================
if not exist "%OUT_SD%" mkdir "%OUT_SD%"
set STAGE=%GAME%\stage

echo.
echo [1/4] build per-archive staging folders
if not exist "%STAGE%\mme_us_eng_tex" mkdir "%STAGE%\mme_us_eng_tex"
if not exist "%STAGE%\mme_eu_eng_tex" mkdir "%STAGE%\mme_eu_eng_tex"
if not exist "%STAGE%\models_data" mkdir "%STAGE%\models_data"
if not exist "%STAGE%\aot_tex" mkdir "%STAGE%\aot_tex"

xcopy /Y /E "%WORK_UPD%\mme_us_eng_tex\*" "%STAGE%\mme_us_eng_tex\" >nul
xcopy /Y /E "%WORK_UPD%\mme_eu_eng_tex\*" "%STAGE%\mme_eu_eng_tex\" >nul
xcopy /Y /E "%WORK_PANEL%\*"             "%STAGE%\mme_eu_eng_tex\" >nul
xcopy /Y /E "%WORK_UPD%\models_data\*"   "%STAGE%\models_data\" >nul
xcopy /Y /E "%WORK_STAMP%\aot\*"         "%STAGE%\aot_tex\" >nul

echo.
echo [2/4] mme_us_eng_tex.jarc
"%PY%" "%SCRIPTS%\putback.py" "%TGT_JARC%" -n "%STAGE%\mme_us_eng_tex" -o "%OUT_SD%\mme_us_eng_tex.jarc"
if errorlevel 1 ( echo [ERROR] mme_us repack failed & pause & exit /b 1 )

echo.
echo [3/4] mme_eu_eng_tex.jarc (banners + panel screenshots)
"%PY%" "%SCRIPTS%\putback.py" "%TGT_JARC_EU%" -n "%STAGE%\mme_eu_eng_tex" -o "%OUT_SD%\mme_eu_eng_tex.jarc"
if errorlevel 1 ( echo [ERROR] mme_eu repack failed & pause & exit /b 1 )

echo.
echo [4/4] models_data.jarc and aot_tex.jarc
"%PY%" "%SCRIPTS%\putback.py" "%TGT_JARC_NEST%" -n "%STAGE%\models_data" -o "%OUT_SD%\models_data.jarc"
if errorlevel 1 ( echo [ERROR] models_data repack failed & pause & exit /b 1 )
"%PY%" "%SCRIPTS%\putback.py" "%TGT_JARC_AOT%" -n "%STAGE%\aot_tex" -o "%OUT_SD%\aot_tex.jarc"
if errorlevel 1 ( echo [ERROR] aot repack failed & pause & exit /b 1 )

echo.
echo Every "[A] in-place overwrite" entry is length-identical and safe.
echo If a "[B] full repack" appears, the alignment line above it must
echo show "rebuild identical" before the output can be trusted.
pause
