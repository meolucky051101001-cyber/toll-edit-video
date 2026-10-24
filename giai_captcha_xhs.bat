@echo off
chcp 65001 >nul
title Giai CAPTCHA Xiaohongshu
cd /d "%~dp0"
echo ============================================================
echo   MO TRINH DUYET XIAOHONGSHU DE GIAI CAPTCHA / DANG NHAP
echo ============================================================
echo.
echo Cua so Chromium dang duoc khoi dong...
echo Vui long keo thanh truot hoac dang nhap tren cua so vua hien len.
echo.
.\.venv\Scripts\python.exe scripts\solve_xhs_captcha.py
echo.
pause
