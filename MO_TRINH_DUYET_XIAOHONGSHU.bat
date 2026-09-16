@echo off
echo ========================================================
echo   Mo trinh duyet Google Chrome voi Profile Xiaohongshu
echo ========================================================
echo.
echo Dang mo trinh duyet tren man hinh cua ban...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0data\browser\xiaohongshu" "https://www.rednote.com/explore"
echo.
echo Hay dang nhap tai khoan Xiaohongshu / Rednote tren cua so vua mo.
echo Sau khi dang nhap thanh cong, phien dang nhap se tu dong luu lai.
echo.
echo LUU Y: Sau khi dang nhap xong, ban hay DONG cua so Chrome nay
echo de he thong tu dong ket noi va tim video tren Web App (http://localhost:3000).
echo.
pause
