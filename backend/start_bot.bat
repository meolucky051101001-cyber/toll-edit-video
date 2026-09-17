@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" telegram_bot.py
) else (
  py telegram_bot.py
)
