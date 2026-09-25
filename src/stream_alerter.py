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

import judge as J
import pricecheck
import purchase_gate as G
import score as S
import stream_bus as bus
from autohunt import merchant_url

ROOT = Path(__file__).resolve().parent.parent
_RUNNING = True

# Price the offer (render the merchant site, find the cheapest item + deep link)
# before alerting, so the push is actionable — not just "a flat offer appeared".
# Set POINTS_ALERT_PRICE=0 to fall back to fast lead-only alerts (no rendering).
_PRICE_IN_ALERT = os.environ.get("POINTS_ALERT_PRICE", "1") != "0"

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


def _phone_push(title: str, body: str, click_url: str | None = None) -> bool:
    if not NTFY_TOPIC:
        return False
    ascii_title = title.encode("ascii", "ignore").decode().strip() or "New Capital One offer"
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"), method="POST",
            headers={"Title": ascii_title, "Priority": "urgent",
                     "Tags": "money_with_wings",
                     # tapping the push opens the exact product (or the feed)
                     "Click": click_url or "https://capitaloneoffers.com/feed"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"    (ntfy push failed: {str(e)[:80]})", flush=True)
        return False


def send_alert(title: str, body: str, click_url: str | None = None) -> str:
    """Fire the alert on every available channel. Returns which fired."""
    _mac_notify(title, body)
    pushed = _phone_push(title, body, click_url)
    return "phone + mac" if pushed else "mac only (set NTFY_TOPIC for phone push)"


def _price_offer(best: dict, urlmap: dict, mv: float) -> dict:
    """Render the merchant's site (paced + block-safe) and judge the cheapest
    qualifying item. Returns {priced, item, price, ratio, link, note}."""
    url = merchant_url(best["domain"], urlmap)
    rendered = pricecheck.find_prices(url, sort_cheapest=True)
    if rendered.get("blocked"):
        return {"priced": False, "link": url, "note": "site blocked pricing"}
    v = J.judge(best, rendered)
    price = v.get("price_usd")
    ok = (price and float(price) > 0 and v.get("plausible")
          and v.get("likely_qualifies") and v.get("confidence") in ("medium", "high"))
    if not ok:
        return {"priced": False, "link": v.get("product_url") or url,
                "note": v.get("note", "no qualifying price")[:60]}
    price = float(price)
    value = best["reward_miles"] * mv
    return {"priced": True, "item": v.get("item", "item"), "price": price,
            "ratio": value / price if price else 0,
            "link": v.get("product_url") or url, "note": ""}


def run_alerter() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    cfg = S.load_config(ROOT / "config" / "config.yaml")
    mv = cfg["mile_value_cents"] / 100.0
    bar = cfg["min_flat_reward_miles"]

    consumer = Consumer({"bootstrap.servers": bus.BOOTSTRAP,
                        "group.id": bus.ALERTER_GROUP,
                        "auto.offset.reset": "latest",   # only alert on offers from now on
                        "enable.auto.commit": True,
                        # pricing renders pages (slow + paced) between polls; don't
                        # let Kafka evict us mid-render.
                        "max.poll.interval.ms": 900000})
    consumer.subscribe([bus.TOPIC_NEW_OFFERS])
    urlmap_path = ROOT / "config" / "hunt_urls.json"
    urlmap = json.loads(urlmap_path.read_text()) if urlmap_path.exists() else {}
    channel = f"phone (ntfy:{NTFY_TOPIC}) + mac" if NTFY_TOPIC else "mac only"
    mode = "price + alert" if _PRICE_IN_ALERT else "lead-only alert"
    print(f"  [alerter {os.getpid()}] listening for NEW flat + single-purchase "
          f"offers ≥ {bar} mi → {channel} ({mode})", flush=True)
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
            merchant = o.get("merchant", "?")
            miles = int(best["reward_miles"])
            value = miles * mv
            cat = best["category"]
            buy = "any cheap item" if cat.lower() in ("any purchase", "") else cat
            click = None

            if _PRICE_IN_ALERT:
                # render the merchant site + judge the cheapest item (paced, safe)
                try:
                    r = _price_offer(best, urlmap, mv)
                except Exception as e:
                    r = {"priced": False, "link": None, "note": f"pricing error: {str(e)[:50]}"}
                click = r.get("link")
                if r.get("priced"):
                    title = f"💰 {merchant} — buy ${r['price']:.2f} → {miles:,} mi ({r['ratio']:.1f}x)"
                    body = (f"Buy: {r['item'][:70]}\n"
                            f"${r['price']:.2f} → {miles:,} miles (~${value:.0f}) · {r['ratio']:.1f}x\n"
                            f"{r['link']}")
                else:
                    title = f"🎯 New flat single-buy — {merchant}"
                    body = (f"{miles:,} miles (~${value:.0f}) · buy: {buy} · {o.get('domain','')}\n"
                            f"(couldn't auto-price: {r.get('note','')})")
            else:
                title = f"🎯 New flat single-buy — {merchant}"
                body = f"{miles:,} miles (~${value:.0f}) · buy: {buy} · {o.get('domain','')}"

            fired = send_alert(title, body, click)
            print(f"    ALERT [{fired}]: {title} — {body}", flush=True)
    finally:
        consumer.close()
        print(f"  [alerter {os.getpid()}] stopped", flush=True)


if __name__ == "__main__":
    run_alerter()
