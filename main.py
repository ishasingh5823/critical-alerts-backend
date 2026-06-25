"""Critical Alert Dashboard — FastAPI entry point.

Runs on port 9002 (changed from 8002 because user had a conflict).
The scheduler kicks a Supermetrics pull + alert evaluation every
POLL_INTERVAL_MIN minutes (default 60).
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from fastapi import BackgroundTasks, FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api.routes import alerts as alerts_routes  # noqa: E402
from api.routes import campaigns as campaigns_routes  # noqa: E402
from api.routes import health as health_routes  # noqa: E402
from core import cache  # noqa: E402
from core.poller import run_cycle  # noqa: E402


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("critical-alerts")

_scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache.init_db()
    interval_min = int(os.environ.get("POLL_INTERVAL_MIN", "60"))
    _scheduler.add_job(
        _scheduled_cycle,
        "interval",
        minutes=interval_min,
        id="sm_poll",
        next_run_time=None,  # don't auto-run at startup
    )
    _scheduler.start()
    log.info("Scheduler started, polling Supermetrics every %d min", interval_min)
    yield
    _scheduler.shutdown()


def _scheduled_cycle() -> None:
    try:
        summary = run_cycle()
        log.info("Sync cycle finished: %d clients, %d alerts fired",
                 len(summary["clients"]), summary["alerts_fired"])
    except Exception:
        log.exception("Sync cycle failed")


app = FastAPI(title="Critical Alert Dashboard", version="0.1.0",
              lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3002",
        "http://localhost:4002",   # new frontend port
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"service": "critical-alert-dashboard", "version": "0.1.0"}


for module in (health_routes, alerts_routes, campaigns_routes):
    app.include_router(module.router, prefix="/api")


_sync_running = False


def _run_sync_background() -> None:
    """Run a sync cycle in a worker thread. Sets _sync_running guard so
    overlapping clicks don't stack multiple cycles."""
    global _sync_running
    if _sync_running:
        log.info("Sync already running; ignoring overlap")
        return
    _sync_running = True
    try:
        summary = run_cycle()
        log.info("Manual sync done: %d clients, %d alerts",
                 len(summary["clients"]), summary["alerts_fired"])
    except Exception:
        log.exception("Manual sync failed")
    finally:
        _sync_running = False


@app.post("/api/sync/now")
async def trigger_sync(background_tasks: BackgroundTasks):
    """Kick a sync cycle without blocking the HTTP response.
    A pull of Kenvue+Monster takes ~2-5 minutes (4 Supermetrics queries
    serialized). Returning immediately keeps the UI responsive — the
    HealthBadge polls /api/health every 30s and will see the new
    last_sync timestamp when the cycle finishes."""
    if _sync_running:
        return {"status": "already_running",
                "message": "Sync already in progress"}
    background_tasks.add_task(_run_sync_background)
    return {"status": "queued", "message": "Sync started in background"}


@app.get("/api/sync/status")
async def sync_status():
    """Tell the UI whether a sync is currently in flight."""
    return {"running": _sync_running}
