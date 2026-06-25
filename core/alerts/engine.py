"""Alert engine — runs all rules across all clients, ads, and end-clients.

Three tiers of evaluation:
  1. **AD-level** — every ad against its own 7-day baseline. Most alerts.
  2. **CAMPAIGN-level** — rejections-cluster (≥3 disapproved ads/campaign).
  3. **ACCOUNT-level (end-client × platform)** — aggregate spend/CPC/CTR,
     no_spend total, all_rejected percentage. These are the
     "executive-level" signals that something is wrong at the brand level.

Reference day = yesterday (UTC) — full-day vs full-day comparison avoids
the partial-today problem. Each rule respects per-client overrides from
the client JSON (alert_overrides.<rule>.<metric>) and the 6-hour cooldown
to prevent re-firing the same alert.

Dismissed ads (in `ad_dismissals` table) are skipped entirely.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from clients import get_client_config
from core import cache
from core.alerts.rules.account_level import (
    check_account_all_rejected,
    check_account_metric_spike,
    check_account_no_spend,
)
from core.alerts.rules.low_impressions import check_low_impressions
from core.alerts.rules.rejections import check_rejections
from core.alerts.rules.spike import SPIKE_CONFIGS, check_spike


log = logging.getLogger(__name__)

COOLDOWN_HOURS = 6
BASELINE_DAYS = 7


def run(client_id: Optional[str] = None,
       reference_date: Optional[str] = None) -> List[Dict]:
    if reference_date is None:
        reference_date = (datetime.utcnow().date()
                          - timedelta(days=1)).isoformat()
    written: List[Dict] = []

    dismissed = cache.get_dismissed_ad_ids(client_id=client_id)

    # AD-level rules
    ads = cache.get_active_ads(client_id=client_id, as_of=reference_date)
    log.info("Evaluating ad-level rules on %d ads (reference=%s, client=%s)",
             len(ads), reference_date, client_id or "*")
    for (cid, platform, ad_id) in ads:
        if (cid, platform, ad_id) in dismissed:
            continue
        written.extend(_eval_ad(cid, platform, ad_id, reference_date))

    # CAMPAIGN-level (rejections cluster)
    campaigns = cache.get_active_campaigns(client_id=client_id,
                                          as_of=reference_date)
    log.info("Evaluating rejections-cluster on %d campaigns", len(campaigns))
    for (cid, platform, campaign_id) in campaigns:
        written.extend(_eval_rejections(cid, platform, campaign_id))

    # ACCOUNT-level (end-client × platform)
    account_keys = _list_active_accounts(client_id=client_id,
                                        as_of=reference_date)
    log.info("Evaluating account-level rules on %d end-client×platform accounts",
             len(account_keys))
    for (cid, end_client, platform) in account_keys:
        written.extend(_eval_account(cid, end_client, platform, reference_date))

    log.info("Alert engine wrote %d events", len(written))
    return written


def _eval_ad(client_id: str, platform: str, ad_id: str,
            reference_date: str) -> List[Dict]:
    rows = cache.get_ad_window(client_id, ad_id, reference_date,
                              days=BASELINE_DAYS + 1)
    if not rows:
        return []

    ref_row = next((r for r in rows if r["date"] == reference_date), None)
    baseline_rows = [r for r in rows if r["date"] != reference_date]
    if ref_row is None:
        return []

    hierarchy = {
        "end_client":    ref_row["end_client"] if "end_client" in ref_row.keys() else None,
        "campaign_id":   ref_row["campaign_id"],
        "campaign_name": ref_row["campaign_name"] or "",
        "ad_set_id":     ref_row["ad_set_id"],
        "ad_set_name":   ref_row["ad_set_name"] or "",
        "ad_id":         ref_row["ad_id"],
        "ad_name":       ref_row["ad_name"] or "",
    }

    try:
        client_cfg: Optional[Dict[str, Any]] = get_client_config(client_id)
    except KeyError:
        client_cfg = None

    fired_now: List[Dict] = []
    for metric in SPIKE_CONFIGS.keys():
        ev = check_spike(metric, ref_row, baseline_rows,
                        client_config=client_cfg)
        if not ev:
            continue
        if _in_cooldown_ad(client_id, ad_id, ev["alert_type"]):
            continue
        ev_full = _finalize(ev, client_id, platform, hierarchy)
        if cache.write_alert(ev_full) is not None:
            fired_now.append(ev_full)

    ev = check_low_impressions(ref_row, baseline_rows,
                              client_config=client_cfg)
    if ev and not _in_cooldown_ad(client_id, ad_id, ev["alert_type"]):
        ev_full = _finalize(ev, client_id, platform, hierarchy)
        if cache.write_alert(ev_full) is not None:
            fired_now.append(ev_full)
    return fired_now


def _eval_rejections(client_id: str, platform: str,
                    campaign_id: str) -> List[Dict]:
    n_disapproved = cache.get_recent_ad_disapprovals(client_id, campaign_id,
                                                    hours=24)
    ev = check_rejections(n_disapproved)
    if not ev:
        return []
    if _in_cooldown_campaign(client_id, campaign_id, ev["alert_type"]):
        return []

    with cache.connect() as conn:
        row = conn.execute("""
            SELECT campaign_name, end_client FROM ads_daily
            WHERE client_id = ? AND campaign_id = ?
            ORDER BY date DESC LIMIT 1
        """, (client_id, campaign_id)).fetchone()
    campaign_name = row["campaign_name"] if row else ""
    end_client = row["end_client"] if row else None

    ev_full = _finalize(ev, client_id, platform, {
        "end_client":    end_client,
        "campaign_id":   campaign_id,
        "campaign_name": campaign_name,
        "ad_set_id":     None,
        "ad_set_name":   None,
        "ad_id":         None,
        "ad_name":       None,
    })
    if cache.write_alert(ev_full) is not None:
        return [ev_full]
    return []


def _list_active_accounts(client_id: Optional[str],
                         as_of: str) -> List[Tuple[str, str, str]]:
    """Distinct (client_id, end_client, platform) tuples with spend in
    the last 14 days. We only evaluate account-level when end_client is
    non-null (parsed) — campaigns without Joveo-format names get
    skipped at this layer."""
    sql = """
    SELECT DISTINCT client_id, end_client, platform
    FROM ads_daily
    WHERE end_client IS NOT NULL AND end_client != ''
      AND date > date(?, '-14 day')
      AND spend > 0
    """
    params: List = [as_of]
    if client_id:
        sql += " AND client_id = ?"
        params.append(client_id)
    with cache.connect() as conn:
        return [(r["client_id"], r["end_client"], r["platform"])
                for r in conn.execute(sql, params)]


def _eval_account(client_id: str, end_client: str, platform: str,
                 reference_date: str) -> List[Dict]:
    """Account = (client_id, end_client, platform). Sums all ad metrics
    under it and runs the account-level rules."""
    sql = """
    SELECT date,
           SUM(spend)       AS spend,
           SUM(impressions) AS impressions,
           SUM(clicks)      AS clicks,
           SUM(applies)     AS applies,
           COUNT(DISTINCT ad_id) AS ad_count
    FROM ads_daily
    WHERE client_id = ? AND platform = ? AND end_client = ?
      AND date > date(?, '-' || ? || ' day')
      AND date <= ?
    GROUP BY date ORDER BY date
    """
    with cache.connect() as conn:
        rows = list(conn.execute(sql, (client_id, platform, end_client,
                                       reference_date, BASELINE_DAYS + 1,
                                       reference_date)))
    if not rows:
        return []

    today = next((dict(r) for r in rows if r["date"] == reference_date), None)
    baseline = [dict(r) for r in rows if r["date"] != reference_date]
    if today is None or not baseline:
        return []

    # Hierarchy for the account-level alert — no campaign/ad_set/ad
    hierarchy = {
        "end_client":    end_client,
        "campaign_id":   None,
        "campaign_name": None,
        "ad_set_id":     None,
        "ad_set_name":   None,
        "ad_id":         None,
        "ad_name":       None,
    }

    fired_now: List[Dict] = []

    # Rule: account_no_spend
    ev = check_account_no_spend(today, baseline)
    if ev and not _in_cooldown_account(client_id, end_client, platform,
                                       ev["alert_type"]):
        ev_full = _finalize(ev, client_id, platform, hierarchy)
        if cache.write_alert(ev_full) is not None:
            fired_now.append(ev_full)

    # Rule: account_all_rejected — count disapproved across all ads under
    # this end_client on this platform
    with cache.connect() as conn:
        n_total = conn.execute("""
            SELECT COUNT(DISTINCT ad_id) FROM ads_daily
            WHERE client_id = ? AND platform = ? AND end_client = ?
              AND date > date(?, '-7 day')
        """, (client_id, platform, end_client, reference_date)).fetchone()[0]
        n_disap = conn.execute("""
            SELECT COUNT(DISTINCT s.ad_id) FROM ads_status s
            JOIN ads_daily d ON s.client_id=d.client_id AND s.ad_id=d.ad_id
            WHERE s.client_id = ? AND s.platform = ?
              AND d.end_client = ?
              AND s.effective_status = 'DISAPPROVED'
              AND s.observed_at > datetime('now', '-24 hour')
        """, (client_id, platform, end_client)).fetchone()[0]
    ev = check_account_all_rejected(n_total, n_disap)
    if ev and not _in_cooldown_account(client_id, end_client, platform,
                                       ev["alert_type"]):
        ev_full = _finalize(ev, client_id, platform, hierarchy)
        if cache.write_alert(ev_full) is not None:
            fired_now.append(ev_full)

    # Rule: account_metric_spike — spend / cpc / ctr aggregates
    for metric in ("spend", "cpc", "ctr"):
        ev = check_account_metric_spike(metric, today, baseline)
        if ev and not _in_cooldown_account(client_id, end_client, platform,
                                           ev["alert_type"]):
            ev_full = _finalize(ev, client_id, platform, hierarchy)
            if cache.write_alert(ev_full) is not None:
                fired_now.append(ev_full)

    return fired_now


def _in_cooldown_ad(client_id: str, ad_id: str, alert_type: str) -> bool:
    last = cache.last_alert_fired(client_id, alert_type, ad_id=ad_id)
    return _is_recent(last)


def _in_cooldown_campaign(client_id: str, campaign_id: str,
                         alert_type: str) -> bool:
    last = cache.last_alert_fired(client_id, alert_type,
                                  campaign_id=campaign_id)
    return _is_recent(last)


def _in_cooldown_account(client_id: str, end_client: str, platform: str,
                        alert_type: str) -> bool:
    """Cooldown for account-level alerts uses end_client + platform as key."""
    sql = """
    SELECT fired_at FROM alert_events
    WHERE client_id = ? AND platform = ? AND end_client = ?
      AND alert_type = ?
    ORDER BY fired_at DESC LIMIT 1
    """
    with cache.connect() as conn:
        row = conn.execute(sql, (client_id, platform, end_client,
                                 alert_type)).fetchone()
    return _is_recent(row["fired_at"] if row else None)


_TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _is_recent(iso_ts: Optional[str]) -> bool:
    if not iso_ts:
        return False
    normalized = _TZ_SUFFIX.sub("", iso_ts)
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (datetime.utcnow() - dt) < timedelta(hours=COOLDOWN_HOURS)


def _finalize(partial: Dict, client_id: str, platform: str,
             hierarchy: Dict) -> Dict:
    return {
        **partial,
        "client_id":     client_id,
        "platform":      platform,
        **hierarchy,
        "fired_at":      datetime.utcnow().isoformat() + "Z",
    }
