@echo off

echo ======================================================================
echo   KHOI PHUC TOOL V1 VE TRUOC KHI TRIEN KHAI AUTO VOICE MODE
echo ======================================================================
echo.

cd /d "C:\tool v1"

echo Dang khoi phuc ve branch: backup/before-voice-auto-mode-20260924...
git checkout backup/before-voice-auto-mode-20260924 -- backend/batch_processor.py backend/telegram_bot.py backend/voice_selection.py backend/ai/voice_cloning.py backend/main.py backend/tool_control_runtime.py backend/ai/v1_voice_cache.py backend/telegram_queue_monitor.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo KHOI PHUC THANH CONG!
    echo Cac file da duoc dua ve trang thai on dinh truoc khi trien khai auto voice mode.
) else (
    echo.
    echo Thu khoi phuc ve branch backup/before-voice-lock-20260924...
    git checkout backup/before-voice-lock-20260924 -- backend/batch_processor.py backend/telegram_bot.py backend/voice_selection.py backend/ai/voice_cloning.py backend/main.py backend/tool_control_runtime.py backend/ai/v1_voice_cache.py
)

echo.
pause
