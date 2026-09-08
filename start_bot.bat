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
"%PYTHON_EXE%" v1_preflight.py --project-root "%PROJECT_DIR%" --interface telegram
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

:: Khóa riêng trong AUTODUB_WORKSPACE chặn V1 chạy trùng. Không dừng bất kỳ
:: telegram_bot.py khác vì tiến trình đó có thể là Tool V2.
cd /d "%PROJECT_DIR%backend"
start /b "" "%PYTHON_EXE%" telegram_bot.py
