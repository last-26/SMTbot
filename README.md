# SMTbot — AI-Powered Crypto Futures Trading Bot

Autonomous crypto-futures bot: TradingView Pine Scripts as the eyes, OKX as the
hands, a Python core as the brain. Rule-based confluence + R:R sizing today;
RL parameter tuning next.

## Architecture

```
TradingView (Eyes)        Python Bot (Brain)         OKX Exchange (Hands)
┌──────────────────┐      ┌─────────────────┐        ┌──────────────────┐
│ smt_overlay      │      │ Analysis        │        │ Demo / Live      │
│ smt_oscillator   │──MCP▶│ Strategy (R:R)  │───MCP─▶│ Market + Algo    │
│ (Pine v6)        │      │ Execution       │        │ (SL/TP OCO)      │
│                  │      │ Journal + RL    │        │                  │
└──────────────────┘      └─────────────────┘        └──────────────────┘
```

Two production Pine indicators live in `pine/` (overlay + oscillator). Earlier
single-purpose scripts (pre-consolidation) are archived in git history.

## Quick Start

```bash
git clone https://github.com/last-26/SMTbot.git
cd SMTbot
cp .env.example .env          # fill in OKX demo + Coinalyze keys

python -m venv .venv
.venv/Scripts/activate        # Windows; `source .venv/bin/activate` on *nix
pip install -r requirements.txt

# One-shot dry run (no live orders)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Full demo run
.venv/Scripts/python.exe -m src.bot --config config/default.yaml
```

For MCP setup (TradingView + OKX Agent Trade Kit), Pine Script contents, full
phase breakdowns, config reference, and operational playbook, see
[CLAUDE.md](CLAUDE.md).

## Safety

- Always start in **demo mode** (`OKX_DEMO_FLAG=1`).
- Never grant withdrawal permission to an API key.
- Circuit breakers and R:R minimums are enforced in `src/strategy/risk_manager.py`.
- This is a research project. Not financial advice.

## License

See [LICENSE](LICENSE).
