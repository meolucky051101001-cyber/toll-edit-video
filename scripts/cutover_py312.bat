@echo off
setlocal
rem Run manually only after stopping Tool V1 with TatHoanToan.bat.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0switch_python_env.ps1" -Mode Cutover
exit /b %errorlevel%
