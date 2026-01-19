from dataclasses import dataclass
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
    Volatility + pattern-based logic:

    - Uses ATR for volatility
    - Uses MA trend as context
    - Enters only when:
        * volatility is elevated
        * AND a strong candlestick pattern appears:
            - bullish/bearish engulfing
            - bullish/bearish pin bar
    - Returns:
        Signal, OHLC df, meta {volatility, confidence}
    """
    df = _candle_df_from_oanda(candles)

    if len(df) < 60:
        return Signal("FLAT", None, None, "not_enough_data"), df, {
            "volatility": 0.0,
            "confidence": 0.0,
        }

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

    # Elevated volatility?
    recent_atr_mean = df["atr"].rolling(window=50).mean().iloc[-1]
    high_vol = (
        not np.isnan(last["atr"])
        and not np.isnan(recent_atr_mean)
        and last["atr"] > 1.1 * recent_atr_mean
    )

    # Trend context
    trend = "FLAT"
    if (
        last["ma_fast"] > last["ma_slow"]
        and df["ma_fast"].iloc[-1] > df["ma_fast"].iloc[-5]
    ):
        trend = "UP"
    elif (
        last["ma_fast"] < last["ma_slow"]
        and df["ma_fast"].iloc[-1] < df["ma_fast"].iloc[-5]
    ):
        trend = "DOWN"

    # Patterns
    bull_engulf = _bullish_engulfing(last, prev)
    bear_engulf = _bearish_engulfing(last, prev)
    bull_pin = _bullish_pin_bar(df)
    bear_pin = _bearish_pin_bar(df)

    side: SignalSide = "FLAT"
    reason = "no_signal"

    # Confidence components
    pattern_strength = 0.0
    trend_alignment = 0.0
    vol_component = 0.0

    if vol_score > 0 and settings.VOLATILITY_MIN_SCORE > 0:
        # cap contribution so crazy vol doesn't explode confidence
        vol_component = min(vol_score / settings.VOLATILITY_MIN_SCORE, 2.0)

    # LONG setups
    if high_vol:
        if trend == "UP" and (bull_engulf or bull_pin):
            side = "LONG"
            if bull_engulf and bull_pin:
                reason = "trend_up_high_vol_engulf+pin"
                pattern_strength = 1.5
            elif bull_engulf:
                reason = "trend_up_high_vol_engulf"
                pattern_strength = 1.2
            else:
                reason = "trend_up_high_vol_pin"
                pattern_strength = 1.0
            trend_alignment = 1.0

        elif trend == "DOWN" and bull_pin:
            side = "LONG"
            reason = "countertrend_bull_pin_high_vol"
            pattern_strength = 1.0
            trend_alignment = 0.5  # countertrend

        # SHORT setups
        if side == "FLAT":
            if trend == "DOWN" and (bear_engulf or bear_pin):
                side = "SHORT"
                if bear_engulf and bear_pin:
                    reason = "trend_down_high_vol_engulf+pin"
                    pattern_strength = 1.5
                elif bear_engulf:
                    reason = "trend_down_high_vol_engulf"
                    pattern_strength = 1.2
                else:
                    reason = "trend_down_high_vol_pin"
                    pattern_strength = 1.0
                trend_alignment = 1.0

            elif trend == "UP" and bear_pin:
                side = "SHORT"
                reason = "countertrend_bear_pin_high_vol"
                pattern_strength = 1.0
                trend_alignment = 0.5

    if side == "FLAT":
        return Signal(side, None, None, reason), df, {
            "volatility": vol_score,
            "confidence": 0.0,
        }

    # ------- ATR-based dynamic SL/TP -------
    price = last["close"]
    pip_factor = _pip_factor(instrument)

    # ATR in pips approx
    atr_in_pips = 0.0
    if not np.isnan(last["atr"]):
        atr_in_pips = last["atr"] / pip_factor

    # dynamic pips using ATR, but never smaller than defaults
    if atr_in_pips > 0:
        dyn_sl_pips = max(sl_pips, atr_in_pips * settings.ATR_SL_MULTIPLIER)
        dyn_tp_pips = max(tp_pips, atr_in_pips * settings.ATR_TP_MULTIPLIER)
    else:
        dyn_sl_pips = sl_pips
        dyn_tp_pips = tp_pips

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

    # Final confidence score
    confidence = vol_component + pattern_strength + trend_alignment

    meta = {
        "volatility": vol_score,
        "confidence": float(confidence),
        "pattern_strength": pattern_strength,
        "trend_alignment": trend_alignment,
        "vol_component": vol_component,
    }

    return Signal(side, stop_loss, take_profit, reason), df, meta


def _pip_factor(instrument: str) -> float:
    """
    Approximate decimal per pip for instrument.
    """
    if "JPY" in instrument:
        return 0.01
    else:
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
