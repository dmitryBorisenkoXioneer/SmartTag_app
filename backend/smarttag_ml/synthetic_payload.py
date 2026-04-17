"""Synthetic MQTT batch payloads (same JSON shape as MCU / simulate_mcu)."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from smarttag_ml.constants import DT_US_DEFAULT, MQTT_BATCH_SAMPLES_IF_OPTIMAL, ODR_HZ_DEFAULT, SCENARIO_ASSEMBLED


def build_payload(seq: int, scenario_id: str, rng: np.random.Generator, ts_last_ms: int | None = None) -> dict[str, Any]:
    """One JSON-serializable batch: MQTT_BATCH_SAMPLES_IF_OPTIMAL samples, mg, ~1 g bias on Z (DC per window in ingest)."""
    n = MQTT_BATCH_SAMPLES_IF_OPTIMAL
    if scenario_id == SCENARIO_ASSEMBLED:
        noise = 5.0
    else:
        noise = 40.0
    xyz = rng.standard_normal((n, 3)) * noise
    xyz[:, 2] += 1000.0
    samples = [{"x": float(x), "y": float(y), "z": float(z)} for x, y, z in xyz]
    last_ms = int(time.time() * 1000) if ts_last_ms is None else ts_last_ms
    return {
        "seq": seq,
        "ts_last_ms": last_ms,
        "dt_us": DT_US_DEFAULT,
        "odr_hz": ODR_HZ_DEFAULT,
        "scenario_id": scenario_id,
        "fw_version": "synthetic-0.1",
        "samples": samples,
    }
