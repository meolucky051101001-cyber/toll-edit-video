@echo off
chcp 65001 >nul
echo ======================================================================
echo 🔄 DANG KHOI PHUC CODEBASE VE BAN AN TOAN GOC (v1.0-stable-baseline)...
echo ======================================================================

git reset --hard v1.0-stable-baseline
git clean -fd

echo.
echo ✅ DA KHOI PHUC THANH CONG VE BAN ON DINH GOC 100%!
pause
