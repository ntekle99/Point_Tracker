#!/usr/bin/env python3
"""
Autonomous item-finder (the orchestrator).

Pipeline, no human in the loop:
  1. load the latest scraped offers (from scrape.py)
  2. flatten into flat per-tier opportunities (score.offer_opportunities)
  3. for each not-yet-priced opportunity worth chasing:
       render the merchant site (pricecheck) -> ask Claude for the cheapest
       QUALIFYING purchase (judge) -> record it in finds.json
  4. re-score and write the report + macOS notification (score.py)

Only touches MERCHANT sites + the Anthropic API — never Capital One. Run it
after scrape.py (or wire both in a daily task). Reads ANTHROPIC_API_KEY from env.

Usage:
    python3 autohunt.py                 # price today's offers
    python3 autohunt.py --max 12        # cap how many merchants to price
    python3 autohunt.py --min-miles 2000
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pricecheck
import score as S
import judge as J

ROOT = Path(__file__).resolve().parent.parent   # repo root (src/ is one level down)
DATA = ROOT / "data"
FINDS = ROOT / "finds.json"
URLMAP = ROOT / "config" / "hunt_urls.json"   # optional {domain: best-price-page-url}


def latest_offers() -> Path | None:
    # only dated files (offers_2026-...); never offers_sample.json
    files = sorted(DATA.glob("offers_2*.json"))
    return files[-1] if files else None


def _price_job(job):
    """Worker: render a merchant page + judge it. Runs in a pool process, so it
    must be module-level (picklable) and do NO file writes. Returns
    (opp, url, verdict|None, error|None)."""
    opp, url = job
    try:
        rendered = pricecheck.find_prices(url)
        verdict = J.judge(opp, rendered)
        return (opp, url, verdict, None)
    except Exception as e:
        return (opp, url, None, str(e)[:120])


def merchant_url(domain: str, urlmap: dict) -> str:
    d = (domain or "").lower()
    if d in urlmap:
        return urlmap[d]
    # heuristic: telecom/plan merchants usually price on /plans
    if any(k in d for k in ("mobile", "wireless", "t-mobile", "cricket", "visible",
                            "cellular", "straighttalk", "metro")):
        return f"https://www.{d}/plans" if not d.startswith("www.") else f"https://{d}/plans"
    return f"https://{d}"


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offers", type=Path, default=None)
    ap.add_argument("--max", type=int, default=300, help="max merchants to price this run")
    ap.add_argument("--min-miles", type=int, default=1500)
    ap.add_argument("--recheck", action="store_true",
                    help="re-render merchants previously found to have no qualifying price")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel render+judge workers (1 = sequential)")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    offers_path = args.offers or latest_offers()
    if not offers_path or not offers_path.exists():
        print("no offers file — run scrape.py first", file=sys.stderr)
        return 1
    cfg = S.load_config(ROOT / "config" / "config.yaml")
    finds = S.load_finds(FINDS)
    urlmap = json.loads(URLMAP.read_text()) if URLMAP.exists() else {}
    payload = json.loads(offers_path.read_text())
    offers = payload.get("offers", payload)

    # flatten -> unpriced opportunities worth chasing, dedup by (domain, category)
    todo, seen = [], set()
    for o in offers:
        for opp in S.offer_opportunities(o):
            if opp["reward_miles"] < args.min_miles:
                continue
            if S.find_for(opp["domain"], opp["category"], finds):
                continue          # already priced
            key = (opp["domain"], opp["category"].lower())
            if key in seen or not opp["domain"]:
                continue
            seen.add(key)
            todo.append(opp)
    todo.sort(key=lambda x: -x["reward_miles"])
    # skip merchant+category we already evaluated and found nothing (so daily
    # runs don't re-render 160 sites — only NEW ones). --recheck ignores this.
    checked_path = DATA / "checked_none.json"
    checked = set(json.loads(checked_path.read_text())) if checked_path.exists() else set()
    if not args.recheck:
        todo = [x for x in todo
                if f"{x['domain']}#{x['category'].lower()}" not in checked]
    todo = todo[: args.max]
    print(f"pricing {len(todo)} opportunities (>= {args.min_miles} mi)...", flush=True)

    def save_finds():
        existing = json.loads(FINDS.read_text()) if FINDS.exists() else {}
        comment = existing.get("_comment")
        out = {"_comment": comment} if comment else {}
        out.update(finds)
        FINDS.write_text(json.dumps(out, indent=2))

    updated = [0]

    def handle(opp, url, verdict, err, idx):
        # runs in the MAIN process only — so finds/checked writes never race
        tag = f"[{idx}/{len(todo)}] {opp['merchant']} / {opp['category']}"
        ckey = f"{opp['domain']}#{opp['category'].lower()}"
        if err or not verdict:
            print(f"  {tag}: skip ({err})", flush=True)
            return
        price = verdict.get("price_usd")
        # Accept only a plausible, qualifying price with real confidence — rejects
        # the LLM's misreads (a $29 tour, a $1 cable "SIM", low confidence).
        accept = (price and float(price) > 0
                  and verdict.get("likely_qualifies")
                  and verdict.get("plausible")
                  and verdict.get("confidence") in ("medium", "high"))
        if accept:
            fkey = (f"{opp['domain']}#{opp['category'].lower()}"
                    if opp["category"].lower() != "any purchase" else opp["domain"])
            finds[fkey] = {
                "item": verdict.get("item", ""),
                "price": round(float(price), 2),
                "in_stock": True,
                "url": verdict.get("product_url") or url,   # exact product link if found
                "page_url": url,                            # the page we rendered
                "purchase_type": verdict.get("purchase_type", "none"),
                "effort_minutes": verdict.get("effort_minutes"),
                "note": f"[auto] {verdict.get('note','')} (confidence: {verdict.get('confidence')})",
                "checked": dt.date.today().isoformat(),
                "auto": True,
            }
            updated[0] += 1
            save_finds()                      # incremental — survives interruption
            ratio = opp["reward_miles"] * cfg["mile_value_cents"] / 100 / float(price)
            print(f"  {tag} -> ${float(price):.2f} ({ratio:.1f}x) {verdict.get('item','')[:45]}",
                  flush=True)
        else:
            checked.add(ckey)                 # remember the miss; skip next time
            checked_path.write_text(json.dumps(sorted(checked)))
            why = (verdict.get("note", "") or "").strip()
            if price and not verdict.get("plausible"):
                why = "implausible price rejected — " + why
            elif price and verdict.get("confidence") == "low":
                why = "low confidence rejected — " + why
            print(f"  {tag} -> no qualifying price ({why[:60]})", flush=True)

    if args.workers > 1 and todo:
        # render+judge N merchants concurrently (each independent). Workers only
        # render+judge and return data; all file writes stay in the main process.
        from concurrent.futures import ProcessPoolExecutor, as_completed
        print(f"  (running {args.workers} workers in parallel)", flush=True)
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_price_job, (opp, merchant_url(opp["domain"], urlmap))): opp
                    for opp in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                opp, url, verdict, err = fut.result()
                handle(opp, url, verdict, err, i)
    else:
        for i, opp in enumerate(todo, 1):
            opp2, url, verdict, err = _price_job((opp, merchant_url(opp["domain"], urlmap)))
            handle(opp2, url, verdict, err, i)

    # persist finds (preserve the leading _comment if present)
    existing = json.loads(FINDS.read_text()) if FINDS.exists() else {}
    comment = existing.get("_comment")
    out = {"_comment": comment} if comment else {}
    out.update(finds)
    FINDS.write_text(json.dumps(out, indent=2))
    print(f"\nupdated {updated} finds -> {FINDS}")

    # re-score with the freshly-priced opportunities
    report, winners = S.build_report(offers, cfg, offers_path.name, S.load_finds(FINDS))
    rep = ROOT / "reports" / f"report_{dt.date.today().isoformat()}.md"
    rep.parent.mkdir(exist_ok=True)
    rep.write_text(report)
    print(f"report -> {rep}")
    if not args.no_notify:
        S.notify(winners, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
