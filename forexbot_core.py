from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Dict, Set

logger = logging.getLogger("forexbot.liquidity")


def _is_forex_market_open(now_utc: datetime) -> bool:
    """
    Returns True if the forex market is open.

    Forex trades Sun 17:00 ET (22:00 UTC) through Fri 17:00 ET (22:00 UTC).
    This means the market is CLOSED from Friday 22:00 UTC to Sunday 22:00 UTC.

    weekday(): Mon=0 … Sun=6
    """
    wd = now_utc.weekday()
    hour = now_utc.hour

    # Saturday: always closed
    if wd == 5:
        return False

    # Sunday: closed until 22:00 UTC
    if wd == 6 and hour < 22:
        return False

    # Friday: closed after 22:00 UTC
    if wd == 4 and hour >= 22:
        return False

    return True

from liquidity_sweep_strategy import (
    MarketDataInterface,
    Candle,
    Signal,
    generate_signals,
    Symbol,
)

SYMBOL_COOLDOWN_SECONDS = 300
_last_order_time: Dict[str, float] = {}


@dataclass
class Quote:
    bid: float
    ask: float


class BrokerClient:
    """
    Minimal interface for a broker used by the liquidity strategy.

    Your concrete broker (DummyBroker / OandaBroker in app.py) must implement:

      - get_ohlc(symbol, timeframe, limit) -> List[dict]
      - get_quote(symbol) -> Quote
      - place_order(symbol, side, units, entry, stop_loss, take_profit) -> Any
    """

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        raise NotImplementedError

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def place_order(
        self,
        symbol: str,
        side: str,
        units: int,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ) -> Any:
        raise NotImplementedError


class MyMarket(MarketDataInterface):
    """
    Adapter that makes your BrokerClient look like the strategy's MarketDataInterface.
    """

    def __init__(self, broker_client: BrokerClient):
        self.client = broker_client

    def get_candles(self, symbol: Symbol, timeframe: str, limit: int) -> List[Candle]:
        raw = self.client.get_ohlc(symbol, timeframe=timeframe, limit=limit)
        candles: List[Candle] = []
        for r in raw:
            candles.append(
                Candle(
                    timestamp=r["timestamp"],
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                )
            )
        return candles

    def get_spread(self, symbol: Symbol) -> float:
        quote = self.client.get_quote(symbol)
        return float(quote.ask) - float(quote.bid)


def _calc_position_size(
    balance: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    pip_value: float,
) -> float:
    """
    Position size calculator for OANDA.

    For OANDA, 1 unit = 1 of the base currency. P&L per unit for a move
    of ``stop_distance`` in price is approximately ``stop_distance`` in the
    quote currency.  So: units = risk_amount / stop_distance.

    risk_pct is in percent (0.5 = 0.5% of balance).
    pip_value is kept as a parameter for future per-pip-value refinement but
    is not used in the core formula.
    """
    risk_amount = balance * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0.0

    size = risk_amount / stop_distance
    return max(size, 0.0)


def run_tick(
    broker_client: BrokerClient,
    balance: float,
    risk_pct_per_trade: float = 0.5,
    execute_trades: bool = False,
    max_units_fx: int = 2_000,
    max_units_xau: int = 20,
) -> Dict[str, Any]:
    """
    Main entrypoint for the Liquidity Sweep engine.

    - Wraps your broker in MyMarket
    - Calls generate_signals(...)
    - Optionally places orders via broker_client.place_order(...)
    - Returns a summary dict used by the dashboard.

    Args:
        broker_client: concrete broker implementation (DummyBroker or OandaBroker).
        balance: virtual account balance used for risk sizing.
        risk_pct_per_trade: percent risk per trade (e.g. 0.5 = 0.5%).
        execute_trades: if True, send orders via broker_client.
        max_units_fx: cap on position size for FX pairs.
        max_units_xau: cap on position size for XAUUSD.

    Returns:
        {
          "timestamp": str ISO,
          "signals": [ {...}, ... ],
          "orders":  [ {...}, ... ],
        }
    """
    market = MyMarket(broker_client)
    now = datetime.now(timezone.utc)

    # --- Market hours guard ---
    if not _is_forex_market_open(now):
        logger.info("Liquidity: forex market is closed (weekend), skipping tick.")
        return {"timestamp": now.isoformat(), "signals": [], "planned_orders": [], "orders": []}

    signals: List[Signal] = generate_signals(market, now)

    if not signals:
        logger.info("Liquidity: no signals.")
        return {"timestamp": now.isoformat(), "signals": [], "planned_orders": [], "orders": []}

    signals_out: List[Dict[str, Any]] = []
    orders_out: List[Dict[str, Any]] = []
    planned_out: List[Dict[str, Any]] = []

    for sig in signals:
        logger.info(
            "LIQ SIGNAL: %s %s entry=%.5f SL=%.5f TP=%.5f RR=%s | %s",
            sig.symbol, sig.side.upper(), sig.entry, sig.stop_loss,
            sig.take_profit, sig.rr, sig.comment,
        )

        signals_out.append(
            {
                "symbol": sig.symbol,
                "side": sig.side,
                "entry": float(sig.entry),
                "stop_loss": float(sig.stop_loss),
                "take_profit": float(sig.take_profit),
                "rr": float(sig.rr),
                "comment": sig.comment,
            }
        )

        # --- Position sizing ---
        if sig.symbol == "XAUUSD":
            pip_factor = 0.01
            max_units = max_units_xau
        else:
            pip_factor = 0.0001
            max_units = max_units_fx

        size = _calc_position_size(
            balance=balance,
            risk_pct=risk_pct_per_trade,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            pip_value=pip_factor,
        )

        units = int(size)
        if units <= 0:
            logger.info("Liquidity: size <= 0 for %s, skip order.", sig.symbol)
            continue

        if units > max_units:
            units = max_units

        # --- Duplicate / cooldown guard ---
        now_ts = _time.monotonic()
        last_ts = _last_order_time.get(sig.symbol, 0.0)
        if now_ts - last_ts < SYMBOL_COOLDOWN_SECONDS:
            logger.info(
                "Liquidity: %s still in cooldown (%ds remaining), skip.",
                sig.symbol, int(SYMBOL_COOLDOWN_SECONDS - (now_ts - last_ts)),
            )
            continue

        # For OANDA: positive = buy, negative = sell
        if sig.side == "short":
            signed_units = -abs(units)
        else:
            signed_units = abs(units)

        logger.info(
            "Liquidity: computed units=%d for %s risk=%.1f%%",
            signed_units, sig.symbol, risk_pct_per_trade,
        )

        plan_payload = {
            "symbol": sig.symbol,
            "side": sig.side,
            "units": signed_units,
            "entry": float(sig.entry),
            "stop_loss": float(sig.stop_loss),
            "take_profit": float(sig.take_profit),
            "rr": float(sig.rr),
            "comment": sig.comment,
        }
        planned_out.append(plan_payload)

        order_resp: Any = None
        if execute_trades:
            try:
                order_resp = broker_client.place_order(
                    symbol=sig.symbol,
                    side=sig.side,
                    units=signed_units,
                    entry=float(sig.entry),
                    stop_loss=float(sig.stop_loss),
                    take_profit=float(sig.take_profit),
                )
                logger.info(
                    "Liquidity: order sent for %s, response=%s",
                    sig.symbol, order_resp,
                )
                _last_order_time[sig.symbol] = _time.monotonic()
                orders_out.append(
                    {
                        **plan_payload,
                        "response": order_resp,
                    }
                )
            except Exception as e:
                order_resp = {"status": "error", "detail": str(e)}
                logger.exception(
                    "Liquidity: ERROR placing order for %s", sig.symbol,
                )
                # Set cooldown on failure too so we don't spam rejected orders
                _last_order_time[sig.symbol] = _time.monotonic()
                orders_out.append(
                    {
                        **plan_payload,
                        "response": order_resp,
                    }
                )
        else:
            logger.info(
                "Liquidity: execute_trades=False, order NOT sent for %s.",
                sig.symbol,
            )

    return {
        "timestamp": now.isoformat(),
        "signals": signals_out,
        "planned_orders": planned_out,
        "orders": orders_out,
    }
