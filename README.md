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

### 4. Market Data Feed (Fixing Delta Demo Wrong Prices)
Delta Exchange Demo (`demo.delta.exchange`) has an illiquid mock order book where simulated test trades push prices to unrealistic levels (e.g. BTC at $136k). To ensure 100% accurate Smart Money Concepts indicators, the bot defaults to fetching live public market data from Binance:
```env
MARKET_DATA_PROVIDER=binance  # Uses real live prices matching TradingView
```

---

## ⚡ 1-Click Launchers (No Terminal Commands Required!)

You can run the bot directly by double-clicking these files in your folder:

| File | What it does |
| :--- | :--- |
| **`RUN_BOT.bat`** | **Double-click to start the live trading bot immediately.** |
| **`RESET_AND_RUN.bat`** | **Double-click to reset all positions & trade history and start fresh.** |
| **`START_TRADINGVIEW_WEBHOOK.bat`** | **Double-click to start the TradingView Webhook Bridge server.** |

---

## ⚡ Manual Terminal Commands (Optional)

### 1. Mathematical Self-Test
Verify all internal SMC math (FVGs, Order Blocks, Liquidity Sweeps, Breakeven Shield):
```bash
py intraday_bot.py --selftest
```

### 2. Single Live Market Scan
Perform a one-time multi-pair structure scan and print the analysis using real-world market prices:
```bash
py intraday_bot.py --once
```

### 3. Risk-Free Paper Trading (Recommended for Testing)
Test the entire bot logic, order fills, 50% partial profit, breakeven shield, and trailing stops with zero financial risk:
In `.env`:
```env
PAPER_TRADING=true
LIVE_TRADING=false
```
Then start the bot:
```bash
py intraday_bot.py
```

### 4. Reset Daily Loss Circuit Breaker
If the daily loss circuit breaker was triggered and you want to unpause trading:
```bash
py intraday_bot.py --reset-daily-loss
```

---

## 📡 TradingView Webhook Bridge

You can trigger automated execution on Delta Exchange directly from your TradingView alerts (Pine Script indicators/strategies, LuxAlgo, Supertrend, etc.):

### 1. Start the Webhook Bridge
```bash
py tradingview_webhook.py
```
By default, the server listens at `http://127.0.0.1:5000/webhook`.

### 2. Expose Port to TradingView (via ngrok or Cloudflare)
```bash
ngrok http 5000
```
Copy your forwarding URL (e.g., `https://xxxx.ngrok-free.app/webhook`).

### 3. Configure Alert in TradingView
1. In TradingView, create an Alert on any chart.
2. In the **Notifications** tab, check **Webhook URL** and enter:
   ```
   https://xxxx.ngrok-free.app/webhook
   ```
3. In the **Settings** tab, paste the JSON payload in **Message**:

#### Buy (Long) Alert:
```json
{
  "secret": "delta_tradingview_secret",
  "action": "BUY",
  "symbol": "BTC/USDT",
  "contracts": 1,
  "sl": 80500,
  "tp": 83500
}
```

#### Sell (Short) Alert:
```json
{
  "secret": "delta_tradingview_secret",
  "action": "SELL",
  "symbol": "BTC/USDT",
  "contracts": 1,
  "sl": 82500,
  "tp": 79500
}
```

#### Close / Exit Position Alert:
```json
{
  "secret": "delta_tradingview_secret",
  "action": "CLOSE",
  "symbol": "BTC/USDT"
}
```

### 4. Test Webhook Locally
You can test the webhook server locally without TradingView:
```bash
py tradingview_webhook.py --test
```

---

## 🛡️ Risk Management

- **Max Daily Loss Guard**: Automatically halts trading for the day if maximum daily drawdown is reached.
- **Breakeven Shield**: Moves stop loss to entry as soon as halfway target is hit.
- **Dynamic Profit Ratchet**: Trails stop to lock in +35% to +60% profit as price approaches TP.
- **Anti-Churn Cooldown**: Prevents overtrading and revenge entries with cooldown bars post-exit.

---

## 📄 License
MIT License. Use at your own risk. Past performance does not guarantee future results.
