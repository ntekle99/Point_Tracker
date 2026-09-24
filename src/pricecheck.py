#!/usr/bin/env python3
"""
Merchant price-finder: render a JS site in a real browser and extract prices.

This is the linchpin of the autonomous item-finder. Static page-readers (and
WebFetch) can't see JS-rendered prices (att.com, boost, etc.); a rendered
browser can. It hits MERCHANT sites only — never Capital One — so there is no
rate-limit/ban concern here.

Usage:
    python3 pricecheck.py https://www.att.com/prepaid/plans/
    python3 pricecheck.py https://www.att.com/prepaid/plans/ --json

Outputs the cheapest prices found with the text around them, so a human (or an
agent step) can pick the cheapest QUALIFYING item. Best-effort: some sites lazy
-load, gate by location, or block bots — those are reported, not faked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from playwright.sync_api import sync_playwright

# handle commas: $1,299.99 -> 1299.99 (not 1)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{1,4}(?:\.\d{2})?)", re.I)


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def find_prices(url: str, headless: bool = True, wait_ms: int = 1500) -> dict:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        info = {"url": url, "prices": [], "ok": False, "note": ""}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)
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
                  // climb to a small block for context
                  let el = n.parentElement, ctx = t;
                  for (let i=0;i<3 && el;i++){ const c=(el.innerText||'').trim();
                    if (c && c.length<=200){ ctx=c; el=el.parentElement; } else break; }
                  if (seen.has(ctx)) continue; seen.add(ctx);
                  // nearest product link: a link wrapping the price, else one in
                  // the surrounding card (skip nav/cart/account/social links).
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
                return out.slice(0, 60);
              }
            """)
            prices = []
            for it in items:
                ctx_txt, purl = it.get("text", ""), it.get("url", "")
                for m in PRICE_RE.finditer(ctx_txt):
                    prices.append({"amount": _to_float(m.group(1)),
                                   "context": ctx_txt[:140], "url": purl})
            # dedupe (keep first url) + sort ascending
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


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", action="store_true", help="run headed (visible)")
    args = ap.parse_args(argv)
    info = find_prices(args.url, headless=not args.show)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"URL: {info['url']}")
    if not info["ok"]:
        print(f"  ✗ {info['note']}")
        return 1
    print(f"  cheapest prices found (ascending):")
    for p in info["prices"][:12]:
        print(f"    ${p['amount']:>7.2f}  {p['context']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
