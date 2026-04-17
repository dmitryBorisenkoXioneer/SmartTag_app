"""Window RMS per contract v0 (DC removal per axis, then vector RMS)."""

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


def window_start_ms(ts_last_ms: int, dt_us: int, n_samples: int) -> int:
    """First sample time in window (ms), per docs/09."""
    span_us = (n_samples - 1) * dt_us
    return int(ts_last_ms - span_us // 1000)
