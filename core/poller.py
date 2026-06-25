"""Supermetrics → cache pull cycle.

**Batched.** With 30+ Meta ad accounts, naive per-account pulls = 60+
queries per cycle. The batcher groups accounts into clusters of N
(default 8) and issues one Supermetrics request per cluster — verified
~5× faster, same data quality, response rows routed back to the right
client by Account ID.

Blocked accounts (the ones Supermetrics rejects with "not a prioritised
account") are auto-detected via per-account fallback when a batch fails,
then skipped for the rest of the run. The blocked list survives across
cycles via the process-level singleton below; it resets when the
backend restarts (clean slate to retry).

Designed to be called both from a scheduled cron (APScheduler) and
manually from CLI for testing.
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Set

from core import cache
from core.alerts import engine as alert_engine
from core.batcher import run_batched_cycle


log = logging.getLogger(__name__)

# Process-level set of accounts known to fail with "not prioritised".
# Populated as we discover them; survives across sync cycles until the
# backend restarts. Restart = clean slate = retry everything.
_blocked_accounts: Set[str] = set()


def run_cycle(client_id: Optional[str] = None,
             dry_run: bool = False) -> Dict:
    """Pull every query for every client (or just one). Then run the
    alert engine. Returns a summary dict."""
    cache.init_db()

    batch_summary = run_batched_cycle(
        client_id=client_id,
        blocked=_blocked_accounts,
    )

    fired = []
    if not dry_run:
        fired = alert_engine.run(client_id=client_id)

    return {
        "started_at":      batch_summary["started_at"],
        "finished_at":     batch_summary["finished_at"],
        "batches":         batch_summary["batches"],
        "blocked_total":   batch_summary["blocked_total"],
        "alerts_fired":    len(fired),
        # Legacy `clients` field shape for any caller that still reads it
        "clients":         _summarize_per_client(batch_summary["batches"]),
    }


def _summarize_per_client(batches: list) -> list:
    """Roll the batch results up into a per-client view for the existing
    /api/sync/now response shape."""
    per_client: Dict[str, Dict] = {}
    for b in batches:
        # Batched results have ok/rows; fallback results have ok/errors
        if b.get("status") == "ok":
            # Successful batch — we don't have per-client breakdown here
            # without re-parsing rows, so just note the rollup.
            continue
        # partial / error: include the per-account info if available
        for acc in b.get("newly_blocked", []):
            per_client.setdefault(acc, {"queries": []})["queries"].append(
                {"name": f"{b['type']}", "status": "blocked",
                 "reason": "Supermetrics prioritised-account limit"})
    return [{"client_id": k, **v} for k, v in per_client.items()]


def get_blocked_accounts() -> Set[str]:
    """Snapshot of accounts marked blocked since process start. Useful
    for surfacing in /api/health."""
    return set(_blocked_accounts)


def reset_blocked() -> None:
    """Clear the blocked-accounts cache. Call this after the user fixes
    prioritisation in Supermetrics Hub and wants a fresh retry."""
    _blocked_accounts.clear()
