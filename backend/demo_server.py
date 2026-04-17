"""
Demo HTTP API + static UI: publish synthetic MQTT bursts, read aggregates from Postgres.
Requires: Docker (MQTT + Timescale), ingest_service.py running with MODEL_PATH for IF scores.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import psycopg
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from smarttag_ml.constants import SCENARIO_ASSEMBLED, SCENARIO_NO_BEARING  # noqa: E402
from smarttag_ml.synthetic_payload import build_payload  # noqa: E402

log = logging.getLogger("demo_server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_demo_last_seq: dict[str, int] = {}


def _deviation_env() -> tuple[float, float, float]:
    """RMS scale (mg) for 0..100% and minimum % when IF marks outlier."""
    low = float(os.environ.get("RMS_DEV_LOW", "5"))
    high = float(os.environ.get("RMS_DEV_HIGH", "85"))
    if high <= low:
        high = low + 1e-3
    floor = float(os.environ.get("IF_OUTLIER_PCT_FLOOR", "90"))
    return low, high, min(100.0, max(0.0, floor))


def pg_conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'smarttag')} "
        f"user={os.environ.get('POSTGRES_USER', 'smarttag')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def _scenario_id_from_key(key: str) -> str:
    if key == "assembled":
        return SCENARIO_ASSEMBLED
    if key == "no_bearing":
        return SCENARIO_NO_BEARING
    raise ValueError(key)


class DemoRunBody(BaseModel):
    scenario: Literal["assembled", "no_bearing"]
    duration_sec: float = Field(default=8.0, ge=1.0, le=120.0)
    device_id: str = Field(default_factory=lambda: os.environ.get("DEVICE_ID", "demo001"))
    publish_hz: float = Field(default=20.0, ge=1.0, le=50.0)


def _mqtt_burst(device_id: str, scenario_id: str, duration_sec: float, publish_hz: float) -> int:
    """Publish batches; returns last seq sent."""
    mqtt_host = os.environ.get("MQTT_HOST", "127.0.0.1")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    topic = f"smarttag/v1/{device_id}/telemetry/json"
    period = 1.0 / publish_hz
    rng = np.random.default_rng(42)

    last_seq = _demo_last_seq.get(device_id, 0)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"demo_ui_{device_id}")
    client.connect(mqtt_host, mqtt_port, keepalive=60)
    client.loop_start()
    try:
        import time as time_mod

        deadline = time_mod.monotonic() + duration_sec
        while time_mod.monotonic() < deadline:
            last_seq += 1
            payload = build_payload(last_seq, scenario_id, rng)
            client.publish(topic, json.dumps(payload), qos=1)
            time_mod.sleep(period)
    finally:
        client.loop_stop()
        client.disconnect()
    _demo_last_seq[device_id] = last_seq
    return last_seq


def _fetch_stats(device_id: str, scenario_id: str, since: datetime) -> dict:
    rms_low, rms_high, if_floor = _deviation_env()
    sql = """
        WITH w AS (
          SELECT
            rms_mag,
            anomaly_score,
            if_outlier,
            LEAST(
              100::double precision,
              GREATEST(
                0::double precision,
                100.0 * (rms_mag - %(rms_low)s) / GREATEST(%(rms_high)s - %(rms_low)s, 1e-9::double precision),
                CASE WHEN if_outlier IS TRUE THEN %(if_floor)s::double precision ELSE 0::double precision END
              )
            ) AS deviation_pct
          FROM feature_windows
          WHERE device_id = %(device_id)s
            AND scenario_id = %(scenario_id)s
            AND received_at >= %(since)s
        )
        SELECT
          COUNT(*)::int AS n_windows,
          AVG(rms_mag)::float AS avg_rms_mag,
          AVG(anomaly_score)::float AS avg_anomaly_score,
          COALESCE(SUM(CASE WHEN if_outlier THEN 1 ELSE 0 END), 0)::int AS n_if_outliers,
          MAX(rms_mag)::float AS max_rms_mag,
          AVG(w.deviation_pct)::float AS avg_deviation_pct,
          MAX(w.deviation_pct)::float AS max_deviation_pct
        FROM w
    """
    params = {
        "rms_low": rms_low,
        "rms_high": rms_high,
        "if_floor": if_floor,
        "device_id": device_id,
        "scenario_id": scenario_id,
        "since": since,
    }
    with psycopg.connect(pg_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    if not row:
        return {
            "n_windows": 0,
            "avg_rms_mag": None,
            "avg_anomaly_score": None,
            "n_if_outliers": 0,
            "max_rms_mag": None,
            "avg_deviation_pct": None,
            "max_deviation_pct": None,
        }
    n_windows, avg_rms, avg_score, n_out, max_rms, avg_dev, max_dev = row
    return {
        "n_windows": n_windows,
        "avg_rms_mag": round(float(avg_rms), 4) if avg_rms is not None else None,
        "avg_anomaly_score": round(float(avg_score), 6) if avg_score is not None else None,
        "n_if_outliers": n_out,
        "max_rms_mag": round(float(max_rms), 4) if max_rms is not None else None,
        "avg_deviation_pct": round(float(avg_dev), 1) if avg_dev is not None and n_windows else None,
        "max_deviation_pct": round(float(max_dev), 1) if max_dev is not None and n_windows else None,
    }


app = FastAPI(title="SmartTag demo UI API", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    try:
        with psycopg.connect(pg_conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        pg_ok = True
    except Exception as e:  # noqa: BLE001
        log.warning("pg health: %s", e)
        pg_ok = False
    low, high, fl = _deviation_env()
    return {
        "postgres": pg_ok,
        "model_path_set": bool(os.environ.get("MODEL_PATH", "").strip()),
        "deviation_scale": {"rms_low_mg": low, "rms_high_mg": high, "if_outlier_floor_pct": fl},
    }


@app.post("/api/demo/run")
async def demo_run(body: DemoRunBody) -> dict:
    scenario_id = _scenario_id_from_key(body.scenario)
    burst_start = datetime.now(timezone.utc)
    try:
        last_seq = await asyncio.to_thread(
            _mqtt_burst,
            body.device_id,
            scenario_id,
            body.duration_sec,
            body.publish_hz,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("mqtt burst failed")
        raise HTTPException(status_code=502, detail=f"MQTT publish failed: {e}") from e

    await asyncio.sleep(1.8)
    stats = await asyncio.to_thread(_fetch_stats, body.device_id, scenario_id, burst_start)
    low, high, fl = _deviation_env()
    deviation_scale = {"rms_low_mg": low, "rms_high_mg": high, "if_outlier_floor_pct": fl}
    return {
        "scenario": body.scenario,
        "scenario_id": scenario_id,
        "device_id": body.device_id,
        "duration_sec": body.duration_sec,
        "last_seq": last_seq,
        "burst_started_at": burst_start.isoformat(),
        "stats": stats,
        "deviation_scale": deviation_scale,
        "hint": (
            "Отклонение % (stats.avg_deviation_pct / max_deviation_pct): RMS от rms_low до rms_high мг "
            "→ линейно 0–100; при if_outlier не ниже if_outlier_floor_pct. "
            "Порог: env RMS_DEV_LOW, RMS_DEV_HIGH, IF_OUTLIER_PCT_FLOOR. "
            "Если n_windows=0 — запустите ingest_service.py; для IF нужен MODEL_PATH на ingest."
        ),
    }


@app.post("/api/demo/train")
async def demo_train() -> dict:
    """Run train_if.py in-process cwd (same env as server)."""

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "train_if.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env=os.environ.copy(),
        )

    try:
        proc = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="train_if.py timed out") from None
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
    }


_frontend = ROOT.parent / "frontend" / "demo"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="demo_ui")
