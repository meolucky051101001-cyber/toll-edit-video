@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title Ma hoa du lieu Google Sheet

where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Khong tim thay Python trong PATH.
  pause
  exit /b 1
)

if not "%~1"=="" goto :mot_file

echo ============================================
echo   MA HOA DU LIEU GOOGLE SHEET (AES-256-GCM)
echo ============================================
echo.
echo Se ma hoa 2 file trong thu muc secure_sheet:
echo   - survey_full.xlsx
echo   - survey_gid1269863222.csv
echo.
echo Mat khau se duoc hoi o buoc tiep theo va KHONG hien ra man hinh.
echo Hay dung mat khau dai it nhat 8 ky tu va luu lai can than.
echo.
python tools\sheet_crypto.py encrypt "secure_sheet\survey_full.xlsx" "secure_sheet\survey_gid1269863222.csv"
goto :ket_qua

:mot_file
echo ============================================
echo   MA HOA FILE
echo ============================================
echo.
echo File: %~1
echo.
echo Mat khau se duoc hoi o buoc tiep theo va KHONG hien ra man hinh.
echo File .enc se nam ngay canh file goc.
echo.
python tools\sheet_crypto.py encrypt "%~1"

:ket_qua
if errorlevel 1 goto :loi
echo.
echo [OK] Ma hoa xong.
goto :end

:loi
echo.
echo [LOI] Ma hoa that bai. Xem thong bao phia tren.

:end
echo.
pause
