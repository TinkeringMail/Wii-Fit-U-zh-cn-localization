@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 4b  repack ui_fb_tex.jarc (jarc-in-jarc: tml_tex)
echo ============================================================
if not exist "%OUT_SD%" mkdir "%OUT_SD%"

echo.
echo [1/2] structure report (no writes)
"%PY%" "%SCRIPTS%\nestput.py" "%TGT_JARC_FB%" -i tml_tex --report
if errorlevel 1 (
    echo [ERROR] report failed
    pause & exit /b 1
)
echo.
echo Both outer and inner alignment lines must show a tick.
choice /c YN /m "Run the real nested pack"
if errorlevel 2 goto :eof

echo.
echo [2/2] WRITE
"%PY%" "%SCRIPTS%\nestput.py" "%TGT_JARC_FB%" -i tml_tex ^
    -n "%WORK_STAMP%" -o "%OUT_SD%\ui_fb_tex.jarc"
if errorlevel 1 (
    echo [ERROR] nested repack failed
    pause & exit /b 1
)

echo.
echo This is the highest-risk archive (18 MB, ~60 nested entries).
echo Rollback on the console is simply deleting the injected file.
pause
