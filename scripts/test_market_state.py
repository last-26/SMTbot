"""Phase 1.6 validation script — read Pine Script data and print MarketState.

Run this with TradingView Desktop open and Pine Scripts loaded on the chart:
  python scripts/test_market_state.py

For continuous polling (every N seconds):
  python scripts/test_market_state.py --poll 10

Prerequisites:
  1. TradingView Desktop running with --remote-debugging-port=9222
  2. Pine Scripts loaded on chart:
     - SMT Master Overlay (pine/smt_overlay.pine) — chart overlay
     - SMT Master Oscillator (pine/smt_oscillator.pine) — lower pane
  3. Chart showing BYBIT:BTCUSDT.P (or any symbol for testing)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.tv_bridge import TVBridge
from src.data.structured_reader import StructuredReader


async def run_once(reader: StructuredReader, verbose: bool = False) -> None:
    """Read market state once and print it."""
    print("=" * 60)
    print("Fetching market state from TradingView...")
    print("=" * 60)

    state = await reader.read_market_state()

    # Print as formatted JSON
    data = state.model_dump(mode="json")
    print(json.dumps(data, indent=2, default=str))

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Symbol:          {state.symbol}")
    print(f"  Timeframe:       {state.timeframe}")
    print(f"  Price:           {state.current_price}")
    print(f"  HTF Trend:       {state.trend_htf.value}")
    print(f"  LTF Trend:       {state.trend_ltf.value}")
    print(f"  Structure:       {state.signal_table.structure}")
    print(f"  Confluence:      {state.confluence_score}/7")
    print(f"  Session:         {state.active_session.value}")
    print(f"  ATR(14):         {state.atr}")
    st = state.signal_table
    print(f"  --- Price Action (SMT Signals) ---")
    print(f"  Last MSS:        {st.last_mss or '\u2014'}")
    print(f"  Active FVG:      {st.active_fvg or '\u2014'}")
    print(f"  Active OB:       {st.active_ob or '\u2014'}")
    print(f"  Last Sweep:      {st.last_sweep or '\u2014'}")
    print(f"  Liq Above:       {st.liquidity_above or '\u2014'}")
    print(f"  Liq Below:       {st.liquidity_below or '\u2014'}")
    print(f"  --- VMC Cipher A (SMT Signals) ---")
    print(f"  VMC Ribbon:      {st.vmc_ribbon}")
    print(f"  VMC WT Bias:     {st.vmc_wt_bias} ({st.vmc_wt_value})")
    print(f"  VMC WT Cross:    {st.vmc_wt_cross}")
    print(f"  VMC Last Signal: {st.vmc_last_signal}")
    print(f"  VMC RSI+MFI:     {st.vmc_rsi_mfi}")
    osc = state.oscillator
    print(f"  --- Momentum (SMT Oscillator) ---")
    print(f"  WT1/WT2:         {osc.wt1} / {osc.wt2}")
    print(f"  WT State:        {osc.wt_state}")
    print(f"  WT Cross:        {osc.wt_cross}")
    print(f"  WT VWAP Fast:    {osc.wt_vwap_fast}")
    print(f"  RSI:             {osc.rsi} ({osc.rsi_state})")
    print(f"  RSI+MFI:         {osc.rsi_mfi} ({osc.rsi_mfi_bias})")
    print(f"  Stoch K/D:       {osc.stoch_k} / {osc.stoch_d} ({osc.stoch_state})")
    print(f"  Last Signal:     {osc.last_signal} ({osc.last_signal_bars_ago}b ago)")
    print(f"  Last WT Div:     {osc.last_wt_div} ({osc.last_wt_div_bars_ago}b ago)")
    print(f"  Momentum:        {osc.momentum}/5")
    print(f"  --- Drawing Objects ---")
    print(f"  MSS Events:      {len(state.mss_events)}")
    print(f"  FVG Zones:       {len(state.fvg_zones)}")
    print(f"  Order Blocks:    {len(state.order_blocks)}")
    print(f"  Sweep Events:    {len(state.sweep_events)}")
    print(f"  Session Levels:  {len(state.session_levels)}")
    print(f"  Timestamp:       {state.timestamp}")


async def run_poll(reader: StructuredReader, interval: int) -> None:
    """Poll market state every N seconds."""
    print(f"Polling every {interval}s. Press Ctrl+C to stop.\n")
    cycle = 0
    while True:
        cycle += 1
        print(f"\n{'#' * 60}")
        print(f"  POLL CYCLE {cycle}")
        print(f"{'#' * 60}")
        await run_once(reader)
        await asyncio.sleep(interval)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test MarketState reader (Phase 1.6)")
    parser.add_argument("--poll", type=int, default=0,
                        help="Poll interval in seconds (0 = run once)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show raw TV CLI responses")
    args = parser.parse_args()

    # Check TradingView connection first
    bridge = TVBridge()
    status = await bridge.status()

    if not status.get("success"):
        print("ERROR: Cannot connect to TradingView Desktop.")
        print("Make sure TradingView is running with --remote-debugging-port=9222")
        print(f"Details: {status.get('error', 'unknown')}")
        sys.exit(1)

    print(f"Connected to TradingView: {status.get('chart_symbol')} @ {status.get('chart_resolution')}")
    print(f"CDP target: {status.get('target_url')}")

    if args.verbose:
        # Print raw data for debugging
        raw = await bridge.fetch_all_pine_data()
        print("\nRaw Pine data:")
        print(json.dumps(raw, indent=2, default=str))

    reader = StructuredReader(bridge)

    if args.poll > 0:
        try:
            await run_poll(reader, args.poll)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        await run_once(reader)


if __name__ == "__main__":
    asyncio.run(main())
