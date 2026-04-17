@echo off
REM Show only ERROR / WARNING lines.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\logs.py --errors %*
