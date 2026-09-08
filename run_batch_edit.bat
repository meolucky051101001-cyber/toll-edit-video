@echo off
chcp 65001 >nul
title Auto Batch Video Dubbing Processor
if not defined AUTODUB_INPUT_DIR set "AUTODUB_INPUT_DIR=D:\video_input"
if not defined AUTODUB_OUTPUT_DIR set "AUTODUB_OUTPUT_DIR=D:\banve"
echo ========================================================
echo   AUTO BATCH VIDEO DUBBING PROCESSOR (OFFLINE / LOCAL)
echo ========================================================
echo.
echo [1] Thư mục chứa video gốc : %AUTODUB_INPUT_DIR%
echo [2] Thư mục lưu thành phẩm : %AUTODUB_OUTPUT_DIR%
echo.
echo Đang quét và bắt đầu xử lý tuần tự từng video...
echo.

cd /d "%~dp0backend"
if not exist ".\venv\Scripts\python.exe" (
  echo [ERROR] Chua co backend\venv. Hay cai dependencies truoc.
  pause
  exit /b 1
)
call ".\venv\Scripts\python.exe" -m pipeline_v2.preflight --project-root "%~dp0" --interface batch
if errorlevel 1 (
  echo [ERROR] Preflight that bai.
  pause
  exit /b 1
)
call ".\venv\Scripts\python.exe" "batch_processor.py" --input "%AUTODUB_INPUT_DIR%" --output "%AUTODUB_OUTPUT_DIR%"

echo.
echo ========================================================
echo   HOÀN TẤT! Video thành phẩm đã được lưu tại %AUTODUB_OUTPUT_DIR%
echo ========================================================
pause
