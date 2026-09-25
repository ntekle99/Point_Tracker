#!/usr/bin/env python3
"""
Producer 1 (two-stage pipeline): publish Capital One offers to `points.offers`.

Stage-1 job is just "find the websites": read the offers the feed scraper already
produced (paced Capital One access, capped->flat expansion, and rate-limit safety
all live in scrape.py) and put each on the bus. Nothing here touches Capital One
directly, so there is no new rate-limit surface — we reuse the latest scraped file.

    python3 stream_feed_producer.py                 # publish latest data/offers_*.json
    python3 stream_feed_producer.py --offers path   # publish a specific file
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from confluent_kafka import Producer

import stream_bus as bus
from autohunt import latest_offers

ROOT = Path(__file__).resolve().parent.parent


def publish_offers(offers: list[dict], bootstrap: str = bus.BOOTSTRAP) -> int:
    producer = Producer({"bootstrap.servers": bootstrap,
                         "linger.ms": 50, "compression.type": "gzip"})
    sent = 0

    def _cb(err, msg):
        if err is not None:
            print(f"  (produce error: {err})", flush=True)

    for o in offers:
        domain = (o.get("domain") or "").encode() or None
        producer.produce(bus.TOPIC_OFFERS, key=domain,
                         value=json.dumps(o).encode(), on_delivery=_cb)
        sent += 1
        if sent % 200 == 0:
            producer.poll(0)
    producer.flush(30)
    print(f"published {sent} offers -> {bus.TOPIC_OFFERS}", flush=True)
    return sent


def load_offers(offers_path: Path | None) -> list[dict]:
    path = offers_path or latest_offers()
    if not path or not Path(path).exists():
        print("no offers file — run ./points scan (scrape) first", file=sys.stderr)
        return []
    payload = json.loads(Path(path).read_text())
    return payload.get("offers", payload)


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offers", type=Path, default=None)
    args = ap.parse_args(argv)
    offers = load_offers(args.offers)
    if not offers:
        return 1
    bus.ensure_topics()
    publish_offers(offers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
