from datetime import datetime
import threading
import time
from typing import Optional

from fastapi import FastAPI

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
            # We just log; FastAPI will keep running
            print(f"[FOREXBOT LOOP ERROR] {e}")
        time.sleep(settings.LOOP_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event():
    """
    Start background trading loop when the app boots (Render included).
    """
    global _background_thread
    if _background_thread is None or not _background_thread.is_alive():
        t = threading.Thread(target=_background_loop, daemon=True)
        _background_thread = t
        t.start()
        print("[FOREXBOT] Background loop started")


@app.on_event("shutdown")
def shutdown_event():
    """
    Stop background loop gracefully on shutdown.
    """
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
    """
    Manual trigger – still handy for testing.
    """
    return engine.run_once()


@app.get("/status")
def status():
    """
    JSON status for other services / debugging.
    """
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
    }


@app.get("/recent-runs")
def recent_runs(limit: int = 20):
    """
    JSON history for dashboard or manual inspection.
    """
    return {
        "count": len(engine.recent_runs[-limit:]),
        "runs": engine.recent_runs[-limit:],
    }


@app.get("/dashboard")
def dashboard():
    """
    Super simple HTML dashboard, ChaosEng style.
    (We keep it inline so you don't need extra files.)
    """
    last = engine.last_summary
    if not last:
        body = "<p>No runs yet. Background loop will populate this soon.</p>"
    else:
        body = f"""
        <p><strong>Last timestamp:</strong> {last["timestamp"]}</p>
        <p><strong>Last equity:</strong> {last["equity"]:.2f}</p>
        <p><strong>Last reason:</strong> {last["reason"]}</p>
        <p><strong>Actions in last run:</strong> {len(last.get("actions", []))}</p>
        """

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
            max-width: 600px;
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
        </style>
      </head>
      <body>
        <div class="card">
          <h1>ForexBot Dashboard</h1>
          <small>Auto-refreshes every 15s</small>
          {body}
        </div>
      </body>
    </html>
    """
    return html
