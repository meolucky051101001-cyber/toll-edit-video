@echo off

echo ======================================================================
echo   KHOI PHUC TOOL V1 VE TRUOC KHI TRIEN KHAI KHOA GIONG (VOICE LOCK)
echo ======================================================================
echo.

cd /d "C:\tool v1"

echo Dang khoi phuc ve branch: backup/before-voice-lock-20260924...
git checkout backup/before-voice-lock-20260924 -- backend/batch_processor.py backend/telegram_bot.py backend/voice_selection.py backend/ai/voice_cloning.py backend/main.py backend/tool_control_runtime.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo KHOI PHUC THANH CONG!
    echo Cac file da duoc dua ve trang thai on dinh truoc khi trien khai voice lock.
) else (
    echo.
    echo Co loi xay ra khi checkout. Kiem tra lai branch git.
)

echo.
pause
