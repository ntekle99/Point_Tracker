# Point Tracker

**An autonomous agent that finds arbitrage in Capital One credit-card reward offers** — where a flat miles bonus is worth more than the cheapest qualifying purchase — and ranks the finds by return on your time.

Some Capital One Offers pay a **flat** miles bonus (e.g. *"7,000 miles for shopping at X"*) regardless of how much you spend. If a merchant sells a cheap in-stock item, the miles can be worth far more than the item costs. This tool hunts those out of **1,000+ daily offers**, prices each merchant live, and tells you exactly what to buy.

> Built for the Capital One **Venture X**. Personal project; see [Disclaimer](#disclaimer).

---

## What it does

```
scrape ─▶ expand tiers ─▶ render each merchant ─▶ LLM judge ─▶ score ─▶ report
```

1. **Scrape** every offer from Capital One's feed API (authenticated, paginated, rate-limited, cached).
2. **Expand tiers** — a *"Up to 11,200 miles"* offer is really a menu; flat sub-tiers (e.g. *"Prepaid → 7,800 miles"*) are where the cheap plays hide.
3. **Render** each candidate merchant's live site with a headless browser (many sites render prices in JavaScript that static scrapers can't see).
4. **Judge** — an LLM reads the rendered prices and picks the *cheapest qualifying* purchase, classifies the friction (one-time buy / cancelable sub / service commitment), estimates effort, and returns a plausibility check.
5. **Score** — ranks every deal by **net points per minute of effort** against a configurable hourly-rate bar, separates verified deals from LLM guesses, and quarantines implausible reads.
6. **Report** — a ranked markdown report + macOS notification, with a 🏆 top pick, exact product links, and a ⚠️ "catch" column (new-customer rules, hold periods, "needs a spare phone", etc.).

Runs itself daily via a macOS launch agent.

## Highlights

- **Reliable data via the private feed API** — reverse-engineered pagination + per-offer detail endpoints instead of brittle DOM scraping.
- **JS-rendering price finder** — a headless-browser step reads prices that `requests`/WebFetch can't.
- **LLM-in-the-loop with guardrails** — plausibility gating, a confidence floor, a deterministic max-ratio backstop, category-to-product matching, and a hand-verified-vs-auto split. (The judge is genuinely useful *and* genuinely fallible; the tool is designed around that.)
- **Return-on-time ranking** — deals scored in *net points per minute*, not just raw ratio, against your hourly rate.
- **Parallel** — merchant pricing runs across a process pool.
- **Anti-ban** — domain-keyed cache, pacing, hard caps, and 429 back-off.

## Tech stack

Python · [Playwright](https://playwright.dev) (headless Chromium) · OpenAI-compatible LLM API (runs on any endpoint — configured here for an internal inference hub) · macOS `launchd`.

---

## Project structure

```
.
├── points                 # single CLI entry point
├── config.yaml            # thresholds, mile valuation, time value
├── hunt_urls.json         # per-merchant price-page overrides
├── requirements.txt
├── src/                   # the pipeline
│   ├── scrape.py          #   feed-API scraper (auth, pagination, tier detail, cache)
│   ├── parse.py           #   reward/tier classification (flat vs multiplier vs capped)
│   ├── pricecheck.py      #   headless-browser price + product-link extractor
│   ├── judge.py           #   LLM: cheapest qualifying purchase + plausibility + effort
│   ├── score.py           #   ranking, pts/min, buckets, report rendering
│   └── autohunt.py        #   orchestrator (parallel render → judge → score)
├── scripts/
│   └── clean_auto.py      # maintenance: drop auto finds to re-validate
├── deploy/
│   └── points-tracker.plist   # macOS launch agent (daily run)
└── docs/
    └── RUN.md             # per-run playbook
```

## Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium
echo "NVIDIA_API_KEY=your-llm-api-key" > .env   # any OpenAI-compatible endpoint

# ./points is the CLI — one command, several subcommands:
./points login                # one-time: sign into Capital One (session saved locally)
./points scan                 # scrape offers + score (fast, no pricing)
./points hunt --workers 6     # price opportunities (render + LLM)
./points run                  # full pipeline: scrape → hunt → report + notify
./points report               # print the latest report
```

Schedule the daily run by editing the paths in `deploy/points-tracker.plist`, then:
```bash
cp deploy/points-tracker.plist ~/Library/LaunchAgents/ && \
launchctl load ~/Library/LaunchAgents/points-tracker.plist
```

## How ranking works

For each flat opportunity the tool computes:

```
net_points   = miles_earned − (item_cost ÷ mile_value)
pts_per_min  = net_points ÷ estimated_effort_minutes
```

and flags a deal as worth-it when `pts_per_min` clears your bar (default **200/min ≈ $120/hr** in miles value, tunable in `config.yaml`). Deals are split into **clean** (buy & keep / cancelable), **service commitments** (fiber/TV/contract), and a **verify** bucket for implausibly-high auto reads.

## Honest limitations

This is an autonomous scrape + LLM pipeline, so it's a **lead generator, not an oracle**:

- Offers rotate constantly — data is only as fresh as the last scrape.
- The LLM occasionally misreads a price (a page fragment, a per-unit rate) — mitigated by guardrails, not eliminated. **Hand-verified (`✓`) deals are the trustworthy core; auto (`🤖`) deals are leads to sanity-check in the live portal.**
- Product deep-links depend on the merchant's page structure.

## Disclaimer

A personal finance-optimization project. Reward programs can **claw back** offers they consider gamed — that's the user's risk. No credentials are stored in the repo; the tool drives your own logged-in browser session (git-ignored). Not affiliated with Capital One.
