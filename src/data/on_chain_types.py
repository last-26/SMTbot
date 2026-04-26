"""Types for the Arkham on-chain integration.

Kept separate from the HTTP client (`src.data.on_chain`) so consumers
(MarketState, runner, entry_signals, multi_timeframe) can import the
dataclasses without pulling in httpx / websockets. Mirrors the shape of
`src.data.derivatives_api.DerivativesSnapshot` — plain dataclass with
scalar fields only; MarketState carries it as `Optional[Any]` to keep
the pydantic model import-cycle-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# All five bot-traded perps (OKX-style internal symbol format kept after
# the 2026-04-25 Bybit migration; bybit_client.py translates at the API
# boundary). Stablecoin whale events shock the whole market (USDT/USDC
# moving in size → possible CEX buy/sell pressure across every quote-
# currency pair) while chain-native whale moves only affect their own
# symbol.
_ALL_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
)


@dataclass(frozen=True)
class OnChainSnapshot:
    """Point-in-time on-chain view.

    `daily_macro_bias` is derived from the daily CEX balance-change pull
    (Phase B): stablecoin CEX balance delta + BTC CEX netflow direction.
    `stablecoin_pulse_1h_usd` is the hourly USDT+USDC delta — positive
    means stablecoins entering CEXes (buying ammo), negative means
    leaving (users withdrawing; risk-off).
    `snapshot_age_s` is computed at attach time by the runner so
    downstream gates can skip stale snapshots via `.fresh`.
    """

    daily_macro_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    stablecoin_pulse_1h_usd: Optional[float] = None
    cex_btc_netflow_24h_usd: Optional[float] = None
    cex_eth_netflow_24h_usd: Optional[float] = None
    coinbase_asia_skew_usd: Optional[float] = None  # legacy / unused
    bnb_self_flow_24h_usd: Optional[float] = None  # legacy / unused
    # 2026-04-22 — per-entity 24h netflow (last completed UTC day) via
    # `/flow/entity/{entity}`. Initially journal-only; promoted to runtime
    # 2026-04-22 (gece, late) as additional inputs to the flow_alignment
    # soft signal ahead of the Pass 1 clean restart. Coinbase/Binance/Bybit
    # weights: 0.15 / 0.10 / 0.10 in `_flow_alignment_score` (vs 0.25
    # each for BTC and stablecoin pulse).
    cex_coinbase_netflow_24h_usd: Optional[float] = None
    cex_binance_netflow_24h_usd: Optional[float] = None
    cex_bybit_netflow_24h_usd: Optional[float] = None
    # 2026-04-23 (night-late) — 4th + 5th venues added journal-only.
    # Live probe against `type:cex` aggregate showed named-entity coverage
    # (CB+BN+BY) was only ~1-6% of the full CEX BTC netflow signal.
    # Bitfinex surfaced as the largest single named INFLOW (+$193M/24h,
    # Tether-adjacent, historical BTC lead); Kraken as the largest single
    # OUTFLOW (−$216M/24h, Western retail/institutional exit). Not yet
    # wired into `_flow_alignment_score` — Pass 3 Optuna will decide
    # weights once uniform post-restart data accumulates.
    cex_bitfinex_netflow_24h_usd: Optional[float] = None
    cex_kraken_netflow_24h_usd: Optional[float] = None
    # 2026-04-24 — 6th venue added journal-only. Bot trades on OKX so
    # the venue's own netflow is a natural self-signal. Live probe showed
    # turnover ~$1.86B/24h (matches Bitfinex scale) but 24h net ≈ 0 because
    # in/out are structurally balanced (−0.12% bias); hourly moves reach
    # $58M. Captured at 24h grain for parity with the other 5 entities;
    # Pass 3 will decide whether to add a 1h-windowed OKX slot separately.
    # Not wired into `_flow_alignment_score`.
    cex_okx_netflow_24h_usd: Optional[float] = None
    # 2026-04-26 — per-venue × per-asset 24h netflow (BTC / ETH / stables).
    # Dict keyed by entity slug ("coinbase", "binance", "bybit", "bitfinex",
    # "kraken", "okx") → signed USD float (in - out). Adding a 7th venue
    # won't require schema migration. Refreshed in a background task so
    # the trade cycle never waits on the 36 histogram calls this requires.
    # Powers the dashboard's per-venue per-asset chart; not yet wired into
    # any runtime scoring (Pass 3 candidate).
    cex_per_venue_btc_netflow_24h_usd_json: Optional[str] = None
    cex_per_venue_eth_netflow_24h_usd_json: Optional[str] = None
    cex_per_venue_stables_netflow_24h_usd_json: Optional[str] = None
    # 2026-04-22 — per-symbol most-recent-hour net CEX flow (USD) via
    # `/token/volume/{id}?granularity=1h`. JSON dict keyed by internal-format symbol;
    # adding a 6th symbol won't require schema migration. Initially
    # journal-only; promoted to runtime 2026-04-22 (gece, late) as the
    # `per_symbol_cex_flow_penalty` soft signal (per-symbol directional
    # bias: IN=bearish, OUT=bullish for the traded token).
    token_volume_1h_net_usd_json: Optional[str] = None
    snapshot_age_s: int = 0
    stale_threshold_s: int = 7200

    @property
    def fresh(self) -> bool:
        """True when the snapshot is younger than `stale_threshold_s`."""
        return self.snapshot_age_s < self.stale_threshold_s


@dataclass(frozen=True)
class WhaleEvent:
    """A single Arkham whale-transfer event.

    `affected_symbols` carries the pre-computed blast radius — stablecoin
    events expand to every watched symbol; chain-native events collapse
    to just the matching internal-format perp. The WebSocket listener computes this
    once so the runner's hot path doesn't map strings on every poll.
    """

    token_id: str
    usd_value: float
    timestamp_ms: int
    affected_symbols: tuple[str, ...]


def affected_symbols_for(token_id: str) -> tuple[str, ...]:
    """Map an Arkham token identifier to the perps it shocks.

    Stablecoin moves (tether, usd-coin) shock every listed perp; chain-
    native moves collapse to just the matching symbol. Unknown
    token_ids return () — the listener logs but does not raise so a
    new token appearing mid-run degrades silently.

    The `binancecoin/bnb` branch was retired on the 2026-04-25 Bybit
    migration when XRP replaced BNB in the watched set; reinstate it
    if BNB is ever swapped back into `trading.symbols`. No `ripple/xrp`
    branch on purpose — Arkham doesn't index XRPL in either the
    `/token/volume/{id}` REST endpoint or the whale-transfer WS stream
    (probed 2026-04-25, all slug variants rejected as
    `token not supported`). Whale events for XRP simply never arrive,
    so the fan-in branch would be dead code.
    """
    t = token_id.lower()
    if t in ("tether", "usd-coin", "usdt", "usdc"):
        return _ALL_SYMBOLS
    if t in ("bitcoin", "btc"):
        return ("BTC-USDT-SWAP",)
    if t in ("ethereum", "eth"):
        return ("ETH-USDT-SWAP",)
    if t in ("solana", "sol"):
        return ("SOL-USDT-SWAP",)
    if t in ("dogecoin", "doge"):
        return ("DOGE-USDT-SWAP",)
    return ()


# 2026-04-22 — reverse mapping: internal-format perp symbol → Arkham token slug
# (CoinGecko-style id). Used by the runner's `/token/volume/{id}` fetch
# pipeline (per-symbol 1h CEX flow → `per_symbol_cex_flow_penalty`).
# Adding a new symbol requires extending BOTH this dict AND
# `affected_symbols_for` above for the chain-native fan-in/out.
#
# **XRP intentionally absent** (2026-04-25 BNB↔XRP swap): Arkham's
# `/token/volume/{id}` endpoint returns `token not supported` for every
# XRP slug variant (xrp / ripple / XRP / xrp-classic / xrpl). XRPL isn't
# indexed in their per-token volume surface — likely because they focus
# on EVM + Bitcoin + Solana. XRP entries therefore lose the
# `per_symbol_cex_flow_penalty` soft signal but keep every other
# Arkham-driven gate (daily_bias modifier, stablecoin_pulse_penalty,
# altcoin_index_penalty, flow_alignment_penalty — none of which are
# per-symbol). Reinstate this entry if Arkham adds XRP support later.
#
# 2026-04-27 F5 re-probe (after the 2026-04-23 histogram fallback was
# added — i.e. checking whether the v2 histogram path could rescue XRP
# even though /token/volume can't): all 6 slug variants tested
# (ripple, xrp, xrpl, xrp-classic, xrp-token, xrp-ledger) — none yield
# usable data:
#   - /token/volume/{id} → HTTP 400 "token not supported" for 5 of 6
#   - /token/volume/xrp-classic → HTTP 200 but 100% zero buckets (a
#     stale/inactive listing, not actual XRPL data)
#   - /transfers/histogram?base=type:cex&tokens={id} → HTTP 400 "bad
#     filter: insufficient criteria" for all variants (slug not in
#     their indexed registry).
# Decision: keep XRP out of the map. Recheck quarterly or when Arkham
# announces XRPL chain support — currently their indexed chains are
# Ethereum + Bitcoin + Solana + a handful of EVM L2s.
WATCHED_SYMBOL_TO_TOKEN_ID: dict[str, str] = {
    "BTC-USDT-SWAP": "bitcoin",
    "ETH-USDT-SWAP": "ethereum",
    "SOL-USDT-SWAP": "solana",
    "DOGE-USDT-SWAP": "dogecoin",
}


@dataclass
class WhaleBlackoutState:
    """Mutable per-symbol blackout window registry.

    The WebSocket listener writes `blackouts[symbol] = until_ms` on each
    qualifying whale event; `entry_signals` reads via `is_active` on
    every entry attempt. Concurrent read/write is safe because Python's
    dict writes are atomic at the key level and the listener only
    extends (max()) whereas the reader only compares.
    """

    blackouts: dict[str, int] = field(default_factory=dict)

    def is_active(self, symbol: str, now_ms: int) -> bool:
        until = self.blackouts.get(symbol)
        if until is None:
            return False
        return now_ms < until

    def set_blackout(self, symbol: str, until_ms: int) -> None:
        """Extend (not shorten) the blackout window for `symbol`.

        Two overlapping whale events on the same symbol should not trim
        the second event's window down to the first event's shorter tail.
        """
        current = self.blackouts.get(symbol, 0)
        if until_ms > current:
            self.blackouts[symbol] = until_ms
