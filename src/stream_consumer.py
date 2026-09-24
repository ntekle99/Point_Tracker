#!/usr/bin/env python3
"""
Consumer: pull rendered pages off `points.pages`, run the LLM judge on each,
and publish the verdict to `points.verdicts`. Start several of these as a
consumer group and Kafka load-balances the partitions across them.

    python src/stream_consumer.py        # one worker (Ctrl-C / SIGTERM to stop)
"""
from __future__ import annotations

import json
import os
import signal
import sys

from confluent_kafka import Consumer, Producer

import judge as J
import stream_bus as bus

_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def run_consumer(bootstrap: str = bus.BOOTSTRAP) -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": bus.CONSUMER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([bus.TOPIC_PAGES])
    producer = Producer({"bootstrap.servers": bootstrap})
    pid = os.getpid()
    print(f"  [consumer {pid}] judging...", flush=True)
    try:
        while _RUNNING:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                continue
            try:
                page = json.loads(msg.value())
                offer, rendered = page["offer"], page.get("rendered")
                if rendered:
                    verdict = J.judge(offer, rendered)
                else:
                    verdict = J._fail(f"render failed: {page.get('err')}")
            except Exception as e:
                offer = {}
                verdict = J._fail(f"consumer error: {str(e)[:120]}")
            out = {"offer": page.get("offer", {}), "url": page.get("url", ""),
                   "verdict": verdict}
            producer.produce(bus.TOPIC_VERDICTS, value=json.dumps(out).encode())
            producer.poll(0)
    finally:
        producer.flush(15)
        consumer.close()
        print(f"  [consumer {pid}] stopped", flush=True)


if __name__ == "__main__":
    run_consumer()
