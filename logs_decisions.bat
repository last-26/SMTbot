@echo off
REM Show only entry/exit decisions (PLANNED, NO_TRADE, fills, rejects).
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\logs.py --decisions %*
