"""
Social Signal Engine client for ForexBot.

Fetches forex social signals from the centralized Social Signal Engine
and provides them for trade confirmation and dashboard display.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("forexbot.social_signals")

SIGNAL_ENGINE_URL = os.getenv(
    "SOCIAL_SIGNAL_ENGINE_URL", "https://app-sgvdyzun.fly.dev"
).rstrip("/")

_TIMEOUT = 10.0

# In-memory cache of latest social signals
_cached_forex_signals: List[Dict[str, Any]] = []
_cached_memecoin_signals: List[Dict[str, Any]] = []
_last_fetch: Optional[datetime] = None


async def fetch_forex_signals(
    min_confidence: float = 0.0,
    pair: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch forex social signals from the Signal Engine.

    Returns a list of dicts with keys:
        pair, sentiment, mentions, confidence, strategies, sources
    """
    global _cached_forex_signals, _last_fetch

    params: Dict[str, Any] = {}
    if min_confidence > 0:
        params["min_confidence"] = min_confidence
    if pair:
        params["pair"] = pair

    url = f"{SIGNAL_ENGINE_URL}/api/signals/forex"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        signals = data.get("forex", [])
        _cached_forex_signals = signals
        _last_fetch = datetime.now(timezone.utc)
        logger.info("Fetched %d forex social signals from Signal Engine", len(signals))
        return signals

    except httpx.HTTPStatusError as e:
        logger.warning("Signal Engine HTTP error: %s", e.response.status_code)
    except httpx.RequestError as e:
        logger.warning("Signal Engine request error: %s", e)
    except Exception as e:
        logger.error("Unexpected error fetching social signals: %s", e)

    return _cached_forex_signals


async def fetch_memecoin_signals(
    min_mentions: int = 0,
    min_sentiment: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Fetch memecoin social signals from the Signal Engine.

    Returns a list of dicts with keys:
        token, contract, mentions, sentiment, trend, engagement, sources
    """
    global _cached_memecoin_signals

    params: Dict[str, Any] = {}
    if min_mentions > 0:
        params["min_mentions"] = min_mentions
    if min_sentiment > 0:
        params["min_sentiment"] = min_sentiment

    url = f"{SIGNAL_ENGINE_URL}/api/signals/memecoins"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        signals = data.get("memecoins", [])
        _cached_memecoin_signals = signals
        logger.info("Fetched %d memecoin social signals from Signal Engine", len(signals))
        return signals

    except Exception as e:
        logger.warning("Error fetching memecoin signals: %s", e)

    return _cached_memecoin_signals


async def fetch_full_feed() -> Dict[str, Any]:
    """Fetch the complete signal feed (memecoins + forex)."""
    url = f"{SIGNAL_ENGINE_URL}/api/signals"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Error fetching full signal feed: %s", e)
        return {"memecoins": _cached_memecoin_signals, "forex": _cached_forex_signals}


def get_cached_forex_signals() -> List[Dict[str, Any]]:
    """Return the most recently fetched forex signals (no network call)."""
    return _cached_forex_signals


def get_cached_last_fetch() -> Optional[datetime]:
    """Return the timestamp of the last successful fetch."""
    return _last_fetch


def get_social_sentiment_for_pair(pair: str) -> Optional[Dict[str, Any]]:
    """
    Look up the social sentiment for a specific forex pair from cached signals.

    The Signal Engine uses pairs like 'EURUSD' while OANDA uses 'EUR_USD'.
    This handles both formats.

    Returns dict with: pair, sentiment, mentions, confidence, strategies
    or None if no signal found.
    """
    normalized = pair.replace("_", "").upper()
    for signal in _cached_forex_signals:
        if signal.get("pair", "").upper() == normalized:
            return signal
    return None
