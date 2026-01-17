from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List

from liquidity_sweep_strategy import (
    MarketDataInterface,
    Candle,
    Signal,
    generate_signals,
    Symbol,
)

# ---------------------------------------------------------------------------
# Liquidity engine trading toggle
# ---------------------------------------------------------------------------

# If True, the liquidity engine will call broker_client.place_order(...)
# If False, it will remain signal-only.
LIQUIDITY_TRADING_ENABLED: bool = True

# Safety caps / minimums for OANDA units (paper)
MIN_UNITS_FX = 1000       # 1k units minimum for FX pairs
MIN_UNITS_XAU = 1         # 1 unit minimum for XAU_USD (paper)
MAX_UNITS = 200_000       # cap to prevent crazy sizing bugs


@dataclass
class Quote:
    bid: float
    ask: float


class BrokerClient:
    """
    Broker interface used by the liquidity strategy.

    Implement these methods in your concrete broker (OandaBroker in app.py).
    """

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        """
        MUST return a list of dicts like:
        {
            "timestamp": datetime in UTC,
            "open": float,
            "high": float,
            "low": float,
            "close": float,
        }
        """
        raise NotImplementedError("Implement get_ohlc in your broker client")

    def get_quote(self, symbol: str) -> Quote:
        """
        MUST return a Quote(bid=..., ask=...)
        """
        raise NotImplementedError("Implement get_quote in your broker client")

    def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ) -> Any:
        """
        Place a market order with SL & TP.

        NOTE: For OANDA, `size` should be interpreted as *units*.
        """
        raise NotImplementedError("Implement place_order in your broker client")


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


def _calc_oanda_units_from_risk(
    symbol: str,
    balance: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
) -> int:
    """
    Practical paper-trading sizing that produces REAL OANDA 'units' (int).

    We keep it simple and stable:
      risk_amount = balance * (risk_pct / 100)
      stop_distance = abs(entry - stop_loss)

    Then approximate:
      units ≈ risk_amount / stop_distance

    This is NOT perfect FX pip-value math across crosses, but:
      - it creates consistent unit sizing
      - it avoids tiny fractional sizes that get int()'d to zero
      - it executes reliably on paper

    We also enforce:
      - minimum units (FX vs XAU)
      - maximum units cap
    """
    risk_amount = balance * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)

    if stop_distance <= 0:
        return 0

    raw_units = risk_amount / stop_distance

    # Minimums per instrument family
    if symbol == "XAUUSD":
        units = max(MIN_UNITS_XAU, int(round(raw_units)))
    else:
        units = max(MIN_UNITS_FX, int(round(raw_units)))

    # Safety cap
    units = min(units, MAX_UNITS)

    return units


def run_tick(
    broker_client: BrokerClient,
    balance: float,
    risk_pct_per_trade: float = 0.5,
) -> List[Signal]:
    """
    Run one evaluation cycle of the liquidity sweep strategy.

    It:
      - Builds MyMarket wrapper
      - Calls generate_signals(...)
      - Logs the signals
      - Places paper orders via broker_client.place_order (when enabled)
      - Returns the list of Signal objects
    """
    market = MyMarket(broker_client)
    now = datetime.now(timezone.utc)

    signals = generate_signals(market, now)

    if not signals:
        print(f"[{now.isoformat()}] No signals from liquidity sweep strategy.")
        return []

    for sig in signals:
        print(
            f"[{now.isoformat()}] SIGNAL: {sig.symbol} {sig.side.upper()} "
            f"entry={sig.entry:.5f} SL={sig.stop_loss:.5f} "
            f"TP={sig.take_profit:.5f} RR={sig.rr}"
        )

        units = _calc_oanda_units_from_risk(
            symbol=sig.symbol,
            balance=balance,
            risk_pct=risk_pct_per_trade,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
        )

        print(
            f"[{now.isoformat()}] Units={units} (risk={risk_pct_per_trade}% "
            f"balance={balance:.2f})"
        )

        # --- ORDER EXECUTION (PAPER) ---
        if LIQUIDITY_TRADING_ENABLED and units > 0:
            try:
                order_resp = broker_client.place_order(
                    symbol=sig.symbol,
                    side=sig.side,
                    size=float(units),  # pass units through 'size'
                    entry=sig.entry,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                )
                print(
                    f"[{now.isoformat()}] Liquidity order response for "
                    f"{sig.symbol}: {order_resp}"
                )
            except Exception as e:
                print(
                    f"[{now.isoformat()}] ERROR placing liquidity order for "
                    f"{sig.symbol}: {e}"
                )

    return signals
