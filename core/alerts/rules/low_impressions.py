"""Low-impressions alert — fires when delivery collapses below viability.

Rule is **relative + absolute** to work across platforms with very
different volume profiles without per-platform tuning:
  * Meta ad with 50,000/day baseline → fires when today < 15,000 (30%)
  * LinkedIn ad with 100/day baseline → fires when today < 30 (30%)
  * Google ad with 300/day baseline → fires when today < 90 (30%)

Per-client overrides via `client.alert_overrides.low_impressions` are
supported — same pattern as the spike rule.
"""

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Sequence


@dataclass(frozen=True)
class LowImpressionsConfig:
    today_max_pct: float = 0.30      # fire if today < this fraction of baseline
    baseline_min: float = 100.0      # AND baseline avg ≥ this (must've been delivering)
    severity: str = "CRITICAL"


DEFAULT = LowImpressionsConfig()


def get_low_impressions_config(client_config: Optional[Dict[str, Any]] = None
                              ) -> LowImpressionsConfig:
    if not client_config:
        return DEFAULT
    overrides = (client_config.get("alert_overrides", {})
                 .get("low_impressions", {}))
    if not overrides:
        return DEFAULT
    allowed = {"today_max_pct", "baseline_min", "severity"}
    valid = {k: v for k, v in overrides.items() if k in allowed}
    return replace(DEFAULT, **valid) if valid else DEFAULT


def check_low_impressions(today_row, baseline_rows: Sequence,
                          client_config: Optional[Dict[str, Any]] = None,
                          config: Optional[LowImpressionsConfig] = None
                          ) -> Optional[Dict]:
    """Fires when delivery drops to under `today_max_pct` of baseline AND
    baseline was meaningfully delivering (≥ `baseline_min`/day)."""
    cfg = config or get_low_impressions_config(client_config)
    if not baseline_rows:
        return None

    today_imp = int(today_row["impressions"] or 0)
    baseline_vals = [int(r["impressions"] or 0) for r in baseline_rows]
    nonzero = [v for v in baseline_vals if v > 0]
    if not nonzero:
        return None
    baseline = sum(nonzero) / len(nonzero)

    if baseline < cfg.baseline_min:
        return None

    relative_threshold = baseline * cfg.today_max_pct
    if today_imp >= relative_threshold:
        return None  # delivery within normal range

    drop_pct = (today_imp - baseline) / baseline if baseline else 0.0
    return {
        "alert_type":      "low_impressions",
        "severity":        cfg.severity,
        "direction":       "down",
        "today_value":     float(today_imp),
        "baseline_value":  round(baseline, 1),
        "deviation_pct":   round(drop_pct, 4),
        "detail":          (f"Impressions collapsed — {today_imp:,} today vs "
                            f"{int(baseline):,}/day baseline "
                            f"({abs(drop_pct)*100:.0f}% drop). Possible "
                            f"delivery issue: budget out, audience saturated, "
                            f"ad disapproved, or auction loss."),
    }
