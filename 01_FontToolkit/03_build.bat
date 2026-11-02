@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 3/4  rebuild fonts and repack font_data.jarc
echo  fonts     : %FONTS%
echo  baseline  : %FONTS_ORIG%
echo  charsets  : %CHARS%
echo  output    : %BUILD_OUT%
echo ============================================================
if not exist "%FONTS_ORIG%\style_font_DB_3.jfnt" (
    echo [ERROR] run 01_unpack.bat first
    pause & exit /b 1
)
if not exist "%CHARS%" (
    echo [ERROR] run 02_charset.bat first
    pause & exit /b 1
)

REM ---- 3a. dry run first: no file is written, geometry is printed ----
echo.
echo [3a] DRY RUN
"%PY%" "%SCRIPTS%\jfntbuild.py" --fonts "%FONTS_ORIG%" --chars "%CHARS%" ^
    --ttf "%TTF_MAIN%" ^
    --ttf-for "style_font_EB_L_3.jfnt,%TTF_EB%" ^
    --mode both --cjk-only --verify ^
    -o "%BUILD_OUT%" --jarc "%SRC_JARC%" ^
    --font %FONTS% --dry
if errorlevel 1 (
    echo [ERROR] dry run failed
    pause & exit /b 1
)

echo.
echo Check the numbers above before continuing:
echo   - every font reports  block rows %% 16 == 0   and  imageSize %% 32768 == 0
echo   - "PUA E045/E046" is present
echo   - ink height is within 1-2 px of the original
echo.
choice /c YN /m "Run the real build now"
if errorlevel 2 goto :eof

REM ---- 3b. real build ----
echo.
echo [3b] BUILD
"%PY%" "%SCRIPTS%\jfntbuild.py" --fonts "%FONTS_ORIG%" --chars "%CHARS%" ^
    --ttf "%TTF_MAIN%" ^
    --ttf-for "style_font_EB_L_3.jfnt,%TTF_EB%" ^
    --mode both --cjk-only --verify ^
    -o "%BUILD_OUT%" --jarc "%SRC_JARC%" ^
    --font %FONTS%
if errorlevel 1 (
    echo [ERROR] build failed
    pause & exit /b 1
)

echo.
echo --- result ---
dir "%BUILD_OUT%\font_data.jarc"
echo.
echo  Reference: the verified build is 16,454,127 bytes.
echo  14-17 MB is normal; under 13 MB means the charset was not applied.
echo.
echo Done. Next: 04_deploy.bat
pause
