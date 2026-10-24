@echo off
chcp 65001 >nul
:: =========================================================================
:: DỪNG TELEGRAM BOT TẢI VIDEO DOUYIN & XIAOHONGSHU
:: Chỉ dừng tiến trình telebot_video_downloader.py, không ảnh hưởng Tool V1
:: =========================================================================

echo Dang tim va tat tien trinh telebot_video_downloader.py...
wmic process where "CommandLine like '%%telebot_video_downloader.py%%' and not CommandLine like '%%wmic%%'" call terminate >nul 2>&1
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telebot_video_downloader.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo Da tat bot tai video thanh cong!
timeout /t 2 /nobreak >nul
