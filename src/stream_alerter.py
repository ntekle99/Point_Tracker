#!/usr/bin/env python3
"""
Real-time alerter (CONSUMER). Subscribes to `points.new_offers` and fires an
instant macOS notification the moment a new FLAT offer worth your attention
appears — so you hear about a Pinter-style deal at 2pm, not tomorrow at 8am.

Run several as a consumer group; the watcher (`./points watch`) starts them.
    python src/stream_alerter.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from confluent_kafka import Consumer

import score as S
import stream_bus as bus

ROOT = Path(__file__).resolve().parent.parent
_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def notify(title: str, body: str) -> None:
    js = lambda s: json.dumps(s, ensure_ascii=False)
    script = (f"display notification {js(body)} with title {js(title)} "
              f'sound name "Glass"')
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass


def run_alerter() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    cfg = S.load_config(ROOT / "config" / "config.yaml")
    mv = cfg["mile_value_cents"] / 100.0
    bar = cfg["min_flat_reward_miles"]

    consumer = Consumer({"bootstrap.servers": bus.BOOTSTRAP,
                        "group.id": bus.ALERTER_GROUP,
                        "auto.offset.reset": "latest",   # only alert on offers from now on
                        "enable.auto.commit": True})
    consumer.subscribe([bus.TOPIC_NEW_OFFERS])
    print(f"  [alerter {os.getpid()}] listening for new flat offers ≥ {bar} mi...",
          flush=True)
    try:
        while _RUNNING:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                o = json.loads(msg.value())
            except Exception:
                continue
            miles = int(o.get("reward_miles") or 0)
            # only flat lump-sum offers are worth a real-time ping
            if o.get("reward_type") != "flat" or miles < bar:
                continue
            value = miles * mv
            title = f"🎯 New Capital One flat offer — {o.get('merchant','?')}"
            body = f"{miles:,} miles (~${value:.0f}) · {o.get('domain','')}"
            print(f"    ALERT: {title} — {body}", flush=True)
            notify(title, body)
    finally:
        consumer.close()
        print(f"  [alerter {os.getpid()}] stopped", flush=True)


if __name__ == "__main__":
    run_alerter()
