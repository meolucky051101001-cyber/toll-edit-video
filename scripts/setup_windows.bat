@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (echo Cai dat chua hoan tat. Doc loi phia tren. & pause & exit /b 1)
echo Cai dat thanh cong. Hay mo start.bat.
pause
