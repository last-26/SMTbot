@echo off
REM Start the bot AND wipe halt + daily PnL + streak + peak_balance after
REM journal replay. Use after ANY circuit-breaker stop (daily-loss cooldown,
REM streak halt, OR max_drawdown permanent halt) when positions have been
REM verified and you want to resume trading from the current balance.
REM Re-anchors peak to current_balance so drawdown_pct = 0 going forward.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m src.bot --config config\default.yaml --clear-halt %*
