@echo off
chcp 65001 >nul
title Bot Tai Video Douyin va Xiaohongshu (Doc Lap)
:: =========================================================================
:: KHỞI ĐỘNG TELEGRAM BOT TẢI VIDEO DOUYIN & XIAOHONGSHU
:: Hoàn toàn độc lập với Tool V1 (không ảnh hưởng luồng dubbing / dashboard)
:: =========================================================================

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=%PROJECT_DIR%backend\venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Khong tim thay Python tai backend\venv\Scripts\python.exe
    pause
    exit /b 1
)

echo =========================================================================
echo   DANG KHOI DONG BOT TAI VIDEO DOUYIN ^& XIAOHONGSHU
echo   Bot Username: @douxiao_bot
echo   Trang thai: Doc lap hoan toan voi Tool V1
echo =========================================================================
echo.

cd /d "%PROJECT_DIR%"
"%PYTHON_EXE%" telebot_video_downloader.py

pause
