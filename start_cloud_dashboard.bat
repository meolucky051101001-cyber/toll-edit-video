@echo off
setlocal
if not defined AUTODUB_CLOUD_OUTPUT_DIR set "AUTODUB_CLOUD_OUTPUT_DIR=D:\ToolV1_Cloud"
start "" "%SystemRoot%\System32\wscript.exe" "%~dp0start_cloud_dashboard.vbs"
exit /b %ERRORLEVEL%
