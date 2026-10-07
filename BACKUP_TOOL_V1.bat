@echo off
setlocal enabledelayedexpansion

echo ======================================================================
echo   BACKUP TOAN BO HE THONG TOOL V1 (SAO LUU AN TOAN)
echo ======================================================================
echo.

cd /d "C:\tool v1"

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set datetime=%%I
if defined datetime (
    set TIMESTAMP=!datetime:~0,8!_!datetime:~8,6!
) else (
    set TIMESTAMP=%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%
)
set TIMESTAMP=%TIMESTAMP: =0%

set BRANCH_NAME=backup/v1-auto-%TIMESTAMP%

echo [1/3] Dang tao nhanh Git Backup: %BRANCH_NAME%...
git branch %BRANCH_NAME%
if %ERRORLEVEL% EQU 0 (
    echo       - Da tao nhanh Git: %BRANCH_NAME% thanh cong!
) else (
    echo       - Nhanh da ton tai hoac Git thong bao, tiep tuc...
)

echo [2/3] Dang kiem tra cac file da thay doi...
git status --short

echo [3/3] Dang tao ban sao du phong vat ly tai thu muc backups...
set BACKUP_DIR=C:\tool v1\backups\backup_%TIMESTAMP%
mkdir "%BACKUP_DIR%\backend" 2>nul
mkdir "%BACKUP_DIR%\backend\ai" 2>nul

if exist "backend\batch_processor.py" copy /y "backend\batch_processor.py" "%BACKUP_DIR%\backend\" >nul
if exist "backend\telegram_bot.py" copy /y "backend\telegram_bot.py" "%BACKUP_DIR%\backend\" >nul
if exist "backend\voice_selection.py" copy /y "backend\voice_selection.py" "%BACKUP_DIR%\backend\" >nul
if exist "backend\main.py" copy /y "backend\main.py" "%BACKUP_DIR%\backend\" >nul
if exist "backend\tool_control_runtime.py" copy /y "backend\tool_control_runtime.py" "%BACKUP_DIR%\backend\" >nul
if exist "backend\ai\voice_cloning.py" copy /y "backend\ai\voice_cloning.py" "%BACKUP_DIR%\backend\ai\" >nul
if exist "backend\ai\v1_auto_voice.py" copy /y "backend\ai\v1_auto_voice.py" "%BACKUP_DIR%\backend\ai\" >nul
if exist "backend\ai\v1_voice_cache.py" copy /y "backend\ai\v1_voice_cache.py" "%BACKUP_DIR%\backend\ai\" >nul

echo.
echo ======================================================================
echo SAO LUU HOAN TAT!
echo   - Git Branch : %BRANCH_NAME%
echo   - Thu muc luu: %BACKUP_DIR%
echo.
echo Khi can khoi phuc, ban chi can chay file: RESTORE_VOICE_LOCK_BACKUP.bat
echo hoac dung lenh Git: git checkout %BRANCH_NAME%
echo ======================================================================
echo.
pause
