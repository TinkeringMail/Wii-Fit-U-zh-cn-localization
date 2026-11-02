@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 0  environment / prerequisites
echo ============================================================

echo [1] Python
"%PY%" --version
if errorlevel 1 (
    echo [ERROR] Python not found via "%PY%"
    echo         Install Python 3.10+ and tick "Add Python to PATH",
    echo         or set PY in config.bat
    pause & exit /b 1
)

echo [2] Pillow
"%PY%" -c "import PIL; print('Pillow', PIL.__version__)"
if errorlevel 1 (
    echo [ERROR] Pillow missing. Run:  %PY% -m pip install pillow
    pause & exit /b 1
)

echo [3] script self-tests
"%PY%" "%SCRIPTS%\jarcx.py" --selftest
if errorlevel 1 (
    echo [ERROR] jarcx self-test failed
    pause & exit /b 1
)
"%PY%" "%SCRIPTS%\verifyjarc.py" --selftest
if errorlevel 1 (
    echo [ERROR] verifyjarc self-test failed
    pause & exit /b 1
)

echo [4] expected game files
for %%F in (
    "%TGT_JARC%"
    "%TGT_JARC_EU%"
    "%TGT_JARC_NEST%"
    "%TGT_JARC_FB%"
    "%TGT_JARC_AOT%"
) do (
    if exist "%%~F" (
        echo        OK  %%~F
    ) else (
        echo        MISSING  %%~F
    )
)

echo.
echo [5] TTF
if exist "%TTF%" (echo        OK  %TTF%) else (echo        MISSING  %TTF%)

echo.
echo If any MISSING, put the game files under the "game" folder,
echo keeping the relative paths shown above.
pause
