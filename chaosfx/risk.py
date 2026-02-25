import logging
from typing import Tuple, Dict, List, Any

from chaosfx.config import settings

logger = logging.getLogger("chaosfx.risk")


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
    AGGRESSIVE MODE: triggers at -6% daily drawdown.
    """
    if start_of_day_equity <= 0:
        return False, 0.0

    drawdown = (start_of_day_equity - equity) / start_of_day_equity
    return drawdown >= settings.MAX_DRAWDOWN_PER_DAY, drawdown


# ---------------------------------------------------------------------------
# Risk:Reward validation (AGGRESSIVE MODE)
# ---------------------------------------------------------------------------

def validate_risk_reward(
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    side: str,
) -> Tuple[bool, float]:
    """
    Validate that a trade meets the minimum R:R requirement.

    Returns (is_valid, actual_rr).
    No trade allowed if R:R < MIN_RISK_REWARD (2.0).
    """
    if side == "LONG":
        risk = entry_price - stop_loss_price
        reward = take_profit_price - entry_price
    else:  # SHORT
        risk = stop_loss_price - entry_price
        reward = entry_price - take_profit_price

    if risk <= 0:
        return False, 0.0

    rr = reward / risk
    return rr >= settings.MIN_RISK_REWARD, rr


def select_risk_reward_target(volatility_score: float) -> float:
    """
    Select the target R:R based on current volatility.

    - High volatility: use MAX_RISK_REWARD (1:3)
    - Medium volatility: use PREFERRED_RISK_REWARD (1:2.5)
    - Base: use MIN_RISK_REWARD (1:2)
    """
    high_vol_threshold = settings.VOLATILITY_MIN_SCORE * 2.0
    med_vol_threshold = settings.VOLATILITY_MIN_SCORE * 1.5

    if volatility_score >= high_vol_threshold:
        return settings.MAX_RISK_REWARD
    elif volatility_score >= med_vol_threshold:
        return settings.PREFERRED_RISK_REWARD
    return settings.MIN_RISK_REWARD


# ---------------------------------------------------------------------------
# Portfolio risk calculation (AGGRESSIVE MODE)
# ---------------------------------------------------------------------------

def compute_portfolio_risk(
    open_trades: List[Dict[str, Any]],
    equity: float,
) -> float:
    """
    Compute the total portfolio risk as a fraction of equity.

    Each open trade's risk = abs(units) * abs(entry - stop_loss).
    Total portfolio risk = sum of all trade risks / equity.
    """
    if equity <= 0:
        return 0.0

    total_risk = 0.0
    for trade in open_trades:
        try:
            units = abs(float(trade.get("currentUnits", trade.get("initialUnits", 0))))
            price = float(trade.get("price", 0))
            sl_orders = trade.get("stopLossOrder", {})
            if sl_orders and isinstance(sl_orders, dict):
                sl_price = float(sl_orders.get("price", price))
            else:
                sl_price = price
            stop_distance = abs(price - sl_price)
            trade_risk = units * stop_distance
            total_risk += trade_risk
        except (ValueError, TypeError):
            continue

    return total_risk / equity


def get_effective_max_trades(portfolio_risk: float) -> int:
    """
    Return the effective max open trades based on current portfolio risk.

    - Default: MAX_OPEN_TRADES (2)
    - Extended to MAX_OPEN_TRADES_EXTENDED (3) only if portfolio risk <= 4%
    """
    if portfolio_risk <= settings.MAX_TOTAL_PORTFOLIO_RISK:
        return settings.MAX_OPEN_TRADES_EXTENDED
    return settings.MAX_OPEN_TRADES


# ---------------------------------------------------------------------------
# Kill switch: consecutive losses (AGGRESSIVE MODE)
# ---------------------------------------------------------------------------

def check_consecutive_loss_kill_switch(recent_trades: List[Dict[str, Any]]) -> bool:
    """
    Returns True if the kill switch should be engaged (stop trading).

    Checks the last N trades for consecutive losses.
    A trade is a loss if its P/L < 0.
    """
    threshold = settings.KILL_SWITCH_CONSECUTIVE_LOSSES
    if len(recent_trades) < threshold:
        return False

    # Check last `threshold` trades
    last_trades = recent_trades[-threshold:]
    consecutive_losses = all(
        float(t.get("pl", t.get("realizedPL", 0))) < 0
        for t in last_trades
    )
    return consecutive_losses


# ---------------------------------------------------------------------------
# Currency exposure tracking (AGGRESSIVE MODE)
# ---------------------------------------------------------------------------

def compute_currency_exposure(
    open_trades: List[Dict[str, Any]],
) -> Dict[str, float]:
    """
    Compute net currency exposure from open trades.

    For each open trade, determine the instrument's component currencies
    and accumulate a signed exposure:
      - BUY EUR_USD: +units EUR, -units USD
      - SELL EUR_USD: -units EUR, +units USD

    Returns a dict like {"EUR": 500000, "USD": -500000, "GBP": 0, ...}
    """
    exposure: Dict[str, float] = {}

    for trade in open_trades:
        instrument = trade.get("instrument", "")
        units = float(trade.get("currentUnits", trade.get("initialUnits", 0)))
        # units > 0 = long (buy base, sell quote)
        # units < 0 = short (sell base, buy quote)

        currencies = settings.CURRENCY_COMPONENTS.get(instrument, [])
        if len(currencies) == 2:
            base_ccy, quote_ccy = currencies[0], currencies[1]
            exposure[base_ccy] = exposure.get(base_ccy, 0.0) + units
            exposure[quote_ccy] = exposure.get(quote_ccy, 0.0) - units

    return exposure


def get_usd_directional_bias(exposure: Dict[str, float]) -> str:
    """
    Determine the current USD directional bias from exposure.

    Returns "long_usd", "short_usd", or "neutral".
    """
    usd_exposure = exposure.get("USD", 0.0)
    if usd_exposure > 0:
        return "long_usd"
    elif usd_exposure < 0:
        return "short_usd"
    return "neutral"


def would_stack_usd_exposure(
    instrument: str,
    side: str,
    open_trades: List[Dict[str, Any]],
) -> bool:
    """
    Check if opening this trade would violate USD directional stacking rules.

    Only 1 strong USD bias position at a time.
    Returns True if the trade should be BLOCKED.
    """
    currencies = settings.CURRENCY_COMPONENTS.get(instrument, [])
    if "USD" not in currencies:
        return False  # non-USD pair, no stacking concern

    # Determine what USD direction this new trade would add
    if len(currencies) == 2:
        base_ccy = currencies[0]
        if base_ccy == "USD":
            # e.g., USD_JPY: LONG = long USD, SHORT = short USD
            new_usd_direction = "long_usd" if side == "LONG" else "short_usd"
        else:
            # e.g., EUR_USD: LONG = short USD, SHORT = long USD
            new_usd_direction = "short_usd" if side == "LONG" else "long_usd"
    else:
        return False

    # Count existing USD-directional trades
    usd_directional_count = 0
    for trade in open_trades:
        t_instrument = trade.get("instrument", "")
        t_currencies = settings.CURRENCY_COMPONENTS.get(t_instrument, [])
        if "USD" not in t_currencies:
            continue

        t_units = float(trade.get("currentUnits", trade.get("initialUnits", 0)))
        if len(t_currencies) == 2:
            t_base = t_currencies[0]
            if t_base == "USD":
                t_direction = "long_usd" if t_units > 0 else "short_usd"
            else:
                t_direction = "short_usd" if t_units > 0 else "long_usd"

            if t_direction == new_usd_direction:
                usd_directional_count += 1

    return usd_directional_count >= settings.MAX_USD_DIRECTIONAL_TRADES


def compute_trade_risk_pct(
    entry_price: float,
    stop_loss_price: float,
    units: int,
    equity: float,
) -> float:
    """
    Compute the risk percentage for a single trade.
    Returns the risk as a fraction of equity (e.g., 0.02 = 2%).
    """
    if equity <= 0:
        return 0.0
    stop_distance = abs(entry_price - stop_loss_price)
    trade_risk = abs(units) * stop_distance
    return trade_risk / equity


def compute_r_multiple(
    entry_price: float,
    stop_loss_price: float,
    exit_price: float,
    side: str,
) -> float:
    """
    Compute the R-multiple outcome for a closed trade.

    R = (exit - entry) / (entry - SL) for longs
    R = (entry - exit) / (SL - entry) for shorts

    Positive R = profit, negative R = loss.
    R of -1.0 = hit stop loss exactly.
    """
    if side == "LONG":
        risk = entry_price - stop_loss_price
        if risk <= 0:
            return 0.0
        return (exit_price - entry_price) / risk
    else:  # SHORT
        risk = stop_loss_price - entry_price
        if risk <= 0:
            return 0.0
        return (entry_price - exit_price) / risk
