import httpx
from typing import Dict, Any, List, Optional
from chaosfx.config import settings


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

    def create_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        units > 0: buy, units < 0: sell
        """
        order: Dict[str, Any] = {
            "order": {
                "units": str(units),
                "instrument": instrument,
                "timeInForce": "FOK",
                "type": "MARKET",
                "positionFill": "DEFAULT",
            }
        }

        if stop_loss_price is not None:
            order["order"]["stopLossOnFill"] = {"price": f"{stop_loss_price:.5f}"}
        if take_profit_price is not None:
            order["order"]["takeProfitOnFill"] = {"price": f"{take_profit_price:.5f}"}

        resp = self._client.post(f"/accounts/{self.account_id}/orders", json=order)
        resp.raise_for_status()
        return resp.json()

    def close_trade(self, trade_id: str) -> Dict[str, Any]:
        resp = self._client.put(
            f"/accounts/{self.account_id}/trades/{trade_id}/close"
        )
        resp.raise_for_status()
        return resp.json()
