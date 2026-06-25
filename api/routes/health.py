"""GET /api/health — sync freshness + per-client status + blocked accounts."""

from typing import Optional

from fastapi import APIRouter, Query

from clients import list_clients
from core import cache
from core.poller import get_blocked_accounts, reset_blocked

router = APIRouter()


@router.get("/health")
async def health(client: Optional[str] = Query(None)):
    """Per-client last-sync info + globally-blocked accounts. The dashboard
    uses this to surface 'data is stale' warnings + the
    'X accounts blocked by Supermetrics' hint."""
    last_global = cache.latest_sync()
    last_global_d = ({k: last_global[k] for k in last_global.keys()}
                     if last_global else None)
    blocked = sorted(get_blocked_accounts())
    out: dict = {
        "status":           "ok",
        "last_sync":        last_global_d,
        "blocked_accounts": blocked,
        "blocked_count":    len(blocked),
        "clients":          [],
    }
    for c in list_clients():
        if client and c["id"] != client:
            continue
        last = cache.latest_sync(client_id=c["id"])
        out["clients"].append({
            "id":           c["id"],
            "display_name": c["display_name"],
            "queries":      c["queries_count"],
            "last_sync":    ({k: last[k] for k in last.keys()} if last
                            else None),
        })
    return out


@router.post("/health/reset-blocked")
async def reset_blocked_accounts():
    """Clear the blocked-accounts cache so the next sync retries them
    all. Use after fixing prioritisation in Supermetrics Hub."""
    reset_blocked()
    return {"status": "ok", "message": "blocked list cleared — next sync retries all"}


@router.get("/clients")
async def clients(active_only: bool = Query(False)):
    """List configured clients. Pass `active_only=true` to filter to
    clients with any spend in the last 14 days — useful for the
    dashboard filter pills so blocked / dormant clients don't clutter
    the UI."""
    all_clients = list_clients()
    if not active_only:
        active = _active_client_ids()
        for c in all_clients:
            c["is_active"] = c["id"] in active
        return {"clients": all_clients}
    active = _active_client_ids()
    return {"clients": [c for c in all_clients if c["id"] in active]}


def _active_client_ids() -> set[str]:
    sql = """
    SELECT DISTINCT client_id FROM ads_daily
    WHERE date > date('now', '-14 day') AND spend > 0
    """
    with cache.connect() as conn:
        return {r["client_id"] for r in conn.execute(sql)}


@router.get("/end-clients")
async def end_clients():
    """Distinct end-clients (parsed brand names like 'Kenvue', 'Etihad')
    with their platform mix, ad counts, and 14-day spend. Used by the
    dashboard's account-filter dropdown — this is the user-facing notion
    of 'account', distinct from `client_id` which is the Joveo-internal
    config key tied to a specific ad-platform account."""
    sql = """
    SELECT end_client,
           GROUP_CONCAT(DISTINCT platform) AS platforms,
           COUNT(DISTINCT ad_id) AS ads,
           SUM(spend) AS spend_14d,
           MAX(date) AS last_active
    FROM ads_daily
    WHERE end_client IS NOT NULL AND end_client != ''
      AND date > date('now', '-14 day')
    GROUP BY end_client
    ORDER BY spend_14d DESC
    """
    with cache.connect() as conn:
        rows = list(conn.execute(sql))
    return {
        "count": len(rows),
        "end_clients": [
            {
                "name":        r["end_client"],
                "platforms":   (r["platforms"] or "").split(","),
                "ads":         r["ads"],
                "spend_14d":   r["spend_14d"] or 0,
                "last_active": r["last_active"],
            }
            for r in rows
        ],
    }
