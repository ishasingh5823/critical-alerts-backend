"""Account-level (end-client) alert rules.

These run ABOVE the per-ad rules and aggregate metrics across all ads
under one end-client × platform combination. Examples:

  account_no_spend       — end-client's total spend = 0 today, baseline > $50
  account_all_rejected   — ≥50% of end-client's ads are DISAPPROVED right now
  account_metric_spike   — aggregate CPC, CTR, spend doubled (±40%+ from baseline)

The unit is (client_id, end_client, platform). Multi-platform brands
(e.g. Kenvue runs on Meta + Google + Bing) fire separately per platform
because the diagnosis differs by platform.

Severity for everything here is CRITICAL — these are "executive-level"
signals that something is wrong at the brand level, not a single ad
having a bad day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AccountSpikeConfig:
    metric: str
    low_mult: float = 0.6   # 40% drop
    high_mult: float = 1.4  # 40% rise
    baseline_min: float = 0.0
    today_volume_gate: float = 0.0
    severity: str = "CRITICAL"


# Defaults — for an end-client to fire account-level alerts, the baseline
# numbers have to be meaningful (not a tiny side account).
ACCOUNT_SPIKE_CONFIGS: Dict[str, AccountSpikeConfig] = {
    "spend": AccountSpikeConfig(
        metric="spend",
        low_mult=0.5,   # ±50% — account-level is more tolerant since aggregates are stabler
        high_mult=1.5,
        baseline_min=50.0,    # baseline ≥ $50/day in account aggregate to alert
    ),
    "cpc": AccountSpikeConfig(
        metric="cpc",
        low_mult=0.6,
        high_mult=1.6,
        today_volume_gate=50,  # account-level needs at least 50 clicks today
    ),
    "ctr": AccountSpikeConfig(
        metric="ctr",
        low_mult=0.6,
        high_mult=1.6,
        today_volume_gate=500,  # account-level needs 500 imp today
    ),
}


# ----- Rule 1: account total spend went to ~0 -----

@dataclass(frozen=True)
class AccountNoSpendConfig:
    today_max: float = 0.50         # today's account total spend < this $
    baseline_min: float = 50.0      # AND baseline (7d avg) account total ≥ this
    severity: str = "CRITICAL"


def check_account_no_spend(
    today_totals: Dict[str, float],
    baseline_totals: List[Dict[str, float]],
    config: Optional[AccountNoSpendConfig] = None,
) -> Optional[Dict]:
    cfg = config or AccountNoSpendConfig()
    if not baseline_totals:
        return None
    today_spend = float(today_totals.get("spend", 0) or 0)
    baseline_spends = [float(t.get("spend", 0) or 0) for t in baseline_totals]
    baseline_avg = sum(baseline_spends) / len(baseline_spends)
    if baseline_avg < cfg.baseline_min:
        return None
    if today_spend >= cfg.today_max:
        return None
    return {
        "alert_type":     "account_no_spend",
        "severity":       cfg.severity,
        "direction":      "down",
        "today_value":    round(today_spend, 4),
        "baseline_value": round(baseline_avg, 2),
        "deviation_pct":  -1.0,
        "detail":         (f"ACCOUNT-LEVEL: Total spend stopped — "
                          f"${today_spend:.2f} today vs ${baseline_avg:,.2f}/day "
                          f"baseline. Whole account isn't running."),
    }


# ----- Rule 2: most ads in the account are rejected -----

@dataclass(frozen=True)
class AccountAllRejectedConfig:
    min_pct_disapproved: float = 0.50  # ≥ 50% of ads disapproved
    min_ad_count: int = 3              # but only fire if account has ≥ 3 ads
    severity: str = "CRITICAL"


def check_account_all_rejected(
    n_total_ads: int,
    n_disapproved_ads: int,
    config: Optional[AccountAllRejectedConfig] = None,
) -> Optional[Dict]:
    cfg = config or AccountAllRejectedConfig()
    if n_total_ads < cfg.min_ad_count:
        return None
    pct_disap = n_disapproved_ads / n_total_ads if n_total_ads else 0.0
    if pct_disap < cfg.min_pct_disapproved:
        return None
    return {
        "alert_type":     "account_all_rejected",
        "severity":       cfg.severity,
        "direction":      "n/a",
        "today_value":    float(n_disapproved_ads),
        "baseline_value": float(n_total_ads),
        "deviation_pct":  round(pct_disap, 4),
        "detail":         (f"ACCOUNT-LEVEL: {n_disapproved_ads} of {n_total_ads} ads "
                          f"({pct_disap*100:.0f}%) DISAPPROVED. Likely policy-sweep "
                          f"or landing-page rejection across the whole account."),
    }


# ----- Rule 3: account-aggregate metric spike (spend/CPC/CTR) -----

def check_account_metric_spike(
    metric: str,
    today_totals: Dict[str, float],
    baseline_totals: List[Dict[str, float]],
    config: Optional[AccountSpikeConfig] = None,
) -> Optional[Dict]:
    cfg = config or ACCOUNT_SPIKE_CONFIGS.get(metric)
    if cfg is None:
        return None
    if not baseline_totals:
        return None

    today = _aggregate_metrics(today_totals)
    baseline_per_day = [_aggregate_metrics(t) for t in baseline_totals]
    baseline_vals = [d[metric] for d in baseline_per_day]
    if metric != "spend":
        baseline_vals = [v for v in baseline_vals if v > 0]
    if not baseline_vals:
        return None

    baseline = sum(baseline_vals) / len(baseline_vals)
    today_val = today[metric]

    if baseline < cfg.baseline_min and metric == "spend":
        return None

    today_denom = _denominator(metric, today)
    if today_denom < cfg.today_volume_gate:
        return None

    low_threshold = baseline * cfg.low_mult
    high_threshold = baseline * cfg.high_mult
    direction: Optional[str] = None
    if today_val < low_threshold:
        direction = "down"
    elif today_val > high_threshold:
        direction = "up"
    if direction is None:
        return None

    deviation_pct = (today_val - baseline) / baseline if baseline else 0.0
    return {
        "alert_type":     f"account_{metric}_spike",
        "severity":       cfg.severity,
        "direction":      direction,
        "today_value":    round(today_val, 4),
        "baseline_value": round(baseline, 4),
        "deviation_pct":  round(deviation_pct, 4),
        "detail":         _format_account_detail(metric, today_val, baseline,
                                                 direction, deviation_pct),
    }


def _aggregate_metrics(totals: Dict[str, float]) -> Dict[str, float]:
    """Treat the totals dict as a 'whole day's aggregate' and derive CPC/CTR/CPA."""
    spend = float(totals.get("spend", 0) or 0)
    impressions = float(totals.get("impressions", 0) or 0)
    clicks = float(totals.get("clicks", 0) or 0)
    applies = float(totals.get("applies", 0) or 0)
    return {
        "spend":       spend,
        "impressions": impressions,
        "clicks":      clicks,
        "applies":     applies,
        "cpc":         spend / clicks if clicks > 0 else 0.0,
        "ctr":         clicks / impressions if impressions > 0 else 0.0,
        "cpa":         spend / applies if applies > 0 else 0.0,
    }


def _denominator(metric: str, today: Dict[str, float]) -> float:
    return {
        "spend": today["spend"],
        "cpc":   today["clicks"],
        "ctr":   today["impressions"],
        "cpa":   today["applies"],
    }.get(metric, 0.0)


def _format_account_detail(metric: str, today: float, baseline: float,
                          direction: str, deviation_pct: float) -> str:
    arrow = "+" if direction == "up" else "-"
    pct = abs(deviation_pct) * 100
    label_map = {
        "spend": ("Spend", lambda v: f"${v:,.2f}"),
        "cpc":   ("CPC",   lambda v: f"${v:.2f}"),
        "ctr":   ("CTR",   lambda v: f"{v*100:.2f}%"),
        "cpa":   ("Cost/LPV", lambda v: f"${v:.2f}"),
    }
    label, fmt = label_map.get(metric, (metric, str))
    return (f"ACCOUNT-LEVEL: aggregate {label} {arrow}{pct:.0f}% vs 7d avg "
            f"({fmt(today)} today vs {fmt(baseline)} baseline). Whole "
            f"account moved together — not just one ad.")
