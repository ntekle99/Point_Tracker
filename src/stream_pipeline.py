#!/usr/bin/env python3
"""
Streaming orchestrator (`./points stream`).

Decouples rendering from analysis with Kafka:
  • starts a group of consumer processes (the judges),
  • runs the producer (renders pages in parallel, publishes each as it finishes),
  • drains the verdicts topic, applies the same guardrails as autohunt,
  • writes finds.json + the report.

Requires a broker: `docker compose up -d` (see docker-compose.yml).
Reads the LLM key from the environment (NVIDIA_API_KEY), inherited by consumers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from confluent_kafka import Consumer

import score as S
import stream_bus as bus
import stream_producer as prod
from autohunt import merchant_url, latest_offers

ROOT = Path(__file__).resolve().parent.parent
FINDS = ROOT / "finds.json"
DATA = ROOT / "data"
URLMAP = ROOT / "config" / "hunt_urls.json"


def build_todo(cfg, finds, offers, min_miles, cap):
    todo, seen = [], set()
    for o in offers:
        for opp in S.offer_opportunities(o):
            if opp["reward_miles"] < min_miles:
                continue
            if S.find_for(opp["domain"], opp["category"], finds):
                continue
            key = (opp["domain"], opp["category"].lower())
            if key in seen or not opp["domain"]:
                continue
            seen.add(key)
            todo.append(opp)
    checked_path = DATA / "checked_none.json"
    checked = set(json.loads(checked_path.read_text())) if checked_path.exists() else set()
    todo = [x for x in todo
            if f"{x['domain']}#{x['category'].lower()}" not in checked]
    todo.sort(key=lambda x: -x["reward_miles"])
    return todo[:cap], checked, checked_path


def save_finds(finds):
    existing = json.loads(FINDS.read_text()) if FINDS.exists() else {}
    comment = existing.get("_comment")
    out = {"_comment": comment} if comment else {}
    out.update(finds)
    FINDS.write_text(json.dumps(out, indent=2))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumers", type=int, default=4, help="judge worker processes")
    ap.add_argument("--render-workers", type=int, default=6)
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--min-miles", type=int, default=1500)
    args = ap.parse_args(argv)

    cfg = S.load_config(ROOT / "config" / "config.yaml")
    finds = S.load_finds(FINDS)
    urlmap = json.loads(URLMAP.read_text()) if URLMAP.exists() else {}
    offers_path = latest_offers()
    if not offers_path:
        print("no offers file — run ./points scan first", file=sys.stderr)
        return 1
    offers = json.loads(offers_path.read_text())["offers"]

    todo, checked, checked_path = build_todo(cfg, finds, offers, args.min_miles, args.max)
    jobs = [(opp, merchant_url(opp["domain"], urlmap)) for opp in todo]
    if not jobs:
        print("nothing new to price.")
        return 0
    print(f"streaming {len(jobs)} opportunities via Kafka "
          f"({args.consumers} consumers, {args.render_workers} renderers)...", flush=True)

    try:
        bus.ensure_topics()
    except Exception as e:
        print(f"cannot reach Kafka at {bus.BOOTSTRAP}: {e}\n"
              f"start the broker first:  docker compose up -d", file=sys.stderr)
        return 2

    # start the judge consumer group
    consumer_script = str(ROOT / "src" / "stream_consumer.py")
    procs = [subprocess.Popen([sys.executable, consumer_script])
             for _ in range(args.consumers)]
    try:
        # PRODUCER: render in parallel, publish each page as it's ready.
        # Consumers judge concurrently as pages land on the bus.
        sent = prod.produce_pages(jobs, render_workers=args.render_workers)

        # COLLECTOR: drain verdicts, apply the acceptance guardrails.
        collector = Consumer({"bootstrap.servers": bus.BOOTSTRAP,
                              "group.id": "points.collector",
                              "auto.offset.reset": "earliest",
                              "enable.auto.commit": True})
        collector.subscribe([bus.TOPIC_VERDICTS])
        got = updated = idle = 0
        while got < sent and idle < 60:
            msg = collector.poll(1.0)
            if msg is None or msg.error():
                idle += 1
                continue
            idle = 0
            got += 1
            data = json.loads(msg.value())
            opp, url, v = data["offer"], data["url"], data["verdict"]
            price = v.get("price_usd")
            accept = (price and float(price) > 0 and v.get("likely_qualifies")
                      and v.get("plausible") and v.get("confidence") in ("medium", "high"))
            if accept:
                fkey = (f"{opp['domain']}#{opp['category'].lower()}"
                        if opp["category"].lower() != "any purchase" else opp["domain"])
                finds[fkey] = {
                    "item": v.get("item", ""), "price": round(float(price), 2),
                    "in_stock": True, "url": v.get("product_url") or url, "page_url": url,
                    "purchase_type": v.get("purchase_type", "none"),
                    "effort_minutes": v.get("effort_minutes"),
                    "note": f"[auto] {v.get('note','')} (confidence: {v.get('confidence')})",
                    "checked": dt.date.today().isoformat(), "auto": True}
                updated += 1
                save_finds(finds)
                print(f"  [verdict {got}/{sent}] ✓ {opp['merchant']} "
                      f"${float(price):.2f}", flush=True)
            else:
                checked.add(f"{opp['domain']}#{opp['category'].lower()}")
                checked_path.write_text(json.dumps(sorted(checked)))
                print(f"  [verdict {got}/{sent}] – {opp['merchant']} "
                      f"(no qualifying price)", flush=True)
        collector.close()
        print(f"\ncollected {got}/{sent} verdicts, updated {updated} finds", flush=True)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except Exception:
                p.kill()

    report, _ = S.build_report(offers, cfg, offers_path.name, S.load_finds(FINDS))
    rep = ROOT / "reports" / f"report_{dt.date.today().isoformat()}.md"
    rep.parent.mkdir(exist_ok=True)
    rep.write_text(report)
    print(f"report -> {rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
