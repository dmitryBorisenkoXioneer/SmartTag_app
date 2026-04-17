# Aligned with SmartTag_fw/docs/09-critical-decisions-v0.md

WINDOW_SAMPLES = 256
ODR_HZ_DEFAULT = 3332
DT_US_DEFAULT = int(round(1_000_000 / ODR_HZ_DEFAULT))

SCENARIO_ASSEMBLED = "stepper_5rps_assembled"
SCENARIO_NO_BEARING = "stepper_5rps_no_bearing"
