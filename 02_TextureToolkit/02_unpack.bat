@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 1  unpack every jarc in the base-game ui folder
echo  in : %SRC%
echo  out: %TGT_UNPACK%
echo ============================================================
if not exist "%SRC%" (
    echo [ERROR] folder not found: %SRC%
    echo         Put the game tree under the "game" folder first.
    pause & exit /b 1
)
if not exist "%TGT_UNPACK%" mkdir "%TGT_UNPACK%"

set N=0
for %%F in ("%SRC%\*.jarc") do (
    echo.
    echo --- unpacking %%~nxF
    "%PY%" "%SCRIPTS%\jarcx.py" "%%~F" -d "%TGT_UNPACK%\%%~nF"
    if not errorlevel 1 set /a N+=1
)

echo.
echo unpacked %N% archives -^> %TGT_UNPACK%
echo.
echo Quick check:
dir /b "%TGT_UNPACK%"
echo.
echo The mme_us_eng_tex folder should contain 39 .jtex files.
echo Nested archives (ui_fb_tex) keep their own sub-folder structure.
pause
