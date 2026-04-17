#!/usr/bin/env python3
"""
MQTT -> sample buffer -> windows of 256 -> RMS -> TimescaleDB + optional IF score.
Publishers: simulate_mcu.py, replay_csv (later), or ESP32 — same contract.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import paho.mqtt.client as mqtt
import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smarttag_ml.binary_telemetry_v1 import decode_binary_telemetry_v1  # noqa: E402
from smarttag_ml.constants import WINDOW_SAMPLES  # noqa: E402
from smarttag_ml.windowing import rms_mag_from_xyz  # noqa: E402

load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("ingest")


def pg_conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'smarttag')} "
        f"user={os.environ.get('POSTGRES_USER', 'smarttag')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def _batch_sample_times(ts_last_ms: int, dt_us: int, n: int) -> list[float]:
    """Wall time (ms) of each sample in batch, first -> last within batch."""
    dt_ms = dt_us / 1000.0
    t0 = ts_last_ms - (n - 1) * dt_ms
    return [t0 + i * dt_ms for i in range(n)]


@dataclass
class DeviceBuffer:
    # each entry: x, y, z, t_ms (float, wall ms)
    samples: list[tuple[float, float, float, float]] = field(default_factory=list)
    last_seq: int | None = None
    last_scenario: str = ""
    last_dt_us: int = 300

    def reset(self, reason: str) -> None:
        log.warning("buffer reset (%s), dropped %d samples", reason, len(self.samples))
        self.samples.clear()

    def append_batch(
        self,
        device_id: str,
        seq: int,
        dt_us: int,
        ts_last_ms: int,
        scenario_id: str,
        batch: list[dict],
        conn: psycopg.Connection,
        pipeline_version: str,
        model,
        rms_thresh: float,
    ) -> None:
        if self.last_seq is not None and seq != self.last_seq + 1:
            self.reset(f"seq gap expected={self.last_seq + 1} got={seq}")
        self.last_seq = seq
        self.last_dt_us = dt_us
        self.last_scenario = scenario_id

        times = _batch_sample_times(ts_last_ms, dt_us, len(batch))
        for s, t_ms in zip(batch, times, strict=True):
            self.samples.append((float(s["x"]), float(s["y"]), float(s["z"]), t_ms))

        while len(self.samples) >= WINDOW_SAMPLES:
            chunk = self.samples[:WINDOW_SAMPLES]
            self.samples = self.samples[WINDOW_SAMPLES:]
            arr = np.array([[c[0], c[1], c[2]] for c in chunk], dtype=np.float64)
            rms = rms_mag_from_xyz(arr)
            ws_ms = int(round(chunk[0][3]))
            ws_dt = datetime.fromtimestamp(ws_ms / 1000.0, tz=timezone.utc)

            if_outlier = None
            anomaly_score = None
            if model is not None:
                xrow = np.array([[rms]], dtype=np.float64)
                pred = int(model.predict(xrow)[0])
                if_outlier = pred == -1
                anomaly_score = float(model.decision_function(xrow)[0])

            rms_alert = rms >= rms_thresh
            is_alert = rms_alert
            if if_outlier is not None:
                is_alert = bool(if_outlier or rms_alert)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feature_windows (
                      device_id, scenario_id, window_start, rms_mag,
                      pipeline_version, if_outlier, anomaly_score
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (device_id, window_start) DO UPDATE SET
                      rms_mag = EXCLUDED.rms_mag,
                      scenario_id = EXCLUDED.scenario_id,
                      if_outlier = EXCLUDED.if_outlier,
                      anomaly_score = EXCLUDED.anomaly_score,
                      received_at = now()
                    """,
                    (
                        device_id,
                        scenario_id,
                        ws_dt,
                        rms,
                        pipeline_version,
                        if_outlier,
                        anomaly_score,
                    ),
                )
            conn.commit()
            log.info(
                "device=%s rms=%.3f scenario=%s if_outlier=%s rms_alert=%s is_alert=%s",
                device_id,
                rms,
                scenario_id,
                if_outlier,
                rms_alert,
                is_alert,
            )


buffers: dict[str, DeviceBuffer] = {}


def on_message(client, userdata, msg):  # noqa: ARG001
    conn, model, pipeline_version, rms_thresh = userdata
    parts = msg.topic.split("/")
    if len(parts) < 5 or parts[0] != "smarttag" or parts[1] != "v1":
        return
    device_id = parts[2]
    is_binary = len(parts) >= 5 and parts[3] == "telemetry" and parts[4] == "bin"
    if is_binary:
        body = decode_binary_telemetry_v1(msg.payload)
        if body is None:
            log.error("bad binary telemetry (topic=%s len=%d)", msg.topic, len(msg.payload))
            return
    else:
        try:
            body = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.error("bad json: %s", e)
            return

    seq = int(body["seq"])
    dt_us = int(body["dt_us"])
    ts_last_ms = int(body["ts_last_ms"])
    scenario_id = str(body["scenario_id"])
    batch = body["samples"]
    if not isinstance(batch, list) or not batch:
        log.error("empty samples")
        return

    if device_id not in buffers:
        buffers[device_id] = DeviceBuffer()
    buffers[device_id].append_batch(
        device_id,
        seq,
        dt_us,
        ts_last_ms,
        scenario_id,
        batch,
        conn,
        pipeline_version,
        model,
        rms_thresh,
    )


def main() -> None:
    mqtt_host = os.environ.get("MQTT_HOST", "127.0.0.1")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    pipeline_version = os.environ.get("PIPELINE_VERSION", "v0")
    rms_thresh = float(os.environ.get("RMS_THRESH", "25"))
    model_path = os.environ.get("MODEL_PATH", "").strip()
    model = None
    if model_path and Path(model_path).is_file():
        model = joblib.load(model_path)
        log.info("loaded model from %s", model_path)
    else:
        log.info("MODEL_PATH not set or missing — writing rows with if_outlier=NULL")

    conn = psycopg.connect(pg_conninfo())
    userdata = (conn, model, pipeline_version, rms_thresh)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ingest_service")
    client.user_data_set(userdata)
    client.on_message = on_message
    client.connect(mqtt_host, mqtt_port, keepalive=60)
    client.subscribe("smarttag/v1/+/telemetry/json", qos=1)
    client.subscribe("smarttag/v1/+/telemetry/bin", qos=1)
    log.info("subscribed smarttag/v1/+/telemetry/json + .../telemetry/bin, PG ok")
    client.loop_forever()


if __name__ == "__main__":
    main()
