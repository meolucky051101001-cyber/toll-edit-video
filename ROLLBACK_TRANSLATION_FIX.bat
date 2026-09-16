@echo off
chcp 65001 >nul
echo ============================================================
echo   KHOI PHUC TOOL V1 VE TRANG THAI GOC (ROLLBACK CHECKPOINT)
echo ============================================================
echo.
echo Dang khoi phuc backend/ai/v1_translation_cache.py va backend/ai/translation.py ve commit 8c7b99a...
cd /d "C:\tool v1"
git checkout 8c7b99a -- backend/ai/v1_translation_cache.py backend/ai/translation.py
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [THANH CONG] Da khoi phuc toan bo code dich thuat ve trang thai ban dau!
) else (
    echo.
    echo [CANH BAO] Khong the checkout tu git, kiem tra lai trang thai kho luu tru.
)
echo.
pause
