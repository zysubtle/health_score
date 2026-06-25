
"""
Daily readiness scoring module.

This module implements a simplified production-ready version of:
- SleepPerformance: 0-100
- Recovery: 0-100
- Strain: 0-21

Design goals:
- No personal baseline
- Uses same-day inputs only
- Explicit piecewise formulas
- Deterministic rounding across runtimes
- No third-party dependencies

Python version: 3.10+
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping, Optional, Sequence, Dict, List

logger = logging.getLogger(__name__)


# =========================
# Enums and constants
# =========================


class StatusCode(IntEnum):
    SUCCESS = 0
    INVALID_TOTAL_SLEEP = 1001
    INVALID_REQUIRED_FIELD = 1002
    INVALID_DAY_HR_ARRAY = 1003


class WarningCode(IntEnum):
    SLEEP_STAGE_FALLBACK = 2001
    SLEEP_LOW_CONFIDENCE = 2002
    HRV_INVALID_FALLBACK = 2003
    RHR_INVALID_FALLBACK = 2004
    RECOVERY_PARTIAL_FALLBACK = 2005
    RECOVERY_FULL_FALLBACK = 2006
    STRAIN_RHR_FALLBACK = 2007
    STRAIN_LOW_CONFIDENCE = 2008
    NO_VALID_DAY_HR = 2009


HRV_VALID_MIN_MS = 5.0
HRV_VALID_MAX_MS = 250.0
RHR_VALID_MIN_BPM = 30.0
RHR_VALID_MAX_BPM = 100.0
DAY_HR_VALID_MIN_BPM = 35.0
DAY_HR_VALID_MAX_BPM = 220.0

TARGET_SLEEP_MIN = 480.0
DEEP_TARGET_MIN = 100.0
REM_TARGET_MIN = 110.0

W_SLEEP_SUFF = 0.45
W_SLEEP_REST = 0.25
W_SLEEP_CONT = 0.10
W_REST_DEEP = 0.5

SLEEP_SHAPE_ANCHOR = 90.0
SLEEP_SHAPE_P = 1.3
SLEEP_SHAPE_K = 3.0

HRV_LOW = [25.0, 22.0, 18.0, 15.0, 12.0]
HRV_HIGH = [80.0, 70.0, 60.0, 50.0, 45.0]

RHR_GOOD = [50.0, 52.0, 54.0, 56.0, 58.0]
RHR_POOR = [72.0, 74.0, 76.0, 78.0, 80.0]

W_REC_HRV = 0.45
W_REC_RHR = 0.25
W_REC_SLEEP = 0.30

LOW_INTENSITY_HRR_THRESHOLD = 0.30
TRIMP_A = 0.64
TRIMP_B = 1.92
STRAIN_TAU = 100.0
WALK_CADENCE_MIN_SPM = 40.0
WALK_CADENCE_MAX_SPM = 250.0


# =========================
# Helpers
# =========================


def _round_int(x: float) -> int:
    """Round to nearest int with .5 rounding up. Assumes non-negative input."""
    return int(math.floor(x + 0.5))


def _round_1(x: float) -> float:
    """Round to 1 decimal place with .05 rounding up. Assumes non-negative input."""
    return math.floor(x * 10.0 + 0.5) / 10.0


def _clamp_int(x: int, low: int, high: int) -> int:
    return min(max(x, low), high)


def _clamp_float(x: float, low: float, high: float) -> float:
    return min(max(x, low), high)


def _sanitize_age(age_years: Optional[int]) -> Optional[int]:
    if age_years is None:
        return None
    try:
        age = int(age_years)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > 120:
        return None
    return age


def age_bucket(age_years: Optional[int]) -> int:
    age = _sanitize_age(age_years)
    if age is None:
        return 1
    if age < 30:
        return 0
    if age < 40:
        return 1
    if age < 50:
        return 2
    if age < 60:
        return 3
    return 4


def _sleep_level(score: int) -> str:
    if score <= 59:
        return "poor"
    if score <= 79:
        return "fair"
    return "good"


def _sleep_shaped(value: float, target: float) -> float:
    """Map a sleep quantity to 0-100 against a target that anchors the 90 point.

    Convex penalty below target (deficits hurt more than linearly) and a
    saturating approach to 100 above it, so full marks are hard to reach.
    """
    if value <= 0.0 or target <= 0.0:
        return 0.0
    r = value / target
    if r < 1.0:
        s = SLEEP_SHAPE_ANCHOR * (r ** SLEEP_SHAPE_P)
    else:
        s = SLEEP_SHAPE_ANCHOR + (100.0 - SLEEP_SHAPE_ANCHOR) * (1.0 - math.exp(-SLEEP_SHAPE_K * (r - 1.0)))
    return _clamp_float(s, 0.0, 100.0)


def _recovery_level(score: int) -> str:
    if score <= 33:
        return "low"
    if score <= 66:
        return "medium"
    return "high"


def _strain_level(score: float) -> str:
    if score <= 5.9:
        return "very_low"
    if score <= 9.9:
        return "low"
    if score <= 14.9:
        return "moderate"
    if score <= 17.9:
        return "high"
    return "very_high"


def _ensure_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _normalize_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _is_valid_hrv_value(value: Optional[float]) -> bool:
    return value is not None and HRV_VALID_MIN_MS <= value <= HRV_VALID_MAX_MS


def _is_valid_rhr_value(value: Optional[float]) -> bool:
    return value is not None and RHR_VALID_MIN_BPM <= value <= RHR_VALID_MAX_BPM


def _resolve_hrv_valid(explicit_flag: Optional[bool], hrv_night_ms: Optional[float]) -> bool:
    # Production-safe behavior: explicit flag can only confirm validity if the numeric value itself is usable.
    if explicit_flag is True:
        return _is_valid_hrv_value(hrv_night_ms)
    if explicit_flag is False:
        return False
    return _is_valid_hrv_value(hrv_night_ms)


def _resolve_rhr_valid(explicit_flag: Optional[bool], rhr_sleep_bpm: Optional[float]) -> bool:
    # Production-safe behavior: explicit flag can only confirm validity if the numeric value itself is usable.
    if explicit_flag is True:
        return _is_valid_rhr_value(rhr_sleep_bpm)
    if explicit_flag is False:
        return False
    return _is_valid_rhr_value(rhr_sleep_bpm)


# =========================
# Data models
# =========================


@dataclass(frozen=True)
class DailyScoreInput:
    age_years: Optional[int]
    total_sleep_min: float
    deep_sleep_min: float
    rem_sleep_min: float
    sleep_stage_valid: bool
    hrv_night_ms: Optional[float]
    rhr_sleep_bpm: Optional[float]
    day_hr_minute_bpm: Sequence[Optional[float]]
    hrv_valid: Optional[bool] = None
    rhr_valid: Optional[bool] = None
    day_hr_coverage: Optional[float] = None
    day_step_minute_count: Optional[Sequence[Optional[float]]] = None
    waso_min: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DailyScoreInput":
        if not isinstance(data, Mapping):
            raise TypeError("input data must be a mapping")

        day_hr_values = data.get("day_hr_minute_bpm")
        if day_hr_values is None:
            raise ValueError("day_hr_minute_bpm is required")
        if isinstance(day_hr_values, (str, bytes)) or not isinstance(day_hr_values, Sequence):
            raise TypeError("day_hr_minute_bpm must be a sequence")

        normalized_day_hr: List[Optional[float]] = []
        for item in day_hr_values:
            normalized_day_hr.append(_ensure_number(item))

        day_step_values = data.get("day_step_minute_count")
        if isinstance(day_step_values, Sequence) and not isinstance(day_step_values, (str, bytes)):
            normalized_day_step: Optional[List[Optional[float]]] = [
                _ensure_number(item) for item in day_step_values
            ]
        else:
            normalized_day_step = None

        total_sleep_min = _ensure_number(data.get("total_sleep_min"))
        deep_sleep_min = _ensure_number(data.get("deep_sleep_min"))
        rem_sleep_min = _ensure_number(data.get("rem_sleep_min"))
        sleep_stage_valid = _normalize_bool(data.get("sleep_stage_valid"))
        if total_sleep_min is None or deep_sleep_min is None or rem_sleep_min is None or sleep_stage_valid is None:
            raise ValueError(
                "total_sleep_min, deep_sleep_min, rem_sleep_min, and sleep_stage_valid are required"
            )

        age_raw = data.get("age_years")
        age_years = _sanitize_age(age_raw if age_raw is None else int(age_raw)) if age_raw is not None else None

        return cls(
            age_years=age_years,
            total_sleep_min=total_sleep_min,
            deep_sleep_min=deep_sleep_min,
            rem_sleep_min=rem_sleep_min,
            sleep_stage_valid=sleep_stage_valid,
            hrv_night_ms=_ensure_number(data.get("hrv_night_ms")),
            rhr_sleep_bpm=_ensure_number(data.get("rhr_sleep_bpm")),
            day_hr_minute_bpm=normalized_day_hr,
            hrv_valid=_normalize_bool(data.get("hrv_valid")),
            rhr_valid=_normalize_bool(data.get("rhr_valid")),
            day_hr_coverage=_ensure_number(data.get("day_hr_coverage")),
            day_step_minute_count=normalized_day_step,
            waso_min=_ensure_number(data.get("waso_min")),
        )


@dataclass(frozen=True)
class SleepComponents:
    sleep_suff_score: float
    restorative_score: Optional[float]
    restorative_ratio: Optional[float]
    total_sleep_min: float
    deep_sleep_min: float
    rem_sleep_min: float
    deep_score: Optional[float] = None
    rem_score: Optional[float] = None
    continuity_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sleep_suff_score": self.sleep_suff_score,
            "restorative_score": self.restorative_score,
            "restorative_ratio": self.restorative_ratio,
            "total_sleep_min": self.total_sleep_min,
            "deep_sleep_min": self.deep_sleep_min,
            "rem_sleep_min": self.rem_sleep_min,
            "deep_score": self.deep_score,
            "rem_score": self.rem_score,
            "continuity_score": self.continuity_score,
        }


@dataclass(frozen=True)
class SleepPerformanceOutput:
    score: int
    level: str
    sleep_stage_fallback: bool
    low_confidence: bool
    components: SleepComponents
    warning_codes: tuple[WarningCode, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "sleep_stage_fallback": self.sleep_stage_fallback,
            "low_confidence": self.low_confidence,
            "components": self.components.to_dict(),
        }


@dataclass(frozen=True)
class RecoveryComponents:
    hrv_score: Optional[float]
    rhr_score: Optional[float]
    sleep_score: int
    hrv_night_ms: Optional[float]
    rhr_sleep_bpm: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hrv_score": self.hrv_score,
            "rhr_score": self.rhr_score,
            "sleep_score": self.sleep_score,
            "hrv_night_ms": self.hrv_night_ms,
            "rhr_sleep_bpm": self.rhr_sleep_bpm,
        }


@dataclass(frozen=True)
class RecoveryOutput:
    score: int
    level: str
    fallback_level: int
    components: RecoveryComponents
    warning_codes: tuple[WarningCode, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "fallback_level": self.fallback_level,
            "components": self.components.to_dict(),
        }


@dataclass(frozen=True)
class StrainComponents:
    trimp_day: float
    hrmax_est: float
    rhr_for_strain_bpm: float
    zone_minutes: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trimp_day": self.trimp_day,
            "hrmax_est": self.hrmax_est,
            "rhr_for_strain_bpm": self.rhr_for_strain_bpm,
            "zone_minutes": dict(self.zone_minutes),
        }


@dataclass(frozen=True)
class StrainOutput:
    score: float
    level: str
    valid: bool
    fallback_rhr: bool
    low_confidence: bool
    components: StrainComponents
    warning_codes: tuple[WarningCode, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "valid": self.valid,
            "fallback_rhr": self.fallback_rhr,
            "low_confidence": self.low_confidence,
            "components": self.components.to_dict(),
        }


@dataclass(frozen=True)
class DailyScoreOutput:
    sleep_performance: Optional[SleepPerformanceOutput]
    recovery: Optional[RecoveryOutput]
    strain: Optional[StrainOutput]
    status_code: StatusCode
    warning_codes: tuple[WarningCode, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sleep_performance": None if self.sleep_performance is None else self.sleep_performance.to_dict(),
            "recovery": None if self.recovery is None else self.recovery.to_dict(),
            "strain": None if self.strain is None else self.strain.to_dict(),
            "status_code": int(self.status_code),
            "warning_codes": [int(code) for code in self.warning_codes],
        }


# =========================
# Core computations
# =========================


def compute_sleep_performance(inp: DailyScoreInput) -> SleepPerformanceOutput:
    warnings: list[WarningCode] = []

    if inp.total_sleep_min <= 0:
        raise ValueError("total_sleep_min must be > 0")

    sleep_suff_score = _sleep_shaped(inp.total_sleep_min, TARGET_SLEEP_MIN)

    stage_usable = (
        inp.sleep_stage_valid
        and inp.deep_sleep_min >= 0.0
        and inp.rem_sleep_min >= 0.0
        and (inp.deep_sleep_min + inp.rem_sleep_min) <= inp.total_sleep_min
    )

    restorative_ratio: Optional[float] = None
    restorative_score: Optional[float] = None
    deep_score: Optional[float] = None
    rem_score: Optional[float] = None
    if stage_usable:
        restorative_ratio = (inp.deep_sleep_min + inp.rem_sleep_min) / inp.total_sleep_min
        deep_score = _sleep_shaped(inp.deep_sleep_min, DEEP_TARGET_MIN)
        rem_score = _sleep_shaped(inp.rem_sleep_min, REM_TARGET_MIN)
        restorative_score = W_REST_DEEP * deep_score + (1.0 - W_REST_DEEP) * rem_score

    sleep_stage_fallback = not stage_usable
    if sleep_stage_fallback:
        warnings.append(WarningCode.SLEEP_STAGE_FALLBACK)

    continuity_score: Optional[float] = None
    waso = _ensure_number(inp.waso_min)
    if waso is not None and waso >= 0.0:
        continuity_score = _clamp_float(100.0 * (1.0 - waso / inp.total_sleep_min), 0.0, 100.0)

    weighted = [(W_SLEEP_SUFF, sleep_suff_score)]
    if restorative_score is not None:
        weighted.append((W_SLEEP_REST, restorative_score))
    if continuity_score is not None:
        weighted.append((W_SLEEP_CONT, continuity_score))
    weight_sum = sum(w for w, _ in weighted)
    sleep_score_raw = sum(w * s for w, s in weighted) / weight_sum

    score = _clamp_int(_round_int(sleep_score_raw), 0, 100)

    low_confidence = inp.total_sleep_min < 180.0
    if low_confidence:
        warnings.append(WarningCode.SLEEP_LOW_CONFIDENCE)

    return SleepPerformanceOutput(
        score=score,
        level=_sleep_level(score),
        sleep_stage_fallback=sleep_stage_fallback,
        low_confidence=low_confidence,
        components=SleepComponents(
            sleep_suff_score=sleep_suff_score,
            restorative_score=restorative_score,
            restorative_ratio=restorative_ratio,
            total_sleep_min=inp.total_sleep_min,
            deep_sleep_min=inp.deep_sleep_min,
            rem_sleep_min=inp.rem_sleep_min,
            deep_score=deep_score,
            rem_score=rem_score,
            continuity_score=continuity_score,
        ),
        warning_codes=tuple(warnings),
    )


def _compute_hrv_score(age_years: Optional[int], hrv_night_ms: float) -> float:
    b = age_bucket(age_years)
    h_low = HRV_LOW[b]
    h_high = HRV_HIGH[b]
    h = hrv_night_ms

    if h <= h_low:
        return 0.0
    if h >= h_high:
        return 100.0
    return 100.0 * math.log(h / h_low) / math.log(h_high / h_low)


def _compute_rhr_score(age_years: Optional[int], rhr_sleep_bpm: float) -> float:
    b = age_bucket(age_years)
    r_good = RHR_GOOD[b]
    r_poor = RHR_POOR[b]
    r = rhr_sleep_bpm

    if r <= r_good:
        return 100.0
    if r >= r_poor:
        return 0.0
    return 100.0 * (r_poor - r) / (r_poor - r_good)


def compute_recovery(inp: DailyScoreInput, sleep_score: int) -> RecoveryOutput:
    warnings: list[WarningCode] = []

    hrv_valid = _resolve_hrv_valid(inp.hrv_valid, inp.hrv_night_ms)
    rhr_valid = _resolve_rhr_valid(inp.rhr_valid, inp.rhr_sleep_bpm)

    if not hrv_valid:
        warnings.append(WarningCode.HRV_INVALID_FALLBACK)
    if not rhr_valid:
        warnings.append(WarningCode.RHR_INVALID_FALLBACK)

    hrv_score: Optional[float] = None
    rhr_score: Optional[float] = None

    if hrv_valid and inp.hrv_night_ms is not None:
        hrv_score = _compute_hrv_score(inp.age_years, inp.hrv_night_ms)

    if rhr_valid and inp.rhr_sleep_bpm is not None:
        rhr_score = _compute_rhr_score(inp.age_years, inp.rhr_sleep_bpm)

    if hrv_score is not None and rhr_score is not None:
        recovery_raw = W_REC_HRV * hrv_score + W_REC_RHR * rhr_score + W_REC_SLEEP * float(sleep_score)
        fallback_level = 0
    elif rhr_score is not None:
        recovery_raw = 0.40 * rhr_score + 0.60 * float(sleep_score)
        fallback_level = 1
        warnings.append(WarningCode.RECOVERY_PARTIAL_FALLBACK)
    elif hrv_score is not None:
        recovery_raw = 0.60 * hrv_score + 0.40 * float(sleep_score)
        fallback_level = 1
        warnings.append(WarningCode.RECOVERY_PARTIAL_FALLBACK)
    else:
        recovery_raw = float(sleep_score)
        fallback_level = 2
        warnings.append(WarningCode.RECOVERY_FULL_FALLBACK)

    score = _clamp_int(_round_int(recovery_raw), 0, 100)

    return RecoveryOutput(
        score=score,
        level=_recovery_level(score),
        fallback_level=fallback_level,
        components=RecoveryComponents(
            hrv_score=hrv_score,
            rhr_score=rhr_score,
            sleep_score=sleep_score,
            hrv_night_ms=inp.hrv_night_ms,
            rhr_sleep_bpm=inp.rhr_sleep_bpm,
        ),
        warning_codes=tuple(warnings),
    )


def compute_strain(inp: DailyScoreInput) -> StrainOutput:
    warnings: list[WarningCode] = []

    b = age_bucket(inp.age_years)
    age = _sanitize_age(inp.age_years)
    hrmax_est = 208.0 - 0.7 * age if age is not None else 190.0

    rhr_valid = _resolve_rhr_valid(inp.rhr_valid, inp.rhr_sleep_bpm)
    if not rhr_valid:
        warnings.append(WarningCode.RHR_INVALID_FALLBACK)

    if rhr_valid and inp.rhr_sleep_bpm is not None:
        rhr_for_strain_bpm = inp.rhr_sleep_bpm
        fallback_rhr = False
    else:
        rhr_for_strain_bpm = 0.5 * (RHR_GOOD[b] + RHR_POOR[b])
        fallback_rhr = True
        warnings.append(WarningCode.STRAIN_RHR_FALLBACK)

    zone_minutes = {"z1": 0, "z2": 0, "z3": 0, "z4": 0, "z5": 0}
    trimp_day = 0.0
    valid_minutes = 0

    for idx, hr_raw in enumerate(inp.day_hr_minute_bpm):
        hr = _ensure_number(hr_raw)
        if hr is None or hr < DAY_HR_VALID_MIN_BPM or hr > DAY_HR_VALID_MAX_BPM:
            continue

        valid_minutes += 1

        if hrmax_est <= rhr_for_strain_bpm or hr <= rhr_for_strain_bpm:
            hrr = 0.0
        elif hr >= hrmax_est:
            hrr = 1.0
        else:
            hrr = (hr - rhr_for_strain_bpm) / (hrmax_est - rhr_for_strain_bpm)

        if 0.30 <= hrr < 0.50:
            zone_minutes["z1"] += 1
        elif 0.50 <= hrr < 0.60:
            zone_minutes["z2"] += 1
        elif 0.60 <= hrr < 0.70:
            zone_minutes["z3"] += 1
        elif 0.70 <= hrr < 0.80:
            zone_minutes["z4"] += 1
        elif 0.80 <= hrr <= 1.00:
            zone_minutes["z5"] += 1

        step_active = False
        if inp.day_step_minute_count is not None and idx < len(inp.day_step_minute_count):
            steps_m = _ensure_number(inp.day_step_minute_count[idx])
            if steps_m is not None and WALK_CADENCE_MIN_SPM <= steps_m <= WALK_CADENCE_MAX_SPM:
                step_active = True

        if hrr < LOW_INTENSITY_HRR_THRESHOLD and not step_active:
            load_m = 0.0
        else:
            load_m = hrr * TRIMP_A * math.exp(TRIMP_B * hrr)

        trimp_day += load_m

    if valid_minutes == 0:
        warnings.append(WarningCode.NO_VALID_DAY_HR)
        low_confidence = True
        return StrainOutput(
            score=0.0,
            level=_strain_level(0.0),
            valid=False,
            fallback_rhr=fallback_rhr,
            low_confidence=low_confidence,
            components=StrainComponents(
                trimp_day=0.0,
                hrmax_est=hrmax_est,
                rhr_for_strain_bpm=rhr_for_strain_bpm,
                zone_minutes=zone_minutes,
            ),
            warning_codes=tuple(warnings),
        )

    strain_raw = 21.0 * (1.0 - math.exp(-trimp_day / STRAIN_TAU))
    strain_score = _round_1(_clamp_float(strain_raw, 0.0, 21.0))

    low_confidence = inp.day_hr_coverage is not None and inp.day_hr_coverage < 0.5
    if low_confidence:
        warnings.append(WarningCode.STRAIN_LOW_CONFIDENCE)

    return StrainOutput(
        score=strain_score,
        level=_strain_level(strain_score),
        valid=True,
        fallback_rhr=fallback_rhr,
        low_confidence=low_confidence,
        components=StrainComponents(
            trimp_day=trimp_day,
            hrmax_est=hrmax_est,
            rhr_for_strain_bpm=rhr_for_strain_bpm,
            zone_minutes=zone_minutes,
        ),
        warning_codes=tuple(warnings),
    )


# =========================
# Top-level API
# =========================


def compute_daily_score(inp: DailyScoreInput) -> DailyScoreOutput:
    if inp.total_sleep_min <= 0:
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=StatusCode.INVALID_TOTAL_SLEEP,
            warning_codes=tuple(),
        )

    if inp.day_hr_minute_bpm is None:
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=StatusCode.INVALID_REQUIRED_FIELD,
            warning_codes=tuple(),
        )

    if isinstance(inp.day_hr_minute_bpm, (str, bytes)) or not isinstance(inp.day_hr_minute_bpm, Sequence):
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=StatusCode.INVALID_DAY_HR_ARRAY,
            warning_codes=tuple(),
        )

    try:
        sleep_out = compute_sleep_performance(inp)
        recovery_out = compute_recovery(inp, sleep_out.score)
        strain_out = compute_strain(inp)
    except Exception:
        logger.exception("daily score computation failed")
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=StatusCode.INVALID_REQUIRED_FIELD,
            warning_codes=tuple(),
        )

    merged_warnings = tuple(
        sorted(
            set(
                list(sleep_out.warning_codes)
                + list(recovery_out.warning_codes)
                + list(strain_out.warning_codes)
            ),
            key=int,
        )
    )

    return DailyScoreOutput(
        sleep_performance=sleep_out,
        recovery=recovery_out,
        strain=strain_out,
        status_code=StatusCode.SUCCESS,
        warning_codes=merged_warnings,
    )


def compute_daily_score_from_dict(data: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Convenience wrapper for JSON-like dict input/output.
    Never raises for normal validation issues; fatal validation problems are encoded via status_code.
    """
    try:
        inp = DailyScoreInput.from_dict(data)
    except ValueError as exc:
        message = str(exc)
        if "day_hr_minute_bpm" in message:
            status = StatusCode.INVALID_DAY_HR_ARRAY
        else:
            status = StatusCode.INVALID_REQUIRED_FIELD
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=status,
            warning_codes=tuple(),
        ).to_dict()
    except TypeError:
        return DailyScoreOutput(
            sleep_performance=None,
            recovery=None,
            strain=None,
            status_code=StatusCode.INVALID_REQUIRED_FIELD,
            warning_codes=tuple(),
        ).to_dict()

    return compute_daily_score(inp).to_dict()


__all__ = [
    "DailyScoreInput",
    "DailyScoreOutput",
    "SleepPerformanceOutput",
    "RecoveryOutput",
    "StrainOutput",
    "StatusCode",
    "WarningCode",
    "compute_sleep_performance",
    "compute_recovery",
    "compute_strain",
    "compute_daily_score",
    "compute_daily_score_from_dict",
]

