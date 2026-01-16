from pydantic import BaseSettings, Field
from typing import List


class Settings(BaseSettings):
    # General
    ENV: str = Field("dev", description="Environment name")
    LOG_LEVEL: str = "INFO"

    # Engine timing
    LOOP_INTERVAL_SECONDS: int = 60  # how often to run main loop
    MAX_PAIRS: int = 6

    # Trading universe
    FOREX_PAIRS: List[str] = [
        "EUR_USD",
        "GBP_USD",
        "USD_JPY",
        "XAU_USD",   # gold
        "AUD_USD",
        "USD_CHF",
    ]

    # Risk management
    RISK_PER_TRADE: float = 0.01  # 1% of equity
    MAX_DRAWDOWN_PER_DAY: float = 0.03  # 3% daily loss cutoff
    MAX_OPEN_TRADES: int = 5

    # Oanda API
    OANDA_API_KEY: str
    OANDA_ACCOUNT_ID: str
    OANDA_ENV: str = "practice"  # or "live"

    # Take-profit / stop-loss defaults (in pips)
    DEFAULT_SL_PIPS: float = 15.0
    DEFAULT_TP_PIPS: float = 30.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
