from datetime import datetime
from fastapi import FastAPI

from chaosfx.engine import ChaosEngineFX

app = FastAPI(title="Forexbot")

engine = ChaosEngineFX()


@app.get("/")
def root():
    return {
        "name": "Forexbot",
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
    }


@app.get("/recent-runs")
def recent_runs(limit: int = 20):
    return {
        "count": len(engine.recent_runs[-limit:]),
        "runs": engine.recent_runs[-limit:],
    }
