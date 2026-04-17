# Алгоритм локального smoke-теста (Docker → MQTT → окна → БД → IF)

Документ описывает **последовательность действий и логику**, которые были выполнены для проверки цепочки «как у реального ESP32», без прошивки. Контракт полей и чисел: [SmartTag_fw/docs/critical-decisions-v0.md](../../../SmartTag_fw/docs/critical-decisions-v0.md). Номинальный период MQTT при полной пачке 128 и ODR 3332: **§1.1** того же файла. Оглавление всего плана: [SmartTag_fw/docs/plan.md](../../../SmartTag_fw/docs/plan.md).

---

## 1. Цель

Проверить **интеграцию модели в контуре данных**: MQTT-пачки → буфер сэмплов → окна фиксированной длины → **RMS** → запись в **TimescaleDB** → обучение **Isolation Forest** только на «норме» → **инференс** на потоке + запасной порог **RMS_THRESH**.

---

## 2. Инфраструктура

1. Запуск контейнеров из репозитория **SmartTag_fw**: `docker compose -f deploy/docker-compose.yml up -d`.
2. Сервисы: **PostgreSQL + TimescaleDB** (порт 5432), **Eclipse Mosquitto** (1883).
3. Приложение Python на **хосте** подключается к `localhost` (не внутри Docker).

---

## 3. Схема БД (миграция)

Файл `migrations/001_feature_windows.sql`:

1. `CREATE EXTENSION IF NOT EXISTS timescaledb`.
2. Таблица **`feature_windows`**: `device_id`, `scenario_id`, `window_start`, `rms_mag`, `pipeline_version`, `received_at`, опционально `if_outlier`, `anomaly_score`.
3. Первичный ключ **`(device_id, window_start)`** — одно окно на момент времени на устройство.
4. **`SELECT create_hypertable('feature_windows', 'window_start', if_not_exists => TRUE)`** — ряд по времени для TimescaleDB.

Применение: `docker exec -i smarttag-timescaledb psql -U smarttag -d smarttag < migrations/001_feature_windows.sql`.

---

## 4. Поток данных (высокий уровень)

```text
simulate_mcu.py  --MQTT(JSON пачки)-->  Mosquitto  --subscribe-->  ingest_service.py
                                                                    |
                                                                    v
                                                             TimescaleDB
                                                                    ^
train_if.py  <-------- SELECT rms_mag (assembled only) -------------+
       |
       v
  model.joblib  ----загрузка при старте ingest----> predict на каждое новое окно
```

### 4.1 Когда именно учим модель (фазы)

Обучение **не** встроено в `ingest_service.py`: это **отдельный офлайн-шаг** после того, как в БД уже лежат окна «нормы».

| Фаза | Что происходит | Модель |
|------|------------------|--------|
| **A — сбор нормы** | Запущены ingest + `simulate_mcu.py --scenario assembled`; в `feature_windows` накапливаются строки с `scenario_id = stepper_5rps_assembled`. | Файла ещё нет или `MODEL_PATH` в `.env` пуст — ingest пишет только RMS (и опционально RMS-алерт), **без** IF. |
| **B — обучение** | Один раз: **`python scripts/train_if.py`** — читает из БД только assembled, `fit`, **`joblib.dump`** → `MODEL_PATH`. | Создаётся/перезаписывается `*.joblib`. Минимум **50** окон в выборке. |
| **C — инференс** | **Перезапуск** `ingest_service.py` с заданным **`MODEL_PATH`**; дальше каждое новое окно получает `predict` / `decision_function`. | Загрузка при старте ingest, см. §6.6. |

Детали SQL и гиперпараметров IF — в **§7**; порядок команд в одном списке — **§9** (шаги 4–7).

---

## 5. Формат MQTT (publisher)

Топик: **`smarttag/v1/{device_id}/telemetry/json`**, QoS **1**.

Тело JSON (логически):

| Поле | Назначение |
|------|------------|
| `seq` | Монотонный счётчик пакетов на устройстве |
| `ts_last_ms` | Unix ms времени **последнего** сэмпла в пачке |
| `dt_us` | Шаг между сэмплами, \(10^6/\text{ODR}\), ODR по умолчанию **3332** Hz |
| `odr_hz` | Дублирование ODR для проверки |
| `scenario_id` | `stepper_5rps_assembled` или `stepper_5rps_no_bearing` |
| `samples` | До **128** объектов `{x,y,z}` в **mg** |

Для **Isolation Forest** ingest собирает окна по **256** сэмплам; **128** сэмплов в сообщении — это **ровно половина окна** (`MQTT_BATCH_SAMPLES_IF_OPTIMAL` в `smarttag_ml/constants.py`): два сообщения подряд с непрерывным `seq` и согласованным `ts_last_ms` дают одно окно с минимальной частотой MQTT. Симулятор наращивает время конца пачки в **мкс** шагом `128 * dt_us`, чтобы сетка не дрейфовала.

Симулятор генерирует **синтетический** вектор ускорений: для **assembled** — шум **5** mg по осям; для **no_bearing** — **40** mg; на ось **Z** добавляется смещение **~1000 mg** (≈1 g), затем на приёме снимается **DC по окну**, поэтому в RMS остаётся в основном шумовая часть.

---

## 6. Алгоритм `ingest_service.py`

### 6.1 Подписка

Подписка на **`smarttag/v1/+/telemetry/json`**, из пути извлекается **`device_id`**.

### 6.2 Проверка `seq` (gap)

Для каждого `device_id` хранится **`last_seq`**. Если пришло `seq != last_seq + 1`:

- буфер сэмплов **сбрасывается**;
- в лог пишется причина (**gap**);
- новая последовательность начинается «с чистого листа» (как в контракте 09).

### 6.3 Пополнение буфера

Из каждой пачки для каждого сэмпла вычисляется **время в ms** (равномерная сетка внутри пачки от `ts_last_ms` и `dt_us`), в буфер добавляется кортеж **`(x, y, z, t_ms)`**.

### 6.4 Формирование окон

Пока длина буфера **≥ `WINDOW_SAMPLES` (256)**:

1. Берутся **первые 256** точек, остаток буфера сдвигается.
2. Из них матрица **(256, 3)**.
3. **DC removal:** из каждой оси вычитается **среднее по этому окну**.
4. **RMS по вектору:**  
   `rms_mag = sqrt(mean(x'² + y'² + z'²))` по 256 отсчётам.
5. **`window_start`:** время **первого** сэмпла окна = `chunk[0][3]` (уже в ms).

### 6.5 Запись в БД

`INSERT` в **`feature_windows`** с `ON CONFLICT (device_id, window_start) DO UPDATE` — удобно для повторных прогонов.

### 6.6 Модель (если задан `MODEL_PATH`)

- При старте: **`joblib.load(MODEL_PATH)`**.
- На каждое окно: **`predict([[rms_mag]])`**, **`decision_function`** → поля `if_outlier` (`predict == -1`) и `anomaly_score`.

### 6.7 Запасной порог RMS

- **`rms_alert = (rms_mag >= RMS_THRESH)`** (порог из `.env`, например **12** mg при норме ~8.7 mg).
- **`is_alert`:** логическое **OR** `if_outlier` и `rms_alert` (если модель загружена); если модели нет — только RMS.

---

## 7. Алгоритм `train_if.py`

1. Подключение к Postgres (`psycopg`, параметры из `.env`).
2. Выборка **строго**:  
   `WHERE device_id = :id AND scenario_id = 'stepper_5rps_assembled' AND pipeline_version = :pv`.
3. Матрица **`X`** формируется как **`(n, 1)`** из колонки **`rms_mag`**.
4. **`IsolationForest(n_estimators=200, contamination=0.05, random_state=42).fit(X)`**.
5. Сохранение **`joblib.dump`** в путь **`MODEL_PATH`** (например `artifacts/model_v0.joblib`).

Минимум **50** строк в выборке — иначе скрипт завершается с ошибкой (защита от пустого обучения).

---

## 8. Что было исправлено в симуляторе

В первой версии `simulate_mcu.py` ветка «низкий шум» проверяла строку **`"assembled"`**, а в MQTT передавалось **`stepper_5rps_assembled`**. В результате **всегда** использовался шум **40** mg, и **assembled** / **no_bearing** в БД выглядели одинаково по RMS (~69 mg).

Исправление: сравнение с константой **`SCENARIO_ASSEMBLED`** и корректное поле **`scenario_id`** в JSON.

После исправления:

- **assembled:** средний **RMS ≈ 8.7 mg**;
- **no_bearing:** средний **RMS ≈ 69 mg**;
- модель, обученная только на assembled, помечает **no_bearing** как выбросы — ожидаемо при таком разрыве.

---

## 9. Практический порядок запуска (кратко)

1. Docker Compose (**SmartTag_fw**).
2. Миграция SQL (через `docker exec` + файл).
3. `cp .env.example .env`, при необходимости **`RMS_THRESH`**.
4. Терминал 1: **`python scripts/ingest_service.py`** (без модели или с уже обученной).
5. Терминал 2: **`python scripts/simulate_mcu.py --scenario assembled`** (накопить ≥50 окон).
6. **`python scripts/train_if.py`**.
7. Перезапуск ingest **с** `MODEL_PATH` в `.env`.
8. Симуляция **`no_bearing`** — проверка логов и SQL (`AVG(rms_mag)`, сумма `if_outlier`).

Подробные команды: [`../README.md`](../README.md).

---

## 10. Ограничения (честно)

- Это **синтетика**; на реальном узле разница может быть меньше или нестабильной.
- Один признак **RMS** не гарантирует разделимость всех поломок.
- План и риски «модель в контуре» vs «физика стенда» см. план Cursor **backend_ml_smoke_test** и [SmartTag_fw/docs/milestones-and-verification.md](../../../SmartTag_fw/docs/milestones-and-verification.md).
