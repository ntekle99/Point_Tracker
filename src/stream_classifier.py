#!/usr/bin/env python3
"""
Consumer 1 / Producer 2 (two-stage pipeline).

Reads raw offers off `points.offers`, keeps only the ones that:
  1. earn a FLAT reward (via score.offer_opportunities — "Any purchase" plus any
     flat sub-tier expanded from an "up to" offer), and
  2. can be earned with a SINGLE one-time purchase (purchase_gate heuristic on the
     reward requirement, then an LLM confirm for ambiguous survivors).

Each survivor is published to `points.candidates` KEYED BY DOMAIN, so all
candidates for a merchant land on one partition and are priced by one worker in
order (no concurrent same-merchant hits). Run several as a consumer group.

    python src/stream_classifier.py     # one worker (Ctrl-C / SIGTERM to stop)
"""
from __future__ import annotations

import json
import os
import signal
import sys

from confluent_kafka import Consumer, Producer

import judge as J
import purchase_gate as G
import score as S
import stream_bus as bus

_RUNNING = True
# confirm ambiguous keeps with the metadata LLM? (off -> keep heuristic survivors)
_LLM_CONFIRM = os.environ.get("POINTS_GATE_LLM_CONFIRM", "1") != "0"


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def _eligible(opp: dict, offer: dict) -> tuple[bool, str, str]:
    """Return (keep, purchase_type, reason)."""
    gate = G.purchase_gate(opp, offer)
    if not gate["eligible"]:
        return (False, "service_commitment", gate["reason"])
    if not gate["ambiguous"]:
        return (True, "one_time_good", gate["reason"])
    # ambiguous: heuristic kept it, but the category is specific — confirm.
    if not _LLM_CONFIRM:
        return (True, "one_time_good", gate["reason"] + " (llm-confirm off)")
    meta = {"merchant": offer.get("merchant"), "category": opp.get("category"),
            "reward_miles": opp.get("reward_miles"), "terms": offer.get("terms"),
            "detail_terms": offer.get("detail_terms")}
    verdict = J.classify_purchase_type(meta)
    ptype = verdict.get("purchase_type", "unknown")
    keep = ptype == "one_time_good"
    return (keep, ptype, f"llm:{ptype}/{verdict.get('confidence')} — {verdict.get('note','')[:60]}")


def run_classifier(bootstrap: str = bus.BOOTSTRAP) -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": bus.CLASSIFIER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([bus.TOPIC_OFFERS])
    producer = Producer({"bootstrap.servers": bootstrap,
                         "linger.ms": 50, "compression.type": "gzip"})
    pid = os.getpid()
    seen: set[tuple[str, str]] = set()
    print(f"  [classifier {pid}] filtering...", flush=True)
    try:
        while _RUNNING:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                offer = json.loads(msg.value())
            except Exception:
                continue
            for opp in S.offer_opportunities(offer):
                domain = opp.get("domain") or ""
                if not domain:
                    continue
                key = (domain, opp.get("category", "").lower())
                if key in seen:
                    continue
                seen.add(key)
                keep, ptype, reason = _eligible(opp, offer)
                if not keep:
                    print(f"  [classifier {pid}] drop {opp.get('merchant')} / "
                          f"{opp.get('category')} ({reason[:70]})", flush=True)
                    continue
                cand = {"offer": offer, "opp": opp, "purchase_type": ptype}
                producer.produce(bus.TOPIC_CANDIDATES, key=domain.encode(),
                                 value=json.dumps(cand).encode())
                producer.poll(0)
                print(f"  [classifier {pid}] keep {opp.get('merchant')} / "
                      f"{opp.get('category')} ({ptype})", flush=True)
    finally:
        producer.flush(15)
        consumer.close()
        print(f"  [classifier {pid}] stopped", flush=True)


if __name__ == "__main__":
    run_classifier()
