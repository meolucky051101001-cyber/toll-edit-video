@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if exist "%~dp0stop_downloaders.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_downloaders.ps1"
)
if errorlevel 1 (pause & exit /b 1)
echo Da dung cac tien trinh cua du an.
pause
