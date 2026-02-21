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

        - units: positive magnitude (we set sign from side)
        - side: 'long' or 'short'
        """
        instrument = self._instrument(symbol)

        u = abs(int(units))
        if side.lower() == "short":
            u = -u

        order_payload: Dict[str, Any] = {
            "order": {
                "units": str(u),
                "instrument": instrument,
                "timeInForce": "FOK",
                "type": "MARKET",
                "positionFill": "DEFAULT",
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
            return {"status": "rejected", "reason": reason, "raw": data}

        logger.info(
            "ORDER FILLED (%s) %s %s units=%d SL=%s TP=%s",
            self.env, symbol, side.upper(), u,
            f"{stop_loss:.5f}" if stop_loss is not None else "none",
            f"{take_profit:.5f}" if take_profit is not None else "none",
        )
        return {"status": "ok", "raw": data}


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

    <script>
      const liqBtn = document.getElementById("liq-btn");
      const liqStatus = document.getElementById("liq-status");
      const liqRecent = document.getElementById("liq-recent");
      const liqMode = document.getElementById("liq-mode");

      const cxBtn = document.getElementById("cx-btn");
      const cxStatus = document.getElementById("cx-status");
      const cxSummary = document.getElementById("cx-summary");
      const cxTrades = document.getElementById("cx-trades");

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
