@echo off
setlocal
cd /d "%~dp0"
if exist "..\release_risk_agent\.venv\Scripts\python.exe" (
  "..\release_risk_agent\.venv\Scripts\python.exe" run_workbench.py
) else (
  python run_workbench.py
)
if errorlevel 1 pause
