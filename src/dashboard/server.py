"""FastAPI app for the trade journal dashboard.

`GET /`            → serves `static/index.html` (single-page UI).
`GET /api/state`   → JSON payload built by `state.build_dashboard_state`.

The app object is created via `create_app(config_path)` so the entry point
in `__main__.py` can pass through a custom config path. Settings are read
once at startup; the DB is opened fresh per request (cheap on a 5 MB file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.dashboard.state import build_dashboard_state, load_dashboard_config


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config_path: Optional[Path] = None) -> FastAPI:
    cfg = load_dashboard_config(config_path)
    db_path = cfg["db_path"]
    starting_balance = cfg["starting_balance"]
    clean_since = cfg["clean_since"]
    bybit_cfg = cfg["bybit"]

    app = FastAPI(
        title="SMTbot trade dashboard",
        description="Read-only consolidated view over the trade journal.",
        version="1.0.0",
    )

    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        try:
            payload = await build_dashboard_state(
                db_path,
                starting_balance,
                clean_since=clean_since,
                bybit_cfg=bybit_cfg,
            )
            return JSONResponse(payload)
        except Exception as e:
            return JSONResponse(
                {"error": type(e).__name__, "message": str(e)},
                status_code=500,
            )

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "db_path": db_path,
            "starting_balance": starting_balance,
            "clean_since": clean_since.isoformat() if clean_since else None,
        }

    return app
