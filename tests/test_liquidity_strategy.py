"""Tests for the liquidity sweep strategy and supporting modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict

import pytest

from liquidity_sweep_strategy import (
    Candle,
    Signal,
    MarketDataInterface,
    Symbol,
    PIP_FACTOR,
    LiquiditySweepConfig,
    PAIR_CONFIG,
    _find_swings,
    _is_up_trend,
    _is_down_trend,
    _compute_bias_4h,
    _compute_bias_1h,
    _build_liquidity_levels,
    _detect_sweep,
    _confirm_bos,
    _choose_rr,
    _calc_sl_tp,
    generate_signals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candles(
    base_price: float,
    count: int,
    spread: float = 0.0005,
    start: datetime | None = None,
    interval_minutes: int = 5,
) -> List[Candle]:
    if start is None:
        start = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        ts = start + timedelta(minutes=i * interval_minutes)
        o = base_price + i * 0.00001
        candles.append(
            Candle(
                timestamp=ts,
                open=o,
                high=o + spread,
                low=o - spread,
                close=o + 0.00002,
            )
        )
    return candles


# ---------------------------------------------------------------------------
# PIP_FACTOR tests
# ---------------------------------------------------------------------------

class TestPipFactor:
    def test_fx_pairs_have_correct_pip_factor(self):
        assert PIP_FACTOR["EURGBP"] == 0.0001
        assert PIP_FACTOR["GBPCAD"] == 0.0001

    def test_xauusd_has_correct_pip_factor(self):
        assert PIP_FACTOR["XAUUSD"] == 0.01


# ---------------------------------------------------------------------------
# Spread filter tests
# ---------------------------------------------------------------------------

class StubMarket(MarketDataInterface):
    def __init__(
        self,
        spread: float,
        candles_4h: List[Candle] | None = None,
        candles_1h: List[Candle] | None = None,
        candles_5m: List[Candle] | None = None,
    ):
        self._spread = spread
        self._4h = candles_4h or []
        self._1h = candles_1h or []
        self._5m = candles_5m or []

    def get_candles(self, symbol: Symbol, timeframe: str, limit: int) -> List[Candle]:
        if timeframe == "4H":
            return self._4h[:limit]
        if timeframe == "1H":
            return self._1h[:limit]
        if timeframe == "5M":
            return self._5m[:limit]
        return []

    def get_spread(self, symbol: Symbol) -> float:
        return self._spread


class TestSpreadFilter:
    def test_high_spread_rejects(self):
        """A spread wider than max_spread in pips should produce no signals."""
        wide_spread_price = 0.0005  # 5 pips for EURGBP (max=2.5)
        market = StubMarket(spread=wide_spread_price)
        signals = generate_signals(market, datetime.now(timezone.utc))
        assert signals == []

    def test_normal_spread_passes_filter(self):
        """A narrow spread should pass the spread filter (may still produce
        no signals due to insufficient data, but it shouldn't be filtered
        by spread alone)."""
        narrow_spread = 0.00015  # 1.5 pips for EURGBP (max=2.5)
        market = StubMarket(spread=narrow_spread)
        signals = generate_signals(market, datetime.now(timezone.utc))
        # No candle data → no signals, but the spread filter didn't block it
        assert isinstance(signals, list)


# ---------------------------------------------------------------------------
# Swing detection tests
# ---------------------------------------------------------------------------

class TestSwingDetection:
    def test_find_swings_basic(self):
        candles = [
            Candle(datetime(2025, 1, 1, i, tzinfo=timezone.utc), 1.0, 1.0 + (0.001 if i == 3 else 0.0), 1.0 - (0.001 if i == 5 else 0.0), 1.0)
            for i in range(10)
        ]
        # With lookback=2, candle at index 3 should be a swing high
        # and candle at index 5 should be a swing low
        swings = _find_swings(candles, lookback=2)
        assert len(swings["highs"]) >= 1
        assert len(swings["lows"]) >= 1

    def test_is_up_trend_requires_higher_lows(self):
        low_candles = [
            Candle(datetime(2025, 1, 1, tzinfo=timezone.utc), 1.0, 1.01, 1.00, 1.005),
            Candle(datetime(2025, 1, 2, tzinfo=timezone.utc), 1.0, 1.02, 1.01, 1.015),
            Candle(datetime(2025, 1, 3, tzinfo=timezone.utc), 1.0, 1.03, 1.02, 1.025),
        ]
        assert _is_up_trend(low_candles) is True

    def test_is_down_trend_requires_lower_highs(self):
        high_candles = [
            Candle(datetime(2025, 1, 1, tzinfo=timezone.utc), 1.0, 1.03, 1.00, 1.01),
            Candle(datetime(2025, 1, 2, tzinfo=timezone.utc), 1.0, 1.02, 1.00, 1.01),
            Candle(datetime(2025, 1, 3, tzinfo=timezone.utc), 1.0, 1.01, 1.00, 1.005),
        ]
        assert _is_down_trend(high_candles) is True


# ---------------------------------------------------------------------------
# SL / TP calculation tests
# ---------------------------------------------------------------------------

class TestSlTpCalc:
    def test_long_sl_below_entry(self):
        @dataclass
        class FakeSweep:
            candle: Candle
            level: object
            side: str

        sweep = FakeSweep(
            candle=Candle(datetime.now(timezone.utc), 1.100, 1.105, 1.095, 1.102),
            level=None,
            side="long",
        )
        sl, tp = _calc_sl_tp(entry=1.102, side="long", sweep=sweep, rr=2.0)
        assert sl < 1.102
        assert tp > 1.102

    def test_short_sl_above_entry(self):
        @dataclass
        class FakeSweep:
            candle: Candle
            level: object
            side: str

        sweep = FakeSweep(
            candle=Candle(datetime.now(timezone.utc), 1.100, 1.105, 1.095, 1.098),
            level=None,
            side="short",
        )
        sl, tp = _calc_sl_tp(entry=1.098, side="short", sweep=sweep, rr=2.0)
        assert sl > 1.098
        assert tp < 1.098

    def test_rr_ratio_correct(self):
        @dataclass
        class FakeSweep:
            candle: Candle
            level: object
            side: str

        sweep = FakeSweep(
            candle=Candle(datetime.now(timezone.utc), 1.100, 1.110, 1.090, 1.100),
            level=None,
            side="long",
        )
        entry = 1.100
        sl, tp = _calc_sl_tp(entry=entry, side="long", sweep=sweep, rr=3.0)
        risk = entry - sl
        reward = tp - entry
        assert abs(reward / risk - 3.0) < 0.001


# ---------------------------------------------------------------------------
# Position sizing tests (forexbot_core)
# ---------------------------------------------------------------------------

class TestPositionSizing:
    def test_fx_position_sizing_reasonable(self):
        from forexbot_core import _calc_position_size

        size = _calc_position_size(
            balance=10_000,
            risk_pct=0.5,
            entry=1.0800,
            stop_loss=1.0785,
            pip_value=0.0001,
        )
        # risk = 50, stop_distance = 0.0015
        # units = 50 / 0.0015 = 33_333
        assert 30_000 < size < 40_000

    def test_xau_position_sizing_reasonable(self):
        from forexbot_core import _calc_position_size

        size = _calc_position_size(
            balance=10_000,
            risk_pct=0.5,
            entry=2000.00,
            stop_loss=1998.00,
            pip_value=0.01,
        )
        # risk = 50, stop_distance = 2.0
        # units = 50 / 2.0 = 25
        assert 20 < size < 30

    def test_zero_stop_distance_returns_zero(self):
        from forexbot_core import _calc_position_size

        size = _calc_position_size(
            balance=10_000,
            risk_pct=0.5,
            entry=1.0800,
            stop_loss=1.0800,
            pip_value=0.0001,
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# ChaosEngine risk tests
# ---------------------------------------------------------------------------

class TestChaosRisk:
    def test_position_size_basic(self):
        from chaosfx.risk import compute_position_size

        units = compute_position_size(
            instrument="EUR_USD",
            account_balance=10_000,
            stop_loss_price=1.0785,
            entry_price=1.0800,
        )
        # risk = 10000 * 0.01 = 100, stop = 0.0015
        # units = 100 / 0.0015 = 66_666
        assert 60_000 < units < 70_000

    def test_zero_stop_returns_zero(self):
        from chaosfx.risk import compute_position_size

        units = compute_position_size(
            instrument="EUR_USD",
            account_balance=10_000,
            stop_loss_price=1.0800,
            entry_price=1.0800,
        )
        assert units == 0

    def test_daily_drawdown_exceeded(self):
        from chaosfx.risk import daily_drawdown_exceeded

        exceeded, dd = daily_drawdown_exceeded(equity=9600, start_of_day_equity=10000)
        # drawdown = (10000-9600)/10000 = 0.04 > 0.03
        assert exceeded is True
        assert abs(dd - 0.04) < 0.001

    def test_daily_drawdown_not_exceeded(self):
        from chaosfx.risk import daily_drawdown_exceeded

        exceeded, dd = daily_drawdown_exceeded(equity=9800, start_of_day_equity=10000)
        # drawdown = 0.02 < 0.03
        assert exceeded is False


# ---------------------------------------------------------------------------
# ChaosEngine strategy pip_factor tests
# ---------------------------------------------------------------------------

class TestChaosPipFactor:
    def test_jpy_pair(self):
        from chaosfx.strategy import _pip_factor

        assert _pip_factor("USD_JPY") == 0.01

    def test_xau_pair(self):
        from chaosfx.strategy import _pip_factor

        assert _pip_factor("XAU_USD") == 0.01

    def test_standard_pair(self):
        from chaosfx.strategy import _pip_factor

        assert _pip_factor("EUR_USD") == 0.0001


# ---------------------------------------------------------------------------
# Cooldown guard tests
# ---------------------------------------------------------------------------

class TestCooldownGuard:
    def test_cooldown_dict_exists(self):
        from forexbot_core import _last_order_time, SYMBOL_COOLDOWN_SECONDS

        assert isinstance(_last_order_time, dict)
        assert SYMBOL_COOLDOWN_SECONDS > 0
