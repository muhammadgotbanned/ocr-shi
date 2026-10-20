@echo off
rem Start Warraq with a console window that shows the log (useful for troubleshooting).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m warraq -v %*
pause
