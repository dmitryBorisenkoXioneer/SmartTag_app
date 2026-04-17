# SmartTag_app

Web **front-end** and **back-end** for the SmartTag vibration / BME280 monitoring stack.

- **`backend/`** — MQTT ingest (`ingest_service.py`), окна 256 → RMS → TimescaleDB, обучение IF (`train_if.py`), симулятор MCU (`simulate_mcu.py`), демо API + статика (`demo_server.py`). Подробный runbook: [`backend/README.md`](backend/README.md).
- **`frontend/demo/`** — статическая демо-страница (кнопки «с подшипником / без», шкала отклонения %); вызывает API `demo_server`, который **публикует симуляцию в MQTT**, а результат показывает из **БД** после работы **`ingest_service`** (оба процесса — см. `backend/README.md` §7).
- **`frontend/`** — заготовка под полноценный UI по плану (`SmartTag_fw/docs/04-frontend.md`); Vite/React — следующий шаг.

Firmware, sensor driver, and local Docker (Postgres + MQTT) live in the sibling repo **`SmartTag_fw`**. Plan index: [`SmartTag_fw/docs/plan.md`](../SmartTag_fw/docs/plan.md).
