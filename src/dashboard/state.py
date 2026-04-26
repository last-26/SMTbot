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
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

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


async def fetch_wallet(bybit_cfg: Optional[dict]) -> Optional[dict]:
    """Probe Bybit for live wallet balance. Returns None on any failure
    (missing creds, network error, demo edge unreachable) — dashboard
    degrades to the simulated journal balance.

    Runs in a thread because `pybit` is sync and we don't want to block
    the FastAPI event loop on the network round-trip.
    """
    if not bybit_cfg:
        return None
    try:
        from src.execution.bybit_client import BybitClient, BybitCredentials
    except Exception:
        return None

    def _probe() -> dict:
        creds = BybitCredentials(
            api_key=bybit_cfg["api_key"],
            api_secret=bybit_cfg["api_secret"],
            demo=bool(bybit_cfg.get("demo", True)),
            account_type=bybit_cfg.get("account_type", "UNIFIED"),
            category=bybit_cfg.get("category", "linear"),
        )
        client = BybitClient(creds, allow_live=not creds.demo)
        return {
            "available_usd": float(client.get_balance("USDT")),
            "margin_balance_usd": float(client.get_total_equity("USDT")),
            "demo": bool(creds.demo),
        }

    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe), timeout=8.0)
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
    wallet = await wallet_task

    return {
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
        "whale_transfers_recent": whale_recent,
        "wallet": wallet,
        "counts": {
            "closed_total": len(closed),
            "open_total": len(open_trades),
            "rejected_total": len(rejected),
            "whale_total": len(whales),
            "on_chain_snapshots_total": len(on_chain_rows),
        },
    }


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


_JSON_DICT_KEYS = ("token_volume_1h_net_usd_json",)


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
