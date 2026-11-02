@echo off
setlocal
call "%~dp0config.bat"

echo ============================================================
echo  STEP 4/4  deploy into the game tree
echo  from: %BUILD_OUT%\font_data.jarc
echo  to  : %SRC_JARC%
echo ============================================================
if not exist "%BUILD_OUT%\font_data.jarc" (
    echo [ERROR] nothing to deploy: %BUILD_OUT%\font_data.jarc
    pause & exit /b 1
)

for %%F in ("%BUILD_OUT%\font_data.jarc") do echo  new file: %%~zF bytes
for %%F in ("%SRC_JARC%") do echo  old file: %%~zF bytes

if not exist "%SRC_JARC%.orig.bak" (
    echo  backing up original -> %SRC_JARC%.orig.bak
    copy /y "%SRC_JARC%" "%SRC_JARC%.orig.bak" >nul
) else (
    echo  backup already exists: %SRC_JARC%.orig.bak
)

echo.
choice /c YN /m "Overwrite %SRC_JARC%"
if errorlevel 2 goto :eof

copy /y "%BUILD_OUT%\font_data.jarc" "%SRC_JARC%" >nul
if errorlevel 1 (
    echo [ERROR] copy failed
    pause & exit /b 1
)
echo.
echo  deployed.
echo  rollback: copy /y "%SRC_JARC%.orig.bak" "%SRC_JARC%"
echo.
echo  Only font_data.jarc takes effect at runtime. The loose .jfnt files
echo  next to it are NOT loaded by the game (measured), and step.rpx must
echo  stay the ORIGINAL one - do not repoint font names in the rpx.
pause
