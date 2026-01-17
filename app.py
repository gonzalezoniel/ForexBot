from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from forexbot_core import run_tick, BrokerClient, Quote
from chaosfx.engine import ChaosEngineFX


app = FastAPI(
    title="ForexBot – Liquidity + ChaosFX",
    version="2.0.0",
    description=(
        "Combined dashboard for Liquidity Sweep strategy (EURGBP, XAUUSD, GBPCAD) "
        "and ChaosEngine-FX volatility engine."
    ),
)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

# In-memory store for liquidity sweep signals
RECENT_LIQ_SIGNALS: List[dict] = []
MAX_LIQ_SIGNALS = 50

# Single ChaosFX engine instance
CHAOS_ENGINE = ChaosEngineFX()


# ---------------------------------------------------------------------------
# Broker implementations for Liquidity Strategy
# ---------------------------------------------------------------------------


class DummyBroker(BrokerClient):
    """
    Safe broker used when OANDA credentials are not configured or fail.

    - get_ohlc returns an empty list -> strategy finds no signals
    - get_quote returns 0/0 -> spread 0 (not used because no candles)
    - place_order only prints to console
    """

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        return []

    def get_quote(self, symbol: str) -> Quote:
        return Quote(bid=0.0, ask=0.0)

    def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        print(
            f"[DummyBroker] place_order called: "
            f"{symbol} {side} size={size} entry={entry} "
            f"SL={stop_loss} TP={take_profit}"
        )
        return {"status": "dummy", "detail": "No real broker is configured."}


class OandaBroker(BrokerClient):
    """
    OANDA v20 REST broker implementation for data only (no live orders yet).

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
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

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
            # skip incomplete candles
            if not c.get("complete", False):
                continue
            t = c.get("time")
            if not t:
                continue
            # OANDA time format: '2025-01-16T12:00:00.000000000Z'
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
        size: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
    ):
        """
        Placeholder: orders are NOT sent yet for liquidity strategy.
        ChaosFX uses its own OandaClient for trading.
        """
        print(
            f"[OandaBroker] place_order (NOT SENT): "
            f"{symbol} {side} size={size} entry={entry} "
            f"SL={stop_loss} TP={take_profit}"
        )
        return {"status": "not_sent", "detail": "Trading not enabled for this engine."}


def get_broker() -> BrokerClient:
    """
    Chooses broker implementation for the liquidity strategy:
      - If OANDA env vars are set -> OandaBroker
      - Else -> DummyBroker
    """
    api_key = os.getenv("OANDA_API_KEY", "").strip()
    account_id = os.getenv("OANDA_ACCOUNT_ID", "").strip()

    if api_key and account_id:
        try:
            broker = OandaBroker()
            print("[Broker] Using OandaBroker (data live, trading disabled for liquidity engine).")
            return broker
        except Exception as e:
            print(f"[Broker] Failed to init OandaBroker, falling back to DummyBroker: {e}")

    print("[Broker] Using DummyBroker (no real data).")
    return DummyBroker()


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>ForexBot – Liquidity + ChaosFX Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #050816;
      color: #e5e7eb;
      margin: 0;
      padding: 24px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 18px;
      max-width: 1100px;
      margin: 0 auto;
    }
    @media (min-width: 900px) {
      .grid {
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      }
    }
    .card {
      background: rgba(15, 23, 42, 0.95);
      border-radius: 18px;
      padding: 20px 22px;
      box-shadow: 0 18px 45px rgba(0, 0, 0, 0.6);
      border: 1px solid rgba(148, 163, 184, 0.3);
    }
    h1, h2 {
      margin: 0 0 8px;
      font-size: 1.25rem;
      color: #f9fafb;
    }
    .title-main {
      font-size: 1.5rem;
      margin-bottom: 4px;
    }
    .sub {
      margin: 0 0 12px;
      font-size: 0.88rem;
      color: #9ca3af;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.75rem;
      padding: 4px 9px;
      border-radius: 999px;
      background: rgba(16, 185, 129, 0.15);
      color: #6ee7b7;
      border: 1px solid rgba(16, 185, 129, 0.35);
      margin-bottom: 10px;
    }
    .badge-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #22c55e;
      box-shadow: 0 0 8px rgba(34, 197, 94, 0.6);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }
    .pill {
      font-size: 0.75rem;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(30, 64, 175, 0.2);
      color: #bfdbfe;
      border: 1px solid rgba(59, 130, 246, 0.4);
    }
    .label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #6b7280;
      margin-top: 8px;
      margin-bottom: 4px;
    }
    .status-line {
      font-size: 0.8rem;
      color: #9ca3af;
      margin-top: 6px;
      min-height: 1.4em;
    }
    button {
      margin-top: 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 13px;
      border-radius: 999px;
      border: none;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: white;
      font-size: 0.8rem;
      cursor: pointer;
      transition: transform 0.08s ease, box-shadow 0.08s ease, opacity 0.15s ease;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 10px 25px rgba(79, 70, 229, 0.4);
    }
    button:active {
      transform: translateY(0);
      box-shadow: none;
      opacity: 0.9;
    }
    .signals-list, .trades-list {
      max-height: 220px;
      overflow-y: auto;
      margin-top: 6px;
      padding-right: 4px;
      font-size: 0.78rem;
      border-top: 1px solid rgba(31, 41, 55, 0.7);
      padding-top: 6px;
    }
    .row-item {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 0;
      border-bottom: 1px solid rgba(31, 41, 55, 0.7);
    }
    .row-main {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .sym {
      font-weight: 600;
      color: #e5e7eb;
    }
    .meta {
      color: #9ca3af;
    }
    .tag {
      font-weight: 500;
      color: #a5b4fc;
      white-space: nowrap;
    }
    .empty {
      color: #6b7280;
      font-size: 0.78rem;
      padding-top: 4px;
    }
    .footer {
      margin-top: 10px;
      font-size: 0.75rem;
      color: #6b7280;
    }
    a {
      color: #a5b4fc;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .top-header {
      max-width: 1100px;
      margin: 0 auto 12px auto;
    }
  </style>
</head>
<body>
  <div class="top-header">
    <div class="badge">
      <span class="badge-dot"></span>
      <span>Engines online</span>
    </div>
    <h1 class="title-main">ForexBot – Liquidity + ChaosFX</h1>
    <p class="sub">
      Left: Liquidity Sweep (EURGBP · XAUUSD · GBPCAD · 4H/1H → 5M sweeps + BOS)<br />
      Right: ChaosEngine-FX (multi-pair volatility & confidence-based execution).
    </p>
  </div>

  <div class="grid">
    <!-- Liquidity Sweep Panel -->
    <div class="card">
      <h2>Liquidity Sweep Engine</h2>
      <p class="sub">
        HTF trend bias from 4H / 1H. Entries on 5M liquidity sweeps with BOS, RR 1:3–1:5.
      </p>

      <div class="row">
        <div class="pill">Pairs: EURGBP · XAUUSD · GBPCAD</div>
        <div class="pill">Mode: Signals only</div>
      </div>

      <div class="label">Controls</div>
      <button id="liq-btn">Run Liquidity Tick ⚡</button>
      <div id="liq-status" class="status-line"></div>

      <div class="label">Recent Liquidity Signals</div>
      <div id="liq-list" class="signals-list">
        <div class="empty">No signals yet. Run a tick to evaluate the market.</div>
      </div>

      <div class="footer">
        Docs: <a href="/docs" target="_blank">/docs</a> · Health: <a href="/health" target="_blank">/health</a>
      </div>
    </div>

    <!-- ChaosFX Panel -->
    <div class="card">
      <h2>ChaosEngine-FX</h2>
      <p class="sub">
        Volatility-ranked, confidence-filtered entries via ChaosFX strategy and risk engine.
      </p>

      <div class="row">
        <div class="pill">Pairs: settings.FOREX_PAIRS</div>
        <div class="pill">Mode: Live (OANDA practice/live)</div>
      </div>

      <div class="label">Controls</div>
      <button id="chaos-btn">Run ChaosFX Cycle ⚡</button>
      <div id="chaos-status" class="status-line"></div>

      <div class="label">Last Summary</div>
      <div id="chaos-summary" class="status-line"></div>

      <div class="label">Recent ChaosFX Trades</div>
      <div id="chaos-trades" class="trades-list">
        <div class="empty">No trades recorded yet.</div>
      </div>
    </div>
  </div>

  <script>
    const liqBtn = document.getElementById("liq-btn");
    const chaosBtn = document.getElementById("chaos-btn");
    const liqStatus = document.getElementById("liq-status");
    const chaosStatus = document.getElementById("chaos-status");
    const liqList = document.getElementById("liq-list");
    const chaosSummary = document.getElementById("chaos-summary");
    const chaosTrades = document.getElementById("chaos-trades");

    async function loadLiquiditySignals() {
      try {
        const res = await fetch("/api/liquidity/signals");
        const data = await res.json();
        const signals = data.signals || [];

        liqList.innerHTML = "";
        if (!signals.length) {
          const empty = document.createElement("div");
          empty.className = "empty";
          empty.textContent = "No signals yet. Run a tick to evaluate the market.";
          liqList.appendChild(empty);
          return;
        }

        for (const s of signals) {
          const row = document.createElement("div");
          row.className = "row-item";

          const main = document.createElement("div");
          main.className = "row-main";

          const sym = document.createElement("div");
          sym.className = "sym";
          sym.textContent = `${s.symbol} · ${s.side.toUpperCase()}`;

          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = `${s.time} · RR ${s.rr.toFixed(1)} · entry ${s.entry.toFixed(5)}`;

          main.appendChild(sym);
          main.appendChild(meta);

          const tag = document.createElement("div");
          tag.className = "tag";
          tag.textContent = s.comment || "";

          row.appendChild(main);
          row.appendChild(tag);

          liqList.appendChild(row);
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function loadChaosfxStatus() {
      try {
        const res = await fetch("/api/chaosfx/status");
        const data = await res.json();

        const last = data.last_summary;
        const trades = data.recent_trades || [];

        // Summary
        if (!last) {
          chaosSummary.textContent = "No runs yet.";
        } else {
          const actionsCount = (last.actions || []).length;
          chaosSummary.textContent =
            `Equity: ${Number(last.equity || 0).toFixed(2)} · ` +
            `Reason: ${last.reason || "n/a"} · ` +
            `Actions: ${actionsCount} · ` +
            `Surge: ${last.surge_mode ? "ON" : "OFF"}`;
        }

        // Trades
        chaosTrades.innerHTML = "";
        if (!trades.length) {
          const empty = document.createElement("div");
          empty.className = "empty";
          empty.textContent = "No trades recorded yet.";
          chaosTrades.appendChild(empty);
          return;
        }

        for (const t of trades.slice().reverse()) {
          const row = document.createElement("div");
          row.className = "row-item";

          const main = document.createElement("div");
          main.className = "row-main";

          const sym = document.createElement("div");
          sym.className = "sym";
          sym.textContent = `${t.pair} · ${t.side}`;

          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent =
            `${t.timestamp || ""} · vol ${Number(t.volatility || 0).toFixed(4)} ` +
            `· conf ${Number(t.confidence || 0).toFixed(2)} ` +
            `· entry ${Number(t.entry_price || 0).toFixed(5)}`;

          main.appendChild(sym);
          main.appendChild(meta);

          const tag = document.createElement("div");
          tag.className = "tag";
          tag.textContent = t.reason || "";

          row.appendChild(main);
          row.appendChild(tag);
          chaosTrades.appendChild(row);
        }
      } catch (err) {
        console.error(err);
      }
    }

    liqBtn.addEventListener("click", async () => {
      liqStatus.textContent = "Running liquidity tick...";
      liqBtn.disabled = true;
      try {
        const res = await fetch("/api/liquidity/tick", { method: "POST" });
        const data = await res.json();
        liqStatus.textContent =
          `Signals: ${data.signals_found} · ` +
          (data.note || "Tick completed.");
        await loadLiquiditySignals();
      } catch (err) {
        console.error(err);
        liqStatus.textContent = "Error running tick. Check logs.";
      } finally {
        liqBtn.disabled = false;
      }
    });

    chaosBtn.addEventListener("click", async () => {
      chaosStatus.textContent = "Running ChaosFX cycle...";
      chaosBtn.disabled = true;
      try {
        const res = await fetch("/api/chaosfx/run_once", { method: "POST" });
        const data = await res.json();
        chaosStatus.textContent =
          `Reason: ${data.reason || "completed"} · ` +
          `Actions: ${(data.actions || []).length}`;
        await loadChaosfxStatus();
      } catch (err) {
        console.error(err);
        chaosStatus.textContent = "Error running ChaosFX. Check logs.";
      } finally {
        chaosBtn.disabled = false;
      }
    });

    // Initial load
    loadLiquiditySignals();
    loadChaosfxStatus();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root():
    """Combined HTML dashboard for Liquidity Sweep + ChaosFX."""
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health", response_class=JSONResponse)
async def health():
    """Simple health endpoint for uptime checks."""
    return JSONResponse({"status": "healthy"})


# ------------------- Liquidity Sweep endpoints -----------------------------


@app.post("/api/liquidity/tick", response_class=JSONResponse)
async def run_liquidity_tick():
    """
    Manually trigger one liquidity strategy evaluation tick.

    Uses:
      - OandaBroker if OANDA env vars are set & valid
      - DummyBroker otherwise

    Trading is still disabled for this engine; only data is used and signals are logged.
    """
    now = datetime.now(timezone.utc)

    broker = get_broker()
    fake_balance = 10_000.0  # used only for position size math in logs

    signals = run_tick(
        broker_client=broker,
        balance=fake_balance,
        risk_pct_per_trade=0.5,
    )

    # Store in memory
    for sig in signals:
        RECENT_LIQ_SIGNALS.append(
            {
                "time": now.strftime("%Y-%m-%d %H:%M"),
                "symbol": sig.symbol,
                "side": sig.side,
                "entry": float(sig.entry),
                "stop_loss": float(sig.stop_loss),
                "take_profit": float(sig.take_profit),
                "rr": float(sig.rr),
                "comment": sig.comment,
            }
        )

    if len(RECENT_LIQ_SIGNALS) > MAX_LIQ_SIGNALS:
        del RECENT_LIQ_SIGNALS[:-MAX_LIQ_SIGNALS]

    return JSONResponse(
        {
            "status": "tick_completed",
            "timestamp_utc": now.isoformat(),
            "signals_found": len(signals),
            "note": (
                "Liquidity tick executed. Check logs and /api/liquidity/signals for details. "
                "If OANDA is configured, live market data was used. "
                "Trading remains disabled for this engine."
            ),
        }
    )


@app.get("/api/liquidity/signals", response_class=JSONResponse)
async def get_liquidity_signals():
    """
    Return recent liquidity sweep signals in reverse-chronological order.
    """
    return JSONResponse({"signals": list(reversed(RECENT_LIQ_SIGNALS))})


# Backwards-compatible alias for old /api/tick
@app.post("/api/tick", response_class=JSONResponse)
async def legacy_run_strategy_tick():
    """Alias to /api/liquidity/tick for backward compatibility."""
    return await run_liquidity_tick()


# ------------------- ChaosFX endpoints -------------------------------------


@app.post("/api/chaosfx/run_once", response_class=JSONResponse)
async def run_chaosfx_once():
    """
    Run a single ChaosEngine-FX cycle.

    NOTE:
      - ChaosFX uses its own OandaClient + settings from chaosfx.config.
      - It may place real orders on your OANDA account depending on settings.
    """
    summary = CHAOS_ENGINE.run_once()
    return JSONResponse(summary)


@app.get("/api/chaosfx/status", response_class=JSONResponse)
async def chaosfx_status():
    """
    Returns ChaosFX current status, including:
      - last_summary
      - recent_runs
      - recent_trades
    """
    return JSONResponse(
        {
            "last_summary": CHAOS_ENGINE.last_summary,
            "recent_runs": CHAOS_ENGINE.recent_runs,
            "recent_trades": CHAOS_ENGINE.recent_trades,
        }
    )
