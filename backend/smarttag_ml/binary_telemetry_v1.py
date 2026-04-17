"""
Decode SmartTag binary telemetry v1 frames (see SmartTag_fw/docs/telemetry-binary-v1.md).
"""

from __future__ import annotations

import struct
from typing import Any

HEADER_SIZE = 91
MAGIC = b"SMt1"
VERSION = 1


def decode_binary_telemetry_v1(payload: bytes) -> dict[str, Any] | None:
    """
    Parse binary frame. Returns dict with keys seq, ts_last_ms, dt_us, odr_hz, scenario_id, samples (list of dict),
    or None if not a v1 frame / invalid.
    """
    if len(payload) < HEADER_SIZE:
        return None
    head = payload[:HEADER_SIZE]
    magic, version, _res, n_samples, seq, ts_last_ms, dt_us, odr_hz, scen_len, scen_pad = struct.unpack(
        "<4sBBHIqIHB64s",
        head,
    )
    if magic != MAGIC or version != VERSION:
        return None
    if n_samples < 1 or n_samples > 128 or scen_len > 64:
        return None
    raw_scen = scen_pad[:scen_len]
    try:
        scenario_id = raw_scen.decode("utf-8")
    except UnicodeDecodeError:
        return None

    body_len = n_samples * 3 * 2
    if len(payload) < HEADER_SIZE + body_len:
        return None
    fmt = f"<{n_samples * 3}h"
    flat = struct.unpack_from(fmt, payload, HEADER_SIZE)
    samples: list[dict[str, float]] = []
    for i in range(n_samples):
        j = i * 3
        samples.append(
            {
                "x": float(flat[j]),
                "y": float(flat[j + 1]),
                "z": float(flat[j + 2]),
            }
        )

    return {
        "seq": seq,
        "ts_last_ms": int(ts_last_ms),
        "dt_us": int(dt_us),
        "odr_hz": int(odr_hz),
        "scenario_id": scenario_id,
        "samples": samples,
    }
