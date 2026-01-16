from datetime import datetime
import threading
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from chaosfx.engine import ChaosEngineFX
from chaosfx.config import settings

app = FastAPI(title="ForexBot")

engine = ChaosEngineFX()

_background_thread: Optional[threading.Thread] = None
_background_running: bool = False


def _background_loop():
    """
    Background loop that runs engine.run_once() every LOOP_INTERVAL_SECONDS.
    """
    global _background_running
    _background_running = True
    while _background_running:
        try:
            engine.run_once()
        except Exception as e:
            print(f"[FOREXBOT LOOP ERROR] {e}")
        time.sleep(settings.LOOP_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event():
    global _background_thread
    if _background_thread is None or not _background_thread.is_alive():
        t = threading.Thread(target=_background_loop, daemon=True)
        _background_thread = t
        t.start()
        print("[FOREXBOT] Background loop started")


@app.on_event("shutdown")
def shutdown_event():
    global _background_running
    _background_running = False
    print("[FOREXBOT] Background loop stopping")


@app.get("/")
def root():
    return {
        "name": "ForexBot",
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/run-once")
def run_once():
    return engine.run_once()


@app.get("/status")
def status():
    last = engine.last_summary
    if not last:
        return {
            "status": "idle",
            "message": "No runs executed yet",
        }

    return {
        "status": "active",
        "last_timestamp": last["timestamp"],
        "last_equity": last["equity"],
        "last_actions": len(last.get("actions", [])),
        "last_reason": last["reason"],
        "surge_mode": last.get("surge_mode", False),
    }


@app.get("/recent-runs")
def recent_runs(limit: int = 20):
    return {
        "count": len(engine.recent_runs[-limit:]),
        "runs": engine.recent_runs[-limit:],
    }


@app.get("/recent-trades")
def recent_trades(limit: int = 20):
    trades = engine.recent_trades[-limit:]
    return {
        "count": len(trades),
        "trades": trades,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    last = engine.last_summary
    if not last:
        body_status = "<p>No runs yet. Background loop will populate this soon.</p>"
    else:
        body_status = f"""
        <p><strong>Last timestamp:</strong> {last["timestamp"]}</p>
        <p><strong>Last equity:</strong> {last["equity"]:.2f}</p>
        <p><strong>Last reason:</strong> {last["reason"]}</p>
        <p><strong>Surge mode:</strong> {last.get("surge_mode", False)}</p>
        <p><strong>Actions in last run:</strong> {len(last.get("actions", []))}</p>
        """

    # Recent trades table
    if engine.recent_trades:
        rows = ""
        for t in engine.recent_trades[-10:][::-1]:
            rows += f"""
            <tr>
              <td>{t["timestamp"]}</td>
              <td>{t["pair"]}</td>
              <td>{t["side"]}</td>
              <td>{t["units"]}</td>
              <td>{t["entry_price"]:.5f}</td>
              <td>{t["stop_loss"]:.5f}</td>
              <td>{t["take_profit"]:.5f}</td>
              <td>{t.get("volatility", 0):.6f}</td>
              <td>{t.get("confidence", 0):.2f}</td>
              <td>{t["reason"]}</td>
            </tr>
            """
        trades_html = f"""
        <h2 style="margin-top:24px;">Recent Trades</h2>
        <div style="max-height:300px; overflow-y:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:0.85rem;">
          <thead>
            <tr>
              <th align="left">Time (UTC)</th>
              <th align="left">Pair</th>
              <th align="left">Side</th>
              <th align="right">Units</th>
              <th align="right">Entry</th>
              <th align="right">SL</th>
              <th align="right">TP</th>
              <th align="right">Vol</th>
              <th align="right">Conf</th>
              <th align="left">Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
        </div>
        """
    else:
        trades_html = "<p>No trades yet.</p>"

    html = f"""
    <html>
      <head>
        <title>ForexBot Dashboard</title>
        <meta http-equiv="refresh" content="15">
        <style>
          body {{
            font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            background: #050816;
            color: #f5f5f5;
            padding: 20px;
          }}
          .card {{
            max-width: 900px;
            margin: 0 auto;
            background: #111827;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.4);
          }}
          h1 {{
            margin-top: 0;
          }}
          small {{
            color: #9ca3af;
          }}
          th, td {{
            padding: 4px 6px;
            border-bottom: 1px solid #1f2937;
          }}
          th {{
            position: sticky;
            top: 0;
            background: #111827;
          }}
        </style>
      </head>
      <body>
        <div class="card">
          <h1>ForexBot Dashboard</h1>
          <small>Auto-refreshes every 15s</small>
          {body_status}
          {trades_html}
        </div>
      </body>
    </html>
    """
    return html
