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
    Basic fixed-% risk per trade, convert into units.

    Risk = RISK_PER_TRADE * equity
    Pip risk = |entry - stop| in decimal
    Units = Risk / pip_value
    Approximation assuming 1 pip value ~ 0.0001 * units (or 0.01 for JPY)
    """
    risk_amount = account_balance * settings.RISK_PER_TRADE
    pip_factor = _pip_factor(instrument)

    pip_risk = abs(entry_price - stop_loss_price) / pip_factor
    if pip_risk <= 0:
        return 0

    # Value per pip per unit ~ pip_factor (very rough)
    # So pip_value_per_unit ~ pip_factor
    # risk_amount = units * pip_risk * pip_factor -> units = risk_amount / (pip_risk * pip_factor)
    pip_value_per_unit = pip_factor
    units = risk_amount / (pip_risk * pip_value_per_unit)

    # Oanda allows fractional, but we cast to int
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
