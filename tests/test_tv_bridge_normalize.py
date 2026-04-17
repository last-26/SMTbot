"""Guard: `_normalize_tf` converts shorthand into TV's native resolution form.

Regression: sending `"15m"` / `"1h"` to TV silently falls back to whatever the
chart already shows — including premium-only resolutions like `"1R"` (Range
bars). TradingView's `chart.setResolution` wants a bare minute count, or an
uppercase unit for non-minute units.
"""

from __future__ import annotations

import pytest

from src.data.tv_bridge import TVBridge


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1m", "1"),
        ("3m", "3"),
        ("15m", "15"),
        ("60m", "60"),
        ("1h", "60"),
        ("4h", "240"),
        ("1d", "1D"),
        ("1w", "1W"),
        ("15s", "15S"),
        # Already-native forms pass through unchanged:
        ("15", "15"),
        ("240", "240"),
        ("1H", "1H"),
        ("1D", "1D"),
        ("D", "D"),
        ("W", "W"),
        ("1M", "1M"),  # month, TV native
        # Malformed inputs fall through unchanged (TV will error, not us):
        ("garbage", "garbage"),
        ("", ""),
    ],
)
def test_normalize_tf(raw: str, expected: str) -> None:
    assert TVBridge._normalize_tf(raw) == expected
