import logging

import httpx
from typing import Dict, Any, List, Optional
from chaosfx.config import settings

logger = logging.getLogger("chaosfx.oanda")


class OandaClient:
    def __init__(self):
        self.api_key = settings.OANDA_API_KEY
        self.account_id = settings.OANDA_ACCOUNT_ID
        self.env = settings.OANDA_ENV

        if not self.api_key or not self.account_id:
            raise RuntimeError(
                "OANDA_API_KEY and OANDA_ACCOUNT_ID must be set as environment variables."
            )

        if self.env == "live":
            self.base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"

        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    def get_account_summary(self) -> Dict[str, Any]:
        resp = self._client.get(f"/accounts/{self.account_id}/summary")
        resp.raise_for_status()
        return resp.json()["account"]

    def get_open_trades(self) -> List[Dict[str, Any]]:
        resp = self._client.get(f"/accounts/{self.account_id}/openTrades")
        resp.raise_for_status()
        return resp.json().get("trades", [])

    def get_candles(
        self,
        instrument: str,
        granularity: str = "M1",
        count: int = 200,
    ) -> List[Dict[str, Any]]:
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M",  # mid prices
        }
        resp = self._client.get(f"/instruments/{instrument}/candles", params=params)
        resp.raise_for_status()
        return resp.json().get("candles", [])

    def get_open_trades_for_instrument(self, instrument: str) -> List[Dict[str, Any]]:
        """Return open trades for a specific instrument, sorted oldest-first (FIFO order)."""
        resp = self._client.get(
            f"/accounts/{self.account_id}/trades",
            params={"instrument": instrument, "state": "OPEN"},
        )
        resp.raise_for_status()
        trades = resp.json().get("trades", [])
        trades.sort(key=lambda t: t.get("openTime", ""))
        return trades

    def _close_conflicting_trades(self, instrument: str, units: int) -> List[Dict[str, Any]]:
        """
        FIFO compliance: before opening a new position, close any existing
        trades on the same instrument that are in the *opposite* direction.

        Also returns info about same-direction trades so the caller can
        decide whether to skip the new order (avoid duplicates).
        """
        open_trades = self.get_open_trades_for_instrument(instrument)
        if not open_trades:
            return []

        new_is_long = units > 0
        results: List[Dict[str, Any]] = []
        for trade in open_trades:
            current_units = int(trade.get("currentUnits", 0))
            trade_id = trade.get("id", "")
            trade_is_long = current_units > 0

            if trade_is_long != new_is_long:
                logger.info(
                    "FIFO: closing opposing trade %s (%s units) on %s before new order",
                    trade_id, current_units, instrument,
                )
                try:
                    close_resp = self.close_trade(trade_id)
                    results.append({"status": "closed", "trade_id": trade_id, "raw": close_resp})
                except Exception as e:
                    logger.error("FIFO: failed to close trade %s: %s", trade_id, e)
                    results.append({"status": "error", "trade_id": trade_id, "detail": str(e)})

        return results

    def has_open_trade_same_direction(self, instrument: str, units: int) -> bool:
        """Check if there is already an open trade in the same direction on this instrument."""
        open_trades = self.get_open_trades_for_instrument(instrument)
        new_is_long = units > 0
        for trade in open_trades:
            current_units = int(trade.get("currentUnits", 0))
            trade_is_long = current_units > 0
            if trade_is_long == new_is_long:
                return True
        return False

    def create_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        units > 0: buy, units < 0: sell

        FIFO compliance:
        - Checks for existing same-direction trades and skips if one exists
          (prevents duplicate positions with conflicting SL/TP).
        - Closes any opposing trades on the same instrument (oldest first)
          before submitting the new order.
        - Uses positionFill=REDUCE_FIRST for FIFO safety.
        """
        # --- FIFO: skip if same-direction trade already exists ---
        if self.has_open_trade_same_direction(instrument, units):
            logger.info(
                "FIFO: skipping %s units=%d — same-direction trade already open",
                instrument, units,
            )
            return {"status": "skipped", "reason": "same_direction_trade_exists"}

        # --- FIFO: close opposing trades first ---
        closed = self._close_conflicting_trades(instrument, units)
        if closed:
            logger.info(
                "FIFO: closed %d opposing trade(s) on %s before new order",
                len(closed), instrument,
            )

        order: Dict[str, Any] = {
            "order": {
                "units": str(units),
                "instrument": instrument,
                "timeInForce": "FOK",
                "type": "MARKET",
                "positionFill": "REDUCE_FIRST",
            }
        }

        if stop_loss_price is not None:
            order["order"]["stopLossOnFill"] = {"price": f"{stop_loss_price:.5f}"}
        if take_profit_price is not None:
            order["order"]["takeProfitOnFill"] = {"price": f"{take_profit_price:.5f}"}

        resp = self._client.post(f"/accounts/{self.account_id}/orders", json=order)
        resp.raise_for_status()
        data = resp.json()

        if "orderCancelTransaction" in data:
            reason = data["orderCancelTransaction"].get("reason", "UNKNOWN")
            logger.error(
                "ORDER REJECTED %s units=%d reason=%s", instrument, units, reason,
            )
            return {"status": "rejected", "reason": reason, "raw": data, "closed_trades": closed}

        logger.info("ORDER FILLED %s units=%d", instrument, units)
        data["closed_trades"] = closed
        return data

    def close_trade(self, trade_id: str) -> Dict[str, Any]:
        resp = self._client.put(
            f"/accounts/{self.account_id}/trades/{trade_id}/close"
        )
        resp.raise_for_status()
        return resp.json()
