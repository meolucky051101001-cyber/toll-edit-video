@echo off
chcp 65001 >nul
title Auto Batch Video Dubbing Processor
echo ========================================================
echo   AUTO BATCH VIDEO DUBBING PROCESSOR (OFFLINE / LOCAL)
echo ========================================================
echo.
echo [1] Thư mục đầu vào/đầu ra đọc từ backend\.env hoặc cấu hình mặc định theo hệ điều hành.
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
call ".\venv\Scripts\python.exe" "batch_processor.py"

echo.
echo ========================================================
echo   Batch đã kết thúc. Đường dẫn xuất thực tế được in trong log phía trên.
echo ========================================================
pause
