"""Unit tests for `src.data.on_chain_types`."""

from __future__ import annotations

from src.data.on_chain_types import (
    OnChainSnapshot,
    WhaleBlackoutState,
    WhaleEvent,
    affected_symbols_for,
)


# ── OnChainSnapshot ─────────────────────────────────────────────────────────


def test_on_chain_snapshot_defaults_neutral_and_not_stale():
    snap = OnChainSnapshot()
    assert snap.daily_macro_bias == "neutral"
    assert snap.stablecoin_pulse_1h_usd is None
    assert snap.snapshot_age_s == 0
    assert snap.fresh is True


def test_on_chain_snapshot_fresh_flag_respects_threshold():
    snap = OnChainSnapshot(snapshot_age_s=7199, stale_threshold_s=7200)
    assert snap.fresh is True

    stale = OnChainSnapshot(snapshot_age_s=7200, stale_threshold_s=7200)
    assert stale.fresh is False

    explicit_fresh = OnChainSnapshot(snapshot_age_s=1800, stale_threshold_s=3600)
    assert explicit_fresh.fresh is True


def test_on_chain_snapshot_carries_derived_signals():
    snap = OnChainSnapshot(
        daily_macro_bias="bullish",
        stablecoin_pulse_1h_usd=75_000_000.0,
        cex_btc_netflow_24h_usd=-120_000_000.0,
        cex_eth_netflow_24h_usd=-50_000_000.0,
        coinbase_asia_skew_usd=30_000_000.0,
        bnb_self_flow_24h_usd=-8_000_000.0,
        snapshot_age_s=300,
    )
    assert snap.daily_macro_bias == "bullish"
    assert snap.stablecoin_pulse_1h_usd == 75_000_000.0
    assert snap.cex_btc_netflow_24h_usd == -120_000_000.0
    assert snap.fresh is True


# ── affected_symbols_for ────────────────────────────────────────────────────


def test_affected_symbols_for_stablecoin_expands_to_all():
    symbols = affected_symbols_for("tether")
    assert "BTC-USDT-SWAP" in symbols
    assert "ETH-USDT-SWAP" in symbols
    assert "SOL-USDT-SWAP" in symbols
    assert "DOGE-USDT-SWAP" in symbols
    assert "BNB-USDT-SWAP" in symbols
    assert len(symbols) == 5


def test_affected_symbols_for_usd_coin_matches_tether():
    assert affected_symbols_for("usd-coin") == affected_symbols_for("tether")
    assert affected_symbols_for("USDC") == affected_symbols_for("tether")


def test_affected_symbols_for_chain_native_collapses_to_single():
    assert affected_symbols_for("bitcoin") == ("BTC-USDT-SWAP",)
    assert affected_symbols_for("ethereum") == ("ETH-USDT-SWAP",)
    assert affected_symbols_for("solana") == ("SOL-USDT-SWAP",)
    assert affected_symbols_for("dogecoin") == ("DOGE-USDT-SWAP",)
    assert affected_symbols_for("binancecoin") == ("BNB-USDT-SWAP",)


def test_affected_symbols_for_aliases_normalize():
    # Case-insensitive + common aliases all map to the same symbol.
    assert affected_symbols_for("BTC") == ("BTC-USDT-SWAP",)
    assert affected_symbols_for("Bitcoin") == ("BTC-USDT-SWAP",)
    assert affected_symbols_for("BNB") == ("BNB-USDT-SWAP",)
    assert affected_symbols_for("binance-coin") == ("BNB-USDT-SWAP",)


def test_affected_symbols_for_unknown_token_returns_empty_tuple():
    # Degrades silently — caller logs + skips.
    assert affected_symbols_for("ripple") == ()
    assert affected_symbols_for("unknown-token") == ()
    assert affected_symbols_for("") == ()


# ── WhaleBlackoutState ──────────────────────────────────────────────────────


def test_whale_blackout_state_starts_empty():
    state = WhaleBlackoutState()
    assert state.is_active("BTC-USDT-SWAP", now_ms=1_000_000) is False
    assert state.is_active("ETH-USDT-SWAP", now_ms=1_000_000) is False


def test_whale_blackout_state_active_inside_window():
    state = WhaleBlackoutState()
    state.set_blackout("BTC-USDT-SWAP", until_ms=2_000_000)
    assert state.is_active("BTC-USDT-SWAP", now_ms=1_999_999) is True
    # Exact equality → expired.
    assert state.is_active("BTC-USDT-SWAP", now_ms=2_000_000) is False
    assert state.is_active("BTC-USDT-SWAP", now_ms=2_000_001) is False


def test_whale_blackout_state_set_extends_never_shortens():
    state = WhaleBlackoutState()
    state.set_blackout("BTC-USDT-SWAP", until_ms=5_000_000)
    # Second call with an earlier deadline must not trim.
    state.set_blackout("BTC-USDT-SWAP", until_ms=3_000_000)
    assert state.blackouts["BTC-USDT-SWAP"] == 5_000_000
    # Later deadline extends.
    state.set_blackout("BTC-USDT-SWAP", until_ms=7_000_000)
    assert state.blackouts["BTC-USDT-SWAP"] == 7_000_000


def test_whale_blackout_state_is_per_symbol():
    state = WhaleBlackoutState()
    state.set_blackout("BTC-USDT-SWAP", until_ms=5_000_000)
    state.set_blackout("ETH-USDT-SWAP", until_ms=2_000_000)
    assert state.is_active("BTC-USDT-SWAP", now_ms=3_000_000) is True
    assert state.is_active("ETH-USDT-SWAP", now_ms=3_000_000) is False
    assert state.is_active("SOL-USDT-SWAP", now_ms=3_000_000) is False  # never set


# ── WhaleEvent ──────────────────────────────────────────────────────────────


def test_whale_event_is_frozen_tuple_of_affected():
    evt = WhaleEvent(
        token_id="bitcoin",
        usd_value=150_000_000.0,
        timestamp_ms=1_700_000_000_000,
        affected_symbols=("BTC-USDT-SWAP",),
    )
    assert evt.token_id == "bitcoin"
    assert evt.affected_symbols == ("BTC-USDT-SWAP",)
    # Frozen — attribute assignment raises.
    try:
        evt.usd_value = 999  # type: ignore[misc]
    except Exception as e:
        assert "FrozenInstanceError" in type(e).__name__ or "cannot assign" in str(e)
    else:
        raise AssertionError("WhaleEvent should be frozen")
