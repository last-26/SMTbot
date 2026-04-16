# CLAUDE.md — Crypto Futures Trading Bot (v2)

## Project overview

An AI-powered cryptocurrency futures trading bot that combines two MCP bridges:
1. **TradingView MCP** — reads live chart data, indicator values, Pine Script drawing objects, and manages Pine Script development
2. **OKX Agent Trade Kit MCP** — executes trades on OKX exchange (demo first, live later)

The bot analyzes price action and liquidity patterns using structured data from custom Pine Scripts, executes trades through an R:R (Risk/Reward) system, and continuously improves its win rate via a reinforcement learning feedback loop.

### Key architectural principle

Claude Code + MCP = **Orkestra Şefi** (Orchestrator). Claude writes Pine Scripts, debugs strategies, coordinates the system. The actual trade decisions come from a **Python-based RL agent** that Claude writes, trains, and optimizes. Claude does NOT make per-candle decisions at runtime — the Python bot does.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        CLAUDE CODE (Orchestrator)                       │
│  Writes Pine Scripts, builds/trains RL model, debugs, manages system    │
│                                                                         │
│  MCP Server 1: TradingView          MCP Server 2: OKX Agent Trade Kit  │
│  (tradingview-mcp)                  (okx-trade-mcp --profile demo)     │
│  78 tools: chart data, Pine,        107 tools: orders, positions,      │
│  indicators, drawings               market data, algo orders           │
└────────────┬─────────────────────────────────────┬──────────────────────┘
             │                                     │
             ▼                                     ▼
┌────────────────────────┐            ┌────────────────────────────────┐
│  TradingView Desktop   │            │  OKX Exchange                  │
│  (CDP port 9222)       │            │  Demo: --profile demo          │
│                        │            │  Live: --profile live           │
│  Custom Pine Scripts:  │            │                                │
│  - SMT Overlay (chart) │            │  Supported:                    │
│    MSS/FVG/OB/Liq/Sess │            │  - BTC-USDT-SWAP               │
│    + VMC Cipher A       │            │  - ETH-USDT-SWAP               │
│  - SMT Oscillator       │            │  - All perpetual swaps         │
│    WT/RSI/MFI/Stoch/Div│            │  - Algo orders (OCO, trailing) │
└────────────┬───────────┘            └──────────────┬─────────────────┘
             │                                       │
             ▼                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Python Bot Core (Autonomous)                     │
│                                                                      │
│  ┌─────────────┐ ┌────────────────┐ ┌─────────────────────────────┐ │
│  │ Data Layer   │ │ Analysis       │ │ Strategy Engine (R:R)       │ │
│  │             │ │ Engine         │ │                             │ │
│  │ TV stream → │ │                │ │ - Entry/exit signals        │ │
│  │ Structured  │ │ - PA patterns  │ │ - Dynamic SL/TP            │ │
│  │ JSON from   │ │ - MSS/CHoCH   │ │ - Dynamic leverage+size     │ │
│  │ Pine Scripts│ │ - FVG zones    │ │ - Circuit breakers          │ │
│  │             │ │ - OB levels    │ │ - Min R:R enforcement       │ │
│  │ OKX WS →   │ │ - Liquidity    │ │                             │ │
│  │ Order book  │ │   sweeps       │ │                             │ │
│  │ Tick data   │ │ - HTF/LTF      │ │                             │ │
│  └─────────────┘ │   confluence   │ └─────────────────────────────┘ │
│                  └────────────────┘                                  │
│  ┌─────────────┐ ┌────────────────┐ ┌─────────────────────────────┐ │
│  │ Execution   │ │ Trade Journal  │ │ RL Module                   │ │
│  │             │ │                │ │                             │ │
│  │ OKX API v5  │ │ SQLite DB      │ │ Stable Baselines3 / PPO    │ │
│  │ via python- │ │ Every trade    │ │ Parameter tuning agent      │ │
│  │ okx SDK     │ │ with context   │ │ Walk-forward validation     │ │
│  │ or MCP CLI  │ │ + screenshots  │ │ Weekly retrain cycle        │ │
│  └─────────────┘ └────────────────┘ └─────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

## Why two MCP servers?

| Capability | TradingView MCP | OKX MCP |
|---|---|---|
| Live chart data (OHLCV) | ✓ (from your chart) | ✓ (API candles) |
| Custom indicator values | ✓ | ✗ |
| Pine Script drawings (lines, boxes, labels) | ✓ | ✗ |
| Pine Script write/compile/debug | ✓ | ✗ |
| Chart screenshots | ✓ | ✗ |
| 70+ technical indicators | ✗ | ✓ (built-in, no auth) |
| Order placement | ✗ | ✓ |
| Algo orders (OCO, trailing stop) | ✗ | ✓ |
| Position management | ✗ | ✓ |
| Account balance/P&L | ✗ | ✓ |
| Order book depth | ✓ (from TV) | ✓ (from OKX) |
| Demo trading | ✗ (UI only) | ✓ (full API) |

TradingView = eyes (analysis). OKX = hands (execution).

## Prerequisites

- Node.js 18+ (for both MCP servers)
- Python 3.11+
- TradingView Desktop with valid subscription
- OKX account (demo trading requires no deposit)
- Claude Code (primary development tool)
- Hardware: capable CPU recommended (user has Ryzen 9800X3D)

## MCP Setup

### 1. TradingView MCP (chart data + Pine Script)

```bash
# Install
git clone https://github.com/tradesdontlie/tradingview-mcp
cd tradingview-mcp
npm install

# Add to Claude Code MCP config
# In ~/.claude/.mcp.json:
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["/path/to/tradingview-mcp/src/index.js"],
      "env": {
        "TV_DEBUG_PORT": "9222"
      }
    }
  }
}

# Launch TradingView Desktop with debug port
# Windows shortcut: add --remote-debugging-port=9222 to target
# CLI binary is 'tv', not 'tradingview-mcp'
```

Key TV CLI commands:
```bash
tv status                              # Current symbol, TF, indicators
tv quote                               # Real-time quote
tv ohlcv --summary                     # OHLCV bars
tv data lines                          # Pine Script line drawings
tv data values                         # Indicator data window
tv pine set < script.pine              # Load Pine Script
tv pine compile                        # Compile
tv pine analyze                        # Static analysis
tv pine check                          # Server-side compile check
tv stream quote | jq '.close'          # Stream filtered data
tv stream bars                         # Stream candle updates
tv stream lines --filter "MSS"         # Stream specific drawings
tv stream tables --filter "Signals"    # Stream table data
tv stream all                          # Stream everything
tv screenshot                          # Capture chart
tv symbol BTC-USDT                     # Change symbol (use OKX format)
tv timeframe 15                        # Change timeframe
```

### 2. OKX Agent Trade Kit MCP (trade execution)

```bash
# Install globally
npm install -g okx-trade-mcp okx-trade-cli

# Auto-setup for Claude Code (recommended)
okx setup --client claude-code --profile demo --modules all

# Or manual setup in ~/.claude/.mcp.json:
{
  "mcpServers": {
    "okx": {
      "command": "okx-trade-mcp",
      "args": ["--profile", "demo", "--modules", "all"],
      "env": {
        "OKX_API_KEY": "your_demo_api_key",
        "OKX_API_SECRET": "your_demo_secret",
        "OKX_PASSPHRASE": "your_demo_passphrase"
      }
    }
  }
}
```

OKX Demo API key creation:
1. Log in to OKX → Trade → Demo Trading
2. Click Settings icon → Account Mode → set to Single Currency Margin Mode
3. Top right user icon → Demo Trading API → Create Demo Trading V5 API Key
4. Enable Read + Trade permissions
5. Note: demo keys ≠ live keys, they are completely separate

Key OKX CLI commands:
```bash
# Market data (no auth needed)
okx market ticker BTC-USDT
okx market orderbook BTC-USDT-SWAP --sz 10
okx market candles BTC-USDT-SWAP --bar 15m --limit 100
okx market funding-rate BTC-USDT-SWAP
okx market open-interest --instType SWAP

# Account
okx account balance
okx account positions
okx account config

# Trading (demo mode)
okx swap place --instId BTC-USDT-SWAP --side buy --posSide long \
  --ordType market --sz 1
okx swap place --instId BTC-USDT-SWAP --side buy --posSide long \
  --ordType limit --px 68000 --sz 1

# Algo orders (SL/TP)
okx algo place --instId BTC-USDT-SWAP --side sell --posSide long \
  --ordType conditional --slTriggerPx 67500 --slOrdPx -1 \
  --tpTriggerPx 72000 --tpOrdPx -1 --sz 1

# Set leverage
okx account set-leverage --instId BTC-USDT-SWAP --lever 10 --mgnMode cross
```

### Important: OKX instrument naming

OKX uses different naming from other exchanges:
- Perpetual swap: `BTC-USDT-SWAP` (not BTCUSDT)
- Spot: `BTC-USDT`
- Futures (dated): `BTC-USDT-250425`

Set TradingView to display OKX charts: symbol `OKX:BTCUSDT.P` for the perpetual.

## Project structure

```
trading-bot/
├── CLAUDE.md
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .env                          # gitignored
│
├── config/
│   ├── default.yaml              # Bot configuration
│   └── pairs/
│       ├── BTC-USDT-SWAP.yaml    # Per-pair overrides
│       └── ETH-USDT-SWAP.yaml
│
├── pine/                         # Pine Script indicators
│   ├── smt_overlay.pine          # SMT Master Overlay (chart): PA + VMC Cipher A — ACTIVE
│   ├── smt_oscillator.pine       # SMT Master Oscillator (lower pane): VMC Cipher B — ACTIVE
│   ├── vmc_a.txt                 # VuManChu Cipher A reference source (porting input)
│   ├── vmc_b.txt                 # VuManChu Cipher B reference source (porting input)
│   ├── mss_detector.pine         # Standalone MSS detector (reference, not loaded)
│   ├── fvg_mapper.pine           # Standalone FVG mapper (reference, not loaded)
│   ├── order_block.pine          # Standalone OB identifier (reference, not loaded)
│   ├── liquidity_sweep.pine      # Standalone liquidity sweep (reference, not loaded)
│   └── session_levels.pine       # Standalone session levels (reference, not loaded)
│
├── src/
│   ├── __init__.py
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── tv_bridge.py          # TradingView MCP data reader
│   │   ├── okx_bridge.py         # OKX market data (WebSocket + REST)
│   │   ├── candle_buffer.py      # Multi-TF rolling candle buffer
│   │   └── structured_reader.py  # Read Pine Script drawings → structured JSON
│   │
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── price_action.py       # Candlestick pattern detection
│   │   ├── market_structure.py   # HH/HL/LH/LL, BOS, CHoCH, MSS
│   │   ├── liquidity.py          # Liquidity zones, sweeps, equal H/L
│   │   ├── fvg.py                # Fair Value Gap detection
│   │   ├── order_blocks.py       # Order Block identification
│   │   ├── support_resistance.py # S/R level detection and scoring
│   │   └── multi_timeframe.py    # HTF bias + LTF entry confluence
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── rr_system.py          # R:R calculation engine
│   │   ├── entry_signals.py      # Signal generation with confluence
│   │   ├── position_sizer.py     # Dynamic position size + leverage calc
│   │   ├── risk_manager.py       # Circuit breakers, drawdown limits
│   │   └── trade_plan.py         # Trade plan data structure
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── okx_executor.py       # OKX order execution (API v5)
│   │   ├── order_manager.py      # Order lifecycle (entry → SL/TP → exit)
│   │   └── position_tracker.py   # Real-time position monitoring
│   │
│   ├── learning/
│   │   ├── __init__.py
│   │   ├── feature_extractor.py  # Trade context → feature vector
│   │   ├── rl_agent.py           # PPO agent for parameter tuning
│   │   ├── reward.py             # Reward function (R-based + Sharpe)
│   │   ├── environment.py        # Gymnasium environment wrapper
│   │   └── walk_forward.py       # Walk-forward validation
│   │
│   ├── journal/
│   │   ├── __init__.py
│   │   ├── database.py           # SQLite trade journal
│   │   ├── models.py             # Pydantic data models
│   │   └── reporter.py           # Performance reports
│   │
│   └── bot.py                    # Main bot loop
│
├── scripts/
│   ├── setup.sh                  # Full environment setup
│   ├── start_bot.sh              # Launch bot
│   ├── train_rl.py               # Manual RL retraining
│   └── report.py                 # Generate performance report
│
├── tests/
│   ├── test_price_action.py
│   ├── test_rr_system.py
│   ├── test_market_structure.py
│   ├── test_position_sizer.py
│   └── test_structured_reader.py
│
└── data/
    ├── journal.db                # gitignored
    ├── models/                   # RL model checkpoints
    └── screenshots/              # Chart screenshots at trade time
```

## Development phases

### Phase 1: Infrastructure + Pine Script data layer

**Goal:** Read structured analysis data from TradingView, not raw OHLCV.

This is the critical insight from the architecture review: RL models struggle with raw candle data. Instead, Claude writes Pine Scripts that detect PA/liquidity structures and output them as drawing objects (labels, lines, boxes, tables). The bot reads these pre-analyzed structures via MCP.

#### Step 1.1: Pine Script — MSS (Market Structure Shift) Detector

Claude writes a Pine Script that:
- Identifies swing highs and swing lows (configurable lookback)
- Labels them as HH, HL, LH, LL
- Draws a label when MSS occurs (trend reversal signal)
- Outputs via `label.new()` with structured text: `"MSS|BULLISH|69450.5|2024-01-15T14:30:00"`

The bot reads these labels via `tv data values` or `tv stream all`.

#### Step 1.2: Pine Script — FVG (Fair Value Gap) Mapper

Claude writes a Pine Script that:
- Detects 3-candle imbalance zones
- Draws boxes (`box.new()`) at FVG zones with structured tooltip
- Color-codes: bullish FVG = green, bearish FVG = red
- Marks whether FVG has been filled (mitigated) or is still open

#### Step 1.3: Pine Script — Order Block Identifier

Claude writes a Pine Script that:
- Finds the last opposing candle before an impulsive move
- Draws boxes at OB zones
- Tracks OB validity (has price returned to it? has it been broken?)

#### Step 1.4: Pine Script — Liquidity Sweep Detector

Claude writes a Pine Script that:
- Identifies equal highs/lows (liquidity pools)
- Detects when price sweeps these levels and reverses
- Outputs sweep events as labels

#### Step 1.5: Pine Script — Master Signal Table

Claude writes a Pine Script that aggregates all indicators into a single `table.new()` output:
```
| Field              | Value           |
|--------------------|-----------------|
| trend_htf          | BULLISH         |
| trend_ltf          | BEARISH         |
| last_mss           | BULLISH@69450   |
| active_fvg         | BULL@68900-69100|
| active_ob          | BULL@68500-68700|
| liquidity_above    | 70200,70350     |
| liquidity_below    | 68100,67950     |
| last_sweep         | BEAR@70350      |
| confluence_score   | 3               |
```

The bot reads this table via `tv stream tables --filter "Signals"` to get a **structured snapshot** of the market state every candle.

#### Step 1.6: Data bridge

Build `structured_reader.py` that:
- Connects to TradingView MCP
- Parses Pine Script drawing objects into Python dataclasses
- Maintains current market state as a structured object
- Updates on every new candle

Validation: the bot prints a structured market state JSON for every 15m candle on BTCUSDT.

### Phase 2: Analysis engine (Python-side)

Even with Pine Script doing heavy lifting, the Python bot needs its own analysis layer for:
- Cross-referencing multiple Pine Script outputs into confluence scores
- Pattern-specific confidence weighting
- Time-based filtering (session awareness)
- Correlation with OKX order book depth (liquidity confirmation)

#### Candlestick patterns (Python detection):
- Engulfing (bullish/bearish)
- Pin bar / hammer / shooting star
- Inside bar
- Morning/evening star
- Doji

These supplement the Pine Script detections — belt and suspenders approach.

#### Multi-timeframe confluence logic:
```python
def calculate_confluence(market_state: MarketState) -> int:
    score = 0

    # 1. HTF trend alignment
    if market_state.htf_trend == signal_direction:
        score += 1

    # 2. Price at key level (OB, FVG, or S/R)
    if market_state.at_order_block or market_state.at_fvg:
        score += 1

    # 3. Recent liquidity sweep in signal direction
    if market_state.recent_sweep_direction == signal_direction:
        score += 1

    # 4. MSS / BOS confirmation
    if market_state.last_mss_direction == signal_direction:
        score += 1

    # 5. LTF pattern confirmation
    if has_entry_pattern(market_state.ltf_candles, signal_direction):
        score += 1

    return score  # Minimum 2 required to trade
```

### Phase 3: Strategy engine (R:R system)

#### Core R:R math:
```python
def calculate_trade_plan(
    direction: str,        # "LONG" or "SHORT"
    entry_price: float,
    sl_price: float,
    account_balance: float,
    risk_pct: float,       # e.g., 0.01 for 1%
    rr_ratio: float,       # e.g., 3.0
    max_leverage: int       # e.g., 20
) -> TradePlan:

    risk_amount = account_balance * risk_pct
    sl_distance = abs(entry_price - sl_price)
    sl_pct = sl_distance / entry_price

    # Take profit based on R:R
    if direction == "LONG":
        tp_price = entry_price + (sl_distance * rr_ratio)
    else:
        tp_price = entry_price - (sl_distance * rr_ratio)

    # Position size from risk
    position_size_usdt = risk_amount / sl_pct

    # Required leverage
    required_leverage = position_size_usdt / account_balance
    leverage = min(round(required_leverage), max_leverage)

    # Adjust position if leverage capped
    if required_leverage > max_leverage:
        position_size_usdt = account_balance * max_leverage
        actual_risk = position_size_usdt * sl_pct
    else:
        actual_risk = risk_amount

    # Convert to OKX contract size
    # OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC
    contract_value = 0.01 * entry_price  # in USDT
    num_contracts = int(position_size_usdt / contract_value)

    return TradePlan(
        direction=direction,
        entry_price=entry_price,
        stop_loss=sl_price,
        take_profit=tp_price,
        rr_ratio=rr_ratio,
        risk_amount=actual_risk,
        position_size_usdt=position_size_usdt,
        num_contracts=num_contracts,
        leverage=leverage,
        sl_distance_pct=sl_pct * 100
    )
```

Break-even win rates:
- 1:1 R:R = 50.0% needed
- 1:2 R:R = 33.3% needed
- 1:3 R:R = 25.0% needed
- 1:4 R:R = 20.0% needed

#### Dynamic leverage: NEVER use fixed leverage.
- Tight stop (0.3% from entry) → higher leverage (~15-20x)
- Wide stop (2% from entry) → lower leverage (~3-5x)
- The risk in USDT stays constant regardless of leverage

#### Stop loss placement rules:
- LONG: SL below the order block / FVG / swing low that triggered entry
- SHORT: SL above the order block / FVG / swing high that triggered entry
- Add buffer: SL should be ATR(14) * 0.2 beyond the invalidation level
- Never place SL at exact round number (manipulation target)

#### Circuit breakers (non-negotiable):
```yaml
max_daily_loss_pct: 3.0          # Stop trading for 24h
max_consecutive_losses: 5         # Stop + alert user
max_drawdown_from_peak_pct: 10.0  # Full stop, require manual restart
max_concurrent_positions: 2       # Start with 2
max_leverage: 20                  # Per-pair configurable
min_rr_ratio: 2.0                 # Never trade below 1:2
no_trade_before_news: true        # Pause around high-impact events
```

### Phase 4: Execution via OKX

#### Order flow for a trade:
1. Signal fires with TradePlan
2. Set leverage: `okx account set-leverage --instId BTC-USDT-SWAP --lever {n} --mgnMode isolated`
3. Place entry order (market or limit)
4. Immediately place algo SL/TP order (OCO)
5. Monitor position via WebSocket
6. On exit (SL or TP hit), log to journal
7. If partial fill, manage remaining size

#### Using OKX Python SDK:
```python
import okx.Trade as Trade
import okx.Account as Account

# flag = "1" for demo, "0" for live
flag = "1"

tradeAPI = Trade.TradeAPI(api_key, secret_key, passphrase, False, flag)
accountAPI = Account.AccountAPI(api_key, secret_key, passphrase, False, flag)

# Set leverage
accountAPI.set_leverage(
    instId="BTC-USDT-SWAP",
    lever="10",
    mgnMode="isolated"
)

# Place market order
result = tradeAPI.place_order(
    instId="BTC-USDT-SWAP",
    tdMode="isolated",
    side="buy",
    posSide="long",
    ordType="market",
    sz="1"  # number of contracts
)

# Place algo SL/TP (OCO)
tradeAPI.place_algo_order(
    instId="BTC-USDT-SWAP",
    tdMode="isolated",
    side="sell",
    posSide="long",
    ordType="oco",
    sz="1",
    slTriggerPx="67500",
    slOrdPx="-1",      # -1 = market
    tpTriggerPx="72000",
    tpOrdPx="-1"
)
```

### Phase 5: Trade journal

Every trade logged with:
```python
@dataclass
class TradeRecord:
    trade_id: str
    timestamp_signal: datetime
    timestamp_entry: datetime
    timestamp_exit: datetime
    symbol: str                    # BTC-USDT-SWAP
    direction: str                 # LONG / SHORT
    entry_timeframe: str           # 15m
    htf_bias: str                  # BULLISH / BEARISH / RANGING

    entry_price: float
    stop_loss: float
    take_profit: float
    exit_price: float

    rr_ratio: float                # planned R:R
    leverage: int
    num_contracts: int
    risk_amount_usdt: float

    pnl_usdt: float
    pnl_r: float                   # +2.8R or -1.0R
    fees_usdt: float
    outcome: str                   # WIN / LOSS / BREAKEVEN

    confluence_score: int
    patterns_detected: list[str]   # ["bullish_engulfing", "fvg_entry"]
    market_structure: str          # "HH_HL_bullish"
    liquidity_context: str         # "swept_equal_lows_68100"
    ob_level: Optional[str]        # "bullish_ob@68500"
    fvg_level: Optional[str]       # "bullish_fvg@68900-69100"

    screenshot_entry: str          # path to chart screenshot
    screenshot_exit: str
    notes: str
```

Performance metrics:
- Win rate (overall + per pattern + per session)
- Average R gained per trade
- Profit factor = gross wins / gross losses
- Expectancy = (win_rate × avg_win_R) - (loss_rate × avg_loss_R)
- Max consecutive wins/losses
- Max drawdown from peak
- Sharpe ratio (daily returns)
- Calmar ratio (return / max drawdown)

### Phase 6: Reinforcement learning

#### Architecture: parameter tuner, NOT raw decision maker

The RL agent does NOT decide "buy/sell/hold". The rule-based strategy generates signals based on PA + liquidity confluence. The RL agent tunes:
- `confluence_threshold` (2-5): minimum confluence to accept a trade
- `pattern_weights` (dict): confidence for each pattern type (0.0-1.0)
- `min_rr_ratio` (1.5-5.0): minimum R:R to accept
- `risk_pct` (0.005-0.02): risk per trade
- `htf_required` (bool): whether HTF alignment is mandatory
- `session_filter` (list): active trading sessions (London, NY, Asian)
- `volatility_scale` (0.5-2.0): scale risk by ATR-based volatility
- `ob_vs_fvg_preference` (0.0-1.0): weight OB entries vs FVG entries

#### Reward function (R-based + Sharpe):
```python
def calculate_reward(trade: TradeRecord, recent_trades: list[TradeRecord]) -> float:
    # Primary: R-based P&L (the core metric)
    r_reward = trade.pnl_r

    # Penalty: taking a trade without sufficient setup
    if trade.confluence_score < 2:
        setup_penalty = -3.0  # Heavy penalty for undisciplined trades
    else:
        setup_penalty = 0.0

    # Penalty: excessive drawdown
    current_dd = calculate_current_drawdown(recent_trades)
    if current_dd > 0.05:
        dd_penalty = -2.0
    elif current_dd > 0.03:
        dd_penalty = -1.0
    else:
        dd_penalty = 0.0

    # Bonus: consistency (Sharpe-inspired)
    if len(recent_trades) >= 10:
        recent_r = [t.pnl_r for t in recent_trades[-10:]]
        sharpe = np.mean(recent_r) / (np.std(recent_r) + 1e-8)
        consistency_bonus = min(sharpe * 0.5, 1.5)  # Cap bonus
    else:
        consistency_bonus = 0.0

    return r_reward + setup_penalty + dd_penalty + consistency_bonus
```

#### Walk-forward optimization (WFO):
```
Cycle 1: Train on trades 1-100   → Validate on trades 101-150
Cycle 2: Train on trades 1-150   → Validate on trades 151-200
Cycle 3: Train on trades 1-200   → Validate on trades 201-250
...

Rules:
- Never deploy parameters that didn't improve on out-of-sample
- If parameters swing wildly between retrains → reduce learning rate
- Retrain trigger: every 50 new trades OR weekly (whichever comes first)
- Minimum data: 50 trades before first RL training
- Claude Code triggers retraining via: python scripts/train_rl.py
```

#### Training cycle (automated via Claude Code):
1. Bot runs for N trades (50-100) with current parameters
2. Claude triggers `python scripts/train_rl.py --min-trades 50`
3. Script extracts features from each trade context
4. Trains PPO agent (Stable Baselines3) on trade outcomes
5. Walk-forward validates on held-out trades
6. If improved → deploys new params to `config/strategies/active.yaml`
7. If not improved → keeps previous params, logs the attempt
8. Repeat

## Currency pair strategy

### Phase 1: BTC-USDT-SWAP only

- Highest liquidity, tightest spreads
- Most predictable PA patterns
- Available on OKX demo
- Enough data for RL training

### Phase 2: Add ETH-USDT-SWAP (only after criteria met)

Criteria to add second pair:
- BTC has >= 100 demo trades logged
- Win rate >= 40% with 1:2 R:R (or >= 33% with 1:3 R:R)
- Profit factor > 1.2
- RL module completed at least 2 training cycles
- Max drawdown stayed under 10%

### DO NOT add more pairs until Phase 2 is stable

## Configuration

### default.yaml
```yaml
bot:
  mode: demo  # demo | live
  poll_interval_seconds: 5
  timezone: UTC

trading:
  symbol: BTC-USDT-SWAP
  entry_timeframe: 15m
  htf_timeframe: 4H
  risk_per_trade_pct: 1.0
  max_leverage: 20
  default_rr_ratio: 3.0
  min_rr_ratio: 2.0
  max_concurrent_positions: 2

circuit_breakers:
  max_daily_loss_pct: 3.0
  max_consecutive_losses: 5
  max_drawdown_pct: 10.0
  cooldown_hours: 24

analysis:
  min_confluence_score: 2
  candle_buffer_size: 500
  swing_lookback: 20
  sr_min_touches: 3
  sr_zone_atr_mult: 0.5
  session_filter:
    - london      # 07:00-16:00 UTC
    - new_york    # 12:00-21:00 UTC

okx:
  base_url: https://www.okx.com
  demo_flag: "1"     # "1" = demo, "0" = live
  # API credentials in .env

rl:
  min_trades_to_train: 50
  retrain_every_n_trades: 50
  learning_rate: 0.0003
  gamma: 0.99
  ppo_epochs: 10
```

### .env.example
```
OKX_API_KEY=your_demo_api_key
OKX_API_SECRET=your_demo_secret
OKX_PASSPHRASE=your_demo_passphrase
OKX_DEMO_FLAG=1

TV_MCP_PORT=9222
LOG_LEVEL=INFO
```

## Tech stack

### Python:
```
pydantic>=2.0            # Data models
pyyaml>=6.0              # Config
python-dotenv>=1.0       # Env vars
aiosqlite>=0.20          # Trade journal
httpx>=0.27              # HTTP client

python-okx>=5.0          # OKX official SDK
websockets>=12.0         # OKX WebSocket

pandas>=2.0              # Data analysis
numpy>=1.26              # Numerics
ta>=0.11                 # Technical analysis (pure Python, no TA-Lib dependency)

stable-baselines3>=2.3   # RL (PPO)
gymnasium>=0.29          # RL environment
torch>=2.0               # PyTorch backend

loguru>=0.7              # Logging
rich>=13.0               # Terminal UI
schedule>=1.2            # Periodic tasks
```

### Node.js:
```
tradingview-mcp          # TradingView MCP server
okx-trade-mcp            # OKX MCP server (official)
okx-trade-cli            # OKX CLI tool
```

## Safety warnings

### TradingView MCP:
- NOT affiliated with TradingView Inc.
- Uses undocumented internal APIs via Electron debug interface
- Can break without notice on TradingView updates
- Pin your TradingView Desktop version
- All data stays local on your machine

### OKX Agent Trade Kit:
- Official OKX product, MIT licensed, open source
- Start in --demo mode (simulated funds, zero risk)
- Never enable withdrawal permissions on API key
- Bind API key to your machine's IP address
- AI behavior is non-deterministic — always verify before live trading
- Use sub-account with minimal permissions for live

### Trading risks:
- This is a research project, not financial advice
- Past performance ≠ future results
- Crypto futures carry liquidation risk
- Never risk more than you can afford to lose
- Always start with demo, graduate to live with minimal capital
- Check OKX Terms of Service for automated trading rules

### RL risks:
- Overfitting is the #1 risk — always walk-forward validate
- Markets change regime — a trending-market model fails in ranging
- Log everything — data is your most valuable asset
- Simple parameter tuning > complex deep RL architectures

## Workflow commands

```bash
# Setup
./scripts/setup.sh

# Start bot (demo)
python -m src.bot --config config/default.yaml

# Start bot (live, after demo validation)
OKX_DEMO_FLAG=0 python -m src.bot --config config/default.yaml

# Performance report
python scripts/report.py --last 7d

# RL retraining
python scripts/train_rl.py --min-trades 50 --walk-forward

# Run tests
pytest tests/ -v

# Pine Script development cycle
# (Claude Code handles this via TradingView MCP)
# 1. Claude writes .pine file
# 2. tv pine set < pine/mss_detector.pine
# 3. tv pine compile
# 4. If error → Claude reads error → fixes → recompiles
# 5. tv pine analyze (static analysis)
# 6. tv screenshot (visual verification)
```

## Development Progress

### Completed

#### Environment Setup (2026-04-16)

**TradingView MCP:**
- Cloned `tradingview-mcp` to `C:\Users\samet\Desktop\tradingview-mcp\` and ran `npm install`
- TradingView Desktop extracted from MSIX to `C:\TradingView\` (MSIX sandbox blocks debug port, standalone exe required)
- Launch command: `"C:\TradingView\TradingView.exe" --remote-debugging-port=9222`
- CDP verified working on `http://localhost:9222` (Chrome/Electron 140, TradingView 3.0.0)
- MCP config created at `~/.claude/.mcp.json` with tradingview server pointing to `C:/Users/samet/Desktop/tradingview-mcp/src/server.js`

**IMPORTANT — MSIX won't work for debug mode.** Windows Store / MSIX TradingView ignores `ELECTRON_EXTRA_LAUNCH_ARGS` due to sandbox isolation. Must use the extracted exe at `C:\TradingView\TradingView.exe` with `--remote-debugging-port=9222` argument.

**Python environment:**
- Python 3.14.0, Node.js v25.2.1
- Virtual env at `.venv/` with all dependencies installed (pydantic, httpx, pandas, numpy, ta, python-okx, loguru, rich, etc.)
- `requirements.txt` created (python-okx uses 0.4.x versioning, not 5.x)
- `config/default.yaml` created with full bot configuration
- All `__init__.py` files created in `src/` subdirectories

**Not yet set up:**
- OKX MCP (not needed until Phase 4)
- OKX demo API keys (not needed until Phase 4)
- `.env` file (only `.env.example` exists)

#### Phase 1: Pine Script Data Layer (Steps 1.1–1.5)

All 6 individual Pine Scripts written and committed (2026-04-16):

| Step | Script | File | Output Format |
|---|---|---|---|
| 1.1 | MSS Detector | `pine/mss_detector.pine` | Labels: `MSS\|BULLISH\|price\|bar`, Table: trend/SH/SL/lastMSS |
| 1.2 | FVG Mapper | `pine/fvg_mapper.pine` | Tooltips: `FVG\|DIR\|BOT\|TOP\|SIZE%\|STATUS`, Table: counts/nearest |
| 1.3 | Order Block ID | `pine/order_block.pine` | Tooltips: `OB\|DIR\|BOT\|TOP\|STATUS\|TEST`, Table: counts/nearest |
| 1.4 | Liquidity Sweep | `pine/liquidity_sweep.pine` | Labels: `SWEEP\|DIR\|LEVEL\|TOUCHES\|BAR`, Table: pools/nearest |
| 1.5a | Session Levels | `pine/session_levels.pine` | Lines: Asian/London/NY H/L + PDH/PDL/PWH/PWL, Table: all levels |
| 1.5b | Signal Table | `pine/signal_table.pine` | Master table: trend_htf, trend_ltf, structure, last_mss, active_fvg, active_ob, liquidity_above/below, last_sweep, session, confluence (0-5), atr_14, price |

All scripts use Pine Script v6, output structured data via tooltips/tables, and are readable by the bot through TradingView MCP commands.

#### Phase 1: SMT Overlay + SMT Oscillator (2026-04-16)

The 6 individual scripts were combined with VuManChu Cipher A and B into two final production indicators that run on TradingView:

| Script | File | Type | Purpose |
|---|---|---|---|
| **SMT Master Overlay** | `pine/smt_overlay.pine` | Chart overlay | MSS/BOS + FVG boxes + OB boxes + liquidity lines/sweeps + session H/L + PDH/PDL/PWH/PWL + VMC Cipher A (EMA ribbon + WaveTrend shape signals). Outputs 20-row "SMT Signals" table with confluence 0-7. **Primary script for PA + overlay signals.** |
| **SMT Master Oscillator** | `pine/smt_oscillator.pine` | Lower pane | VMC Cipher B: WaveTrend waves + RSI + MFI + Stochastic RSI + Schaff TC + all divergences (WT/RSI/Stoch, regular + hidden) + buy/sell/gold dots. Outputs 15-row "SMT Oscillator" table with momentum 0-5. **Secondary script for momentum + divergences.** |

**SMT Signals table fields (20 rows, from smt_overlay.pine):**
- PA: trend_htf, trend_ltf, structure, last_mss, active_fvg, active_ob, liquidity_above, liquidity_below, last_sweep
- Sessions: session
- VMC Cipher A: vmc_ribbon, vmc_wt_bias (state + WT2 value), vmc_wt_cross, vmc_last_signal, vmc_rsi_mfi
- Summary: confluence (0-7), atr_14, price, last_bar

**SMT Oscillator table fields (15 rows, from smt_oscillator.pine):**
- WaveTrend: wt1, wt2, wt_state, wt_cross, wt_vwap_fast
- RSI/MFI: rsi (value + state), rsi_mfi (value + bias)
- Stochastic: stoch_k, stoch_d, stoch_state
- Signals: last_signal (type + bars ago), last_wt_div (type + bars ago)
- Summary: momentum (0-5), last_bar

Reference files: `pine/vmc_a.txt` and `pine/vmc_b.txt` contain the original VuManChu Cipher source code used during porting.

Individual scripts (`pine/mss_detector.pine`, etc.) are kept as standalone references but are NOT loaded on the chart.

#### Phase 1, Step 1.6: Data Bridge (2026-04-16)

Python-side data bridge completed:

| File | Purpose |
|---|---|
| `src/data/models.py` | Pydantic models: `MarketState`, `SignalTableData` (VMC A overlay fields), `OscillatorTableData` (VMC B momentum fields), `MSSEvent`, `FVGZone`, `OrderBlock`, `LiquidityLevel`, `SweepEvent`, `SessionLevel` |
| `src/data/tv_bridge.py` | Async TradingView CLI wrapper. Calls `node tradingview-mcp/src/cli/index.js` subprocess. Parallel fetch of tables+labels+boxes+lines+status. |
| `src/data/structured_reader.py` | Parses two Pine Script tables + drawing objects into `MarketState`. Handles SMT Signals table (20 fields), SMT Oscillator table (15 fields), MSS/BOS labels, FVG/OB boxes, session lines, sweep labels. |
| `src/data/candle_buffer.py` | Rolling OHLCV candle buffer with `Candle` dataclass, `CandleBuffer` (single TF), `MultiTFBuffer` (multi TF). |
| `scripts/test_market_state.py` | Phase 1.6 validation script. Connects to TradingView, fetches all Pine data, prints MarketState JSON + summary. Supports `--poll N` for continuous monitoring. |

**TV CLI arg format:** `data tables --filter`, `data labels --filter --max`, `data boxes --filter --verbose`, `data lines --filter --verbose` (not `--study-filter`).

**Bot reads tables via:** `tv stream tables --filter "SMT Signals"` (overlay) and `tv stream tables --filter "SMT Oscillator"` (oscillator).

**Validation:** Test script confirmed working — connects to TradingView CDP, fetches data in parallel (5 concurrent CLI calls), parses into MarketState. Both SMT Overlay and SMT Oscillator are loaded and running on the chart.

### Phase 1 Complete

Phase 1 (Infrastructure + Pine Script data layer) is fully complete:
- All Pine Scripts written, merged, and running on TradingView (smt_overlay + smt_oscillator)
- Python data bridge reads both tables + all drawing objects into a unified `MarketState`
- Two-indicator architecture provides both PA analysis (overlay) and momentum/divergence data (oscillator)

### Next Up

**Phase 2: Analysis Engine** — Python-side confluence scoring, candlestick pattern detection, multi-timeframe logic. Build on top of the MarketState that Phase 1.6 provides.
