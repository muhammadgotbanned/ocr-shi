@echo off
rem Warraq - one-click installer for Windows. Double-click this file.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
if errorlevel 1 (
  echo.
  echo   Installation failed. Scroll up for the error, then try again.
  pause
  exit /b 1
)
echo.
set /p START=  Start Warraq now? [Y/n]
if /i "%START%"=="n" exit /b 0
start "" "%~dp0.venv\Scripts\pythonw.exe" -m warraq
