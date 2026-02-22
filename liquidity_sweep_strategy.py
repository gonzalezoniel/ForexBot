from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Dict, Optional
from datetime import datetime, time


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


@dataclass
class LiquiditySweepConfig:
    max_spread: float          # in pips or points, depending what you feed in
    min_wick_ratio: float      # wick / total range
    rr_default: float
    rr_premium: float
    asian_session: tuple[time, time]  # reserved if you want to use it later


# --- Pair-specific configuration -------------------------------------------

# Tweaked for:
# - slightly lower wick requirement (more sweeps)
# - more attainable RR (1.8–2.5 area instead of 3–5)
PAIR_CONFIG: Dict[Symbol, LiquiditySweepConfig] = {
    "EURGBP": LiquiditySweepConfig(
        max_spread=2.5,
        min_wick_ratio=0.35,   # was 0.40 – easier to qualify a sweep
        rr_default=2.0,        # was 3.0 – more realistic reaction target
        rr_premium=2.5,        # was 3.0
        asian_session=(time(0, 0), time(7, 0)),
    ),
    "XAUUSD": LiquiditySweepConfig(
        max_spread=35.0,
        min_wick_ratio=0.30,   # was 0.35 – XAU often wicks aggressively
        rr_default=2.5,        # was 4.0
        rr_premium=3.0,        # was 5.0
        asian_session=(time(0, 0), time(7, 0)),
    ),
    "GBPCAD": LiquiditySweepConfig(
        max_spread=4.0,
        min_wick_ratio=0.35,   # was 0.40
        rr_default=2.0,        # was 3.0
        rr_premium=2.5,        # was 4.0
        asian_session=(time(0, 0), time(7, 0)),
    ),
}


# --- Utility helpers -------------------------------------------------------

def _get_last_n(candles: List[Candle], n: int) -> List[Candle]:
    return candles[-n:] if len(candles) >= n else candles


def _is_up_trend(swings: List[Candle]) -> bool:
    """Very simple HH/HL detection from a list of swing points (lows for uptrend)."""
    if len(swings) < 3:
        return False
    lows = [c.low for c in swings[-3:]]
    return lows[2] > lows[1] > lows[0]


def _is_down_trend(swings: List[Candle]) -> bool:
    """Very simple LH/LL detection from a list of swing points (highs for downtrend)."""
    if len(swings) < 3:
        return False
    highs = [c.high for c in swings[-3:]]
    return highs[2] < highs[1] < highs[0]


def _find_swings(candles: List[Candle], lookback: int = 2) -> Dict[str, List[Candle]]:
    """Detects swing highs and lows using a basic fractal concept."""
    swing_highs: List[Candle] = []
    swing_lows: List[Candle] = []

    n = len(candles)
    for i in range(lookback, n - lookback):
        c = candles[i]
        left = candles[i - lookback : i]
        right = candles[i + 1 : i + 1 + lookback]

        if all(c.high > x.high for x in left + right):
            swing_highs.append(c)
        if all(c.low < x.low for x in left + right):
            swing_lows.append(c)

    return {"highs": swing_highs, "lows": swing_lows}


def _compute_bias_4h(candles_4h: List[Candle]) -> str:
    """Returns 'bullish', 'bearish', or 'range' based on 4H structure."""
    if len(candles_4h) < 20:
        return "range"

    swings = _find_swings(candles_4h, lookback=2)
    highs = swings["highs"]
    lows = swings["lows"]

    bullish = _is_up_trend(lows)
    bearish = _is_down_trend(highs)

    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "range"


def _compute_bias_1h(candles_1h: List[Candle]) -> str:
    """Same logic as 4H but for 1H."""
    if len(candles_1h) < 20:
        return "range"

    swings = _find_swings(candles_1h, lookback=2)
    highs = swings["highs"]
    lows = swings["lows"]

    bullish = _is_up_trend(lows)
    bearish = _is_down_trend(highs)

    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "range"


# --- Liquidity levels (previous day high/low & equal highs/lows) -----------

@dataclass
class LiquidityLevel:
    price: float
    kind: Literal["PDH", "PDL", "EQH", "EQL"]


def _previous_day_high_low(candles_5m: List[Candle]) -> Optional[tuple[float, float]]:
    if not candles_5m:
        return None

    # Group by date; take previous day
    by_date: Dict[str, List[Candle]] = {}
    for c in candles_5m:
        d = c.timestamp.date().isoformat()
        by_date.setdefault(d, []).append(c)

    if len(by_date) < 2:
        return None

    dates = sorted(by_date.keys())
    prev_date = dates[-2]
    day_candles = by_date[prev_date]

    high = max(c.high for c in day_candles)
    low = min(c.low for c in day_candles)
    return high, low


def _equal_levels(
    candles_5m: List[Candle],
    threshold_ratio: float = 0.20,  # was 0.15 – slightly looser clustering
) -> List[LiquidityLevel]:
    """Find very rough equal highs/lows using an ATR-ish tolerance."""
    if len(candles_5m) < 10:
        return []

    highs = [c.high for c in candles_5m]
    lows = [c.low for c in candles_5m]
    avg_range = sum(h - l for h, l in zip(highs, lows)) / len(highs)
    if avg_range <= 0:
        return []

    tol = avg_range * threshold_ratio
    levels: List[LiquidityLevel] = []

    # group highs
    for i in range(1, len(highs)):
        if abs(highs[i] - highs[i - 1]) <= tol:
            levels.append(
                LiquidityLevel(price=(highs[i] + highs[i - 1]) / 2.0, kind="EQH")
            )

    # group lows
    for i in range(1, len(lows)):
        if abs(lows[i] - lows[i - 1]) <= tol:
            levels.append(
                LiquidityLevel(price=(lows[i] + lows[i - 1]) / 2.0, kind="EQL")
            )

    return levels


def _build_liquidity_levels(candles_5m: List[Candle]) -> List[LiquidityLevel]:
    levels: List[LiquidityLevel] = []
    pd = _previous_day_high_low(candles_5m)
    if pd:
        pdh, pdl = pd
        levels.append(LiquidityLevel(price=pdh, kind="PDH"))
        levels.append(LiquidityLevel(price=pdl, kind="PDL"))

    levels.extend(_equal_levels(candles_5m))
    return levels


# --- Sweep & BOS detection -------------------------------------------------

@dataclass
class SweepResult:
    candle: Candle
    level: LiquidityLevel
    side: Side  # side of potential trade (opposite of the sweep direction)


def _detect_sweep(
    candles_5m: List[Candle],
    levels: List[LiquidityLevel],
    bias: str,
    cfg: LiquiditySweepConfig,
) -> Optional[SweepResult]:
    if not candles_5m or not levels:
        return None

    last = candles_5m[-1]
    rng = last.high - last.low
    if rng <= 0:
        return None

    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    # For bullish bias we want a sweep BELOW (grab liquidity then go up)
    if bias == "bullish" and lower_ratio >= cfg.min_wick_ratio:
        for level in levels:
            if last.low < level.price <= last.close:
                return SweepResult(candle=last, level=level, side="long")

    # For bearish bias we want a sweep ABOVE
    if bias == "bearish" and upper_ratio >= cfg.min_wick_ratio:
        for level in levels:
            if last.high > level.price >= last.close:
                return SweepResult(candle=last, level=level, side="short")

    return None


def _confirm_bos(
    candles_5m: List[Candle],
    sweep: SweepResult,
) -> bool:
    """
    Very simple BOS: break of minor swing in direction of 'side' after the sweep candle.

    Loosened slightly to increase hit rate:
      - shorter lookback window (3 instead of 5)
      - allow wick-based break (high/low), not only close
    """
    if len(candles_5m) < 10:
        return False

    # find index of sweep candle
    try:
        idx = next(i for i, c in enumerate(candles_5m) if c.timestamp == sweep.candle.timestamp)
    except StopIteration:
        return False

    # look back a few bars before sweep to define a minor structure level
    lookback = candles_5m[max(0, idx - 3) : idx]  # was 5

    if sweep.side == "long":
        if not lookback:
            return False
        minor_high = max(c.high for c in lookback)
        for c in candles_5m[idx + 1 :]:
            # allow wick break, not just close break
            if c.high > minor_high:
                return True
    else:  # short
        if not lookback:
            return False
        minor_low = min(c.low for c in lookback)
        for c in candles_5m[idx + 1 :]:
            if c.low < minor_low:
                return True

    return False


# --- RR selection & SL/TP calculation -------------------------------------

def _choose_rr(symbol: Symbol, sweep: SweepResult) -> float:
    cfg = PAIR_CONFIG[symbol]
    # simple rule: PDH/PDL = premium RR, else default
    if sweep.level.kind in ("PDH", "PDL"):
        return cfg.rr_premium
    return cfg.rr_default


def _calc_sl_tp(
    entry: float,
    side: Side,
    sweep: SweepResult,
    rr: float,
    buffer_points: float = 0.0,
) -> tuple[float, float]:
    """SL beyond sweep wick, TP based on RR."""
    if side == "long":
        sl = sweep.candle.low - buffer_points
        risk = entry - sl
        tp = entry + risk * rr
    else:
        sl = sweep.candle.high + buffer_points
        risk = sl - entry
        tp = entry - risk * rr
    return sl, tp


# --- Public API: generate_signals -----------------------------------------

class MarketDataInterface:
    """
    Minimal interface the rest of your bot can implement so this strategy
    can fetch candles & spread info without caring about the broker.
    """

    def get_candles(self, symbol: Symbol, timeframe: str, limit: int) -> List[Candle]:
        raise NotImplementedError

    def get_spread(self, symbol: Symbol) -> float:
        raise NotImplementedError


def generate_signals(market: MarketDataInterface, now_utc: datetime) -> List[Signal]:
    """
    Main strategy entrypoint.

    - Computes 4H and 1H bias
    - Confirms pair-specific rules
    - Checks 5M liquidity sweep + BOS
    - Returns a list of Signal objects (0, 1, or more)
    """
    signals: List[Signal] = []

    for symbol in ("EURGBP", "XAUUSD", "GBPCAD"):
        cfg = PAIR_CONFIG[symbol]

        # --- Spread filter (convert raw spread to pips) ---
        raw_spread = market.get_spread(symbol)
        pip_f = PIP_FACTOR.get(symbol, 0.0001)
        spread_in_pips = raw_spread / pip_f if pip_f > 0 else float("inf")
        if spread_in_pips > cfg.max_spread:
            continue

        # --- Get candles ---
        candles_4h = market.get_candles(symbol, "4H", limit=150)
        candles_1h = market.get_candles(symbol, "1H", limit=150)
        candles_5m = market.get_candles(symbol, "5M", limit=200)

        if len(candles_4h) < 30 or len(candles_1h) < 30 or len(candles_5m) < 50:
            continue

        bias_4h = _compute_bias_4h(candles_4h)
        bias_1h = _compute_bias_1h(candles_1h)

        if bias_4h == "range":
            # For all three pairs we skip range conditions
            continue

        # Pair-specific alignment logic
        if symbol in ("EURGBP", "GBPCAD"):
            if bias_1h != bias_4h:
                continue
            bias = bias_4h
        else:  # XAUUSD: trust 4H more, ignore 1H conflict
            bias = bias_4h

        # --- Liquidity levels & sweep ---
        levels = _build_liquidity_levels(candles_5m)
        sweep = _detect_sweep(candles_5m, levels, bias, cfg)
        if not sweep:
            continue

        # --- BOS confirmation ---
        bos_ok = _confirm_bos(candles_5m, sweep)
        if not bos_ok:
            continue

        # --- Build signal ---
        last = candles_5m[-1]
        entry = last.close
        rr = _choose_rr(symbol, sweep)
        # Add a small buffer beyond the sweep wick so SL isn't right at the
        # liquidity level.  For XAU use a wider buffer (price is ~2000).
        pip_f = PIP_FACTOR.get(symbol, 0.0001)
        buffer = 3.0 * pip_f  # 3 pips / 3 points buffer
        sl, tp = _calc_sl_tp(entry, sweep.side, sweep, rr, buffer_points=buffer)

        sig = Signal(
            symbol=symbol,
            side=sweep.side,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            comment=f"HTF {bias.upper()} + 5M liquidity sweep ({sweep.level.kind}) + BOS",
        )
        signals.append(sig)

    return signals
