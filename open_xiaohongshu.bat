@echo off
chcp 65001 >nul
echo Đang mở trình duyệt Google Chrome với profile Xiaohongshu của bạn...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0data\browser\xiaohongshu" "https://www.rednote.com"
