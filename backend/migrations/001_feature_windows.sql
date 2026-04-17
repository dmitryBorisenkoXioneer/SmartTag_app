-- Run once against smarttag DB (TimescaleDB image enables extension).
-- Example: PGPASSWORD=smarttag_local_only psql -h localhost -U smarttag -d smarttag -f migrations/001_feature_windows.sql

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS feature_windows (
  device_id         TEXT        NOT NULL,
  scenario_id       TEXT        NOT NULL,
  window_start      TIMESTAMPTZ NOT NULL,
  rms_mag           DOUBLE PRECISION NOT NULL,
  pipeline_version  TEXT        NOT NULL DEFAULT 'v0',
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  if_outlier        BOOLEAN,
  anomaly_score     DOUBLE PRECISION,
  PRIMARY KEY (device_id, window_start)
);

SELECT public.create_hypertable(
  'feature_windows',
  'window_start',
  if_not_exists => TRUE
);
