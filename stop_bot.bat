@echo off
:: ==========================================
:: SCRIPT DỪNG AN TOÀN TOOL V1 TELEGRAM BOT
:: ==========================================

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=%PROJECT_DIR%backend\venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "%PROJECT_DIR%backend\v1_stop_bot.py"
