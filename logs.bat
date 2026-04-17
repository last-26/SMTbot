@echo off
REM Follow logs/bot.log live (default: 50 lines tail + tail -F).
REM Pass extra flags: logs.bat --decisions  /  logs.bat --errors  /  logs.bat --filter SOL
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\logs.py %*
