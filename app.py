from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI

from forexbot_core import run_tick, BrokerClient, Quote
from liquidity_sweep_strategy import Candle, Symbol


app = FastAPI(
    title="ForexBot – Liquidity Sweep Strategy",
    version="1.0.0",
    description=(
        "Forex day trading engine focused on EURGBP, XAUUSD, GBPCAD using "
        "4H/1H bias + 5M liquidity sweeps and BOS confirmation."
    ),
)


# ---------------------------------------------------------------------------
# Dummy broker implementation (SAFE: no real orders, no real data)
# ---------------------------------------------------------------------------


class DummyBroker(BrokerClient):
    """
    This is a placeholder broker implementation so the app runs safely.

    - get_ohlc returns an empty list -> strategy finds no signals
    - get_quote returns 0/0 -> spread 0 (not used because no candles)
    - place_order only prints to console

    Once you're ready to connect to OANDA/MT5, replace this with a real
    implementation that subclasses BrokerClient and overrides the methods.
    """

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        # TODO: Replace with real broker candles.
        # Example expected structure:
        # return [
        #   {
        #       "timestamp": datetime(..., tzinfo=timezone.utc),
        #       "open": 1.2345,
        #       "high": 1.2350,
        #       "low": 1.2330,
        #       "close": 1.2340,
        #   },
        #   ...
        # ]
        return []

    def get_quote(self, symbol: str) -> Quote:
        # TODO: Replace with real broker quote.
        # Must return Quote(bid=..., ask=...)
        return Quote(bid=0.0, ask=0.0)

    def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        # TODO: Replace with real order placement.
        print(
            f"[DummyBroker] place_order called: "
            f"{symbol} {side} size={size} entry={entry} "
            f"SL={stop_loss} TP={take_profit}"
        )
        return {"status": "dummy", "detail": "No real broker is configured."}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    """
    Root endpoint for Render and browser checks.
    Shows basic status and current UTC timestamp.
    """
    now = datetime.now(timezone.utc)
    return {
        "service": "forexbot",
        "status": "ok",
        "timestamp_utc": now.isoformat(),
        "message": "ForexBot is running. Liquidity sweep strategy is loaded.",
    }


@app.get("/health")
async def health():
    """
    Simple health endpoint for uptime checks.
    """
    return {"status": "healthy"}


@app.post("/tick")
async def run_strategy_tick():
    """
    Manually trigger one strategy evaluation tick.

    For now:
      - Uses DummyBroker (no live trading)
      - Logs signals, if any, to stdout (Render logs)
    """
    now = datetime.now(timezone.utc)

    broker = DummyBroker()
    # Fake balance for size calculation; doesn't matter until we go live.
    fake_balance = 10_000.0

    # This will:
    #   - Build a MyMarket wrapper
    #   - Call generate_signals(...)
    #   - Print signals to logs
    run_tick(broker_client=broker, balance=fake_balance, risk_pct_per_trade=0.5)

    return {
        "status": "tick_completed",
        "timestamp_utc": now.isoformat(),
        "note": (
            "Tick executed with DummyBroker. "
            "No real orders were placed. Check logs for signals."
        ),
    }
