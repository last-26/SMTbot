"""Read-only aggregator that builds the dashboard JSON payload.

Opens the journal SQLite file in `?mode=ro` URI mode so we can never write,
then reuses the existing `TradeJournal` reader methods + `reporter.summary`
to produce the single dict the frontend renders.

The bot is a separate writer process. Default SQLite journal mode (DELETE)
serializes writers vs. readers, so a brief `SQLITE_BUSY` is possible during
a bot commit; the read-only connection's `timeout=10` rides through it.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
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


def load_dashboard_config(path: Optional[Path] = None) -> dict:
    """Returns {db_path, starting_balance, clean_since, bybit} — fields the
    dashboard needs. Skips `BotConfig.load_config()` (which would require
    full schema validation) and loads only what the read-only dashboard uses.

    `bybit` block is None when credentials are missing — the dashboard then
    falls back to the simulated journal balance instead of querying Bybit.
    """
    cfg = _load_yaml(path)
    return {
        "db_path": resolve_db_path(cfg),
        "starting_balance": resolve_starting_balance(cfg),
        "clean_since": resolve_clean_since(cfg),
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
_RECENT_WHALE_LIMIT = 25


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

    No cache — operator wants live UPnL ticking every 5s. Position endpoint
    sits in its own rate-limit bucket on Bybit V5; 1 call / 5s is comfortable.
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


async def build_dashboard_state(
    db_path: str,
    starting_balance: float,
    *,
    clean_since: Optional[datetime] = None,
    bybit_cfg: Optional[dict] = None,
) -> dict:
    """One-shot aggregator: opens RO journal, runs all reads, returns dict.

    `clean_since` filters closed-trade summaries (matches the rest of the
    tooling's reporting window). Open positions and live snapshots ignore it
    — operator wants to see currently held positions regardless of cutoff.
    `bybit_cfg` enables a live wallet probe; None falls back to journal-only.
    """
    on_chain_24h_since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    wallet_task = asyncio.create_task(fetch_wallet(bybit_cfg))
    live_positions_task = asyncio.create_task(fetch_live_positions(bybit_cfg))

    async with ReadOnlyJournal(db_path) as j:
        closed = await j.list_closed_trades(since=clean_since)
        open_trades = await j.list_open_trades()
        rejected = await j.list_rejected_signals(since=clean_since)
        whales = await j.list_whale_transfers(since=clean_since)
        on_chain_rows = await j.list_on_chain_snapshots(since=clean_since)
        on_chain_24h = await j.list_on_chain_snapshots(since=on_chain_24h_since)

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

    closed_recent = [_trade_dump(t) for t in closed[-_RECENT_CLOSED_LIMIT:]]
    closed_recent.reverse()  # newest first for table display

    rejected_recent = [r.model_dump(mode="json") for r in rejected[-_RECENT_REJECTED_LIMIT:]]
    rejected_recent.reverse()

    reject_counts = Counter(r.reject_reason for r in rejected)

    whale_recent = [w.model_dump(mode="json") for w in whales[-_RECENT_WHALE_LIMIT:]]
    whale_recent.reverse()

    on_chain_latest = on_chain_rows[-1] if on_chain_rows else None
    if on_chain_latest is not None:
        on_chain_latest = _normalize_on_chain_row(on_chain_latest)

    on_chain_series = _build_on_chain_series(on_chain_24h)
    on_chain_candles = _build_exchange_candles_24h(on_chain_24h)
    on_chain_per_asset = _build_per_venue_per_asset_series_24h(on_chain_24h)
    on_chain_aggregate_per_asset = _build_aggregate_per_asset_series_24h(on_chain_24h)
    wallet = await wallet_task
    live_positions = await live_positions_task

    return _sanitize_floats({
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "starting_balance": starting_balance,
        "clean_since": clean_since.isoformat() if clean_since else None,
        "summary": summary_dict,
        "equity_curve": eq_points,
        "open_positions": open_payload,
        "closed_trades_recent": closed_recent,
        "rejected_recent": rejected_recent,
        "reject_reason_counts": dict(reject_counts.most_common()),
        "on_chain_latest": on_chain_latest,
        "on_chain_series_24h": on_chain_series,
        "on_chain_candles_24h": on_chain_candles,
        "on_chain_per_venue_per_asset_24h": on_chain_per_asset,
        "on_chain_aggregate_per_asset_24h": on_chain_aggregate_per_asset,
        "whale_transfers_recent": whale_recent,
        "wallet": wallet,
        "live_positions": live_positions,
        "counts": {
            "closed_total": len(closed),
            "open_total": len(open_trades),
            "rejected_total": len(rejected),
            "whale_total": len(whales),
            "on_chain_snapshots_total": len(on_chain_rows),
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


def _build_on_chain_series(rows: list[dict]) -> dict:
    """Slice the last-24h on_chain_snapshots rows into per-metric arrays
    suitable for Chart.js. Only keeps non-null values per metric so the
    line plots don't draw zero-trough gaps when a column is NULL.
    """
    series = {
        "btc_netflow_24h": [],
        "eth_netflow_24h": [],
        "stablecoin_pulse_1h": [],
    }
    for r in rows:
        ts = r.get("captured_at")
        if not ts:
            continue
        btc = r.get("cex_btc_netflow_24h_usd")
        eth = r.get("cex_eth_netflow_24h_usd")
        stb = r.get("stablecoin_pulse_1h_usd")
        if btc is not None:
            series["btc_netflow_24h"].append({"ts": ts, "v": float(btc)})
        if eth is not None:
            series["eth_netflow_24h"].append({"ts": ts, "v": float(eth)})
        if stb is not None:
            series["stablecoin_pulse_1h"].append({"ts": ts, "v": float(stb)})
    return series


_EXCHANGE_NETFLOW_FIELDS: tuple[tuple[str, str], ...] = (
    ("coinbase", "cex_coinbase_netflow_24h_usd"),
    ("binance", "cex_binance_netflow_24h_usd"),
    ("bybit", "cex_bybit_netflow_24h_usd"),
    ("bitfinex", "cex_bitfinex_netflow_24h_usd"),
    ("kraken", "cex_kraken_netflow_24h_usd"),
    ("okx", "cex_okx_netflow_24h_usd"),
)


def _build_exchange_candles_24h(rows: list[dict]) -> dict:
    """Bucket on_chain_snapshots into 96 \u00d7 15min OHLC candles per named CEX.

    The underlying value is `cex_<venue>_netflow_24h_usd` \u2014 a *rolling 24h*
    aggregate sampled every \u22485 min by the writer. So each candle's OHLC
    represents how that rolling sum drifted within the 15-min slot:
      open  = first sample's value, close = last sample's value,
      high  = max sample, low = min sample.
    Empty slots emit `null` so the frontend can render a gap while keeping
    a stable 24h X-axis (96 evenly-spaced slots, ending at the current
    UTC quarter-hour). Color (frontend): green if close > open
    (rolling-sum trending up = inflow accelerating), red otherwise.
    """
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    end_ms = (end_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
    slot_ms = 15 * 60 * 1000

    parsed: list[tuple[int, dict]] = []
    for r in rows or []:
        ts = r.get("captured_at")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            parsed.append((int(t.timestamp() * 1000), r))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda p: p[0])

    out: dict[str, list[dict]] = {label: [] for label, _ in _EXCHANGE_NETFLOW_FIELDS}
    for i in range(96):
        start = end_ms - (95 - i) * slot_ms
        end = start + slot_ms
        bucket = [(ms, row) for ms, row in parsed if start <= ms < end]
        ts_iso = datetime.fromtimestamp(start / 1000, tz=timezone.utc).isoformat()
        for label, field in _EXCHANGE_NETFLOW_FIELDS:
            vals = [float(row[field]) for _, row in bucket
                    if row.get(field) is not None]
            if not vals:
                out[label].append({"ts": ts_iso, "o": None, "h": None,
                                    "l": None, "c": None})
                continue
            out[label].append({
                "ts": ts_iso,
                "o": vals[0],
                "h": max(vals),
                "l": min(vals),
                "c": vals[-1],
            })
    return out


_JSON_DICT_KEYS = (
    "token_volume_1h_net_usd_json",
    "cex_per_venue_btc_netflow_24h_usd_json",
    "cex_per_venue_eth_netflow_24h_usd_json",
    "cex_per_venue_stables_netflow_24h_usd_json",
)


_PER_ASSET_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("btc",     "cex_per_venue_btc_netflow_24h_usd_json"),
    ("eth",     "cex_per_venue_eth_netflow_24h_usd_json"),
    ("stables", "cex_per_venue_stables_netflow_24h_usd_json"),
)


def _build_per_venue_per_asset_series_24h(rows: list[dict]) -> dict:
    """Slice on_chain_snapshots rows into per-venue × per-asset 24h series.

    Returns ``{venue: {asset: [{ts, v}, ...]}}`` where venue ∈
    coinbase/binance/bybit/bitfinex/kraken/okx and asset ∈ btc/eth/stables.
    Source columns are JSON-encoded dicts ``{venue: signed_usd_float}``
    written by the runner's background fetcher; this function unrolls them
    into per-venue arrays so each card can render 3 independent lines.

    Pre-feature rows (where the JSON columns are NULL) silently contribute
    nothing — the array stays empty for that timestamp + venue + asset.
    """
    import json as _json
    out: dict[str, dict[str, list[dict]]] = {
        venue: {"btc": [], "eth": [], "stables": []}
        for venue, _ in _EXCHANGE_NETFLOW_FIELDS
    }
    for r in rows or []:
        ts = r.get("captured_at")
        if not ts:
            continue
        for asset_key, field in _PER_ASSET_FIELD_MAP:
            raw = r.get(field)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                d = _json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            for venue, _ in _EXCHANGE_NETFLOW_FIELDS:
                v = d.get(venue)
                if v is None:
                    continue
                try:
                    out[venue][asset_key].append({"ts": ts, "v": float(v)})
                except (TypeError, ValueError):
                    continue
    return out


def _build_aggregate_per_asset_series_24h(rows: list[dict]) -> dict:
    """Sum per-venue × per-asset values across all 6 venues per timestamp.

    Frontend renders this as the "Total netflow per asset" panel below the
    6-card grid. A timestamp contributes only if at least one venue has
    a value for that asset (so the line stays empty during pre-feature
    rows where the JSON columns are NULL).
    """
    import json as _json
    out: dict[str, list[dict]] = {"btc": [], "eth": [], "stables": []}
    for r in rows or []:
        ts = r.get("captured_at")
        if not ts:
            continue
        for asset_key, field in _PER_ASSET_FIELD_MAP:
            raw = r.get(field)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                d = _json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            total: Optional[float] = None
            for venue, _ in _EXCHANGE_NETFLOW_FIELDS:
                v = d.get(venue)
                if v is None:
                    continue
                try:
                    total = (total or 0.0) + float(v)
                except (TypeError, ValueError):
                    continue
            if total is not None:
                out[asset_key].append({"ts": ts, "v": total})
    return out


def _normalize_on_chain_row(row: dict) -> dict:
    """Inline-parse the JSON-string columns in an on_chain_snapshots row so
    the frontend doesn't have to do nested JSON.parse. Other columns pass
    through unchanged.
    """
    import json as _json
    out = dict(row)
    for k in _JSON_DICT_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v:
            try:
                out[k] = _json.loads(v)
            except (TypeError, ValueError):
                pass
    return out


# ── Generic DB browser (read-only, whitelist-only) ───────────────────────────
#
# Powers the "Database" overlay in the dashboard. Each entry maps a SQL
# table to (default ORDER BY column, descending). Whitelist is closed —
# unknown table names are rejected at the endpoint boundary so a typo / URL
# probe can't pivot into arbitrary SQL.
_DB_BROWSER_TABLES: dict[str, tuple[str, bool]] = {
    "trades":             ("entry_timestamp",  True),
    "rejected_signals":   ("signal_timestamp", True),
    "on_chain_snapshots": ("captured_at",      True),
    "whale_transfers":    ("captured_at",      True),
    "position_snapshots": ("captured_at",      True),
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
