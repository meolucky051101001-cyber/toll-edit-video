@echo off
setlocal
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "PYTHON_EXE=%PROJECT_DIR%\backend\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Dang kiem tra va tat cac dich vu Tool V1 da xac minh (Port 8088, 8090, Telegram Bot, Background Service)...
"%PYTHON_EXE%" -B "%PROJECT_DIR%\backend\v1_shutdown.py" --scope all --project-root "%PROJECT_DIR%" %*
set "EXIT_CODE=%ERRORLEVEL%"
exit /b %EXIT_CODE%
