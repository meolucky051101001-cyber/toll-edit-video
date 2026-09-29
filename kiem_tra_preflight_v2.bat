@echo off
chcp 65001 >nul
title Kiem tra Preflight Pipeline v2

set "PYTHON_EXE=C:\tool v2\backend\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=C:\tool v1\backend\venv\Scripts\python.exe"
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Khong tim thay Python venv tai C:\tool v2\backend\venv hoac C:\tool v1\backend\venv
    pause
    exit /b 1
)

echo ========================================================
echo   KIEM TRA PREFLIGHT PIPELINE V2 PRODUCTION
echo   Python: %PYTHON_EXE%
echo ========================================================
echo.

cd /d "C:\tool v2\backend"
"%PYTHON_EXE%" -m pipeline_v2.preflight --project-root "C:\tool v2" --interface all

echo.
echo ========================================================
pause
