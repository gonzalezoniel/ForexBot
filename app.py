from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from forexbot_core import run_tick, BrokerClient, Quote

# ChaosFX
from chaosfx.engine import ChaosEngineFX


app = FastAPI(
    title="ForexBot – Liquidity + ChaosFX",
    version="1.2.0",
    description=(
        "Forex bot combining: "
        "Liquidity Sweep (EURGBP/XAUUSD/GBPCAD HTF bias + 5M sweeps) + "
        "ChaosEngine-FX (volatility/confidence execution)."
    ),
)


# ---------------------------------------------------------------------------
# Broker implementations (Liquidity engine uses this)
# ---------------------------------------------------------------------------

class DummyBroker(BrokerClient):
    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        return []

    def get_quote(self, symbol: str) -> Quote:
        return Quote(bid=0.0, ask=0.0)

    def place_order(
        self,
        symbol: str,
        side: str,
        units: int,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        print(
            f"[DummyBroker] place_order called: {symbol} {side} units={units} "
            f"entry={entry} SL={stop_loss} TP={take_profit}"
        )
        return {"status": "dummy", "detail": "No real broker is configured."}


class OandaBroker(BrokerClient):
    """
    OANDA v20 REST broker for Liquidity engine (data + market orders).

    Env:
      - OANDA_API_KEY
      - OANDA_ACCOUNT_ID
      - OANDA_ENV = practice|live

    SAFETY:
      - Liquidity orders only allowed automatically on practice
      - live requires LIQUIDITY_ALLOW_LIVE=1
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

        self.env = env
        self.account_id = account_id

        if env == "live":
            self.base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        self.session.headers.update({"Content-Type": "application/json"})

    def _instrument(self, symbol: str) -> str:
        if symbol not in self.SYMBOL_MAP:
            raise ValueError(f"Unsupported symbol for OANDA: {symbol}")
        return self.SYMBOL_MAP[symbol]

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        instrument = self._instrument(symbol)
        granularity = self.TF_MAP.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        url = f"{self.base_url}/instruments/{instrument}/candles"
        params = {"granularity": granularity, "count": limit, "price": "M"}

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

            candles.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": cl})

        return candles

    def get_quote(self, symbol: str) -> Quote:
        instrument = self._instrument(symbol)
        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        params = {"instruments": instrument}

        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[OandaBroker] get_quote error for {symbol}: {e}")
            return Quote(bid=0.0, ask=0.0)

        data = resp.json()
        prices = data.get("prices", [])
        if not prices:
            return Quote(bid=0.0, ask=0.0)

        p = prices[0]
        try:
            bid = float(p["bids"][0]["price"])
            ask = float(p["asks"][0]["price"])
        except Exception:
            return Quote(bid=0.0, ask=0.0)

        return Quote(bid=bid, ask=ask)

    def place_order(
        self,
        symbol: str,
        side: str,
        units: int,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        # SAFETY: block live unless explicitly allowed
        allow_live = os.getenv("LIQUIDITY_ALLOW_LIVE", "0").strip() == "1"
        if self.env == "live" and not allow_live:
            return {"status": "blocked", "detail": "Liquidity live trading is blocked. Set LIQUIDITY_ALLOW_LIVE=1 to allow."}

        instrument = self._instrument(symbol)
        url = f"{self.base_url}/accounts/{self.account_id}/orders"

        # OANDA market order with attached SL/TP
        payload: Dict[str, Any] = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{stop_loss:.5f}"},
                "takeProfitOnFill": {"price": f"{take_profit:.5f}"},
            }
        }

        try:
            resp = self.session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "ok", "oanda": data}
        except Exception as e:
            print(f"[OandaBroker] place_order error: {e}")
            try:
                return {"status": "error", "detail": str(e), "body": resp.text}  # type: ignore
            except Exception:
                return {"status": "error", "detail": str(e)}


def get_liquidity_broker() -> BrokerClient:
    api_key = os.getenv("OANDA_API_KEY", "").strip()
    account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()

    if api_key and account_id:
        try:
            b = OandaBroker()
            print("[LiquidityBroker] Using OandaBroker")
            return b
        except Exception as e:
            print(f"[LiquidityBroker] Failed to init OandaBroker, falling back to DummyBroker: {e}")

    print("[LiquidityBroker] Using DummyBroker")
    return DummyBroker()


# ---------------------------------------------------------------------------
# Engines (ChaosFX object is long-lived)
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


def _push_recent(buf: List[Dict[str, Any]], item: Dict[str, Any], max_len: int = 25) -> None:
    buf.append(item)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
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

        <div class="small">Docs: <a href="/docs" target="_blank">/docs</a> · Health: <a href="/health" target="_blank">/health</a></ Fletcher >
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
    If OANDA_ENV=practice and LIQUIDITY_TRADING_ENABLED=1, it will place paper trades.
    """
    broker = get_liquidity_broker()

    # execution gates
    oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()
    enabled = os.getenv("LIQUIDITY_TRADING_ENABLED", "0").strip() == "1"

    execute_trades = bool(enabled and oanda_env == "practice")

    # sizing controls
    balance = float(os.getenv("LIQUIDITY_PAPER_BALANCE", "10000"))
    risk_pct = float(os.getenv("LIQUIDITY_RISK_PCT", "0.5"))
    max_units_fx = int(os.getenv("LIQUIDITY_MAX_UNITS_FX", "2000"))
    max_units_xau = int(os.getenv("LIQUIDITY_MAX_UNITS_XAU", "20"))

    result = run_tick(
        broker_client=broker,
        balance=balance,
        risk_pct_per_trade=risk_pct,
        execute_trades=execute_trades,
        max_units_fx=max_units_fx,
        max_units_xau=max_units_xau,
    )

    # store for dashboard
    _push_recent(RECENT_LIQUIDITY, result, max_len=25)

    # store trades separately
    for o in result.get("orders", []):
        _push_recent(RECENT_LIQUIDITY_TRADES, o, max_len=25)

    signals_count = len(result.get("signals", []))
    orders_count = len(result.get("orders", []))

    mode = "Paper (OANDA practice only)" if execute_trades else "Signals only"

    note = (
        f"Signals: {signals_count} · Orders placed: {orders_count} · Mode: {mode}. "
        "If zero, it simply means no valid setup at this moment."
    )

    return JSONResponse(
        {
            "status": "ok",
            "timestamp_utc": result.get("timestamp"),
            "signals_found": signals_count,
            "orders_placed": orders_count,
            "mode": mode,
            "note": note,
        }
    )


@app.get("/api/liquidity/recent", response_class=JSONResponse)
async def liquidity_recent():
    oanda_env = os.getenv("OANDA_ENV", "practice").strip().lower()
    enabled = os.getenv("LIQUIDITY_TRADING_ENABLED", "0").strip() == "1"
    mode = "Mode: Paper (OANDA practice only)" if (enabled and oanda_env == "practice") else "Mode: Signals only"

    if not RECENT_LIQUIDITY:
        return JSONResponse({"mode": mode, "text": "No liquidity runs yet. Click 'Run Liquidity Tick'."})

    last = RECENT_LIQUIDITY[-1]
    ts = last.get("timestamp", "")
    sigs = last.get("signals", [])
    orders = last.get("orders", [])

    lines = [f"Last run: {ts}", f"Signals: {len(sigs)} · Orders: {len(orders)}"]
    if sigs:
        lines.append("")
        lines.append("Signals:")
        for s in sigs[:6]:
            lines.append(
                f"- {s.get('symbol')} {str(s.get('side')).upper()} entry={s.get('entry'):.5f} "
                f"SL={s.get('stop_loss'):.5f} TP={s.get('take_profit'):.5f} RR={s.get('rr')}"
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
    engine = get_chaos_engine()
    summary = engine.run_once()
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
    engine = get_chaos_engine()
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
        f"Actions: {len(last.get('actions', []))} · Surge: {'ON' if last.get('surge_mode') else 'OFF'}"
    )

    if not trades:
        trades_text = "No trades recorded yet."
    else:
        lines = []
        for t in trades[-8:]:
            lines.append(
                f"- {t.get('pair')} {t.get('side')} units={t.get('units')} "
                f"SL={t.get('stop_loss')} TP={t.get('take_profit')} conf={t.get('confidence')}"
            )
        trades_text = "\n".join(lines)

    return JSONResponse({"summary_text": summary_text, "trades_text": trades_text})
