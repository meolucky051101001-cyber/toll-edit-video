@echo off
echo Dang tat toan bo he thong Tool V1 (Port 8088, 8090, Telegram Bot, Background Service)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tool v1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Da tat sach toan bo tien trinh Tool V1 an toan!
timeout /t 2 > NUL
