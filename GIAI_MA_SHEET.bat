@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
title Giai ma du lieu Google Sheet

where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Khong tim thay Python trong PATH.
  pause
  exit /b 1
)

if not "%~1"=="" goto :mot_file

echo ============================================
echo   GIAI MA DU LIEU GOOGLE SHEET
echo ============================================
echo.
echo File sau khi giai ma se nam trong: secure_sheet\giai_ma
echo.
python tools\sheet_crypto.py decrypt "secure_sheet\survey_full.xlsx.enc" "secure_sheet\survey_gid1269863222.csv.enc" --out-dir "secure_sheet\giai_ma"
goto :ket_qua

:mot_file
echo ============================================
echo   GIAI MA FILE
echo ============================================
echo.
echo File: %~1
echo File giai ma se nam trong thu muc "giai_ma" cung cho voi file .enc.
echo.
python tools\sheet_crypto.py decrypt "%~1" --out-dir "%~dp1giai_ma"

:ket_qua
if errorlevel 1 goto :loi
echo.
echo [OK] Giai ma xong.
goto :end

:loi
echo.
echo [LOI] Giai ma that bai (sai mat khau hoac file da bi sua).

:end
echo.
pause
