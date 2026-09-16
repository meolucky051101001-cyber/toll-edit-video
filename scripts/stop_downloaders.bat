@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_downloaders.ps1"
if errorlevel 1 (echo Khong the dung downloader. Doc loi phia tren. & pause & exit /b 1)
