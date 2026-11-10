@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto failed
if exist "..\release_risk_agent\.venv\Scripts\python.exe" (
  "..\release_risk_agent\.venv\Scripts\python.exe" setup_env.py --launch
  goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 setup_env.py --launch
  goto finished
)
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. See README.md for manual Docker Compose startup.
  goto failed
)
python setup_env.py --launch
:finished
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Startup did not finish. See the error above.
pause
exit /b 1
