@echo off
chcp 65001 >nul
echo ========================================================
echo   ÁP DỤNG BẢN SỬA LỖI DỊCH THUẬT VÀO TOOL V1
echo ========================================================

echo 1. Đang sao lưu file gốc Tool V1...
if not exist "C:\tool v1\backend\ai\translation.py.backup_original" (
    copy "C:\tool v1\backend\ai\translation.py" "C:\tool v1\backend\ai\translation.py.backup_original" >nul
)
if not exist "C:\tool v1\backend\ai\v1_translation_budget.py.backup_original" (
    copy "C:\tool v1\backend\ai\v1_translation_budget.py" "C:\tool v1\backend\ai\v1_translation_budget.py.backup_original" >nul
)

echo 2. Đang dọn dẹp các cache dịch thuật hỏng...
"C:\tool v2\backend\venv\Scripts\python.exe" "C:\tool v2\fixes_for_v1\clean_corrupted_cache.py"

echo 3. Cập nhật cấu hình GEMINI_MODEL sang gemini-3.6-flash trong .env...
powershell -Command "(Get-Content 'C:\tool v1\backend\.env') -replace 'GEMINI_MODEL=.*', 'GEMINI_MODEL=gemini-3.6-flash' | Set-Content 'C:\tool v1\backend\.env' -Encoding utf8"

echo ========================================================
echo   ĐÃ ÁP DỤNG THÀNH CÔNG!
echo   Để hoàn tác về ban đầu, hãy chạy rollback_tool_v1.bat
echo ========================================================
pause
