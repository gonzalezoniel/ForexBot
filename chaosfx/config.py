from typing import List, Dict
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Pydantic v2 settings config
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # General
    ENV: str = Field("dev", description="Environment name")
    LOG_LEVEL: str = "INFO"

    # Engine timing
    LOOP_INTERVAL_SECONDS: int = 60
    MAX_PAIRS: int = 14  # expanded for aggressive mode instrument universe

    # -----------------------------------------------------------------------
    # Trading universe (AGGRESSIVE MODE)
    # USD majors + JPY crosses + commodity crosses + XAU_USD
    # -----------------------------------------------------------------------
    FOREX_PAIRS: List[str] = [
        # USD majors
        "EUR_USD",
        "GBP_USD",
        "USD_JPY",
        "AUD_USD",
        "USD_CHF",
        # JPY crosses
        "GBP_JPY",
        "EUR_JPY",
        "AUD_JPY",
        # Other crosses
        "EUR_GBP",
        "GBP_AUD",
        "EUR_AUD",
        # Commodities
        "XAU_USD",
    ]

    # -----------------------------------------------------------------------
    # Risk management (AGGRESSIVE MODE)
    # -----------------------------------------------------------------------
    RISK_PER_TRADE: float = 0.01          # 1% of equity per trade (reduced from 2% to limit losses)
    MAX_DRAWDOWN_PER_DAY: float = 0.05    # 5% daily drawdown kill switch
    MAX_OPEN_TRADES: int = 3              # 3 default (4 max only if total risk <= 4%)
    MAX_OPEN_TRADES_EXTENDED: int = 3     # extended slots when total risk is low
    MAX_TOTAL_PORTFOLIO_RISK: float = 0.04  # 4% max total portfolio risk

    # Kill switch: consecutive losses
    KILL_SWITCH_CONSECUTIVE_LOSSES: int = 3  # stop after 3 consecutive losses

    # -----------------------------------------------------------------------
    # Reward structure (AGGRESSIVE MODE)
    # -----------------------------------------------------------------------
    MIN_RISK_REWARD: float = 2.0            # minimum R:R = 1:2 (no trade below this)
    PREFERRED_RISK_REWARD: float = 2.5      # prefer 1:2.5 when volatility supports
    MAX_RISK_REWARD: float = 3.0            # use 1:3 in high volatility

    # -----------------------------------------------------------------------
    # Volatility & selection (AGGRESSIVE MODE)
    # -----------------------------------------------------------------------
    VOLATILITY_MIN_SCORE: float = 0.0005    # ATR/price threshold (raised to filter low-vol chop)
    VOLATILITY_TOP_K: int = 1               # trade only the single best signal

    # Volatility surge mode
    VOLATILITY_EXTREME_MULTIPLIER: float = 1.8
    EXTREME_RISK_FACTOR: float = 0.5

    # ATR expansion filter: current ATR must be > this multiplier * rolling mean ATR
    ATR_EXPANSION_MULTIPLIER: float = 1.15  # ATR must expand 15% above mean

    # -----------------------------------------------------------------------
    # Confidence scoring (AGGRESSIVE MODE)
    # -----------------------------------------------------------------------
    CONFIDENCE_MIN: float = 1.5             # moderate confluence requirement

    # -----------------------------------------------------------------------
    # Currency exposure control (AGGRESSIVE MODE)
    # -----------------------------------------------------------------------
    # Max number of trades with the same USD directional bias
    MAX_USD_DIRECTIONAL_TRADES: int = 2
    # Map each instrument to the currencies it contains
    CURRENCY_COMPONENTS: Dict[str, List[str]] = {
        "EUR_USD": ["EUR", "USD"],
        "GBP_USD": ["GBP", "USD"],
        "USD_JPY": ["USD", "JPY"],
        "AUD_USD": ["AUD", "USD"],
        "USD_CHF": ["USD", "CHF"],
        "GBP_JPY": ["GBP", "JPY"],
        "EUR_JPY": ["EUR", "JPY"],
        "AUD_JPY": ["AUD", "JPY"],
        "EUR_GBP": ["EUR", "GBP"],
        "GBP_AUD": ["GBP", "AUD"],
        "EUR_AUD": ["EUR", "AUD"],
        "XAU_USD": ["XAU", "USD"],
    }

    # Session control (UTC: 6–22 ~ London+NY)
    SESSION_ONLY: bool = True
    SESSION_UTC_START_HOUR: int = 6
    SESSION_UTC_END_HOUR: int = 22

    # Recent trades memory
    RECENT_TRADES_LIMIT: int = 20

    # Oanda API (set via environment variables or .env file)
    OANDA_API_KEY: str = ""
    OANDA_ACCOUNT_ID: str = ""
    OANDA_ENV: str = "practice"

    # Momentum engine toggle (used by app.py)
    MOMENTUM_TRADING_ENABLED: int = 1


    # Defaults
    DEFAULT_SL_PIPS: float = 15.0
    DEFAULT_TP_PIPS: float = 30.0

    # ATR-based dynamic SL/TP (multipliers of ATR)
    ATR_SL_MULTIPLIER: float = 1.5
    ATR_TP_MULTIPLIER: float = 3.0   # was 2.4 — enforce minimum 1:2 R:R


settings = Settings()
