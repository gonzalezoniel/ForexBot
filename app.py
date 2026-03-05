from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import forexbot_core
from chaosfx.engine import ChaosEngineFX
from chaosfx.config import settings
import social_signals

logger = logging.getLogger("forexbot")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
)

SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))


async def _background_loop():
    logger.info(
        "Background scheduler started (interval=%ds)", SCHEDULER_INTERVAL
    )
    await asyncio.sleep(5)
    while True:
        # --- Liquidity engine ---
        try:
            broker = get_liquidity_broker()
            oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()
            enabled = os.getenv("LIQUIDITY_TRADING_ENABLED", "0").strip() == "1"
            execute_trades = bool(enabled and oanda_env in {"practice", "live"})

            result = forexbot_core.run_tick(
                broker_client=broker,
                balance=float(os.getenv("LIQUIDITY_PAPER_BALANCE", "10000")),
                risk_pct_per_trade=float(os.getenv("LIQUIDITY_RISK_PCT", "0.5")),
                execute_trades=execute_trades,
                max_units_fx=int(os.getenv("LIQUIDITY_MAX_UNITS_FX", "2000")),
                max_units_xau=int(os.getenv("LIQUIDITY_MAX_UNITS_XAU", "20")),
            )
            _push_recent(RECENT_LIQUIDITY, result, max_len=25)
            for o in result.get("orders", []):
                _push_recent(RECENT_LIQUIDITY_TRADES, o, max_len=25)
            sigs = len(result.get("signals", []))
            ords = len(result.get("orders", []))
            logger.info("[Liquidity] signals=%d orders=%d", sigs, ords)
        except Exception:
            logger.exception("Liquidity tick failed")

        # --- ChaosFX engine ---
        try:
            engine = get_chaos_engine()
            summary = engine.run_once()
            logger.info(
                "[ChaosFX] reason=%s actions=%d",
                summary.get("reason"),
                len(summary.get("actions", [])),
            )
        except Exception:
            logger.exception("ChaosFX tick failed")

        # --- Social Signal Engine ---
        try:
            forex_signals = await social_signals.fetch_forex_signals()
            logger.info(
                "[SocialSignals] fetched %d forex signals", len(forex_signals)
            )
        except Exception:
            logger.exception("Social signal fetch failed")

        await asyncio.sleep(SCHEDULER_INTERVAL)


@asynccontextmanager
async def lifespan(application: FastAPI):
    task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
    api_key = os.getenv("OANDA_API_KEY", "").strip()
    account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()
    if api_key and account_id:
        task = asyncio.create_task(_background_loop())
        logger.info("Scheduler task created")
    else:
        logger.warning(
            "OANDA keys not set; background scheduler disabled. "
            "Set OANDA_API_KEY and OANDA_ACCOUNT_ID to enable."
        )
    yield
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Scheduler task stopped")


app = FastAPI(
    title="ForexBot – Liquidity + ChaosFX",
    version="1.2.3",
    description=(
        "Forex bot combining: "
        "Liquidity Sweep (EURGBP/XAUUSD/GBPCAD HTF bias + 5M sweeps) + "
        "ChaosEngine-FX (volatility/confidence execution)."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Minimal shared types for the liquidity engine brokers
# (local versions – we no longer import these from forexbot_core)
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    bid: float
    ask: float


class BrokerClient:
    """
    Minimal base just for type structure; concrete brokers implement:

      - get_ohlc(symbol, timeframe, limit) -> list[dict]
      - get_quote(symbol) -> Quote
      - place_order(symbol, side, units, entry, stop_loss, take_profit)
    """
    pass


# ---------------------------------------------------------------------------
# Broker implementations (Liquidity engine uses this)
# ---------------------------------------------------------------------------

class OandaBroker(BrokerClient):
    """
    OANDA v20 REST broker implementation.

    Reads config from environment:
      - OANDA_API_KEY
      - OANDA_ACCOUNT_ID
      - OANDA_ENV = 'practice' or 'live' (default: practice)

    Instruments mapping:
      EURGBP -> EUR_GBP
      XAUUSD -> XAU_USD
      GBPCAD -> GBP_CAD
    """

    SYMBOL_MAP = {
        "EURGBP": "EUR_GBP",
        "XAUUSD": "XAU_USD",
        "GBPCAD": "GBP_CAD",
    }

    TF_MAP = {
        "4H": "H4",
        "1H": "H1",
        "5M": "M5",
    }

    def __init__(self):
        api_key = os.getenv("OANDA_API_KEY", "").strip()
        account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()
        env = os.getenv("OANDA_ENV", "practice").strip().lower()

        if not api_key or not account_id:
            raise RuntimeError("OANDA_API_KEY or OANDA_ACCOUNT_ID not set")

        if env == "live":
            base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            base_url = "https://api-fxpractice.oanda.com/v3"

        self.api_key = api_key
        self.account_id = account_id
        self.base_url = base_url
        self.env = env

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def _instrument(self, symbol: str) -> str:
        if symbol not in self.SYMBOL_MAP:
            raise ValueError(f"Unsupported symbol for OANDA: {symbol}")
        return self.SYMBOL_MAP[symbol]

    # --------- Market data ----------

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        instrument = self._instrument(symbol)
        granularity = self.TF_MAP.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        url = f"{self.base_url}/instruments/{instrument}/candles"
        params = {
            "granularity": granularity,
            "count": limit,
            "price": "M",  # mid prices
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[OandaBroker] get_ohlc error for {symbol} {timeframe}: {e}")
            return []

        data = resp.json()
        candles_raw = data.get("candles", [])
        candles: List[dict] = []

        for c in candles_raw:
            if not c.get("complete", False):
                continue
            t = c.get("time")
            if not t:
                continue

            t = t.replace("Z", "+00:00")
            try:
                ts = datetime.fromisoformat(t)
            except Exception:
                continue

            mid = c.get("mid", {})
            try:
                o = float(mid.get("o"))
                h = float(mid.get("h"))
                l = float(mid.get("l"))
                cl = float(mid.get("c"))
            except Exception:
                continue

            candles.append(
                {
                    "timestamp": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": cl,
                }
            )

        return candles

    def get_quote(self, symbol: str) -> Quote:
        instrument = self._instrument(symbol)
        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        params = {"instruments": instrument}

        resp = self.session.get(url, params=params, timeout=10)
        resp.raise_for_status()

        data = resp.json()
        prices = data.get("prices", [])
        if not prices:
            raise RuntimeError(f"No pricing data returned for {symbol}")

        p = prices[0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])

        if bid <= 0 or ask <= 0:
            raise RuntimeError(f"Invalid quote for {symbol}: bid={bid} ask={ask}")

        return Quote(bid=bid, ask=ask)

    # --------- Open trades / positions (FIFO helpers) ----------

    def get_open_trades(self, instrument: str) -> List[Dict[str, Any]]:
        """
        Return open trades for *instrument* sorted oldest-first (FIFO order).

        Each element has at least: id, instrument, currentUnits, openTime.
        """
        url = f"{self.base_url}/accounts/{self.account_id}/trades"
        params = {"instrument": instrument, "state": "OPEN"}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            trades = resp.json().get("trades", [])
            # Oldest first so we close in FIFO order
            trades.sort(key=lambda t: t.get("openTime", ""))
            return trades
        except Exception as e:
            logger.warning("get_open_trades(%s) failed: %s", instrument, e)
            return []

    def close_trade(self, trade_id: str) -> Dict[str, Any]:
        """
        Close a single trade by its OANDA trade id (FIFO: always close oldest first).
        """
        url = f"{self.base_url}/accounts/{self.account_id}/trades/{trade_id}/close"
        try:
            resp = self.session.put(url, json={}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Closed trade %s: %s", trade_id, data)
            return {"status": "ok", "trade_id": trade_id, "raw": data}
        except Exception as e:
            logger.error("Failed to close trade %s: %s", trade_id, e)
            return {"status": "error", "trade_id": trade_id, "detail": str(e)}

    def has_same_direction_trade(self, instrument: str, side: str) -> bool:
        """
        FIFO: check if there's already an open trade in the same direction.
        Returns True if a duplicate same-direction position exists.
        """
        open_trades = self.get_open_trades(instrument)
        new_is_long = side.lower() != "short"
        for trade in open_trades:
            current_units = int(trade.get("currentUnits", 0))
            trade_is_long = current_units > 0
            if trade_is_long == new_is_long:
                return True
        return False

    def _close_opposing_trades(self, instrument: str, side: str) -> List[Dict[str, Any]]:
        """
        FIFO compliance: before opening a new position, close any existing
        trades on the same instrument that are in the *opposite* direction.

        Closes oldest first (FIFO order).
        Returns list of close results.
        """
        open_trades = self.get_open_trades(instrument)
        if not open_trades:
            return []

        results: List[Dict[str, Any]] = []
        for trade in open_trades:
            current_units = int(trade.get("currentUnits", 0))
            trade_id = trade.get("id", "")
            # currentUnits > 0 means long, < 0 means short
            trade_is_long = current_units > 0
            new_is_long = side.lower() != "short"

            if trade_is_long != new_is_long:
                logger.info(
                    "FIFO: closing opposing trade %s (%s units) on %s before opening %s",
                    trade_id, current_units, instrument, side.upper(),
                )
                result = self.close_trade(trade_id)
                results.append(result)

        return results

    # --------- Orders (market + SL/TP) ----------

    def place_order(
        self,
        symbol: str,
        side: str,
        units: int,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        """
        Send a MARKET order to OANDA with SL & TP.

        FIFO compliance:
        - Closes any opposing trades on the same instrument (oldest first)
          before submitting the new order.
        - Uses positionFill=REDUCE_FIRST so OANDA reduces an existing
          opposite position rather than rejecting the order.

        - units: positive magnitude (we set sign from side)
        - side: 'long' or 'short'
        """
        instrument = self._instrument(symbol)

        # --- FIFO: skip if same-direction trade already exists ---
        if self.has_same_direction_trade(instrument, side):
            logger.info(
                "FIFO: skipping %s %s — same-direction trade already open on %s",
                symbol, side.upper(), instrument,
            )
            return {"status": "skipped", "reason": "same_direction_trade_exists", "closed_trades": []}

        # --- FIFO: close opposing trades first ---
        closed = self._close_opposing_trades(instrument, side)
        if closed:
            logger.info(
                "FIFO: closed %d opposing trade(s) on %s before new %s order",
                len(closed), instrument, side.upper(),
            )

        u = abs(int(units))
        if side.lower() == "short":
            u = -u

        order_payload: Dict[str, Any] = {
            "order": {
                "units": str(u),
                "instrument": instrument,
                "timeInForce": "FOK",
                "type": "MARKET",
                "positionFill": "REDUCE_FIRST",
            }
        }

        if stop_loss is not None:
            order_payload["order"]["stopLossOnFill"] = {"price": f"{stop_loss:.5f}"}
        if take_profit is not None:
            order_payload["order"]["takeProfitOnFill"] = {"price": f"{take_profit:.5f}"}

        url = f"{self.base_url}/accounts/{self.account_id}/orders"

        resp = self.session.post(url, json=order_payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if "orderCancelTransaction" in data:
            reason = data["orderCancelTransaction"].get("reason", "UNKNOWN")
            logger.error(
                "ORDER REJECTED (%s) %s %s units=%d reason=%s",
                self.env, symbol, side.upper(), u, reason,
            )
            return {"status": "rejected", "reason": reason, "raw": data, "closed_trades": closed}

        logger.info(
            "ORDER FILLED (%s) %s %s units=%d SL=%s TP=%s",
            self.env, symbol, side.upper(), u,
            f"{stop_loss:.5f}" if stop_loss is not None else "none",
            f"{take_profit:.5f}" if take_profit is not None else "none",
        )
        return {"status": "ok", "raw": data, "closed_trades": closed}


def get_liquidity_broker() -> OandaBroker:
    """
    HARD REQUIREMENT:
    Liquidity engine must use OANDA. No DummyBroker fallback.
    This prevents silent 'it ran but didn't trade' behavior.
    """
    api_key = os.getenv("OANDA_API_KEY", "").strip()
    account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()

    if not api_key or not account_id:
        raise RuntimeError(
            "Liquidity requires OANDA. Missing OANDA_API_KEY or OANDA_ACCOUNT_ID in environment."
        )

    broker = OandaBroker()
    logger.info("LiquidityBroker: Using OandaBroker (env=%s)", broker.env)
    return broker


# ---------------------------------------------------------------------------
# ChaosFX engine (singleton-style)
# ---------------------------------------------------------------------------

CHAOS_ENGINE: Optional[ChaosEngineFX] = None


def get_chaos_engine() -> ChaosEngineFX:
    global CHAOS_ENGINE
    if CHAOS_ENGINE is None:
        CHAOS_ENGINE = ChaosEngineFX()
    return CHAOS_ENGINE


# ---------------------------------------------------------------------------
# In-memory dashboard state
# ---------------------------------------------------------------------------

RECENT_LIQUIDITY: List[Dict[str, Any]] = []
RECENT_LIQUIDITY_TRADES: List[Dict[str, Any]] = []


def _push_recent(buf: List[Dict[str, Any]], item: Dict[str, Any], max_len: int = 25):
    buf.append(item)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>ForexBot – Liquidity + ChaosFX</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #050816;
      color: #e5e7eb;
      margin: 0;
      padding: 0;
      display: flex;
      min-height: 100vh;
      align-items: center;
      justify-content: center;
    }
    .wrap { width: min(1100px, 96vw); padding: 32px 0; }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      font-size: 0.8rem; padding: 6px 12px; border-radius: 999px;
      background: rgba(16, 185, 129, 0.12); color: #6ee7b7;
      border: 1px solid rgba(16, 185, 129, 0.35);
      margin-bottom: 14px;
    }
    .dot { width: 9px; height: 9px; border-radius: 999px; background: #22c55e; box-shadow: 0 0 10px rgba(34,197,94,0.6); }
    h1 { margin: 0 0 6px; font-size: 2.2rem; color: #f9fafb; letter-spacing: -0.02em; }
    .sub { margin: 0 0 22px; color: #9ca3af; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
    .card {
      background: rgba(15,23,42,0.95);
      border: 1px solid rgba(148,163,184,0.26);
      border-radius: 20px;
      padding: 22px 22px 18px;
      box-shadow: 0 18px 45px rgba(0,0,0,0.5);
    }
    .title { font-size: 1.55rem; margin: 0 0 6px; color: #f9fafb; }
    .desc { margin: 0 0 14px; color: #a1a1aa; }
    .row { display:flex; flex-wrap:wrap; gap:10px; margin: 10px 0 14px; }
    .pill {
      font-size: 0.8rem; padding: 4px 10px; border-radius: 999px;
      background: rgba(30, 64, 175, 0.18);
      color: #bfdbfe;
      border: 1px solid rgba(59, 130, 246, 0.35);
    }
    .label { font-size: 0.72rem; letter-spacing: 0.12em; color: #6b7280; margin: 10px 0 6px; }
    button {
      display:inline-flex; align-items:center; justify-content:center; gap:8px;
      padding: 10px 16px; border-radius: 999px; border: none;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: white; font-size: 0.9rem; cursor: pointer;
      transition: transform 0.08s ease, box-shadow 0.08s ease, opacity 0.15s ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 10px 25px rgba(79,70,229,0.4); }
    button:active { transform: translateY(0); box-shadow: none; opacity: 0.9; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .status { margin-top: 10px; color: #9ca3af; min-height: 1.4em; }
    .small { font-size: 0.85rem; color: #9ca3af; margin-top: 8px; }
    .hr { height:1px; background: rgba(148,163,184,0.18); margin: 14px 0; }
    a { color: #a5b4fc; text-decoration:none; }
    a:hover { text-decoration: underline; }
    pre { margin: 0; white-space: pre-wrap; color: #d1d5db; font-size: 0.85rem; }
    .full-width { grid-column: 1 / -1; }
    .social-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
    .signal-card {
      background: rgba(10,18,36,0.85);
      border: 1px solid rgba(148,163,184,0.18);
      border-radius: 14px;
      padding: 16px;
    }
    .signal-pair { font-size: 1.1rem; font-weight: 700; color: #f9fafb; margin: 0 0 8px; }
    .sentiment-badge {
      display: inline-block; font-size: 0.75rem; padding: 3px 10px;
      border-radius: 999px; font-weight: 600; text-transform: uppercase;
    }
    .sentiment-bullish { background: rgba(34,197,94,0.18); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
    .sentiment-bearish { background: rgba(239,68,68,0.18); color: #f87171; border: 1px solid rgba(239,68,68,0.4); }
    .sentiment-neutral { background: rgba(156,163,175,0.18); color: #9ca3af; border: 1px solid rgba(156,163,175,0.4); }
    .signal-stat { display: flex; justify-content: space-between; margin: 6px 0; font-size: 0.85rem; color: #9ca3af; }
    .signal-stat span:last-child { color: #e5e7eb; font-weight: 500; }
    .conf-bar { height: 6px; border-radius: 999px; background: rgba(148,163,184,0.15); margin-top: 4px; overflow: hidden; }
    .conf-fill { height: 100%; border-radius: 999px; transition: width 0.4s ease; }
    .conf-high { background: linear-gradient(90deg, #22c55e, #4ade80); }
    .conf-med { background: linear-gradient(90deg, #eab308, #facc15); }
    .conf-low { background: linear-gradient(90deg, #6b7280, #9ca3af); }
    .signal-strategies { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 5px; }
    .strat-tag {
      font-size: 0.7rem; padding: 2px 8px; border-radius: 999px;
      background: rgba(139,92,246,0.15); color: #c4b5fd;
      border: 1px solid rgba(139,92,246,0.3);
    }
    .trade-influence {
      margin-top: 10px; padding: 8px 12px; border-radius: 10px;
      font-size: 0.8rem;
      background: rgba(79,70,229,0.1); border: 1px solid rgba(79,70,229,0.25);
      color: #a5b4fc;
    }
    .influence-aligned { background: rgba(34,197,94,0.08); border-color: rgba(34,197,94,0.25); color: #86efac; }
    .influence-conflicting { background: rgba(239,68,68,0.08); border-color: rgba(239,68,68,0.25); color: #fca5a5; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="badge"><span class="dot"></span><span>Engines online</span></div>
    <h1>ForexBot – Liquidity + ChaosFX</h1>
    <p class="sub">
      Left: Liquidity Sweep (EURGBP · XAUUSD · GBPCAD · 4H/1H → 5M sweeps + BOS)<br/>
      Right: ChaosEngine-FX (multi-pair volatility & confidence-based execution).
    </p>

    <div class="grid">
      <div class="card">
        <h2 class="title">Liquidity Sweep Engine</h2>
        <p class="desc">HTF trend bias from 4H / 1H. Entries on 5M liquidity sweeps with BOS, RR tuned for higher hit rate.</p>
        <div class="row">
          <span class="pill">Pairs: EURGBP · XAUUSD · GBPCAD</span>
          <span class="pill" id="liq-mode">Mode: …</span>
        </div>

        <div class="label">CONTROLS</div>
        <button id="liq-btn">Run Liquidity Tick ⚡</button>
        <div id="liq-status" class="status"></div>

        <div class="label">RECENT LIQUIDITY</div>
        <div class="hr"></div>
        <pre id="liq-recent">Loading…</pre>

        <div class="small">Docs: <a href="/docs" target="_blank">/docs</a> · Health: <a href="/health" target="_blank">/health</a></div>
      </div>

      <div class="card">
        <h2 class="title">ChaosEngine-FX</h2>
        <p class="desc">Volatility-ranked, confidence-filtered entries via ChaosFX strategy and risk engine.</p>
        <div class="row">
          <span class="pill">Pairs: settings.FOREX_PAIRS</span>
          <span class="pill">Mode: Live (OANDA practice/live)</span>
        </div>

        <div class="label">CONTROLS</div>
        <button id="cx-btn">Run ChaosFX Cycle ⚡</button>
        <div id="cx-status" class="status"></div>

        <div class="label">LAST SUMMARY</div>
        <div class="hr"></div>
        <pre id="cx-summary">Loading…</pre>

        <div class="label">RECENT CHAOSFX TRADES</div>
        <div class="hr"></div>
        <pre id="cx-trades">Loading…</pre>
      </div>
    </div>

    <!-- Social Signals Card (full width below the grid) -->
    <div class="card" style="margin-top: 18px;">
      <h2 class="title">Social Signals Intelligence</h2>
      <p class="desc">
        Live sentiment from social sources — influences position sizing and can block trades when strongly conflicting.
      </p>
      <div class="row">
        <span class="pill" id="ss-source">Source: loading…</span>
        <span class="pill" id="ss-count">Signals: …</span>
        <span class="pill" id="ss-updated">Updated: …</span>
      </div>

      <div class="label">CONTROLS</div>
      <button id="ss-btn">Refresh Social Signals</button>
      <div id="ss-status" class="status"></div>

      <div class="label">PAIR SENTIMENT & TRADE INFLUENCE</div>
      <div class="hr"></div>
      <div id="ss-grid" class="social-grid">
        <div style="color:#6b7280;">Loading social signals…</div>
      </div>

      <div class="label" style="margin-top:16px;">HOW SIGNALS AFFECT DECISIONS</div>
      <div class="hr"></div>
      <div style="font-size:0.85rem; color:#9ca3af; line-height:1.6;">
        <strong style="color:#e5e7eb;">Position Boost:</strong> When social sentiment <em>aligns</em> with trade direction (confidence &ge; 50%), size is increased by up to 25%.<br/>
        <strong style="color:#e5e7eb;">Position Cut:</strong> When sentiment <em>conflicts</em> (confidence &ge; 40%), size is reduced by up to 30%.<br/>
        <strong style="color:#e5e7eb;">Trade Block:</strong> If sentiment <em>strongly conflicts</em> (confidence &ge; 60%), the trade is blocked entirely.
      </div>
    </div>

    <script>
      const liqBtn = document.getElementById("liq-btn");
      const liqStatus = document.getElementById("liq-status");
      const liqRecent = document.getElementById("liq-recent");
      const liqMode = document.getElementById("liq-mode");

      const cxBtn = document.getElementById("cx-btn");
      const cxStatus = document.getElementById("cx-status");
      const cxSummary = document.getElementById("cx-summary");
      const cxTrades = document.getElementById("cx-trades");

      const ssBtn = document.getElementById("ss-btn");
      const ssStatus = document.getElementById("ss-status");
      const ssGrid = document.getElementById("ss-grid");
      const ssSource = document.getElementById("ss-source");
      const ssCount = document.getElementById("ss-count");
      const ssUpdated = document.getElementById("ss-updated");

      function esc(str) {
        const d = document.createElement("div");
        d.textContent = String(str);
        return d.innerHTML;
      }

      function renderSocialSignals(data) {
        const signals = data.forex_signals || [];
        ssSource.textContent = `Source: ${data.source || "unknown"}`;
        ssCount.textContent = `Signals: ${data.count || 0}`;
        ssUpdated.textContent = data.last_fetch
          ? `Updated: ${new Date(data.last_fetch).toLocaleTimeString()}`
          : "Updated: never";

        if (!signals.length) {
          ssGrid.innerHTML = '<div style="color:#6b7280;">No social signals available yet. The engine fetches data every tick cycle.</div>';
          return;
        }

        ssGrid.innerHTML = signals.map(sig => {
          const pair = esc(sig.pair || "???");
          const sentiment = esc((sig.sentiment || "neutral").toLowerCase());
          const confidence = parseFloat(sig.confidence || 0);
          const mentions = parseInt(sig.mentions || 0, 10);
          const strategies = (sig.strategies || []).map(s => esc(s));
          const sources = (sig.sources || []).map(s => esc(s));

          const sentClass = sentiment === "bullish" ? "sentiment-bullish"
            : sentiment === "bearish" ? "sentiment-bearish" : "sentiment-neutral";

          const confPct = Math.round(confidence * 100);
          const confClass = confidence >= 0.6 ? "conf-high" : confidence >= 0.4 ? "conf-med" : "conf-low";

          // Determine trade influence text
          let influenceHTML = "";
          if (confidence >= 0.6) {
            if (sentiment === "bullish") {
              influenceHTML = `<div class="trade-influence influence-aligned">Strongly bullish — would BOOST long sizes +25% or BLOCK shorts</div>`;
            } else if (sentiment === "bearish") {
              influenceHTML = `<div class="trade-influence influence-conflicting">Strongly bearish — would BOOST short sizes +25% or BLOCK longs</div>`;
            }
          } else if (confidence >= 0.4) {
            if (sentiment === "bullish") {
              influenceHTML = `<div class="trade-influence">Moderately bullish — would CUT short sizes by 30%</div>`;
            } else if (sentiment === "bearish") {
              influenceHTML = `<div class="trade-influence">Moderately bearish — would CUT long sizes by 30%</div>`;
            }
          }

          const stratTags = strategies.map(s => `<span class="strat-tag">${s}</span>`).join("");
          const sourceList = sources.length ? `<div class="signal-stat"><span>Sources</span><span>${sources.join(", ")}</span></div>` : "";

          return `
            <div class="signal-card">
              <div class="signal-pair">${pair} <span class="sentiment-badge ${sentClass}">${sentiment}</span></div>
              <div class="signal-stat"><span>Confidence</span><span>${confPct}%</span></div>
              <div class="conf-bar"><div class="conf-fill ${confClass}" style="width:${confPct}%"></div></div>
              <div class="signal-stat"><span>Mentions</span><span>${mentions}</span></div>
              ${sourceList}
              ${strategies.length ? `<div class="signal-strategies">${stratTags}</div>` : ""}
              ${influenceHTML}
            </div>
          `;
        }).join("");
      }

      async function refreshSocial() {
        try {
          const ss = await fetch("/api/social-signals").then(r => r.json());
          renderSocialSignals(ss);
        } catch(e) {
          // ignore
        }
      }

      async function refresh() {
        try {
          const liq = await fetch("/api/liquidity/recent").then(r => r.json());
          liqMode.textContent = liq.mode || "Mode: …";
          liqRecent.textContent = liq.text || "No liquidity runs yet.";

          const cx = await fetch("/api/chaosfx/status").then(r => r.json());
          cxSummary.textContent = cx.summary_text || "No runs yet.";
          cxTrades.textContent = cx.trades_text || "No trades recorded yet.";
        } catch(e) {
          // ignore
        }
        await refreshSocial();
      }

      liqBtn.addEventListener("click", async () => {
        liqStatus.textContent = "Running liquidity tick…";
        liqBtn.disabled = true;
        try {
          const res = await fetch("/api/liquidity/tick", { method: "POST" });
          const data = await res.json();
          liqStatus.textContent = data.note || "Done.";
        } catch (e) {
          liqStatus.textContent = "Error running tick. Check logs.";
        } finally {
          liqBtn.disabled = false;
          refresh();
        }
      });

      cxBtn.addEventListener("click", async () => {
        cxStatus.textContent = "Running ChaosFX cycle…";
        cxBtn.disabled = true;
        try {
          const res = await fetch("/api/chaosfx/tick", { method: "POST" });
          const data = await res.json();
          cxStatus.textContent = `Reason: ${data.reason} · Actions: ${data.actions || 0}`;
        } catch (e) {
          cxStatus.textContent = "Error running cycle. Check logs.";
        } finally {
          cxBtn.disabled = false;
          refresh();
        }
      });

      ssBtn.addEventListener("click", async () => {
        ssStatus.textContent = "Fetching social signals…";
        ssBtn.disabled = true;
        try {
          const res = await fetch("/api/social-signals").then(r => r.json());
          renderSocialSignals(res);
          ssStatus.textContent = `Fetched ${res.count || 0} signals.`;
        } catch(e) {
          ssStatus.textContent = "Error fetching signals. Check logs.";
        } finally {
          ssBtn.disabled = false;
        }
      });

      refresh();
      setInterval(refresh, 8000);
    </script>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health", response_class=JSONResponse)
async def health():
    return JSONResponse({"status": "healthy"})


# ---------------------------- Liquidity API -------------------------------

@app.post("/api/liquidity/tick", response_class=JSONResponse)
async def liquidity_tick():
    """
    Runs Liquidity engine once.
    If LIQUIDITY_TRADING_ENABLED=1, it will place trades against the configured
    OANDA_ENV (practice or live).
    """
    try:
        broker = get_liquidity_broker()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "note": f"Liquidity broker error: {str(e)}",
            },
        )

    oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()
    enabled = os.getenv("LIQUIDITY_TRADING_ENABLED", "0").strip() == "1"

    if enabled and oanda_env not in {"practice", "live"}:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "note": f"Invalid OANDA_ENV={oanda_env!r}. Expected 'practice' or 'live'.",
            },
        )

    execute_trades = bool(enabled and oanda_env in {"practice", "live"})

    balance = float(os.getenv("LIQUIDITY_PAPER_BALANCE", "10000"))
    risk_pct = float(os.getenv("LIQUIDITY_RISK_PCT", "0.5"))
    max_units_fx = int(os.getenv("LIQUIDITY_MAX_UNITS_FX", "2000"))
    max_units_xau = int(os.getenv("LIQUIDITY_MAX_UNITS_XAU", "20"))

    result = forexbot_core.run_tick(
        broker_client=broker,
        balance=balance,
        risk_pct_per_trade=risk_pct,
        execute_trades=execute_trades,
        max_units_fx=max_units_fx,
        max_units_xau=max_units_xau,
    )

    _push_recent(RECENT_LIQUIDITY, result, max_len=25)
    for o in result.get("orders", []):
        _push_recent(RECENT_LIQUIDITY_TRADES, o, max_len=25)

    signals_count = len(result.get("signals", []))
    planned_count = len(result.get("planned_orders", []))
    orders_count = len(result.get("orders", []))

    if execute_trades:
        mode = f"Mode: Execution enabled ({oanda_env})"
    else:
        mode = "Mode: Signals only"

    note = (
        f"Signals: {signals_count} · Planned: {planned_count} · "
        f"Orders placed: {orders_count} · {mode}. "
        "If zero, it simply means no valid setup at this moment."
    )

    return JSONResponse(
        {
            "status": "ok",
            "timestamp_utc": result.get("timestamp"),
            "signals_found": signals_count,
            "orders_planned": planned_count,
            "orders_placed": orders_count,
            "mode": mode,
            "note": note,
        }
    )


@app.get("/api/liquidity/recent", response_class=JSONResponse)
async def liquidity_recent():
    oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()
    enabled = os.getenv("LIQUIDITY_TRADING_ENABLED", "0").strip() == "1"
    mode = (
        f"Mode: Execution enabled ({oanda_env})"
        if (enabled and oanda_env in {"practice", "live"})
        else "Mode: Signals only"
    )

    if not RECENT_LIQUIDITY:
        return JSONResponse(
            {
                "mode": mode,
                "text": "No liquidity runs yet. Click 'Run Liquidity Tick'.",
            }
        )

    last = RECENT_LIQUIDITY[-1]
    ts = last.get("timestamp", "")
    sigs = last.get("signals", [])
    planned = last.get("planned_orders", [])
    orders = last.get("orders", [])

    lines = [
        f"Last run: {ts}",
        f"Signals: {len(sigs)} · Planned: {len(planned)} · Orders: {len(orders)}",
    ]

    if sigs:
        lines.append("")
        lines.append("Signals:")
        for s in sigs[:6]:
            lines.append(
                f"- {s.get('symbol')} {str(s.get('side')).upper()} "
                f"entry={s.get('entry'):.5f} SL={s.get('stop_loss'):.5f} "
                f"TP={s.get('take_profit'):.5f} RR={s.get('rr')}"
            )

    if orders:
        lines.append("")
        lines.append("Orders:")
        for o in orders[:6]:
            lines.append(f"- {o.get('symbol')} {str(o.get('side')).upper()} units={o.get('units')} (sent)")

    return JSONResponse({"mode": mode, "text": "\n".join(lines)})


# ----------------------------- Social Signals API --------------------------

@app.get("/api/social-signals", response_class=JSONResponse)
async def social_signals_endpoint():
    """Fetch latest social signals from the centralized Signal Engine."""
    forex = await social_signals.fetch_forex_signals()
    last_fetch = social_signals.get_cached_last_fetch()
    return JSONResponse({
        "status": "ok",
        "forex_signals": forex,
        "count": len(forex),
        "last_fetch": last_fetch.isoformat() if last_fetch else None,
        "source": social_signals.SIGNAL_ENGINE_URL,
    })


@app.get("/api/social-signals/{pair}", response_class=JSONResponse)
async def social_signal_for_pair(pair: str):
    """Get social sentiment for a specific forex pair (e.g. EURUSD or EUR_USD)."""
    # Ensure we have fresh data
    if not social_signals.get_cached_forex_signals():
        await social_signals.fetch_forex_signals()

    signal = social_signals.get_social_sentiment_for_pair(pair)
    if signal is None:
        return JSONResponse(
            status_code=404,
            content={"status": "not_found", "pair": pair, "message": "No social signal data for this pair"},
        )
    return JSONResponse({"status": "ok", "signal": signal})


# ----------------------------- ChaosFX API --------------------------------

@app.post("/api/chaosfx/tick", response_class=JSONResponse)
async def chaosfx_tick():
    try:
        engine = get_chaos_engine()
        summary = engine.run_once()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": str(e), "actions": 0},
        )
    return JSONResponse(
        {
            "status": "ok",
            "timestamp": summary.get("timestamp"),
            "equity": summary.get("equity"),
            "reason": summary.get("reason"),
            "actions": len(summary.get("actions", [])),
            "surge_mode": summary.get("surge_mode", False),
        }
    )


@app.get("/api/chaosfx/status", response_class=JSONResponse)
async def chaosfx_status():
    try:
        engine = get_chaos_engine()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "summary_text": f"ChaosFX error: {e}",
                "trades_text": "Engine not available.",
            },
        )
    last = engine.last_summary or {}
    trades = engine.recent_trades or []

    if not last:
        return JSONResponse(
            {
                "summary_text": "No runs recorded yet. Click 'Run ChaosFX Cycle'.",
                "trades_text": "No trades recorded yet.",
            }
        )

    summary_text = (
        f"Equity: {last.get('equity')} · Reason: {last.get('reason')} · "
        f"Actions: {len(last.get('actions', []))} · "
        f"Surge: {'ON' if last.get('surge_mode') else 'OFF'}"
    )

    if not trades:
        trades_text = "No trades recorded yet."
    else:
        lines = []
        for t in trades[-8:]:
            lines.append(
                f"- {t.get('pair')} {t.get('side')} units={t.get('units')} "
                f"SL={t.get('stop_loss')} TP={t.get('take_profit')} "
                f"conf={t.get('confidence')}"
            )
        trades_text = "\n".join(lines)

    return JSONResponse({"summary_text": summary_text, "trades_text": trades_text})
