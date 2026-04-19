"""Parse TradingView MCP Pine Script outputs into a unified MarketState.

The reader fetches data from two Pine Script indicators via tv_bridge.py,
then parses tables, labels, boxes, and lines into structured Python objects.

Data flow:
  TradingView chart (SMT Overlay + SMT Oscillator running)
    -> TV CLI (subprocess)
    -> JSON responses
    -> This module parses into MarketState
    -> Bot uses MarketState for analysis

Two tables are the primary data sources:
  - SMT Signals (smt_overlay.pine) — PA + VMC Cipher A overlay signals
  - SMT Oscillator (smt_oscillator.pine) — momentum + divergences
Drawing objects (labels, boxes, lines) provide supplementary detail.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from .models import (
    Direction,
    FVGZone,
    LiquidityLevel,
    MarketState,
    MSSEvent,
    OrderBlock,
    OscillatorTableData,
    Session,
    SessionLevel,
    SignalTableData,
    SweepEvent,
)
from .tv_bridge import TVBridge


# ── Parsing helpers ──────────────────────────────────────────────────────────


def _parse_direction(s: str) -> Direction:
    """Parse a direction string into a Direction enum."""
    s = s.strip().upper()
    if s in ("BULLISH", "BULL"):
        return Direction.BULLISH
    if s in ("BEARISH", "BEAR"):
        return Direction.BEARISH
    if s == "RANGING":
        return Direction.RANGING
    return Direction.UNDEFINED


def _parse_session(s: str) -> Session:
    """Parse a session string into a Session enum."""
    s = s.strip().upper()
    for member in Session:
        if member.value == s:
            return member
    return Session.OFF


def _parse_float(s: str) -> Optional[float]:
    """Parse a float from string, return None on failure."""
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_leading_float(s: Optional[str]) -> float:
    """Parse the leading float from a compound cell like '67450.21 (above)'.

    Returns 0.0 for missing / dash / unparsable cells. Used by VWAP rows
    where the suffix is human-eyeball metadata that the bot ignores.
    """
    if not s:
        return 0.0
    s = s.strip()
    if s in ("", "—", "-"):
        return 0.0
    m = re.match(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    return float(m.group(0))


def _parse_int(s: Optional[str]) -> Optional[int]:
    """Parse an int from string, return None on failure / missing."""
    if s is None:
        return None
    try:
        return int(float(s.strip().replace(",", "")))
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_float_list(s: str) -> list[float]:
    """Parse comma-separated floats like '70200,70350'."""
    if not s or s.strip() in ("", "—", "-", "none"):
        return []
    parts = s.split(",")
    result = []
    for p in parts:
        v = _parse_float(p.strip())
        if v is not None:
            result.append(v)
    return result


# ── Signal Table parser ──────────────────────────────────────────────────────


def parse_signal_table(tables_data: dict[str, Any]) -> Optional[SignalTableData]:
    """Parse the SMT Signals table from tv data tables output.

    Expected table format (from smt_overlay.pine, post-7.D4 trim):
    Row 0:  === SMT Signals === | BTCUSDT.P
    Row 1:  trend_htf       | BULLISH
    Row 2:  trend_ltf       | BEARISH
    Row 3:  structure       | HH_HL_bullish
    Row 4:  last_mss        | BULLISH@69450
    Row 5:  active_fvg      | BULL@68900-69100
    Row 6:  liquidity_above | 70200,70350
    Row 7:  liquidity_below | 68100,67950
    Row 8:  last_sweep      | BEAR@70350
    Row 9:  session         | LONDON
    Row 10: vmc_ribbon      | BULLISH
    Row 11: vmc_wt_bias     | OVERSOLD (-67.42)
    Row 12: vmc_wt_cross    | UP
    Row 13: vmc_last_signal | YELLOW_X_BUY@69450
    Row 14: vmc_rsi_mfi     | -2.50
    Row 15: atr_14          | 450.5
    Row 16: price           | 69500.0
    Row 17: vwap_1m         | 69480.5 (above)
    Row 18: vwap_3m         | 69450.0 (above)
    Row 19: vwap_3m_upper   | 69520.0
    Row 20: vwap_3m_lower   | 69380.0
    Row 21: vwap_15m        | 69400.0 (above)
    Row 22: last_bar        | 12345

    Phase 7.D4 dropped `active_ob` and `confluence` rows. `_none_if_dash`
    defaults keep this parser backward-compatible with older Pine versions.
    """
    if not tables_data.get("success") or not tables_data.get("studies"):
        return None

    # Find the SMT Signals table (from smt_overlay.pine / "SMT Master Overlay")
    for study in tables_data["studies"]:
        name = study.get("name", "")

        for table in study.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue

            # Match on table header containing "SMT Signals"
            first_row = rows[0] if rows else ""
            if "SIGNALS" not in first_row.upper() and "Signal" not in name:
                continue

            # Parse key-value rows
            kv = {}
            for row in rows:
                parts = row.split("|")
                if len(parts) >= 2:
                    key = parts[0].strip().lower().replace(" ", "_")
                    val = parts[1].strip()
                    if key and not key.startswith("="):
                        kv[key] = val

            return _build_signal_data(kv)

    return None


def _build_signal_data(kv: dict[str, str]) -> SignalTableData:
    """Build SignalTableData from parsed key-value pairs."""
    # Parse confluence "5/7" -> 5
    confluence_str = kv.get("confluence", "0")
    confluence = 0
    match = re.match(r"(\d+)", confluence_str)
    if match:
        confluence = int(match.group(1))

    # Parse vmc_wt_bias: "OVERSOLD (-67.42)" -> state + float
    wt_bias_raw = kv.get("vmc_wt_bias", "NEUTRAL")
    wt_bias_state, wt_bias_value = _parse_state_with_value(wt_bias_raw, "NEUTRAL", 0.0)

    # Parse vwap_*: "67450.21 (above)" -> 67450.21. The "(above)" suffix is
    # eyeball confirmation only; the bot derives side from price comparison.
    vwap_1m_val  = _parse_leading_float(kv.get("vwap_1m"))
    vwap_3m_val  = _parse_leading_float(kv.get("vwap_3m"))
    vwap_15m_val = _parse_leading_float(kv.get("vwap_15m"))
    vwap_3m_upper_val = _parse_leading_float(kv.get("vwap_3m_upper"))
    vwap_3m_lower_val = _parse_leading_float(kv.get("vwap_3m_lower"))

    return SignalTableData(
        # Price Action
        trend_htf=_parse_direction(kv.get("trend_htf", "")),
        trend_ltf=_parse_direction(kv.get("trend_ltf", "")),
        structure=kv.get("structure", ""),
        last_mss=_none_if_dash(kv.get("last_mss", "")),
        active_fvg=_none_if_dash(kv.get("active_fvg", "")),
        active_ob=_none_if_dash(kv.get("active_ob", "")),
        liquidity_above=_parse_float_list(kv.get("liquidity_above", "")),
        liquidity_below=_parse_float_list(kv.get("liquidity_below", "")),
        last_sweep=_none_if_dash(kv.get("last_sweep", "")),
        # Sessions
        session=_parse_session(kv.get("session", "")),
        # VMC Cipher A
        vmc_ribbon=kv.get("vmc_ribbon", "BEARISH"),
        vmc_wt_bias=wt_bias_state,
        vmc_wt_value=wt_bias_value,
        vmc_wt_cross=kv.get("vmc_wt_cross", "\u2014"),
        vmc_last_signal=kv.get("vmc_last_signal", "\u2014"),
        vmc_rsi_mfi=_parse_float(kv.get("vmc_rsi_mfi", "0")) or 0.0,
        # Summary
        confluence=confluence,
        atr_14=_parse_float(kv.get("atr_14", "0")) or 0.0,
        price=_parse_float(kv.get("price", "0")) or 0.0,
        vwap_1m=vwap_1m_val,
        vwap_3m=vwap_3m_val,
        vwap_15m=vwap_15m_val,
        vwap_3m_upper=vwap_3m_upper_val,
        vwap_3m_lower=vwap_3m_lower_val,
        last_bar=_parse_int(kv.get("last_bar")),
    )


def _parse_state_with_value(
    raw: str, default_state: str, default_value: float
) -> tuple[str, float]:
    """Parse a compound field like 'OVERSOLD (-67.42)' into (state, value)."""
    m = re.match(r"([A-Z_]+)\s*\(([^)]+)\)", raw.strip())
    if m:
        state = m.group(1)
        value = _parse_float(m.group(2)) or default_value
        return state, value
    return raw.strip() or default_state, default_value


def _parse_signal_with_bars(raw: str) -> tuple[str, int]:
    """Parse a signal+age field like 'BUY (3b ago)' into (signal, bars_ago)."""
    m = re.match(r"(.+?)\s*\((\d+)b\s+ago\)", raw.strip())
    if m:
        return m.group(1).strip(), int(m.group(2))
    return raw.strip(), 0


def _none_if_dash(s: str) -> Optional[str]:
    """Return None if value is a dash/empty placeholder."""
    if not s or s.strip() in ("", "\u2014", "-", "none", "N/A"):
        return None
    return s.strip()


# ── Oscillator Table parser ─────────────────────────────────────────────────


def parse_oscillator_table(tables_data: dict[str, Any]) -> Optional[OscillatorTableData]:
    """Parse the SMT Oscillator table from tv data tables output.

    Expected table format (from smt_oscillator.pine):
    Row 0:  === SMT Oscillator === | BTCUSDT.P
    Row 1:  wt1             | -42.50
    Row 2:  wt2             | -38.20
    Row 3:  wt_state        | OVERSOLD
    Row 4:  wt_cross        | UP
    Row 5:  wt_vwap_fast    | -4.30
    Row 6:  rsi             | 35.20 (OVERSOLD)
    Row 7:  rsi_mfi         | -2.50 (BEARISH)
    Row 8:  stoch_k         | 25.50
    Row 9:  stoch_d         | 30.20
    Row 10: stoch_state     | D>K (bearish)
    Row 11: last_signal     | BUY (3b ago)
    Row 12: last_wt_div     | BULL_REG (12b ago)
    Row 13: momentum        | 3/5
    Row 14: last_bar        | 12345
    """
    if not tables_data.get("success") or not tables_data.get("studies"):
        return None

    for study in tables_data["studies"]:
        for table in study.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue

            first_row = rows[0] if rows else ""
            if "OSCILLATOR" not in first_row.upper():
                continue

            # Parse key-value rows
            kv: dict[str, str] = {}
            for row in rows:
                parts = row.split("|")
                if len(parts) >= 2:
                    key = parts[0].strip().lower().replace(" ", "_")
                    val = parts[1].strip()
                    if key and not key.startswith("="):
                        kv[key] = val

            return _build_oscillator_data(kv)

    return None


def _build_oscillator_data(kv: dict[str, str]) -> OscillatorTableData:
    """Build OscillatorTableData from parsed key-value pairs."""
    # Parse rsi: "35.20 (OVERSOLD)" -> value + state
    rsi_raw = kv.get("rsi", "50")
    rsi_state, rsi_val = _parse_state_with_value(rsi_raw, "NEUTRAL", 50.0)
    # rsi field may be "35.20 (OVERSOLD)" — state is second, value is first
    # Re-parse: value is the number, state is the parenthesized text
    rsi_m = re.match(r"([\d.\-]+)\s*\(([^)]+)\)", rsi_raw.strip())
    if rsi_m:
        rsi_val = _parse_float(rsi_m.group(1)) or 50.0
        rsi_state = rsi_m.group(2).strip()
    else:
        rsi_val = _parse_float(rsi_raw) or 50.0
        rsi_state = "NEUTRAL"

    # Parse rsi_mfi: "-2.50 (BEARISH)" -> value + bias
    mfi_raw = kv.get("rsi_mfi", "0")
    mfi_m = re.match(r"([\d.\-]+)\s*\(([^)]+)\)", mfi_raw.strip())
    if mfi_m:
        mfi_val = _parse_float(mfi_m.group(1)) or 0.0
        mfi_bias = mfi_m.group(2).strip()
    else:
        mfi_val = _parse_float(mfi_raw) or 0.0
        mfi_bias = "NEUTRAL"

    # Parse last_signal: "BUY (3b ago)" -> signal + bars
    sig_name, sig_bars = _parse_signal_with_bars(kv.get("last_signal", "\u2014"))

    # Parse last_wt_div: "BULL_REG (12b ago)" -> div + bars
    div_name, div_bars = _parse_signal_with_bars(kv.get("last_wt_div", "\u2014"))

    # Parse momentum: "3/5" -> 3
    mom_str = kv.get("momentum", "0")
    mom_m = re.match(r"(\d+)", mom_str)
    momentum = int(mom_m.group(1)) if mom_m else 0

    return OscillatorTableData(
        wt1=_parse_float(kv.get("wt1", "0")) or 0.0,
        wt2=_parse_float(kv.get("wt2", "0")) or 0.0,
        wt_state=kv.get("wt_state", "NEUTRAL"),
        wt_cross=kv.get("wt_cross", "\u2014"),
        wt_vwap_fast=_parse_float(kv.get("wt_vwap_fast", "0")) or 0.0,
        rsi=rsi_val,
        rsi_state=rsi_state,
        rsi_mfi=mfi_val,
        rsi_mfi_bias=mfi_bias,
        stoch_k=_parse_float(kv.get("stoch_k", "50")) or 50.0,
        stoch_d=_parse_float(kv.get("stoch_d", "50")) or 50.0,
        stoch_state=kv.get("stoch_state", "K>D (bullish)"),
        last_signal=sig_name,
        last_signal_bars_ago=sig_bars,
        last_wt_div=div_name,
        last_wt_div_bars_ago=div_bars,
        momentum=momentum,
    )


# ── Label parsers (MSS, BOS, Sweep) ─────────────────────────────────────────


def parse_mss_labels(labels_data: dict[str, Any]) -> list[MSSEvent]:
    """Parse MSS/BOS labels from mss_detector.pine.

    Label tooltip format: MSS|BULLISH|69450.5|1234
    Label text format: MSS ▲\\nBULLISH  or  BOS ▼\\nBEARISH
    """
    events = []
    if not labels_data.get("success") or not labels_data.get("studies"):
        return events

    for study in labels_data["studies"]:
        name = study.get("name", "")
        if "MSS" not in name and "mss" not in name.lower() and "SMT" not in name:
            continue

        for label in study.get("labels", []):
            text = label.get("text", "")
            price = label.get("price")

            # Try to parse from text
            event = _parse_mss_label_text(text, price)
            if event:
                events.append(event)

    return events


def _parse_mss_label_text(text: str, price: Optional[float]) -> Optional[MSSEvent]:
    """Parse a single MSS/BOS label."""
    if not text:
        return None

    text_upper = text.upper().replace("\n", " ")

    event_type = None
    direction = None

    if "MSS" in text_upper:
        event_type = "MSS"
    elif "BOS" in text_upper:
        event_type = "BOS"
    else:
        return None

    if "BULLISH" in text_upper or "▲" in text:
        direction = Direction.BULLISH
    elif "BEARISH" in text_upper or "▼" in text:
        direction = Direction.BEARISH
    else:
        return None

    return MSSEvent(
        event_type=event_type,
        direction=direction,
        price=price or 0.0,
    )


def parse_sweep_labels(labels_data: dict[str, Any]) -> list[SweepEvent]:
    """Parse sweep labels from liquidity_sweep.pine.

    Label tooltip format: SWEEP|BULLISH|68100|3|5678
    Label text: SWEEP ▲ 68100 (3x)
    """
    events = []
    if not labels_data.get("success") or not labels_data.get("studies"):
        return events

    for study in labels_data["studies"]:
        name = study.get("name", "")
        if "Liquidity" not in name and "liquidity" not in name.lower() and "sweep" not in name.lower() and "SMT" not in name:
            continue

        for label in study.get("labels", []):
            text = label.get("text", "")
            price = label.get("price")

            if "SWEEP" not in text.upper():
                continue

            direction = Direction.BULLISH if "▲" in text or "BULL" in text.upper() else Direction.BEARISH
            events.append(SweepEvent(
                direction=direction,
                level=price or 0.0,
            ))

    return events


# ── Box parsers (FVG, OB) ───────────────────────────────────────────────────


def parse_fvg_boxes(boxes_data: dict[str, Any]) -> list[FVGZone]:
    """Parse FVG zones from fvg_mapper.pine boxes.

    Box tooltip format: FVG|BULLISH|68900|69100|0.29|ACTIVE
    Without verbose: zones=[{high, low}]
    With verbose: all_boxes=[{id, high, low, x1, x2, borderColor, bgColor}]
    """
    zones = []
    if not boxes_data.get("success") or not boxes_data.get("studies"):
        return zones

    for study in boxes_data["studies"]:
        name = study.get("name", "")
        if "FVG" not in name and "fvg" not in name.lower() and "Fair Value" not in name and "SMT" not in name:
            continue

        # Use verbose boxes if available for color-based direction detection
        boxes = study.get("all_boxes", [])
        if boxes:
            for box in boxes:
                high = box.get("high")
                low = box.get("low")
                if high is None or low is None:
                    continue

                # Detect direction from color (green = bullish, red = bearish)
                bg_color = str(box.get("bgColor", ""))
                direction = _direction_from_color(bg_color, Direction.BULLISH)

                zones.append(FVGZone(
                    direction=direction,
                    bottom=low,
                    top=high,
                ))
        else:
            # Non-verbose: just zones with {high, low}
            for zone in study.get("zones", []):
                zones.append(FVGZone(
                    direction=Direction.BULLISH,  # can't determine without color
                    bottom=zone.get("low", 0),
                    top=zone.get("high", 0),
                ))

    return zones


def parse_ob_boxes(boxes_data: dict[str, Any]) -> list[OrderBlock]:
    """Parse Order Block zones from order_block.pine boxes."""
    blocks = []
    if not boxes_data.get("success") or not boxes_data.get("studies"):
        return blocks

    for study in boxes_data["studies"]:
        name = study.get("name", "")
        if "Order Block" not in name and "order_block" not in name.lower() and "OB" not in name and "SMT" not in name:
            continue

        boxes = study.get("all_boxes", [])
        if boxes:
            for box in boxes:
                high = box.get("high")
                low = box.get("low")
                if high is None or low is None:
                    continue

                bg_color = str(box.get("bgColor", ""))
                direction = _direction_from_color(bg_color, Direction.BULLISH)

                blocks.append(OrderBlock(
                    direction=direction,
                    bottom=low,
                    top=high,
                ))
        else:
            for zone in study.get("zones", []):
                blocks.append(OrderBlock(
                    direction=Direction.BULLISH,
                    bottom=zone.get("low", 0),
                    top=zone.get("high", 0),
                ))

    return blocks


def _direction_from_color(color_str: str, default: Direction = Direction.BULLISH) -> Direction:
    """Infer direction from a color value.

    Green-ish colors = bullish, red-ish colors = bearish.
    TradingView color indices or hex values.
    """
    if not color_str:
        return default

    color_lower = color_str.lower()

    # Hex color analysis
    if "#" in color_lower:
        hex_match = re.search(r"#([0-9a-f]{6})", color_lower)
        if hex_match:
            r = int(hex_match.group(1)[0:2], 16)
            g = int(hex_match.group(1)[2:4], 16)
            if g > r + 30:
                return Direction.BULLISH
            if r > g + 30:
                return Direction.BEARISH

    # Color name hints
    if any(w in color_lower for w in ("green", "bull", "lime", "teal")):
        return Direction.BULLISH
    if any(w in color_lower for w in ("red", "bear", "crimson", "pink")):
        return Direction.BEARISH

    return default


# ── Line parsers (Session levels) ────────────────────────────────────────────


def parse_session_lines(lines_data: dict[str, Any]) -> list[SessionLevel]:
    """Parse session level lines from session_levels.pine.

    The script draws horizontal lines for session H/L, PDH/PDL, PWH/PWL.
    With verbose=True, we get all_lines with y1, y2 values.
    Without verbose, we get horizontal_levels (just prices, no names).
    """
    levels = []
    if not lines_data.get("success") or not lines_data.get("studies"):
        return levels

    for study in lines_data["studies"]:
        name = study.get("name", "")
        if "Session" not in name and "session" not in name.lower() and "SMT" not in name:
            continue

        # Use horizontal_levels as fallback (unnamed)
        for i, price in enumerate(study.get("horizontal_levels", [])):
            levels.append(SessionLevel(
                name=f"session_level_{i}",
                price=price,
            ))

    return levels


# ── Main reader class ────────────────────────────────────────────────────────


class StructuredReader:
    """Reads Pine Script outputs from TradingView and builds MarketState.

    Usage::

        reader = StructuredReader()
        state = await reader.read_market_state()
        print(state.model_dump_json(indent=2))
    """

    def __init__(self, bridge: Optional[TVBridge] = None):
        self.bridge = bridge or TVBridge()
        self._last_state: Optional[MarketState] = None

    async def read_market_state(self) -> MarketState:
        """Fetch all Pine data and assemble into a MarketState.

        Calls TradingView MCP in parallel (tables + labels + boxes + lines + status),
        then parses each into structured models.

        Two tables are parsed:
          - SMT Signals (smt_overlay.pine) — PA + VMC Cipher A
          - SMT Oscillator (smt_oscillator.pine) — momentum + divergences
        """
        raw = await self.bridge.fetch_all_pine_data()

        # Parse chart metadata
        status = raw.get("status", {})
        symbol = status.get("chart_symbol", "")
        timeframe = status.get("chart_resolution", "")

        tables_data = raw.get("tables", {})

        # Parse SMT Signals table (primary — PA + VMC A)
        signal_table = parse_signal_table(tables_data)
        if signal_table is None:
            logger.warning("SMT Signals table not found — using empty state")
            signal_table = SignalTableData()

        # Parse SMT Oscillator table (secondary — momentum + divergences)
        oscillator = parse_oscillator_table(tables_data)
        if oscillator is None:
            logger.warning("SMT Oscillator table not found — using empty state")
            oscillator = OscillatorTableData()

        # Parse individual drawing objects (supplementary detail)
        labels_data = raw.get("labels", {})
        boxes_data = raw.get("boxes", {})
        lines_data = raw.get("lines", {})

        mss_events = parse_mss_labels(labels_data)
        sweep_events = parse_sweep_labels(labels_data)
        fvg_zones = parse_fvg_boxes(boxes_data)
        order_blocks = parse_ob_boxes(boxes_data)
        session_levels = parse_session_lines(lines_data)

        # Build liquidity levels from signal table data
        liquidity_levels = []
        for price in signal_table.liquidity_above:
            liquidity_levels.append(LiquidityLevel(price=price, side="above"))
        for price in signal_table.liquidity_below:
            liquidity_levels.append(LiquidityLevel(price=price, side="below"))

        state = MarketState(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=datetime.now(timezone.utc),
            signal_table=signal_table,
            oscillator=oscillator,
            mss_events=mss_events,
            fvg_zones=fvg_zones,
            order_blocks=order_blocks,
            liquidity_levels=liquidity_levels,
            sweep_events=sweep_events,
            session_levels=session_levels,
        )

        self._last_state = state
        return state

    @property
    def last_state(self) -> Optional[MarketState]:
        """Return the last read market state (or None if never read)."""
        return self._last_state
