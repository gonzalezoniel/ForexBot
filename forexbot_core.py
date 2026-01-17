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


@dataclass
class Quote:
    bid: float
    ask: float


class BrokerClient:
    """
    Placeholder interface for your real broker client.

    Replace the methods below with calls to your existing OANDA/MT5/etc. client.
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
        Place a market/limit order with SL & TP.

        For now you can leave this unimplemented or just log.
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


def _calc_position_size(
    balance: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    pip_value: float,
) -> float:
    """
    Very rough position size calculator.
    You will likely replace this with your own logic later.
    """
    risk_amount = balance * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0.0
    # size * stop_distance * pip_value = risk_amount  ->  size = risk / (dist * pip_value)
    size = risk_amount / (stop_distance * pip_value)
    return max(size, 0.0)


def run_tick(
    broker_client: BrokerClient,
    balance: float,
    risk_pct_per_trade: float = 0.5,
) -> None:
    """
    Call this from your existing main loop.

    It:
      - Builds MyMarket wrapper
      - Calls generate_signals(...)
      - Logs the signals
      - (Optional) places orders via broker_client.place_order
    """
    market = MyMarket(broker_client)
    now = datetime.now(timezone.utc)

    signals = generate_signals(market, now)

    if not signals:
        print(f"[{now.isoformat()}] No signals from liquidity sweep strategy.")
        return

    for sig in signals:
        print(
            f"[{now.isoformat()}] SIGNAL: {sig.symbol} {sig.side.upper()} "
            f"entry={sig.entry:.5f} SL={sig.stop_loss:.5f} "
            f"TP={sig.take_profit:.5f} RR={sig.rr}"
        )

        # --- ORDER EXECUTION (OPTIONAL, SAFE TO LEAVE COMMENTED WHILE TESTING) ---
        # TODO: tune pip_value / volume logic per symbol + your broker
        # Example rough pip_value assumption:
        if sig.symbol == "XAUUSD":
            pip_value = 1.0
        else:
            pip_value = 0.0001

        size = _calc_position_size(
            balance=balance,
            risk_pct=risk_pct_per_trade,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            pip_value=pip_value,
        )

        print(
            f"[{now.isoformat()}] Calculated size={size:.4f} for {sig.symbol} "
            f"risk={risk_pct_per_trade}%."
        )

        # When you're ready to let it fire live orders, uncomment and wire this:
        # order_resp = broker_client.place_order(
        #     symbol=sig.symbol,
        #     side=sig.side,
        #     size=size,
        #     entry=sig.entry,
        #     stop_loss=sig.stop_loss,
        #     take_profit=sig.take_profit,
        # )
        # print(f"[{now.isoformat()}] Order response: {order_resp}")
