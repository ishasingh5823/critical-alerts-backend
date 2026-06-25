"""Bidirectional spike detection with **dynamic, per-ad** volume gates.

The gate scales to each ad's own baseline. A 30%-of-baseline floor means
the same rule works on a Meta ad doing 50,000 impressions/day AND a
LinkedIn ad doing 100 impressions/day — no platform-specific tuning
needed, no client-specific tuning needed, because each ad's "normal"
volume IS its own scale.

Hierarchy of config resolution (highest priority wins):
  1. `client.alert_overrides.spike.<metric>.<field>` in clients/<id>.json
     (e.g. Kenvue can request low_mult=0.4 to be more sensitive)
  2. Default `_DEFAULT_CONFIGS` here.

For spend specifically, the rule branches by magnitude into:
  * spend_zero     — spend ≈ 0 against active baseline (CRITICAL)
  * spend_runaway  — spend > 2× baseline AND > $30 today (CRITICAL)
  * spend_spike    — moderate deviation (50-100%) in either direction

Gate rules:
  * `baseline_volume_min` — baseline avg denominator (clicks/imp/applies)
    must be at least this to consider the rate meaningful at all.
  * `today_pct_of_baseline` — today's denominator must be at least this
    fraction of the baseline avg. Prevents alerts on days where the ad
    barely ran (1 imp 1 click = 100% CTR is not a real signal).
  * `baseline_min` — for spend, baseline spend itself must meet this $/day.
"""

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Sequence


@dataclass(frozen=True)
class SpikeConfig:
    """Per-metric thresholds. low_mult/high_mult are deviation thresholds
    (multipliers vs baseline mean). Volume gates are RELATIVE to the
    ad's own baseline (scale-free) — keeps the rule generalizable across
    platforms and ad sizes."""
    metric: str
    low_mult: float
    high_mult: float
    # Baseline avg of the denominator (clicks/impressions/applies) — must
    # meet this absolute floor to consider the rate worth evaluating.
    # Below this, the ad just doesn't have enough volume for the metric
    # to be meaningful even on a "normal" day.
    baseline_volume_min: float = 0.0
    # Today's denominator must be at least this FRACTION of the baseline
    # average. Filters out low-volume days where today's rate is noisy.
    today_pct_of_baseline: float = 0.30
    # For spend metric only — baseline spend itself must meet this.
    baseline_min: float = 0.0
    severity: str = "WARNING"


# v2.1 defaults (2026-05-27 night). PER-AD RELATIVE thresholds — every ad
# is judged against its own 7-day baseline. Standardized at ±40% deviation
# across metrics per user request: "if an ad goes beyond 40% of its own
# baseline, alert".
#   low_mult=0.6  → fire on 40%+ drop  (today < 0.6 × baseline)
#   high_mult=1.4 → fire on 40%+ rise  (today > 1.4 × baseline)
_DEFAULT_CONFIGS: Dict[str, SpikeConfig] = {
    "spend": SpikeConfig(
        metric="spend", low_mult=0.6, high_mult=1.4,   # ±40%
        baseline_min=5.0,
        today_pct_of_baseline=0.0,
        severity="CRITICAL",
    ),
    "cpc":   SpikeConfig(
        metric="cpc", low_mult=0.6, high_mult=1.4,     # ±40%
        baseline_volume_min=10,
        today_pct_of_baseline=0.3,
    ),
    "ctr":   SpikeConfig(
        metric="ctr", low_mult=0.6, high_mult=1.4,     # ±40%
        baseline_volume_min=50,
        today_pct_of_baseline=0.3,
    ),
    "cpa":   SpikeConfig(
        metric="cpa", low_mult=0.6, high_mult=1.4,     # ±40%
        baseline_volume_min=2,
        today_pct_of_baseline=0.3,
        severity="CRITICAL",
    ),
}


def get_spike_config(metric: str,
                    client_config: Optional[Dict[str, Any]] = None
                    ) -> SpikeConfig:
    """Resolve the SpikeConfig for `metric`, applying per-client overrides
    from the client JSON if present.

    Client JSON format (optional):
        {
          "id": "kenvue",
          ...
          "alert_overrides": {
            "spike": {
              "ctr":  { "low_mult": 0.5, "high_mult": 1.7 },
              "spend": { "baseline_min": 10.0 }
            }
          }
        }
    """
    cfg = _DEFAULT_CONFIGS[metric]
    if not client_config:
        return cfg
    overrides = (client_config.get("alert_overrides", {})
                 .get("spike", {})
                 .get(metric, {}))
    if not overrides:
        return cfg
    # Only allow whitelisted fields — guards against typos in client JSON
    allowed = {"low_mult", "high_mult", "baseline_volume_min",
              "today_pct_of_baseline", "baseline_min", "severity"}
    valid = {k: v for k, v in overrides.items() if k in allowed}
    return replace(cfg, **valid) if valid else cfg


# Kept for backward compat — engine.py iterates SPIKE_CONFIGS.keys() to
# know what metrics to evaluate.
SPIKE_CONFIGS = _DEFAULT_CONFIGS


def _derive_metrics(row) -> Dict[str, float]:
    spend = float(row["spend"] or 0)
    impressions = float(row["impressions"] or 0)
    clicks = float(row["clicks"] or 0)
    applies = float(row["applies"] or 0)
    return {
        "spend":       spend,
        "impressions": impressions,
        "clicks":      clicks,
        "applies":     applies,
        "cpc":         spend / clicks   if clicks   > 0 else 0.0,
        "ctr":         clicks / impressions if impressions > 0 else 0.0,
        "cpa":         spend / applies  if applies  > 0 else 0.0,
    }


def check_spike(metric: str, today_row, baseline_rows: Sequence,
               client_config: Optional[Dict[str, Any]] = None,
               config: Optional[SpikeConfig] = None) -> Optional[Dict]:
    """Run the spike check for one metric on one ad."""
    cfg = config or get_spike_config(metric, client_config)
    if not baseline_rows:
        return None

    today = _derive_metrics(today_row)
    baseline_vals = [_derive_metrics(r)[metric] for r in baseline_rows]
    if metric != "spend":
        baseline_vals = [v for v in baseline_vals if v > 0]
    if not baseline_vals:
        return None

    baseline = sum(baseline_vals) / len(baseline_vals)
    today_val = today[metric]

    # ---- Volume gates ----
    if baseline < cfg.baseline_min:
        return None

    # Baseline volume gate — the DENOMINATOR avg, not the metric itself
    baseline_denom = _baseline_denominator(metric, baseline_rows)
    if baseline_denom < cfg.baseline_volume_min:
        return None

    # Today's volume gate — relative to baseline (% of) — keeps the rule
    # scale-free across platforms and ad sizes.
    today_denom = _denominator(metric, today)
    relative_floor = baseline_denom * cfg.today_pct_of_baseline
    is_spend_zero_case = (metric == "spend" and today_val == 0)
    if not is_spend_zero_case and today_denom < relative_floor:
        return None

    # ---- SPEND: branch on magnitude into spend_zero / spend_runaway / spend_spike ----
    if metric == "spend":
        if today_val < 0.50 and baseline >= 10.0:
            return {
                "alert_type":      "spend_zero",
                "severity":        "CRITICAL",
                "direction":       "down",
                "today_value":     round(today_val, 4),
                "baseline_value":  round(baseline, 4),
                "deviation_pct":   -1.0,
                "detail":          (f"Spend stopped — only ${today_val:.2f} today vs "
                                    f"${baseline:.2f}/day baseline. Ad may be paused, "
                                    f"disapproved, or out of budget."),
            }
        if today_val > 2.0 * baseline and today_val > 30.0:
            mult = today_val / baseline
            return {
                "alert_type":      "spend_runaway",
                "severity":        "CRITICAL",
                "direction":       "up",
                "today_value":     round(today_val, 4),
                "baseline_value":  round(baseline, 4),
                "deviation_pct":   round(mult - 1.0, 4),
                "detail":          (f"Spend runaway — ${today_val:,.2f} today, "
                                    f"{mult:.1f}× the ${baseline:,.2f}/day baseline. "
                                    f"Check for accidental budget edits."),
            }

    # ---- Standard spike check ----
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
        "alert_type":      f"{metric}_spike",
        "severity":        cfg.severity,
        "direction":       direction,
        "today_value":     round(today_val, 4),
        "baseline_value":  round(baseline, 4),
        "deviation_pct":   round(deviation_pct, 4),
        "detail":          _format_detail(metric, today_val, baseline,
                                          direction, deviation_pct),
    }


def _denominator(metric: str, today: Dict[str, float]) -> float:
    return {
        "spend": today["spend"],
        "cpc":   today["clicks"],
        "ctr":   today["impressions"],
        "cpa":   today["applies"],
    }.get(metric, 0.0)


def _baseline_denominator(metric: str, baseline_rows: Sequence) -> float:
    """Mean of the denominator across baseline rows (skipping zero days
    for non-spend metrics)."""
    if not baseline_rows:
        return 0.0
    field = {"spend": "spend", "cpc": "clicks", "ctr": "impressions",
             "cpa": "applies"}.get(metric)
    if field is None:
        return 0.0
    vals = [float(r[field] or 0) for r in baseline_rows]
    if metric != "spend":
        vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _format_detail(metric: str, today: float, baseline: float,
                  direction: str, deviation_pct: float) -> str:
    """Format the human-readable detail. The phrase 'vs this ad's 7d avg'
    is intentional — the comparison is ALWAYS per-ad, never against
    portfolio averages or platform medians."""
    arrow = "+" if direction == "up" else "-"
    pct = abs(deviation_pct) * 100
    if metric == "spend":
        return (f"Spend {arrow}{pct:.0f}% vs this ad's 7d avg "
                f"(${today:,.2f} today vs ${baseline:,.2f} baseline)")
    if metric == "cpc":
        return (f"CPC {arrow}{pct:.0f}% vs this ad's 7d avg "
                f"(${today:.2f} today vs ${baseline:.2f} baseline)")
    if metric == "ctr":
        return (f"CTR {arrow}{pct:.0f}% vs this ad's 7d avg "
                f"({today*100:.2f}% today vs {baseline*100:.2f}% baseline)")
    if metric == "cpa":
        return (f"Cost/LPV {arrow}{pct:.0f}% vs this ad's 7d avg "
                f"(${today:.2f} today vs ${baseline:.2f} baseline)")
    return f"{metric} {arrow}{pct:.0f}% vs this ad's 7d avg"
