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

:: Fail early with a readable dependency/config report.
cd /d "%PROJECT_DIR%backend"
"%PYTHON_EXE%" -m pipeline_v2.preflight --project-root "%PROJECT_DIR%" --interface all
if errorlevel 1 (
  echo [ERROR] Preflight that bai. Sua cac muc error o tren roi chay lai.
  pause
  exit /b 1
)

:: Khởi động Frontend (Log Viewer) ngầm
cd /d "%PROJECT_DIR%frontend"
if not exist "node_modules" (
  call npm ci
  if errorlevel 1 (
    echo [ERROR] Khong the cai dependency frontend.
    pause
    exit /b 1
  )
)
start /b cmd /c "npm run dev"

:: Đợi 2 giây để Frontend kịp chạy
ping 127.0.0.1 -n 3 > NUL

:: Dọn dẹp tiến trình telegram_bot cũ nếu có để tránh chạy trùng lặp
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { $_.CommandLine -like '*telegram_bot.py*' } | Stop-Process -Force" >NUL 2>&1

:: Khởi động Backend (Telegram Bot) ngầm
cd /d "%PROJECT_DIR%backend"
start /b "" "%PYTHON_EXE%" telegram_bot.py
