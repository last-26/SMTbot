"""Lock the Bybit base-coin → internal-contracts conversion.

Regression test for the 2026-04-28 IEEE 754 bug:
`size_contracts = size_base / ct_val` would return 6.999999999999999
for ETH (base=0.7, ct_val=0.1), tripping `_detect_tp1_and_move_sl`'s
"size shrank" branch on every poll for fractional-ct_val symbols.

Fix: `round()` absorbs ULP drift since contract units are always
integer by construction.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from src.execution.bybit_client import BybitClient, BybitCredentials


class _FakeSDK:
    """Minimal pybit-shaped session for `get_positions` only."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def get_positions(self, **_kwargs: Any) -> dict:
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": self._rows},
        }


def _row(symbol: str, size: str, *, side: str = "Buy",
         pos_idx: int = 1, avg: str = "1000.0",
         mark: str = "1000.0") -> dict:
    return {
        "symbol": symbol,
        "size": size,
        "side": side,
        "positionIdx": pos_idx,
        "avgPrice": avg,
        "markPrice": mark,
        "unrealisedPnl": "0",
        "leverage": "10",
    }


def _client(rows: list[dict]) -> BybitClient:
    return BybitClient(
        credentials=BybitCredentials(api_key="x", api_secret="y", demo=True),
        sdk=_FakeSDK(rows),
    )


@pytest.mark.parametrize(
    "bybit_symbol,size_base,expected_contracts",
    [
        # ETH ct_val=0.1; 0.7 / 0.1 = 6.999999999999999 → MUST round to 7.
        ("ETHUSDT", "0.7", 7.0),
        # The original bug manifested at size=7; verify a few more ETH
        # multiples to catch any partial-fill scenario.
        ("ETHUSDT", "0.1", 1.0),
        ("ETHUSDT", "1.5", 15.0),
        # BTC ct_val=0.01; 0.04 / 0.01 happens to land cleanly at 4.0,
        # but 0.03 / 0.01 = 2.9999999999999996 → MUST round to 3.
        ("BTCUSDT", "0.04", 4.0),
        ("BTCUSDT", "0.03", 3.0),
        # Integer-ct_val symbols are clean by construction; lock them anyway.
        ("SOLUSDT", "27", 27.0),
        ("DOGEUSDT", "15000", 15.0),  # ct_val=1000
        ("XRPUSDT", "800", 8.0),       # ct_val=100
    ],
)
def test_get_positions_size_contracts_is_integer_after_round_trip(
    bybit_symbol: str, size_base: str, expected_contracts: float,
) -> None:
    client = _client([_row(bybit_symbol, size_base)])
    snaps = client.get_positions()
    assert len(snaps) == 1
    assert snaps[0].size == expected_contracts
    # Stronger: must be exact float-int (no IEEE 754 fractional residue).
    assert snaps[0].size == int(snaps[0].size)


def test_get_positions_zero_size_yields_zero_contracts() -> None:
    """Empty / closed positions should pass through as size=0 cleanly."""
    client = _client([_row("ETHUSDT", "0")])
    snaps = client.get_positions()
    assert len(snaps) == 1
    assert snaps[0].size == 0.0


def test_get_positions_unknown_ct_val_falls_back_to_base() -> None:
    """Symbol not in `_INTERNAL_CT_VAL` (e.g. a new pair before
    `_INTERNAL_CT_VAL` is updated) returns the raw base-coin size so the
    caller at least sees a non-zero position rather than silently zero."""
    # Use a symbol that internal-format translation passes through unchanged
    # AND is not in _INTERNAL_CT_VAL — feed an oddball shape.
    client = _client([{
        "symbol": "ZZZUSDT",
        "size": "5.5",
        "side": "Buy",
        "positionIdx": 1,
        "avgPrice": "1.0",
        "markPrice": "1.0",
        "unrealisedPnl": "0",
        "leverage": "10",
    }])
    snaps = client.get_positions()
    assert len(snaps) == 1
    # Unknown symbol path: ct_val=0 → no division → raw size_base passes through.
    assert snaps[0].size == 5.5
