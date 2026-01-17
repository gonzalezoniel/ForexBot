from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from forexbot_core import run_tick, BrokerClient, Quote


app = FastAPI(
    title="ForexBot – Liquidity Sweep Strategy",
    version="1.0.0",
    description=(
        "Forex day trading engine focused on EURGBP, XAUUSD, GBPCAD using "
        "4H/1H bias + 5M liquidity sweeps and BOS confirmation."
    ),
)


# ---------------------------------------------------------------------------
# Dummy broker implementation (SAFE: no real orders, no real data)
# ---------------------------------------------------------------------------


class DummyBroker(BrokerClient):
    """
    Placeholder broker so the app runs safely.

    - get_ohlc returns an empty list -> strategy finds no signals
    - get_quote returns 0/0 -> spread 0 (not used because no candles)
    - place_order only prints to console

    When you're ready to go live, replace this with a real broker client.
    """

    def get_ohlc(self, symbol: str, timeframe: str, limit: int) -> List[dict]:
        # TODO: Replace with real broker candles.
        return []

    def get_quote(self, symbol: str) -> Quote:
        # TODO: Replace with real broker quote.
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


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>ForexBot – Liquidity Sweep Dashboard</title>
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
    .card {
      background: rgba(15, 23, 42, 0.95);
      border-radius: 18px;
      padding: 24px 28px;
      max-width: 520px;
      width: 100%;
      box-shadow: 0 18px 45px rgba(0, 0, 0, 0.6);
      border: 1px solid rgba(148, 163, 184, 0.3);
    }
    h1 {
      margin: 0 0 12px;
      font-size: 1.5rem;
      color: #f9fafb;
    }
    .sub {
      margin: 0 0 16px;
      font-size: 0.9rem;
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
      margin-bottom: 16px;
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
      gap: 12px;
      margin-bottom: 16px;
    }
    .pill {
      font-size: 0.8rem;
      padding: 4px 8px;
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
      margin-bottom: 4px;
    }
    .value {
      font-size: 0.85rem;
      color: #e5e7eb;
    }
    button {
      margin-top: 10px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 14px;
      border-radius: 999px;
      border: none;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: white;
      font-size: 0.85rem;
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
    .status-line {
      font-size: 0.8rem;
      color: #9ca3af;
      margin-top: 8px;
      min-height: 1.5em;
    }
    a {
      color: #a5b4fc;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .footer {
      margin-top: 18px;
      font-size: 0.75rem;
      color: #6b7280;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">
      <span class="badge-dot"></span>
      <span>Live engine</span>
    </div>
    <h1>ForexBot – Liquidity Sweep</h1>
    <p class="sub">
      EURGBP · XAUUSD · GBPCAD<br />
      4H / 1H trend bias · 5M liquidity sweeps · BOS confirmations.
    </p>

    <div class="row">
      <div class="pill">Session: London / NY</div>
      <div class="pill">RR: 1:3 – 1:5</div>
      <div class="pill">Mode: Safe (DummyBroker)</div>
    </div>

    <div class="label">Controls</div>
    <button id="tick-btn">
      Run Tick
      <span style="font-size: 0.9em;">⚡</span>
    </button>
    <div id="status" class="status-line"></div>

    <div class="footer">
      <div>Docs: <a href="/docs" target="_blank">/docs</a></div>
      <div>Health: <a href="/health" target="_blank">/health</a></div>
    </div>
  </div>

  <script>
    const btn = document.getElementById("tick-btn");
    const statusEl = document.getElementById("status");

    btn.addEventListener("click", async () => {
      statusEl.textContent = "Running tick...";
      btn.disabled = true;

      try {
        const res = await fetch("/api/tick", { method: "POST" });
        const data = await res.json();
        statusEl.textContent = data.note || "Tick completed.";
      } catch (err) {
        console.error(err);
        statusEl.textContent = "Error running tick. Check logs.";
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root():
    """
    HTML dashboard for the bot.
    """
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health", response_class=JSONResponse)
async def health():
    """
    Simple health endpoint for uptime checks.
    """
    return JSONResponse({"status": "healthy"})


@app.post("/api/tick", response_class=JSONResponse)
async def run_strategy_tick():
    """
    Manually trigger one strategy evaluation tick.

    For now:
      - Uses DummyBroker (no live trading)
      - Logs signals, if any, to stdout (Render logs)
    """
    now = datetime.now(timezone.utc)

    broker = DummyBroker()
    fake_balance = 10_000.0  # used only for position size math

    run_tick(
        broker_client=broker,
        balance=fake_balance,
        risk_pct_per_trade=0.5,
    )

    return JSONResponse(
        {
            "status": "tick_completed",
            "timestamp_utc": now.isoformat(),
            "note": (
                "Tick executed with DummyBroker. "
                "No real orders were placed. Check logs for signals."
            ),
        }
    )
