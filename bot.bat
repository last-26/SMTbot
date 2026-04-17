@echo off
REM Start the bot normally (resumes from journal state, respects existing halts).
REM Pass extra flags through: bot.bat --dry-run --once  /  bot.bat --max-closed-trades 50
cd /d "%~dp0"
".venv\Scripts\python.exe" -m src.bot --config config\default.yaml %*
