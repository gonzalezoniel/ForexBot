from dataclasses import dataclass, field
from typing import Literal, Optional, Tuple, Dict, Any
import pandas as pd
import numpy as np

from chaosfx.config import settings

SignalSide = Literal["LONG", "SHORT", "FLAT"]


@dataclass
class Signal:
    side: SignalSide
    stop_loss: Optional[float]
    take_profit: Optional[float]
    reason: str
    risk_reward: float = 0.0  # actual R:R for this signal


def _candle_df_from_oanda(candles) -> pd.DataFrame:
    """
    Convert Oanda candle list into OHLC DataFrame.
    """
    rows = []
    for c in candles:
        if not c["complete"]:
            continue
        t = c["time"]
        mid = c["mid"]
        rows.append(
            {
                "time": pd.to_datetime(t),
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
            }
        )

    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def generate_signal(
    instrument: str,
    candles,
    sl_pips: float,
    tp_pips: float,
) -> Tuple[Signal, pd.DataFrame, Dict[str, Any]]:
    """
    AGGRESSIVE MODE strategy logic:

    - Uses ATR for volatility with ATR EXPANSION filter
    - Uses MA trend as context with trend ALIGNMENT requirement
    - Detects BREAKOUT structure (price breaking recent range)
    - Enters only when ALL conditions met:
        * ATR(14) expanding (current > multiplier * rolling mean)
        * Breakout structure confirmed
        * Trend alignment confirmed
        * Strong candlestick pattern (engulfing / pin bar)
    - No ranging-market entries in aggressive mode
    - Enforces minimum R:R of 2.0 (rejects trades below)
    - Returns:
        Signal, OHLC df, meta {volatility, confidence, atr_expanding,
        breakout_confirmed, trend_aligned, risk_reward}
    """
    df = _candle_df_from_oanda(candles)

    flat_meta = {
        "volatility": 0.0,
        "confidence": 0.0,
        "atr_expanding": False,
        "breakout_confirmed": False,
        "trend_aligned": False,
        "risk_reward": 0.0,
        "opportunity_score": 0.0,
    }

    if len(df) < 60:
        return Signal("FLAT", None, None, "not_enough_data"), df, flat_meta

    # Core features
    df["ma_fast"] = df["close"].rolling(window=10).mean()
    df["ma_slow"] = df["close"].rolling(window=30).mean()
    df["atr"] = _atr(df, period=14)
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_wick"] = df[["close", "open"]].min(axis=1) - df["low"]

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Volatility score
    vol_score = 0.0
    if not np.isnan(last["atr"]) and last["close"] > 0:
        vol_score = float(last["atr"] / last["close"])

    # -----------------------------------------------------------------------
    # AGGRESSIVE MODE: ATR expansion filter (Critical)
    # Only allow trades when ATR(14) is expanding
    # -----------------------------------------------------------------------
    recent_atr_mean = df["atr"].rolling(window=50).mean().iloc[-1]
    atr_expanding = (
        not np.isnan(last["atr"])
        and not np.isnan(recent_atr_mean)
        and last["atr"] > settings.ATR_EXPANSION_MULTIPLIER * recent_atr_mean
    )

    # -----------------------------------------------------------------------
    # AGGRESSIVE MODE: Trend alignment filter
    # Require clear directional bias — no ranging market entries
    # -----------------------------------------------------------------------
    trend = "FLAT"
    trend_aligned = False
    if (
        last["ma_fast"] > last["ma_slow"]
        and df["ma_fast"].iloc[-1] > df["ma_fast"].iloc[-5]
    ):
        trend = "UP"
        trend_aligned = True
    elif (
        last["ma_fast"] < last["ma_slow"]
        and df["ma_fast"].iloc[-1] < df["ma_fast"].iloc[-5]
    ):
        trend = "DOWN"
        trend_aligned = True

    # -----------------------------------------------------------------------
    # AGGRESSIVE MODE: Breakout structure detection
    # Price breaking above/below recent consolidation range
    # -----------------------------------------------------------------------
    breakout_confirmed = _detect_breakout(df)

    # -----------------------------------------------------------------------
    # AGGRESSIVE MODE: All three filters must pass
    # ATR expanding + Breakout confirmed + Trend aligned
    # -----------------------------------------------------------------------
    if not atr_expanding:
        flat_meta.update({
            "volatility": vol_score,
            "atr_expanding": False,
            "breakout_confirmed": breakout_confirmed,
            "trend_aligned": trend_aligned,
        })
        return Signal("FLAT", None, None, "atr_not_expanding"), df, flat_meta

    if not trend_aligned:
        flat_meta.update({
            "volatility": vol_score,
            "atr_expanding": True,
            "breakout_confirmed": breakout_confirmed,
            "trend_aligned": False,
        })
        return Signal("FLAT", None, None, "no_trend_alignment_ranging"), df, flat_meta

    if not breakout_confirmed:
        flat_meta.update({
            "volatility": vol_score,
            "atr_expanding": True,
            "breakout_confirmed": False,
            "trend_aligned": True,
        })
        return Signal("FLAT", None, None, "no_breakout_structure"), df, flat_meta

    # Patterns
    bull_engulf = _bullish_engulfing(last, prev)
    bear_engulf = _bearish_engulfing(last, prev)
    bull_pin = _bullish_pin_bar(df)
    bear_pin = _bearish_pin_bar(df)

    side: SignalSide = "FLAT"
    reason = "no_signal"

    # Confidence components
    pattern_strength = 0.0
    trend_alignment_score = 0.0
    vol_component = 0.0

    if vol_score > 0 and settings.VOLATILITY_MIN_SCORE > 0:
        vol_component = min(vol_score / settings.VOLATILITY_MIN_SCORE, 2.0)

    # LONG setups (only with trend — no countertrend in aggressive mode)
    if trend == "UP" and (bull_engulf or bull_pin):
        side = "LONG"
        if bull_engulf and bull_pin:
            reason = "aggressive_trend_up_breakout_engulf+pin"
            pattern_strength = 1.5
        elif bull_engulf:
            reason = "aggressive_trend_up_breakout_engulf"
            pattern_strength = 1.2
        else:
            reason = "aggressive_trend_up_breakout_pin"
            pattern_strength = 1.0
        trend_alignment_score = 1.0

    # SHORT setups (only with trend — no countertrend in aggressive mode)
    if side == "FLAT" and trend == "DOWN" and (bear_engulf or bear_pin):
        side = "SHORT"
        if bear_engulf and bear_pin:
            reason = "aggressive_trend_down_breakout_engulf+pin"
            pattern_strength = 1.5
        elif bear_engulf:
            reason = "aggressive_trend_down_breakout_engulf"
            pattern_strength = 1.2
        else:
            reason = "aggressive_trend_down_breakout_pin"
            pattern_strength = 1.0
        trend_alignment_score = 1.0

    if side == "FLAT":
        flat_meta.update({
            "volatility": vol_score,
            "confidence": 0.0,
            "atr_expanding": True,
            "breakout_confirmed": True,
            "trend_aligned": True,
        })
        return Signal(side, None, None, reason), df, flat_meta

    # -----------------------------------------------------------------------
    # ATR-based dynamic SL/TP with R:R enforcement
    # -----------------------------------------------------------------------
    price = last["close"]
    pip_factor = _pip_factor(instrument)

    atr_in_pips = 0.0
    if not np.isnan(last["atr"]):
        atr_in_pips = last["atr"] / pip_factor

    # Select target R:R based on volatility
    from chaosfx.risk import select_risk_reward_target
    target_rr = select_risk_reward_target(vol_score)

    # Dynamic SL using ATR
    if atr_in_pips > 0:
        dyn_sl_pips = max(sl_pips, atr_in_pips * settings.ATR_SL_MULTIPLIER)
        # TP forced to meet minimum R:R
        dyn_tp_pips = max(tp_pips, dyn_sl_pips * target_rr)
    else:
        dyn_sl_pips = sl_pips
        dyn_tp_pips = max(tp_pips, sl_pips * target_rr)

    sl_distance = dyn_sl_pips * pip_factor
    tp_distance = dyn_tp_pips * pip_factor

    stop_loss = None
    take_profit = None

    if side == "LONG":
        stop_loss = price - sl_distance
        take_profit = price + tp_distance
    elif side == "SHORT":
        stop_loss = price + sl_distance
        take_profit = price - tp_distance

    # -----------------------------------------------------------------------
    # AGGRESSIVE MODE: Validate R:R >= 2.0 (reject if below)
    # -----------------------------------------------------------------------
    from chaosfx.risk import validate_risk_reward
    rr_valid, actual_rr = validate_risk_reward(price, stop_loss, take_profit, side)

    if not rr_valid:
        flat_meta.update({
            "volatility": vol_score,
            "confidence": 0.0,
            "atr_expanding": True,
            "breakout_confirmed": True,
            "trend_aligned": True,
            "risk_reward": actual_rr,
        })
        return Signal("FLAT", None, None, f"rr_too_low_{actual_rr:.2f}"), df, flat_meta

    # Final confidence score
    confidence = vol_component + pattern_strength + trend_alignment_score

    # -----------------------------------------------------------------------
    # Opportunity score for ranking (AGGRESSIVE MODE)
    # Combines ATR expansion strength, trend strength, breakout strength
    # -----------------------------------------------------------------------
    atr_expansion_ratio = 0.0
    if not np.isnan(last["atr"]) and not np.isnan(recent_atr_mean) and recent_atr_mean > 0:
        atr_expansion_ratio = float(last["atr"] / recent_atr_mean)

    # Trend strength: how far apart are the MAs relative to price
    trend_strength = 0.0
    if last["close"] > 0:
        trend_strength = abs(float(last["ma_fast"] - last["ma_slow"])) / last["close"]

    opportunity_score = (
        atr_expansion_ratio * 0.4
        + pattern_strength * 0.3
        + (trend_strength * 10000) * 0.3  # normalize trend spread
    )

    meta = {
        "volatility": vol_score,
        "confidence": float(confidence),
        "pattern_strength": pattern_strength,
        "trend_alignment": trend_alignment_score,
        "vol_component": vol_component,
        "atr_expanding": True,
        "breakout_confirmed": True,
        "trend_aligned": True,
        "risk_reward": actual_rr,
        "target_rr": target_rr,
        "atr_expansion_ratio": atr_expansion_ratio,
        "trend_strength": trend_strength,
        "opportunity_score": float(opportunity_score),
    }

    return Signal(side, stop_loss, take_profit, reason, actual_rr), df, meta


def _detect_breakout(df: pd.DataFrame, lookback: int = 20) -> bool:
    """
    AGGRESSIVE MODE: Detect breakout structure.

    A breakout is confirmed when the latest close is above or below the
    recent consolidation range (defined by the high/low channel of the
    lookback period, excluding the last 2 candles which form the breakout).

    This filters out ranging/choppy markets.
    """
    if len(df) < lookback + 2:
        return False

    # Consolidation range = high/low of lookback candles before the last 2
    consolidation = df.iloc[-(lookback + 2):-2]
    range_high = consolidation["high"].max()
    range_low = consolidation["low"].min()

    last = df.iloc[-1]

    # Breakout above or below the consolidation range
    breakout_up = last["close"] > range_high
    breakout_down = last["close"] < range_low

    return breakout_up or breakout_down


def _pip_factor(instrument: str) -> float:
    """
    Approximate decimal per pip for instrument.
    """
    if "JPY" in instrument:
        return 0.01
    if "XAU" in instrument:
        return 0.01
    return 0.0001


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _bullish_engulfing(last: pd.Series, prev: pd.Series) -> bool:
    """
    Bullish engulfing pattern on the last two candles.
    We compare absolute body size using built-in abs() so we don't
    hit numpy.float64 `.abs` attribute errors.
    """
    prev_body = float(prev["close"] - prev["open"])
    curr_body = float(last["close"] - last["open"])

    prev_bear = prev_body < 0   # previous red
    curr_bull = curr_body > 0   # current green

    body_engulf = (
        last["close"] >= prev["open"]
        and last["open"] <= prev["close"]
        and abs(curr_body) > abs(prev_body)
    )

    return bool(prev_bear and curr_bull and body_engulf)


def _bearish_engulfing(last: pd.Series, prev: pd.Series) -> bool:
    """
    Bearish engulfing pattern on the last two candles.
    """
    prev_body = float(prev["close"] - prev["open"])
    curr_body = float(last["close"] - last["open"])

    prev_bull = prev_body > 0   # previous green
    curr_bear = curr_body < 0   # current red

    body_engulf = (
        last["close"] <= prev["open"]
        and last["open"] >= prev["close"]
        and abs(curr_body) > abs(prev_body)
    )

    return bool(prev_bull and curr_bear and body_engulf)


def _bullish_pin_bar(df: pd.DataFrame, lookback: int = 10) -> bool:
    """
    Long lower wick, small body, near short-term low.
    """
    last = df.iloc[-1]
    recent = df.iloc[-lookback:]
    body = last["body"]
    lower_wick = last["lower_wick"]
    is_pin = lower_wick > 2 * body and last["low"] <= recent["low"].min() + 0.25 * recent["range"].mean()
    closes_ok = last["close"] >= last["open"]
    return bool(is_pin and closes_ok)


def _bearish_pin_bar(df: pd.DataFrame, lookback: int = 10) -> bool:
    last = df.iloc[-1]
    recent = df.iloc[-lookback:]
    body = last["body"]
    upper_wick = last["upper_wick"]
    is_pin = upper_wick > 2 * body and last["high"] >= recent["high"].max() - 0.25 * recent["range"].mean()
    closes_ok = last["close"] <= last["open"]
    return bool(is_pin and closes_ok)
