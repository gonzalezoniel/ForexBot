from typing import Tuple
from chaosfx.config import settings
from chaosfx.strategy import _pip_factor


def compute_position_size(
    instrument: str,
    account_balance: float,
    stop_loss_price: float,
    entry_price: float,
) -> int:
    """
    Fixed-% risk per trade, convert into OANDA units.

    For OANDA, 1 unit = 1 of the base currency.  P&L for a price move of
    ``stop_distance`` is approximately ``units * stop_distance`` in the quote
    currency.  Therefore: units = risk_amount / stop_distance.
    """
    risk_amount = account_balance * settings.RISK_PER_TRADE
    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        return 0

    units = risk_amount / stop_distance
    return int(units)


def daily_drawdown_exceeded(
    equity: float,
    start_of_day_equity: float,
) -> Tuple[bool, float]:
    """
    Check if equity has dropped more than allowed daily drawdown.
    """
    if start_of_day_equity <= 0:
        return False, 0.0

    drawdown = (start_of_day_equity - equity) / start_of_day_equity
    return drawdown >= settings.MAX_DRAWDOWN_PER_DAY, drawdown
