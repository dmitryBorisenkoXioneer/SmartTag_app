"""
Map RMS + optional IF outlier flag to a single 0..100% deviation index for UI / alerts.

RMS is linearly scaled between rms_low (0%) and rms_high (100%), clamped.
If the model marks a window as an outlier, the index is at least if_outlier_floor (still capped at 100).
"""

from __future__ import annotations


def deviation_pct(
    rms_mag: float,
    if_outlier: bool | None,
    *,
    rms_low: float,
    rms_high: float,
    if_outlier_floor: float = 90.0,
) -> float:
    """Return deviation in [0, 100]."""
    span = max(float(rms_high) - float(rms_low), 1e-9)
    rms_part = 100.0 * (float(rms_mag) - float(rms_low)) / span
    rms_part = max(0.0, min(100.0, rms_part))
    if if_outlier is True:
        return min(100.0, max(rms_part, float(if_outlier_floor)))
    return rms_part
