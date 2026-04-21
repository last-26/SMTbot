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

# All five bot-traded OKX perps. Stablecoin whale events shock the whole
# market (USDT/USDC moving in size → possible CEX buy/sell pressure
# across every quote-currency pair) while chain-native whale moves only
# affect their own symbol.
_ALL_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "BNB-USDT-SWAP",
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
    coinbase_asia_skew_usd: Optional[float] = None
    bnb_self_flow_24h_usd: Optional[float] = None
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
    to just the matching OKX perp. The WebSocket listener computes this
    once so the runner's hot path doesn't map strings on every poll.
    """

    token_id: str
    usd_value: float
    timestamp_ms: int
    affected_symbols: tuple[str, ...]


def affected_symbols_for(token_id: str) -> tuple[str, ...]:
    """Map an Arkham token identifier to the OKX perps it shocks.

    Stablecoin moves (tether, usd-coin) shock every listed perp; chain-
    native moves collapse to just the matching OKX symbol. Unknown
    token_ids return () — the listener logs but does not raise so a
    new token appearing mid-run degrades silently.
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
    if t in ("binancecoin", "binance-coin", "bnb"):
        return ("BNB-USDT-SWAP",)
    return ()


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
