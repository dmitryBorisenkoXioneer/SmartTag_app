# SmartTag back-end (v0 smoke)

Один **ingest** на MQTT → окна **256** → **RMS** → TimescaleDB → опционально **IsolationForest** (`train_if.py`). Источник MQTT: **`scripts/simulate_mcu.py`**, позже ESP32 или `replay_csv.py` — контракт [SmartTag_fw/docs/critical-decisions-v0.md](../../SmartTag_fw/docs/critical-decisions-v0.md). Оглавление плана: [SmartTag_fw/docs/plan.md](../../SmartTag_fw/docs/plan.md).

**Полный разбор алгоритма (шаги, SQL, IF, багфикс симулятора):** [docs/SMOKE_PIPELINE_ALGORITHM.md](./docs/SMOKE_PIPELINE_ALGORITHM.md).

## Границы модулей (переиспользование)

| Компонент | Роль |
|-----------|------|
| `smarttag_ml/` | Константы и формула RMS — общие для ingest / train / тестов |
| `smarttag_ml/synthetic_payload.py` | Тот же JSON-пачки, что у MCU-симулятора (используют `simulate_mcu` и demo UI) |
| `smarttag_ml/binary_telemetry_v1.py` | Декод бинарных кадров v1 → тот же вид, что JSON для ingest ([telemetry-binary-v1.md](../../SmartTag_fw/docs/telemetry-binary-v1.md)) |
| `smarttag_ml/deviation.py` | Индекс отклонения 0–100% по RMS + опция «пол IF» (дублирует логику SQL в `demo_server`) |
| `scripts/ingest_service.py` | MQTT `…/telemetry/json` и `…/telemetry/bin` → буфер → окна → БД → `predict` |
| `scripts/simulate_mcu.py` | Только publisher (как ESP32) |
| `scripts/train_if.py` | Офлайн fit + `joblib` |
| `demo_server.py` | Мини-API + статика: публикация пачек в MQTT, сводка по `feature_windows`, опционально вызов `train_if.py` |

## 1. Инфраструктура

Из репозитория **SmartTag_fw** (рядом на диске):

```bash
cd /path/to/SmartTag_fw
docker compose -f deploy/docker-compose.yml up -d
```

Проверка: `PGPASSWORD=smarttag_local_only psql -h localhost -U smarttag -d smarttag -c "SELECT extname FROM pg_extension WHERE extname='timescaledb';"`

## 2. Миграция

```bash
cd /path/to/SmartTag_app/backend
cp .env.example .env
PGPASSWORD=smarttag_local_only psql -h localhost -U smarttag -d smarttag -f migrations/001_feature_windows.sql
```

## 3. Python

```bash
cd /path/to/SmartTag_app/backend
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Прогон smoke

Терминал A — ingest:

```bash
cd /path/to/SmartTag_app/backend
source .venv/bin/activate
# MODEL_PATH не задан — пишем только rms_mag (if_outlier NULL)
python scripts/ingest_service.py
```

Терминал B — симулятор «норма»:

```bash
cd /path/to/SmartTag_app/backend
source .venv/bin/activate
python scripts/simulate_mcu.py --scenario assembled --hz 20
```

Подождать **~1–2 минуты** (нужно **≥50** окон для train). Остановить симулятор (Ctrl+C).

## 5. Обучение IF

```bash
cd /path/to/SmartTag_app/backend
source .venv/bin/activate
export MODEL_PATH=artifacts/model_v0.joblib
python scripts/train_if.py
```

## 6. Инференс

Перезапустить ingest **с** `MODEL_PATH` в `.env` (или `export MODEL_PATH=...`). Снова запустить симулятор:

- `--scenario assembled` — ожидаем в основном `if_outlier=False`.
- `--scenario no_bearing` — ожидаем чаще `if_outlier=True` и/или `rms_alert=True` (при необходимости снизить `RMS_THRESH` в `.env`).

## 7. Демо-интерфейс (кнопки «с подшипником» / «без»)

Нужны **Docker** (MQTT + Postgres), отдельный процесс **`python scripts/ingest_service.py`** (для колонок IF — тот же `MODEL_PATH`, что после `train_if.py`). Затем:

```bash
cd /path/to/SmartTag_app/backend
source .venv/bin/activate
uvicorn demo_server:app --host 127.0.0.1 --port 8787
```

Браузер: **http://127.0.0.1:8787/** — страница из `frontend/demo/`: кнопки вызывают `POST /api/demo/run` (пачки в MQTT ~8 с), затем читается агрегат из БД за этот прогон (`avg_rms_mag`, доля `if_outlier`, средний `anomaly_score`). **Отклонение в %** (`avg_deviation_pct`, `max_deviation_pct`): линейно по RMS между **`RMS_DEV_LOW`** и **`RMS_DEV_HIGH`** (мг) → 0–100, при **`if_outlier`** не ниже **`IF_OUTLIER_PCT_FLOOR`** (см. `.env.example`). Кнопка «Обучить IF» дергает `POST /api/demo/train` (обёртка над `scripts/train_if.py`).

## Критерий «можно ехать дальше»

См. чеклист в [SmartTag_fw/docs/milestones-and-verification.md](../../SmartTag_fw/docs/milestones-and-verification.md) (блоки C/E) и цели плана в Cursor `backend_ml_smoke_test`.
