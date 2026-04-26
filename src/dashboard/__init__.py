"""Single-page real-time dashboard over the trade journal SQLite DB.

Read-only sibling process: opens `data/trades.db` with `?mode=ro` and serves
a FastAPI app that aggregates closed trades, open positions, latest position
snapshots, rejected signals, on-chain state, and whale transfers into one
JSON payload. The bundled `static/index.html` polls that endpoint every 5 s
and renders everything on a single scrollable page.
"""
