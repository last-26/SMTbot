"""Read-only aggregator that builds the dashboard JSON payload.

Opens the journal SQLite file in `?mode=ro` URI mode so we can never write,
then reuses the existing `TradeJournal` reader methods + `reporter.summary`
to produce the single dict the frontend renders.

Post-Arkham purge (2026-05-05): on_chain / whale_transfers panels removed.
Post-Yol-B (2026-05-05): decision_log per-cycle audit + Yol A/B/legacy
strategy breakdown added.

The bot is a separate writer process. Default SQLite journal mode (DELETE)
serializes writers vs. readers, so a brief `SQLITE_BUSY` is possible during
a bot commit; the read-only connection's `timeout=10` rides through it.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import yaml
from dotenv import load_dotenv

from src.journal.database import TradeJournal
from src.journal.models import TradeRecord
from src.journal.reporter import equity_curve, summary


# ── Config helpers (YAML-direct, mirrors scripts/report.py) ──────────────────


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def _load_yaml(path: Optional[Path] = None) -> dict:
    p = path or _CONFIG_PATH
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_db_path(cfg: dict) -> str:
    return (cfg.get("journal") or {}).get("db_path", "data/trades.db")


def resolve_starting_balance(cfg: dict) -> float:
    return float((cfg.get("bot") or {}).get("starting_balance", 10_000.0))


def resolve_clean_since(cfg: dict) -> Optional[datetime]:
    raw = (cfg.get("rl") or {}).get("clean_since")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_symbols(cfg: dict) -> list[str]:
    raw = (cfg.get("trading") or {}).get("symbols") or []
    return [str(s) for s in raw if s]


def load_dashboard_config(path: Optional[Path] = None) -> dict:
    """Returns {db_path, starting_balance, clean_since, symbols, bybit} —
    fields the dashboard needs. Skips `BotConfig.load_config()` (which would
    require full schema validation) and loads only what the read-only
    dashboard uses.

    `bybit` block is None when credentials are missing — the dashboard then
    falls back to the simulated journal balance instead of querying Bybit.
    """
    cfg = _load_yaml(path)
    return {
        "db_path": resolve_db_path(cfg),
        "starting_balance": resolve_starting_balance(cfg),
        "clean_since": resolve_clean_since(cfg),
        "symbols": resolve_symbols(cfg),
        "bybit": resolve_bybit_credentials(cfg),
    }


def resolve_bybit_credentials(cfg: dict) -> Optional[dict]:
    """Read Bybit creds from .env (BYBIT_API_KEY / BYBIT_API_SECRET / BYBIT_DEMO).
    YAML `bybit:` block supplies non-secret defaults (account_type, category).
    Returns None when the API key/secret are not set so the dashboard can
    silently skip the live wallet probe.
    """
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    api_key = os.environ.get("BYBIT_API_KEY", "").strip()
    api_secret = os.environ.get("BYBIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return None
    yaml_block = cfg.get("bybit") or {}
    demo_env = os.environ.get("BYBIT_DEMO")
    demo = (demo_env != "0") if demo_env is not None else bool(yaml_block.get("demo", True))
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "demo": demo,
        "account_type": yaml_block.get("account_type", "UNIFIED"),
        "category": yaml_block.get("category", "linear"),
    }


# ── Read-only journal wrapper ────────────────────────────────────────────────


class ReadOnlyJournal(TradeJournal):
    """Subclass that opens the DB read-only and skips the schema/migration
    script — the bot is the schema owner, the dashboard is a passive reader.
    """

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self._db_path == ":memory:":
            self._conn = await aiosqlite.connect(":memory:")
        else:
            # Forward slashes on Windows are fine in a SQLite URI.
            uri = "file:" + self._db_path.replace("\\", "/") + "?mode=ro"
            self._conn = await aiosqlite.connect(uri, uri=True, timeout=10)
        self._conn.row_factory = aiosqlite.Row


# ── Payload builder ──────────────────────────────────────────────────────────


_RECENT_CLOSED_LIMIT = 50
_RECENT_REJECTED_LIMIT = 50
_RECENT_DECISION_LOG_LIMIT = 60


def _trade_dump(t: TradeRecord) -> dict:
    return t.model_dump(mode="json")


def _equity_points(closed: list[TradeRecord], starting_balance: float) -> list[dict]:
    """Turn `equity_curve` (list of running balances after each closed trade)
    into a list of {trade_id, exit_ts, balance, cum_r} so the chart can plot
    by exit time. `equity_curve` returns N+1 points (starting + after each
    trade); we drop the leading starting point and zip with the trades.
    """
    curve = equity_curve(closed, starting_balance)
    if len(curve) <= 1 or not closed:
        return []
    points: list[dict] = []
    cum_r = 0.0
    for t, bal in zip(closed, curve[1:]):
        cum_r += float(t.pnl_r or 0.0)
        exit_ts = t.exit_timestamp.isoformat() if t.exit_timestamp else None
        points.append({
            "trade_id": t.trade_id,
            "exit_ts": exit_ts,
            "balance": round(float(bal), 4),
            "cum_r": round(cum_r, 4),
        })
    return points


_WALLET_CACHE: dict = {"ts": 0.0, "data": None}
_WALLET_TTL_S = 60.0


def _build_bybit_client(bybit_cfg: dict):
    from src.execution.bybit_client import BybitClient, BybitCredentials
    creds = BybitCredentials(
        api_key=bybit_cfg["api_key"],
        api_secret=bybit_cfg["api_secret"],
        demo=bool(bybit_cfg.get("demo", True)),
        account_type=bybit_cfg.get("account_type", "UNIFIED"),
        category=bybit_cfg.get("category", "linear"),
    )
    return BybitClient(creds, allow_live=not creds.demo), creds


async def fetch_wallet(bybit_cfg: Optional[dict]) -> Optional[dict]:
    """Probe Bybit for live wallet balance with a 60s TTL cache.

    Wallet doesn't change between cycles unless an order fills, so polling
    once a minute is plenty. Returns None on any failure (missing creds,
    network error, demo edge unreachable) — dashboard degrades to the
    simulated journal balance.

    Runs in a thread because `pybit` is sync and we don't want to block
    the FastAPI event loop on the network round-trip.
    """
    if not bybit_cfg:
        return None
    now = asyncio.get_event_loop().time()
    if _WALLET_CACHE["data"] is not None and now - _WALLET_CACHE["ts"] < _WALLET_TTL_S:
        return _WALLET_CACHE["data"]
    try:
        from src.execution.bybit_client import BybitClient  # noqa: F401
    except Exception:
        return None

    def _probe() -> dict:
        client, creds = _build_bybit_client(bybit_cfg)
        return {
            "available_usd": float(client.get_balance("USDT")),
            "margin_balance_usd": float(client.get_total_equity("USDT")),
            "demo": bool(creds.demo),
        }

    try:
        data = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=8.0)
    except Exception:
        return _WALLET_CACHE["data"]  # serve stale on transient failure
    _WALLET_CACHE["ts"] = now
    _WALLET_CACHE["data"] = data
    return data


async def fetch_live_positions(bybit_cfg: Optional[dict]) -> Optional[list[dict]]:
    """Live snapshot of every USDT-linear position on Bybit. Returns a list of
    {inst_id, pos_side, mark_price, entry_price, unrealized_pnl_usd, size,
    leverage} keyed for frontend merge against the journal's open trades.

    No cache — operator wants live UPnL ticking every 10s. Position endpoint
    sits in its own rate-limit bucket on Bybit V5; 1 call / 10s is comfortable.
    Returns None on any failure so the frontend falls back to the journal's
    last `position_snapshots` row.
    """
    if not bybit_cfg:
        return None
    try:
        from src.execution.bybit_client import BybitClient  # noqa: F401
    except Exception:
        return None

    def _probe() -> list[dict]:
        client, _creds = _build_bybit_client(bybit_cfg)
        snaps = client.get_positions()
        out: list[dict] = []
        for s in snaps:
            if not s.size or float(s.size) <= 0:
                continue
            out.append({
                "inst_id": s.inst_id,
                "pos_side": s.pos_side,
                "size": float(s.size),
                "entry_price": float(s.entry_price),
                "mark_price": float(s.mark_price),
                "unrealized_pnl_usd": float(s.unrealized_pnl),
                "leverage": int(s.leverage),
            })
        return out

    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe), timeout=6.0)
    except Exception:
        return None


# ── Decision log + strategy breakdown (Yol B per-cycle audit) ────────────────


_DECISION_LOG_FIELDS: tuple[str, ...] = (
    "id", "timestamp", "symbol", "decision", "decision_reason",
    "entry_path", "major_reversal_score", "continuation_score",
    "micro_reversal_score", "confluence_score", "trend_regime",
    "ha_color_3m", "ha_color_15m", "ha_streak_3m", "ha_streak_15m",
    "price", "atr_14", "session", "vwap_3m_side",
)


async def fetch_decision_log_recent(
    db_path: str,
    *,
    limit: int = _RECENT_DECISION_LOG_LIMIT,
) -> list[dict]:
    """Read last N rows from `decision_log`, newest first.

    Compact projection (19 of 40 columns) — the dashboard only needs
    decision + entry_path + 3 scores + light context. Full row inspection
    goes through the DB browser overlay.

    Returns [] when the table is empty or the DB is missing.
    """
    cols = ", ".join(_DECISION_LOG_FIELDS)
    sql = (
        f"SELECT {cols} FROM decision_log "
        f"ORDER BY timestamp DESC, id DESC "
        f"LIMIT ?"
    )
    out: list[dict] = []
    try:
        async with ReadOnlyJournal(db_path) as j:
            conn = j._conn
            assert conn is not None
            async with conn.execute(sql, (int(limit),)) as cur:
                async for row in cur:
                    out.append({k: _db_cell(row[k]) for k in _DECISION_LOG_FIELDS})
    except Exception:
        return []
    return out


def _strategy_label(t: TradeRecord) -> str:
    """Classify a closed trade by its entry strategy.

    is_vmc_strategy = 1   → "vmc"     (Yol B HA Strategy, post-2026-05-05)
    is_ha_native    = 1   → "ha"      (Yol A HA-native, 2026-05-04 → 2026-05-05)
    both NULL/0           → "legacy"  (pre-Yol-A 5-pillar zone-based)
    """
    if getattr(t, "is_vmc_strategy", None):
        return "vmc"
    if getattr(t, "is_ha_native", None):
        return "ha"
    return "legacy"


def _build_strategy_breakdown(closed: list[TradeRecord]) -> dict:
    """Group closed trades by strategy label and emit per-bucket stats:
    count, wins, losses, breakeven, win_rate (decimal 0..1), net_r, gross_r.

    Always includes vmc/ha/legacy keys, even if empty, so the frontend can
    render a stable 3-row table.
    """
    buckets: dict[str, dict] = {
        k: {"count": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "net_r": 0.0, "gross_r_wins": 0.0, "gross_r_losses": 0.0}
        for k in ("vmc", "ha", "legacy")
    }
    for t in closed:
        b = buckets[_strategy_label(t)]
        b["count"] += 1
        r = float(t.pnl_r or 0.0)
        b["net_r"] += r
        if r > 0:
            b["wins"] += 1
            b["gross_r_wins"] += r
        elif r < 0:
            b["losses"] += 1
            b["gross_r_losses"] += r
        else:
            b["breakeven"] += 1
    out: dict[str, dict] = {}
    for k, b in buckets.items():
        decided = b["wins"] + b["losses"]
        wr = (b["wins"] / decided) if decided > 0 else None
        out[k] = {
            "count": b["count"],
            "wins": b["wins"],
            "losses": b["losses"],
            "breakeven": b["breakeven"],
            "win_rate": wr,
            "net_r": round(b["net_r"], 4),
            "gross_r_wins": round(b["gross_r_wins"], 4),
            "gross_r_losses": round(b["gross_r_losses"], 4),
        }
    return out


async def build_dashboard_state(
    db_path: str,
    starting_balance: float,
    *,
    clean_since: Optional[datetime] = None,
    symbols: Optional[list[str]] = None,
    bybit_cfg: Optional[dict] = None,
) -> dict:
    """One-shot aggregator: opens RO journal, runs all reads, returns dict.

    `clean_since` filters closed-trade summaries (matches the rest of the
    tooling's reporting window). Open positions and live snapshots ignore it
    — operator wants to see currently held positions regardless of cutoff.
    `bybit_cfg` enables a live wallet probe; None falls back to journal-only.
    """
    wallet_task = asyncio.create_task(fetch_wallet(bybit_cfg))
    live_positions_task = asyncio.create_task(fetch_live_positions(bybit_cfg))
    decision_log_task = asyncio.create_task(fetch_decision_log_recent(db_path))

    async with ReadOnlyJournal(db_path) as j:
        closed = await j.list_closed_trades(since=clean_since)
        open_trades = await j.list_open_trades()
        rejected = await j.list_rejected_signals(since=clean_since)

        open_payload: list[dict] = []
        for t in open_trades:
            snaps = await j.get_position_snapshots(t.trade_id)
            latest = snaps[-1].model_dump(mode="json") if snaps else None
            open_payload.append({
                "trade": _trade_dump(t),
                "latest_snapshot": latest,
            })

    summary_dict = summary(closed, starting_balance)
    eq_points = _equity_points(closed, starting_balance)
    strategy_breakdown = _build_strategy_breakdown(closed)

    closed_recent = [_trade_dump(t) for t in closed[-_RECENT_CLOSED_LIMIT:]]
    closed_recent.reverse()  # newest first for table display

    rejected_recent = [r.model_dump(mode="json") for r in rejected[-_RECENT_REJECTED_LIMIT:]]
    rejected_recent.reverse()

    reject_counts = Counter(r.reject_reason for r in rejected)

    wallet = await wallet_task
    live_positions = await live_positions_task
    decision_log_recent = await decision_log_task

    return _sanitize_floats({
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "starting_balance": starting_balance,
        "clean_since": clean_since.isoformat() if clean_since else None,
        "symbols": symbols or [],
        "summary": summary_dict,
        "strategy_breakdown": strategy_breakdown,
        "equity_curve": eq_points,
        "open_positions": open_payload,
        "closed_trades_recent": closed_recent,
        "rejected_recent": rejected_recent,
        "reject_reason_counts": dict(reject_counts.most_common()),
        "decision_log_recent": decision_log_recent,
        "wallet": wallet,
        "live_positions": live_positions,
        "counts": {
            "closed_total": len(closed),
            "open_total": len(open_trades),
            "rejected_total": len(rejected),
        },
    })


def _sanitize_floats(obj: Any) -> Any:
    """Recursively replace inf / -inf / NaN with None.

    FastAPI's JSONResponse uses `json.dumps(allow_nan=False)` and rejects
    non-finite floats — `reporter.summary` legitimately returns
    `profit_factor=inf` when there are no losses, which would 500 the
    `/api/state` endpoint. Walk the payload once at the boundary and swap
    non-finite floats for null so the frontend just sees a missing value.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_floats(v) for v in obj)
    return obj


# ── Generic DB browser (read-only, whitelist-only) ───────────────────────────
#
# Powers the "Database" overlay in the dashboard. Each entry maps a SQL
# table to (default ORDER BY column, descending). Whitelist is closed —
# unknown table names are rejected at the endpoint boundary so a typo / URL
# probe can't pivot into arbitrary SQL.
_DB_BROWSER_TABLES: dict[str, tuple[str, bool]] = {
    "trades":             ("entry_timestamp",  True),
    "rejected_signals":   ("signal_timestamp", True),
    "position_snapshots": ("captured_at",      True),
    "decision_log":       ("timestamp",        True),
    # Legacy archive tables — kept for inspection of pre-purge data, no live
    # writers as of 2026-05-05.
    "on_chain_snapshots": ("captured_at",      True),
    "whale_transfers":    ("captured_at",      True),
}

_DB_BROWSER_DEFAULT_LIMIT = 200
_DB_BROWSER_MAX_LIMIT = 2000


async def list_db_tables(db_path: str) -> list[dict]:
    """Return [{name, row_count, order_by, order_desc}] for each whitelist
    table. Row count via `SELECT COUNT(*)`; cheap on the indexes we already
    have. Tables that don't exist (older DB before a migration) yield
    `row_count=None` so the UI can render them as missing without 500'ing.
    """
    out: list[dict] = []
    async with ReadOnlyJournal(db_path) as j:
        conn = j._conn
        assert conn is not None
        for name, (order_by, order_desc) in _DB_BROWSER_TABLES.items():
            try:
                async with conn.execute(f"SELECT COUNT(*) FROM {name}") as cur:
                    row = await cur.fetchone()
                    rc = int(row[0]) if row else 0
            except Exception:
                rc = None
            out.append({
                "name": name,
                "row_count": rc,
                "order_by": order_by,
                "order_desc": order_desc,
            })
    return out


async def fetch_db_rows(
    db_path: str,
    table: str,
    *,
    limit: int = _DB_BROWSER_DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Return {columns: [...], rows: [[...], ...], total, limit, offset}
    for a whitelisted table, ordered by its registered timestamp column
    DESC so the most recent rows land first.

    Whitelist guard is the only defence against arbitrary table names —
    `table` MUST be a key of `_DB_BROWSER_TABLES`. We still parameterize
    LIMIT/OFFSET via aiosqlite bindings even though they're ints, since
    that keeps the SQL builder uniform.
    """
    if table not in _DB_BROWSER_TABLES:
        raise ValueError(f"unknown table: {table!r}")
    order_by, order_desc = _DB_BROWSER_TABLES[table]
    limit = max(1, min(int(limit), _DB_BROWSER_MAX_LIMIT))
    offset = max(0, int(offset))
    direction = "DESC" if order_desc else "ASC"

    async with ReadOnlyJournal(db_path) as j:
        conn = j._conn
        assert conn is not None
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            r = await cur.fetchone()
            total = int(r[0]) if r else 0
        sql = (
            f"SELECT * FROM {table} "
            f"ORDER BY {order_by} {direction} "
            f"LIMIT ? OFFSET ?"
        )
        async with conn.execute(sql, (limit, offset)) as cur:
            cols = [d[0] for d in (cur.description or [])]
            raw = await cur.fetchall()
        rows = [[_db_cell(v) for v in row] for row in raw]
    return {
        "table": table,
        "columns": cols,
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "order_by": order_by,
        "order_desc": order_desc,
    }


def _db_cell(v: Any) -> Any:
    """Coerce a raw SQLite cell to JSON-safe primitives. Non-finite floats
    (inf / -inf / NaN) → None so FastAPI's strict JSON encoder doesn't
    explode. bytes → hex string (rare; sqlite blobs aren't expected on
    these tables but defensive). Everything else passes through.
    """
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v
