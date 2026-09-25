#!/usr/bin/env python3
"""
Per-offer decision graph (LangGraph).

Kafka handles streaming/distribution — fanning offers out to workers and keying by
domain. This module is the *reasoning* each worker runs on a single offer: a small
LangGraph StateGraph that walks one deal through the decision flow and returns a
verdict + accept/reject, with a shared state object flowing between nodes.

    gate ─(eligible?)─▶ price ─▶ judge ─▶ decide ─▶ END
      └────(no)────────────────────────────────────▶ END

  • gate   — flat + single one-time purchase? (purchase_gate; skips subs/commitments)
  • price  — render the merchant site, find prices + deep links (pricecheck, paced)
  • judge  — LLM picks the cheapest QUALIFYING item + plausibility/effort (judge)
  • decide — apply the acceptance guardrail (price>0, qualifies, plausible, confident)

Used by stream_pricer; run_deal() invokes the compiled graph on one offer.
"""
from __future__ import annotations

from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

import judge as J
import pricecheck
import purchase_gate as G


class DealState(TypedDict, total=False):
    offer: dict            # raw offer (merchant, domain, terms, …)
    opp: dict              # one flat earning path (merchant, domain, category, reward_miles)
    url: str               # merchant page to price (plain site, not affiliate link)
    eligible: bool
    gate_reason: str
    rendered: dict         # pricecheck.find_prices output
    verdict: dict          # judge output
    decision: str          # "accept" | "reject"


def _gate(state: DealState) -> dict:
    g = G.purchase_gate(state["opp"], state["offer"])
    return {"eligible": g["eligible"], "gate_reason": g["reason"]}


def _price(state: DealState) -> dict:
    return {"rendered": pricecheck.find_prices(state["url"], sort_cheapest=True)}


def _judge(state: DealState) -> dict:
    rendered = state.get("rendered") or {}
    if rendered.get("blocked"):
        return {"verdict": J._fail(f"blocked/throttled: {rendered.get('note', '')}")}
    return {"verdict": J.judge(state["opp"], rendered)}


def _decide(state: DealState) -> dict:
    v = state.get("verdict") or {}
    price = v.get("price_usd")
    ok = (price and float(price) > 0 and v.get("likely_qualifies")
          and v.get("plausible") and v.get("confidence") in ("medium", "high"))
    return {"decision": "accept" if ok else "reject"}


def _after_gate(state: DealState) -> str:
    return "price" if state.get("eligible") else "end"


def build_graph():
    g = StateGraph(DealState)
    g.add_node("gate", _gate)
    g.add_node("price", _price)
    g.add_node("judge", _judge)
    g.add_node("decide", _decide)
    g.set_entry_point("gate")
    g.add_conditional_edges("gate", _after_gate, {"price": "price", "end": END})
    g.add_edge("price", "judge")
    g.add_edge("judge", "decide")
    g.add_edge("decide", END)
    return g.compile()


# compiled once; safe to reuse across invocations
GRAPH = build_graph()


def run_deal(offer: dict, opp: dict, url: str) -> DealState:
    """Run one offer through the decision graph. Returns the final state
    (verdict + decision, or an early exit if the gate rejects it)."""
    return GRAPH.invoke({"offer": offer, "opp": opp, "url": url})


if __name__ == "__main__":
    import json
    # smoke test with mocked price/judge so it runs offline
    pricecheck.find_prices = lambda *a, **k: {"blocked": False, "prices": [
        {"amount": 5.97, "context": "ShieldView", "url": "https://x/p"}]}
    J.judge = lambda opp, r: {"price_usd": 5.97, "item": "ShieldView", "product_url": "https://x/p",
                              "plausible": True, "likely_qualifies": True, "confidence": "high",
                              "purchase_type": "one_time_good", "note": "ok"}
    out = run_deal({"merchant": "Verizon", "terms": "Any purchase"},
                   {"merchant": "Verizon", "domain": "verizon.com",
                    "category": "Any purchase", "reward_miles": 10500},
                   "https://verizon.com")
    print(json.dumps({k: out.get(k) for k in ("eligible", "decision", "verdict")}, indent=2))
