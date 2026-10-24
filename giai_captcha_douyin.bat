@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Dang Nhap & Luu Phien Douyin (Giai Phap 1)
cd /d "%~dp0"
echo ============================================================
echo   DANG NHAP DOUYIN & LUU PHIEN BEN VUNG (GIAI PHAP 1)
echo ============================================================
echo.
echo Cua so Microsoft Edge dang duoc mo tren man hinh...
echo.
echo 1. Neu thay manh ghep bao mat (CAPTCHA):
echo    Hay dung chuot keo thanh truot tren cua so Edge de ghep hinh.
echo.
echo 2. Khi ma QR hien len:
echo    Dung app Douyin tren dien thoai quet ma va bam Xac nhan dang nhap.
echo.
echo He thong se tu dong phat hien, luu auth_state.json va file .env!
echo ------------------------------------------------------------
echo.
.\.venv\Scripts\python.exe scripts\solve_douyin_captcha.py
echo.
pause
