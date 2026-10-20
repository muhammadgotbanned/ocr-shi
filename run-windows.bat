@echo off
rem Start Warraq without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Warraq is not installed yet. Run install-windows.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m warraq %*
