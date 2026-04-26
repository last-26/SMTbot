@echo off
REM Run the read-only trade journal dashboard at http://127.0.0.1:8765/
REM Pass extra flags through: dashboard.bat --port 9000  /  dashboard.bat --host 0.0.0.0
cd /d "%~dp0"
".venv\Scripts\python.exe" -m src.dashboard --config config\default.yaml %*
echo.
echo Dashboard stopped (exit code %ERRORLEVEL%). Press any key to close.
pause >nul
