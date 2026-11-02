@echo off
setlocal
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "PYTHON_EXE=%PROJECT_DIR%\backend\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

rem Stop only verified telebot_video_downloader.py process without touching Tool V1 services
echo Dang tim va tat tien trinh telebot_video_downloader.py...
"%PYTHON_EXE%" -B "%PROJECT_DIR%\backend\v1_shutdown.py" --scope downloader --project-root "%PROJECT_DIR%" %*
exit /b %ERRORLEVEL%
