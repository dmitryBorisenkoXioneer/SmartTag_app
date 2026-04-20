#!/usr/bin/env python3
"""
Train IsolationForest on rms_mag rows (assembled only), save joblib.
Matches SmartTag_fw/docs/critical-decisions-v0.md whitelist.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
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


def _session_start_env() -> datetime | None:
    raw = os.environ.get("TRAIN_SESSION_START", "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid TRAIN_SESSION_START: {raw}") from exc


def _as_matrix(rows: list[tuple[float, float, float, float]]) -> np.ndarray:
    return np.array(rows, dtype=np.float64)


def _robust_mask(x: np.ndarray, mad_k: float, max_delta_rms: float) -> np.ndarray:
    rms = x[:, 0]
    med = np.median(rms)
    mad = np.median(np.abs(rms - med))
    robust_sigma = max(1.4826 * mad, 1e-9)
    z = np.abs(rms - med) / robust_sigma
    z_mask = z <= mad_k

    delta = np.zeros_like(rms)
    delta[1:] = np.abs(np.diff(rms))
    d_mask = delta <= max_delta_rms

    return np.logical_and(z_mask, d_mask)


def _split_train_holdout(x: np.ndarray, holdout_ratio: float) -> tuple[np.ndarray, np.ndarray]:
    n = x.shape[0]
    holdout_n = max(1, int(round(n * holdout_ratio)))
    holdout_n = min(holdout_n, n - 1)
    train_n = n - holdout_n
    return x[:train_n], x[train_n:]


def _quality_label(score: float) -> str:
    if score >= 85.0:
        return "good"
    if score >= 70.0:
        return "degraded"
    return "poor"


def main() -> None:
    device_id = os.environ.get("TRAIN_DEVICE_ID", "demo001")
    pipeline_version = os.environ.get("PIPELINE_VERSION", "v0")
    min_windows = int(os.environ.get("MIN_TRAIN_WINDOWS", "50"))
    min_critical_windows = int(os.environ.get("TRAIN_MIN_CRITICAL_WINDOWS", "120"))
    contamination = float(os.environ.get("IF_CONTAMINATION", "0.01"))
    train_mad_k = float(os.environ.get("TRAIN_MAD_K", "3.5"))
    train_max_delta_rms = float(os.environ.get("TRAIN_MAX_DELTA_RMS", "15.0"))
    holdout_ratio = float(os.environ.get("TRAIN_HOLDOUT_RATIO", "0.2"))
    max_holdout_outlier_rate = float(os.environ.get("MAX_HOLDOUT_OUTLIER_RATE", "0.03"))
    out_path = Path(os.environ.get("MODEL_PATH", str(ROOT / "artifacts/model_v0.joblib")))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    session_start = _session_start_env()

    sql = """
        SELECT rms_mag, std_mag, peak_to_peak_mag, crest_factor
        FROM feature_windows
        WHERE device_id = %s
          AND scenario_id = %s
          AND pipeline_version = %s
          AND (%s::timestamptz IS NULL OR received_at >= %s::timestamptz)
          AND std_mag IS NOT NULL
          AND peak_to_peak_mag IS NOT NULL
          AND crest_factor IS NOT NULL
        ORDER BY received_at
    """
    with psycopg.connect(pg_conninfo()) as conn:
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
        with conn.cursor() as cur:
            cur.execute(sql, (device_id, SCENARIO_ASSEMBLED, pipeline_version, session_start, session_start))
            rows = [(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in cur.fetchall()]

    if len(rows) < min_windows:
        if len(rows) < min_critical_windows:
            raise SystemExit(
                f"critical: need at least ~{min_critical_windows} raw windows, got {len(rows)}"
            )
        print(
            f"warning: requested min_windows={min_windows}, using available raw windows={len(rows)}",
            flush=True,
        )

    X_raw = _as_matrix(rows)
    mask = _robust_mask(X_raw, mad_k=train_mad_k, max_delta_rms=train_max_delta_rms)
    X = X_raw[mask]
    dropped = int(X_raw.shape[0] - X.shape[0])
    if X.shape[0] < min_windows:
        if X.shape[0] >= min_critical_windows:
            print(
                f"warning: after quality filter windows={X.shape[0]} < requested min_windows={min_windows}; "
                "continuing with filtered set",
                flush=True,
            )
        elif X_raw.shape[0] >= min_critical_windows:
            print(
                f"warning: filtered windows={X.shape[0]} below critical threshold, "
                f"falling back to raw windows={X_raw.shape[0]}",
                flush=True,
            )
            X = X_raw
            dropped = 0
        else:
            raise SystemExit(
                f"critical: not enough windows after filter/raw fallback, filtered={X.shape[0]} raw={X_raw.shape[0]}"
            )

    raw_rows = int(X_raw.shape[0])
    clean_rows = int(X.shape[0])
    dirty_ratio = max(0.0, min(1.0, float(dropped) / max(1, raw_rows)))
    quality_score = round(100.0 * (1.0 - dirty_ratio), 1)
    quality_label = _quality_label(quality_score)

    x_train, x_holdout = _split_train_holdout(X, holdout_ratio=holdout_ratio)
    if x_train.shape[0] < 2 or x_holdout.shape[0] < 1:
        raise SystemExit("not enough windows for train/holdout split")

    clf = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=42,
    )
    clf.fit(x_train)

    holdout_pred = clf.predict(x_holdout)
    holdout_outlier_rate = float(np.mean(holdout_pred == -1))
    if holdout_outlier_rate > max_holdout_outlier_rate:
        print(
            "warning: holdout check failed; "
            f"outlier_rate={holdout_outlier_rate:.4f} > max={max_holdout_outlier_rate:.4f}; "
            "saving model anyway (non-critical mode)",
            flush=True,
        )

    joblib.dump(clf, out_path)
    print(
        "wrote "
        f"{out_path} trained_on={x_train.shape[0]} holdout={x_holdout.shape[0]} "
        f"holdout_outlier_rate={holdout_outlier_rate:.4f} "
        f"raw_rows={raw_rows} clean_rows={clean_rows} dropped={dropped} "
        f"dirty_ratio={dirty_ratio:.4f} dtus_ok_ratio=na quality_score={quality_score:.1f} "
        f"quality_label={quality_label} session_start={session_start}",
        flush=True,
    )


if __name__ == "__main__":
    main()
