@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem ============================================================
rem P8 two-pass runner: filter out GPU/clock flaky tests.
rem Pass 1: full tests/ run. Pass 2: re-run only failed (--lf).
rem   Fail BOTH passes = real regression -> fix before commit.
rem   Pass on 2nd run  = flaky (GPU frame counts / disk cache).
rem ============================================================

set PY=%~dp0..\backend\venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [1/2] Running full tests/ ...
"%PY%" -m pytest tests/ -q --tb=no

echo [2/2] Re-running failed tests only (--lf) to filter flaky ...
"%PY%" -m pytest tests/ --lf -q --tb=short
if errorlevel 1 (
  echo.
  echo [RESULT] Test still fails after re-run = REAL regression. Fix before commit!
  exit /b 1
)
echo.
echo [RESULT] Suite green - pass-1 failures that pass on re-run are GPU/clock flaky.
endlocal
