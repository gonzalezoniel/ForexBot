"""
Trend Momentum Strategy
========================

Multi-timeframe trend-following strategy with momentum confirmation.

Entry logic (all conditions must be met):
  1. **4H Trend** — EMA(21) vs EMA(55) determines directional bias.
  2. **1H Momentum** — RSI(14) confirms momentum is with the trend and not
     exhausted (long: 40-70, short: 30-60).
  3. **5M Pullback Entry** — Price pulls back near the 5M EMA(21) and prints
     a reversal candle (engulfing or pin bar) in the trend direction.
  4. **Volatility Gate** — 5M ATR(14) must be above a minimum threshold so
     we only trade when the market is actually moving.
  5. **Spread Filter** — Spread must be below a per-pair maximum.

Exit:
  - SL: 1.5× ATR(14) beyond entry.
  - TP: Dynamic R:R (2.0–3.0) × SL distance depending on trend strength.

Pairs: EUR_GBP, XAU_USD, GBP_CAD (same OANDA instruments as before).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Dict, Optional
from datetime import datetime


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Symbol = Literal["EURGBP", "XAUUSD", "GBPCAD"]
Side = Literal["long", "short"]

PIP_FACTOR: Dict[str, float] = {
    "EURGBP": 0.0001,
    "XAUUSD": 0.01,
    "GBPCAD": 0.0001,
}


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Signal:
    symbol: Symbol
    side: Side
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    timeframe_entry: str = "5m"
    comment: str = ""


# ---------------------------------------------------------------------------
# Per-pair configuration
# ---------------------------------------------------------------------------

@dataclass
class PairConfig:
    max_spread_pips: float       # max spread in pips
    atr_sl_multiplier: float     # SL = atr * this
    min_rr: float                # minimum R:R to accept
    max_rr: float                # R:R used in strong trends
    min_atr_pips: float          # minimum ATR in pips to trade
    pullback_atr_ratio: float    # how close price must be to EMA (as fraction of ATR)
    rsi_long_min: float          # RSI lower bound for longs
    rsi_long_max: float          # RSI upper bound for longs
    rsi_short_min: float         # RSI lower bound for shorts
    rsi_short_max: float         # RSI upper bound for shorts


PAIR_CONFIG: Dict[Symbol, PairConfig] = {
    "EURGBP": PairConfig(
        max_spread_pips=4.0,
        atr_sl_multiplier=1.5,
        min_rr=1.5,
        max_rr=3.0,
        min_atr_pips=1.5,
        pullback_atr_ratio=2.0,
        rsi_long_min=35.0,
        rsi_long_max=75.0,
        rsi_short_min=25.0,
        rsi_short_max=65.0,
    ),
    "XAUUSD": PairConfig(
        max_spread_pips=50.0,
        atr_sl_multiplier=1.5,
        min_rr=1.5,
        max_rr=3.0,
        min_atr_pips=50.0,
        pullback_atr_ratio=2.0,
        rsi_long_min=35.0,
        rsi_long_max=78.0,  # XAU trends hard, allow higher RSI
        rsi_short_min=22.0,
        rsi_short_max=65.0,
    ),
    "GBPCAD": PairConfig(
        max_spread_pips=6.0,
        atr_sl_multiplier=1.5,
        min_rr=1.5,
        max_rr=3.0,
        min_atr_pips=2.0,
        pullback_atr_ratio=2.0,
        rsi_long_min=35.0,
        rsi_long_max=75.0,
        rsi_short_min=25.0,
        rsi_short_max=65.0,
    ),
}


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------

def _ema(values: List[float], period: int) -> List[float]:
    """Compute EMA over a list of floats. Returns list of same length (NaN-padded)."""
    if not values or period <= 0:
        return []
    result: List[float] = []
    k = 2.0 / (period + 1)
    ema_val = 0.0
    for i, v in enumerate(values):
        if i == 0:
            ema_val = v
        else:
            ema_val = v * k + ema_val * (1.0 - k)
        result.append(ema_val)
    return result


def _rsi(closes: List[float], period: int = 14) -> List[float]:
    """Compute RSI. Returns list of same length as *closes* (padded with 50.0)."""
    n = len(closes)
    if n < period + 1:
        return [50.0] * n

    result: List[float] = [50.0] * (period)  # pad first `period` entries
    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Initial averages from first `period` deltas
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # RSI at index `period` (corresponds to closes[period])
    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100.0 - 100.0 / (1.0 + rs))

    # Smoothed RSI for remaining bars
    for i in range(period + 1, len(gains) + 1):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - 100.0 / (1.0 + rs))

    return result


def _atr(candles: List[Candle], period: int = 14) -> List[float]:
    """Compute ATR. Returns list of same length (0.0 padded)."""
    if len(candles) < 2:
        return [0.0] * len(candles)

    tr_values: List[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        tr_values.append(tr)

    # Simple rolling mean for ATR
    atr_values: List[float] = []
    for i in range(len(tr_values)):
        if i < period - 1:
            atr_values.append(0.0)
        else:
            window = tr_values[i - period + 1: i + 1]
            atr_values.append(sum(window) / period)

    return atr_values


# ---------------------------------------------------------------------------
# 4H Trend detection via EMA crossover
# ---------------------------------------------------------------------------

def _compute_trend_4h(candles_4h: List[Candle]) -> str:
    """
    Returns 'bullish', 'bearish', or 'flat' based on 4H EMA(21) vs EMA(55).
    Requires EMA(21) to be clearly above/below EMA(55) for at least the
    last 3 candles to confirm a trend.
    """
    if len(candles_4h) < 60:
        return "flat"

    closes = [c.close for c in candles_4h]
    ema21 = _ema(closes, 21)
    ema55 = _ema(closes, 55)

    # Check the last 2 candles for consistent trend (relaxed from 3)
    bullish_count = 0
    bearish_count = 0
    for i in range(-2, 0):
        if ema21[i] > ema55[i]:
            bullish_count += 1
        elif ema21[i] < ema55[i]:
            bearish_count += 1

    if bullish_count == 2:
        return "bullish"
    if bearish_count == 2:
        return "bearish"
    return "flat"


# ---------------------------------------------------------------------------
# 1H RSI momentum check
# ---------------------------------------------------------------------------

def _check_momentum_1h(candles_1h: List[Candle], trend: str, cfg: PairConfig) -> bool:
    """
    Confirm momentum on 1H using RSI(14).
    For bullish trend: RSI should be in the 'goldilocks' zone — trending but
    not overbought (e.g. 40–70).
    For bearish: RSI should be trending down but not oversold (30–60).
    """
    if len(candles_1h) < 20:
        return False

    closes = [c.close for c in candles_1h]
    rsi_values = _rsi(closes, 14)
    current_rsi = rsi_values[-1]

    if trend == "bullish":
        return cfg.rsi_long_min <= current_rsi <= cfg.rsi_long_max
    elif trend == "bearish":
        return cfg.rsi_short_min <= current_rsi <= cfg.rsi_short_max
    return False


# ---------------------------------------------------------------------------
# 5M Pullback + Reversal candle detection
# ---------------------------------------------------------------------------

def _is_bullish_engulfing(curr: Candle, prev: Candle) -> bool:
    """Bullish engulfing: previous red, current green, body engulfs."""
    prev_body = prev.close - prev.open
    curr_body = curr.close - curr.open
    if prev_body >= 0 or curr_body <= 0:
        return False
    return (
        curr.close >= prev.open
        and curr.open <= prev.close
        and abs(curr_body) > abs(prev_body)
    )


def _is_bearish_engulfing(curr: Candle, prev: Candle) -> bool:
    """Bearish engulfing: previous green, current red, body engulfs."""
    prev_body = prev.close - prev.open
    curr_body = curr.close - curr.open
    if prev_body <= 0 or curr_body >= 0:
        return False
    return (
        curr.close <= prev.open
        and curr.open >= prev.close
        and abs(curr_body) > abs(prev_body)
    )


def _is_bullish_pin_bar(candle: Candle) -> bool:
    """Long lower wick, small upper wick, close near high."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    lower_wick = min(candle.open, candle.close) - candle.low
    upper_wick = candle.high - max(candle.open, candle.close)
    body = abs(candle.close - candle.open)
    return (
        lower_wick > 1.0 * body
        and lower_wick > 0.35 * rng
        and upper_wick < 0.4 * rng
    )


def _is_bearish_pin_bar(candle: Candle) -> bool:
    """Long upper wick, small lower wick, close near low."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    upper_wick = candle.high - max(candle.open, candle.close)
    lower_wick = min(candle.open, candle.close) - candle.low
    body = abs(candle.close - candle.open)
    return (
        upper_wick > 1.0 * body
        and upper_wick > 0.35 * rng
        and lower_wick < 0.4 * rng
    )


def _is_bullish_hammer(candle: Candle) -> bool:
    """Hammer pattern — synonym for bullish pin bar with more generous thresholds."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    lower_wick = min(candle.open, candle.close) - candle.low
    body = abs(candle.close - candle.open)
    return lower_wick > 0.8 * body and lower_wick > 0.3 * rng


def _is_bearish_shooting_star(candle: Candle) -> bool:
    """Shooting star — bearish hammer variant."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    upper_wick = candle.high - max(candle.open, candle.close)
    body = abs(candle.close - candle.open)
    return upper_wick > 0.8 * body and upper_wick > 0.3 * rng


def _is_bullish_momentum(candle: Candle) -> bool:
    """Strong green candle — close well above open with decent body."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    body = candle.close - candle.open
    return body > 0 and body > 0.5 * rng


def _is_bearish_momentum(candle: Candle) -> bool:
    """Strong red candle — close well below open with decent body."""
    rng = candle.high - candle.low
    if rng <= 0:
        return False
    body = candle.open - candle.close
    return body > 0 and body > 0.5 * rng


def _detect_pullback_entry(
    candles_5m: List[Candle],
    trend: str,
    cfg: PairConfig,
) -> Optional[Side]:
    """
    Check if any of the last 3 completed 5M candles show a pullback to
    EMA(21) with a reversal or momentum candle in the trend direction.

    Returns 'long', 'short', or None.
    """
    if len(candles_5m) < 30:
        return None

    closes = [c.close for c in candles_5m]
    ema21 = _ema(closes, 21)
    atr_values = _atr(candles_5m, 14)

    current_atr = atr_values[-1]
    if current_atr <= 0:
        return None

    max_distance = cfg.pullback_atr_ratio * current_atr

    # Scan the last 3 candles for a valid entry pattern
    for offset in range(1, 4):  # -1, -2, -3
        idx = len(candles_5m) - offset
        if idx < 1:
            break

        candle = candles_5m[idx]
        prev_candle = candles_5m[idx - 1]
        ema_at = ema21[idx]

        # How close is this candle to the EMA?
        distance_to_ema = abs(candle.close - ema_at)
        if distance_to_ema > max_distance:
            continue

        if trend == "bullish":
            has_reversal = (
                _is_bullish_engulfing(candle, prev_candle)
                or _is_bullish_pin_bar(candle)
                or _is_bullish_hammer(candle)
                or _is_bullish_momentum(candle)
            )
            # Price pulled back close to EMA from above — or touched it
            pulled_back = candle.low <= ema_at * 1.005  # within 0.5% of EMA
            if has_reversal and pulled_back:
                return "long"

        elif trend == "bearish":
            has_reversal = (
                _is_bearish_engulfing(candle, prev_candle)
                or _is_bearish_pin_bar(candle)
                or _is_bearish_shooting_star(candle)
                or _is_bearish_momentum(candle)
            )
            pulled_back = candle.high >= ema_at * 0.995
            if has_reversal and pulled_back:
                return "short"

    return None


# ---------------------------------------------------------------------------
# Dynamic R:R selection based on trend strength
# ---------------------------------------------------------------------------

def _select_rr(candles_4h: List[Candle], cfg: PairConfig) -> float:
    """
    Select R:R based on trend strength.
    Stronger trend (wider EMA gap) → higher R:R target.
    """
    if len(candles_4h) < 60:
        return cfg.min_rr

    closes = [c.close for c in candles_4h]
    ema21 = _ema(closes, 21)
    ema55 = _ema(closes, 55)

    current_price = closes[-1]
    if current_price <= 0:
        return cfg.min_rr

    ema_gap_pct = abs(ema21[-1] - ema55[-1]) / current_price * 100

    # Wider gap = stronger trend = more room for TP
    if ema_gap_pct > 0.5:
        return cfg.max_rr
    elif ema_gap_pct > 0.2:
        return (cfg.min_rr + cfg.max_rr) / 2.0
    return cfg.min_rr


# ---------------------------------------------------------------------------
# Market data interface (same contract as before)
# ---------------------------------------------------------------------------

class MarketDataInterface:
    """
    Minimal interface the rest of your bot can implement so this strategy
    can fetch candles & spread info without caring about the broker.
    """

    def get_candles(self, symbol: Symbol, timeframe: str, limit: int) -> List[Candle]:
        raise NotImplementedError

    def get_spread(self, symbol: Symbol) -> float:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Public API: generate_signals
# ---------------------------------------------------------------------------

def generate_signals(market: MarketDataInterface, now_utc: datetime) -> List[Signal]:
    """
    Main strategy entrypoint.

    For each pair:
      1. Check spread
      2. Determine 4H trend (EMA crossover)
      3. Confirm 1H momentum (RSI)
      4. Look for 5M pullback + reversal entry
      5. Calculate ATR-based SL/TP
      6. Emit Signal if all conditions pass
    """
    signals: List[Signal] = []

    for symbol in ("EURGBP", "XAUUSD", "GBPCAD"):
        cfg = PAIR_CONFIG[symbol]
        pip_f = PIP_FACTOR.get(symbol, 0.0001)

        # --- Spread filter ---
        try:
            raw_spread = market.get_spread(symbol)
        except Exception:
            continue
        spread_pips = raw_spread / pip_f if pip_f > 0 else float("inf")
        if spread_pips > cfg.max_spread_pips:
            continue

        # --- Get candles ---
        try:
            candles_4h = market.get_candles(symbol, "4H", limit=100)
            candles_1h = market.get_candles(symbol, "1H", limit=100)
            candles_5m = market.get_candles(symbol, "5M", limit=200)
        except Exception:
            continue

        if len(candles_4h) < 60 or len(candles_1h) < 20 or len(candles_5m) < 30:
            continue

        # --- 1. 4H Trend ---
        trend = _compute_trend_4h(candles_4h)
        if trend == "flat":
            continue

        # --- 2. 1H Momentum (RSI) ---
        if not _check_momentum_1h(candles_1h, trend, cfg):
            continue

        # --- 3. Volatility gate (5M ATR) ---
        atr_values = _atr(candles_5m, 14)
        current_atr = atr_values[-1]
        atr_pips = current_atr / pip_f if pip_f > 0 else 0.0
        if atr_pips < cfg.min_atr_pips:
            continue

        # --- 4. Pullback + reversal entry ---
        entry_side = _detect_pullback_entry(candles_5m, trend, cfg)
        if entry_side is None:
            continue

        # --- 5. Calculate SL/TP ---
        entry = candles_5m[-1].close
        sl_distance = current_atr * cfg.atr_sl_multiplier
        rr = _select_rr(candles_4h, cfg)
        tp_distance = sl_distance * rr

        if entry_side == "long":
            sl = entry - sl_distance
            tp = entry + tp_distance
        else:
            sl = entry + sl_distance
            tp = entry - tp_distance

        # Final safety: SL distance must be > 2x spread
        if sl_distance < 2.0 * raw_spread:
            continue

        sig = Signal(
            symbol=symbol,
            side=entry_side,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            comment=(
                f"4H {trend.upper()} trend (EMA21>EMA55) + "
                f"1H RSI momentum + 5M pullback reversal | "
                f"ATR={atr_pips:.1f}pip SL={sl_distance/pip_f:.1f}pip"
            ),
        )
        signals.append(sig)

    return signals
