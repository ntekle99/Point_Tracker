#!/usr/bin/env python3
"""
Consumer 2 (two-stage pipeline): price single-purchase candidates.

Reads `points.candidates` (keyed by domain, so one worker handles a given
merchant in order). For each candidate it visits the merchant's NORMAL website
(plain domain / curated catalog URL — decoupled from the feed; no affiliate link),
finds the cheapest QUALIFYING one-time item + its deep link, and publishes the
judged result to `points.rated`.

Rendering safety lives in pricecheck.find_prices (process-wide pacing, stealth,
sort-cheapest, and abort-without-retry on a bot-block). Because candidates are
partitioned by domain, same-merchant renders never overlap across workers.

    python src/stream_pricer.py     # one worker (Ctrl-C / SIGTERM to stop)
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from confluent_kafka import Consumer, Producer

import agent_graph
import judge as J
import stream_bus as bus
from autohunt import merchant_url

ROOT = Path(__file__).resolve().parent.parent
URLMAP = ROOT / "config" / "hunt_urls.json"
_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def run_pricer(bootstrap: str = bus.BOOTSTRAP) -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    urlmap = json.loads(URLMAP.read_text()) if URLMAP.exists() else {}
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": bus.PRICER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
        "max.poll.interval.ms": 900000,   # rendering + pacing can be slow; don't get kicked
    })
    consumer.subscribe([bus.TOPIC_CANDIDATES])
    producer = Producer({"bootstrap.servers": bootstrap,
                         "linger.ms": 50, "compression.type": "gzip"})
    pid = os.getpid()
    print(f"  [pricer {pid}] pricing...", flush=True)
    try:
        while _RUNNING:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                cand = json.loads(msg.value())
                offer, opp = cand["offer"], cand["opp"]
                url = merchant_url(opp["domain"], urlmap)
                # run the offer through the LangGraph decision graph
                # (gate -> price -> judge -> decide); Kafka already distributed it here.
                state = agent_graph.run_deal(offer, opp, url)
                verdict = state.get("verdict") or J._fail(
                    f"gate rejected: {state.get('gate_reason', '')}")
                # carry the classifier's purchase_type unless the judge found a truer one
                verdict.setdefault("purchase_type", cand.get("purchase_type", "one_time_good"))
            except Exception as e:
                offer, opp, url = cand.get("offer", {}), cand.get("opp", {}), ""
                verdict = J._fail(f"pricer error: {str(e)[:120]}")
            out = {"offer": offer, "opp": opp, "url": url, "verdict": verdict}
            producer.produce(bus.TOPIC_RATED, value=json.dumps(out).encode())
            producer.poll(0)
            price = verdict.get("price_usd")
            print(f"  [pricer {pid}] {opp.get('merchant')} / {opp.get('category')}"
                  f" -> {('$%.2f' % float(price)) if price else 'no price'}", flush=True)
    finally:
        producer.flush(15)
        consumer.close()
        print(f"  [pricer {pid}] stopped", flush=True)


if __name__ == "__main__":
    run_pricer()
