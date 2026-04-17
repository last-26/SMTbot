# SMTbot — AI-Powered Crypto Futures Trading Bot

[![SafeSkill 60/100](https://img.shields.io/badge/SafeSkill-60%2F100_Use%20with%20Caution-orange)](https://safeskill.dev/scan/last-26-smtbot)
An AI-powered cryptocurrency futures trading bot that analyzes price action and liquidity patterns using custom Pine Scripts on TradingView, executes trades on OKX via R:R-based risk management, and continuously improves through reinforcement learning.

## How It Works

```
TradingView (Eyes)          Claude Code (Brain)           OKX Exchange (Hands)
┌──────────────────┐       ┌───────────────────┐        ┌──────────────────┐
│ Custom Pine       │       │ Orchestrator       │        │ Demo / Live      │
│ Scripts detect:   │──────▶│                    │───────▶│                  │
│ - MSS / BOS       │ MCP   │ Writes Pine Scripts│  MCP   │ Place orders     │
│ - FVG zones       │       │ Builds RL model    │        │ Manage positions │
│ - Order Blocks    │       │ Debugs strategies  │        │ Algo SL/TP       │
│ - Liquidity sweeps│       │                    │        │ Account mgmt     │
│ - Session levels  │       └────────┬───────────┘        └──────────────────┘
└──────────────────┘                │
                                    ▼
                        ┌───────────────────────┐
                        │ Python Bot (Autonomo.) │
                        │                        │
                        │ Analysis Engine         │
                        │ R:R Strategy Engine     │
                        │ OKX Execution           │
                        │ Trade Journal (SQLite)  │
                        │ RL Parameter Tuning     │
                        └────────────────────────┘
```

**Key principle:** Claude orchestrates and builds the system. A Python-based RL agent makes the actual per-candle trade decisions at runtime.

## Pine Script Indicators

| Script | Purpose |
|---|---|
| `mss_detector.pine` | Swing H/L detection, HH/HL/LH/LL classification, MSS & BOS signals |
| `fvg_mapper.pine` | Fair Value Gap detection, mitigation tracking, nearest FVG levels |
| `order_block.pine` | Order Block identification, test/break lifecycle, ATR-filtered impulse |
| `liquidity_sweep.pine` | Equal highs/lows pooling, sweep event detection |
| `session_levels.pine` | Asian/London/NY session H/L, Previous Day & Week levels |
| `signal_table.pine` | Master aggregator — all signals + confluence score in one table |

All scripts output structured data via tooltips and tables, readable by the bot through TradingView MCP (`tv stream tables --filter "Signals"`).

## Development Phases

| Phase | Description | Status |
|---|---|---|
| 1 | Pine Script data layer + Python data bridge | In progress |
| 2 | Analysis engine (confluence scoring, pattern detection) | Planned |
| 3 | R:R strategy engine (dynamic SL/TP, position sizing, circuit breakers) | Planned |
| 4 | OKX execution (order placement, position management) | Planned |
| 5 | Trade journal (SQLite logging, performance metrics) | Planned |
| 6 | Reinforcement learning (PPO parameter tuning, walk-forward validation) | Planned |

## Tech Stack

- **Python 3.11+** — bot core, analysis, RL training
- **Pine Script v6** — TradingView indicators
- **TradingView MCP** — chart data & Pine Script management (78 tools)
- **OKX Agent Trade Kit MCP** — trade execution (107 tools)
- **Stable Baselines3 / PPO** — reinforcement learning
- **SQLite** — trade journal

## Quick Start

```bash
# Clone
git clone https://github.com/last-26/SMTbot.git
cd SMTbot

# Copy env template
cp .env.example .env
# Edit .env with your OKX demo API credentials

# Install Python dependencies (coming in Phase 2)
# pip install -r requirements.txt

# Pine Scripts: load into TradingView via MCP
# tv pine set < pine/signal_table.pine && tv pine compile
```

## Safety

- Always start in **demo mode** (`OKX_DEMO_FLAG=1`)
- Circuit breakers: 3% daily loss limit, 10% max drawdown, 5 consecutive loss stop
- Minimum 1:2 R:R enforced on every trade
- Never risk more than you can afford to lose

## License

See [LICENSE](LICENSE) file.
