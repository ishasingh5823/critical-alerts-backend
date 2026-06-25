"""Supermetrics enterprise v2 JSON Query API client.

Supermetrics' Query API accepts a JSON-encoded query in the URL:

    GET https://api.supermetrics.com/enterprise/v2/query/data/json?json=<urlenc>

where the JSON includes ds_id, ds_accounts, date_range_type, fields, etc.,
plus the api_key and ds_user credentials.

This client builds those URLs from a per-query spec dict declared in
backend/clients/*.json. The spec carries everything except the credentials,
which come from .env. Each query in the client config declares its
`type` (campaign_daily | ad_status) so the right normalizer runs.

Field aliases below are the headers Supermetrics actually returns for the
field IDs we use (verified empirically against Joveo's Kenvue account on
2026-05-26):

  Meta (FA) — campaign_daily query
    `Adcampaign_id`  -> header "Campaign ID"
    `Adcampaign`     -> header "Campaign name"
    `Date`           -> "Date"
    `Cost`           -> "Cost"
    `Impressions`    -> "Impressions"
    `Clicks`         -> "Clicks (all)"
    `Conversions`    -> "Conversions"

  Meta (FA) — ad_status query
    `Ad_id`          -> "Ad ID"
    `Ad_name`        -> "Ad name"
    `Ad_status`      -> "Ad status"   (carries DISAPPROVED etc.)
"""

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests


log = logging.getLogger(__name__)


# Map our canonical column names to the Supermetrics-returned header strings.
# Verified empirically against Joveo accounts 2026-05-26 / 2026-05-27.
#
# Cross-platform hierarchy mapping:
#   ours        Meta (FA)              Google Ads (AW)          Bing (AC)
#   ----------- ---------------------- ------------------------ -------------
#   campaign    Adcampaign_id          CampaignId               CampaignId
#   ad_set      Campaign_id (!)        AdGroupId                AdGroupId
#   ad          Ad_id                  Id  /  AdId              AdId
#
# Meta naming is confusing — `Campaign_id` in Supermetrics-FA returns ad-set
# level (legacy FB API naming). Google + Bing use "Ad Group" instead of
# "Ad Set" for the middle level. Same concept either way.
_FIELD_ALIASES = {
    # Account level (used by the batcher to route multi-account rows)
    "account_id":    ["Account ID", "Account_id", "AccountId", "account_id",
                      "account.id"],
    "account_name":  ["Account name", "Account Name", "Account",
                      "AccountName", "AccountDescriptiveName",
                      "account_name"],
    # Campaign level
    "campaign_id":   ["Campaign ID", "campaign_id", "campaign.id", "CampaignId"],
    "campaign_name": ["Campaign name", "Campaign Name", "campaign_name",
                      "campaign.name"],
    # Ad Set / Ad Group level (middle)
    "ad_set_id":     ["Ad set ID", "Adset ID", "Ad group ID", "AdGroupId",
                      "ad_set_id", "adset.id"],
    "ad_set_name":   ["Ad set name", "Adset name", "Ad group name",
                      "AdGroupName", "ad_set_name", "adset.name"],
    # Ad level (the creative). LinkedIn uses "Creative title" as the
    # ad's display name (no separate "Ad name" field).
    "ad_id":         ["Ad ID", "Creative ID", "ad_id", "ad.id", "AdId",
                      "Creative_id"],
    "ad_name":       ["Ad name", "Ad Name",
                      "Ad title", "AdTitle",            # Bing
                      "Creative title", "Creative_title",  # LinkedIn
                      "ad_name", "ad.name",
                      "Headline 1", "Headline part 1",
                      "HeadlinePart1"],                  # Google = headline-1
    # Time
    "date":          ["Date", "date", "Day", "segments.date"],
    # Metrics — LinkedIn calls spend "Total spent"; Meta + Google use "Cost".
    "spend":         ["Cost", "Total spent", "Amount spent", "Spend", "cost"],
    "impressions":   ["Impressions", "impressions", "Impr."],
    # Order matters: prefer "Link clicks" over "Clicks (all)" for Meta.
    # Meta's "Clicks (all)" includes social interactions (likes, post
    # engagements) which inflates CTR 1-7% vs Meta UI's "Link CTR".
    # Google/Bing/LinkedIn report "Clicks" already as link-equivalent.
    "clicks":        ["Link clicks", "Clicks", "Clicks (all)", "clicks"],
    # `applies` for v1 — proxy depending on platform:
    #   Meta:     Landing page views (LPV) — pixel-driven
    #   LinkedIn: Landing page clicks — closest analog
    #   Google:   Conversions (if conversion tracking is set up)
    # Swap to true ATS-side applies in a future iteration.
    "applies":       ["Landing page views", "Landing page clicks",
                      "Landing_page_views", "Landing_page_clicks",
                      "Conversions", "All conversions", "Leads",
                      "Submit application"],
    # Engagement / fatigue (Meta exposes both; other platforms may not)
    "frequency":     ["Frequency", "frequency", "Avg. frequency"],
    "reach":         ["Reach", "reach", "Unique users"],
    "currency":      ["Currency", "Account currency", "currency"],
    # Ad status (Meta + Google + Bing all surface as DISAPPROVED for our rule)
    "effective_status": ["Ad status", "Effective ad status", "Status",
                         "ad.policy_summary.approval_status",
                         "PolicySummaryApprovalStatus",
                         "Editorial status", "Approval status"],
}


class SupermetricsError(RuntimeError):
    pass


@dataclass
class QueryConfig:
    """One query declared in a client JSON file."""
    name: str
    platform: str                                  # meta / google / bing / linkedin
    type: str                                      # campaign_daily / ad_status
    query: Optional[Dict[str, Any]] = None         # full JSON query spec
    url: Optional[str] = None                      # legacy: full Query URL


class SupermetricsClient:
    """Build + execute Supermetrics enterprise v2 JSON queries."""

    DEFAULT_BASE = "https://api.supermetrics.com"
    QUERY_PATH = "/enterprise/v2/query/data/json"
    DEFAULT_TIMEOUT = 180

    def __init__(self, api_key: Optional[str] = None,
                 ds_user: Optional[str] = None,
                 base_url: Optional[str] = None,
                 timeout: int = DEFAULT_TIMEOUT):
        self.api_key = api_key or os.environ.get("SUPERMETRICS_API_KEY", "")
        self.ds_user = ds_user or os.environ.get("SUPERMETRICS_DS_USER", "")
        self.base_url = (base_url
                         or os.environ.get("SUPERMETRICS_BASE_URL")
                         or self.DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # ---------------- low-level fetch ----------------

    def _build_url(self, query_spec: Dict) -> str:
        """Inject credentials and JSON-encode into a Supermetrics URL.

        Resolution order for `ds_user` (each platform in Supermetrics has
        its own auth identity):
          1. `query_spec["ds_user"]` — explicit per-query override (preferred
             way for non-Meta platforms; client config carries it)
          2. `SUPERMETRICS_DS_USER_<DS_ID>` env var (e.g. ..._LIA, ..._AW)
          3. `SUPERMETRICS_DS_USER` env var (default — Meta in our case)
        """
        ds_id = query_spec.get("ds_id", "")
        per_platform_env = os.environ.get(f"SUPERMETRICS_DS_USER_{ds_id}", "")
        # Start with env defaults; query_spec wins because spread comes after.
        merged = {
            "api_key": self.api_key,
            "ds_user": per_platform_env or self.ds_user,
            **query_spec,
        }
        return (self.base_url + self.QUERY_PATH
                + "?json=" + urllib.parse.quote(json.dumps(merged)))

    def _fetch(self, url: str) -> Dict:
        redacted = url.replace(self.api_key, "<KEY>") if self.api_key else url
        log.info("GET %s", redacted)
        resp = requests.get(url, timeout=self.timeout)
        if not resp.ok:
            # Try to extract the structured error payload so the message
            # is actually useful for diagnosing.
            try:
                err = resp.json().get("error", {})
                code = err.get("code", "?")
                desc = err.get("description", "")
                raise SupermetricsError(
                    f"HTTP {resp.status_code} {code}: {desc[:200]}"
                )
            except (ValueError, AttributeError):
                raise SupermetricsError(
                    f"Supermetrics HTTP {resp.status_code}: {resp.text[:300]}"
                )
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise SupermetricsError(
                f"Supermetrics error: {payload['error']}"
            )
        return payload

    def run_query(self, q: QueryConfig) -> Dict:
        """Fetch a single saved query. Returns the raw JSON response."""
        if q.url:
            return self._fetch(q.url)
        if q.query:
            if not (self.api_key and self.ds_user):
                raise SupermetricsError(
                    "SUPERMETRICS_API_KEY and SUPERMETRICS_DS_USER required "
                    "in .env to use the JSON query form."
                )
            return self._fetch(self._build_url(q.query))
        raise SupermetricsError(
            f"Query '{q.name}' has neither `query` nor `url` — fill one in."
        )

    # ---------------- normalization ----------------

    @staticmethod
    def _matrix_to_dicts(payload: Dict) -> List[Dict[str, Any]]:
        """Supermetrics returns { data: [[header...], [row1...], ...] }."""
        if not payload:
            return []
        data = payload.get("data")
        if data is None:
            raise SupermetricsError(
                f"Unexpected response shape: keys={list(payload)[:8]}"
            )
        if not data:
            return []
        if isinstance(data[0], list):
            headers = data[0]
            return [dict(zip(headers, row)) for row in data[1:]]
        if isinstance(data[0], dict):
            return data
        raise SupermetricsError(
            f"Unexpected `data` shape: first element type={type(data[0]).__name__}"
        )

    @classmethod
    def _lookup(cls, row: Dict, canonical: str) -> Any:
        """Find a value in the row using all known aliases for this canonical."""
        for alias in _FIELD_ALIASES.get(canonical, []):
            if alias in row:
                return row[alias]
        lower = {k.lower(): v for k, v in row.items()}
        for alias in _FIELD_ALIASES.get(canonical, []):
            if alias.lower() in lower:
                return lower[alias.lower()]
        return None

    @classmethod
    def normalize_ad_daily(cls, payload: Dict, platform: str,
                          account_to_client: Optional[Dict[str, str]] = None,
                          fallback_client_id: Optional[str] = None
                          ) -> List[Dict]:
        """Map Supermetrics rows for an ad-level daily query into our schema.

        Supports two modes:
        - **Batched** (account_to_client passed): rows from a multi-account
          response are routed to the right client by Account ID.
        - **Single-client** (fallback_client_id passed): all rows belong to
          the same client. Used by legacy single-account queries that don't
          include Account_id in their fields.
        """
        rows = cls._matrix_to_dicts(payload)
        out: List[Dict] = []
        for r in rows:
            ad_id = cls._lookup(r, "ad_id")
            date = cls._lookup(r, "date")
            campaign_id = cls._lookup(r, "campaign_id")
            ad_set_id = cls._lookup(r, "ad_set_id")
            if not ad_id or not date or not campaign_id:
                continue
            client_id = cls._resolve_client(r, account_to_client,
                                           fallback_client_id)
            if not client_id:
                continue
            campaign_name = str(cls._lookup(r, "campaign_name") or "")
            # Parse end_client from campaign name — the real "account" in
            # business terms (e.g. Kenvue, Scale AI). Falls back to None
            # if the campaign doesn't follow Joveo naming.
            from core.end_client import parse_end_client  # local import to avoid circular
            end_client = parse_end_client(campaign_name)
            out.append({
                "client_id":     client_id,
                "platform":      platform,
                "end_client":    end_client,
                "campaign_id":   str(campaign_id),
                "campaign_name": campaign_name,
                "ad_set_id":     str(ad_set_id or ""),
                "ad_set_name":   str(cls._lookup(r, "ad_set_name") or ""),
                "ad_id":         str(ad_id),
                "ad_name":       str(cls._lookup(r, "ad_name") or ""),
                "date":          str(date)[:10],
                "spend":         _num(cls._lookup(r, "spend")),
                "impressions":   int(_num(cls._lookup(r, "impressions"))),
                "clicks":        int(_num(cls._lookup(r, "clicks"))),
                # applies is REAL — Google returns fractional conversion
                # counts (e.g. 659.99) due to attribution model weighting.
                "applies":       _num(cls._lookup(r, "applies")),
                "frequency":     _num(cls._lookup(r, "frequency")),
                "reach":         int(_num(cls._lookup(r, "reach"))),
                "currency":      str(cls._lookup(r, "currency") or ""),
            })
        return out

    @classmethod
    def normalize_ad_status(cls, payload: Dict, platform: str,
                            account_to_client: Optional[Dict[str, str]] = None,
                            fallback_client_id: Optional[str] = None
                            ) -> List[Dict]:
        rows = cls._matrix_to_dicts(payload)
        observed_at = datetime.utcnow().isoformat() + "Z"
        out: List[Dict] = []
        for r in rows:
            ad_id = cls._lookup(r, "ad_id")
            if not ad_id:
                continue
            client_id = cls._resolve_client(r, account_to_client,
                                           fallback_client_id)
            if not client_id:
                continue
            raw = cls._lookup(r, "effective_status") or ""
            out.append({
                "client_id":        client_id,
                "platform":         platform,
                "campaign_id":      str(cls._lookup(r, "campaign_id") or ""),
                "ad_set_id":        str(cls._lookup(r, "ad_set_id") or "") or None,
                "ad_id":            str(ad_id),
                "ad_name":          str(cls._lookup(r, "ad_name") or ""),
                "effective_status": _normalize_status(raw),
                "raw_status":       str(raw),
                "observed_at":      observed_at,
            })
        return out

    @classmethod
    def _resolve_client(cls, row: Dict,
                       account_to_client: Optional[Dict[str, str]],
                       fallback_client_id: Optional[str]) -> Optional[str]:
        """Map a response row to its client_id. Prefers account_id lookup
        (batched case); falls back to a single client_id (legacy single-
        account case)."""
        if account_to_client:
            account_id = cls._lookup(row, "account_id")
            if account_id is not None:
                # ds_accounts uses "act_<id>" but response gives raw numeric ID
                key = f"act_{account_id}"
                if key in account_to_client:
                    return account_to_client[key]
                # Some platforms may already return with prefix
                if str(account_id) in account_to_client:
                    return account_to_client[str(account_id)]
        return fallback_client_id


def _num(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return 0.0


_STATUS_MAP = {
    # Meta
    "ACTIVE": "ACTIVE",
    "PAUSED": "PAUSED",
    "DELETED": "DELETED",
    "ARCHIVED": "ARCHIVED",
    "DISAPPROVED": "DISAPPROVED",
    "WITH_ISSUES": "WITH_ISSUES",
    "PENDING_REVIEW": "PENDING_REVIEW",
    "ADSET_PAUSED": "PAUSED",
    "CAMPAIGN_PAUSED": "PAUSED",
    "IN_PROCESS": "PENDING_REVIEW",
    # Google Ads — uses Enabled / Paused / Removed
    "ENABLED": "ACTIVE",
    "REMOVED": "DELETED",
    "APPROVED": "ACTIVE",
    "ELIGIBLE": "ACTIVE",
    "REVIEW": "PENDING_REVIEW",
    "SITE_SUSPENDED": "DISAPPROVED",
    # Bing/Microsoft — uses Active / Paused / Deleted / Disapproved
    "APPROVEDLIMITED": "WITH_ISSUES",
    "DISAPPROVED ": "DISAPPROVED",
}


def _normalize_status(raw: str) -> str:
    if not raw:
        return "OTHER"
    key = str(raw).strip().upper().replace(" ", "_")
    if "DISAPPROV" in key:
        return "DISAPPROVED"
    return _STATUS_MAP.get(key, "OTHER")
