# Institutional SMC Intraday Trading Bot

An advanced algorithmic intraday trading bot engineered for **Delta Exchange** (Live & Demo) and **Binance**, built around **Smart Money Concepts (SMC)**, **Order Blocks**, **Fair Value Gaps (FVG)**, and **Multi-Timeframe Market Structure**.

---

## 🚀 Key Features

- **Multi-Timeframe SMC Matrix**:
  - Macro HTF Trend Analysis: `1D` & `4H`
  - Market Structure & Setup Bias: `15m` / `1H`
  - High-Precision Execution: `5m`
- **Institutional Accuracy Filters**:
  - ICT Premium & Discount Array (Discount for Longs, Premium for Shorts)
  - Optimal Trade Entry (OTE: 61.8% – 78.6% Fibonacci Retracement)
  - Liquidity Sweeps & FVG Mitigation Validation
  - Volume Surge Confirmation (>1.15x 20-period Volume SMA)
  - Overextension Gate (<0.35% drift from zone edge)
  - Dynamic Risk-to-Reward (Minimum 1:1.8 after fees)
- **Advanced Exchange Integration (Delta Exchange & Binance)**:
  - Dual Protection: Native exchange cloud stops (`STOP_MARKET` & `TAKE_PROFIT`) + local ticker ratchet
  - Automatic Dynamic Contract Sizing (e.g. 0.001 BTC/contract, 0.01 ETH/contract)
  - Live Position & Order Status Synchronization (resolves dual-order race conditions)
  - Smart Partial Profit Taking (50% target lock-in) with Breakeven Shield protection
- **Live Terminal Dashboard**:
  - Real-time Portfolio table
  - Market Structure & Dealing Range Matrix
  - Responsive Candle Close Countdown timer

---

## 📦 Installation & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/rajsanja/Intraday-trading-bot.git
cd Intraday-trading-bot
```

### 2. Install Dependencies
```bash
pip install ccxt requests python-dateutil
```

### 3. Configure `.env`
Copy the template configuration:
```bash
cp .env.example .env
```
Open `.env` and fill in your API credentials:
```env
EXCHANGE=delta
API_KEY=your_delta_api_key
API_SECRET=your_delta_api_secret
USE_TESTNET=true     # Set true for demo.delta.exchange, false for live
LIVE_TRADING=true
```

---

## ⚡ Running the Bot

### Self-Test
Verify mathematical indicators and SMC detectors:
```bash
python intraday_bot.py --selftest
```

### Single Market Scan
Perform a one-time multi-pair structure scan and print the analysis:
```bash
python intraday_bot.py --once
```

### Run Live Trading Loop
Start continuous live trading and monitoring:
```bash
python intraday_bot.py
```

### Reset Daily Loss Circuit Breaker
If the daily loss circuit breaker was triggered and you want to unpause trading:
```bash
python intraday_bot.py --reset-daily-loss
```

---

## 🛡️ Risk Management

- **Max Daily Loss Guard**: Automatically halts trading for the day if maximum daily drawdown is reached.
- **Breakeven Shield**: Moves stop loss to entry as soon as halfway target is hit.
- **Anti-Churn Cooldown**: Prevents overtrading and revenge entries with cooldown bars post-exit.

---

## 📄 License
MIT License. Use at your own risk. Past performance does not guarantee future results.
