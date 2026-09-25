#!/usr/bin/env python3
"""
Single-purchase gate — decides whether an offer's reward can be earned with a
one-time purchase, WITHOUT visiting the merchant site.

CRUCIAL DESIGN RULE (per user): the decision keys off HOW THE REWARD IS EARNED —
the flat tier's category plus the offer's terms — NOT the merchant's name or
domain. A merchant literally named "Jack's Insurance" with an *Any purchase*
reward stays in, because you can buy a cheap non-insurance item there. It is only
dropped when EARNING THE REWARD ITSELF requires a subscription or a service
commitment (fiber install, a new phone line, a 2-year agreement, a monthly
membership, etc.).

This is a cheap heuristic pre-filter. Anything it keeps but isn't sure about is
flagged `ambiguous=True` so the caller can confirm with a metadata-only LLM call
(judge.classify_purchase_type). Anything it drops is a confident drop.
"""
from __future__ import annotations

import re

# Generic categories that ANY cheap one-time item satisfies -> always eligible,
# regardless of what the merchant is called or otherwise sells.
_GENERIC_CATEGORIES = {"", "any purchase", "anything", "all purchases",
                       "any order", "sitewide", "site wide", "storewide"}

# Markers in the REWARD-REQUIREMENT text (category + terms) that mean earning the
# reward needs an ongoing subscription or a service/contract commitment. Word-
# boundary matched so "planter" doesn't trip "plan", etc.
_COMMIT_MARKERS = [
    r"fiber", r"broadband", r"internet plan", r"\binternet\b", r"\bfios\b",
    r"\btv\b", r"satellite", r"postpaid", r"new line", r"add a line",
    r"add-a-line", r"activation", r"activate a", r"2-?year", r"two-?year",
    r"annual (?:agreement|contract|commitment)", r"contract", r"installation",
    r"install(?:ed)?\b", r"credit check", r"\bsolar\b", r"monitoring",
    r"home security", r"subscription", r"subscribe", r"membership",
    r"per month", r"/mo\b", r"\bmonthly\b", r"\bper line\b", r"lease",
    r"financing", r"enroll", r"policy",  # 'policy' = insurance policy commitment
]
_COMMIT_RE = re.compile("|".join(_COMMIT_MARKERS), re.I)


def _requirement_text(opp: dict, offer: dict) -> str:
    """The text that describes what you must buy to EARN the reward — category +
    the offer's terms/detail terms. Deliberately excludes merchant/domain."""
    parts = [
        opp.get("category", ""),
        offer.get("terms", "") or "",
        offer.get("detail_terms", "") or "",
        offer.get("reward_text_raw", "") or "",
    ]
    return " ".join(p for p in parts if p)


def purchase_gate(opp: dict, offer: dict) -> dict:
    """Return {eligible, ambiguous, reason}.

    eligible=False  -> confident drop (reward needs a subscription/commitment).
    eligible=True, ambiguous=False -> confident keep (generic 'any purchase').
    eligible=True, ambiguous=True  -> keep but confirm with an LLM (no marker hit,
                                       but the category is specific so we're unsure).
    """
    category = (opp.get("category", "") or "").strip().lower()
    if category in _GENERIC_CATEGORIES:
        return {"eligible": True, "ambiguous": False,
                "reason": "generic category ('any purchase') — any cheap item qualifies"}

    text = _requirement_text(opp, offer)
    m = _COMMIT_RE.search(text)
    if m:
        return {"eligible": False, "ambiguous": False,
                "reason": f"reward requires a commitment/subscription (matched '{m.group(0)}')"}

    # Specific category, no commitment marker — plausibly a single purchase, but
    # not certain (e.g. a niche tier). Keep and let the LLM confirm.
    return {"eligible": True, "ambiguous": True,
            "reason": f"specific category '{category}', no commitment marker — confirm"}


if __name__ == "__main__":
    import json
    tests = [
        ({"category": "Any purchase"}, {"merchant": "Jack's Insurance", "terms": ""}),
        ({"category": "Fiber Internet"}, {"merchant": "Verizon", "terms": "2-year agreement"}),
        ({"category": "Unlimited Bundles"}, {"merchant": "Disney+",
                                             "terms": "monthly subscription required"}),
        ({"category": "Screen Protectors"}, {"merchant": "Verizon", "terms": "in-store only"}),
    ]
    for opp, offer in tests:
        print(f"{opp['category']:22} @ {offer['merchant']:16} -> "
              f"{json.dumps(purchase_gate(opp, offer))}")
