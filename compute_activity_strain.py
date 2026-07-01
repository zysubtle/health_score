"""
Activity-strain channel (standalone).

Heart-rate-independent activity-load contribution for the daily readiness
strain score. Intended to be combined with the heart-rate-based strain in the
caller via ``max(strain_hr, strain_activity)``. This module does NOT import,
modify, or depend on ``compute_strain`` / ``daily_scores.py``.

Input contract
--------------
``cumulative_steps`` is the day's cumulative step count that the UPSTREAM
pipeline has ALREADY aggregated under a low-heart-rate gate (only steps taken
during low-HR hours are included). This module performs no gating and no
per-hour anomaly filtering — do that upstream. It only maps an already-gated
step total to a bounded score.

Model
-----
    strain_activity = ACTIVITY_CAP * (1 - exp(-cumulative_steps / TAU_STEP))

A bounded, monotonic, saturating curve from the same family as the HR strain
map ``21 * (1 - exp(-trimp / tau))``, so the two channels share scale behaviour
and combine cleanly. Calibrated so that ANCHOR_STEPS -> ANCHOR_SCORE.

    e.g. 2000 -> 4.0, 4000 -> 6.0, 6000 -> 7.0, asymptote -> 8.0

Python version: 3.10+
"""

from __future__ import annotations

import math
from typing import Any, Optional

# --- Calibration ---------------------------------------------------------
ACTIVITY_CAP = 8.0          # upper bound (asymptote) of the activity channel
ANCHOR_STEPS = 4000.0       # calibration anchor: this many gated steps ...
ANCHOR_SCORE = 6.0          # ... maps to this activity-strain score

# Time constant derived so that ANCHOR_STEPS maps to ANCHOR_SCORE exactly.
# Solve ANCHOR_SCORE = ACTIVITY_CAP * (1 - exp(-ANCHOR_STEPS / TAU_STEP)) for TAU_STEP.
TAU_STEP = -ANCHOR_STEPS / math.log(1.0 - ANCHOR_SCORE / ACTIVITY_CAP)  # ~= 2885.39


def _ensure_number(value: Any) -> Optional[float]:
    """Return a finite float, or None for None / bool / NaN / Inf / non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def compute_activity_strain(cumulative_steps: Any) -> float:
    """Map an upstream low-HR-gated cumulative daily step count to a bounded
    activity-strain contribution in ``[0.0, ACTIVITY_CAP]``.

    Parameters
    ----------
    cumulative_steps:
        Day's cumulative step count, already aggregated upstream under a
        low-heart-rate gate. No gating or anomaly filtering is performed here.
        Negative / non-numeric / None input yields 0.0.

    Returns
    -------
    float
        Activity-strain in ``[0.0, ACTIVITY_CAP]``. Combine with the HR strain
        via ``max(strain_hr, strain_activity)``; apply the caller's existing
        rounding (e.g. round to 1 decimal) to the final combined value.
    """
    s = _ensure_number(cumulative_steps)
    if s is None or s < 0.0:
        return 0.0
    return ACTIVITY_CAP * (1.0 - math.exp(-s / TAU_STEP))


__all__ = ["compute_activity_strain", "ACTIVITY_CAP", "TAU_STEP"]