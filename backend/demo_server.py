"""
Real-device HTTP API + static UI for one ESP32 SmartTag.
Training uses only the latest session and auto-switches to detection.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("demo_server")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEVICE_ID = os.environ.get("DEVICE_ID", "esp32dev001")
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "v0")
TARGET_WINDOWS = int(os.environ.get("MIN_TRAIN_WINDOWS", "50"))
MODEL_PATH = Path(os.environ.get("MODEL_PATH", str(ROOT / "artifacts/model_v0.joblib")))
SCENARIO_ASSEMBLED = "stepper_5rps_assembled"
DEVICE_ONLINE_WINDOW_S = float(os.environ.get("DEVICE_ONLINE_WINDOW_S", "5"))
AUTO_START_INGEST = os.environ.get("AUTO_START_INGEST", "1").strip() not in {"0", "false", "False"}

_ingest_proc: subprocess.Popen[str] | None = None


@dataclass
class TrainingResult:
    ok: bool | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    session_start: str | None = None
    stop_reason: str | None = None
    finished_at: str | None = None


@dataclass
class TrainingState:
    device_id: str
    target_windows: int
    model_state: str
    training_enabled: bool = False
    training_in_progress: bool = False
    auto_stop_triggered: bool = False
    training_started_at: datetime | None = None
    training_finished_at: datetime | None = None
    transition_reason: str = "Нет обученной модели."
    last_result: TrainingResult | None = None
    active_job_id: int = 0


class TrainingConfigPayload(BaseModel):
    target_windows: int = Field(ge=10, le=50000)


_state_lock = threading.Lock()
_state = TrainingState(
    device_id=DEVICE_ID,
    target_windows=TARGET_WINDOWS,
    model_state="detecting" if MODEL_PATH.is_file() else "untrained",
    transition_reason="Детектирование активно." if MODEL_PATH.is_file() else "Нет обученной модели.",
)

def _deviation_env() -> tuple[float, float, float]:
    low = float(os.environ.get("RMS_DEV_LOW", "5"))
    high = float(os.environ.get("RMS_DEV_HIGH", "85"))
    if high <= low:
        high = low + 1e-3
    floor = float(os.environ.get("IF_OUTLIER_PCT_FLOOR", "90"))
    return low, high, min(100.0, max(0.0, floor))


def _deviation_pct(rms_mag: float | None, if_outlier: bool | None) -> float | None:
    if rms_mag is None:
        return None
    low, high, if_floor = _deviation_env()
    span = max(high - low, 1e-9)
    pct = 100.0 * (float(rms_mag) - low) / span
    pct = min(100.0, max(0.0, pct))
    if if_outlier:
        pct = max(pct, if_floor)
    return round(pct, 1)


def pg_conninfo() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'smarttag')} "
        f"user={os.environ.get('POSTGRES_USER', 'smarttag')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def _external_ingest_running() -> bool:
    owned_pid = _ingest_proc.pid if _ingest_proc is not None and _ingest_proc.poll() is None else None
    return any(pid != owned_pid for pid in _list_ingest_pids())


def _list_ingest_pids() -> list[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-fal", "ingest_service.py"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []

    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid_text, cmdline = parts
        if "ingest_service.py" not in cmdline:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        pids.append(pid)
    return pids


def _terminate_ingest_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        log.warning("cannot terminate duplicate ingest_service pid=%s", pid)
        return
    log.warning("terminated duplicate ingest_service pid=%s", pid)


def _cleanup_duplicate_ingest_processes() -> None:
    owned_pid = _ingest_proc.pid if _ingest_proc is not None and _ingest_proc.poll() is None else None
    pids = _list_ingest_pids()
    if len(pids) <= 1:
        return

    keep_pid = owned_pid if owned_pid in pids else max(pids)
    for pid in pids:
        if pid != keep_pid:
            _terminate_ingest_pid(pid)


def _ingest_status() -> dict:
    _cleanup_duplicate_ingest_processes()
    launched_here = _ingest_proc is not None
    running_here = launched_here and _ingest_proc.poll() is None
    return {
        "auto_start_enabled": AUTO_START_INGEST,
        "launched_by_ui": launched_here,
        "running": bool(running_here or _external_ingest_running()),
        "pid": _ingest_proc.pid if running_here and _ingest_proc is not None else None,
    }


def _ensure_ingest_running() -> None:
    global _ingest_proc

    if not AUTO_START_INGEST:
        return
    _cleanup_duplicate_ingest_processes()
    if _ingest_proc is not None and _ingest_proc.poll() is None:
        return
    if _external_ingest_running():
        return

    _ingest_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "ingest_service.py")],
        cwd=str(ROOT),
        env=os.environ.copy(),
    )
    log.info("started ingest_service pid=%s", _ingest_proc.pid)


def _stop_ingest_if_owned() -> None:
    global _ingest_proc
    if _ingest_proc is None:
        return
    if _ingest_proc.poll() is None:
        _ingest_proc.terminate()
        try:
            _ingest_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _ingest_proc.kill()
            _ingest_proc.wait(timeout=5)
    _ingest_proc = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _ensure_ingest_running()
    yield
    _stop_ingest_if_owned()


app = FastAPI(title="SmartTag live UI API", version="0.2", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _training_snapshot() -> dict:
    with _state_lock:
        snapshot = {
            "device_id": _state.device_id,
            "target_windows": _state.target_windows,
            "model_state": _state.model_state,
            "training_enabled": _state.training_enabled,
            "training_in_progress": _state.training_in_progress,
            "auto_stop_triggered": _state.auto_stop_triggered,
            "training_started_at": _serialize_dt(_state.training_started_at),
            "training_finished_at": _serialize_dt(_state.training_finished_at),
            "transition_reason": _state.transition_reason,
            "last_result": asdict(_state.last_result) if _state.last_result is not None else None,
        }
    return snapshot


def _delete_model_file() -> None:
    if MODEL_PATH.is_file():
        MODEL_PATH.unlink()
        log.info("deleted previous model %s", MODEL_PATH)


def _count_training_windows(since: datetime | None) -> int:
    if since is None:
        return 0
    sql = """
        SELECT COUNT(*)::int
        FROM feature_windows
        WHERE device_id = %s
          AND scenario_id = %s
          AND pipeline_version = %s
          AND received_at >= %s
    """
    with psycopg.connect(pg_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (DEVICE_ID, SCENARIO_ASSEMBLED, PIPELINE_VERSION, since))
            row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _fetch_latest_window() -> dict | None:
    sql = """
        SELECT device_id, scenario_id, window_start, received_at, rms_mag, if_outlier, anomaly_score
        FROM feature_windows
        WHERE device_id = %s
        ORDER BY received_at DESC
        LIMIT 1
    """
    with psycopg.connect(pg_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (DEVICE_ID,))
            row = cur.fetchone()
    if not row:
        return None
    device_id, scenario_id, window_start, received_at, rms_mag, if_outlier, anomaly_score = row
    return {
        "device_id": device_id,
        "scenario_id": scenario_id,
        "window_start": window_start.isoformat(),
        "received_at": received_at.isoformat(),
        "rms_mag": round(float(rms_mag), 4),
        "if_outlier": bool(if_outlier) if if_outlier is not None else None,
        "anomaly_score": round(float(anomaly_score), 6) if anomaly_score is not None else None,
        "deviation_pct": _deviation_pct(float(rms_mag), bool(if_outlier) if if_outlier is not None else None),
        "status_label": "Отклонение" if if_outlier else "Норма",
    }


def _mask_latest_window_for_mode(latest: dict | None, mode: str) -> dict | None:
    if latest is None or mode == "detecting":
        return latest
    latest = dict(latest)
    latest["if_outlier"] = None
    latest["anomaly_score"] = None
    latest["deviation_pct"] = None
    latest["status_label"] = "Модель неактивна"
    return latest


def _device_status(latest: dict | None) -> dict:
    if latest is None or latest.get("received_at") is None:
        return {
            "online": False,
            "label": "Оффлайн",
            "last_seen_at": None,
            "last_seen_ago_sec": None,
            "online_window_sec": DEVICE_ONLINE_WINDOW_S,
        }

    received_at = datetime.fromisoformat(latest["received_at"])
    age_s = max(0.0, (datetime.now(timezone.utc) - received_at).total_seconds())
    online = age_s <= DEVICE_ONLINE_WINDOW_S
    return {
        "online": online,
        "label": "Онлайн" if online else "Оффлайн",
        "last_seen_at": latest["received_at"],
        "last_seen_ago_sec": round(age_s, 1),
        "online_window_sec": DEVICE_ONLINE_WINDOW_S,
    }


def _run_training_job(job_id: int, session_start: datetime, stop_reason: str) -> None:
    env = os.environ.copy()
    env["TRAIN_DEVICE_ID"] = DEVICE_ID
    env["TRAIN_SESSION_START"] = session_start.isoformat()
    env["MIN_TRAIN_WINDOWS"] = str(TARGET_WINDOWS)

    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "train_if.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        ok = proc.returncode == 0
        result = TrainingResult(
            ok=ok,
            returncode=proc.returncode,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            session_start=session_start.isoformat(),
            stop_reason=stop_reason,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except subprocess.TimeoutExpired:
        result = TrainingResult(
            ok=False,
            returncode=None,
            stdout="",
            stderr="train_if.py timed out",
            session_start=session_start.isoformat(),
            stop_reason=stop_reason,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    with _state_lock:
        if _state.active_job_id != job_id:
            return
        _state.training_enabled = False
        _state.training_in_progress = False
        _state.training_finished_at = datetime.now(timezone.utc)
        _state.last_result = result
        _state.model_state = "detecting" if result.ok else "untrained"
        if result.ok:
            _state.transition_reason = "Обучение завершено. Детектирование активно."
        else:
            _state.transition_reason = "Обучение завершилось с ошибкой. Модель не обучена."
    log.info("training job finished ok=%s reason=%s", result.ok, stop_reason)


def _start_training_job(stop_reason: str) -> None:
    with _state_lock:
        if _state.training_in_progress:
            return
        session_start = _state.training_started_at
        if session_start is None:
            raise HTTPException(status_code=409, detail="training session is not active")
        _state.training_enabled = False
        _state.training_in_progress = True
        _state.auto_stop_triggered = stop_reason == "auto"
        _state.model_state = "training"
        _state.transition_reason = (
            "Данных достаточно, обучаем модель..." if stop_reason == "auto" else "Обучаем модель..."
        )
        _state.active_job_id += 1
        job_id = _state.active_job_id
    thread = threading.Thread(target=_run_training_job, args=(job_id, session_start, stop_reason), daemon=True)
    thread.start()


def _maybe_trigger_auto_training(windows_collected: int) -> None:
    with _state_lock:
        should_trigger = (
            _state.training_enabled
            and not _state.training_in_progress
            and _state.training_started_at is not None
            and windows_collected >= _state.target_windows
        )
    if should_trigger:
        _start_training_job("auto")


def _live_status() -> dict:
    _ensure_ingest_running()
    snapshot = _training_snapshot()
    started_at = (
        datetime.fromisoformat(snapshot["training_started_at"]) if snapshot["training_started_at"] is not None else None
    )
    windows_collected = _count_training_windows(started_at)
    _maybe_trigger_auto_training(windows_collected)
    snapshot = _training_snapshot()

    progress_pct = None
    if snapshot["training_enabled"] or snapshot["training_in_progress"]:
        progress_pct = round(min(100.0, 100.0 * windows_collected / max(snapshot["target_windows"], 1)), 1)

    latest = _mask_latest_window_for_mode(_fetch_latest_window(), snapshot["model_state"])
    device_status = _device_status(latest)
    return {
        "device_id": DEVICE_ID,
        "device_status": device_status,
        "ingest": _ingest_status(),
        "training": {
            "enabled": snapshot["training_enabled"],
            "in_progress": snapshot["training_in_progress"],
            "auto_stop_triggered": snapshot["auto_stop_triggered"],
            "started_at": snapshot["training_started_at"],
            "finished_at": snapshot["training_finished_at"],
            "target_windows": snapshot["target_windows"],
            "windows_collected": windows_collected,
            "progress_pct": progress_pct,
        },
        "mode": snapshot["model_state"],
        "transition_reason": snapshot["transition_reason"],
        "latest_window": latest,
        "last_training_result": snapshot["last_result"],
        "deviation_scale": {
            "rms_low_mg": _deviation_env()[0],
            "rms_high_mg": _deviation_env()[1],
            "if_outlier_floor_pct": _deviation_env()[2],
        },
    }


@app.get("/api/health")
def health() -> dict:
    _ensure_ingest_running()
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
        "device_id": DEVICE_ID,
        "model_path": str(MODEL_PATH),
        "model_path_set": bool(os.environ.get("MODEL_PATH", "").strip()),
        "model_file_exists": MODEL_PATH.is_file(),
        "target_windows": TARGET_WINDOWS,
        "device_online_window_sec": DEVICE_ONLINE_WINDOW_S,
        "ingest": _ingest_status(),
        "deviation_scale": {"rms_low_mg": low, "rms_high_mg": high, "if_outlier_floor_pct": fl},
    }


@app.get("/api/live/status")
def live_status() -> dict:
    return _live_status()


@app.post("/api/live/training/start")
def live_training_start() -> dict:
    with _state_lock:
        if _state.training_in_progress:
            raise HTTPException(status_code=409, detail="training job already running")
        _state.training_enabled = True
        _state.auto_stop_triggered = False
        _state.training_started_at = datetime.now(timezone.utc)
        _state.training_finished_at = None
        _state.last_result = None
        _state.model_state = "untrained"
        _state.transition_reason = "Сбор данных для обучения..."
        _state.active_job_id += 1
    _delete_model_file()
    return _live_status()


@app.post("/api/live/training/stop")
def live_training_stop() -> dict:
    with _state_lock:
        if _state.training_in_progress:
            raise HTTPException(status_code=409, detail="training job already running")
        if not _state.training_enabled or _state.training_started_at is None:
            raise HTTPException(status_code=409, detail="training session is not active")
    _start_training_job("manual")
    return _live_status()


@app.post("/api/live/training/config")
def live_training_config(payload: TrainingConfigPayload) -> dict:
    with _state_lock:
        if _state.training_enabled or _state.training_in_progress:
            raise HTTPException(status_code=409, detail="cannot change target while training is active")
        _state.target_windows = int(payload.target_windows)
    return _live_status()


_frontend = ROOT.parent / "frontend" / "demo"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="demo_ui")
