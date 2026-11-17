@echo off
chcp 65001 >nul
title Tool V1 - Cloud GPU AutoDub Node (Port 8089)
cls

echo ==============================================================================
echo       TOOL V1 // CLOUD GPU AUTODUB NODE (STANDALONE ON-DEMAND)
echo ==============================================================================
echo   * URL Dashboard:    http://127.0.0.1:8089/cloud
echo   * Cong dich vu:     8089 (Hoan toan tach biet voi Dashboard 8088)
echo   * Ha tang AI:       Google Colab GPU T4/L4 (Demucs + Whisper + CapCut + EBU R128)
echo   * Luu y:            Chi chay tam thoi khi can render gap/nang.
echo                       Dong cua so nay de tat hoan toan dich vu Cloud.
echo ==============================================================================
echo.

set "UPDATES_DIR=C:\Projects\Tools\Tool V1\Updates"
set "PYTHON_EXE=C:\tool v1\backend\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

cd /d "%UPDATES_DIR%"
"%PYTHON_EXE%" run_cloud_node.py --port 8089

echo.
echo [!] Dich vu Cloud GPU Standalone da dung.
pause
