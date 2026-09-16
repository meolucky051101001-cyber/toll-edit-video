@echo off
:: ==========================================
:: SCRIPT KHỞI ĐỘNG AUTO VIDEO DUBBING BOT
:: ==========================================

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=%PROJECT_DIR%backend\venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Chua co backend\venv. Hay tao venv va cai requirements.txt truoc.
  pause
  exit /b 1
)

:: Kiểm tra riêng cho Tool V1; không yêu cầu hoặc kích hoạt Pipeline V2.
cd /d "%PROJECT_DIR%backend"
"%PYTHON_EXE%" v1_preflight.py --project-root "%PROJECT_DIR%" --interface all
if errorlevel 1 (
  echo [ERROR] Preflight that bai. Sua cac muc error o tren roi chay lai.
  pause
  exit /b 1
)

:: Đảm bảo Dashboard Tool V1 (cổng 8088) đang chạy
netstat -ano | findstr ":8088" >nul
if errorlevel 1 (
  echo [Tool V1] Dang khoi dong Dashboard tren cong 8088...
  start /b "" "%PYTHON_EXE%" main.py
  timeout /t 2 /nobreak >nul
)

:: Khởi động Telegram Bot Tool V1
echo [Tool V1] Dang khoi dong Telegram Bot...
start /b "" "%PYTHON_EXE%" telegram_bot.py
echo [Tool V1] Khoi dong thanh cong! Dashboard dang chay tai http://127.0.0.1:8088
