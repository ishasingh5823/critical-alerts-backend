"""Ad rejections cluster rule.

Fires when ≥N ads in a single campaign have effective_status = DISAPPROVED
observed within the trailing window. This catches policy sweeps that hit
multiple ads in a campaign at once — much more actionable than a single-ad
disapproval that the platform's own UI will surface anyway.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class RejectionsConfig:
    min_disapproved: int = 3
    window_hours: int = 24
    severity: str = "CRITICAL"


DEFAULT_CONFIG = RejectionsConfig()


def check_rejections(disapproved_count: int,
                    config: Optional[RejectionsConfig] = None) -> Optional[Dict]:
    """Fire if disapproved_count >= threshold.

    Args:
      disapproved_count: distinct ads with DISAPPROVED status in the window
      config: optional override for the threshold

    Returns:
      Partial alert dict on trigger, else None.
    """
    cfg = config or DEFAULT_CONFIG
    if disapproved_count < cfg.min_disapproved:
        return None
    return {
        "alert_type":      "rejections_cluster",
        "severity":        cfg.severity,
        "direction":       "n/a",
        "today_value":     float(disapproved_count),
        "baseline_value":  float(cfg.min_disapproved),
        "deviation_pct":   None,
        "detail":          (f"{disapproved_count} ads DISAPPROVED in this "
                            f"campaign within the last {cfg.window_hours}h"),
    }
