#!/usr/bin/env python3
"""
Capital One Offers scraper (Playwright, persistent login profile).

Because the portal is behind your login and NVIDIA policy blocks the Chrome
extension, this drives its OWN Chromium with a persistent user-data dir. You
log in ONCE (manually); the session cookie is saved to ./pw_profile and reused
on every later run — no credentials ever touch this code.

FIRST TIME:
    python3 scrape.py --login
      -> opens a browser, you sign into Capital One by hand, then press Enter.

EVERY RUN AFTER:
    python3 scrape.py
      -> reuses the saved session, scrolls the offers feed, extracts cards,
         writes data/offers_<date>.json (+ a raw dump for debugging).

Then score it:
    python3 score.py data/offers_<date>.json

Notes on being a good citizen (bank bot-detection):
  * Runs HEADED by default (harder to fingerprint than headless). --headless to override.
  * Human-paced scrolling, no hammering. Don't schedule it more than ~1x/day.
  * If Capital One shows a re-auth / MFA / CAPTCHA, solve it yourself in the
    window — this script never touches credentials or CAPTCHAs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

import parse as P

ROOT = Path(__file__).resolve().parent.parent   # repo root (src/ is one level down)
PROFILE_DIR = ROOT / "pw_profile"
DATA_DIR = ROOT / "data"
FEED_URL = "https://capitaloneoffers.com/feed"

# --- Anti-ban rate limiting (be a good citizen; don't get throttled) ---------
# Very conservative on purpose. Detail calls are cached BY DOMAIN so daily runs
# only fetch merchants we've never seen.
DETAIL_DELAY_SEC = 2.5     # pause between per-offer detail calls (very gentle)
PAGE_DELAY_SEC = 1.0       # pause between feed pagination requests
MAX_DETAILS = 15           # hard cap on NEW detail calls per run (capped offers are few)
DETAILS_CACHE = DATA_DIR / "details_cache.json"   # keyed by merchant domain; reused across days

# Best-effort DOM extractor. Selectors on this portal are not documented, so we
# extract heuristically: find every element whose text mentions a reward, climb
# to a plausible "card" ancestor, and pull merchant + terms + link from it.
# This is the ONE piece expected to need tuning on the first real run — the raw
# dump is saved so we can refine selectors against the actual markup.
EXTRACT_JS = r"""
() => {
  const rewardRe = /(\d[\d,]*\s*miles?\b)|(\bup to\b.*?\d+\s*[xX]\b)|(\d+\s*[xX]\s*miles)|(\d+(?:\.\d+)?\s*%)/i;
  // merchant name on this portal lives in the logo image alt / aria-label,
  // NOT the visible card text (which just says "Online"). Collect those.
  const GENERIC = /^(online|in-store|in store|get this offer|new offers?|today'?s top offer|featured|limited availability)/i;
  const seen = new Set();
  const out = [];
  const all = Array.from(document.querySelectorAll('a,div,li,article,section'));
  for (const el of all) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 600) continue;
    if (!rewardRe.test(txt)) continue;
    let card = el;
    for (let i = 0; i < 4 && card.parentElement; i++) {
      const p = card.parentElement;
      if ((p.innerText || '').length > 800) break;
      card = p;
    }
    const cardTxt = (card.innerText || '').trim();
    if (!cardTxt || seen.has(cardTxt)) continue;
    seen.add(cardTxt);

    // gather merchant candidates: image alts, aria-labels, img src filenames
    const cands = [];
    card.querySelectorAll('img[alt]').forEach(i => {
      const a = (i.getAttribute('alt') || '').trim();
      if (a) cands.push(a);
    });
    card.querySelectorAll('[aria-label]').forEach(e => {
      const a = (e.getAttribute('aria-label') || '').trim();
      if (a) cands.push(a);
    });
    const link = card.querySelector('a[href]');
    const heading = card.querySelector('h1,h2,h3,h4,[class*="title" i],[class*="name" i]');
    if (heading) cands.push(heading.innerText.trim());
    // pick first non-generic, non-reward candidate as merchant
    let merchant = '';
    for (const c of cands) {
      if (!c || GENERIC.test(c) || rewardRe.test(c)) continue;
      merchant = c; break;
    }
    if (!merchant) merchant = cardTxt.split('\n')[0].trim();

    const rewardMatch = cardTxt.match(rewardRe);
    out.push({
      merchant,
      merchant_candidates: cands,   // kept for debugging / tuning
      reward_text: rewardMatch ? rewardMatch[0].trim() : '',
      terms: cardTxt,
      url: link ? link.href : ''
    });
  }
  return out;
}
"""


def _click_view_more(page) -> bool:
    """Click a 'View More Offers' / 'Load More' button if present. Returns
    True if it clicked one. Fully defensive — never raises."""
    try:
        return bool(page.evaluate(r"""
          () => {
            const re = /view more|load more|show more|see more/i;
            const els = Array.from(document.querySelectorAll('button,a,[role="button"],span,div'));
            for (const el of els) {
              const t = (el.innerText || '').trim();
              if (t && t.length < 40 && re.test(t) && el.offsetParent !== null) {
                el.click();
                return true;
              }
            }
            return false;
          }
        """))
    except Exception:
        return False


def autoscroll(page, max_rounds: int = 120, pause: float = 0.6,
               count_fn=None) -> None:
    """Scroll to the bottom and click 'View More Offers' REPEATEDLY until the
    button is gone (the feed virtualizes the DOM, so we can't count tiles —
    termination is driven by the button's presence). `count_fn` returns the
    true offer total (from captured feed-API pages) for progress display.
    Never raises."""
    no_button = 0
    for r in range(max_rounds):
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(pause)
        except Exception:
            break
        clicked = _click_view_more(page)
        if clicked:
            no_button = 0
            time.sleep(pause + 0.6)      # let the next page of offers load
        else:
            no_button += 1
        n = count_fn() if count_fn else "?"
        print(f"  loading offers... {n} captured"
              f"{' (View More)' if clicked else ' (no button)'}", flush=True)
        if no_button >= 3:
            break   # button gone for several rounds -> all offers loaded
    print(f"  done loading: {count_fn() if count_fn else '?'} offers captured",
          flush=True)


def run(login_only: bool, headless: bool, url: str) -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--hide-crash-restore-bubble"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Capture the feed API JSON (the reliable source of merchant + reward)
        # and the pagination token/cursor so we can page through the WHOLE feed
        # via the same /xhr/feed/<token>/offers/<cursor> endpoint the "View More
        # Offers" button hits — no scrolling/clicking needed.
        feed_items: list[dict] = []
        state = {"token": None, "cursor": None, "feed_url": None}
        tok_re = re.compile(r"/(?:xhr/)?feed/([^/?]+)")
        def on_response(resp):
            try:
                if "/feed/" not in resp.url or "/offers/" in resp.url:
                    return
                if "json" not in resp.headers.get("content-type", ""):
                    return
                m = tok_re.search(resp.url)
                if m and not state["token"]:
                    state["token"] = m.group(1)
                if "?" in resp.url and not state["feed_url"]:
                    state["feed_url"] = resp.url   # the paginated feed endpoint
                j = resp.json()
                if isinstance(j, dict):
                    if isinstance(j.get("data"), list):
                        feed_items.extend(j["data"])
                    if j.get("cursor"):
                        state["cursor"] = j["cursor"]
            except Exception:
                pass
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        if login_only:
            print("\n>>> A browser window is open. Sign into Capital One, get to")
            print(">>> your Offers feed, then come back here and press Enter.")
            try:
                input(">>> Press Enter when you're logged in and see your offers... ")
            except EOFError:
                print("(no stdin — waiting 90s instead)"); time.sleep(90)
            ctx.close()
            print("Session saved to pw_profile/. Next: python3 scrape.py")
            return 0

        # scrape mode
        time.sleep(2)
        if "signin" in page.url or "login" in page.url.lower():
            print("error: not logged in (redirected to sign-in).", file=sys.stderr)
            print("       run:  python3 scrape.py --login", file=sys.stderr)
            ctx.close()
            return 2

        # Wait for the initial feed request to land (populates token/cursor).
        # Nudge the page (small scroll) in case the feed is lazy-loaded.
        today0 = dt.date.today().isoformat()
        for _ in range(30):  # up to ~15s
            if state["token"] and state["cursor"]:
                break
            try:
                page.mouse.wheel(0, 1500)
            except Exception:
                pass
            time.sleep(0.5)

        # Page through the ENTIRE feed by re-hitting the initial feed URL with
        # &cursor=<cursor> (that's what "View More Offers" does). Fast, no scroll.
        base = "https://capitaloneoffers.com"
        cursor = state["cursor"]
        feed_url = state["feed_url"]
        hdrs = {"Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{base}/feed"}
        pages = 0
        if feed_url:
            while cursor and pages < 60:
                api = f"{feed_url}&cursor={quote(cursor, safe='-_')}"
                try:
                    r = page.request.get(api, headers=hdrs, timeout=20000)
                    body = r.text()
                    j = json.loads(body)
                except Exception as e:
                    print(f"  (pagination stopped: {e})", file=sys.stderr)
                    break
                data = j.get("data") if isinstance(j, dict) else None
                if not data:
                    break
                feed_items.extend(data)
                new_cursor = j.get("cursor")
                if new_cursor == cursor:
                    break                    # no progress -> done
                cursor = new_cursor
                pages += 1
                print(f"  loaded {len(feed_items)} offers "
                      f"(page {pages+1})...", flush=True)
                time.sleep(PAGE_DELAY_SEC)    # gentle pacing between pages
        else:
            print("(warn) no feed url captured; got first page only",
                  file=sys.stderr)

        # Enrich EVERY offer with its tier breakdown + terms via the detail
        # endpoint /xhr/feed/<token>/offers/<offer.id>. This is how we find flat
        # tiers hidden inside "Up to X" offers (e.g. AT&T Prepaid 7,800 mi).
        token = state["token"]
        # Persistent detail cache keyed by MERCHANT DOMAIN (offer ids change
        # daily; domains don't). So we only ever fetch merchants we've never
        # seen — daily runs make just a few calls. Anti-ban.
        cache = {}
        if DETAILS_CACHE.exists():
            try:
                cache = json.loads(DETAILS_CACHE.read_text())
            except Exception:
                cache = {}
        if token:
            def worth_detail(it):
                # ONLY "capped" (Up to X) offers need the per-offer detail call —
                # it's the only way to see their hidden flat sub-tiers (e.g. AT&T
                # Prepaid 7,800). Flat offers already carry their reward in the
                # feed list, and multipliers never qualify — so neither needs a
                # detail call. This keeps detail traffic to a handful/day and
                # avoids throttling the offers/<id> endpoint.
                bt = (it.get("buttonText") or "")
                rt, _ = P.classify_reward(bt, it.get("text", "") or "")
                return rt == "capped"
            # candidates = (domain, id) for CAPPED offers not yet cached
            todo, seen_dom = [], set()
            for it in feed_items:
                dom = (it.get("merchantTLD") or "").lower()
                oid = it.get("id")
                if not dom or not oid or not worth_detail(it):
                    continue
                if dom in cache or dom in seen_dom:
                    continue
                seen_dom.add(dom)
                todo.append((dom, oid))
            todo = todo[:MAX_DETAILS]         # hard cap per run
            print(f"  fetching tier detail for {len(todo)} NEW merchants "
                  f"({len(cache)} cached, {DETAIL_DELAY_SEC}s apart)...", flush=True)
            for n, (dom, oid) in enumerate(todo, 1):
                durl = f"{base}/xhr/feed/{token}/offers/{oid}"
                stop = False
                for attempt in range(3):      # retry for transient rate-limits
                    try:
                        dr = page.request.get(durl, headers=hdrs, timeout=15000)
                        if dr.status == 429:
                            print("  ⚠️ HTTP 429 rate-limit — STOPPING detail "
                                  "fetch now (progress cached, re-run later).",
                                  file=sys.stderr)
                            stop = True
                            break
                        dj = dr.json()
                        off = dj.get("offer") if isinstance(dj, dict) else None
                        if off:
                            cache[dom] = off
                        break
                    except Exception:
                        time.sleep(1.0)
                if stop:
                    break
                print(f"    [{n}/{len(todo)}] {dom}", flush=True)
                time.sleep(DETAIL_DELAY_SEC)  # very gentle pacing

        DATA_DIR.mkdir(exist_ok=True)
        DETAILS_CACHE.write_text(json.dumps(cache, indent=2))
        (DATA_DIR / f"feed_{today0}.json").write_text(json.dumps(feed_items, indent=2))
        ctx.close()

    # Build offers from the feed API, de-duped by merchant+reward, and attach
    # the tier breakdown + terms from the detail endpoint.
    source = "feed-api"
    offers, keys = [], set()
    for it in feed_items:
        o = P.parse_feed_item(it)
        if not o:
            continue
        k = (o["merchant"].lower(), o["reward_type"], o["reward_miles"])
        if k in keys:
            continue
        keys.add(k)
        det = cache.get((o.get("domain") or "").lower())
        if det:
            o["tiers"] = P.parse_detail_categories(det)
            o["detail_terms"] = P.detail_terms_text(det)
            # flat tiers = genuine lump-sum opportunities (any 'N miles' tier)
            o["flat_tiers"] = [t for t in o["tiers"] if t["reward_type"] == "flat"]
        offers.append(o)

    today = dt.date.today().isoformat()
    out = DATA_DIR / f"offers_{today}.json"
    out.write_text(json.dumps(
        {"scraped_at": dt.datetime.now().isoformat(), "source": source,
         "offers": offers}, indent=2))

    flat = sum(1 for o in offers if o["reward_type"] == "flat")
    print(f"scraped {len(offers)} offers ({flat} flat) via {source} -> {out}")
    print(f"\nnext: python3 score.py {out}")
    return 0


def run_detail(headless: bool, url: str, limit: int) -> int:
    """Stage A: open each offer modal, capture tier menu + terms, and record
    every JSON network response (that's where the tier data comes from) so we
    can parse real tiers instead of guessing at the DOM."""
    PROFILE_DIR.mkdir(exist_ok=True); DATA_DIR.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    api_hits: list[dict] = []
    details: list[dict] = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--hide-crash-restore-bubble"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            # capture ALL json responses (no keyword filter) so we find the
            # per-offer detail endpoint even if its shape is unexpected.
            try:
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = resp.text()
                if len(body) < 2_000_000:
                    api_hits.append({"url": resp.url, "status": resp.status,
                                     "body": body[:1_000_000]})
            except Exception:
                pass
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        if "signin" in page.url.lower() or "login" in page.url.lower():
            print("error: not logged in. run: python3 scrape.py --login", file=sys.stderr)
            ctx.close(); return 2
        autoscroll(page)

        tiles = page.query_selector_all(".standard-tile")
        print(f"found {len(tiles)} .standard-tile offers; opening up to {limit}...")

        for i, tile in enumerate(tiles[:limit]):
            merchant = ""
            try:
                img = tile.query_selector("img[alt]")
                merchant = (img.get_attribute("alt") or "").strip() if img else ""
                tile.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.15)
                napi = len(api_hits)
                # native click first; fall back to a direct JS click (dispatches
                # the event straight on the element, bypassing overlay/interception)
                try:
                    tile.click(timeout=2500)
                except Exception:
                    try:
                        tile.click(timeout=1500, force=True)
                    except Exception:
                        tile.evaluate("el => el.click()")
                # wait for a modal to appear (dialog, or detail text/network)
                try:
                    page.wait_for_selector(
                        "[role=dialog], text=/offer terms/i, text=/Shop Online/i",
                        timeout=1800)
                except Exception:
                    pass
                time.sleep(0.4)
                modal = page.evaluate(r"""
                  () => {
                    const d = document.querySelector('[role=dialog]')
                      || document.querySelector('[class*="modal" i],[class*="dialog" i]');
                    if (!d) return null;
                    const alts = [];
                    d.querySelectorAll('img[alt]').forEach(i=>{const a=(i.alt||'').trim(); if(a) alts.push(a);});
                    return {text: (d.innerText||'').trim(), img_alts: alts};
                  }
                """)
                details.append({"merchant": merchant,
                                "new_api_responses": len(api_hits) - napi,
                                **(modal or {"text": None})})
                ok = "Y" if (modal and modal.get("text")) else "n"
                print(f"  [{i+1}/{min(limit,len(tiles))}] {merchant[:24]:24} "
                      f"modal={ok} newapi={len(api_hits)-napi}", flush=True)
                page.keyboard.press("Escape")
                time.sleep(0.35)
            except Exception as e:
                details.append({"merchant": merchant, "error": str(e)[:200]})
                print(f"  [{i+1}] {merchant[:24]:24} ERROR {str(e)[:60]}", flush=True)
                try: page.keyboard.press("Escape")
                except Exception: pass
                time.sleep(0.3)

        ctx.close()

    (DATA_DIR / f"details_{today}.json").write_text(json.dumps(details, indent=2))
    (DATA_DIR / f"api_{today}.json").write_text(json.dumps(api_hits, indent=2))
    got_modal = sum(1 for d in details if d.get("text"))
    print(f"captured {got_modal}/{len(details)} modals with text -> data/details_{today}.json")
    print(f"captured {len(api_hits)} json api responses -> data/api_{today}.json")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Scrape Capital One Offers feed.")
    ap.add_argument("--login", action="store_true",
                    help="open browser to sign in once; saves session")
    ap.add_argument("--detail", action="store_true",
                    help="Stage A: open each offer modal, capture tiers+terms+api")
    ap.add_argument("--limit", type=int, default=120,
                    help="max offers to open in --detail mode")
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window (riskier for bot-detection)")
    ap.add_argument("--url", default=FEED_URL)
    args = ap.parse_args(argv)
    if args.detail:
        return run_detail(args.headless, args.url, args.limit)
    return run(args.login, args.headless, args.url)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
