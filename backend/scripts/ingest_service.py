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
from typing import Any

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
from smarttag_ml.windowing import window_features_from_xyz  # noqa: E402

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


def ensure_feature_columns(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE feature_windows
            ADD COLUMN IF NOT EXISTS std_mag DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS peak_to_peak_mag DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS crest_factor DOUBLE PRECISION
            """
        )
    conn.commit()


@dataclass
class ModelRuntime:
    model_path: Path | None
    check_interval_s: float = 1.0
    model: Any = None
    last_mtime_ns: int | None = None
    last_check_monotonic: float = 0.0

    def refresh(self) -> Any:
        import time

        now = time.monotonic()
        if now - self.last_check_monotonic < self.check_interval_s:
            return self.model
        self.last_check_monotonic = now

        if self.model_path is None:
            self.model = None
            self.last_mtime_ns = None
            return None

        if not self.model_path.is_file():
            if self.model is not None:
                log.info("model file missing -> unloading IF model")
            self.model = None
            self.last_mtime_ns = None
            return None

        mtime_ns = self.model_path.stat().st_mtime_ns
        if self.last_mtime_ns == mtime_ns and self.model is not None:
            return self.model

        self.model = joblib.load(self.model_path)
        self.last_mtime_ns = mtime_ns
        log.info("loaded model from %s", self.model_path)
        return self.model


@dataclass
class DeviceBuffer:
    # each entry: x, y, z
    samples: list[tuple[float, float, float]] = field(default_factory=list)
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
        scenario_id: str,
        batch: list[dict],
        conn: psycopg.Connection,
        pipeline_version: str,
        model_runtime: ModelRuntime,
        rms_thresh: float,
    ) -> None:
        if self.last_seq is not None and seq != self.last_seq + 1:
            self.reset(f"seq gap expected={self.last_seq + 1} got={seq}")
        self.last_seq = seq
        self.last_dt_us = dt_us
        self.last_scenario = scenario_id

        for s in batch:
            self.samples.append((float(s["x"]), float(s["y"]), float(s["z"])))

        while len(self.samples) >= WINDOW_SAMPLES:
            chunk = self.samples[:WINDOW_SAMPLES]
            self.samples = self.samples[WINDOW_SAMPLES:]
            arr = np.array([[c[0], c[1], c[2]] for c in chunk], dtype=np.float64)
            feat = window_features_from_xyz(arr)
            rms = feat["rms_mag"]
            ws_dt = datetime.now(timezone.utc)

            model = model_runtime.refresh()
            if_outlier = None
            anomaly_score = None
            if model is not None:
                n_features = int(getattr(model, "n_features_in_", 1))
                if n_features == 1:
                    xrow = np.array([[feat["rms_mag"]]], dtype=np.float64)
                else:
                    xrow = np.array(
                        [[feat["rms_mag"], feat["std_mag"], feat["peak_to_peak_mag"], feat["crest_factor"]]],
                        dtype=np.float64,
                    )
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
                      device_id, scenario_id, window_start, rms_mag, std_mag, peak_to_peak_mag, crest_factor,
                      pipeline_version, if_outlier, anomaly_score
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (device_id, window_start) DO UPDATE SET
                      rms_mag = EXCLUDED.rms_mag,
                      std_mag = EXCLUDED.std_mag,
                      peak_to_peak_mag = EXCLUDED.peak_to_peak_mag,
                      crest_factor = EXCLUDED.crest_factor,
                      scenario_id = EXCLUDED.scenario_id,
                      if_outlier = EXCLUDED.if_outlier,
                      anomaly_score = EXCLUDED.anomaly_score,
                      received_at = now()
                    """,
                    (
                        device_id,
                        scenario_id,
                        ws_dt,
                        feat["rms_mag"],
                        feat["std_mag"],
                        feat["peak_to_peak_mag"],
                        feat["crest_factor"],
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
    conn, model_runtime, pipeline_version, rms_thresh = userdata
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
        scenario_id,
        batch,
        conn,
        pipeline_version,
        model_runtime,
        rms_thresh,
    )


def main() -> None:
    mqtt_host = os.environ.get("MQTT_HOST", "127.0.0.1")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    pipeline_version = os.environ.get("PIPELINE_VERSION", "v0")
    rms_thresh = float(os.environ.get("RMS_THRESH", "25"))
    model_path_raw = os.environ.get("MODEL_PATH", "").strip()
    model_path = Path(model_path_raw) if model_path_raw else None
    model_runtime = ModelRuntime(model_path=model_path)
    if model_path is None:
        log.info("MODEL_PATH not set — writing rows with if_outlier=NULL")
    else:
        model_runtime.refresh()
        if model_runtime.model is None:
            log.info("MODEL_PATH missing/untrained — writing rows with if_outlier=NULL")

    conn = psycopg.connect(pg_conninfo())
    ensure_feature_columns(conn)
    userdata = (conn, model_runtime, pipeline_version, rms_thresh)

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
