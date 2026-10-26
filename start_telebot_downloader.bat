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
echo   BOT TAI VIDEO DOUYIN ^& XIAOHONGSHU (@douxiao_bot)
echo   Che do: Tu dong chay khi bat may (Scheduled Task: AutoDubV1-telebot-downloader)
echo   Thu muc luu video: D:\vid chua edit
echo =========================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telebot_video_downloader*' -and $_.ProcessId -ne $PID }; if (-not $running) { Start-ScheduledTask -TaskName 'AutoDubV1-telebot-downloader' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Write-Host 'Da kich hoat bot tai video thanh cong!' -ForegroundColor Green } else { Write-Host 'Bot da dang chay ngam san sang!' -ForegroundColor Green }; Write-Host '--- LOG HOAT DONG REALTIME (Nhan Ctrl+C de thoat xem log, bot van chay ngam) ---' -ForegroundColor Cyan; Get-Content '%PROJECT_DIR%telebot_downloader.log' -Tail 15 -Wait"

