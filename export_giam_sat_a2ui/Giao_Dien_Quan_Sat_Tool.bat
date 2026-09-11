@echo off
chcp 65001 >nul
title Bang Dieu Khien Tool V1

netstat -ano | findstr :8088 | findstr LISTENING >nul
if %errorlevel% equ 0 goto OPEN_BROWSER

start "" "C:\tool v1\backend\venv\Scripts\pythonw.exe" "C:\tool v1\backend\main.py"
ping 127.0.0.1 -n 3 >nul

:OPEN_BROWSER
start "" "http://127.0.0.1:8088/"
exit
