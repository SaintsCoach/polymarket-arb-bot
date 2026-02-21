# Polymarket Arb Bot

A sports-market arbitrage and whale-mirroring bot for [Polymarket](https://polymarket.com), with a live web dashboard.

## Features

**Arb Bot**
- Scans sports markets for cross-market arbitrage opportunities (YES + NO prices summing below 100%)
- Paper trading mode with virtual balance — no real money until you flip the flag
- Configurable profit threshold, trade size, slippage tolerance, and liquidity minimums
- Simulated P&L tracking with cumulative chart

**Mirror Bot**
- Monitors any Polymarket wallet address for position changes (30s poll interval with jitter)
- Silently baselines existing positions on first poll — only *new* trades after startup trigger signals
- 40-slot virtual portfolio ($500/slot, $20k total) with queue for overflow
- Reset button clears portfolio and re-baselines at any time
- Add/pause/remove watched addresses from the dashboard UI

**Dashboard**
- Live WebSocket feed — no page refresh needed
- ARB BOT tab: market scanner, opportunity feed, trade log, cumulative P&L chart
- MIRROR BOT tab: portfolio overview, open positions table, trade queue, resolved trades, watched address health

## Setup

**Requirements:** Python 3.10+

```bash
git clone https://github.com/SaintsCoach/polymarket-arb-bot.git
cd polymarket-arb-bot
pip install -r requirements.txt
```

Edit `config.yaml`:

```yaml
wallet:
  private_key: "0xYOUR_PRIVATE_KEY_HERE"
  address:     "0xYOUR_WALLET_ADDRESS_HERE"

api:
  key:        "YOUR_POLYMARKET_API_KEY"
  secret:     "YOUR_POLYMARKET_API_SECRET"
  passphrase: "YOUR_POLYMARKET_API_PASSPHRASE"
```

For paper trading (no real credentials needed), leave `paper_mode.enabled: true`.

## Usage

```bash
# Dashboard mode (recommended)
python main_dashboard.py

# Open http://127.0.0.1:8080 in your browser
```

```bash
# Headless bot only
python main.py
```

Optional flags:
```
--config config.yaml   # path to config file (default: config.yaml)
--port   8080          # dashboard port (default: 8080)
--host   127.0.0.1     # dashboard host (default: 127.0.0.1)
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `strategy.min_profit_threshold_pct` | `2.0` | Minimum edge (%) to act on |
| `strategy.max_trade_size_usdc` | `100.0` | Max USDC per side |
| `strategy.max_risk_per_trade_usdc` | `200.0` | Max total USDC per opportunity |
| `strategy.slippage_tolerance_pct` | `0.3` | Abort if execution price drifts beyond this |
| `strategy.polling_interval_seconds` | `15` | How often to scan markets |
| `mirror_mode.starting_balance_usdc` | `20000.0` | Virtual mirror portfolio size |
| `mirror_mode.poll_interval_seconds` | `30.0` | Whale address poll frequency |

## Mirror Bot — Watched Addresses

Pre-load addresses in `config.yaml`:

```yaml
mirror_mode:
  watched_addresses:
    - address: "0xabc..."
      nickname: "Whale #1"
```

Or add/remove them live from the **MIRROR BOT → WATCHED ADDRESSES** panel in the dashboard.

## Project Structure

```
├── bot/
│   ├── arbitrage.py        # Opportunity detection logic
│   ├── client.py           # Polymarket API client (CLOB + Gamma)
│   ├── events.py           # Thread-safe async event bus
│   ├── monitor.py          # Market scanner loop
│   ├── paper_trader.py     # Simulated trade execution + P&L
│   └── mirror/
│       ├── address_monitor.py  # Wallet poller with backoff + rate-limit handling
│       ├── mirror_bot.py       # Orchestrator
│       ├── models.py           # Dataclasses (WatchedAddress, MirrorPosition, …)
│       └── portfolio.py        # 40-slot portfolio manager
├── dashboard/
│   ├── server.py           # FastAPI app + WebSocket endpoint
│   └── static/
│       ├── index.html
│       ├── app.js          # Arb bot UI + WebSocket client
│       ├── mirror.js       # Mirror bot UI
│       └── style.css
├── main.py                 # Headless entry point
├── main_dashboard.py       # Dashboard entry point
├── config.yaml
└── requirements.txt
```

## Disclaimer

This is a simulation/research tool. Paper mode is enabled by default. Use real credentials and live trading at your own risk.
