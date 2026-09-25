#!/usr/bin/env python3
"""
Two-stage streaming orchestrator (`./points stream2`).

Chains two producer/consumer queues so each concern is decoupled:

  feed  ─▶ points.offers ─▶ [classifiers] ─▶ points.candidates ─▶ [pricers] ─▶ points.rated ─▶ collector

  • Producer 1  — publish scraped offers (stream_feed_producer).
  • Consumer 1  — classifiers: flat-only + single-purchase gate (stream_classifier),
                  which become the producer for queue 2.
  • Consumer 2  — pricers: render the plain merchant site, find the cheapest
                  qualifying one-time item + deep link, judge it (stream_pricer).
  • Collector   — apply the acceptance guardrail, write finds.json, verify the
                  top picks' EXACT deep links, and render the report.

Async by nature (no fixed message count), so the collector drains until the
`points.rated` topic goes quiet for --idle seconds.

Requires a broker: `docker compose up -d`.
Reads the LLM key from the environment (MODEL_API_KEY), inherited by workers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from confluent_kafka import Consumer

import pricecheck
import score as S
import stream_bus as bus
import stream_feed_producer as feed

ROOT = Path(__file__).resolve().parent.parent
FINDS = ROOT / "finds.json"


def save_finds(finds):
    existing = json.loads(FINDS.read_text()) if FINDS.exists() else {}
    comment = existing.get("_comment")
    out = {"_comment": comment} if comment else {}
    out.update(finds)
    FINDS.write_text(json.dumps(out, indent=2))


def _fkey(opp: dict) -> str:
    cat = (opp.get("category", "") or "").lower()
    dom = opp.get("domain", "")
    return dom if cat in ("", "any purchase") else f"{dom}#{cat}"


def _start_group(script: str, n: int) -> list:
    path = str(ROOT / "src" / script)
    return [subprocess.Popen([sys.executable, path]) for _ in range(n)]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--classifiers", type=int, default=2, help="stage-1 workers")
    ap.add_argument("--pricers", type=int, default=2, help="stage-2 workers")
    ap.add_argument("--offers", type=Path, default=None, help="offers file (else latest)")
    ap.add_argument("--idle", type=int, default=90,
                    help="stop after the rated topic is quiet this many seconds")
    ap.add_argument("--verify-top", type=int, default=5,
                    help="how many top deals to click-verify exact deep links for")
    args = ap.parse_args(argv)

    cfg = S.load_config(ROOT / "config" / "config.yaml")
    finds = S.load_finds(FINDS)
    offers = feed.load_offers(args.offers)
    if not offers:
        return 1

    try:
        bus.ensure_topics()
    except Exception as e:
        print(f"cannot reach Kafka at {bus.BOOTSTRAP}: {e}\n"
              f"start the broker first:  docker compose up -d", file=sys.stderr)
        return 2

    classifiers = _start_group("stream_classifier.py", args.classifiers)
    pricers = _start_group("stream_pricer.py", args.pricers)
    print(f"started {args.classifiers} classifiers + {args.pricers} pricers", flush=True)

    collector = Consumer({"bootstrap.servers": bus.BOOTSTRAP,
                          "group.id": bus.COLLECTOR2_GROUP,
                          "auto.offset.reset": "earliest",
                          "enable.auto.commit": True})
    collector.subscribe([bus.TOPIC_RATED])

    # Producer 1: publish the offers (consumers read from earliest, so ordering vs
    # subscribe doesn't matter).
    feed.publish_offers(offers)

    updated = accepted = idle = 0
    try:
        while idle < args.idle:
            msg = collector.poll(1.0)
            if msg is None or msg.error():
                idle += 1
                continue
            idle = 0
            data = json.loads(msg.value())
            opp, url, v = data["opp"], data["url"], data["verdict"]
            price = v.get("price_usd")
            accept = (price and float(price) > 0 and v.get("likely_qualifies")
                      and v.get("plausible") and v.get("confidence") in ("medium", "high"))
            if accept:
                finds[_fkey(opp)] = {
                    "item": v.get("item", ""), "price": round(float(price), 2),
                    "in_stock": True, "url": v.get("product_url") or url, "page_url": url,
                    "purchase_type": v.get("purchase_type", "one_time_good"),
                    "effort_minutes": v.get("effort_minutes"),
                    "note": f"[auto] {v.get('note','')} (confidence: {v.get('confidence')})",
                    "checked": dt.date.today().isoformat(), "auto": True,
                    "link_verified": False}
                accepted += 1
                updated += 1
                save_finds(finds)
                print(f"  [rated] ✓ {opp.get('merchant')} ${float(price):.2f} "
                      f"{v.get('item','')[:40]}", flush=True)
            else:
                print(f"  [rated] – {opp.get('merchant')} (no qualifying price)", flush=True)
    finally:
        for p in classifiers + pricers:
            p.terminate()
        for p in classifiers + pricers:
            try:
                p.wait(10)
            except Exception:
                p.kill()
        collector.close()

    print(f"\ncollected {accepted} accepted deals", flush=True)

    # --- verify the top picks' EXACT deep links (bounded, paced) ---
    if args.verify_top > 0:
        _, clean = S.build_report(offers, cfg, "stream2", S.load_finds(FINDS))
        checked = 0
        for c in clean:
            if checked >= args.verify_top:
                break
            fk = _fkey({"domain": c.get("domain", ""), "category": c.get("category", "")})
            rec = finds.get(fk)
            if not rec or not rec.get("page_url") or not rec.get("item"):
                continue
            checked += 1
            res = pricecheck.resolve_deep_link(rec["page_url"], rec["item"])
            if res.get("ok") and res.get("url"):
                rec["url"] = res["url"]
                rec["link_verified"] = True
                save_finds(finds)
                print(f"  [verify] {c.get('merchant')}: {res['url']}", flush=True)
            elif res.get("blocked"):
                print(f"  [verify] {c.get('merchant')}: blocked — kept nearest link", flush=True)
            else:
                print(f"  [verify] {c.get('merchant')}: {res.get('note','')[:50]}", flush=True)

    report, _ = S.build_report(offers, cfg, "stream2", S.load_finds(FINDS))
    rep = ROOT / "reports" / f"report_{dt.date.today().isoformat()}.md"
    rep.parent.mkdir(exist_ok=True)
    rep.write_text(report)
    print(f"report -> {rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
