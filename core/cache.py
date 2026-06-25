"""SQLite cache for Supermetrics snapshots and alert state.

**Alerting unit = AD** (the creative). Each Ad belongs to an Ad Set,
each Ad Set belongs to a Campaign. We store the full hierarchy on
every row so the dashboard can roll up to ad-set or campaign as needed
without a second pull.

Tables:
  ads_daily      — one row per (client, platform, ad_id, date).
                   Holds the metrics the spike rules run on.
  ads_status     — one row per (client, platform, ad_id, observed_at).
                   Used by the rejections-cluster rule.
  alert_events   — one row per fired alert. Carries the full hierarchy
                   (campaign/ad_set/ad) so the UI can show it in context.
  sync_log       — one row per (client, query) sync attempt.

Cooldowns are derived from alert_events at read-time (last fire of the
same alert_type for the same ad — or campaign, in the case of the
rejections-cluster rule which aggregates ads).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ads_daily (
    client_id      TEXT NOT NULL,
    platform       TEXT NOT NULL,
    end_client     TEXT,                  -- parsed from campaign name; e.g. "Kenvue" inside Monster account holds "Trimac"
    campaign_id    TEXT NOT NULL,
    campaign_name  TEXT,
    ad_set_id      TEXT NOT NULL,
    ad_set_name    TEXT,
    ad_id          TEXT NOT NULL,
    ad_name        TEXT,
    date           TEXT NOT NULL,         -- YYYY-MM-DD
    spend          REAL DEFAULT 0,
    impressions    INTEGER DEFAULT 0,
    clicks         INTEGER DEFAULT 0,
    applies        REAL DEFAULT 0,        -- LPVs (Meta) / Conversions (Google) — fractional on Google due to attribution weighting
    -- Engagement / fatigue metrics (Meta only for now; nulls on other platforms)
    frequency      REAL DEFAULT 0,        -- Meta: avg impressions per reached user
    reach          INTEGER DEFAULT 0,     -- Meta: unique users reached
    currency       TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (client_id, platform, ad_id, date)
);
CREATE INDEX IF NOT EXISTS idx_ads_daily_end_client
    ON ads_daily(client_id, end_client, platform, date);
CREATE INDEX IF NOT EXISTS idx_ads_daily_date
    ON ads_daily(date);
CREATE INDEX IF NOT EXISTS idx_ads_daily_client_date
    ON ads_daily(client_id, date);
CREATE INDEX IF NOT EXISTS idx_ads_daily_campaign
    ON ads_daily(client_id, campaign_id, date);
CREATE INDEX IF NOT EXISTS idx_ads_daily_ad_set
    ON ads_daily(client_id, ad_set_id, date);

CREATE TABLE IF NOT EXISTS ads_status (
    client_id        TEXT NOT NULL,
    platform         TEXT NOT NULL,
    campaign_id      TEXT NOT NULL,
    ad_set_id        TEXT,
    ad_id            TEXT NOT NULL,
    ad_name          TEXT,
    effective_status TEXT NOT NULL,        -- normalized: ACTIVE / PAUSED / DISAPPROVED / WITH_ISSUES / ...
    raw_status       TEXT,                 -- platform-native string
    observed_at      TEXT NOT NULL,        -- ISO 8601
    PRIMARY KEY (client_id, platform, ad_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_ads_status_campaign
    ON ads_status(client_id, campaign_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_ads_status_recent
    ON ads_status(observed_at);

CREATE TABLE IF NOT EXISTS alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    platform        TEXT NOT NULL,
    end_client      TEXT,                   -- end-brand parsed from campaign name; null when unknown
    campaign_id     TEXT,                   -- nullable for account-level alerts
    campaign_name   TEXT,
    ad_set_id       TEXT,                   -- nullable for campaign- or account-level alerts
    ad_set_name     TEXT,
    ad_id           TEXT,                   -- nullable for campaign- or account-level alerts
    ad_name         TEXT,
    alert_type      TEXT NOT NULL,          -- spend_spike / ctr_spike / cpa_spike / ... / account_no_spend / account_metric_spike / ...
    severity        TEXT NOT NULL,          -- CRITICAL / WARNING / INFO — also encodes the ranking
    direction       TEXT,
    today_value     REAL,
    baseline_value  REAL,
    deviation_pct   REAL,
    detail          TEXT,
    fired_at        TEXT NOT NULL,
    resolved_at     TEXT,                   -- auto-set when condition normalizes (future)
    acknowledged_at TEXT                    -- operator clicked the checkbox; alert moves out of active list
);
CREATE INDEX IF NOT EXISTS idx_alert_events_unique
    ON alert_events(client_id, platform, COALESCE(ad_id, ''),
                    COALESCE(end_client, ''), alert_type, fired_at);

CREATE TABLE IF NOT EXISTS ad_dismissals (
    client_id       TEXT NOT NULL,
    platform        TEXT NOT NULL,
    ad_id           TEXT NOT NULL,
    dismissed_at    TEXT NOT NULL,
    reason          TEXT,
    PRIMARY KEY (client_id, platform, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_alert_events_active
    ON alert_events(resolved_at, fired_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_campaign
    ON alert_events(client_id, campaign_id);
CREATE INDEX IF NOT EXISTS idx_alert_events_ad
    ON alert_events(client_id, ad_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    query_name      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    rows_pulled     INTEGER,
    status          TEXT NOT NULL,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_log_client_started
    ON sync_log(client_id, started_at);
"""


def _db_path() -> Path:
    raw = os.environ.get("ALERT_DB_PATH", "backend/data/alerts.db")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / raw
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def connect():
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


# ---------- writers ----------

def upsert_ad_daily(rows: Iterable[Dict]) -> int:
    """Insert or update daily ad metrics. Returns rows written."""
    now = datetime.utcnow().isoformat() + "Z"
    sql = """
    INSERT INTO ads_daily
        (client_id, platform, end_client, campaign_id, campaign_name,
         ad_set_id, ad_set_name, ad_id, ad_name, date,
         spend, impressions, clicks, applies,
         frequency, reach, currency, updated_at)
    VALUES
        (:client_id, :platform, :end_client, :campaign_id, :campaign_name,
         :ad_set_id, :ad_set_name, :ad_id, :ad_name, :date,
         :spend, :impressions, :clicks, :applies,
         :frequency, :reach, :currency, :updated_at)
    ON CONFLICT(client_id, platform, ad_id, date) DO UPDATE SET
        end_client    = excluded.end_client,
        campaign_id   = excluded.campaign_id,
        campaign_name = excluded.campaign_name,
        ad_set_id     = excluded.ad_set_id,
        ad_set_name   = excluded.ad_set_name,
        ad_name       = excluded.ad_name,
        spend         = excluded.spend,
        impressions   = excluded.impressions,
        clicks        = excluded.clicks,
        applies       = excluded.applies,
        frequency     = excluded.frequency,
        reach         = excluded.reach,
        currency      = excluded.currency,
        updated_at    = excluded.updated_at
    """
    count = 0
    with connect() as conn:
        for r in rows:
            r = {**{"frequency": 0.0, "reach": 0, "end_client": None},
                 **r, "updated_at": now}
            conn.execute(sql, r)
            count += 1
    return count


# ---------- ad dismissals (permanent skip) ----------

def dismiss_ad(client_id: str, platform: str, ad_id: str,
              reason: Optional[str] = None) -> bool:
    """Mark an ad as permanently dismissed — engine will skip it on
    future runs. Returns True if newly dismissed, False if already was."""
    sql = """
    INSERT OR IGNORE INTO ad_dismissals
        (client_id, platform, ad_id, dismissed_at, reason)
    VALUES (?, ?, ?, ?, ?)
    """
    with connect() as conn:
        cur = conn.execute(sql, (client_id, platform, ad_id,
                                 datetime.utcnow().isoformat() + "Z",
                                 reason))
        return cur.rowcount > 0


def undismiss_ad(client_id: str, platform: str, ad_id: str) -> bool:
    sql = """
    DELETE FROM ad_dismissals
    WHERE client_id = ? AND platform = ? AND ad_id = ?
    """
    with connect() as conn:
        cur = conn.execute(sql, (client_id, platform, ad_id))
        return cur.rowcount > 0


def get_dismissed_ad_ids(client_id: Optional[str] = None) -> set:
    """Set of dismissed ad_ids — used by engine to skip these ads."""
    sql = "SELECT client_id, platform, ad_id FROM ad_dismissals"
    params: List = []
    if client_id:
        sql += " WHERE client_id = ?"
        params.append(client_id)
    with connect() as conn:
        return {(r["client_id"], r["platform"], r["ad_id"])
                for r in conn.execute(sql, params)}


def upsert_ads_status(rows: Iterable[Dict]) -> int:
    sql = """
    INSERT OR IGNORE INTO ads_status
        (client_id, platform, campaign_id, ad_set_id, ad_id, ad_name,
         effective_status, raw_status, observed_at)
    VALUES
        (:client_id, :platform, :campaign_id, :ad_set_id, :ad_id, :ad_name,
         :effective_status, :raw_status, :observed_at)
    """
    count = 0
    with connect() as conn:
        for r in rows:
            r = {**{"ad_set_id": None}, **r}  # default
            conn.execute(sql, r)
            count += 1
    return count


def write_alert(event: Dict) -> Optional[int]:
    sql = """
    INSERT INTO alert_events
        (client_id, platform, end_client, campaign_id, campaign_name,
         ad_set_id, ad_set_name, ad_id, ad_name,
         alert_type, severity, direction, today_value, baseline_value,
         deviation_pct, detail, fired_at)
    VALUES
        (:client_id, :platform, :end_client, :campaign_id, :campaign_name,
         :ad_set_id, :ad_set_name, :ad_id, :ad_name,
         :alert_type, :severity, :direction, :today_value, :baseline_value,
         :deviation_pct, :detail, :fired_at)
    """
    # Legacy schema had NOT NULL on campaign_id/ad_set_id/ad_id; coerce
    # None → "" so account-level alerts (no campaign/ad context) still write.
    e = {**{"end_client": None,
            "campaign_id": "", "campaign_name": "",
            "ad_set_id": "", "ad_set_name": "",
            "ad_id": "", "ad_name": ""}, **event}
    for k in ("campaign_id", "ad_set_id", "ad_id",
              "campaign_name", "ad_set_name", "ad_name"):
        if e.get(k) is None:
            e[k] = ""
    with connect() as conn:
        cur = conn.execute(sql, e)
        return cur.lastrowid if cur.rowcount else None


def record_sync(client_id: str, query_name: str, started_at: str,
                finished_at: str, rows_pulled: int, status: str,
                error_message: Optional[str] = None) -> None:
    sql = """
    INSERT INTO sync_log
        (client_id, query_name, started_at, finished_at,
         rows_pulled, status, error_message)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    with connect() as conn:
        conn.execute(sql, (client_id, query_name, started_at,
                           finished_at, rows_pulled, status, error_message))


# ---------- readers ----------

def get_active_ads(client_id: Optional[str] = None,
                  as_of: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """List (client_id, platform, ad_id) tuples for **active Joveo-format**
    ads with spend in the last 14 days.

    Joveo-format = campaign name follows the `Joveo | <reseller?> |
    <end_client> | <platform> | ...` convention. Non-Joveo campaigns
    (`G_Horsham_RN_NB_CONV`, "Career Site Builder", etc.) are
    explicitly filtered out — per user spec, those are not client work
    and shouldn't drive alerts."""
    as_of = as_of or datetime.utcnow().date().isoformat()
    sql = """
    SELECT DISTINCT client_id, platform, ad_id
    FROM ads_daily
    WHERE date > date(?, '-14 day')
      AND spend > 0
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: List = [as_of]
    if client_id:
        sql += " AND client_id = ?"
        params.append(client_id)
    with connect() as conn:
        return [(r["client_id"], r["platform"], r["ad_id"])
                for r in conn.execute(sql, params)]


def get_ad_window(client_id: str, ad_id: str, end_date: str,
                 days: int) -> List[sqlite3.Row]:
    """Return rows for an ad ending on `end_date` (inclusive), covering
    `days` calendar days. Used to assemble today + 7-day baseline."""
    sql = """
    SELECT * FROM ads_daily
    WHERE client_id = ?
      AND ad_id = ?
      AND date <= ?
      AND date > date(?, '-' || ? || ' day')
    ORDER BY date
    """
    with connect() as conn:
        return list(conn.execute(sql, (client_id, ad_id, end_date,
                                       end_date, days)))


def get_active_campaigns(client_id: Optional[str] = None,
                        as_of: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """List (client_id, platform, campaign_id) tuples for active
    **Joveo-format** campaigns. Same end_client filter as get_active_ads —
    we don't alert on campaigns whose names don't follow the convention."""
    as_of = as_of or datetime.utcnow().date().isoformat()
    sql = """
    SELECT DISTINCT client_id, platform, campaign_id
    FROM ads_daily
    WHERE date > date(?, '-14 day')
      AND spend > 0
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: List = [as_of]
    if client_id:
        sql += " AND client_id = ?"
        params.append(client_id)
    with connect() as conn:
        return [(r["client_id"], r["platform"], r["campaign_id"])
                for r in conn.execute(sql, params)]


def get_recent_ad_disapprovals(client_id: str, campaign_id: str,
                              hours: int = 24) -> int:
    """Count distinct ads in this campaign DISAPPROVED in the last N hours."""
    sql = """
    SELECT COUNT(DISTINCT ad_id) AS n
    FROM ads_status
    WHERE client_id = ?
      AND campaign_id = ?
      AND effective_status = 'DISAPPROVED'
      AND observed_at > datetime('now', '-' || ? || ' hour')
    """
    with connect() as conn:
        row = conn.execute(sql, (client_id, campaign_id, hours)).fetchone()
        return row["n"] if row else 0


def last_alert_fired(client_id: str, alert_type: str,
                    ad_id: Optional[str] = None,
                    campaign_id: Optional[str] = None) -> Optional[str]:
    """Last firing of this alert for this ad (preferred) or campaign.
    Used to enforce cooldowns."""
    if ad_id:
        sql = """
        SELECT fired_at FROM alert_events
        WHERE client_id = ? AND ad_id = ? AND alert_type = ?
        ORDER BY fired_at DESC LIMIT 1
        """
        params = (client_id, ad_id, alert_type)
    elif campaign_id:
        sql = """
        SELECT fired_at FROM alert_events
        WHERE client_id = ? AND campaign_id = ? AND alert_type = ?
        ORDER BY fired_at DESC LIMIT 1
        """
        params = (client_id, campaign_id, alert_type)
    else:
        return None
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return row["fired_at"] if row else None


def get_active_alerts(client_id: Optional[str] = None,
                     limit: int = 200) -> List[sqlite3.Row]:
    """Alerts that are neither resolved nor acknowledged. Excludes alerts
    on non-Joveo-format campaigns (those are noise per the v2.1 spec)."""
    sql = """
    SELECT * FROM alert_events
    WHERE resolved_at IS NULL AND acknowledged_at IS NULL
      AND end_client IS NOT NULL AND end_client != ''
    """
    params: List = []
    if client_id:
        sql += " AND client_id = ?"
        params.append(client_id)
    sql += " ORDER BY fired_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return list(conn.execute(sql, params))


def acknowledge_alert(alert_id: int) -> bool:
    """Mark an alert as acknowledged by the operator. Returns True if a
    row was updated, False if alert wasn't found / already ack'd."""
    sql = """
    UPDATE alert_events
    SET acknowledged_at = ?
    WHERE id = ? AND acknowledged_at IS NULL
    """
    with connect() as conn:
        cur = conn.execute(sql, (datetime.utcnow().isoformat() + "Z",
                                 alert_id))
        return cur.rowcount > 0


def unacknowledge_alert(alert_id: int) -> bool:
    """Reverse an acknowledgement (operator changed their mind)."""
    sql = """
    UPDATE alert_events
    SET acknowledged_at = NULL
    WHERE id = ? AND acknowledged_at IS NOT NULL
    """
    with connect() as conn:
        cur = conn.execute(sql, (alert_id,))
        return cur.rowcount > 0


def latest_sync(client_id: Optional[str] = None) -> Optional[sqlite3.Row]:
    sql = "SELECT * FROM sync_log"
    params: List = []
    if client_id:
        sql += " WHERE client_id = ?"
        params.append(client_id)
    sql += " ORDER BY finished_at DESC LIMIT 1"
    with connect() as conn:
        return conn.execute(sql, params).fetchone()
