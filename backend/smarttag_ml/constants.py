# Aligned with SmartTag_fw/docs/critical-decisions-v0.md

WINDOW_SAMPLES = 256
# Isolation Forest ingest: one feature row = RMS over WINDOW_SAMPLES consecutive samples.
# Two full MQTT batches of this size = exactly one IF window (minimal messages at full ODR).
MQTT_BATCH_SAMPLES_IF_OPTIMAL = WINDOW_SAMPLES // 2
ODR_HZ_DEFAULT = 3332
DT_US_DEFAULT = int(round(1_000_000 / ODR_HZ_DEFAULT))

SCENARIO_ASSEMBLED = "stepper_5rps_assembled"
SCENARIO_NO_BEARING = "stepper_5rps_no_bearing"
