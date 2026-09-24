"""
Pure text -> structured offer parsing. No browser, no I/O — unit-testable.

Capital One Offers cards show a headline like "7,000 miles" or "5X miles" or
"Up to 3X miles", plus terms text that may contain a minimum spend. These
functions turn that raw text into the fields score.py consumes.
"""
from __future__ import annotations

import re

# "5X miles", "Up to 14X miles", "3x" -> multiplier
_MULT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX]\b")
# "7,000 miles", "7000 miles" -> flat lump sum
_FLAT_RE = re.compile(r"([\d,]{2,})\s*miles?\b", re.IGNORECASE)
# "% back" / "X% miles" also a multiplier-style (percent of spend)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# minimum spend: "spend $50", "minimum purchase of $60", "$50+"
_MIN_SPEND_RES = [
    re.compile(r"spend\s+\$?\s*([\d,]+(?:\.\d{2})?)", re.IGNORECASE),
    re.compile(r"minimum(?:\s+purchase)?(?:\s+of)?\s+\$?\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)\s*\+", re.IGNORECASE),
    re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)\s+or\s+more", re.IGNORECASE),
]


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_min_spend(terms: str | None) -> float | None:
    """Extract a minimum qualifying spend from terms text, else None."""
    if not terms:
        return None
    for rx in _MIN_SPEND_RES:
        m = rx.search(terms)
        if m:
            try:
                return _num(m.group(1))
            except ValueError:
                continue
    return None


def classify_reward(reward_text: str, context: str = "") -> tuple[str, float]:
    """Return (reward_type, reward_miles).

    Three buckets, checked in this order:
      1. "multiplier"  — "5X miles", "3% back": reward scales with spend, so it
                         NEVER nets positive. reward_miles = the per-$1 multiple.
      2. "capped"      — "Up to 12,000 miles": the number is a CEILING on a
                         spend-proportional offer, NOT a guaranteed lump sum. You
                         only hit it with huge spend, so it's not a deal either.
                         reward_miles = the cap (for display only).
      3. "flat"        — a bare "7,000 miles" with no "up to" and no multiplier:
                         a fixed lump sum. THIS is the arbitrage target.

    The "Up to" test is the crux — without it, capped ceilings masquerade as
    flat rewards and produce false "deals".
    """
    t = (reward_text or "").strip()
    low = t.lower()
    # the "up to" cue often lives in the surrounding card text, not the matched
    # reward snippet, so check both.
    ctx_low = (low + " " + (context or "").lower())

    # 1. explicit multiplier / percentage -> scales with spend
    m = _MULT_RE.search(t)
    if m:
        return "multiplier", _num(m.group(1))
    if _PCT_RE.search(t) and "mile" not in low:
        return "multiplier", _num(_PCT_RE.search(t).group(1))

    # 2. "up to N miles" -> capped ceiling, not a flat guarantee
    fm = _FLAT_RE.search(t)
    if fm and ("up to" in ctx_low or "up-to" in ctx_low):
        return "capped", _num(fm.group(1))

    # 3. bare "N miles" -> flat lump sum (the target)
    if fm:
        val = _num(fm.group(1))
        if val >= 1:
            return "flat", val
    return "flat", 0.0


def parse_offer(merchant: str, reward_text: str, terms: str | None = None,
                url: str = "") -> dict:
    rtype, miles = classify_reward(reward_text, terms or "")
    return {
        "merchant": (merchant or "").strip(),
        "reward_type": rtype,
        "reward_miles": miles,
        "min_spend_usd": parse_min_spend(terms) if rtype == "flat" else None,
        "url": url,
        "terms": (terms or "").strip(),
        "reward_text_raw": (reward_text or "").strip(),
    }


_IS_REWARD_RE = re.compile(r"(\d[\d,]*\s*miles?\b)|(\d+(?:\.\d+)?\s*[xX]\b)|(\d+(?:\.\d+)?\s*%)",
                           re.IGNORECASE)


def _is_reward_line(s: str) -> bool:
    s = (s or "").strip()
    return len(s) < 40 and bool(_IS_REWARD_RE.search(s))


def parse_modal_tiers(text: str) -> dict:
    """Parse an offer modal's text into headline + tier menu + exclusions.

    Modal text looks like:
        Shop Online
        Up to 11,200 miles
        Add A Line/New Line Of Service
        11,200 miles
        Prepaid
        7,800 miles
        Accessories
        7X miles
        Review exclusions: ...

    Returns {"headline", "tiers":[{label, reward_type, reward_miles, reward_text}],
             "exclusions"}. A simple offer with no menu yields tiers=[].
    """
    if not text:
        return {"headline": "", "tiers": [], "exclusions": ""}
    excl = ""
    if "Review exclusions" in text:
        after = text.split("Review exclusions", 1)[1]
        excl = after.split("Click the button")[0].lstrip(": ").strip()
    pre = text.split("Review exclusions")[0]
    lines = [l.strip() for l in pre.split("\n")
             if l.strip() and l.strip().lower() != "shop online"]
    headline = lines[0] if lines else ""
    tiers, i = [], 1
    while i < len(lines):
        label = lines[i]
        if _is_reward_line(label):          # stray reward w/o a label -> skip
            i += 1
            continue
        if i + 1 < len(lines) and _is_reward_line(lines[i + 1]):
            rt, mi = classify_reward(lines[i + 1], lines[i + 1])
            tiers.append({"label": label, "reward_type": rt,
                          "reward_miles": mi, "reward_text": lines[i + 1]})
            i += 2
        else:
            i += 1
    return {"headline": headline, "tiers": tiers, "exclusions": excl}


def parse_feed_item(item: dict) -> dict | None:
    """Turn one Capital One feed-API item into an offer record.

    The feed API (captured from network) is the reliable source: it carries the
    merchant name, domain, and reward headline directly, no DOM guessing.
    Fields: merchantDisplayName, merchantTLD, text ("Online"), buttonText
    (the reward, e.g. "10,500 miles" / "Up to 14X miles" / "Shop Now").
    Returns None for non-offer tiles (banners, "Shop Now" heroes).
    """
    name = (item.get("merchantDisplayName") or "").strip()
    tld = (item.get("merchantTLD") or "").strip()
    button = (item.get("buttonText") or "").strip()
    text = (item.get("text") or "").strip()
    if not name or not button:
        return None
    rtype, miles = classify_reward(button, text)
    # tiles with no parseable reward (hero banners: "Shop Now", "Explore ...")
    if rtype == "flat" and miles <= 0:
        return None
    return {
        "id": item.get("id", ""),
        "merchant": name,
        "domain": tld,
        "reward_type": rtype,
        "reward_miles": miles,
        "min_spend_usd": None,           # not in feed; from detail if needed
        "url": f"https://{tld}" if tld else "",
        "terms": text,
        "reward_text_raw": button,
    }


def parse_detail_categories(offer_detail: dict) -> list[dict]:
    """From an offer's detail JSON (offer.affiliate.categories), return the tier
    menu as [{name, reward_type, reward_miles, display}]. Empty if no tiers.

    This is how flat sub-tiers hidden inside an 'Up to X' headline surface, e.g.
    AT&T -> [{'Prepaid', flat, 7800}, {'Accessories', multiplier, 7}].
    """
    aff = (offer_detail or {}).get("affiliate") or {}
    cats = aff.get("categories") or []
    out = []
    for c in cats:
        disp = (c.get("displayAmount") or "").strip()
        rt, mi = classify_reward(disp, disp)
        out.append({"name": (c.get("name") or "").strip(),
                    "reward_type": rt, "reward_miles": mi, "display": disp})
    return out


def detail_terms_text(offer_detail: dict) -> str:
    """Flatten an offer's terms/exclusions into one searchable string."""
    d = offer_detail or {}
    parts = []
    aff = d.get("affiliate") or {}
    for co in aff.get("callouts") or []:
        t = co.get("markdownText")
        if t:
            parts.append(t)
    for tc in d.get("termsAndConditions") or []:
        for s in tc.get("markdownTextSections") or []:
            parts.append(s)
    return "\n".join(parts).strip()


# --- tiny self-test: `python3 parse.py` ---
if __name__ == "__main__":
    cases = [
        ("Pinter", "7,000 miles", "One offer per account.", "flat", 7000, None),
        ("Sam's Club", "Up to 14X miles", "", "multiplier", 14, None),
        ("CVS", "3X miles", "", "multiplier", 3, None),
        ("Kendra Scott", "3,000 miles", "Spend $50 or more.", "flat", 3000, 50),
        ("Farmer's Dog", "5,000 miles", "minimum purchase of $60", "flat", 5000, 60),
        ("Shop", "5% back", "", "multiplier", 5, None),
        ("Store", "2,500 miles", "$25+ required", "flat", 2500, 25),
        # the critical cases from the real scrape: "up to" is a ceiling, not flat
        ("Online", "Up to 15,000 miles", "", "capped", 15000, None),
        ("Online", "Up to 3,700 miles", "", "capped", 3700, None),
        ("Online", "6,200 miles", "", "flat", 6200, None),
        # real portal shape: reward snippet lacks "up to" but the card text has it
        ("Online", "15,000 miles", "Online\nUp to 15,000 miles", "capped", 15000, None),
    ]
    ok = True
    for merch, rw, terms, xt, xm, xs in cases:
        o = parse_offer(merch, rw, terms)
        got = (o["reward_type"], o["reward_miles"], o["min_spend_usd"])
        exp = (xt, xm, xs)
        status = "ok " if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(f"[{status}] {merch:14} {rw:16} -> {got}  (want {exp})")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    raise SystemExit(0 if ok else 1)
