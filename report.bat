@echo off
REM Run the trade journal report. Default window: last 7 days.
REM Pass --last 24h  /  --last 30d  /  --starting-balance 5000 etc.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\report.py %*
