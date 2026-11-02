@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 2  locate the 12 text-bearing entries
echo  unpacked tree: %TGT_UNPACK%
echo ============================================================

echo.
echo [1] wide banner textures (256x64 BC3)
"%PY%" "%SCRIPTS%\jarcinfo.py" "%SRC%\mme_us_eng_tex.jarc"
echo.
echo Expected: bck_obj_txt / bck_obj_txt_baby / bck_obj_txt_pet.

echo.
echo [2] group similar textures to find the 6 panel screenshots (548x308 BC1)
"%PY%" "%SCRIPTS%\group.py" "%TGT_UNPACK%" --min-count 5 --ar-min 1.70 --ar-max 1.85 --min-w 300 --top 20
echo.
echo Expected: mme_panel001_pic_tst / tpt / fav / tad / tat / tsl.

echo.
echo [3] stamp textures (256x256 BC3)
"%PY%" "%SCRIPTS%\group.py" "%TGT_UNPACK%" --name stamp --min-count 1
echo.
echo Expected: tml_stamp_pic001 (inside ui_fb_tex\tml_tex)
echo           aot_stamp001     (inside aot_tex)

echo.
echo [4] jmdl embedded text
echo    training_obj001_txt_us_eng / _eu_eng inside models_data.

echo.
echo Full target map (12 entries / 5 archives):
echo   mme_us_eng_tex : 3 banners   Body Test / Baby Stats / Pet Stats
echo   mme_eu_eng_tex : 3 banners + 6 panel screenshots
echo   models_data    : 2 jmdl
echo   ui_fb_tex      : 1 nested stamp
echo   aot_tex        : 1 stamp (base game only)
pause
