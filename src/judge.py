#!/usr/bin/env python3
"""
The reasoning step: given an offer's tier/category and a merchant page's rendered
prices+context, ask an LLM to pick the single cheapest QUALIFYING purchase.

This is what a min() can't do — it distinguishes a $10/mo plan from a $10/1GB
overage fee, a $5 SIM from a $5/day roaming pass, etc.

Backend: any OpenAI-compatible chat endpoint (configurable). Reads:
  MODEL_API_KEY          (or OPENAI_API_KEY)   — the key; never stored/printed here
  POINTS_JUDGE_BASE_URL  default https://inference-api.nvidia.com/v1/
  POINTS_JUDGE_MODEL     default nvidia/meta/eccn-llama-3.3-70b-instruct

Returns:
  {item, price_usd (float|None), one_time_purchase (bool), likely_qualifies (bool),
   confidence ("low"|"medium"|"high"), note}
"""
from __future__ import annotations

import json
import os

from openai import OpenAI

BASE_URL = os.environ.get("POINTS_JUDGE_BASE_URL", "https://inference-api.nvidia.com/v1/")
MODEL = os.environ.get("POINTS_JUDGE_MODEL", "nvidia/meta/eccn-llama-3.3-70b-instruct")


def _client() -> OpenAI:
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("set MODEL_API_KEY (or OPENAI_API_KEY) in the environment")
    return OpenAI(base_url=BASE_URL, api_key=key)


_SYSTEM = (
    "You evaluate Capital One Shopping miles offers. Given a merchant offer and a "
    "list of prices scraped from that merchant's live site (each with the text "
    "around it), find the price of the purchase that EARNS THIS OFFER'S REWARD.\n"
    "CATEGORY MATCHING IS MANDATORY. The miles are earned ONLY by buying the "
    "product that matches the offer's category. If the category names a specific "
    "tier or product ('Unlimited Bundles', 'Prepaid', 'Internet 2 Gig', "
    "'Add A Line'), you MUST price THAT specific product — never substitute a "
    "cheaper, different product. Example: a Disney+ 'Unlimited Bundles' tier is "
    "earned by the expensive Disney+/Hulu/ESPN Unlimited bundle (~$26+), NOT the "
    "cheap $8 basic plan (which earns a different, smaller tier). If you cannot "
    "find the price of the product that actually matches the category, return "
    "price_usd=null and likely_qualifies=false — do NOT pair this reward with an "
    "unrelated cheaper price. Only when the category is 'Any purchase' may you "
    "pick the cheapest item on the site.\n"
    "Once you've identified the matching product, find its SINGLE CHEAPEST real "
    "price. Ignore prices that are NOT real "
    "qualifying purchases: overage/usage fees (e.g. '$10/1GB'), regulatory/recovery "
    "charges, per-day roaming add-ons, warranty/insurance add-ons, accessory-line "
    "add-ons, device financing, and taxes. Prefer a genuine one-time purchase or "
    "the cheapest entry-level plan's first-month cost. If the page shows no real "
    "qualifying purchase, use price_usd=null.\n"
    "CRUCIAL — classify the friction of the purchase in 'purchase_type':\n"
    "  'one_time_good' = buy a physical product once and keep it, no ongoing "
    "commitment (a ~$5 SIM kit, a $20 replacement part, a book).\n"
    "  'subscription' = a monthly service you could pay ONE month then cancel or "
    "not renew (a prepaid phone month, an ~$8 streaming month).\n"
    "  'service_commitment' = needs installation, a contract, credit check, a new "
    "phone line, or a long commitment (home/business fiber, satellite TV, solar, "
    "insurance, a postpaid line). NOT a cheap easy play.\n"
    "  'none' = no qualifying purchase found.\n"
    "PLAUSIBILITY — sanity-check the price against what this merchant actually "
    "sells. A guided-tour operator's cheapest real tour is hundreds-to-thousands "
    "of dollars, NOT $29; a cable-internet provider does NOT sell $1 SIM kits; a "
    "furniture store's cheapest item is not $2. If the cheapest number you see "
    "looks like a page fragment, a filter/slider value, a '$X off' discount, a "
    "deposit, a per-GB or per-day rate, or is otherwise implausibly low for this "
    "merchant's real catalog, set plausible=false and price_usd=null. Only set "
    "plausible=true if you are confident the price is a real, buyable item/plan.\n"
    "EFFORT — estimate 'effort_minutes': realistic total minutes to complete THIS "
    "purchase end to end. A quick cart checkout of a physical item ≈ 5. A "
    "new-customer signup (create account, enter payment, activate) that you must "
    "later remember to cancel ≈ 12-20. Anything needing an install visit, "
    "contract, or credit check ≈ 60+.\n"
    "PRODUCT LINK — for the item you pick, set 'product_url' to the URL shown on "
    "that price's line, copied EXACTLY. If that line's URL is '(none)' or you "
    "can't tell, use an empty string. Never invent or guess a URL.\n"
    "Be conservative and honest; never invent a price not supported by the context. "
    "Reply with ONLY a JSON object of exactly this shape and nothing else: "
    '{"item": string, "price_usd": number or null, "product_url": string, '
    '"purchase_type": "one_time_good" or "subscription" or "service_commitment" or "none", '
    '"plausible": boolean, "effort_minutes": number, '
    '"likely_qualifies": boolean, "confidence": "low" or "medium" or "high", '
    '"note": string}'
)


def judge(offer: dict, rendered: dict) -> dict:
    """offer: {merchant, domain, category, reward_miles}. rendered: pricecheck
    find_prices() output {prices:[{amount, context}], ...}."""
    prices = rendered.get("prices") or []
    price_lines = "\n".join(
        f"- ${p['amount']:.2f}  |  {p['context']}  |  URL: {p.get('url','') or '(none)'}"
        for p in prices[:40]
    ) or "(no prices found on the page)"

    user = (
        f"OFFER\n"
        f"  merchant: {offer.get('merchant')}\n"
        f"  category to buy in: {offer.get('category')}\n"
        f"  reward: {int(offer.get('reward_miles', 0))} miles (flat lump sum)\n\n"
        f"PRICES SCRAPED FROM {offer.get('domain')} (amount | surrounding text):\n"
        f"{price_lines}\n\n"
        f"Pick the cheapest QUALIFYING purchase for this offer's category. JSON only."
    )

    try:
        resp = _client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.1,
            top_p=0.7,
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return _fail(f"api error: {str(e)[:140]}")

    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
    except Exception:
        return _fail(f"parse failed: {text[:120]}")

    data.setdefault("item", "")
    data.setdefault("price_usd", None)
    data.setdefault("product_url", "")
    data.setdefault("purchase_type", "none")
    data.setdefault("plausible", False)
    data.setdefault("effort_minutes", None)
    data.setdefault("likely_qualifies", False)
    data.setdefault("confidence", "low")
    data.setdefault("note", "")
    return data


def _fail(msg: str) -> dict:
    return {"item": "", "price_usd": None, "purchase_type": "none",
            "plausible": False, "effort_minutes": None,
            "likely_qualifies": False, "confidence": "low", "note": msg}


if __name__ == "__main__":
    demo_offer = {"merchant": "Boost Mobile", "domain": "boostmobile.com",
                  "category": "Any purchase", "reward_miles": 7200}
    demo_rendered = {"prices": [
        {"amount": 1299.99, "context": "iPhone 16 Pro $1,299.99"},
        {"amount": 10.0, "context": "$10/mo for 6 months, then $25 forever. New customers only."},
        {"amount": 2.20, "context": "Reg and Telco Recovery Charge $2.20"},
    ]}
    print(json.dumps(judge(demo_offer, demo_rendered), indent=2))
