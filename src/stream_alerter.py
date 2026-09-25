#!/usr/bin/env python3
"""
Real-time alerter (CONSUMER). Subscribes to `points.new_offers` and fires an
instant push the moment a new offer that fits ALL the criteria appears — flat
reward, single one-time purchase (no subscription / service commitment / spend
threshold), and above your miles bar. Offers get devalued as they get popular,
so catching a Pinter/Faire-style deal the hour it lands is the whole point.

Run several as a consumer group; the watcher (`./points watch`) starts them.
    python src/stream_alerter.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import urllib.request
from pathlib import Path

from confluent_kafka import Consumer

import purchase_gate as G
import score as S
import stream_bus as bus

ROOT = Path(__file__).resolve().parent.parent
_RUNNING = True

# Phone push via ntfy.sh (free, no account). Set NTFY_TOPIC in your .env to a
# unique, hard-to-guess string, then subscribe to it in the ntfy app on your
# phone. Leave it unset to use macOS notifications only (local dev).
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def _stop(*_):
    global _RUNNING
    _RUNNING = False


def _mac_notify(title: str, body: str) -> None:
    js = lambda s: json.dumps(s, ensure_ascii=False)
    script = (f"display notification {js(body)} with title {js(title)} "
              f'sound name "Glass"')
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass  # not on macOS (e.g. a Linux VM) — phone push carries it


def _phone_push(title: str, body: str) -> bool:
    if not NTFY_TOPIC:
        return False
    ascii_title = title.encode("ascii", "ignore").decode().strip() or "New Capital One offer"
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"), method="POST",
            headers={"Title": ascii_title, "Priority": "high",
                     "Tags": "money_with_wings",
                     "Click": "https://capitaloneoffers.com/feed"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"    (ntfy push failed: {str(e)[:80]})", flush=True)
        return False


def send_alert(title: str, body: str) -> str:
    """Fire the alert on every available channel. Returns which fired."""
    _mac_notify(title, body)
    pushed = _phone_push(title, body)
    return "phone + mac" if pushed else "mac only (set NTFY_TOPIC for phone push)"


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
    channel = f"phone (ntfy:{NTFY_TOPIC}) + mac" if NTFY_TOPIC else "mac only"
    print(f"  [alerter {os.getpid()}] listening for NEW flat + single-purchase "
          f"offers ≥ {bar} mi → {channel}", flush=True)
    try:
        while _RUNNING:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                o = json.loads(msg.value())
            except Exception:
                continue
            # Apply the FULL criteria: flat earning path (incl. flat tiers) + a
            # single one-time purchase (drops subs / commitments / spend-thresholds)
            # + above the miles bar. Pick the best-qualifying path.
            best = None
            for opp in S.offer_opportunities(o):
                if opp["reward_miles"] < bar:
                    continue
                if not G.purchase_gate(opp, o)["eligible"]:
                    continue
                if best is None or opp["reward_miles"] > best["reward_miles"]:
                    best = opp
            if not best:
                continue
            miles = int(best["reward_miles"])
            value = miles * mv
            cat = best["category"]
            buy = "any cheap item" if cat.lower() in ("any purchase", "") else cat
            title = f"🎯 New flat single-buy — {o.get('merchant','?')}"
            body = (f"{miles:,} miles (~${value:.0f}) · buy: {buy} · "
                    f"{o.get('domain','')}")
            fired = send_alert(title, body)
            print(f"    ALERT [{fired}]: {title} — {body}", flush=True)
    finally:
        consumer.close()
        print(f"  [alerter {os.getpid()}] stopped", flush=True)


if __name__ == "__main__":
    run_alerter()
