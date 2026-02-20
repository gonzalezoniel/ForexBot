from typing import List
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
    MAX_PAIRS: int = 6  # max symbols to scan per cycle

    # Trading universe
    FOREX_PAIRS: List[str] = [
        "EUR_USD",
        "GBP_USD",
        "USD_JPY",
        "XAU_USD",
        "AUD_USD",
        "USD_CHF",
    ]

    # Risk management
    RISK_PER_TRADE: float = 0.01  # 1% of equity per trade
    MAX_DRAWDOWN_PER_DAY: float = 0.03
    MAX_OPEN_TRADES: int = 5

    # Volatility & selection
    VOLATILITY_MIN_SCORE: float = 0.0003  # ATR/price threshold (rough)
    VOLATILITY_TOP_K: int = 4            # trade only top K most volatile symbols

    # Volatility surge mode
    VOLATILITY_EXTREME_MULTIPLIER: float = 1.8   # extreme threshold = MIN_SCORE * this
    EXTREME_RISK_FACTOR: float = 0.5            # use 50% of normal size in surge mode

    # Confidence scoring
    CONFIDENCE_MIN: float = 1.5   # minimum confidence to allow a trade

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

    # Liquidity engine toggle (used by app.py)
    LIQUIDITY_TRADING_ENABLED: int = 0


    # Defaults
    DEFAULT_SL_PIPS: float = 15.0
    DEFAULT_TP_PIPS: float = 30.0

    # ATR-based dynamic SL/TP (multipliers of ATR)
    ATR_SL_MULTIPLIER: float = 1.2
    ATR_TP_MULTIPLIER: float = 2.0


settings = Settings()
