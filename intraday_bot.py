#!/usr/bin/env python3
"""
Institutional SMC Intraday Trading Bot (Delta Exchange & Binance Supported)
- Strategy: Multi-Timeframe Smart Money Concepts (4H/1D Macro -> 1H Bias -> 5m Execution)
- Accuracy Boosters:
    1. Institutional Killzones (London 07:00-10:00 UTC, NY 13:00-17:00 UTC)
    2. Volume Surge Confirmation (>1.15x 20-period Volume SMA)
    3. Fair Value Gap & Order Block Mitigation Verification
    4. Overextension Gate (<0.4% from zone edge)
    5. Dynamic Risk-to-Reward (Minimum 1:1.8 after fees)
    6. Automatic Breakeven Protection at 50% TP distance
- Delta Exchange API:
    - Automatic contract sizing (contractSize e.g. 0.001 BTC/contract)
    - Native Delta exchange Stop-Loss & Take-Profit orders
    - Dual protection: Cloud orders + Fast local ticker monitoring
- Professional Swing-Bot Terminal Dashboard:
    - Real-time Live Portfolio Table
    - Institutional SMC Market Structure Matrix
    - Responsive Candle Close Countdown
"""

import sys
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

import os
import time
import json
import math
import hmac
import hashlib
import requests
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Tuple, List, Dict
from zoneinfo import ZoneInfo

try:
    import ccxt
except ImportError:
    ccxt = None

# ==========================================
# FILE PATHS & CONSTANTS
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
STATE_PATH = os.path.join(BASE_DIR, "bot_state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "trades_history.json")

MACRO_TIMEFRAMES = ("4h", "1d", "1w")
SETUP_TIMEFRAME = os.environ.get("SETUP_TIMEFRAME", "15m")
# ==========================================
# ENVIRONMENT & CONFIGURATION
# ==========================================
def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            delim = "=" if "=" in line else (":-" if ":-" in line else (":" if ":" in line else None))
            if not delim:
                continue
            key, value = line.split(delim, 1)
            norm_k = key.strip().upper().replace(" ", "_")
            norm_v = value.strip().strip('"').strip("'")
            os.environ[norm_k] = norm_v


def env_str(key: str, default: str) -> str:
    val = os.environ.get(key)
    return default if val is None or val.strip() == "" else val.strip()


def env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    try:
        return default if val is None or val.strip() == "" else float(val.strip())
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    try:
        return default if val is None or val.strip() == "" else int(float(val.strip()))
    except ValueError:
        return default


def env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Config:
    exchange: str = "delta"
    market_type: str = "future"
    symbol: str = "BTC/USDT"
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    use_testnet: bool = False
    delta_india: bool = False
    paper_trading: bool = True
    live_trading: bool = False
    ltf: str = "5m"
    setup_tf: str = "15m"
    risk_per_trade: float = 0.01          # 1% risk per trade
    leverage: int = 10                    # 10x leverage
    max_daily_loss_pct: float = 0.03      # 3% daily loss limit
    enable_session_filter: bool = False   # Set True for strict London & NY killzones only
    min_rr: float = 1.8                  # Minimum 1:1.8 Risk:Reward
    min_volume_ratio: float = 1.05       # Volume spike >= 1.05x average
    max_overextension_pct: float = 0.0025 # 0.25% maximum drift from zone for precision entries
    require_discount_premium: bool = True # Enforce ICT PD Array: buy in Discount (<50%), sell in Premium (>50%)
    enable_ote_filter: bool = True       # Prioritize 0.618 - 0.786 Fibonacci Optimal Trade Entry
    min_gap_atr_ratio: float = 0.25      # Minimum FVG gap size relative to ATR (filters micro-tick noise)
    fee_rate: float = 0.0005             # 0.05% fee estimation
    min_confluence_score: int = 75       # Minimum 75/100 points for institutional entry
    enable_partial_tp: bool = True       # Take 50% profit at halfway mark
    enable_trailing_stop: bool = True    # Dynamic profit ratchet after breakeven
    trailing_stop_trigger_pct: float = 0.65 # Activate profit trailing at 65% of TP distance
    max_trade_duration_bars: int = 36    # 36 bars = 3 hours on 5m (stale trade chop guard)
    enable_btc_correlation: bool = True  # Anchor ETH & altcoins to BTC directional trend
    enable_vwap_filter: bool = True      # Intraday institutional Session VWAP filter
    poll_seconds: int = 10               # 10-second monitoring loop
    cooldown_bars: int = 6               # 6-bar cooldown after a stopped trade
    max_hold_hours: float = 8.0          # Max hold time for intraday trades
    market_data_provider: str = "binance" # "binance", "live_delta", or "exchange"
    day_tz: str = "Asia/Kolkata"

    @property
    def live_orders(self) -> bool:
        return self.live_trading and not self.paper_trading and bool(self.api_key and self.api_secret)


def load_config() -> Config:
    load_env_file(ENV_PATH)
    global SETUP_TIMEFRAME
    SETUP_TIMEFRAME = env_str("SETUP_TIMEFRAME", "15m")

    raw_syms = env_str("SYMBOLS", "")
    if not raw_syms:
        sym_env = env_str("SYMBOL", "")
        if "," in sym_env:
            raw_syms = sym_env
        elif sym_env in {"BTC/USDT", ""}:
            raw_syms = "BTC/USDT,ETH/USDT"
        else:
            raw_syms = sym_env + ",ETH/USDT"
    parsed_symbols = [s.strip() for s in raw_syms.split(",") if s.strip()]
    if not parsed_symbols:
        parsed_symbols = ["BTC/USDT", "ETH/USDT"]

    cfg = Config(
        exchange=env_str("EXCHANGE", "delta").lower(),
        market_type=env_str("MARKET_TYPE", "future").lower(),
        symbol=parsed_symbols[0],
        symbols=parsed_symbols,
        api_key=env_str("BINANCE_API_KEY", "") or env_str("API_KEY", "") or env_str("DELTA_API_KEY", "") or env_str("DELTA_KEY", ""),
        api_secret=env_str("BINANCE_API_SECRET", "") or env_str("API_SECRET", "") or env_str("DELTA_API_SECRET", "") or env_str("DELTA_SECRET", ""),
        api_passphrase=env_str("API_PASSPHRASE", ""),
        use_testnet=env_bool("USE_TESTNET", False),
        delta_india=env_bool("DELTA_INDIA", False),
        paper_trading=env_bool("PAPER_TRADING", True),
        live_trading=env_bool("LIVE_TRADING", False),
        ltf=env_str("LTF", "5m"),
        setup_tf=SETUP_TIMEFRAME,
        risk_per_trade=env_float("RISK_PER_TRADE", 0.01),
        leverage=env_int("LEVERAGE", 10),
        max_daily_loss_pct=env_float("MAX_DAILY_LOSS_PCT", 0.03),
        enable_session_filter=env_bool("ENABLE_SESSION_FILTER", False),
        min_rr=env_float("MIN_RR", 1.8),
        min_volume_ratio=env_float("MIN_VOLUME_RATIO", 1.05),
        max_overextension_pct=env_float("MAX_OVEREXTENSION_PCT", 0.0025),
        require_discount_premium=env_bool("REQUIRE_DISCOUNT_PREMIUM", True),
        enable_ote_filter=env_bool("ENABLE_OTE_FILTER", True),
        min_gap_atr_ratio=env_float("MIN_GAP_ATR_RATIO", 0.25),
        min_confluence_score=env_int("MIN_CONFLUENCE_SCORE", 75),
        enable_partial_tp=env_bool("ENABLE_PARTIAL_TP", True),
        enable_trailing_stop=env_bool("ENABLE_TRAILING_STOP", True),
        trailing_stop_trigger_pct=env_float("TRAILING_STOP_TRIGGER_PCT", 0.65),
        max_trade_duration_bars=env_int("MAX_TRADE_DURATION_BARS", 36),
        enable_btc_correlation=env_bool("ENABLE_BTC_CORRELATION", True),
        enable_vwap_filter=env_bool("ENABLE_VWAP_FILTER", True),
        market_data_provider=env_str("MARKET_DATA_PROVIDER", "binance" if env_bool("USE_TESTNET", False) else "exchange").lower(),
        day_tz=env_str("DAY_TZ", "Asia/Kolkata"),
    )
    if cfg.risk_per_trade <= 0 or cfg.risk_per_trade > 0.1:
        cfg.risk_per_trade = 0.01
    if cfg.leverage < 1 or cfg.leverage > 50:
        cfg.leverage = 10
    return cfg


def tf_seconds(timeframe: str) -> int:
    unit = timeframe[-1]
    count = int(timeframe[:-1])
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    return count * mult


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def fmt_px(price: float) -> str:
    if abs(price) >= 100:
        return f"{price:,.2f}"
    if abs(price) >= 1:
        return f"{price:,.4f}"
    return f"{price:,.6f}"


# ==========================================
# INSTITUTIONAL KILLZONES (SESSION FILTER)
# ==========================================
def get_session_status(enable_filter: bool = True) -> Tuple[bool, str]:
    """
    Institutional high-probability killzones & crypto sessions:
    - London Killzone:  07:00 - 10:30 UTC (12:30 - 16:00 IST)
    - New York Session: 12:30 - 19:00 UTC (18:00 - 00:30 IST)
    - Asian Session:    00:00 - 05:00 UTC (05:30 - 10:30 IST)
    """
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + now_utc.minute / 60.0

    if 7.0 <= hour <= 10.5:
        desc = "🟢 LONDON KILLZONE (07:00 - 10:30 UTC)"
        is_kz = True
    elif 12.5 <= hour <= 19.0:
        desc = "🟢 NEW YORK SESSION (12:30 - 19:00 UTC)"
        is_kz = True
    elif 0.0 <= hour <= 5.0:
        desc = "🟡 ASIAN SESSION (00:00 - 05:00 UTC)"
        is_kz = True
    else:
        desc = f"⏳ OFF-PEAK HOURS ({now_utc.strftime('%H:%M')} UTC)"
        is_kz = False

    if not enable_filter:
        return True, f"{desc} [ALL-HOURS TRADING ACTIVE]"
    return is_kz, desc


# ==========================================
# DATA STRUCTURES
# ==========================================
@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Zone:
    low: float
    high: float
    kind: str
    index: int
    extreme: float
    impulse: float
    htf: bool
    source_tf: str = "5m"

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class Signal:
    side: str
    entry: float
    sl: float
    tp: float
    rr: float
    score: int
    reasons: List[str]
    atr: float
    zone_low: float
    zone_high: float
    zone_kind: str
    volume_ratio: float
    confluence_score: int = 65
    liq_sweep_desc: str = "NONE"
    rejection_desc: str = "CONFIRMED"
    rsi_val: float = 50.0
    adx_val: float = 20.0
    score_breakdown: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    early_entry: float = 0.0
    early_sl: float = 0.0
    early_rr: float = 0.0
    entry_style: str = "EARLY_ZONE_RETEST"
    pd_status: str = "N/A"
    depth_pct: float = 50.0
    is_ote: bool = False
    zone_status: str = "UNMITIGATED"


@dataclass
class Analysis:
    bias: Optional[str]
    notes: List[str]
    signal: Optional[Signal]


# ==========================================
# TECHNICAL & SMC INDICATORS
# ==========================================
def ema(values: List[float], period: int) -> List[Optional[float]]:
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1.0)
    out: List[Optional[float]] = [None] * (period - 1)
    prev = sum(values[:period]) / period
    out.append(prev)
    for value in values[period:]:
        prev = value * k + prev * (1.0 - k)
        out.append(prev)
    return out


def rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    if len(prices) < period + 1:
        return [None] * len(prices)
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out: List[Optional[float]] = [None] * period
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 100.0
    out.append(100.0 - (100.0 / (1.0 + rs)))
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else 100.0
        out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    if len(closes) < period * 2:
        return [None] * len(closes)
    trs, pos_dms, neg_dms = [], [], []
    for i in range(1, len(closes)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        prev_h, prev_l = highs[i - 1], lows[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        up_move = h - prev_h
        down_move = prev_l - l
        pos_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        neg_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        pos_dms.append(pos_dm)
        neg_dms.append(neg_dm)
    tr_smooth = sum(trs[:period])
    pos_smooth = sum(pos_dms[:period])
    neg_smooth = sum(neg_dms[:period])
    dxs = []
    for i in range(period, len(trs)):
        tr_smooth = tr_smooth - (tr_smooth / period) + trs[i]
        pos_smooth = pos_smooth - (pos_smooth / period) + pos_dms[i]
        neg_smooth = neg_smooth - (neg_smooth / period) + neg_dms[i]
        pos_di = (pos_smooth / tr_smooth * 100) if tr_smooth > 0 else 0.0
        neg_di = (neg_smooth / tr_smooth * 100) if tr_smooth > 0 else 0.0
        di_sum = pos_di + neg_di
        dx = (abs(pos_di - neg_di) / di_sum * 100) if di_sum > 0 else 0.0
        dxs.append(dx)
    if len(dxs) < period:
        return [None] * len(closes)
    adx_val = sum(dxs[:period]) / period
    out = [None] * (len(closes) - len(dxs) + period - 1)
    out.append(adx_val)
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
        out.append(adx_val)
    return out


def last_value(series: List[Optional[float]]) -> Optional[float]:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def atr(candles: List[Candle], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs: List[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        trs.append(max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        ))
    prev = sum(trs[:period]) / period
    out[period] = prev
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def volume_sma(candles: List[Candle], period: int = 20) -> float:
    if len(candles) < period:
        return sum(c.volume for c in candles) / max(len(candles), 1)
    return sum(c.volume for c in candles[-period:]) / period


def support_resistance(candles: List[Candle]) -> Tuple[List[float], List[float]]:
    highs: List[float] = []
    lows: List[float] = []
    if len(candles) < 7:
        return highs, lows
    for i in range(2, len(candles) - 2):
        c = candles[i]
        if c.high == max(candles[j].high for j in range(i - 2, i + 3)):
            highs.append(c.high)
        if c.low == min(candles[j].low for j in range(i - 2, i + 3)):
            lows.append(c.low)
    return highs, lows


def structure_bias(candles: List[Candle]) -> Optional[str]:
    highs, lows = support_resistance(candles)
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return "long"
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return "short"
    return None


def trend_bias(candles: List[Candle], min_sep: float = 0.0005) -> Tuple[Optional[str], bool, Dict[str, float]]:
    closes = [c.close for c in candles]
    e20 = last_value(ema(closes, 20))
    e50 = last_value(ema(closes, 50))
    info: Dict[str, float] = {}
    if e20 is not None:
        info["ema20"] = e20
    if e50 is not None:
        info["ema50"] = e50
    if e20 is None or e50 is None:
        return None, False, info
    price = closes[-1]
    sep = abs(e20 - e50) / price
    if price > e50 and e20 > e50 and sep >= min_sep:
        return "long", True, info
    if price < e50 and e20 < e50 and sep >= min_sep:
        return "short", True, info
    if price > e50:
        return "long", False, info
    if price < e50:
        return "short", False, info
    return None, False, info


def classify_macro_trend(candles: List[Candle]) -> Tuple[str, str]:
    if len(candles) < 25:
        return "RANGE", "insufficient history"
    struct = structure_bias(candles)
    tbias, stack, emas = trend_bias(candles, 0.0005)
    price = candles[-1].close
    e50 = emas.get("ema50")

    if struct == "long" and tbias != "short":
        return "BULLISH", "Higher Highs & Higher Lows"
    if struct == "short" and tbias != "long":
        return "BEARISH", "Lower Highs & Lower Lows"
    if stack and tbias == "long":
        return "BULLISH", "Stacked Bullish EMAs"
    if stack and tbias == "short":
        return "BEARISH", "Stacked Bearish EMAs"
    if e50 is not None:
        return ("BULLISH" if price >= e50 else "BEARISH"), f"Holding {'above' if price >= e50 else 'below'} 50-EMA"
    return "RANGE", "Choppy Consolidation"


def find_swing_pivots(candles: List[Candle], left: int = 2, right: int = 2) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Identifies clean fractal swing highs and swing lows (pivots).
    A candle is a swing high if its high >= highs of left bars before and right bars after.
    """
    swing_highs: List[Dict[str, Any]] = []
    swing_lows: List[Dict[str, Any]] = []
    if len(candles) < (left + right + 1):
        return swing_highs, swing_lows

    for i in range(left, len(candles) - right):
        c = candles[i]
        is_high = all(candles[j].high <= c.high for j in range(i - left, i + right + 1) if j != i)
        is_low = all(candles[j].low >= c.low for j in range(i - left, i + right + 1) if j != i)

        if is_high:
            swing_highs.append({"index": i, "price": c.high, "candle": c, "ts": c.ts})
        if is_low:
            swing_lows.append({"index": i, "price": c.low, "candle": c, "ts": c.ts})

    return swing_highs, swing_lows


def calculate_vwap(candles: List[Candle]) -> float:
    """
    Calculates Volume-Weighted Average Price (VWAP) across the session candles:
    VWAP = sum(typical_price * volume) / sum(volume)
    where typical_price = (high + low + close) / 3
    """
    if not candles:
        return 0.0
    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles:
        typ = (c.high + c.low + c.close) / 3.0
        cum_pv += typ * c.volume
        cum_vol += c.volume
    return (cum_pv / cum_vol) if cum_vol > 0 else candles[-1].close


def analyze_market_structure(candles: List[Candle]) -> Dict[str, Any]:
    """
    Institutional ICT Market Structure & Dealing Range Analysis:
    - Identifies Dealing Range: Swing High to Swing Low.
    - Equilibrium (EQ = 50% midpoint).
    - Premium Zone (>50%) & Discount Zone (<50%).
    - Optimal Trade Entry (OTE: 61.8% to 78.6% Fib retracement).
    - Structural Breaks: BOS (Break of Structure) & CHoCH (Change of Character).
    """
    if len(candles) < 15:
        cp = candles[-1].close if candles else 0.0
        return {
            "range_high": cp * 1.01,
            "range_low": cp * 0.99,
            "eq": cp,
            "pd_status": "EQUILIBRIUM",
            "depth_pct": 50.0,
            "is_ote": False,
            "ote_low": cp * 0.995,
            "ote_high": cp * 1.005,
            "recent_bos": None,
            "structure_trend": "RANGE"
        }

    highs, lows = find_swing_pivots(candles, left=2, right=2)
    cp = candles[-1].close

    recent_highs = [h["price"] for h in highs[-6:]] if highs else [max(c.high for c in candles[-20:])]
    recent_lows = [l["price"] for l in lows[-6:]] if lows else [min(c.low for c in candles[-20:])]

    range_high = max(recent_highs) if recent_highs else cp * 1.01
    range_low = min(recent_lows) if recent_lows else cp * 0.99
    range_span = max(range_high - range_low, 1e-9)

    eq = (range_high + range_low) / 2.0
    depth_pct = round(((cp - range_low) / range_span) * 100, 1)

    pd_status = "DISCOUNT (BUYING ZONE)" if cp < eq else "PREMIUM (SELLING ZONE)"

    # Optimal Trade Entry (OTE: 61.8% - 78.6% Retracement)
    # In Bullish trend: Retracement downwards from range_high into 61.8% - 78.6%
    # In Bearish trend: Retracement upwards from range_low into 61.8% - 78.6%
    bull_ote_top = range_high - (range_span * 0.618)
    bull_ote_bot = range_high - (range_span * 0.786)
    bear_ote_bot = range_low + (range_span * 0.618)
    bear_ote_top = range_low + (range_span * 0.786)

    is_bull_ote = (bull_ote_bot <= cp <= bull_ote_top)
    is_bear_ote = (bear_ote_bot <= cp <= bear_ote_top)
    is_ote = is_bull_ote or is_bear_ote

    recent_bos = None
    if highs and len(highs) >= 2:
        last_sh = highs[-1]["price"]
        if any(c.close > last_sh for c in candles[-4:]):
            recent_bos = "BULLISH_BOS"
    if lows and len(lows) >= 2 and not recent_bos:
        last_sl = lows[-1]["price"]
        if any(c.close < last_sl for c in candles[-4:]):
            recent_bos = "BEARISH_BOS"

    struct_trend = "BULLISH" if (highs and lows and len(highs) >= 2 and len(lows) >= 2 and highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]) else (
        "BEARISH" if (highs and lows and len(highs) >= 2 and len(lows) >= 2 and highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]) else "RANGE"
    )

    return {
        "range_high": range_high,
        "range_low": range_low,
        "eq": eq,
        "pd_status": pd_status,
        "depth_pct": depth_pct,
        "is_ote": is_ote,
        "is_bull_ote": is_bull_ote,
        "is_bear_ote": is_bear_ote,
        "bull_ote_top": bull_ote_top,
        "bull_ote_bot": bull_ote_bot,
        "bear_ote_bot": bear_ote_bot,
        "bear_ote_top": bear_ote_top,
        "recent_bos": recent_bos,
        "structure_trend": struct_trend
    }


def detect_fvgs(
    candles: List[Candle],
    lookback: int = 35,
    expected_type: Optional[str] = None,
    atr_val: Optional[float] = None,
    min_atr_mult: float = 0.20
) -> List[Dict[str, Any]]:
    """
    Detects verified institutional Fair Value Gaps (FVG) & Inversion FVGs (IFVG):
    - 3-Candle imbalance gap.
    - Requires genuine displacement candle (body >= 46% of range, volume >= 0.88x SMA).
    - Meaningful gap: gap >= max(0.12 * candle range, min_atr_mult * ATR).
    - Consequent Encroachment (CE): 50% midpoint of the imbalance.
    - Full Mitigation Tracking: UNMITIGATED, TESTING, MITIGATED, INVERSION.
    """
    if len(candles) < 3:
        return []

    current_price = candles[-1].close
    start = max(2, len(candles) - lookback)
    avg_vol = volume_sma(candles, 20)
    fvgs: List[Dict[str, Any]] = []

    for i in range(len(candles) - 1, start - 1, -1):
        c1 = candles[i - 2]
        c2 = candles[i - 1]
        c3 = candles[i]

        c2_rng = c2.high - c2.low
        if c2_rng <= 0:
            continue
        c2_body = abs(c2.close - c2.open)
        body_pct = c2_body / c2_rng

        if body_pct < 0.46:
            continue

        subsequent = candles[i:]

        # Bullish FVG
        if c3.low > c1.high and expected_type != "bearish":
            gap = c3.low - c1.high
            min_gap_req = max(c2_rng * 0.12, (atr_val or 0.0) * min_atr_mult)
            if gap < min_gap_req:
                continue

            bottom = c1.high
            top = c3.low
            ce = (top + bottom) / 2.0

            is_inversion = False
            if any(c.close < bottom for c in subsequent):
                if current_price < bottom:
                    is_inversion = True
                else:
                    continue

            prior_highs = [candles[j].high for j in range(max(0, i - 4), i - 1)]
            has_bos = bool(prior_highs and c2.close > max(prior_highs))
            vol_confirmed = bool(c2.volume >= avg_vol * 0.90)

            in_zone = (current_price <= top * 1.002) and (current_price >= bottom * 0.998)
            retests = [c for c in subsequent if c.low <= top and c.high >= bottom]

            if in_zone:
                status_label = "TESTING"
            elif not retests:
                status_label = "UNMITIGATED"
            elif any(c.low <= ce for c in retests):
                status_label = "DEEP_MITIGATED"
            else:
                status_label = "PARTIALLY_MITIGATED"

            if is_inversion:
                status_label = "INVERSION_RESISTANCE"
                quality = "A+ INVERSION FVG"
                zone_type = "bearish"
            elif has_bos and vol_confirmed:
                quality = "A+ DISPLACEMENT"
                zone_type = "bullish"
            elif has_bos:
                quality = "A DISPLACEMENT"
                zone_type = "bullish"
            else:
                quality = "CONFIRMED"
                zone_type = "bullish"

            if expected_type and zone_type != expected_type:
                continue

            fvgs.append({
                "type": zone_type,
                "top": top,
                "bottom": bottom,
                "ce": ce,
                "gap": gap,
                "in_zone": in_zone,
                "retested": bool(retests) or in_zone,
                "status": status_label,
                "quality": quality,
                "has_bos": has_bos,
                "vol_confirmed": vol_confirmed,
                "body_pct": round(body_pct * 100, 1),
                "index": i,
                "distance_pct": abs(current_price - (bottom if zone_type == 'bullish' else top)) / current_price if current_price else 0.0
            })

        # Bearish FVG
        elif c3.high < c1.low and expected_type != "bullish":
            gap = c1.low - c3.high
            min_gap_req = max(c2_rng * 0.12, (atr_val or 0.0) * min_atr_mult)
            if gap < min_gap_req:
                continue

            top = c1.low
            bottom = c3.high
            ce = (top + bottom) / 2.0

            is_inversion = False
            if any(c.close > top for c in subsequent):
                if current_price > top:
                    is_inversion = True
                else:
                    continue

            prior_lows = [candles[j].low for j in range(max(0, i - 4), i - 1)]
            has_bos = bool(prior_lows and c2.close < min(prior_lows))
            vol_confirmed = bool(c2.volume >= avg_vol * 0.90)

            in_zone = (current_price >= bottom * 0.998) and (current_price <= top * 1.002)
            retests = [c for c in subsequent if c.high >= bottom and c.low <= top]

            if in_zone:
                status_label = "TESTING"
            elif not retests:
                status_label = "UNMITIGATED"
            elif any(c.high >= ce for c in retests):
                status_label = "DEEP_MITIGATED"
            else:
                status_label = "PARTIALLY_MITIGATED"

            if is_inversion:
                status_label = "INVERSION_SUPPORT"
                quality = "A+ INVERSION FVG"
                zone_type = "bullish"
            elif has_bos and vol_confirmed:
                quality = "A+ DISPLACEMENT"
                zone_type = "bearish"
            elif has_bos:
                quality = "A DISPLACEMENT"
                zone_type = "bearish"
            else:
                quality = "CONFIRMED"
                zone_type = "bearish"

            if expected_type and zone_type != expected_type:
                continue

            fvgs.append({
                "type": zone_type,
                "top": top,
                "bottom": bottom,
                "ce": ce,
                "gap": gap,
                "in_zone": in_zone,
                "retested": bool(retests) or in_zone,
                "status": status_label,
                "quality": quality,
                "has_bos": has_bos,
                "vol_confirmed": vol_confirmed,
                "body_pct": round(body_pct * 100, 1),
                "index": i,
                "distance_pct": abs(current_price - (top if zone_type == 'bearish' else bottom)) / current_price if current_price else 0.0
            })

    def fvg_sort_key(f):
        priority = 0 if f["in_zone"] else (1 if f["status"] == "UNMITIGATED" else 2)
        return (priority, f["distance_pct"])

    fvgs.sort(key=fvg_sort_key)
    return fvgs


def detect_fvg(candles: List[Candle], lookback: int = 35, expected_type: Optional[str] = None, atr_val: Optional[float] = None) -> Optional[Dict[str, Any]]:
    fvgs = detect_fvgs(candles, lookback=lookback, expected_type=expected_type, atr_val=atr_val)
    return fvgs[0] if fvgs else None


def detect_order_blocks(
    candles: List[Candle],
    lookback: int = 35,
    expected_type: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Detects verified institutional Order Blocks (OB) & Breaker Blocks (BB):
    - Bullish OB: Last down-close candle(s) prior to aggressive expansion that broke structure (BOS).
    - Bearish OB: Last up-close candle(s) prior to aggressive expansion that broke structure (BOS).
    - Tracks complete mitigation lifecycle:
        - UNMITIGATED: Untapped fresh OB (highest win-rate).
        - TESTING: Price is currently within the OB boundaries (prime execution window).
        - MITIGATED: Price previously visited the OB.
        - BREAKER: OB was violated with displacement, flipping its polarity into opposite support/resistance.
    - Boundaries:
        - top / bottom: High & Low extreme (SL Anchor).
        - open: Open price (cleanest institutional retest boundary).
        - ce: Mean Threshold / Consequent Encroachment (50% midpoint of body or range).
    """
    if len(candles) < 3:
        return []

    current_price = candles[-1].close
    start = max(0, len(candles) - lookback)
    avg_vol = volume_sma(candles, 20)
    swing_highs, swing_lows = find_swing_pivots(candles, left=2, right=2)
    obs: List[Dict[str, Any]] = []

    for i in range(len(candles) - 2, start - 1, -1):
        c = candles[i]
        next_c = candles[i + 1]
        next_rng = next_c.high - next_c.low
        if next_rng <= 0:
            continue

        next_body = abs(next_c.close - next_c.open)
        next_body_pct = next_body / next_rng

        if next_body_pct < 0.46:
            continue

        # Bullish Order Block (Red candle before strong green breakout)
        if c.close <= c.open and next_c.close > c.high and expected_type != "bearish":
            prior_sh = [sh["price"] for sh in swing_highs if sh["index"] < i]
            prior_candles = [candles[j].high for j in range(max(0, i - 4), i)]
            broke_swing = any(next_c.close > sh_px for sh_px in prior_sh[-3:]) if prior_sh else (next_c.close > max(prior_candles) if prior_candles else True)

            subsequent = candles[i + 2:]
            
            is_breaker = False
            if subsequent and any(bar.close < c.low for bar in subsequent):
                if any(bar.close > c.high for bar in subsequent):
                    is_breaker = True
                else:
                    continue

            prior_lows = [candles[j].low for j in range(max(0, i - 4), i)]
            swept_liquidity = bool(prior_lows and c.low <= min(prior_lows))

            vol_confirmed = bool((next_c.volume >= c.volume * 0.90) or (next_c.volume >= avg_vol * 0.92))
            leaves_fvg = bool((i + 2 < len(candles)) and (candles[i + 2].low > c.high))

            ob_top = c.high
            ob_bot = c.low
            ob_open = c.open
            ob_ce = (ob_top + ob_bot) / 2.0
            mean_threshold = (max(c.open, c.close) + min(c.open, c.close)) / 2.0

            retests = [bar for bar in subsequent if bar.low <= ob_top and bar.high >= ob_bot]
            currently_in_zone = (ob_bot <= current_price <= (ob_top * 1.002))
            
            if currently_in_zone:
                mitigation_status = "TESTING"
            elif not retests:
                mitigation_status = "UNMITIGATED"
            elif any(bar.low <= ob_ce for bar in retests):
                mitigation_status = "DEEP_MITIGATED"
            else:
                mitigation_status = "PARTIALLY_MITIGATED"

            if is_breaker:
                status_label = "BREAKER_SUPPORT"
                quality = "A+ BREAKER"
            elif swept_liquidity and vol_confirmed and broke_swing:
                status_label = mitigation_status
                quality = "A+ SWEEP+BOS"
            elif vol_confirmed and broke_swing:
                status_label = mitigation_status
                quality = "A EXPANSION+BOS"
            else:
                status_label = mitigation_status
                quality = "CONFIRMED"

            obs.append({
                "type": "bullish",
                "top": ob_top,
                "bottom": ob_bot,
                "open": ob_open,
                "ce": ob_ce,
                "mean_threshold": mean_threshold,
                "in_zone": currently_in_zone,
                "retested": bool(retests) or currently_in_zone,
                "status": status_label,
                "quality": quality,
                "swept_liquidity": swept_liquidity,
                "vol_confirmed": vol_confirmed,
                "leaves_fvg": leaves_fvg,
                "has_bos": broke_swing,
                "index": i,
                "distance_pct": abs(current_price - ob_top) / current_price if current_price else 0.0
            })

        # Bearish Order Block (Green candle before strong red selloff)
        elif c.close >= c.open and next_c.close < c.low and expected_type != "bullish":
            prior_sl = [sl["price"] for sl in swing_lows if sl["index"] < i]
            prior_candles = [candles[j].low for j in range(max(0, i - 4), i)]
            broke_swing = any(next_c.close < sl_px for sl_px in prior_sl[-3:]) if prior_sl else (next_c.close < min(prior_candles) if prior_candles else True)

            subsequent = candles[i + 2:]
            
            is_breaker = False
            if subsequent and any(bar.close > c.high for bar in subsequent):
                if any(bar.close < c.low for bar in subsequent):
                    is_breaker = True
                else:
                    continue

            prior_highs = [candles[j].high for j in range(max(0, i - 4), i)]
            swept_liquidity = bool(prior_highs and c.high >= max(prior_highs))

            vol_confirmed = bool((next_c.volume >= c.volume * 0.90) or (next_c.volume >= avg_vol * 0.92))
            leaves_fvg = bool((i + 2 < len(candles)) and (candles[i + 2].high < c.low))

            ob_top = c.high
            ob_bot = c.low
            ob_open = c.open
            ob_ce = (ob_top + ob_bot) / 2.0
            mean_threshold = (max(c.open, c.close) + min(c.open, c.close)) / 2.0

            retests = [bar for bar in subsequent if bar.high >= ob_bot and bar.low <= ob_top]
            currently_in_zone = ((ob_bot * 0.998) <= current_price <= ob_top)

            if currently_in_zone:
                mitigation_status = "TESTING"
            elif not retests:
                mitigation_status = "UNMITIGATED"
            elif any(bar.high >= ob_ce for bar in retests):
                mitigation_status = "DEEP_MITIGATED"
            else:
                mitigation_status = "PARTIALLY_MITIGATED"

            if is_breaker:
                status_label = "BREAKER_RESISTANCE"
                quality = "A+ BREAKER"
            elif swept_liquidity and vol_confirmed and broke_swing:
                status_label = mitigation_status
                quality = "A+ SWEEP+BOS"
            elif vol_confirmed and broke_swing:
                status_label = mitigation_status
                quality = "A EXPANSION+BOS"
            else:
                status_label = mitigation_status
                quality = "CONFIRMED"

            obs.append({
                "type": "bearish",
                "top": ob_top,
                "bottom": ob_bot,
                "open": ob_open,
                "ce": ob_ce,
                "mean_threshold": mean_threshold,
                "in_zone": currently_in_zone,
                "retested": bool(retests) or currently_in_zone,
                "status": status_label,
                "quality": quality,
                "swept_liquidity": swept_liquidity,
                "vol_confirmed": vol_confirmed,
                "leaves_fvg": leaves_fvg,
                "has_bos": broke_swing,
                "index": i,
                "distance_pct": abs(current_price - ob_bot) / current_price if current_price else 0.0
            })

    def ob_sort_key(o):
        priority = 0 if o["in_zone"] else (1 if o["status"] == "UNMITIGATED" else 2)
        return (priority, o["distance_pct"])

    obs.sort(key=ob_sort_key)
    return obs


def detect_order_block(candles: List[Candle], lookback: int = 35, expected_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    obs = detect_order_blocks(candles, lookback=lookback, expected_type=expected_type)
    return obs[0] if obs else None


def detect_liquidity_sweep(candles: List[Candle], lookback: int = 25, sweep_window: int = 6) -> Optional[Dict[str, Any]]:
    """
    Detects Institutional Liquidity Sweeps across recent candles (last 1 to 6 bars):
    - BSL Sweep (Buy-Side Liquidity): Wick pierces prior swing high / EQH to trigger breakout stops, then closes back inside.
    - SSL Sweep (Sell-Side Liquidity): Wick pierces prior swing low / EQL to trigger sell stops, then closes back inside.
    """
    if len(candles) < 4:
        return None

    window_start = max(0, len(candles) - sweep_window)

    for idx in range(len(candles) - 1, window_start - 1, -1):
        c = candles[idx]
        rng = c.high - c.low
        if rng <= 0:
            continue

        ref_start = max(0, idx - lookback)
        prior_highs = [candles[j].high for j in range(ref_start, idx)]
        prior_lows = [candles[j].low for j in range(ref_start, idx)]
        if not prior_highs or not prior_lows:
            continue

        max_h = max(prior_highs)
        min_l = min(prior_lows)

        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low
        bars_ago = len(candles) - 1 - idx

        # BSL Sweep
        if c.high > max_h and c.close < max_h and (upper_wick / rng) >= 0.28:
            return {
                "type": "BSL_SWEEP",
                "level": max_h,
                "wick_pct": round(upper_wick / rng, 2),
                "bars_ago": bars_ago,
                "description": f"BSL Swept (${max_h:,.2f}) with {upper_wick/rng*100:.0f}% Wick ({bars_ago}b ago)"
            }

        # SSL Sweep
        if c.low < min_l and c.close > min_l and (lower_wick / rng) >= 0.28:
            return {
                "type": "SSL_SWEEP",
                "level": min_l,
                "wick_pct": round(lower_wick / rng, 2),
                "bars_ago": bars_ago,
                "description": f"SSL Swept (${min_l:,.2f}) with {lower_wick/rng*100:.0f}% Wick ({bars_ago}b ago)"
            }

    return None


def detect_market_structure_shift(candles: List[Candle], lookback: int = 20) -> Optional[str]:
    """Detects 5m Change of Character (CHoCH) or Market Structure Shift (MSS)."""
    if len(candles) < lookback:
        return None
    highs, lows = support_resistance(candles[-lookback:])
    if len(highs) < 2 or len(lows) < 2:
        return None
    curr_close = candles[-1].close
    curr_open = candles[-1].open

    if curr_close > highs[-1] and curr_close > curr_open:
        return "BULLISH_MSS"
    if curr_close < lows[-1] and curr_close < curr_open:
        return "BEARISH_MSS"
    return None


def detect_rejection_candle(candle: Candle, expected_side: str) -> Tuple[bool, float, str]:
    """Validates that the 5m trigger candle shows genuine institutional rejection / absorption."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False, 0.0, "Zero Range"

    if expected_side == "long":
        lower_wick = min(candle.open, candle.close) - candle.low
        wick_ratio = lower_wick / rng
        body_pct = abs(candle.close - candle.open) / rng
        is_bullish = candle.close >= candle.open

        if wick_ratio >= 0.30 and candle.close >= (candle.low + rng * 0.35):
            return True, wick_ratio, f"Absorption Wick ({wick_ratio*100:.0f}%)"
        if is_bullish and body_pct >= 0.55:
            return True, wick_ratio, "Strong Bullish Expansion Candle"
        return False, wick_ratio, f"No Bullish Wick ({wick_ratio*100:.0f}%)"
    else:
        upper_wick = candle.high - max(candle.open, candle.close)
        wick_ratio = upper_wick / rng
        body_pct = abs(candle.close - candle.open) / rng
        is_bearish = candle.close <= candle.open

        if wick_ratio >= 0.30 and candle.close <= (candle.high - rng * 0.35):
            return True, wick_ratio, f"Supply Defense Wick ({wick_ratio*100:.0f}%)"
        if is_bearish and body_pct >= 0.55:
            return True, wick_ratio, "Strong Bearish Expansion Candle"
        return False, wick_ratio, f"No Bearish Wick ({wick_ratio*100:.0f}%)"


def detect_rsi_divergence(candles: List[Candle], rsi_series: List[Optional[float]], lookback: int = 25) -> Optional[str]:
    """Detects Regular RSI Divergence at institutional turning points."""
    if len(candles) < lookback or len(rsi_series) < lookback:
        return None
    valid_rsi = [r for r in rsi_series[-lookback:] if r is not None]
    if len(valid_rsi) < 10:
        return None

    c_recent = candles[-1]
    c_prior_highs = [candles[i].high for i in range(len(candles) - lookback, len(candles) - 3)]
    c_prior_lows = [candles[i].low for i in range(len(candles) - lookback, len(candles) - 3)]
    r_prior = [r for r in rsi_series[-lookback:-3] if r is not None]
    curr_rsi = rsi_series[-1]

    if curr_rsi is None or not c_prior_highs or not c_prior_lows or not r_prior:
        return None

    if c_recent.high > max(c_prior_highs) and curr_rsi < max(r_prior) - 2.0:
        return "BEARISH_DIVERGENCE"
    if c_recent.low < min(c_prior_lows) and curr_rsi > min(r_prior) + 2.0:
        return "BULLISH_DIVERGENCE"
    return None


# ==========================================
# MULTI-TIMEFRAME SMC ANALYSIS ENGINE (100-PT CONFLUENCE)
# ==========================================
def analyze_smc_setup(
    macro_data: Dict[str, List[Candle]],
    setup_data: List[Candle],
    ltf_data: List[Candle],
    cfg: Config,
    btc_context: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Optional[Signal], List[str]]:
    notes: List[str] = []
    
    # 1. Institutional Session Filter
    is_killzone, session_desc = get_session_status(cfg.enable_session_filter)

    if not ltf_data or len(ltf_data) < 15 or not setup_data:
        fallback_info = {
            "session": session_desc,
            "is_killzone": is_killzone,
            "trend_1d": "N/A",
            "trend_4h": "N/A",
            "bias_1h": "N/A",
            "current_price": ltf_data[-1].close if ltf_data else 0.0,
            "vwap": ltf_data[-1].close if ltf_data else 0.0,
            "ema20_1h": 0.0,
            "ema50_1h": 0.0,
            "nearest_sup": 0.0,
            "nearest_res": 0.0,
            "major_high": 0.0,
            "major_low": 0.0,
            "vol_ratio": 1.0,
            "atr": 0.0,
            "rsi": 50.0,
            "adx": 20.0,
            "confluence_score": 0,
            "score_label": "INSUFFICIENT DATA",
            "liq_sweep_desc": "NONE",
            "rejection_desc": "NONE",
            "pd_status": "N/A",
            "depth_pct": 50.0,
            "is_ote": False,
        }
        return fallback_info, None, ["Insufficient candle history returned by exchange."]
    
    # 2. Macro Multi-Timeframe Trend (1D, 4H, 1W)
    macro_status: Dict[str, str] = {}
    for tf in MACRO_TIMEFRAMES:
        t_label, _ = classify_macro_trend(macro_data.get(tf) or [])
        macro_status[tf] = t_label
    
    trend_1d = macro_status.get("1d", "RANGE")
    trend_4h = macro_status.get("4h", "RANGE")

    # 3. Setup Timeframe Bias & Key Levels
    bias_1h, stack_1h, emas_1h = trend_bias(setup_data)
    struct_1h = structure_bias(setup_data)
    highs_1h, lows_1h = support_resistance(setup_data)
    current_price = ltf_data[-1].close
    
    resistances = sorted([h for h in highs_1h if h > current_price])
    supports = sorted([l for l in lows_1h if l < current_price], reverse=True)
    nearest_res = resistances[0] if resistances else current_price * 1.02
    nearest_sup = supports[0] if supports else current_price * 0.98
    major_high = max(highs_1h[-15:]) if len(highs_1h) >= 3 else current_price * 1.04
    major_low = min(lows_1h[-15:]) if len(lows_1h) >= 3 else current_price * 0.96

    # 4. Dealing Range & Premium/Discount Analysis (Setup TF)
    market_struct = analyze_market_structure(setup_data)
    pd_status = market_struct["pd_status"]
    depth_pct = market_struct["depth_pct"]
    is_ote = market_struct["is_ote"]

    # 5. Directional Bias Resolution
    overall_bias = bias_1h or ("long" if trend_4h == "BULLISH" else "short" if trend_4h == "BEARISH" else None)
    expected_type = "bullish" if overall_bias == "long" else ("bearish" if overall_bias == "short" else None)

    # 6. Execution Indicators: ATR, Volume, RSI, ADX
    ltf_atr = last_value(atr(ltf_data, 14)) or (current_price * 0.002)
    avg_vol = volume_sma(ltf_data, 20)
    current_vol = ltf_data[-1].volume
    vol_ratio = current_vol / max(avg_vol, 1e-9)

    ltf_closes = [c.close for c in ltf_data]
    ltf_highs = [c.high for c in ltf_data]
    ltf_lows = [c.low for c in ltf_data]

    ltf_rsi_series = rsi(ltf_closes, period=14)
    curr_rsi = last_value(ltf_rsi_series) or 50.0
    rsi_div = detect_rsi_divergence(ltf_data, ltf_rsi_series, lookback=25)

    ltf_adx_series = adx(ltf_highs, ltf_lows, ltf_closes, period=14)
    curr_adx = last_value(ltf_adx_series) or 20.0
    session_vwap = calculate_vwap(ltf_data) if getattr(cfg, "enable_vwap_filter", True) else current_price

    # Multi-bar Liquidity Sweep & MSS Detection
    liq_sweep = detect_liquidity_sweep(ltf_data, lookback=25, sweep_window=6)
    mss = detect_market_structure_shift(ltf_data, lookback=20)

    # 7. Multi-Zone Scanning for Order Blocks & FVGs
    stf_label = getattr(cfg, "setup_tf", "15m").upper()
    min_atr_m = getattr(cfg, "min_gap_atr_ratio", 0.25)

    all_obs_ltf = detect_order_blocks(ltf_data, lookback=35, expected_type=expected_type)
    all_fvgs_ltf = detect_fvgs(ltf_data, lookback=35, expected_type=expected_type, atr_val=ltf_atr, min_atr_mult=min_atr_m)
    all_obs_htf = detect_order_blocks(setup_data, lookback=35, expected_type=expected_type)
    all_fvgs_htf = detect_fvgs(setup_data, lookback=35, expected_type=expected_type, atr_val=ltf_atr, min_atr_mult=min_atr_m)

    ob_5m = all_obs_ltf[0] if all_obs_ltf else None
    fvg_5m = all_fvgs_ltf[0] if all_fvgs_ltf else None
    ob_1h = all_obs_htf[0] if all_obs_htf else None
    fvg_1h = all_fvgs_htf[0] if all_fvgs_htf else None

    # Collect valid zones prioritizing matching bias
    valid_zones: List[Dict[str, Any]] = []
    for ob in all_obs_ltf:
        if not expected_type or ob.get("type") == expected_type:
            valid_zones.append(dict(ob, source="5m OB"))
    for fvg in all_fvgs_ltf:
        if not expected_type or fvg.get("type") == expected_type:
            valid_zones.append(dict(fvg, source="5m FVG"))
    for ob in all_obs_htf:
        if not expected_type or ob.get("type") == expected_type:
            valid_zones.append(dict(ob, source=f"{stf_label} OB"))
    for fvg in all_fvgs_htf:
        if not expected_type or fvg.get("type") == expected_type:
            valid_zones.append(dict(fvg, source=f"{stf_label} FVG"))

    # Select best candidate: prioritized by active in_zone / testing, then unmitigated proximity
    zone_candidate = None
    for vz in valid_zones:
        if vz.get("in_zone") or vz.get("status") == "TESTING":
            zone_candidate = vz
            break
    if not zone_candidate:
        for vz in valid_zones:
            if vz.get("status") == "UNMITIGATED" or vz.get("retested"):
                zone_candidate = vz
                break
    if not zone_candidate and valid_zones:
        zone_candidate = valid_zones[0]

    # Check 5m Candlestick Rejection
    is_rejected, wick_ratio, rej_desc = detect_rejection_candle(ltf_data[-1], overall_bias or "long")

    # ==========================================
    # INSTITUTIONAL CONFLUENCE SCORING (Max 100 Points)
    # ==========================================
    confluence_score = 0
    score_breakdown: List[str] = []

    if overall_bias:
        # A. Macro Trend Confluence (Max 20 pts)
        if overall_bias == "long":
            if trend_1d == "BULLISH" and trend_4h == "BULLISH":
                confluence_score += 20
                score_breakdown.append("Macro Full Alignment (1D+4H BULLISH): +20")
            elif trend_4h == "BULLISH":
                confluence_score += 15
                score_breakdown.append("Macro 4H Bullish Trend: +15")
            elif trend_4h == "RANGE":
                confluence_score += 10
                score_breakdown.append("Macro 4H Range: +10")
        else:
            if trend_1d == "BEARISH" and trend_4h == "BEARISH":
                confluence_score += 20
                score_breakdown.append("Macro Full Alignment (1D+4H BEARISH): +20")
            elif trend_4h == "BEARISH":
                confluence_score += 15
                score_breakdown.append("Macro 4H Bearish Trend: +15")
            elif trend_4h == "RANGE":
                confluence_score += 10
                score_breakdown.append("Macro 4H Range: +10")

        # B. Setup Bias & EMAs (Max 20 pts)
        if (overall_bias == "long" and bias_1h == "long") or (overall_bias == "short" and bias_1h == "short"):
            if stack_1h:
                confluence_score += 20
                score_breakdown.append(f"{stf_label} Stacked Trend EMAs (20>50): +20")
            else:
                confluence_score += 15
                score_breakdown.append(f"{stf_label} Directional Bias: +15")
        elif struct_1h == overall_bias:
            confluence_score += 12
            score_breakdown.append(f"{stf_label} Swing Structure Alignment: +12")

        # C. Real Institutional Zone & Mitigation Status (Max 20 pts)
        if zone_candidate and zone_candidate.get("type") == ("bullish" if overall_bias == "long" else "bearish"):
            qual = zone_candidate.get("quality", "CONFIRMED")
            z_status = zone_candidate.get("status", "UNMITIGATED")
            z_src = zone_candidate.get("source", "Zone")

            if "TESTING" in z_status or zone_candidate.get("in_zone"):
                confluence_score += 20
                score_breakdown.append(f"Institutional {z_src} Currently Testing ({qual}): +20")
            elif "UNMITIGATED" in z_status:
                confluence_score += 18
                score_breakdown.append(f"Fresh Unmitigated {z_src} ({qual}): +18")
            elif "A+" in qual:
                confluence_score += 16
                score_breakdown.append(f"High-Quality {z_src} [{qual}]: +16")
            else:
                confluence_score += 14
                score_breakdown.append(f"Validated {z_src} [{z_status}]: +14")

        # D. Liquidity Sweep / MSS (Max 15 pts)
        if overall_bias == "long" and liq_sweep and liq_sweep["type"] == "SSL_SWEEP":
            confluence_score += 15
            score_breakdown.append(f"SSL Liquidity Swept ({liq_sweep['description']}): +15")
        elif overall_bias == "short" and liq_sweep and liq_sweep["type"] == "BSL_SWEEP":
            confluence_score += 15
            score_breakdown.append(f"BSL Liquidity Swept ({liq_sweep['description']}): +15")
        elif (overall_bias == "long" and mss == "BULLISH_MSS") or (overall_bias == "short" and mss == "BEARISH_MSS"):
            confluence_score += 12
            score_breakdown.append(f"Market Structure Shift ({mss}): +12")

        # E. Price Action Rejection Candle (Max 15 pts)
        if is_rejected:
            confluence_score += 15
            score_breakdown.append(f"5m Rejection Candle ({rej_desc}): +15")
        else:
            score_breakdown.append(f"5m Candle: {rej_desc} (+0)")

        # F. Premium/Discount & OTE Array Alignment (Max 10 pts)
        if overall_bias == "long" and "DISCOUNT" in pd_status:
            if is_ote:
                confluence_score += 10
                score_breakdown.append(f"Discount + Optimal Trade Entry (OTE 61.8-78.6% Fib): +10")
            else:
                confluence_score += 8
                score_breakdown.append(f"Discount Buying Territory ({depth_pct}%): +8")
        elif overall_bias == "short" and "PREMIUM" in pd_status:
            if is_ote:
                confluence_score += 10
                score_breakdown.append(f"Premium + Optimal Trade Entry (OTE 61.8-78.6% Fib): +10")
            else:
                confluence_score += 8
                score_breakdown.append(f"Premium Selling Territory ({depth_pct}%): +8")
        elif (overall_bias == "long" and "PREMIUM" in pd_status) or (overall_bias == "short" and "DISCOUNT" in pd_status):
            confluence_score = max(0, confluence_score - 8)
            score_breakdown.append(f"PD Array Conflict ({pd_status}): -8 Penalty")

        # G. Volume Surge Ratio (Max 10 pts)
        if vol_ratio >= 1.30:
            confluence_score += 10
            score_breakdown.append(f"Institutional Volume Surge ({vol_ratio:.2f}x): +10")
        elif vol_ratio >= cfg.min_volume_ratio:
            confluence_score += 7
            score_breakdown.append(f"Healthy Volume Surge ({vol_ratio:.2f}x): +7")

        # H. RSI Momentum & Divergence (Max 10 pts)
        if (overall_bias == "short" and rsi_div == "BEARISH_DIVERGENCE") or (overall_bias == "long" and rsi_div == "BULLISH_DIVERGENCE"):
            confluence_score += 10
            score_breakdown.append(f"RSI Divergence ({rsi_div}): +10")
        elif overall_bias == "short" and 45.0 <= curr_rsi <= 70.0:
            confluence_score += 6
            score_breakdown.append(f"RSI ({curr_rsi:.1f}) in Optimal Bearish Zone: +6")
        elif overall_bias == "long" and 30.0 <= curr_rsi <= 55.0:
            confluence_score += 6
            score_breakdown.append(f"RSI ({curr_rsi:.1f}) in Optimal Bullish Zone: +6")
        elif (overall_bias == "short" and curr_rsi < 30.0) or (overall_bias == "long" and curr_rsi > 70.0):
            confluence_score = max(0, confluence_score - 10)
            score_breakdown.append(f"RSI Overextended ({curr_rsi:.1f}): -10 Penalty")

        # I. Intraday Session VWAP Confluence (Max 8 pts)
        if getattr(cfg, "enable_vwap_filter", True) and session_vwap > 0:
            if overall_bias == "long":
                if current_price >= session_vwap:
                    confluence_score += 8
                    score_breakdown.append(f"Institutional VWAP (Price >= VWAP ${session_vwap:,.2f}): +8")
                else:
                    confluence_score = max(0, confluence_score - 6)
                    score_breakdown.append(f"Below Session VWAP (${session_vwap:,.2f}): -6 Penalty")
            else:
                if current_price <= session_vwap:
                    confluence_score += 8
                    score_breakdown.append(f"Institutional VWAP (Price <= VWAP ${session_vwap:,.2f}): +8")
                else:
                    confluence_score = max(0, confluence_score - 6)
                    score_breakdown.append(f"Above Session VWAP (${session_vwap:,.2f}): -6 Penalty")

    # Confluence Score Quality Tier
    score_label = (
        "A+ ELITE INSTITUTIONAL" if confluence_score >= 85 else (
            "A HIGH-PROBABILITY" if confluence_score >= 75 else (
                "WATCHLIST" if confluence_score >= 60 else "LOW QUALITY"
            )
        )
    )

    # Early Retest Entry calculation (Consequent Encroachment & Mean Threshold)
    early_entry_px = 0.0
    early_sl_px = 0.0
    early_tp_px = 0.0
    early_rr_val = 0.0
    if zone_candidate and overall_bias:
        z_top = zone_candidate.get("top", current_price)
        z_bot = zone_candidate.get("bottom", current_price)
        ce = zone_candidate.get("ce") or ((z_top + z_bot) / 2.0)
        
        if overall_bias == "long":
            early_entry_px = round(ce, 2)
            early_sl_px = round(z_bot - (0.15 * ltf_atr), 2)
            e_risk = max(early_entry_px - early_sl_px, 1e-6)
            higher_res = [h for h in highs_1h if h > early_entry_px]
            target_res = min(higher_res) if higher_res else early_entry_px + (e_risk * 2.5)
            early_tp_px = round(target_res * 0.998, 2)
            if early_tp_px <= early_entry_px:
                early_tp_px = round(early_entry_px + (e_risk * cfg.min_rr), 2)
            early_rr_val = round((early_tp_px - early_entry_px) / e_risk, 2)
        else:
            early_entry_px = round(ce, 2)
            early_sl_px = round(z_top + (0.15 * ltf_atr), 2)
            e_risk = max(early_sl_px - early_entry_px, 1e-6)
            lower_sup = [l for l in lows_1h if l < early_entry_px]
            target_sup = max(lower_sup) if lower_sup else early_entry_px - (e_risk * 2.5)
            early_tp_px = round(target_sup * 1.002, 2)
            if early_tp_px >= early_entry_px:
                early_tp_px = round(early_entry_px - (e_risk * cfg.min_rr), 2)
            early_rr_val = round((early_entry_px - early_tp_px) / e_risk, 2)

    analysis_info = {
        "session": session_desc,
        "is_killzone": is_killzone,
        "trend_1d": trend_1d,
        "trend_4h": trend_4h,
        "bias_1h": (bias_1h or "NEUTRAL").upper(),
        "current_price": current_price,
        "vwap": session_vwap,
        "ema20_1h": emas_1h.get("ema20", 0.0),
        "ema50_1h": emas_1h.get("ema50", 0.0),
        "nearest_sup": nearest_sup,
        "nearest_res": nearest_res,
        "major_high": major_high,
        "major_low": major_low,
        "pd_status": pd_status,
        "depth_pct": depth_pct,
        "is_ote": is_ote,
        "fvg_1h": fvg_1h,
        "ob_1h": ob_1h,
        "fvg_5m": fvg_5m,
        "ob_5m": ob_5m,
        "vol_ratio": vol_ratio,
        "atr": ltf_atr,
        "rsi": curr_rsi,
        "adx": curr_adx,
        "confluence_score": confluence_score,
        "score_label": score_label,
        "liq_sweep_desc": liq_sweep["description"] if liq_sweep else "NONE",
        "rejection_desc": rej_desc,
        "early_entry": early_entry_px,
        "early_sl": early_sl_px,
        "early_tp": early_tp_px,
        "early_rr": early_rr_val,
        "zone_source": zone_candidate.get("source", "N/A") if zone_candidate else "NONE",
        "zone_status": zone_candidate.get("status", "N/A") if zone_candidate else "NONE",
        "setup_tf": cfg.setup_tf,
    }

    # Strict Institutional Quality Gates:
    if not is_killzone:
        notes.append(f"Filtered: Outside active session ({session_desc}).")
        return analysis_info, None, notes

    if not overall_bias:
        notes.append("Filtered: Directional bias is neutral/unresolved.")
        return analysis_info, None, notes

    # BTC Macro Correlation Anchor Gate (For ETH & other altcoin pairs)
    if getattr(cfg, "enable_btc_correlation", True) and btc_context:
        btc_bias = str(btc_context.get("bias", "")).lower()
        if overall_bias == "long" and btc_bias == "short":
            notes.append("BTC Correlation Filter: Altcoin Long blocked by conflicting BTC Bearish Structure.")
            return analysis_info, None, notes
        elif overall_bias == "short" and btc_bias == "long":
            notes.append("BTC Correlation Filter: Altcoin Short blocked by conflicting BTC Bullish Structure.")
            return analysis_info, None, notes

    if overall_bias == "long" and trend_1d == "BEARISH" and trend_4h == "BEARISH":
        notes.append("Filtered: Cannot buy against both 1D and 4H macro downtrend.")
        return analysis_info, None, notes

    if overall_bias == "short" and trend_1d == "BULLISH" and trend_4h == "BULLISH":
        notes.append("Filtered: Cannot short against both 1D and 4H macro uptrend.")
        return analysis_info, None, notes

    # ADX Chop filter
    if curr_adx < 15.0:
        notes.append(f"Filtered: Low ADX ({curr_adx:.1f} < 15.0) indicates choppy consolidation.")
        return analysis_info, None, notes
        return analysis_info, None, notes

    # PD-Array Enforcement Gate (Never buy in Premium / Never sell in Discount)
    req_pd = getattr(cfg, "require_discount_premium", True)
    if req_pd:
        if overall_bias == "long" and "PREMIUM" in pd_status and not (liq_sweep and liq_sweep["type"] == "SSL_SWEEP"):
            notes.append(f"PD Filtered: Long bias rejected in Premium territory ({depth_pct}%). Await retracement into Discount (<50%).")
            return analysis_info, None, notes
        if overall_bias == "short" and "DISCOUNT" in pd_status and not (liq_sweep and liq_sweep["type"] == "BSL_SWEEP"):
            notes.append(f"PD Filtered: Short bias rejected in Discount territory ({depth_pct}%). Await retracement into Premium (>50%).")
            return analysis_info, None, notes

    # Minimum Institutional Confluence Threshold
    if confluence_score < cfg.min_confluence_score:
        notes.append(f"Filtered: Confluence Score ({confluence_score}/100) below required {cfg.min_confluence_score}/100 threshold.")
        return analysis_info, None, notes

    # Check zone candidate exists and is not conflicting
    if not zone_candidate:
        notes.append("Scanning: Waiting for clear institutional FVG or Order Block.")
        return analysis_info, None, notes

    if zone_candidate["type"] != ("bullish" if overall_bias == "long" else "bearish"):
        notes.append(f"Scanning: Zone type ({zone_candidate['type'].upper()}) conflicts with bias ({overall_bias.upper()}).")
        return analysis_info, None, notes

    # Strict Precision Overextension Gate
    z_top = zone_candidate["top"]
    z_bot = zone_candidate["bottom"]
    max_drift = getattr(cfg, "max_overextension_pct", 0.0035)

    if overall_bias == "long":
        drift = (current_price - z_top) / current_price if current_price > z_top else 0.0
        if drift > max_drift:
            notes.append(f"Overextended: Price (${current_price:,.2f}) is {drift*100:.2f}% above zone top (Max: {max_drift*100:.2f}%). Awaiting retest.")
            return analysis_info, None, notes
    else:
        drift = (z_bot - current_price) / current_price if current_price < z_bot else 0.0
        if drift > max_drift:
            notes.append(f"Overextended: Price (${current_price:,.2f}) is {drift*100:.2f}% below zone bottom (Max: {max_drift*100:.2f}%). Awaiting retest.")
            return analysis_info, None, notes

    # Precision Anchor Stop Loss & Take Profit Calculation
    entry = current_price
    if overall_bias == "long":
        # Anchor SL strictly behind the zone bottom + buffer (not arbitrary distant levels)
        sl = round(min(entry * 0.9985, z_bot - (0.15 * ltf_atr)), 2)
        risk = entry - sl
        if risk <= 0:
            sl = round(entry * 0.995, 2)
            risk = entry - sl
        tp = round(nearest_res * 0.998, 2)
        if (tp - entry) / risk < cfg.min_rr:
            tp = round(entry + (risk * cfg.min_rr), 2)
        rr = round((tp - entry) / risk, 2)
    else:
        # Anchor SL strictly behind the zone top + buffer
        sl = round(max(entry * 1.0015, z_top + (0.15 * ltf_atr)), 2)
        risk = sl - entry
        if risk <= 0:
            sl = round(entry * 1.005, 2)
            risk = sl - entry
        tp = round(nearest_sup * 1.002, 2)
        if (entry - tp) / risk < cfg.min_rr:
            tp = round(entry - (risk * cfg.min_rr), 2)
        rr = round((entry - tp) / risk, 2)

    if rr < cfg.min_rr:
        notes.append(f"Poor R:R: Calculated ratio 1:{rr:.2f} is below 1:{cfg.min_rr:.1f} minimum.")
        return analysis_info, None, notes

    reasons = [
        f"Score: {confluence_score}/100 [{score_label}]",
        f"Session: {session_desc}",
        f"Macro: 1D {trend_1d} / 4H {trend_4h}",
        f"{stf_label} Bias: {bias_1h.upper() if bias_1h else 'N/A'}",
        f"Zone: {zone_candidate.get('source', 'FVG')} [{zone_candidate.get('status', 'ACTIVE')}]",
        f"PD Array: {pd_status} ({depth_pct}%)",
        f"5m Rejection: {rej_desc}",
        f"RSI(14): {curr_rsi:.1f} | ADX: {curr_adx:.1f}",
        f"Volume Surge: {vol_ratio:.2f}x 20-SMA"
    ]
    if is_ote:
        reasons.append("Optimal Trade Entry (OTE 61.8-78.6% Fib)")
    if liq_sweep:
        reasons.append(f"Liquidity: {liq_sweep['description']}")

    entry_style = "ZONE_RETEST" if (zone_candidate and (zone_candidate.get("in_zone") or zone_candidate.get("status") == "TESTING")) else "CONFIRMATION_TRIGGER"

    signal = Signal(
        side=overall_bias,
        entry=entry,
        sl=sl,
        tp=tp,
        rr=rr,
        score=confluence_score,
        reasons=reasons,
        atr=ltf_atr,
        zone_low=z_bot,
        zone_high=z_top,
        zone_kind=zone_candidate.get("type", overall_bias),
        volume_ratio=vol_ratio,
        confluence_score=confluence_score,
        liq_sweep_desc=liq_sweep["description"] if liq_sweep else "NONE",
        rejection_desc=rej_desc,
        rsi_val=curr_rsi,
        adx_val=curr_adx,
        score_breakdown=score_breakdown,
        notes=notes,
        early_entry=early_entry_px,
        early_sl=early_sl_px,
        early_rr=early_rr_val,
        entry_style=entry_style,
        pd_status=pd_status,
        depth_pct=depth_pct,
        is_ote=is_ote,
        zone_status=zone_candidate.get("status", "UNMITIGATED")
    )
    return analysis_info, signal, notes


# ==========================================
# STATE & HISTORY RECORDING
# ==========================================
def load_state(cfg: Config) -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'equity': 0.0,
        'day': datetime.now().strftime('%Y-%m-%d'),
        'day_start_equity': 0.0,
        'daily_loss': 0.0,
        'trades_today': 0,
        'day_r': 0.0,
        'cooldown_until_ts': 0,
        'position': None,
        'breakeven_triggered': False,
        'last_candle_ts': 0,
        'exchange': cfg.exchange,
        'symbols': cfg.symbols,
    }


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log(f'Failed to save bot state: {e}')


def record_trade(trade: Dict[str, Any]) -> None:
    trades = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                trades = json.load(f)
        except Exception:
            trades = []
    trade['timestamp'] = datetime.now().isoformat()
    trades.append(trade)
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        log(f'Failed to record trade history: {e}')


# ==========================================
# DELTA & CCXT BROKER INTEGRATION
# ==========================================
class DeltaBroker:
    """
    Institutional Broker Wrapper with specialized Delta Exchange support:
    - Multi-pair resolution (e.g. 'BTC/USDT' -> 'BTC/USD:USD', 'ETH/USDT' -> 'ETH/USD:USD')
    - Dynamic contract sizing (BTC contractSize = 0.001, ETH contractSize = 0.01)
    - Native Delta exchange STOP_MARKET & TAKE_PROFIT orders per symbol
    - Dual protection: Cloud orders + Fast local ticker monitoring
    - Real-time equity pulled directly from Delta Exchange API
    """
    def __init__(self, cfg: Config, state: Optional[Dict[str, Any]] = None):
        if ccxt is None:
            raise SystemExit("ccxt package is required: pip install ccxt")
        self.cfg = cfg
        self.state = state
        self.exchange = None
        self.symbols = list(cfg.symbols)
        self.symbol = cfg.symbols[0]
        self.resolved_symbols: Dict[str, str] = {}
        self.contract_sizes: Dict[str, float] = {}
        self.market_infos: Dict[str, Any] = {}
        self.contract_size = 1.0
        self.data_exchange = None
        self._init_exchange()

    def _init_exchange(self):
        opts: Dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "swap"}
        }
        if self.cfg.api_key:
            opts["apiKey"] = self.cfg.api_key
        if self.cfg.api_secret:
            opts["secret"] = self.cfg.api_secret
        if self.cfg.api_passphrase:
            opts["password"] = self.cfg.api_passphrase

        ex_name = self.cfg.exchange.lower()
        if ex_name == "delta":
            self.exchange = ccxt.delta(opts)
            if self.cfg.delta_india:
                self.exchange.urls["api"] = {
                    "public": "https://api.india.delta.exchange",
                    "private": "https://api.india.delta.exchange"
                }
        elif "binance" in ex_name:
            self.exchange = ccxt.binanceusdm(opts) if self.cfg.market_type == "future" else ccxt.binance(opts)
        else:
            self.exchange = getattr(ccxt, ex_name)(opts)

        if self.cfg.use_testnet:
            try:
                if "binance" in ex_name:
                    if hasattr(self.exchange, "enable_demo_trading"):
                        self.exchange.enable_demo_trading(True)
                    else:
                        self.exchange.set_sandbox_mode(True)
                elif ex_name == "delta":
                    self.exchange.set_sandbox_mode(True)
                    self.exchange.urls["api"] = {
                        "public": "https://cdn-ind.testnet.deltaex.org",
                        "private": "https://cdn-ind.testnet.deltaex.org"
                    }
                else:
                    self.exchange.set_sandbox_mode(True)
            except Exception:
                pass

        # Dedicated live market data client for accurate, uncorrupted candles & tickers
        provider = getattr(self.cfg, "market_data_provider", "exchange").lower()
        if provider == "binance":
            try:
                self.data_exchange = ccxt.binanceusdm({"options": {"defaultType": "future"}, "enableRateLimit": True})
                self.data_exchange.load_markets()
            except Exception:
                try:
                    self.data_exchange = ccxt.binance({"enableRateLimit": True})
                    self.data_exchange.load_markets()
                except Exception:
                    self.data_exchange = self.exchange
        elif provider == "live_delta":
            try:
                self.data_exchange = ccxt.delta({"options": {"defaultType": "swap"}, "enableRateLimit": True})
                if self.cfg.delta_india:
                    self.data_exchange.urls["api"] = {
                        "public": "https://api.india.delta.exchange",
                        "private": "https://api.india.delta.exchange"
                    }
                else:
                    self.data_exchange.urls["api"] = {
                        "public": "https://api.delta.exchange",
                        "private": "https://api.delta.exchange"
                    }
                self.data_exchange.load_markets()
            except Exception:
                self.data_exchange = self.exchange
        else:
            self.data_exchange = self.exchange

    def get_resolved_symbol(self, sym: Optional[str] = None) -> str:
        sym = sym or self.symbol
        if sym in self.resolved_symbols:
            return self.resolved_symbols[sym]
        return self._resolve_single_symbol(sym)

    def get_contract_size(self, sym: Optional[str] = None) -> float:
        res = self.get_resolved_symbol(sym)
        return float(self.contract_sizes.get(res) or self.contract_sizes.get(sym or self.symbol) or 1.0)

    def _resolve_single_symbol(self, sym: str) -> str:
        if not self.exchange or not self.exchange.markets:
            return sym
        ex_id = getattr(self.exchange, "id", "")
        if "binance" in ex_id:
            clean_base = sym.split("/")[0].upper()
            pair = f"{clean_base}/USDT:USDT" if f"{clean_base}/USDT:USDT" in self.exchange.markets else (f"{clean_base}/USDT" if f"{clean_base}/USDT" in self.exchange.markets else sym)
            self.resolved_symbols[sym] = pair
            market = self.exchange.market(pair) if pair in self.exchange.markets else {}
            c_size = float(market.get("precision", {}).get("amount") or market.get("limits", {}).get("amount", {}).get("min") or 0.001)
            self.contract_sizes[pair] = c_size
            self.contract_sizes[sym] = c_size
            self.market_infos[pair] = market
            self.market_infos[sym] = market
            return pair

        base = sym.split("/")[0].upper()
        resolved = None
        for m_id, m in self.exchange.markets.items():
            if m.get("swap") and m.get("base") == base:
                resolved = m_id
                break
        if not resolved:
            candidates = [sym + ":USDT", sym + ":USD", sym.replace("USDT", "USD:USD"), sym]
            for c in candidates:
                if c in self.exchange.markets:
                    resolved = c
                    break
        resolved = resolved or sym
        market = self.exchange.market(resolved) if resolved in self.exchange.markets else {}
        self.resolved_symbols[sym] = resolved
        c_size = float(market.get("contractSize") or 1.0)
        self.contract_sizes[resolved] = c_size
        self.contract_sizes[sym] = c_size
        self.market_infos[resolved] = market
        self.market_infos[sym] = market
        return resolved

    def _resolve_symbols(self):
        for s in self.cfg.symbols:
            self._resolve_single_symbol(s)
        self.symbol = self.get_resolved_symbol(self.cfg.symbols[0])
        self.contract_size = self.get_contract_size(self.symbol)

    def connect(self) -> bool:
        load_env_file(ENV_PATH)
        ex_name = self.cfg.exchange.lower()

        if "binance" in ex_name:
            print("\n" + "=" * 70)
            print("🔄 CONNECTING TO BINANCE FUTURES...")
            print("=" * 70)

            key = env_str("BINANCE_API_KEY", "") or (env_str("API_KEY", "") if "binance" in env_str("EXCHANGE", "").lower() else "")
            secret = env_str("BINANCE_API_SECRET", "") or (env_str("API_SECRET", "") if "binance" in env_str("EXCHANGE", "").lower() else "")
            if key:
                self.cfg.api_key = key
            if secret:
                self.cfg.api_secret = secret

            has_keys = bool(self.cfg.api_key and self.cfg.api_secret)
            opts = {
                "enableRateLimit": True,
                "options": {"defaultType": "future"}
            }
            if has_keys:
                opts["apiKey"] = self.cfg.api_key
                opts["secret"] = self.cfg.api_secret

            self.exchange = ccxt.binanceusdm(opts) if self.cfg.market_type == "future" else ccxt.binance(opts)
            if self.cfg.use_testnet:
                if hasattr(self.exchange, "enable_demo_trading"):
                    self.exchange.enable_demo_trading(True)
                else:
                    self.exchange.set_sandbox_mode(True)

            found_balance = 0.0
            if has_keys:
                try:
                    bal = self.exchange.fetch_balance()
                    usdt = bal.get("USDT") or bal.get("USD") or {}
                    found_balance = float(usdt.get("total") or usdt.get("free") or 0.0)
                except Exception as e:
                    if self.cfg.live_orders:
                        print("======================================================================")
                        print("❌ BINANCE API AUTHENTICATION FAILED!")
                        print("======================================================================")
                        print(f"• Error: {e}")
                        print("• Fix: Verify BINANCE_API_KEY and BINANCE_API_SECRET in .env.")
                        if self.cfg.use_testnet:
                            print("  (For demo trading, keys must come from https://demo.binance.com)")
                            print("  (Or set PAPER_TRADING=true and USE_TESTNET=false for instant risk-free testing)")
                        print("======================================================================\n")
                        return False
                    else:
                        print(f"ℹ️ Binance auth notice: {e}. Continuing in Paper Trading mode.")
                        has_keys = False
                        opts.pop("apiKey", None)
                        opts.pop("secret", None)
                        self.exchange = ccxt.binanceusdm(opts) if self.cfg.market_type == "future" else ccxt.binance(opts)
                        if self.cfg.use_testnet and hasattr(self.exchange, "enable_demo_trading"):
                            self.exchange.enable_demo_trading(True)

            if not has_keys:
                found_balance = float(self.state.get("equity") or 1000.0) if self.state else 1000.0

            env_label = "BINANCE FUTURES (TESTNET)" if self.cfg.use_testnet else "BINANCE FUTURES (LIVE)"
            exec_mode = "LIVE EXECUTION" if (self.cfg.live_orders and has_keys) else "PAPER TRADING (SIMULATION)"
            print("======================================================================")
            print("✅ BINANCE CONNECTED SUCCESSFULLY!")
            print("======================================================================")
            print(f"| Exchange Platform          | {env_label:<36} |")
            print(f"| Mode                       | {exec_mode:<36} |")
            if has_keys:
                print(f"| Dynamic Exchange Balance   | ${found_balance:<35,.2f} |")
            else:
                print(f"| Dynamic Exchange Balance   | {'Simulation / Paper Mode':<36} |")

            try:
                self.exchange.load_markets()
                self._resolve_symbols()
                print(f"| Market Data Feed (SMC)     | {'BINANCE FUTURES (LIVE)':<36} |")
                for s in self.cfg.symbols:
                    res_s = self.get_resolved_symbol(s)
                    cs = self.get_contract_size(s)
                    print(f"| Traded Pair                | {f'{res_s} (Min: {cs})':<36} |")
                    if self.cfg.market_type == "future" and has_keys:
                        try:
                            self.exchange.set_leverage(self.cfg.leverage, res_s)
                        except Exception:
                            pass
                print(f"| Institutional Leverage     | {str(self.cfg.leverage)+'x':<36} |")
            except Exception:
                pass
            print("======================================================================\n")

            if self.state is not None and found_balance > 0:
                self.state["equity"] = found_balance
                self.state["day_start_equity"] = found_balance
                save_state(self.state)
            return True

        print("\n" + "=" * 70)
        print("🔄 CONNECTING TO DELTA EXCHANGE...")
        print("=" * 70)

        load_env_file(ENV_PATH)
        key = (
            env_str("API_KEY", "") or 
            env_str("DELTA_API_KEY", "") or 
            env_str("DELTA_KEY", "") or 
            env_str("DELTA_EXCHANGE_API_KEY", "")
        )
        secret = (
            env_str("API_SECRET", "") or 
            env_str("DELTA_API_SECRET", "") or 
            env_str("DELTA_SECRET", "") or 
            env_str("DELTA_EXCHANGE_API_SECRET", "")
        )
        if key:
            self.cfg.api_key = key
        if secret:
            self.cfg.api_secret = secret

        if not self.cfg.api_key or not self.cfg.api_secret:
            print("======================================================================")
            print("❌ DELTA API CREDENTIALS NOT FOUND IN .env")
            print("======================================================================")
            print("• Reason: API_KEY or API_SECRET is missing or empty in .env")
            print("• Fix: Open .env in your editor and enter:")
            print("    API_KEY=your_actual_delta_api_key")
            print("    API_SECRET=your_actual_delta_api_secret")
            print("  (Make sure you press Ctrl+S to save the .env file in your editor!)")
            print("======================================================================\n")
            return False

        endpoints = []
        if self.cfg.use_testnet:
            endpoints.append(("DELTA INDIA DEMO (demo.delta.exchange)", "https://cdn-ind.testnet.deltaex.org"))
        if self.cfg.delta_india:
            endpoints.append(("DELTA INDIA LIVE (india.delta.exchange)", "https://api.india.delta.exchange"))
        endpoints.extend([
            ("DELTA INDIA DEMO (demo.delta.exchange)", "https://cdn-ind.testnet.deltaex.org"),
            ("DELTA INDIA LIVE (india.delta.exchange)", "https://api.india.delta.exchange"),
            ("DELTA GLOBAL DEMO (testnet-api.delta.exchange)", "https://testnet-api.delta.exchange"),
            ("DELTA GLOBAL LIVE (delta.exchange)", "https://api.delta.exchange")
        ])

        seen_urls = set()
        uniq_endpoints = []
        for label, url in endpoints:
            if url not in seen_urls:
                seen_urls.add(url)
                uniq_endpoints.append((label, url))

        def probe_auth(base_url: str, k: str, s: str):
            endpoint = "/v2/wallet/balances"
            ts = str(int(time.time()))
            sig_data = "GET" + ts + endpoint
            sig = hmac.new(s.encode("utf-8"), sig_data.encode("utf-8"), hashlib.sha256).hexdigest()
            headers = {
                "api-key": k,
                "timestamp": ts,
                "signature": sig,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DeltaBot"
            }
            try:
                resp = requests.get(base_url + endpoint, headers=headers, timeout=6)
                if resp.status_code == 200:
                    data = resp.json()
                    bal = 0.0
                    for item in data.get("result", []):
                        b_str = item.get("balance") or item.get("available_balance") or "0"
                        try:
                            bal = max(bal, float(b_str))
                        except Exception:
                            pass
                    return True, bal, "OK"
                else:
                    return False, 0.0, resp.text
            except Exception as ex:
                return False, 0.0, str(ex)

        connected_label = None
        connected_url = None
        working_key = self.cfg.api_key
        working_secret = self.cfg.api_secret
        found_balance = 0.0
        failures = []

        for label, url in uniq_endpoints:
            ok, bal, err = probe_auth(url, self.cfg.api_key, self.cfg.api_secret)
            if ok:
                connected_label = label
                connected_url = url
                found_balance = bal
                break
            else:
                failures.append((label, err))

        if not connected_label:
            for label, url in uniq_endpoints:
                ok, bal, err = probe_auth(url, self.cfg.api_secret, self.cfg.api_key)
                if ok:
                    connected_label = label
                    connected_url = url
                    found_balance = bal
                    working_key = self.cfg.api_secret
                    working_secret = self.cfg.api_key
                    print("💡 Note: Detected swapped API_KEY and API_SECRET in .env! Automatically corrected.")
                    break

        if connected_label and connected_url:
            opts = {
                "apiKey": working_key,
                "secret": working_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"}
            }
            self.exchange = ccxt.delta(opts)
            self.exchange.urls["api"] = {"public": connected_url, "private": connected_url}
            
            try:
                ccxt_bal = self.exchange.fetch_balance()
                usdt = ccxt_bal.get("USDT") or ccxt_bal.get("USD") or {}
                b_val = float(usdt.get("total") or usdt.get("free") or 0.0)
                if b_val <= 0 and "total" in ccxt_bal and isinstance(ccxt_bal["total"], dict):
                    b_val = float(ccxt_bal["total"].get("USDT") or ccxt_bal["total"].get("USD") or 0.0)
                if b_val > 0:
                    found_balance = b_val
            except Exception:
                pass

            print("======================================================================")
            print("✅ DELTA API CONNECTED SUCCESSFULLY!")
            print("======================================================================")
            print(f"| Exchange Platform          | {connected_label:<36} |")
            print(f"| Mode                       | {'LIVE EXECUTION':<36} |")
            print(f"| Dynamic Exchange Balance   | ${found_balance:<35,.2f} |")
            
            try:
                self.exchange.load_markets()
                self._resolve_symbols()
                data_feed_lbl = "BINANCE FUTURES (LIVE)" if getattr(self.data_exchange, "id", "") in ("binance", "binanceusdm") else ("DELTA LIVE" if getattr(self.data_exchange, "id", "") == "delta" and not self.cfg.use_testnet else connected_label)
                print(f"| Market Data Feed (SMC)     | {data_feed_lbl:<36} |")
                for s in self.cfg.symbols:
                    res_s = self.get_resolved_symbol(s)
                    cs = self.get_contract_size(s)
                    print(f"| Traded Pair                | {f'{res_s} (Contract: {cs})':<36} |")
                    if self.cfg.market_type == "future":
                        try:
                            self.exchange.set_leverage(self.cfg.leverage, res_s)
                        except Exception:
                            pass
                print(f"| Institutional Leverage     | {str(self.cfg.leverage)+'x':<36} |")
            except Exception:
                pass
            print("======================================================================\n")

            if self.state is not None:
                self.state["equity"] = found_balance
                self.state["day_start_equity"] = found_balance
                save_state(self.state)
            return True
        else:
            print("======================================================================")
            print("❌ DELTA API CONNECTION FAILED!")
            print("======================================================================")
            for lbl, err in failures[:3]:
                short_e = err.replace("\n", " ").strip()
                if len(short_e) > 80:
                    short_e = short_e[:77] + "..."
                print(f"• {lbl:<40} : {short_e}")
            print("----------------------------------------------------------------------")
            print("🔍 REQUIRED FIXES:")
            print("1. IP Whitelist (Mandatory on Delta):")
            print("   Your computer's current public IPv4 is: 150.129.166.208")
            print("   👉 In your Delta API settings, enter '150.129.166.208' in Whitelisted IPs.")
            print("2. Platform Environment Match:")
            print("   👉 Check where you created this key:")
            print("      - demo.delta.exchange   -> Delta India Demo")
            print("      - india.delta.exchange  -> Delta India Live")
            print("3. Check .env is Saved:")
            print("   👉 Ensure Line 8 (API_KEY) and Line 9 (API_SECRET) are filled and saved (Ctrl+S).")
            print("======================================================================\n")
            return False

    def fetch_candles(self, timeframe: str, limit: int = 250, symbol: Optional[str] = None) -> List[Candle]:
        dex = getattr(self, "data_exchange", None) or self.exchange
        if dex is None:
            return []

        raw_sym = symbol or self.cfg.symbols[0]
        if hasattr(dex, "id") and dex.id in ("binance", "binanceusdm"):
            clean_base = raw_sym.split("/")[0].upper()
            target_sym = f"{clean_base}/USDT"
        elif hasattr(dex, "id") and dex.id == "delta" and dex != self.exchange:
            clean_base = raw_sym.split("/")[0].upper()
            target_sym = f"{clean_base}/USDT:USDT" if f"{clean_base}/USDT:USDT" in getattr(dex, "markets", {}) else f"{clean_base}/USDT"
        else:
            target_sym = self.get_resolved_symbol(symbol)

        try:
            rows = dex.fetch_ohlcv(target_sym, timeframe, limit=limit)
            candles = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
            tf_ms = tf_seconds(timeframe) * 1000
            now = dex.milliseconds() if hasattr(dex, "milliseconds") else int(time.time() * 1000)
            if candles and candles[-1].ts + tf_ms > now:
                candles.pop()
            return candles
        except Exception as e:
            log(f"Failed to fetch {timeframe} candles for {target_sym}: {e}")
            return []

    def fetch_ticker_price(self, symbol: Optional[str] = None) -> float:
        dex = getattr(self, "data_exchange", None) or self.exchange
        if dex is None:
            return 0.0

        raw_sym = symbol or self.cfg.symbols[0]
        if hasattr(dex, "id") and dex.id in ("binance", "binanceusdm"):
            clean_base = raw_sym.split("/")[0].upper()
            target_sym = f"{clean_base}/USDT"
        elif hasattr(dex, "id") and dex.id == "delta" and dex != self.exchange:
            clean_base = raw_sym.split("/")[0].upper()
            target_sym = f"{clean_base}/USDT:USDT" if f"{clean_base}/USDT:USDT" in getattr(dex, "markets", {}) else f"{clean_base}/USDT"
        else:
            target_sym = self.get_resolved_symbol(symbol)

        try:
            t = dex.fetch_ticker(target_sym)
            return float(t.get("last") or t.get("close") or 0.0)
        except Exception:
            return 0.0

    def get_equity(self) -> float:
        if self.cfg.api_key and self.cfg.api_secret and self.exchange is not None:
            try:
                bal = self.exchange.fetch_balance()
                usdt = bal.get("USDT") or bal.get("USD") or {}
                b_val = float(usdt.get("total") or usdt.get("free") or 0.0)
                if b_val <= 0 and "total" in bal and isinstance(bal["total"], dict):
                    b_val = float(bal["total"].get("USDT") or bal["total"].get("USD") or 0.0)
                if b_val > 0:
                    if self.state is not None:
                        self.state["equity"] = b_val
                    return b_val
            except Exception:
                pass
        return float(self.state.get("equity", 0.0)) if self.state else 0.0

    def calculate_order_sizing(self, entry: float, sl: float, equity: float, symbol: Optional[str] = None) -> Tuple[int, float]:
        contract_size = self.get_contract_size(symbol)
        risk_cash = equity * self.cfg.risk_per_trade
        risk_per_coin = abs(entry - sl)
        if risk_per_coin <= 0 or entry <= 0:
            return 1, contract_size
        
        target_coin_qty = risk_cash / risk_per_coin
        contracts = max(1, int(round(target_coin_qty / contract_size)))
        
        max_notional = equity * self.cfg.leverage * 0.70
        contract_notional = contract_size * entry
        if contract_notional > 0 and (contracts * contract_notional) > max_notional:
            contracts = max(1, int(max_notional / contract_notional))
            
        effective_coin_qty = contracts * contract_size
        return contracts, effective_coin_qty

    def place_market_order(self, side: str, contracts: int, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        target_sym = self.get_resolved_symbol(symbol)
        order_side = "buy" if side.lower() in ("long", "buy") else "sell"
        ex_id = getattr(self.exchange, "id", "")
        c_size = self.get_contract_size(symbol)

        if "binance" in ex_id:
            raw_amt = contracts * c_size
            amt = float(self.exchange.amount_to_precision(target_sym, raw_amt)) if hasattr(self.exchange, "amount_to_precision") else raw_amt
        else:
            amt = int(contracts) if contracts else 1

        try:
            order = self.exchange.create_order(
                symbol=target_sym,
                type="market",
                side=order_side,
                amount=amt
            )
            return order
        except Exception as e:
            log(f"❌ Failed to execute market {order_side.upper()} order on {target_sym}: {e}")
            return None

    def get_open_position(self, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.exchange or not self.cfg.live_orders:
            return None
        target_sym = self.get_resolved_symbol(symbol)
        ex_id = getattr(self.exchange, "id", "")
        c_size = self.get_contract_size(symbol)

        try:
            if "binance" in ex_id:
                positions = self.exchange.fetch_positions([target_sym]) if hasattr(self.exchange, "fetch_positions") else [self.exchange.fetch_position(target_sym)]
                for p in positions:
                    m_sym = target_sym.replace("/", "").replace(":USDT", "")
                    if p.get("symbol") == target_sym or p.get("info", {}).get("symbol") == m_sym:
                        amt = abs(float(p.get("contracts") or p.get("info", {}).get("positionAmt") or 0.0))
                        contracts = int(round(amt / c_size)) if c_size > 0 else int(amt)
                        p_amt = float(p.get("info", {}).get("positionAmt", 0.0) or 0.0)
                        side = "buy" if p_amt > 0 else ("sell" if p_amt < 0 else None)
                        return {
                            "contracts": contracts,
                            "side": side,
                            "entry_price": float(p.get("entryPrice") or p.get("info", {}).get("entryPrice") or 0.0),
                            "raw": p
                        }
            else:
                p = self.exchange.fetch_position(target_sym)
                contracts = abs(float(p.get("contracts") or p.get("info", {}).get("size") or 0.0))
                side = p.get("side")
                if not side and p.get("info", {}).get("size"):
                    try:
                        s_val = float(p["info"]["size"])
                        side = "buy" if s_val > 0 else ("sell" if s_val < 0 else None)
                    except Exception:
                        side = None
                return {
                    "contracts": contracts,
                    "side": side,
                    "entry_price": p.get("entryPrice") or p.get("info", {}).get("entry_price"),
                    "raw": p
                }
        except Exception:
            return None
        return None

    def check_order_status(self, order_id: Optional[str], symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not order_id or not self.exchange or not self.cfg.live_orders or str(order_id).strip() in {"paper_sl", "paper_tp", "None", "null", ""}:
            return None
        target_sym = self.get_resolved_symbol(symbol)
        try:
            o = self.exchange.fetch_order(order_id, target_sym)
            return {
                "id": str(o.get("id")),
                "status": str(o.get("status") or "").lower(),
                "type": o.get("type"),
                "stop_order_type": o.get("info", {}).get("stop_order_type"),
                "average": float(o.get("average") or o.get("info", {}).get("avgPrice") or o.get("info", {}).get("average_fill_price") or 0.0) or None,
                "amount": float(o.get("amount") or 0.0)
            }
        except Exception:
            return None

    def close_position_order(self, side: str, contracts: int, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        target_sym = self.get_resolved_symbol(symbol)
        close_side = "sell" if side.lower() in ("long", "buy") else "buy"
        ex_id = getattr(self.exchange, "id", "")
        c_size = self.get_contract_size(symbol)

        if "binance" in ex_id:
            raw_amt = contracts * c_size
            amt = float(self.exchange.amount_to_precision(target_sym, raw_amt)) if hasattr(self.exchange, "amount_to_precision") else raw_amt
            params = {"reduceOnly": True}
        else:
            amt = int(contracts) if contracts else 1
            params = {"reduceOnly": True, "reduce_only": True}

        try:
            open_pos = self.get_open_position(symbol)
            if open_pos is not None and open_pos.get("contracts", 0.0) == 0.0:
                log(f"ℹ️ [EXCHANGE SYNC] Position on {target_sym} is already closed on exchange (0 open contracts).")
                return {"status": "already_closed", "id": None}

            order = self.exchange.create_order(
                symbol=target_sym,
                type="market",
                side=close_side,
                amount=amt,
                params=params
            )
            return order
        except Exception as e:
            err_msg = str(e).lower()
            if "no_position_for_reduce_only" in err_msg or "position does not exist" in err_msg:
                log(f"ℹ️ [EXCHANGE SYNC] Position on {target_sym} already closed on exchange (0 open contracts remaining).")
                return {"status": "already_closed", "id": None}
            if "binance" in ex_id and ("reduceonly" in err_msg or "-2022" in err_msg):
                try:
                    order = self.exchange.create_order(
                        symbol=target_sym,
                        type="market",
                        side=close_side,
                        amount=amt
                    )
                    return order
                except Exception:
                    pass
            log(f"❌ Failed to close position on {target_sym}: {e}")
            return None

    def place_native_stops(self, side: str, contracts: int, sl: Optional[float] = None, tp: Optional[float] = None, symbol: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        if not self.cfg.live_orders:
            return "paper_sl", "paper_tp"

        target_sym = self.get_resolved_symbol(symbol)
        close_side = "sell" if side.lower() in ("long", "buy") else "buy"
        ex_id = getattr(self.exchange, "id", "")
        c_size = self.get_contract_size(symbol)

        if "binance" in ex_id:
            raw_amt = contracts * c_size
            amt = float(self.exchange.amount_to_precision(target_sym, raw_amt)) if hasattr(self.exchange, "amount_to_precision") else raw_amt
            is_binance = True
        else:
            amt = int(contracts) if contracts else 1
            is_binance = False

        sl_id = None
        tp_id = None

        if sl is not None:
            try:
                sl_price = self.exchange.price_to_precision(target_sym, sl) if hasattr(self.exchange, "price_to_precision") else str(sl)
                if is_binance:
                    sl_order = self.exchange.create_order(
                        symbol=target_sym,
                        type="STOP_MARKET",
                        side=close_side,
                        amount=amt,
                        params={"stopPrice": float(sl_price), "reduceOnly": True}
                    )
                else:
                    sl_order = self.exchange.create_order(
                        symbol=target_sym,
                        type="market",
                        side=close_side,
                        amount=amt,
                        params={
                            "stop_order_type": "stop_loss_order",
                            "stop_price": str(sl_price),
                            "stop_trigger_method": "mark_price",
                            "close_on_trigger": True,
                            "reduce_only": True,
                            "reduceOnly": True
                        }
                    )
                if isinstance(sl_order, dict) and sl_order.get("id") is not None:
                    sl_id = str(sl_order["id"])
                    platform = "BINANCE CLOUD STOP" if is_binance else "DELTA CLOUD STOP"
                    log(f"🛡️ [{platform}] Placed at ${float(sl):,.2f} for {target_sym} | Order ID: {sl_id}")
            except Exception as e:
                log(f"⚠️ Native Stop-Loss order not placed on {target_sym}: {e}. Local ticker ratchet will protect trade.")

        if tp is not None:
            try:
                tp_price = self.exchange.price_to_precision(target_sym, tp) if hasattr(self.exchange, "price_to_precision") else str(tp)
                if is_binance:
                    tp_order = self.exchange.create_order(
                        symbol=target_sym,
                        type="TAKE_PROFIT_MARKET",
                        side=close_side,
                        amount=amt,
                        params={"stopPrice": float(tp_price), "reduceOnly": True}
                    )
                else:
                    tp_order = self.exchange.create_order(
                        symbol=target_sym,
                        type="market",
                        side=close_side,
                        amount=amt,
                        params={
                            "stop_order_type": "take_profit_order",
                            "stop_price": str(tp_price),
                            "stop_trigger_method": "mark_price",
                            "close_on_trigger": True,
                            "reduce_only": True,
                            "reduceOnly": True
                        }
                    )
                if isinstance(tp_order, dict) and tp_order.get("id") is not None:
                    tp_id = str(tp_order["id"])
                    platform = "BINANCE CLOUD TP" if is_binance else "DELTA CLOUD TP"
                    log(f"🎯 [{platform}] Placed at ${float(tp):,.2f} for {target_sym} | Order ID: {tp_id}")
            except Exception as e:
                log(f"⚠️ Native Take-Profit order not placed on {target_sym}: {e}. Local ticker ratchet will protect trade.")

        return sl_id, tp_id

    def cancel_order(self, order_id: Optional[str], symbol: Optional[str] = None, reason: str = "") -> None:
        if not order_id or not self.cfg.live_orders or str(order_id).strip() in {"paper_sl", "paper_tp", "None", "null", ""}:
            return
        target_sym = self.get_resolved_symbol(symbol)
        try:
            self.exchange.cancel_order(order_id, target_sym)
            desc = f" ({reason})" if reason else ""
            log(f"🧹 [EXCHANGE CANCEL] Stop order {order_id} cancelled successfully{desc}.")
        except Exception:
            pass


# ==========================================
# TERMINAL DASHBOARD (SWING-BOT STYLE)
# ==========================================
def print_portfolio_dashboard(pos: Optional[Dict[str, Any]], current_price: float, equity: float, daily_loss: float, trades_count: int):
    print("\n" + "=" * 90)
    print("💼 [LIVE PORTFOLIO DASHBOARD - ALL ACTIVE POSITIONS]")
    print("=" * 90)
    print(f"| {'Symbol':<11} | {'Side':<5} | {'Contracts':<9} | {'Size':<9} | {'Entry':<10} | {'Current':<10} | {'SL':<10} | {'TP':<10} | {'PnL ($)':<9} | {'PnL (%)':<8} |")
    print(f"|{'-'*13}|{'-'*7}|{'-'*11}|{'-'*11}|{'-'*12}|{'-'*12}|{'-'*12}|{'-'*12}|{'-'*11}|{'-'*10}|")
    
    if not pos:
        print(f"| {'No active position running':<86} |")
    else:
        side = pos["side"].upper()
        entry = pos["entry"]
        contracts = pos["contracts"]
        coin_qty = pos["coin_qty"]
        sl = pos["sl"]
        tp = pos["tp"]
        
        diff = (current_price - entry) if side == "LONG" else (entry - current_price)
        pnl_usd = diff * coin_qty
        pnl_pct = (diff / entry) * 100 if entry > 0 else 0.0
        
        sign = "+" if pnl_usd >= 0 else "-"
        pnl_usd_str = f"{sign}${abs(pnl_usd):,.2f}"
        pnl_pct_str = f"{sign}{abs(pnl_pct):.2f}%"

        print(f"| {pos['symbol']:<11} | {side:<5} | {contracts:<9} | {coin_qty:<9.4g} | ${entry:<9,.2f} | ${current_price:<9,.2f} | ${sl:<9,.2f} | ${tp:<9,.2f} | {pnl_usd_str:<9} | {pnl_pct_str:<8} |")
    
    print("=" * 90)
    loss_str = f"${daily_loss:,.2f}" if daily_loss > 0 else "$0.00"
    status_str = "ACTIVE IN TRADE ⚡" if pos else "STANDBY (Monitoring & Ready for Next Candle)"
    print(f"💰 Equity: ${equity:,.2f} | Daily Loss: {loss_str} | Trades Today: {trades_count}")
    print(f"☁️ Exchange Status: {status_str}")
    print("=" * 90 + "\n")


def print_smc_structure_box(symbol: str, info: Dict[str, Any]):
    print("=" * 70)
    print(f"🏛️  [INSTITUTIONAL SMC MARKET STRUCTURE MATRIX - {symbol}]")
    print("=" * 70)
    print(f"| Traded Pair                | {symbol:<36} |")
    print(f"| Active Trading Session     | {info.get('session', 'N/A'):<36} |")
    print(f"| Macro HTF Trend (1D / 4H)  | {f'{info.get("trend_1d")}/{info.get("trend_4h")}':<36} |")
    stf = str(info.get("setup_tf", "15m")).upper()
    print(f"| Setup Bias ({stf} Structure) | {info.get('bias_1h', 'NEUTRAL'):<36} |")
    print(f"| Live Market Price          | ${info.get('current_price', 0.0):<35,.2f} |")
    print(f"| Institutional Resistance   | ${info.get('nearest_res', 0.0):<35,.2f} |")
    print(f"| Institutional Support      | ${info.get('nearest_sup', 0.0):<35,.2f} |")
    if info.get("vwap"):
        print(f"| Session VWAP Benchmark     | ${info.get('vwap', 0.0):<35,.2f} |")
    
    # Premium / Discount & OTE
    pd_str = str(info.get("pd_status", "N/A"))
    depth = info.get("depth_pct", 50.0)
    ote_flag = "🎯 ACTIVE OTE (61.8-78.6%)" if info.get("is_ote") else "NONE"
    print(f"| PD Array (Premium/Discount)| {pd_str[:36]:<36} |")
    print(f"| Dealing Range Depth / OTE  | {f'{depth:.1f}% | {ote_flag}'[:36]:<36} |")

    # Order Block & FVG Status
    fvg_1h = info.get("fvg_1h")
    ob_1h = info.get("ob_1h")
    fvg_1h_str = f"{fvg_1h.get('type', '').upper()} [{fvg_1h.get('status', 'ACTIVE')}]" if fvg_1h else "NONE (No Genuine Gap)"
    ob_1h_str = f"{ob_1h.get('type', '').upper()} [{ob_1h.get('status', 'ACTIVE')}]" if ob_1h else "NONE (No Sweep/Impulse)"
    print(f"| {stf} FVG Imbalance          | {fvg_1h_str:<36} |")
    print(f"| {stf} Order Block            | {ob_1h_str:<36} |")
    
    fvg_5m = info.get("fvg_5m")
    ob_5m = info.get("ob_5m")
    fvg_5m_str = f"FVG: {fvg_5m.get('status', 'CONFIRMED')}" if fvg_5m else "FVG: NONE"
    ob_5m_str = f"OB: {ob_5m.get('status', 'CONFIRMED')}" if ob_5m else "OB: NONE"
    print(f"| 5m Execution Zone (FVG/OB) | {f'{fvg_5m_str} | {ob_5m_str}'[:36]:<36} |")
    
    score = info.get("confluence_score", 0)
    label = info.get("score_label", "WATCHLIST")
    print(f"| SMC Confluence Score       | {f'{score}/100 [{label}]':<36} |")
    print(f"| Liquidity Sweep Status     | {str(info.get('liq_sweep_desc', 'NONE'))[:36]:<36} |")
    print(f"| 5m Price Action Rejection  | {str(info.get('rejection_desc', 'NONE'))[:36]:<36} |")
    print(f"| 5m RSI(14) Momentum        | {f'{info.get("rsi", 50.0):.1f}':<36} |")
    print(f"| 5m ADX(14) Trend Strength  | {f'{info.get("adx", 20.0):.1f}':<36} |")
    print(f"| 20-SMA Volume Surge Ratio  | {f'{info.get("vol_ratio", 1.0):.2f}x':<36} |")
    print(f"| 5m Execution Volatility    | ${info.get('atr', 0.0):<35,.2f} |")
    if info.get("early_entry", 0.0) > 0:
        print("----------------------------------------------------------------------")
        print(f"| 🎯 Optimal Early Retest Entry| ${info['early_entry']:<35,.2f} |")
        print(f"| 🛡️ Invalidation Stop Loss   | ${info.get('early_sl', 0.0):<35,.2f} |")
        print(f"| ⚖️ Optimal Early R:R Ratio  | 1:{info.get('early_rr', 0.0):<34.2f} |")
    print("=" * 70)


def get_next_candle_target(timeframe: str) -> Dict[str, Any]:
    secs = tf_seconds(timeframe)
    now = time.time()
    next_close = (int(now // secs) + 1) * secs
    remaining = max(0, next_close - now)
    local_target = datetime.fromtimestamp(next_close).strftime("%I:%M:%S %p")
    return {
        "target_ts": next_close,
        "remaining_seconds": remaining,
        "minutes": remaining / 60.0,
        "target_local": local_target
    }


# ==========================================
# MAIN INTRADAY BOT CONTROLLER
# ==========================================
class IntradayBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = load_state(cfg)
        self.broker = DeltaBroker(cfg, self.state)

    def _scan_and_execute(self, current_equity: float):
        best_signal = None
        best_sym = None
        btc_context = None

        # Ensure BTC is analyzed first to provide market anchor context
        sorted_symbols = sorted(self.cfg.symbols, key=lambda s: 0 if "BTC" in s.upper() else 1)

        for sym in sorted_symbols:
            ltf_candles = self.broker.fetch_candles(self.cfg.ltf, 150, symbol=sym)
            if not ltf_candles:
                continue

            setup_candles = self.broker.fetch_candles(SETUP_TIMEFRAME, 150, symbol=sym)
            macro_data = {
                "4h": self.broker.fetch_candles("4h", 120, symbol=sym),
                "1d": self.broker.fetch_candles("1d", 100, symbol=sym),
                "1w": self.broker.fetch_candles("1w", 50, symbol=sym),
            }

            is_btc = "BTC" in sym.upper()
            ctx_for_pair = None if is_btc else btc_context

            analysis_info, signal, notes = analyze_smc_setup(macro_data, setup_candles, ltf_candles, self.cfg, btc_context=ctx_for_pair)
            if is_btc:
                btc_context = {
                    "bias": analysis_info.get("bias_1h", "NEUTRAL").lower(),
                    "trend_4h": analysis_info.get("trend_4h", "RANGE"),
                    "price": analysis_info.get("current_price", 0.0)
                }

            print_smc_structure_box(sym, analysis_info)

            for note in notes:
                log(f"   • [{sym}] {note}")

            if signal and (best_signal is None or signal.score > best_signal.score):
                best_signal = signal
                best_sym = sym

        if best_signal and best_sym:
            self.execute_entry(best_signal, current_equity, symbol=best_sym)

    def run(self):
        log("=" * 70)
        syms_str = ", ".join(self.cfg.symbols)
        log(f"🚀 INTRADAY SMC BOT STARTED on [{syms_str}]")
        log(f"Exchange: {self.cfg.exchange.upper()} | Mode: {'LIVE TRADING' if self.cfg.live_orders else 'PAPER TRADING'}")
        log(f"Setup TF: {self.cfg.setup_tf} | Execution TF: {self.cfg.ltf} | Leverage: {self.cfg.leverage}x | Risk/Trade: {self.cfg.risk_per_trade*100:.1f}%")
        log(f"Confluence Gate: {self.cfg.min_confluence_score}/100 | Session Filter: {'ON (London/NY)' if self.cfg.enable_session_filter else 'OFF (24/7 Trading)'}")
        log("=" * 70)

        self.broker.connect()

        # Initial Fast Bootstrap: display initial dashboard and scan once on startup
        current_equity = self.broker.get_equity()
        pos = self.state.get("position")
        active_sym = pos.get("symbol") if pos else self.cfg.symbols[0]
        current_price = self.broker.fetch_ticker_price(active_sym)
        if current_price <= 0 and pos:
            current_price = pos["entry"]
        print_portfolio_dashboard(pos, current_price, current_equity, self.state.get("daily_loss", 0.0), self.state.get("trades_today", 0))

        log("⚡ [FAST BOOTSTRAP] Performing immediate initial SMC market scan across all pairs...")
        self._scan_and_execute(current_equity)

        # Mark current candle bar as scanned to avoid duplicate re-scans before next close
        secs = tf_seconds(self.cfg.ltf)
        last_scanned_bar_ts = int(time.time() // secs) * secs

        while True:
            try:
                # 1. Day Roll & Circuit Breaker Check
                today = datetime.now().strftime("%Y-%m-%d")
                if today != self.state.get("day"):
                    self.state["day"] = today
                    self.state["daily_loss"] = 0.0
                    self.state["trades_today"] = 0
                    self.state["day_start_equity"] = self.broker.get_equity()
                    save_state(self.state)

                current_equity = self.broker.get_equity()
                max_loss_usd = current_equity * self.cfg.max_daily_loss_pct
                if self.state.get("daily_loss", 0.0) >= max_loss_usd:
                    log(f"🛑 Daily Loss Limit reached (-${self.state['daily_loss']:,.2f} >= ${max_loss_usd:,.2f}). Trading paused until tomorrow.")
                    time.sleep(300)
                    continue

                # 2. Check Post-Trade Cooldown
                now_ms = int(time.time() * 1000)
                cooldown_ts = int(self.state.get("cooldown_until_ts", 0))
                if now_ms < cooldown_ts and not self.state.get("position"):
                    rem_sec = (cooldown_ts - now_ms) / 1000.0
                    log(f"⏳ [COOLDOWN ACTIVE] Post-trade cooldown active ({rem_sec:.1f}s remaining). Standing by to avoid churn/re-entry...")
                    time.sleep(min(rem_sec, 15))
                    continue

                # 3. Synchronize with Next Candle Close
                target = get_next_candle_target(self.cfg.ltf)
                remaining = target["remaining_seconds"]

                if remaining > 2.0:
                    pos = self.state.get("position")
                    if pos:
                        # ACTIVE POSITION: Guard SL/TP/trailing stop every poll interval
                        sleep_step = min(self.cfg.poll_seconds, remaining - 1.0)
                        time.sleep(max(1.0, sleep_step))
                        cur_p = self.broker.fetch_ticker_price(pos.get("symbol"))
                        self.manage_active_position(pos, cur_p)
                        continue
                    else:
                        # STANDBY MODE: Wait peacefully until candle close
                        log(f"💤 Next {self.cfg.ltf} candle close at {target['target_local']} ({target['minutes']:.2f} mins remaining)...")
                        while remaining > 2.0:
                            # Sleep in calm chunks up to 30 seconds
                            sleep_chunk = min(30.0, remaining - 1.5)
                            if sleep_chunk <= 0:
                                time.sleep(remaining + 1.5)
                                break
                            time.sleep(sleep_chunk)
                            target = get_next_candle_target(self.cfg.ltf)
                            remaining = target["remaining_seconds"]
                            if self.state.get("position"):
                                break

                # 4. Candle Close Rollover: Add 1.5s grace buffer for exchange to commit OHLCV
                time.sleep(1.5)
                now_epoch = time.time()
                closed_bar_ts = int(now_epoch // secs) * secs

                if closed_bar_ts <= last_scanned_bar_ts:
                    time.sleep(1.0)
                    continue

                last_scanned_bar_ts = closed_bar_ts

                # Print clean Portfolio Dashboard at candle close
                pos = self.state.get("position")
                active_sym = pos.get("symbol") if pos else self.cfg.symbols[0]
                current_price = self.broker.fetch_ticker_price(active_sym)
                if current_price <= 0 and pos:
                    current_price = pos["entry"]
                print_portfolio_dashboard(pos, current_price, current_equity, self.state.get("daily_loss", 0.0), self.state.get("trades_today", 0))

                if pos:
                    self.manage_active_position(pos, current_price)

                # 5. Multi-Pair SMC Scan across all configured symbols (BTC, ETH)
                if not self.state.get("position") and int(time.time() * 1000) >= int(self.state.get("cooldown_until_ts", 0)):
                    self._scan_and_execute(current_equity)

            except KeyboardInterrupt:
                log("🛑 Bot stopped safely by user. Active protection orders remain on exchange.")
                break
            except Exception as e:
                log(f"⚠️ Loop exception: {e}")
                time.sleep(10)

    def manage_active_position(self, pos: Dict[str, Any], current_price: float):
        entry = pos["entry"]
        sl = pos["sl"]
        tp = pos["tp"]
        side = pos["side"]
        contracts = pos["contracts"]
        coin_qty = pos["coin_qty"]
        pos_sym = pos["symbol"]

        # 1. Real-time Exchange Live Position Verification & Cloud TP/SL Fill Detection
        if self.cfg.live_orders:
            open_pos = self.broker.get_open_position(pos_sym)
            if open_pos is not None and open_pos.get("contracts", 0.0) == 0.0:
                # The position on Delta is already 0! It was closed directly on the exchange.
                tp_order = self.broker.check_order_status(pos.get("tp_order_id"), pos_sym)
                sl_order = self.broker.check_order_status(pos.get("sl_order_id"), pos_sym)

                tp_closed = tp_order and tp_order.get("status") in ("closed", "filled")
                sl_closed = sl_order and sl_order.get("status") in ("closed", "filled")

                if not tp_closed and not sl_closed:
                    tp_closed = (current_price >= tp) if side == "long" else (current_price <= tp)
                    sl_closed = (current_price <= sl) if side == "long" else (current_price >= sl)

                if tp_closed:
                    avg_price = float(tp_order.get("average") or tp) if tp_order else tp
                    log(f"🎯 [DELTA CLOUD TAKE PROFIT] Native Take Profit filled on Delta Exchange at ${avg_price:,.2f}!")
                    self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Position closed at final Take Profit")

                    pnl_usd = abs(avg_price - entry) * pos["coin_qty"]
                    pnl_pct = ((avg_price - entry) / entry * 100) if side == "long" else ((entry - avg_price) / entry * 100)
                    self.state["equity"] = float(self.state.get("equity", 0.0)) + pnl_usd
                    self.state["cooldown_until_ts"] = int(time.time() * 1000) + (self.cfg.cooldown_bars * tf_seconds(self.cfg.ltf) * 1000)
                    self.state["position"] = None
                    save_state(self.state)

                    record_trade({
                        "symbol": pos_sym,
                        "side": side.upper(),
                        "entry": entry,
                        "exit": avg_price,
                        "contracts": pos["contracts"],
                        "qty": pos["coin_qty"],
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": "Take Profit Final (Delta Cloud 🎯)"
                    })
                    return

                elif sl_closed:
                    avg_price = float(sl_order.get("average") or sl) if sl_order else sl
                    reason = "Breakeven Shield (Delta Cloud 🛡️)" if pos.get("breakeven") else "Stop Loss Hit (Delta Cloud 🛑)"
                    log(f"❌ [{reason}] Native Stop Order executed on Delta Exchange at ${avg_price:,.2f}!")
                    self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason=f"Position closed on {reason}")

                    loss = abs(entry - avg_price) * pos["coin_qty"] if not pos.get("breakeven") else 0.0
                    pnl_usd = -loss
                    pnl_pct = (-loss / (entry * pos["coin_qty"] / self.cfg.leverage)) * 100 if loss > 0 else 0.0

                    self.state["daily_loss"] = float(self.state.get("daily_loss", 0.0)) + loss
                    self.state["equity"] = float(self.state.get("equity", 0.0)) - loss
                    self.state["cooldown_until_ts"] = int(time.time() * 1000) + (self.cfg.cooldown_bars * tf_seconds(self.cfg.ltf) * 1000)
                    self.state["position"] = None
                    save_state(self.state)

                    record_trade({
                        "symbol": pos_sym,
                        "side": side.upper(),
                        "entry": entry,
                        "exit": avg_price,
                        "contracts": pos["contracts"],
                        "qty": pos["coin_qty"],
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": reason
                    })
                    return

                else:
                    # Position 0 on exchange, user manually closed or liquidated
                    log(f"ℹ️ [EXCHANGE SYNC] Position on {pos_sym} detected closed on Delta Exchange. Cleaning up state & orders.")
                    self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Position closed on exchange")
                    self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason="Position closed on exchange")

                    diff = (current_price - entry) if side == "long" else (entry - current_price)
                    pnl_usd = diff * pos["coin_qty"]
                    pnl_pct = (diff / entry) * 100 if entry > 0 else 0.0
                    self.state["equity"] = float(self.state.get("equity", 0.0)) + pnl_usd
                    self.state["position"] = None
                    save_state(self.state)

                    record_trade({
                        "symbol": pos_sym,
                        "side": side.upper(),
                        "entry": entry,
                        "exit": current_price,
                        "contracts": pos["contracts"],
                        "qty": pos["coin_qty"],
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": "Manual / Exchange Exit ⚡"
                    })
                    return

        # 2. Cloud SL/TP sync: verify native cloud protection if missing
        if self.cfg.live_orders:
            if not pos.get("sl_order_id") and not pos.get("_sl_sync_attempted"):
                pos["_sl_sync_attempted"] = True
                curr_sl = pos.get("sl", entry)
                new_sl, _ = self.broker.place_native_stops(side, contracts, sl=curr_sl, tp=None, symbol=pos_sym)
                if new_sl:
                    pos["sl_order_id"] = new_sl
                    save_state(self.state)
            if not pos.get("tp_order_id") and not pos.get("_tp_sync_attempted"):
                pos["_tp_sync_attempted"] = True
                curr_tp = pos.get("tp")
                if curr_tp:
                    _, new_tp = self.broker.place_native_stops(side, contracts, sl=None, tp=curr_tp, symbol=pos_sym)
                    if new_tp:
                        pos["tp_order_id"] = new_tp
                        save_state(self.state)

        halfway = entry + (tp - entry) * 0.5 if side == "long" else entry - (entry - tp) * 0.5
        hit_halfway = (current_price >= halfway) if side == "long" else (current_price <= halfway)
        
        # 3. Smart Partial Profit Taking & Breakeven Protection at Halfway Target
        if hit_halfway:
            # A. If multiple contracts and partial TP not yet taken: lock in 50% profit
            if self.cfg.enable_partial_tp and contracts >= 2 and not pos.get("partial_tp_hit"):
                partial_contracts = max(1, contracts // 2)
                partial_coin_qty = partial_contracts * self.broker.get_contract_size(pos_sym)
                
                log(f"\n💰 [PARTIAL PROFIT SECURED] 50% target reached on {pos_sym} at ${current_price:,.2f}!")
                if self.cfg.live_orders:
                    self.broker.close_position_order(side, partial_contracts, symbol=pos_sym)
                
                diff = (current_price - entry) if side == "long" else (entry - current_price)
                partial_pnl = diff * partial_coin_qty
                partial_pct = (diff / entry) * 100 if entry > 0 else 0.0
                
                self.state["equity"] = float(self.state.get("equity", 0.0)) + partial_pnl
                pos["contracts"] = contracts - partial_contracts
                pos["coin_qty"] = coin_qty - partial_coin_qty
                pos["partial_tp_hit"] = True
                pos["sl"] = entry
                pos["breakeven"] = True
                
                # Update cloud stops and TP for remaining contracts to Breakeven & TP
                self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Replacing stop with Breakeven stop after partial TP")
                self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason="Replacing TP order with remaining contracts after partial TP")
                new_sl_id, new_tp_id = self.broker.place_native_stops(side, pos["contracts"], sl=entry, tp=tp, symbol=pos_sym)
                pos["sl_order_id"] = new_sl_id
                pos["tp_order_id"] = new_tp_id
                save_state(self.state)
                
                log(f"   • Secured Realized PnL: +${partial_pnl:,.2f} (+{partial_pct:.2f}%)")
                log(f"   • Remaining Position:   {pos['contracts']} contracts running risk-free with Breakeven Shield (${entry:,.2f})\n")
                
                record_trade({
                    "symbol": pos_sym,
                    "side": side.upper(),
                    "entry": entry,
                    "exit": current_price,
                    "contracts": partial_contracts,
                    "qty": partial_coin_qty,
                    "pnl_usd": round(partial_pnl, 2),
                    "pnl_pct": round(partial_pct, 2),
                    "reason": "Partial Take Profit (50%) 💰"
                })

            # B. Single contract or partial already hit: ensure Breakeven Shield
            elif not pos.get("breakeven"):
                pos["sl"] = entry
                pos["breakeven"] = True
                log(f"🛡️ [RISK-FREE PROTECTION] Price reached 50% target on {pos_sym}. Stop Loss moved to Breakeven (${entry:,.2f})!")
                self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Moving stop to Breakeven")
                new_sl_id, _ = self.broker.place_native_stops(side, pos["contracts"], sl=entry, tp=None, symbol=pos_sym)
                pos["sl_order_id"] = new_sl_id
                save_state(self.state)

        # 3.1 Dynamic Profit Ratchet (Trailing Stop once in deep profit)
        if getattr(self.cfg, "enable_trailing_stop", True) and pos.get("breakeven"):
            dist = abs(tp - entry)
            if dist > 0:
                progress = abs(current_price - entry) / dist
                target_lock_ratio = 0.0
                if progress >= 0.85:
                    target_lock_ratio = 0.60
                elif progress >= getattr(self.cfg, "trailing_stop_trigger_pct", 0.65):
                    target_lock_ratio = 0.35

                if target_lock_ratio > 0.0:
                    cand_sl = (entry + dist * target_lock_ratio) if side == "long" else (entry - dist * target_lock_ratio)
                    cand_sl = round(cand_sl, 2)
                    is_better = (cand_sl > sl) if side == "long" else (cand_sl < sl)
                    if is_better:
                        pos["sl"] = cand_sl
                        log(f"📈 [PROFIT RATCHET] Price reached {progress*100:.1f}% of target on {pos_sym}! Advanced Trailing Stop to ${cand_sl:,.2f} (Locked +{target_lock_ratio*100:.0f}% profit).")
                        if self.cfg.live_orders:
                            self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Advancing trailing stop ratchet")
                            new_sl_id, _ = self.broker.place_native_stops(side, pos["contracts"], sl=cand_sl, tp=None, symbol=pos_sym)
                            pos["sl_order_id"] = new_sl_id
                        save_state(self.state)

        # 3.2 Stale Trade / Consolidation Guard (Prevent capital lockup in dead chop)
        max_bars = getattr(self.cfg, "max_trade_duration_bars", 36)
        entered_iso = pos.get("entered_at")
        if max_bars > 0 and entered_iso:
            try:
                entered_dt = datetime.fromisoformat(entered_iso)
                elapsed_sec = (datetime.now() - entered_dt).total_seconds()
                bars_elapsed = elapsed_sec / tf_seconds(self.cfg.ltf)
                if bars_elapsed >= max_bars and not pos.get("partial_tp_hit"):
                    drift_from_entry = abs(current_price - entry) / entry
                    if drift_from_entry < 0.003:
                        log(f"⏱️ [STALE TRADE GUARD] Trade has been stagnant for {bars_elapsed:.0f} bars ({elapsed_sec/3600:.1f}h) near entry (${current_price:,.2f}). Gracefully closing position.")
                        if self.cfg.live_orders:
                            self.broker.close_position_order(side, pos["contracts"], symbol=pos_sym)
                            self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Stale trade exit")
                            self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason="Stale trade exit")

                        diff = (current_price - entry) if side == "long" else (entry - current_price)
                        pnl_usd = diff * pos["coin_qty"]
                        pnl_pct = (diff / entry) * 100 if entry > 0 else 0.0

                        self.state["equity"] = float(self.state.get("equity", 0.0)) + pnl_usd
                        self.state["cooldown_until_ts"] = int(time.time() * 1000) + (self.cfg.cooldown_bars * tf_seconds(self.cfg.ltf) * 1000)
                        self.state["position"] = None
                        save_state(self.state)

                        record_trade({
                            "symbol": pos_sym,
                            "side": side.upper(),
                            "entry": entry,
                            "exit": current_price,
                            "contracts": pos["contracts"],
                            "qty": pos["coin_qty"],
                            "pnl_usd": round(pnl_usd, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "reason": "Stale Trade Time Exit ⏱️"
                        })
                        return
            except Exception:
                pass

        # 4. Full Take Profit Hit
        hit_tp = (current_price >= tp) if side == "long" else (current_price <= tp)
        if hit_tp:
            log(f"🎯 [TAKE PROFIT HIT] Final Target reached on {pos_sym} at ${current_price:,.2f}! Closing trade...")
            if self.cfg.live_orders:
                open_pos = self.broker.get_open_position(pos_sym)
                if open_pos and open_pos.get("contracts", 0.0) == 0.0:
                    log("   • Position already executed by Delta Cloud Take Profit.")
                else:
                    self.broker.close_position_order(side, pos["contracts"], symbol=pos_sym)
                self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason="Position closed at final Take Profit")
                self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason="Position closed at final Take Profit")

            pnl_usd = abs(tp - entry) * pos["coin_qty"]
            pnl_pct = ((tp - entry) / entry * 100) if side == "long" else ((entry - tp) / entry * 100)
            self.state["equity"] = float(self.state.get("equity", 0.0)) + pnl_usd
            self.state["cooldown_until_ts"] = int(time.time() * 1000) + (self.cfg.cooldown_bars * tf_seconds(self.cfg.ltf) * 1000)
            self.state["position"] = None
            save_state(self.state)

            record_trade({
                "symbol": pos_sym,
                "side": side.upper(),
                "entry": entry,
                "exit": tp,
                "contracts": pos["contracts"],
                "qty": pos["coin_qty"],
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": "Take Profit Final 🎯"
            })
            return

        # 5. Stop Loss Hit (or Breakeven Exit)
        hit_sl = (current_price <= sl) if side == "long" else (current_price >= sl)
        if hit_sl:
            reason = "Breakeven Shield 🛡️" if pos.get("breakeven") else "Stop Loss Hit 🛑"
            log(f"❌ [{reason}] Exit triggered on {pos_sym} at ${current_price:,.2f}!")
            if self.cfg.live_orders:
                open_pos = self.broker.get_open_position(pos_sym)
                if open_pos and open_pos.get("contracts", 0.0) == 0.0:
                    log("   • Position already executed by Delta Cloud Stop.")
                else:
                    self.broker.close_position_order(side, pos["contracts"], symbol=pos_sym)
                self.broker.cancel_order(pos.get("sl_order_id"), symbol=pos_sym, reason=f"Position closed on {reason}")
                self.broker.cancel_order(pos.get("tp_order_id"), symbol=pos_sym, reason=f"Position closed on {reason}")

            loss = abs(entry - sl) * pos["coin_qty"] if not pos.get("breakeven") else 0.0
            pnl_usd = -loss
            pnl_pct = (-loss / (entry * pos["coin_qty"] / self.cfg.leverage)) * 100 if loss > 0 else 0.0
            
            self.state["daily_loss"] = float(self.state.get("daily_loss", 0.0)) + loss
            self.state["equity"] = float(self.state.get("equity", 0.0)) - loss
            self.state["cooldown_until_ts"] = int(time.time() * 1000) + (self.cfg.cooldown_bars * tf_seconds(self.cfg.ltf) * 1000)
            self.state["position"] = None
            save_state(self.state)

            record_trade({
                "symbol": pos_sym,
                "side": side.upper(),
                "entry": entry,
                "exit": sl,
                "contracts": pos["contracts"],
                "qty": pos["coin_qty"],
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason
            })
            return

    def execute_entry(self, signal: Signal, equity: float, symbol: str):
        contracts, coin_qty = self.broker.calculate_order_sizing(signal.entry, signal.sl, equity, symbol=symbol)
        c_size = self.broker.get_contract_size(symbol)
        
        log("\n" + "🚀" * 35)
        log(f"🎯 [SMC TRIGGER CONFIRMED] Entering {signal.side.upper()} on {symbol}")
        log(f"   • Confluence:    {signal.confluence_score}/100 [{signal.reasons[0].split('[')[-1].replace(']', '')}]")
        log(f"   • Entry Price:   ${signal.entry:,.2f}")
        log(f"   • Stop Loss:     ${signal.sl:,.2f}")
        log(f"   • Take Profit:   ${signal.tp:,.2f}")
        log(f"   • R:R Ratio:     1:{signal.rr:.2f}")
        log(f"   • Contracts:     {contracts} (Unit Size: {c_size})")
        log(f"   • Rejection PA:  {signal.rejection_desc}")
        log(f"   • RSI / ADX:     RSI(14): {signal.rsi_val:.1f} | ADX: {signal.adx_val:.1f}")
        log(f"   • Volume Surge:  {signal.volume_ratio:.2f}x 20-SMA")
        if signal.score_breakdown:
            log("   • Confluence Breakdown:")
            for item in signal.score_breakdown:
                log(f"       + {item}")
        log("🚀" * 35 + "\n")

        sl_id = None
        tp_id = None

        if self.cfg.live_orders:
            order = self.broker.place_market_order(signal.side, contracts, symbol=symbol)
            if not order:
                log("❌ Order placement failed on exchange. Entry aborted.")
                return
            sl_id, tp_id = self.broker.place_native_stops(signal.side, contracts, signal.sl, signal.tp, symbol=symbol)

        pos_record = {
            "symbol": symbol,
            "side": signal.side,
            "entry": signal.entry,
            "sl": signal.sl,
            "tp": signal.tp,
            "contracts": contracts,
            "coin_qty": coin_qty,
            "breakeven": False,
            "sl_order_id": sl_id,
            "tp_order_id": tp_id,
            "entered_at": datetime.now().isoformat()
        }
        self.state["position"] = pos_record
        self.state["trades_today"] = int(self.state.get("trades_today", 0)) + 1
        save_state(self.state)
        log(f"✅ Trade registered for {symbol}. Live monitoring active.")


def main():
    parser = argparse.ArgumentParser(description="Institutional SMC Intraday Trading Bot")
    parser.add_argument("--once", action="store_true", help="Perform single scan analysis and exit")
    parser.add_argument("--selftest", action="store_true", help="Verify mathematical and indicator logic")
    parser.add_argument("--reset", action="store_true", help="Completely reset bot state, trade history, and active positions for a fresh start")
    parser.add_argument("--reset-equity", type=float, default=None, help="Set initial starting equity when resetting (e.g. --reset-equity 1000)")
    parser.add_argument("--reset-daily-loss", action="store_true", help="Reset today's daily loss tracker to unpause trading")
    args = parser.parse_args()

    cfg = load_config()

    if args.reset:
        start_eq = args.reset_equity if args.reset_equity is not None else 1000.0
        new_state = {
            "equity": start_eq,
            "day": datetime.now().strftime("%Y-%m-%d"),
            "day_start_equity": start_eq,
            "daily_loss": 0.0,
            "trades_today": 0,
            "day_r": 0.0,
            "cooldown_until_ts": 0,
            "position": None,
            "breakeven_triggered": False,
            "last_candle_ts": 0,
            "exchange": cfg.exchange,
            "symbols": cfg.symbols,
        }
        save_state(new_state)
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)
        except Exception:
            pass

        print("=" * 70)
        print("🧹 BOT STATE & TRADE HISTORY COMPLETELY RESET!")
        print("=" * 70)
        print("• Active Position   : CLEARED (None)")
        print("• Trade History     : CLEARED (0 trades)")
        print(f"• Starting Equity   : ${start_eq:,.2f}")
        print("• Daily Loss & PnL  : $0.00")
        print("• Cooldown Status   : READY (0s remaining)")
        print(f"• Traded Exchange   : {cfg.exchange.upper()}")
        print("=" * 70)
        print("👉 You are ready to start fresh! Run: py intraday_bot.py\n")
        return

    if args.reset_daily_loss:
        state = load_state(cfg)
        old_loss = float(state.get("daily_loss", 0.0))
        state["daily_loss"] = 0.0
        state["trades_today"] = 0
        state["cooldown_until_ts"] = 0
        save_state(state)
        print("=" * 70)
        print("✅ DAILY LOSS TRACKER RESET!")
        print(f"• Cleared Previous Loss : -${old_loss:,.2f}")
        print("• New Daily Loss        : $0.00")
        print("• Cooldown Status       : CLEARED (Trading Unpaused)")
        print("=" * 70)
        return

    if args.selftest:
        print("Running self-test...")
        closes = [100.0 + i * 0.5 for i in range(50)]
        e = last_value(ema(closes, 20))
        assert e is not None and e > 100.0, "EMA test failed"
        assert get_session_status(False)[0] is True, "Session filter test failed"

        # Test Synthetic Candles for SMC logic
        sample_candles = [
            Candle(1000 + i * 300, 100.0 + (i % 5), 105.0 + (i % 5), 95.0 + (i % 5), 102.0 + (i % 5), 1000.0)
            for i in range(30)
        ]
        sh, sl = find_swing_pivots(sample_candles)
        assert isinstance(sh, list) and isinstance(sl, list), "Swing pivots test failed"
        
        m_struct = analyze_market_structure(sample_candles)
        assert "pd_status" in m_struct and "eq" in m_struct, "Market structure test failed"

        # Test FVG detection with synthetic imbalance
        fvg_candles = [
            Candle(1000, 100.0, 102.0, 99.0, 101.0, 100.0),
            Candle(1300, 101.0, 115.0, 101.0, 114.0, 500.0), # Big green displacement
            Candle(1600, 114.0, 118.0, 108.0, 117.0, 200.0), # c3.low (108.0) > c1.high (102.0)
            Candle(1900, 117.0, 119.0, 112.0, 115.0, 150.0),
        ]
        fvgs = detect_fvgs(fvg_candles)
        assert len(fvgs) > 0 and fvgs[0]["type"] == "bullish", "FVG detection test failed"

        # Test Order Block detection
        ob_candles = [
            Candle(1000, 105.0, 106.0, 98.0, 99.0, 100.0),   # prior low 98.0
            Candle(1300, 100.0, 101.0, 97.0, 98.0, 200.0),   # down candle sweeping low (97.0 <= 98.0)
            Candle(1600, 98.0, 112.0, 98.0, 111.0, 600.0),   # aggressive impulse breakout
            Candle(1900, 111.0, 115.0, 110.0, 114.0, 300.0), # expansion continuation
            Candle(2200, 114.0, 116.0, 105.0, 108.0, 250.0), # retest
        ]
        obs = detect_order_blocks(ob_candles, expected_type="bullish")
        assert len(obs) > 0 and obs[0]["type"] == "bullish", "Order block detection test failed"

        # Test Liquidity Sweep
        sweep_candles = [
            Candle(1000, 100.0, 110.0, 95.0, 105.0, 100.0), # prior high 110.0
            Candle(1300, 105.0, 108.0, 102.0, 106.0, 100.0),
            Candle(1600, 106.0, 112.0, 104.0, 105.0, 250.0), # wicks to 112.0 > 110.0, closes back at 105.0
            Candle(1900, 105.0, 106.0, 101.0, 102.0, 150.0),
        ]
        sweep = detect_liquidity_sweep(sweep_candles, lookback=5, sweep_window=3)
        assert sweep is not None and sweep["type"] == "BSL_SWEEP", "Liquidity sweep test failed"

        print("✅ All Institutional SMC self-tests passed successfully!")
        return

    bot = IntradayBot(cfg)
    if args.once:
        broker = bot.broker
        broker.connect()
        btc_context = None
        sorted_symbols = sorted(cfg.symbols, key=lambda s: 0 if "BTC" in s.upper() else 1)
        for sym in sorted_symbols:
            ltf = broker.fetch_candles(cfg.ltf, 150, symbol=sym)
            setup = broker.fetch_candles(SETUP_TIMEFRAME, 150, symbol=sym)
            macro = {
                "4h": broker.fetch_candles("4h", 120, symbol=sym),
                "1d": broker.fetch_candles("1d", 100, symbol=sym),
                "1w": broker.fetch_candles("1w", 50, symbol=sym),
            }
            is_btc = "BTC" in sym.upper()
            ctx_for_pair = None if is_btc else btc_context
            info, sig, notes = analyze_smc_setup(macro, setup, ltf, cfg, btc_context=ctx_for_pair)
            if is_btc:
                btc_context = {
                    "bias": info.get("bias_1h", "NEUTRAL").lower(),
                    "trend_4h": info.get("trend_4h", "RANGE"),
                    "price": info.get("current_price", 0.0)
                }
            print_smc_structure_box(sym, info)
            for n in notes:
                print(f" • [{sym}] {n}")
            if sig:
                print(f"\n🎯 ACTIVE SIGNAL ({sym}): {sig.side.upper()} @ ${sig.entry:,.2f} | SL: ${sig.sl:,.2f} | TP: ${sig.tp:,.2f} (1:{sig.rr})")
            else:
                print(f"\nℹ️ No active trade signal on {sym} for current candle.\n")
    else:
        bot.run()


if __name__ == "__main__":
    main()
