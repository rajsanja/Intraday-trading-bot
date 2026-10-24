#!/usr/bin/env python3
"""
TradingView Webhook Bridge for Delta Exchange & Binance
Receives real-time trading alerts from TradingView Pine Script / indicators
and executes Market Orders, Native Cloud Stop-Loss, and Take-Profit orders.

Run locally:
    py tradingview_webhook.py

Expose to TradingView (via ngrok):
    ngrok http 5000
Then paste your ngrok URL into TradingView Alert:
    https://xxxx.ngrok-free.app/webhook
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

try:
    from fastapi import FastAPI, HTTPException, Request, Header
    import uvicorn
except ImportError:
    print("FastAPI and Uvicorn are required. Run: pip install fastapi uvicorn")
    sys.exit(1)

from intraday_bot import (
    load_config,
    load_state,
    save_state,
    record_trade,
    DeltaBroker,
    Config,
    log,
    fmt_px,
    ENV_PATH
)

app = FastAPI(
    title="TradingView Delta Bot Webhook Bridge",
    description="Automated execution engine bridging TradingView alerts to Delta Exchange",
    version="1.0.0"
)

# Global broker instance
cfg: Config = load_config()
state: Dict[str, Any] = load_state(cfg)
broker: DeltaBroker = DeltaBroker(cfg, state)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "delta_tradingview_secret")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "5000"))


class TradingViewAlert(BaseModel):
    secret: Optional[str] = Field(default=None, description="Secret token for authentication")
    action: str = Field(description="BUY, SELL, LONG, SHORT, CLOSE, or EXIT")
    symbol: Optional[str] = Field(default="BTC/USDT", description="Traded asset e.g. BTC/USDT or ETH/USDT")
    contracts: Optional[int] = Field(default=None, description="Number of contracts. If None, auto-calculated from risk %")
    entry: Optional[float] = Field(default=None, description="Expected entry price")
    sl: Optional[float] = Field(default=None, description="Stop Loss price level")
    tp: Optional[float] = Field(default=None, description="Take Profit price level")
    risk_pct: Optional[float] = Field(default=None, description="Custom risk % for this trade (e.g. 0.01 for 1%)")
    notes: Optional[str] = Field(default=None, description="Optional indicator message or notes")


@app.on_event("startup")
async def startup_event():
    print("\n" + "=" * 70)
    print("🚀 TRADINGVIEW WEBHOOK BRIDGE INITIALIZING...")
    print("=" * 70)
    broker.connect()
    equity = broker.get_equity()
    mode = "LIVE TRADING" if cfg.live_orders else "PAPER TRADING (SIMULATION)"
    print(f"| Exchange               | {cfg.exchange.upper():<36} |")
    print(f"| Execution Mode         | {mode:<36} |")
    print(f"| Account Equity         | ${equity:<35,.2f} |")
    print(f"| Webhook Port           | {WEBHOOK_PORT:<36} |")
    print(f"| Webhook Path           | /webhook{'':<28} |")
    print(f"| Secret Auth Key        | {WEBHOOK_SECRET:<36} |")
    print("=" * 70)
    print("👉 Listening for incoming TradingView alerts...\n")


@app.get("/")
def health_check():
    equity = broker.get_equity()
    return {
        "status": "online",
        "service": "TradingView Delta Webhook Bridge",
        "exchange": cfg.exchange,
        "mode": "live" if cfg.live_orders else "paper",
        "equity": equity,
        "endpoint": "/webhook",
        "instructions": "Send POST JSON payload to /webhook with action, symbol, and optional sl/tp"
    }


@app.get("/status")
def bot_status():
    equity = broker.get_equity()
    pos = state.get("position")
    return {
        "exchange": cfg.exchange,
        "equity": equity,
        "active_position": pos,
        "symbols": cfg.symbols,
        "leverage": cfg.leverage,
        "risk_per_trade": cfg.risk_per_trade,
    }


@app.post("/webhook")
async def receive_tradingview_alert(alert: TradingViewAlert, request: Request):
    # 1. Security Check
    expected_secret = os.environ.get("WEBHOOK_SECRET", WEBHOOK_SECRET)
    if expected_secret and alert.secret != expected_secret:
        log(f"⚠️ [WEBHOOK UNAUTHORIZED] Received alert with invalid secret: {alert.secret}")
        raise HTTPException(status_code=401, detail="Unauthorized: invalid secret passphrase")

    action = alert.action.upper().strip()
    symbol = alert.symbol or cfg.symbols[0]
    log("=" * 70)
    log(f"📡 [TRADINGVIEW ALERT RECEIVED] Action: {action} | Symbol: {symbol}")
    if alert.notes:
        log(f"   • Message: {alert.notes}")

    equity = broker.get_equity()
    if equity <= 0:
        equity = float(state.get("equity", 1000.0))

    # 2. Handle CLOSE / EXIT alerts
    if action in ("CLOSE", "EXIT", "FLAT"):
        pos = state.get("position")
        if not pos:
            log("ℹ️ Received CLOSE alert but no active position is recorded in bot state.")
            return {"status": "success", "message": "No active position to close"}

        pos_sym = pos.get("symbol", symbol)
        contracts = pos.get("contracts", 1)
        side = pos.get("side", "long")

        if cfg.live_orders:
            broker.close_position_order(side, contracts, symbol=pos_sym)
            broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="TradingView close alert")
            broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason="TradingView close alert")

        curr_px = broker.fetch_ticker_price(pos_sym)
        entry_px = pos.get("entry", curr_px)
        diff = (curr_px - entry_px) if side == "long" else (entry_px - curr_px)
        pnl = diff * pos.get("coin_qty", 0.0)

        record_trade({
            "symbol": pos_sym,
            "side": side.upper(),
            "entry": entry_px,
            "exit": curr_px,
            "contracts": contracts,
            "qty": pos.get("coin_qty", 0.0),
            "pnl_usd": round(pnl, 2),
            "reason": "TradingView Webhook Close Alert 📡"
        })

        state["position"] = None
        save_state(state)
        log(f"✅ Closed position on {pos_sym} at ${curr_px:,.2f} | Est PnL: ${pnl:,.2f}")
        return {"status": "success", "closed": pos_sym, "exit_price": curr_px, "pnl": round(pnl, 2)}

    # 3. Handle BUY / SELL (LONG / SHORT)
    if action in ("BUY", "LONG", "SELL", "SHORT"):
        side = "long" if action in ("BUY", "LONG") else "short"
        curr_price = broker.fetch_ticker_price(symbol)
        if curr_price <= 0 and alert.entry:
            curr_price = alert.entry
        if curr_price <= 0:
            raise HTTPException(status_code=500, detail=f"Failed to fetch current ticker price for {symbol}")

        # Position Sizing
        sl_price = alert.sl
        tp_price = alert.tp

        # Default SL & TP if not provided in alert (e.g. 1.0% SL, 2.0% TP)
        if not sl_price:
            sl_price = curr_price * 0.99 if side == "long" else curr_price * 1.01
        if not tp_price:
            tp_price = curr_price * 1.02 if side == "long" else curr_price * 0.98

        if alert.contracts and alert.contracts > 0:
            contracts = int(alert.contracts)
            contract_size = broker.get_contract_size(symbol)
            coin_qty = contracts * contract_size
        else:
            contracts, coin_qty = broker.calculate_order_sizing(curr_price, sl_price, equity, symbol=symbol)

        log(f"⚡ Executing {side.upper()} order on {symbol}:")
        log(f"   • Contracts:   {contracts} ({coin_qty:.4f} coins)")
        log(f"   • Market Price: ${curr_price:,.2f}")
        log(f"   • Stop Loss:    ${sl_price:,.2f}")
        log(f"   • Take Profit:  ${tp_price:,.2f}")

        sl_id, tp_id = None, None
        order_res = None

        if cfg.live_orders:
            # 1. Place Market Order
            order_res = broker.place_market_order(side, contracts, symbol=symbol)
            if not order_res:
                log(f"❌ Market order failed on Delta Exchange.")
                raise HTTPException(status_code=500, detail="Market order placement failed on exchange")

            # 2. Place Cloud Stops
            sl_id, tp_id = broker.place_native_stops(side, contracts, sl=sl_price, tp=tp_price, symbol=symbol)
        else:
            log(f"📝 [PAPER TRADING] Simulated {side.upper()} executed at ${curr_price:,.2f} with {contracts} contracts.")
            sl_id, tp_id = "paper_sl", "paper_tp"

        # Update Bot State
        state["position"] = {
            "symbol": symbol,
            "side": side,
            "entry": curr_price,
            "contracts": contracts,
            "coin_qty": coin_qty,
            "sl": sl_price,
            "tp": tp_price,
            "sl_order_id": sl_id,
            "tp_order_id": tp_id,
            "entered_at": datetime.now().isoformat() if "datetime" in globals() else time.strftime("%Y-%m-%dT%H:%M:%S"),
            "breakeven": False
        }
        state["trades_today"] = state.get("trades_today", 0) + 1
        save_state(state)

        log("✅ Trade recorded and protection orders active!")
        log("=" * 70 + "\n")

        return {
            "status": "success",
            "action": side.upper(),
            "symbol": symbol,
            "contracts": contracts,
            "coin_qty": coin_qty,
            "entry_price": curr_price,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "sl_order_id": sl_id,
            "tp_order_id": tp_id,
            "mode": "live" if cfg.live_orders else "paper"
        }

    raise HTTPException(status_code=400, detail=f"Unrecognized action '{action}'. Use BUY, SELL, or CLOSE.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TradingView Webhook Bridge for Delta Exchange")
    parser.add_argument("--port", type=int, default=WEBHOOK_PORT, help="Port to listen on (default 5000)")
    parser.add_argument("--test", action="store_true", help="Send a test simulated webhook alert")
    args = parser.parse_args()

    if args.test:
        print("\n🧪 Sending test simulated webhook alert...")
        import urllib.request
        url = f"http://127.0.0.1:{args.port}/webhook"
        payload = json.dumps({
            "secret": WEBHOOK_SECRET,
            "action": "BUY",
            "symbol": "BTC/USDT",
            "contracts": 1,
            "sl": 80000.0,
            "tp": 84000.0,
            "notes": "Test alert from command line"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                print("Response:", resp.read().decode())
        except Exception as e:
            print("Test failed (make sure the webhook server is running):", e)
        return

    print("=" * 70)
    print("🌐 STARTING TRADINGVIEW WEBHOOK SERVER ON 0.0.0.0:" + str(args.port))
    print(f"👉 Local Webhook URL: http://127.0.0.1:{args.port}/webhook")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
