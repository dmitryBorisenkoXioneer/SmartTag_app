#!/usr/bin/env python3
"""
Train IsolationForest on rms_mag rows (assembled only), save joblib.
Matches SmartTag_fw/docs/09-critical-decisions-v0.md whitelist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import joblib
import numpy as np
import psycopg
from dotenv import load_dotenv
from sklearn.ensemble import IsolationForest

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from smarttag_ml.constants import SCENARIO_ASSEMBLED  # noqa: E402


def pg_conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'smarttag')} "
        f"user={os.environ.get('POSTGRES_USER', 'smarttag')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def main() -> None:
    device_id = os.environ.get("TRAIN_DEVICE_ID", "demo001")
    pipeline_version = os.environ.get("PIPELINE_VERSION", "v0")
    out_path = Path(os.environ.get("MODEL_PATH", str(ROOT / "artifacts/model_v0.joblib")))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sql = """
        SELECT rms_mag FROM feature_windows
        WHERE device_id = %s
          AND scenario_id = %s
          AND pipeline_version = %s
        ORDER BY window_start
    """
    with psycopg.connect(pg_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (device_id, SCENARIO_ASSEMBLED, pipeline_version))
            rows = [float(r[0]) for r in cur.fetchall()]

    if len(rows) < 50:
        raise SystemExit(f"need at least ~50 windows, got {len(rows)} — run simulate_mcu + ingest longer")

    X = np.array(rows, dtype=np.float64).reshape(-1, 1)
    clf = IsolationForest(
        n_estimators=200,
        contamination=0.05,
        random_state=42,
    )
    clf.fit(X)
    joblib.dump(clf, out_path)
    print(f"wrote {out_path} trained on n={len(rows)} rows", flush=True)


if __name__ == "__main__":
    main()
