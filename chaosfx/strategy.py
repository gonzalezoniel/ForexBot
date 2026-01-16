from dataclasses import dataclass
from typing import Literal, Optional, Tuple
import pandas as pd
import numpy as np

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
) -> Tuple[Signal, pd.DataFrame]:
    """
    Volatility + pattern-based logic:

    - Uses ATR for volatility
    - Uses MA trend as context
    - Enters only when:
        * volatility is elevated
        * AND a strong candlestick pattern appears:
            - bullish/bearish engulfing
            - bullish/bearish pin bar
    """
    df = _candle_df_from_oanda(candles)

    if len(df) < 60:
        return Signal("FLAT", None, None, "not_enough_data"), df

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

    # Volatility score (used by engine too)
    vol_score = 0.0
    if not np.isnan(last["atr"]) and last["close"] > 0:
        vol_score = float(last["atr"] / last["close"])

    # Elevated volatility? Compare to recent ATR average
    recent_atr_mean = df["atr"].rolling(window=50).mean().iloc[-1]
    high_vol = (
        not np.isnan(last["atr"])
        and not np.isnan(recent_atr_mean)
        and last["atr"] > 1.1 * recent_atr_mean
    )

    # Trend context
    trend = "FLAT"
    if last["ma_fast"] > last["ma_slow"] and df["ma_fast"].iloc[-1] > df["ma_fast"].iloc[-5]:
        trend = "UP"
    elif last["ma_fast"] < last["ma_slow"] and df["ma_fast"].iloc[-1] < df["ma_fast"].iloc[-5]:
        trend = "DOWN"

    # Patterns
    bull_engulf = _bullish_engulfing(last, prev)
    bear_engulf = _bearish_engulfing(last, prev)
    bull_pin = _bullish_pin_bar(df)
    bear_pin = _bearish_pin_bar(df)

    side: SignalSide = "FLAT"
    reason = "no_signal"

    # LONG setups
    if high_vol:
        if trend == "UP" and (bull_engulf or bull_pin):
            side = "LONG"
            if bull_engulf and bull_pin:
                reason = "trend_up_high_vol_engulf+pin"
            elif bull_engulf:
                reason = "trend_up_high_vol_engulf"
            else:
                reason = "trend_up_high_vol_pin"

        # Counter-trend long off big rejection wick
        elif trend == "DOWN" and bull_pin:
            side = "LONG"
            reason = "countertrend_bull_pin_high_vol"

        # SHORT setups
        if side == "FLAT":
            if trend == "DOWN" and (bear_engulf or bear_pin):
                side = "SHORT"
                if bear_engulf and bear_pin:
                    reason = "trend_down_high_vol_engulf+pin"
                elif bear_engulf:
                    reason = "trend_down_high_vol_engulf"
                else:
                    reason = "trend_down_high_vol_pin"

            elif trend == "UP" and bear_pin:
                side = "SHORT"
                reason = "countertrend_bear_pin_high_vol"

    if side == "FLAT":
        return Signal(side, None, None, reason), df

    price = last["close"]
    pip_factor = _pip_factor(instrument)
    sl_distance = sl_pips * pip_factor
    tp_distance = tp_pips * pip_factor

    stop_loss = None
    take_profit = None

    if side == "LONG":
        stop_loss = price - sl_distance
        take_profit = price + tp_distance
    elif side == "SHORT":
        stop_loss = price + sl_distance
        take_profit = price - tp_distance

    return Signal(side, stop_loss, take_profit, reason), df


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
    # Previous candle down, current candle up and body engulfs previous body
    prev_bear = prev["close"] < prev["open"]
    curr_bull = last["close"] > last["open"]
    body_engulf = (
        last["close"] >= prev["open"]
        and last["open"] <= prev["close"]
        and (last["close"] - last["open"]) > (prev["open"] - prev["close"]).abs()
    )
    return bool(prev_bear and curr_bull and body_engulf)


def _bearish_engulfing(last: pd.Series, prev: pd.Series) -> bool:
    prev_bull = prev["close"] > prev["open"]
    curr_bear = last["close"] < last["open"]
    body_engulf = (
        last["close"] <= prev["open"]
        and last["open"] >= prev["close"]
        and (last["open"] - last["close"]) > (prev["close"] - prev["open"]).abs()
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
    # wick at least 2x body and near local low
    is_pin = lower_wick > 2 * body and last["low"] <= recent["low"].min() + 0.25 * recent["range"].mean()
    # candle closes bullish or at least off the lows
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
