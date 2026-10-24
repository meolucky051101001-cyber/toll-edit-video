@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Smoke Test Live (Douyin + Xiaohongshu)
cd /d "%~dp0"
echo ============================================================
echo   SMOKE TEST LIVE - 1 luot tim that tren Douyin + XHS
echo ============================================================
echo.
echo Script se chay 1 luat tim headless bang phien dang nhap da luu
echo de phat hien som khi Douyin/Xiaohongshu doi DOM hoac API.
echo.
echo Su dung: sua tham so ben duoi de doi chu de hoac so ket qua toi thieu.
echo ------------------------------------------------------------
echo.
.\.venv\Scripts\python.exe scripts\smoke_live.py --platform both --query "unbox do cute" --min-results 3
echo.
pause
