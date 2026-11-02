@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 5  verify the five repacked archives
echo ============================================================

echo.
echo [1/5] mme_us_eng_tex.jarc
"%PY%" "%SCRIPTS%\verifyjarc.py" "%TGT_JARC%" "%OUT_SD%\mme_us_eng_tex.jarc" ^
    --expect bck_obj_txt_us_eng.jtex
if errorlevel 1 echo [ERROR] mme_us verification failed

echo.
echo [2/5] mme_eu_eng_tex.jarc
"%PY%" "%SCRIPTS%\verifyjarc.py" "%TGT_JARC_EU%" "%OUT_SD%\mme_eu_eng_tex.jarc" ^
    --expect bck_obj_txt_eu_eng.jtex --expect mme_panel001_pic_tst_us_eng.jtex
if errorlevel 1 echo [ERROR] mme_eu verification failed

echo.
echo [3/5] models_data.jarc
"%PY%" "%SCRIPTS%\verifyjarc.py" "%TGT_JARC_NEST%" "%OUT_SD%\models_data.jarc" ^
    --expect training_obj001_txt_us_eng.jmdl
if errorlevel 1 echo [ERROR] models_data verification failed

echo.
echo [4/5] aot_tex.jarc (base game only)
"%PY%" "%SCRIPTS%\verifyjarc.py" "%TGT_JARC_AOT%" "%OUT_SD%\aot_tex.jarc" ^
    --expect aot_stamp001_us_eng.jtex
if errorlevel 1 echo [ERROR] aot verification failed

echo.
echo [5/5] ui_fb_tex.jarc (nested tml_tex)
"%PY%" "%SCRIPTS%\verifyjarc.py" "%TGT_JARC_FB%" "%OUT_SD%\ui_fb_tex.jarc" ^
    --nested tml_tex.jarc --expect tml_stamp_pic001_us_eng.jtex
if errorlevel 1 echo [ERROR] ui_fb verification failed

echo.
echo [visual check] re-extract the two mme archives
"%PY%" "%SCRIPTS%\jarcx.py" "%OUT_SD%\mme_us_eng_tex.jarc" -d "%GAME%\verify\mme_us_eng_tex"
"%PY%" "%SCRIPTS%\jarcx.py" "%OUT_SD%\mme_eu_eng_tex.jarc" -d "%GAME%\verify\mme_eu_eng_tex"
echo.
echo Open the PNGs under %GAME%\verify and confirm every Chinese label.
echo Only then deploy:
echo   mme_* / models_data / ui_fb  -^> update\content\ui\
echo   aot_tex                      -^> ui\ (base game path)
echo Back up every original file before overwriting.
pause
