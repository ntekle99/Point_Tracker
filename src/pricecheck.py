#!/usr/bin/env python3
"""
Merchant price-finder: render a JS site in a real browser and extract prices.

This is the linchpin of the autonomous item-finder. Static page-readers (and
WebFetch) can't see JS-rendered prices (att.com, boost, verizon, etc.); a
rendered browser can.

SAFETY (this file is deliberately gentle — merchants DO rate-limit/bot-block):
  • ONE navigation per call. Never reloads, never retries, never "hits refresh".
  • A process-wide minimum delay is enforced between navigations (with jitter),
    so back-to-back calls in a hunt can't hammer a site.
  • If a page returns a bot-block / rate-limit (HTTP 403/418/429 or a known
    block page), we ABORT immediately with blocked=True and do NOT retry — the
    caller should stop hitting that merchant.
  • Stealth headers + masked automation flags reduce the chance of tripping the
    block in the first place.

CHEAPEST-FINDING:
  • On catalog/listing pages, if a "Price: low to high" sort control exists we
    select it ONCE (a normal user interaction, not a reload) so the cheap tail
    of a 1,000+ item catalog actually surfaces — otherwise a static read only
    sees the "Featured" page and misses the cheapest item entirely.
  • Prices are returned ascending, each with its surrounding text and the
    nearest product deep-link.

Usage:
    python3 pricecheck.py https://www.att.com/prepaid/plans/
    python3 pricecheck.py <catalog-url> --referer <affiliate-url> --json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

# handle commas: $1,299.99 -> 1299.99 (not 1)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{1,4}(?:\.\d{2})?)", re.I)

# ---------------------------------------------------------------------------
# Process-wide pacing. Enforced BEFORE every navigation so no run — however
# many opportunities it prices — can fire requests faster than this. Tune with
# PRICECHECK_MIN_INTERVAL (seconds); default is intentionally conservative.
# ---------------------------------------------------------------------------
_MIN_INTERVAL_SEC = float(os.environ.get("PRICECHECK_MIN_INTERVAL", "8"))
_last_nav_monotonic = 0.0

# Signals that we've been blocked / throttled. On any of these we STOP — we do
# not retry, because retrying a block is exactly what escalates it to a ban.
_BLOCK_STATUSES = {403, 418, 429, 503}
_BLOCK_MARKERS = (
    "isn't available",
    "unable to process",
    "unable to retrieve",
    "access denied",
    "request unsuccessful",
    "pardon our interruption",
    "are you a human",
    "verify you are a human",
    "detected unusual",
    "temporarily blocked",
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

_EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _pace() -> None:
    """Block until at least _MIN_INTERVAL_SEC (plus jitter) has passed since the
    last navigation anywhere in this process. This is the core anti-hammer guard."""
    global _last_nav_monotonic
    elapsed = time.monotonic() - _last_nav_monotonic
    wait = _MIN_INTERVAL_SEC - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0.4, 1.6))
    _last_nav_monotonic = time.monotonic()


def _looks_blocked(status: int, head_text: str) -> bool:
    if status in _BLOCK_STATUSES:
        return True
    low = head_text.lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _try_sort_low_to_high(page) -> bool:
    """If a native <select> offers a price-ascending sort, pick it ONCE. This is
    a user-style interaction (fires one XHR for sorted results), never a reload.
    Best-effort: any failure is swallowed and we just read the page as-is."""
    try:
        for sel in page.query_selector_all("select"):
            for opt in sel.query_selector_all("option"):
                label = (opt.inner_text() or "").strip()
                if "low to high" in label.lower():
                    val = opt.get_attribute("value")
                    if val:
                        sel.select_option(value=val)
                    else:
                        sel.select_option(label=label)
                    page.wait_for_timeout(3500)
                    return True
    except Exception:
        pass
    return False


def _gentle_scroll(page, steps: int = 5) -> None:
    """Nudge lazy-loaded tiles into rendering. Scrolling only — no navigation."""
    for _ in range(steps):
        try:
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(800)
        except Exception:
            break


def find_prices(url: str, headless: bool = True, wait_ms: int = 1500,
                referer: str | None = None, sort_cheapest: bool = True) -> dict:
    """Render `url` ONCE and return prices ascending. Never retries.

    referer:       pass the affiliate/landing URL to navigate "in session" (helps
                   dodge blocks and preserves affiliate cookies).
    sort_cheapest: on catalog pages, select "Price low to high" before reading.

    Returns {url, prices:[{amount, context, url}], ok, blocked, note}.
    """
    info = {"url": url, "prices": [], "ok": False, "blocked": False, "note": ""}
    _pace()  # <-- enforce the min interval BEFORE we touch the network
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            extra_http_headers=_EXTRA_HEADERS)
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000,
                             referer=referer)
            status = resp.status if resp else 0

            # Read a small slice of the body to sniff for block pages.
            try:
                head_text = page.inner_text("body")[:400]
            except Exception:
                head_text = ""

            if _looks_blocked(status, head_text):
                info["blocked"] = True
                info["note"] = (f"bot-block / rate-limit (HTTP {status}) — "
                                f"aborting WITHOUT retry; back off this merchant")
                return info  # <-- critical: no retry, no refresh, just stop

            # let the app settle (Verizon & co never go network-idle; keep short)
            try:
                page.wait_for_load_state("networkidle", timeout=3500)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)

            _gentle_scroll(page, steps=4)
            if sort_cheapest:
                if _try_sort_low_to_high(page):
                    _gentle_scroll(page, steps=5)  # re-render sorted tiles

            # collect price tokens with surrounding text AND the nearest product
            # link, so we can deep-link the exact item (not the homepage).
            items = page.evaluate(r"""
              () => {
                const re = /\$\s?\d{1,4}(?:\.\d{2})?/;
                const out = [];
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const seen = new Set();
                let n;
                while ((n = walk.nextNode())) {
                  const t = (n.textContent || '').trim();
                  if (!t || t.length > 160 || !re.test(t)) continue;
                  let el = n.parentElement, ctx = t;
                  for (let i=0;i<3 && el;i++){ const c=(el.innerText||'').trim();
                    if (c && c.length<=200){ ctx=c; el=el.parentElement; } else break; }
                  if (seen.has(ctx)) continue; seen.add(ctx);
                  let url = '';
                  const bad = /(cart|account|login|sign[- ]?in|help|support|privacy|terms|facebook|instagram|twitter|tiktok|youtube|#$)/i;
                  let a = n.parentElement ? n.parentElement.closest('a[href]') : null;
                  if (!a) {
                    let c = n.parentElement;
                    for (let i=0;i<4 && c && !a;i++){
                      const links = c.querySelectorAll ? c.querySelectorAll('a[href]') : [];
                      for (const l of links){ if (l.href && !bad.test(l.href)){ a=l; break; } }
                      c = c.parentElement;
                    }
                  }
                  if (a && a.href && !bad.test(a.href)) url = a.href;
                  out.push({text: ctx.replace(/\s+/g,' '), url});
                }
                return out.slice(0, 80);
              }
            """)
            prices = []
            for it in items:
                ctx_txt, purl = it.get("text", ""), it.get("url", "")
                for m in PRICE_RE.finditer(ctx_txt):
                    prices.append({"amount": _to_float(m.group(1)),
                                   "context": ctx_txt[:140], "url": purl})
            uniq = {}
            for p in prices:
                key = (p["amount"], p["context"])
                uniq.setdefault(key, p)
            info["prices"] = sorted(uniq.values(), key=lambda p: p["amount"])
            info["ok"] = bool(info["prices"])
            if not info["prices"]:
                info["note"] = "no prices found (JS-gated, location wall, or bot block)"
        except Exception as e:
            info["note"] = f"load error: {e}"
        finally:
            ctx.close(); browser.close()
        return info


def resolve_deep_link(page_url: str, item_hint: str, headless: bool = True,
                      referer: str | None = None) -> dict:
    """Best-effort EXACT deep link for one product: load the catalog page ONCE
    (paced, stealth), find the tile whose text contains `item_hint`, click it, and
    return where it lands. Used only for the handful of top picks — never in bulk.

    Returns {url|None, ok, blocked, note}. Callers fall back to the nearest-link
    already captured by find_prices() when ok is False.
    """
    out = {"url": None, "ok": False, "blocked": False, "note": ""}
    hint = (item_hint or "").strip()
    if not hint:
        out["note"] = "no item hint"
        return out
    _pace()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=_UA, locale="en-US",
                                  viewport={"width": 1440, "height": 1000},
                                  extra_http_headers=_EXTRA_HEADERS)
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        try:
            resp = page.goto(page_url, wait_until="domcontentloaded", timeout=30000,
                             referer=referer)
            status = resp.status if resp else 0
            try:
                head_text = page.inner_text("body")[:400]
            except Exception:
                head_text = ""
            if _looks_blocked(status, head_text):
                out["blocked"] = True
                out["note"] = f"bot-block (HTTP {status}) — not retrying"
                return out
            page.wait_for_timeout(1500)
            _gentle_scroll(page, steps=4)
            if sort_cheapest := True:
                _try_sort_low_to_high(page)
                _gentle_scroll(page, steps=3)
            # match on a distinctive fragment of the hint (first ~5 words)
            frag = " ".join(hint.split()[:5])
            loc = page.get_by_text(frag, exact=False).first
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=6000)
            page.wait_for_timeout(3000)
            landed = page.url
            if landed and landed.rstrip("/#") != page_url.rstrip("/#"):
                out["url"], out["ok"] = landed, True
            else:
                out["note"] = "click did not navigate to a distinct product URL"
        except Exception as e:
            out["note"] = f"resolve error: {str(e)[:120]}"
        finally:
            ctx.close(); browser.close()
        return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", action="store_true", help="run headed (visible)")
    ap.add_argument("--referer", default=None,
                    help="affiliate/landing URL to navigate in-session")
    ap.add_argument("--no-sort", action="store_true",
                    help="don't select 'Price low to high' on catalog pages")
    args = ap.parse_args(argv)
    info = find_prices(args.url, headless=not args.show, referer=args.referer,
                       sort_cheapest=not args.no_sort)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"URL: {info['url']}")
    if info.get("blocked"):
        print(f"  ⛔ {info['note']}")
        return 2
    if not info["ok"]:
        print(f"  ✗ {info['note']}")
        return 1
    print("  cheapest prices found (ascending):")
    for p in info["prices"][:12]:
        link = f"  ->  {p['url']}" if p["url"] else ""
        print(f"    ${p['amount']:>7.2f}  {p['context']}{link}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
