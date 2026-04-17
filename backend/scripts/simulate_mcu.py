#!/usr/bin/env python3
"""
Publish synthetic accelerometer batches to MQTT (contract v0 / docs/09).
Same JSON shape as real ESP32 — ingest_service does not distinguish source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smarttag_ml.constants import SCENARIO_ASSEMBLED, SCENARIO_NO_BEARING  # noqa: E402
from smarttag_ml.synthetic_payload import build_payload  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--device-id", default=os.environ.get("DEVICE_ID", "demo001"))
    p.add_argument(
        "--scenario",
        choices=("assembled", "no_bearing"),
        default="assembled",
        help="assembled -> stepper_5rps_assembled; no_bearing -> stepper_5rps_no_bearing",
    )
    p.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    p.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--hz", type=float, default=20.0, help="MQTT publish rate (messages / s)")
    args = p.parse_args()

    scenario_id = SCENARIO_ASSEMBLED if args.scenario == "assembled" else SCENARIO_NO_BEARING
    topic = f"smarttag/v1/{args.device_id}/telemetry/json"
    period = 1.0 / max(args.hz, 0.1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim_{args.device_id}")
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_start()

    rng = np.random.default_rng(42)
    seq = 0
    print(f"publishing to {topic} scenario={scenario_id} ~{args.hz} Hz", flush=True)
    try:
        while True:
            seq += 1
            payload = build_payload(seq, scenario_id, rng)
            payload["fw_version"] = "simulate_mcu-0.1"
            client.publish(topic, json.dumps(payload), qos=1)
            time.sleep(period)
    except KeyboardInterrupt:
        print("stop", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
