@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_downloaders.ps1"
if errorlevel 1 (echo Khong the khoi dong downloader. Doc loi phia tren. & pause & exit /b 1)
