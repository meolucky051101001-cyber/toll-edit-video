@echo off
title Tool V1 - Bang Dieu Khien va Giam Sat Render

set "PROJECT_DIR=C:\tool v1"
set "PYTHON_EXE=%PROJECT_DIR%\backend\venv\Scripts\python.exe"
set "PYTHONW_EXE=%PROJECT_DIR%\backend\venv\Scripts\pythonw.exe"

cd /d "%PROJECT_DIR%\backend"

netstat -ano | findstr ":8088" >nul
if not errorlevel 1 goto :server_ready

echo [1/2] Dang khoi dong may chu Dashboard Tool V1 tren cong 8088...
if exist "%PYTHONW_EXE%" (
    start "" "%PYTHONW_EXE%" main.py
) else (
    start "" /min "%PYTHON_EXE%" main.py
)
ping 127.0.0.1 -n 3 >nul

:server_ready
echo [1/2] May chu Dashboard Tool V1 da san sang!
echo [2/2] Dang mo giao dien Dashboard...

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app="http://127.0.0.1:8088"
    goto :done
)

if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="http://127.0.0.1:8088"
    goto :done
)

start "" "http://127.0.0.1:8088"

:done
echo.
echo Giao dien da duoc mo tai http://127.0.0.1:8088
ping 127.0.0.1 -n 2 >nul
exit
