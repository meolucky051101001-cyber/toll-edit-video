@echo off
setlocal
rem Stop only verified Tool V1 Telegram bot process without touching other services

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "PYTHON_EXE=%PROJECT_DIR%\backend\venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" -B "%PROJECT_DIR%\backend\v1_stop_bot.py" --project-root "%PROJECT_DIR%" %*
exit /b %ERRORLEVEL%
