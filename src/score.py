#!/usr/bin/env python3
"""
Points arbitrage scoring engine.

Input : a JSON file of scraped Capital One offers (see offers.schema.json).
Output: a ranked markdown report of flat-reward offers that clear your
        value-to-cost ratio, plus a macOS notification if any qualify.

Usage:
    python3 score.py data/offers_2026-09-23.json
    python3 score.py data/offers_2026-09-23.json --config config.yaml

The Chrome scraping step is done interactively by the agent, which writes
the offers JSON. This script is the deterministic math + reporting half.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root (src/ is one level down)


# ---- config loading (tiny YAML subset, no dependency) ----------------------

DEFAULTS = {
    "mile_value_cents": 1.0,
    "mile_value_aspirational_cents": 1.85,
    "min_ratio": 2.0,
    "min_flat_reward_miles": 1000,
    "assumed_floor_cost_usd": 8.0,
    "show_multiplier_offers": False,
    "time_value_per_hour": 50.0,
    "min_points_per_minute": 200.0,
    # Auto-judged (LLM) deals above this ratio are almost always a misread price
    # (a $0.71 topper, a $1 order). They get pulled into a "verify" bucket rather
    # than shown as clean deals. Hand-verified finds are exempt.
    "max_auto_ratio": 12.0,
}

# Fallback effort (minutes to complete the purchase) by purchase type, used when
# the LLM didn't estimate it. Buy-and-keep is quick; subscriptions add signup +
# a cancel reminder; service commitments are long (and usually filtered anyway).
EFFORT_MINUTES = {"one_time_good": 5, "subscription": 12,
                  "service_commitment": 90, "": 10}


def load_config(path: Path | None) -> dict:
    cfg = dict(DEFAULTS)
    if not path or not path.exists():
        return cfg
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = (p.strip() for p in line.split(":", 1))
        if key not in cfg:
            continue
        low = val.lower()
        if low in ("true", "false"):
            cfg[key] = low == "true"
        else:
            try:
                cfg[key] = float(val) if "." in val else int(val)
            except ValueError:
                cfg[key] = val
    return cfg


# ---- scoring ----------------------------------------------------------------

def score_offer(offer: dict, cfg: dict) -> dict:
    """Attach computed value/cost/ratio fields to a copy of an offer.

    Offer fields expected (missing ones handled gracefully):
      merchant       str
      reward_type    "flat" | "multiplier"
      reward_miles   number   (flat: total miles; multiplier: miles per $1)
      min_spend_usd  number | null
      url            str
      terms          str
    """
    o = dict(offer)
    rtype = o.get("reward_type", "flat")
    miles = float(o.get("reward_miles") or 0)
    mv = cfg["mile_value_cents"] / 100.0
    mv_asp = cfg["mile_value_aspirational_cents"] / 100.0

    if rtype == "multiplier":
        # miles per $1 -> value returned per $1. Ratio is just miles*mv.
        o["value_usd"] = None
        o["cost_usd"] = None
        o["ratio"] = round(miles * mv, 3)  # e.g. 5x * $0.01 = 0.05
        o["ratio_aspirational"] = round(miles * mv_asp, 3)
        o["needs_confirm"] = False
        o["reason"] = "multiplier — value per $1 spent"
        return o

    if rtype == "capped":
        # "Up to N miles" is a ceiling on a spend-proportional offer, not a
        # guaranteed lump sum. Not scoreable as a deal — you'd need huge spend.
        o["value_usd"] = None
        o["cost_usd"] = None
        o["ratio"] = 0.0
        o["ratio_aspirational"] = 0.0
        o["needs_confirm"] = False
        o["reason"] = "'up to' cap — needs large spend, not a flat bonus"
        return o

    # flat reward
    value = miles * mv
    value_asp = miles * mv_asp

    # Stage B overlay: a real named in-stock item for this merchant?
    find = (o.get("_find") or {})
    if find.get("price") is not None:
        cost = float(find["price"])
        o["needs_confirm"] = not find.get("in_stock", True)
        o["item"] = find.get("item", "")
        o["item_url"] = find.get("url", "")
        o["reason"] = find.get("note", "named in-stock item")
    else:
        min_spend = o.get("min_spend_usd")
        if min_spend is None:
            cost = float(cfg["assumed_floor_cost_usd"])
            o["needs_confirm"] = True
            o["reason"] = "no item found yet — assumed floor; run item-finder"
        else:
            cost = float(min_spend)
            o["needs_confirm"] = cost <= 0
            o["reason"] = "min spend from terms"

    o["value_usd"] = round(value, 2)
    o["value_aspirational_usd"] = round(value_asp, 2)
    o["cost_usd"] = round(cost, 2)
    o["ratio"] = round(value / cost, 2) if cost > 0 else float("inf")
    o["ratio_aspirational"] = round(value_asp / cost, 2) if cost > 0 else float("inf")
    return o


def qualifies(o: dict, cfg: dict) -> bool:
    # only genuine flat lump-sum offers can be deals
    if o.get("reward_type") != "flat":
        return False
    if float(o.get("reward_miles") or 0) < cfg["min_flat_reward_miles"]:
        return False
    return o.get("ratio", 0) >= cfg["min_ratio"]


# Reward requires a service signup / subscription / contract, not a cheap
# one-time purchase — so there's no Pinter-style cheap item. Detected from
# merchant, domain, and terms.
_SERVICE_DOMAINS = ("t-mobile", "att.com", "attwireless", "boostmobile", "cricket",
                    "metrobyt", "visible", "consumercellular", "straighttalk",
                    "mintmobile", "verizon", "xfinity", "spectrum", "fiber.",
                    "postpaid", "doordash", "ubereats", "paramountplus", "peacocktv",
                    "disney", "weightwatchers", "homechef", "hungryroot", "intuit",
                    "wsj.com", "lemonade", "remitly", "superhuman", "cloaked",
                    "fortect", "sling", "babbel", "dasher")
_SERVICE_TERMS = ("new line", "add a line", "activation", "new customer",
                  "subscription", "monthly", "first box", "first order", "sign up",
                  "new service", "plan", "membership", "trial", "per month")


# Carriers where "buying a plan" actually needs a compatible phone + activation.
_PREPAID_PHONE = ("attwireless", "att.com", "t-mobile", "postpaid", "boostmobile",
                  "cricketwireless", "metrobyt", "visible.com", "mintmobile",
                  "consumercellular", "straighttalk", "totalwireless")


def deal_caveat(opp: dict) -> str:
    """A short buyer-beware note for deals whose 'cheap price' hides real
    prerequisites (a phone, new-customer rules, a hold period)."""
    dom = (opp.get("domain") or "").lower()
    ptype = opp.get("purchase_type", "")
    if any(k in dom for k in _PREPAID_PHONE):
        return ("needs a spare unlocked phone (BYOD) + must be a NEW customer + "
                "miles post ~45 days after the first bill clears, then cancel")
    if ptype == "subscription":
        return ("new customers only; miles post after the 1st bill clears "
                "(~45 days), then cancel/don't renew")
    return ""


def barrier_kind(o: dict) -> str:
    """'service' (reward needs a plan/subscription/signup — no cheap item) or
    'goods' (a physical store where a cheap qualifying item may exist)."""
    dom = (o.get("domain") or "").lower()
    if any(s in dom for s in _SERVICE_DOMAINS):
        return "service"
    terms = (o.get("detail_terms") or o.get("terms") or "").lower()
    hits = sum(1 for k in _SERVICE_TERMS if k in terms)
    return "service" if hits >= 2 else "goods"


# ---- reporting --------------------------------------------------------------

def load_finds(path: Path) -> dict:
    """Stage B results keyed by merchant domain (cheapest in-stock item)."""
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


def offer_opportunities(o: dict) -> list[dict]:
    """Enumerate every FLAT earning path in an offer as a separate opportunity.

    An offer is a menu: the headline flat reward (buy anything) PLUS each flat
    tier/category (e.g. AT&T 'Prepaid' -> 7,800 mi for a cheap prepaid SIM).
    Multiplier tiers are skipped — they scale with spend and never arbitrage.
    """
    opps, seen = [], set()
    def add(category, miles):
        miles = float(miles or 0)
        if miles <= 0:
            return
        key = (category.lower(), miles)
        if key in seen:
            return
        seen.add(key)
        opps.append({"merchant": o.get("merchant", "?"), "domain": o.get("domain", ""),
                     "url": o.get("url", ""), "category": category,
                     "reward_miles": miles})
    if o.get("reward_type") == "flat":
        add("Any purchase", o.get("reward_miles"))
    for t in o.get("tiers", []) or []:
        if t.get("reward_type") == "flat":
            add(t.get("name") or "tier", t.get("reward_miles"))
    return opps


def find_for(domain: str, category: str, finds: dict) -> dict | None:
    """Look up a priced item for this domain+category. Keys may be
    'domain#Category' (specific) or 'domain' (whole-offer/any-purchase)."""
    dom = (domain or "").lower()
    for key in (f"{dom}#{(category or '').lower()}", dom):
        for fk, fv in finds.items():
            if fk.lower() == key:
                return fv
    return None


def price_opportunity(opp: dict, cfg: dict, finds: dict) -> dict:
    mv = cfg["mile_value_cents"] / 100.0
    mv_asp = cfg["mile_value_aspirational_cents"] / 100.0
    opp["value_usd"] = round(opp["reward_miles"] * mv, 2)
    opp["value_aspirational_usd"] = round(opp["reward_miles"] * mv_asp, 2)
    find = find_for(opp["domain"], opp["category"], finds)
    if find and find.get("price") is not None and float(find["price"]) > 0:
        cost = float(find["price"])
        ptype = find.get("purchase_type", "")
        # effort: LLM estimate if present, else per-type fallback
        effort = find.get("effort_minutes")
        try:
            effort = float(effort) if effort else EFFORT_MINUTES.get(ptype, 10)
        except (TypeError, ValueError):
            effort = EFFORT_MINUTES.get(ptype, 10)
        effort = max(effort, 1.0)
        # profit in POINTS = miles earned minus the cost expressed in points
        cost_points = cost / (mv)          # $cost / ($/mile) = miles-equivalent
        net_points = opp["reward_miles"] - cost_points
        # net dollars after charging your time at the hourly rate
        time_cost = effort * (cfg["time_value_per_hour"] / 60.0)
        opp.update(priced=True, cost_usd=round(cost, 2),
                   item=find.get("item", ""), item_url=find.get("url", ""),
                   in_stock=find.get("in_stock", True), note=find.get("note", ""),
                   auto=find.get("auto", False),
                   purchase_type=ptype, effort_min=round(effort, 1),
                   net_points=round(net_points),
                   points_per_min=round(net_points / effort),
                   net_after_time_usd=round(opp["value_usd"] - cost - time_cost, 2),
                   ratio=round(opp["value_usd"] / cost, 2),
                   ratio_aspirational=round(opp["value_aspirational_usd"] / cost, 2))
    else:
        opp.update(priced=False, cost_usd=None, item="", ratio=None)
    return opp


def build_report(offers: list[dict], cfg: dict, source: str,
                 finds: dict | None = None) -> tuple[str, list[dict]]:
    finds = finds or {}
    # Flatten offers into per-tier opportunities, then price each.
    opps = []
    for o in offers:
        for opp in offer_opportunities(o):
            if opp["reward_miles"] >= cfg["min_flat_reward_miles"]:
                opps.append(price_opportunity(opp, cfg, finds))

    priced = [x for x in opps if x["priced"]]
    unpriced = [x for x in opps if not x["priced"]]
    winners = sorted([x for x in priced if x["ratio"] >= cfg["min_ratio"]],
                     key=lambda x: x["ratio"], reverse=True)
    marginal = sorted([x for x in priced if 1.0 <= x["ratio"] < cfg["min_ratio"]],
                      key=lambda x: x["ratio"], reverse=True)
    loss = sorted([x for x in priced if x["ratio"] < 1.0],
                  key=lambda x: x["ratio"], reverse=True)
    # unpriced: rank by potential value; note the category (what to buy)
    to_research = sorted(unpriced, key=lambda x: -x["value_usd"])

    today = dt.date.today().isoformat()
    L = [f"# Points deals — {today}", "",
         f"_Source: {source} · mile value {cfg['mile_value_cents']}¢ "
         f"(aspirational {cfg['mile_value_aspirational_cents']}¢) · "
         f"threshold {cfg['min_ratio']}x · {len(opps)} flat opportunities_", ""]

    # split clean deals (buy & keep / cancel anytime) from service commitments,
    # and rank by RETURN ON YOUR TIME (net points per minute of effort).
    non_commit = [x for x in winners if x.get("purchase_type") != "service_commitment"]
    commit = [x for x in winners if x.get("purchase_type") == "service_commitment"]
    # deterministic backstop: auto-judged deals with an implausibly high ratio are
    # almost always a misread price — quarantine them for manual verification.
    cap = cfg["max_auto_ratio"]
    suspicious = sorted([x for x in non_commit
                         if x.get("auto") and x.get("ratio", 0) > cap],
                        key=lambda x: -x.get("ratio", 0))
    clean = sorted([x for x in non_commit
                    if not (x.get("auto") and x.get("ratio", 0) > cap)],
                   key=lambda x: -x.get("points_per_min", 0))
    ppm_bar = cfg["min_points_per_minute"]

    # 🏆 single best recommendation by points-per-minute. Prefer a HAND-VERIFIED
    # deal for the headline; auto-judged ones can be wrong, so they're leads.
    if clean:
        verified = [x for x in clean if not x.get("auto")]
        top = verified[0] if verified else clean[0]
        link = f"[{top.get('item') or top['merchant']}]({top.get('item_url') or top.get('url','')})"
        kind = {"one_time_good": "one-time buy", "subscription": "1-month, then cancel"}.get(
            top.get("purchase_type"), "")
        worth = "✅ clears" if top.get("points_per_min", 0) >= ppm_bar else "⚠️ below"
        top_cav = deal_caveat(top)
        L += [f"## 🏆 Top pick: {top['merchant']} — "
              f"{int(top.get('points_per_min',0)):,} pts/min", "",
              f"**Buy:** {link} — **${top['cost_usd']:.2f}**"
              + (f" ({kind})" if kind else ""),
              f"**Get:** {int(top['reward_miles']):,} miles (~${top['value_usd']:.0f}, "
              f"up to ${top.get('value_aspirational_usd',0):.0f}) · {top['ratio']:.1f}x",
              f"**Effort:** ~{top.get('effort_min',0):.0f} min → "
              f"**{int(top.get('points_per_min',0)):,} net pts/min** "
              f"({worth} your {ppm_bar:.0f}/min bar) · ${top.get('net_after_time_usd',0):.0f} "
              f"net after your time"]
        if top_cav:
            L += [f"**⚠️ Catch:** {top_cav}"]
        L += [f"_How: open the **{top['merchant']}** offer in your Capital One portal → "
              f"click **Shop Online** → buy the item above on the merchant site._", ""]

    if clean:
        L += [f"## ✅ {len(clean)} clean deal(s), ranked by return on your time", "",
              "| ✓ | Merchant | Item | Type | Cost | Miles | Ratio | Effort | **Net pts/min** | Catch |",
              "|:--:|---|---|---|--:|--:|--:|--:|--:|:--:|"]
        cav_notes = []
        for x in clean:
            item = x.get("item") or "—"
            if x.get("item_url"):
                item = f"[{item}]({x['item_url']})"
            ptype = {"one_time_good": "one-time", "subscription": "sub (cancel)"}.get(
                x.get("purchase_type"), "—")
            ppm = int(x.get("points_per_min", 0))
            src = "🤖" if x.get("auto") else "✓"      # 🤖 = auto-judged, verify; ✓ = hand-verified
            cav = deal_caveat(x)
            catch = "⚠️" if cav else ("✅" if ppm >= ppm_bar else "—")
            if cav:
                cav_notes.append(f"- **{x['merchant']}** — ⚠️ {cav}")
            L.append(f"| {src} | [{x['merchant']}]({x.get('url','')}) | {item} | {ptype} "
                     f"| ${x['cost_usd']:.2f} | {int(x['reward_miles']):,} "
                     f"| {x['ratio']:.1f}x | ~{x.get('effort_min',0):.0f}m "
                     f"| **{ppm:,}** | {catch} |")
        L += ["", "_✓ = price hand-verified · 🤖 = auto-judged by the LLM (sanity-check "
              "before buying) · ⚠️ = has a catch (see below)._"]
        if cav_notes:
            L += ["", "**⚠️ Catches — read before buying these:**"] + cav_notes
        L += ["", f"_Net pts/min = miles earned minus cost (in points), per minute "
              f"of effort. Your bar: **{ppm_bar:.0f}/min** (≈ your "
              f"${cfg['time_value_per_hour']:.0f}/hr)._", ""]
    else:
        L += ["## ✅ No clean deals cleared the threshold today", ""]

    if suspicious:
        L += [f"## ⚠️ {len(suspicious)} too-good-to-be-true — VERIFY the price yourself",
              "", f"_Auto-judged at over {cap:.0f}x — almost always a misread "
              "(a page fragment, a per-unit price, a '$X off'). Open the link and "
              "confirm the real cheapest price before trusting these._", "",
              "| Merchant | Item | Claimed cost | Miles | Claimed ratio | Link |",
              "|---|---|--:|--:|--:|---|"]
        for x in suspicious:
            L.append(f"| {x['merchant']} | {x.get('item','')} | ${x['cost_usd']:.2f} "
                     f"| {int(x['reward_miles']):,} | {x['ratio']:.1f}x "
                     f"| [{x.get('domain','')}]({x.get('item_url') or x.get('url','')}) |")
        L.append("")

    if commit:
        L += [f"<details><summary>⚠️ {len(commit)} high-ratio but require a service "
              f"signup (fiber/TV/contract — not a cheap easy buy)</summary>", "",
              "| Merchant | Item | Cost | Miles | Ratio |", "|---|---|--:|--:|--:|"]
        for x in commit:
            L.append(f"| [{x['merchant']}]({x.get('item_url') or x.get('url','')}) "
                     f"| {x.get('item','')} | ${x['cost_usd']:.2f} "
                     f"| {int(x['reward_miles']):,} | {x['ratio']:.1f}x |")
        L += ["", "</details>", ""]

    if marginal:
        L += [f"## 🟡 {len(marginal)} marginal (1–2x)", "",
              "| Merchant | Category | Item | Cost | Miles | Ratio |",
              "|---|---|---|--:|--:|--:|"]
        for x in marginal:
            L.append(f"| {x['merchant']} | {x['category']} | {x.get('item','')} "
                     f"| ${x['cost_usd']:.2f} | {int(x['reward_miles']):,} "
                     f"| {x['ratio']:.1f}x |")
        L.append("")

    if loss:
        L += [f"<details><summary>❌ Skip: under 1x ({len(loss)})</summary>", "",
              "| Merchant | Category | Cost | Miles | Ratio |", "|---|---|--:|--:|--:|"]
        for x in loss[:30]:
            L.append(f"| {x['merchant']} | {x['category']} | ${x['cost_usd']:.2f} "
                     f"| {int(x['reward_miles']):,} | {x['ratio']:.1f}x |")
        L += ["", "</details>", ""]

    if to_research:
        L += [f"## 🔎 {len(to_research)} opportunities to price "
              "(find cheapest item in the category)", "",
              "_Each row: buy the cheapest qualifying item in that category to "
              "claim the miles. Run the item-finder or price by hand._", "",
              "| Merchant | Buy category | Miles | Max value | Store |",
              "|---|---|--:|--:|---|"]
        for x in to_research[:40]:
            L.append(f"| {x['merchant']} | **{x['category']}** "
                     f"| {int(x['reward_miles']):,} | ${x['value_usd']:.0f} "
                     f"| [{x['domain']}]({x.get('url','')}) |")
        L.append("")

    L += ["---",
          "_Reminder: verify a qualifying purchase exists AND read the terms "
          "(new-customer-only, one-time, expiry, hold period). Capital One can "
          "claw back offers it deems gamed._"]
    return "\n".join(L), clean   # notify on the clean top pick, not a signup


def notify(winners: list[dict], cfg: dict) -> None:
    if not winners:
        return
    top = winners[0]
    title = f"{len(winners)} points deal(s) >= {cfg['min_ratio']}x"
    body = (f"Top: {top.get('merchant','?')} - {int(top.get('reward_miles',0)):,} mi "
            f"(~${top.get('value_usd',0):.0f}) at {top.get('ratio',0):.1f}x")
    # ensure_ascii=False keeps unicode literal; AppleScript can't parse \uXXXX.
    js = lambda s: json.dumps(s, ensure_ascii=False)
    script = (f'display notification {js(body)} '
              f'with title {js(title)} sound name "Glass"')
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass  # notification is best-effort


# ---- main -------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Score scraped points offers.")
    ap.add_argument("offers_json", type=Path)
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--finds", type=Path, default=ROOT / "finds.json",
                    help="Stage B named-item results keyed by domain")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (default reports/report_<date>.md)")
    args = ap.parse_args(argv)

    if not args.offers_json.exists():
        print(f"error: {args.offers_json} not found", file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    payload = json.loads(args.offers_json.read_text())
    offers = payload if isinstance(payload, list) else payload.get("offers", [])

    finds = load_finds(args.finds)
    report, winners = build_report(offers, cfg, args.offers_json.name, finds)

    out = args.out or Path("reports") / f"report_{dt.date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)

    print(report)
    print(f"\n[written] {out}")
    if not args.no_notify:
        notify(winners, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
