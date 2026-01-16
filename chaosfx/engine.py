import time
import logging
from datetime import datetime, date
from typing import Dict, Any, List, Optional

from chaosfx.config import settings
from chaosfx.oanda_client import OandaClient
from chaosfx.strategy import generate_signal, Signal
from chaosfx.risk import compute_position_size, daily_drawdown_exceeded

logger = logging.getLogger("chaosfx")
logger.setLevel(settings.LOG_LEVEL)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)


class ChaosEngineFX:
    """
    Chaos-style Forex engine:

    - Scans multiple FX pairs
    - Generates signals using MA+momentum strategy
    - Applies risk management (fixed % per trade, daily max DD)
    - Places market orders via Oanda
    - Tracks basic history for dashboard/status endpoints
    """

    def __init__(self):
        self.client = OandaClient()
        self.last_day: date = date.today()
        self.start_of_day_equity: float = self._get_equity()

        # Dashboard / status
        self.last_summary: Optional[Dict[str, Any]] = None
        self.recent_runs: List[Dict[str, Any]] = []

        logger.info("ChaosEngine-FX initialized")

    def _get_equity(self) -> float:
        summary = self.client.get_account_summary()
        return float(summary["NAV"])

    def _refresh_daily_equity_anchor(self):
        today = date.today()
        if today != self.last_day:
            self.last_day = today
            self.start_of_day_equity = self._get_equity()
            logger.info(
                f"New trading day detected. Start-of-day equity: {self.start_of_day_equity:.2f}"
            )

    def _can_trade(self) -> bool:
        equity = self._get_equity()
        exceeded, dd = daily_drawdown_exceeded(equity, self.start_of_day_equity)
        if exceeded:
            logger.warning(
                f"Daily drawdown limit exceeded: {dd*100:.2f}% (equity {equity:.2f}). "
                "No more trades for today."
            )
            return False
        return True

    def _record_run(self, summary: Dict[str, Any]) -> None:
        """Keep a rolling history in memory (for dashboard endpoints)."""
        self.last_summary = summary
        self.recent_runs.append(summary)
        # Keep only the last 100 runs in memory
        if len(self.recent_runs) > 100:
            self.recent_runs = self.recent_runs[-100:]

    def run_once(self) -> Dict[str, Any]:
        """
        Run a single scan-execute cycle across all pairs.
        Returns summary for logging / API.
        """
        self._refresh_daily_equity_anchor()

        account = self.client.get_account_summary()
        equity = float(account["NAV"])
        open_trades = self.client.get_open_trades()

        logger.info(
            f"Run cycle - equity: {equity:.2f}, open_trades: {len(open_trades)}"
        )

        if not self._can_trade():
            summary = {
                "timestamp": datetime.utcnow().isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "daily_drawdown_limit_reached",
            }
            self._record_run(summary)
            return summary

        actions: List[Dict[str, Any]] = []

        # apply simple cap on open trades
        if len(open_trades) >= settings.MAX_OPEN_TRADES:
            logger.info("Max open trades reached, skipping new entries")
            summary = {
                "timestamp": datetime.utcnow().isoformat(),
                "equity": equity,
                "actions": [],
                "reason": "max_open_trades_reached",
            }
            self._record_run(summary)
            return summary

        for pair in settings.FOREX_PAIRS[: settings.MAX_PAIRS]:
            try:
                # Don't double stack same direction trades on same pair for now
                if any(t["instrument"] == pair for t in open_trades):
                    logger.debug(f"Skipping {pair}: trade already open")
                    continue

                candles = self.client.get_candles(pair, granularity="M1", count=200)
                signal, df = generate_signal(
                    instrument=pair,
                    candles=candles,
                    sl_pips=settings.DEFAULT_SL_PIPS,
                    tp_pips=settings.DEFAULT_TP_PIPS,
                )

                if signal.side == "FLAT":
                    logger.debug(f"{pair}: FLAT ({signal.reason})")
                    continue

                last_price = df["close"].iloc[-1]
                units = compute_position_size(
                    instrument=pair,
                    account_balance=equity,
                    stop_loss_price=signal.stop_loss,
                    entry_price=last_price,
                )

                if units <= 0:
                    logger.debug(f"{pair}: position size <= 0, skip")
                    continue

                if signal.side == "SHORT":
                    units = -abs(units)  # negative for short
                else:
                    units = abs(units)

                order_resp = self.client.create_market_order(
                    instrument=pair,
                    units=units,
                    stop_loss_price=signal.stop_loss,
                    take_profit_price=signal.take_profit,
                )

                action_info = {
                    "pair": pair,
                    "side": signal.side,
                    "units": units,
                    "entry_price": last_price,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "reason": signal.reason,
                    "order_response": order_resp,
                }
                actions.append(action_info)
                logger.info(
                    f"Opened {signal.side} {pair} units={units} "
                    f"SL={signal.stop_loss:.5f} TP={signal.take_profit:.5f} "
                    f"reason={signal.reason}"
                )

            except Exception as e:
                logger.exception(f"Error processing {pair}: {e}")

        summary = {
            "timestamp": datetime.utcnow().isoformat(),
            "equity": equity,
            "actions": actions,
            "reason": "completed",
        }
        self._record_run(summary)
        return summary

    def run_forever_blocking(self):
        """
        Simple blocking loop for local testing.
        On Render we'd probably use a background task instead.
        """
        logger.info("Starting ChaosEngine-FX loop")
        while True:
            summary = self.run_once()
            logger.debug(f"Cycle summary: {summary}")
            time.sleep(settings.LOOP_INTERVAL_SECONDS)
