from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Dict

from liquidity_sweep_strategy import (
    MarketDataInterface,
    Candle,
    Signal,
    generate_signals,
    Symbol,
)


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
    Very rough position size calculator.

    risk_pct is in percent (0.5 = 0.5% of balance).
    """
    risk_amount = balance * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0.0

    # size * stop_distance * pip_value = risk_amount
    size = risk_amount / (stop_distance * pip_value)
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

    signals: List[Signal] = generate_signals(market, now)

    if not signals:
        print(f"[{now.isoformat()}] Liquidity: no signals.")
        return {"timestamp": now.isoformat(), "signals": [], "planned_orders": [], "orders": []}

    signals_out: List[Dict[str, Any]] = []
    orders_out: List[Dict[str, Any]] = []
    planned_out: List[Dict[str, Any]] = []

    for sig in signals:
        # --- Log the signal ---
        print(
            f"[{now.isoformat()}] LIQ SIGNAL: {sig.symbol} {sig.side.upper()} "
            f"entry={sig.entry:.5f} SL={sig.stop_loss:.5f} "
            f"TP={sig.take_profit:.5f} RR={sig.rr} | {sig.comment}"
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
            pip_value = 1.0
            max_units = max_units_xau
        else:
            pip_value = 0.0001
            max_units = max_units_fx

        size = _calc_position_size(
            balance=balance,
            risk_pct=risk_pct_per_trade,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            pip_value=pip_value,
        )

        units = int(size)
        if units <= 0:
            print(
                f"[{now.isoformat()}] Liquidity: size <= 0 for {sig.symbol}, skip order."
            )
            continue

        if units > max_units:
            units = max_units

        # For OANDA: positive = buy, negative = sell
        if sig.side == "short":
            signed_units = -abs(units)
        else:
            signed_units = abs(units)

        print(
            f"[{now.isoformat()}] Liquidity: computed units={signed_units} "
            f"for {sig.symbol} risk={risk_pct_per_trade}%."
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
                print(
                    f"[{now.isoformat()}] Liquidity: order sent for {sig.symbol}, "
                    f"response={order_resp}"
                )
                orders_out.append(
                    {
                        **plan_payload,
                        "response": order_resp,
                    }
                )
            except Exception as e:
                order_resp = {"status": "error", "detail": str(e)}
                print(
                    f"[{now.isoformat()}] Liquidity: ERROR placing order for "
                    f"{sig.symbol}: {e}"
                )
                orders_out.append(
                    {
                        **plan_payload,
                        "response": order_resp,
                    }
                )
        else:
            print(
                f"[{now.isoformat()}] Liquidity: execute_trades=False, "
                f"order NOT sent for {sig.symbol}."
            )

    return {
        "timestamp": now.isoformat(),
        "signals": signals_out,
        "planned_orders": planned_out,
        "orders": orders_out,
    }
