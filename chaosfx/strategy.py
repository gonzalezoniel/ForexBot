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
    Very basic MA + momentum logic for now:
    - Use close prices
    - Fast MA vs slow MA + recent candle momentum
    """
    df = _candle_df_from_oanda(candles)

    if len(df) < 50:
        return Signal("FLAT", None, None, "not_enough_data"), df

    df["ma_fast"] = df["close"].rolling(window=10).mean()
    df["ma_slow"] = df["close"].rolling(window=30).mean()
    df["atr"] = _atr(df, period=14)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    side: SignalSide = "FLAT"
    reason = "no_signal"
    stop_loss = None
    take_profit = None

    # Simple crossover with momentum confirmation
    # LONG: fast above slow, strong close near high
    # SHORT: fast below slow, strong close near low
    if last["ma_fast"] > last["ma_slow"] and prev["ma_fast"] <= prev["ma_slow"]:
        # golden cross
        # bullish candle
        if last["close"] > last["open"] and last["close"] >= last["high"] - 0.25 * (last["high"] - last["low"]):
            side = "LONG"
            reason = "ma_bull_cross_momentum"
    elif last["ma_fast"] < last["ma_slow"] and prev["ma_fast"] >= prev["ma_slow"]:
        # death cross
        if last["close"] < last["open"] and last["close"] <= last["low"] + 0.25 * (last["high"] - last["low"]):
            side = "SHORT"
            reason = "ma_bear_cross_momentum"

    if side == "FLAT":
        return Signal(side, None, None, reason), df

    price = last["close"]

    # Use pip distance for SL/TP; simple approximation
    pip_factor = _pip_factor(instrument)
    sl_distance = sl_pips * pip_factor
    tp_distance = tp_pips * pip_factor

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
    This is rough but fine for first pass.
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
