@echo off
echo ========================================================
echo   Mo trinh duyet Google Chrome voi Profile Douyin
echo ========================================================
echo.
echo Dang mo trinh duyet Douyin tren man hinh cua ban...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0data\browser\douyin" "https://www.douyin.com"
echo.
echo Hay dang nhap tai khoan hoac giai ma keo ghep CAPTCHA (neu co).
echo Sau khi hoan tat, phien lam viec se tu dong luu lai trong profile.
echo.
echo LUU Y: Sau khi dang nhap/xac minh xong, ban hay DONG cua so Chrome nay
echo de he thong tu dong ket noi va tim video tren Web App (http://localhost:3000).
echo.
pause
