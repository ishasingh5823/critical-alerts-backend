"""GET /api/ads, /api/campaigns, /api/ad-sets — hierarchy rollups."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from core import cache

router = APIRouter()


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


@router.get("/ads")
async def list_ads(client: Optional[str] = Query(None),
                  platform: Optional[str] = Query(None),
                  campaign_id: Optional[str] = Query(None),
                  ad_set_id: Optional[str] = Query(None)):
    """Distinct ads active in the last 14 days, with 14-day rollups."""
    sql = """
    SELECT
        client_id, platform, campaign_id, ad_set_id, ad_id,
        MAX(campaign_name) AS campaign_name,
        MAX(ad_set_name)   AS ad_set_name,
        MAX(ad_name)       AS ad_name,
        SUM(spend)         AS spend_14d,
        SUM(impressions)   AS impressions_14d,
        SUM(clicks)        AS clicks_14d,
        SUM(applies)       AS applies_14d,
        MAX(date)          AS last_active
    FROM ads_daily
    WHERE date > date('now', '-14 day')
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: list = []
    if client:
        sql += " AND client_id = ?"; params.append(client)
    if platform:
        sql += " AND platform = ?"; params.append(platform)
    if campaign_id:
        sql += " AND campaign_id = ?"; params.append(campaign_id)
    if ad_set_id:
        sql += " AND ad_set_id = ?"; params.append(ad_set_id)
    sql += """ GROUP BY client_id, platform, campaign_id, ad_set_id, ad_id
               ORDER BY spend_14d DESC """
    with cache.connect() as conn:
        rows = list(conn.execute(sql, params))
    return {"count": len(rows), "ads": [_row_to_dict(r) for r in rows]}


@router.get("/campaigns")
async def list_campaigns(client: Optional[str] = Query(None),
                        platform: Optional[str] = Query(None)):
    """Campaign-level rollups (aggregated from ads_daily)."""
    sql = """
    SELECT
        client_id, platform, campaign_id,
        MAX(campaign_name) AS campaign_name,
        COUNT(DISTINCT ad_set_id) AS ad_sets,
        COUNT(DISTINCT ad_id)     AS ads,
        SUM(spend)         AS spend_14d,
        SUM(applies)       AS applies_14d,
        MAX(date)          AS last_active
    FROM ads_daily
    WHERE date > date('now', '-14 day')
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: list = []
    if client:
        sql += " AND client_id = ?"; params.append(client)
    if platform:
        sql += " AND platform = ?"; params.append(platform)
    sql += """ GROUP BY client_id, platform, campaign_id
               ORDER BY spend_14d DESC """
    with cache.connect() as conn:
        rows = list(conn.execute(sql, params))
    return {"count": len(rows), "campaigns": [_row_to_dict(r) for r in rows]}


@router.get("/ad-sets")
async def list_ad_sets(client: Optional[str] = Query(None),
                      campaign_id: Optional[str] = Query(None)):
    """Ad-set-level rollups."""
    sql = """
    SELECT
        client_id, platform, campaign_id, ad_set_id,
        MAX(campaign_name) AS campaign_name,
        MAX(ad_set_name)   AS ad_set_name,
        COUNT(DISTINCT ad_id) AS ads,
        SUM(spend)         AS spend_14d,
        SUM(applies)       AS applies_14d,
        MAX(date)          AS last_active
    FROM ads_daily
    WHERE date > date('now', '-14 day')
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: list = []
    if client:
        sql += " AND client_id = ?"; params.append(client)
    if campaign_id:
        sql += " AND campaign_id = ?"; params.append(campaign_id)
    sql += """ GROUP BY client_id, platform, campaign_id, ad_set_id
               ORDER BY spend_14d DESC """
    with cache.connect() as conn:
        rows = list(conn.execute(sql, params))
    return {"count": len(rows), "ad_sets": [_row_to_dict(r) for r in rows]}


@router.get("/ads/{ad_id}/history")
async def ad_history(ad_id: str,
                    client: str = Query(...),
                    days: int = Query(30, ge=1, le=180)):
    """Daily metrics for one ad — for the drilldown chart."""
    sql = """
    SELECT * FROM ads_daily
    WHERE client_id = ? AND ad_id = ?
      AND date > date('now', '-' || ? || ' day')
    ORDER BY date
    """
    with cache.connect() as conn:
        rows = list(conn.execute(sql, (client, ad_id, days)))
    if not rows:
        raise HTTPException(404, f"No data for ad {ad_id}")
    return {"count": len(rows), "history": [_row_to_dict(r) for r in rows]}
