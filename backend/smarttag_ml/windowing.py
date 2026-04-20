"""Window features per contract v0 (DC removal per axis, then magnitude-domain features)."""

from __future__ import annotations

import numpy as np


def rms_mag_from_xyz(samples: np.ndarray) -> float:
    """
    samples: shape (N, 3) accelerations in mg (or any consistent unit).
    """
    if samples.shape[0] < 1 or samples.shape[1] != 3:
        raise ValueError("samples must be (N, 3)")
    x = samples[:, 0] - np.mean(samples[:, 0])
    y = samples[:, 1] - np.mean(samples[:, 1])
    z = samples[:, 2] - np.mean(samples[:, 2])
    return float(np.sqrt(np.mean(x * x + y * y + z * z)))


def window_features_from_xyz(samples: np.ndarray) -> dict[str, float]:
    """
    Build compact feature vector from one window.
    Returns: rms_mag, std_mag, peak_to_peak_mag, crest_factor.
    """
    if samples.shape[0] < 1 or samples.shape[1] != 3:
        raise ValueError("samples must be (N, 3)")

    x = samples[:, 0] - np.mean(samples[:, 0])
    y = samples[:, 1] - np.mean(samples[:, 1])
    z = samples[:, 2] - np.mean(samples[:, 2])
    mag = np.sqrt(x * x + y * y + z * z)

    rms_mag = float(np.sqrt(np.mean(mag * mag)))
    std_mag = float(np.std(mag))
    peak_to_peak_mag = float(np.max(mag) - np.min(mag))
    crest_factor = float(np.max(mag) / max(rms_mag, 1e-9))

    return {
        "rms_mag": rms_mag,
        "std_mag": std_mag,
        "peak_to_peak_mag": peak_to_peak_mag,
        "crest_factor": crest_factor,
    }


def window_start_ms(ts_last_ms: int, dt_us: int, n_samples: int) -> int:
    """First sample time in window (ms), per docs/09."""
    span_us = (n_samples - 1) * dt_us
    return int(ts_last_ms - span_us // 1000)
