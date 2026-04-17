# SmartTag_app

Web **front-end** and **back-end** for the SmartTag vibration / BME280 monitoring stack.

- **`frontend/`** — browser UI (two modes: dataset + train, live monitoring).
- **`backend/`** — MQTT ingest, окна, TimescaleDB, IF train/score, симулятор MCU; см. [`backend/README.md`](backend/README.md).

Firmware, sensor driver, and local Docker (Postgres + MQTT) live in the sibling repo **`SmartTag_fw`**. Plan and contracts: `SmartTag_fw/docs/plan.md`.
