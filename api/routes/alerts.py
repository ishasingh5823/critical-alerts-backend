"""GET /api/alerts and /api/alerts/history, plus ack endpoints."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from core import cache

router = APIRouter()


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


@router.get("/alerts")
async def list_active_alerts(client: Optional[str] = Query(None),
                            limit: int = Query(200, ge=1, le=1000)):
    """All active (unresolved, unacknowledged) alerts. Optionally filter by client."""
    rows = cache.get_active_alerts(client_id=client, limit=limit)
    return {"count": len(rows), "alerts": [_row_to_dict(r) for r in rows]}


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: int):
    """Operator marks an alert as handled. It leaves the active list."""
    ok = cache.acknowledge_alert(alert_id)
    if not ok:
        raise HTTPException(404, f"Alert {alert_id} not found or already acknowledged")
    return {"status": "ok", "alert_id": alert_id}


@router.post("/alerts/{alert_id}/unack")
async def unacknowledge_alert(alert_id: int):
    """Undo an acknowledgement (if the operator clicked by accident)."""
    ok = cache.unacknowledge_alert(alert_id)
    if not ok:
        raise HTTPException(404, f"Alert {alert_id} not found or not acknowledged")
    return {"status": "ok", "alert_id": alert_id}


# ---------- Ad dismissal (permanent skip) ----------

@router.post("/ads/{ad_id}/dismiss")
async def dismiss_ad(ad_id: str, client: str = Query(...),
                    platform: str = Query(...),
                    reason: Optional[str] = Query(None)):
    """Permanently dismiss an ad — engine will skip it on future runs.
    Different from acknowledging: ack just clears one fired alert; dismiss
    tells the engine to stop evaluating this ad at all."""
    ok = cache.dismiss_ad(client, platform, ad_id, reason)
    return {"status": "ok" if ok else "already_dismissed",
            "ad_id": ad_id, "client": client, "platform": platform}


@router.post("/ads/{ad_id}/undismiss")
async def undismiss_ad(ad_id: str, client: str = Query(...),
                      platform: str = Query(...)):
    ok = cache.undismiss_ad(client, platform, ad_id)
    if not ok:
        raise HTTPException(404, f"Ad {ad_id} not dismissed")
    return {"status": "ok", "ad_id": ad_id}


@router.get("/ads/dismissed")
async def list_dismissed(client: Optional[str] = Query(None)):
    """List currently-dismissed ads. Useful for the dashboard "settings"
    panel or for the operator to undo a dismissal."""
    with cache.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ad_dismissals" + (" WHERE client_id = ?" if client else "")
            + " ORDER BY dismissed_at DESC",
            ([client] if client else [])
        ).fetchall()
    return {"count": len(rows),
            "dismissed": [{k: r[k] for k in r.keys()} for r in rows]}


@router.get("/alerts/history")
async def alert_history(client: Optional[str] = Query(None),
                       campaign_id: Optional[str] = Query(None),
                       limit: int = Query(500, ge=1, le=5000)):
    """Recent alert events, including resolved ones."""
    sql = "SELECT * FROM alert_events WHERE 1=1"
    params: list = []
    if client:
        sql += " AND client_id = ?"
        params.append(client)
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY fired_at DESC LIMIT ?"
    params.append(limit)
    with cache.connect() as conn:
        rows = list(conn.execute(sql, params))
    return {"count": len(rows), "alerts": [_row_to_dict(r) for r in rows]}


@router.get("/alerts/stats")
async def alert_stats(client: Optional[str] = Query(None),
                     days: int = Query(14, ge=1, le=90)):
    """Alert volume by type by day. Used to calibrate thresholds over time —
    if any one alert_type fires >5x/day on average, threshold is too loose;
    <0.1x/day means too tight (or genuinely healthy)."""
    sql_total = """
    SELECT alert_type, severity, COUNT(*) AS total
    FROM alert_events
    WHERE fired_at > datetime('now', '-' || ? || ' day')
    """
    sql_daily = """
    SELECT date(fired_at) AS day, alert_type, COUNT(*) AS n
    FROM alert_events
    WHERE fired_at > datetime('now', '-' || ? || ' day')
    """
    params_total: list = [days]
    params_daily: list = [days]
    if client:
        sql_total += " AND client_id = ?"
        sql_daily += " AND client_id = ?"
        params_total.append(client)
        params_daily.append(client)
    sql_total += " GROUP BY alert_type, severity ORDER BY total DESC"
    sql_daily += " GROUP BY day, alert_type ORDER BY day DESC, alert_type"

    with cache.connect() as conn:
        totals = [_row_to_dict(r) for r in conn.execute(sql_total, params_total)]
        daily  = [_row_to_dict(r) for r in conn.execute(sql_daily, params_daily)]
        # Active campaign / ad denominators so we can compute alert rate
        n_ads = conn.execute(
            "SELECT COUNT(DISTINCT ad_id) FROM ads_daily WHERE date > date('now','-14 day')"
            + (" AND client_id = ?" if client else ""),
            ([client] if client else []),
        ).fetchone()[0]

    return {
        "window_days": days,
        "client": client,
        "ads_tracked": n_ads,
        "totals_by_type": totals,
        "daily_counts": daily,
    }
