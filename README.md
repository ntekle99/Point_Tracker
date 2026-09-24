# Point Tracker

**An autonomous agent that finds arbitrage in Capital One credit-card reward offers** — where a flat miles bonus is worth more than the cheapest qualifying purchase — and ranks the finds by return on your time.

Some Capital One Offers pay a **flat** miles bonus (e.g. *"7,000 miles for shopping at X"*) regardless of how much you spend. If a merchant sells a cheap in-stock item, the miles can be worth far more than the item costs. This tool hunts those out of **1,000+ daily offers**, prices each merchant live, and tells you exactly what to buy.

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

## Streaming mode (Kafka)

Two Kafka-backed pipelines decouple the stages so rendering, LLM analysis, and
alerting scale and fail independently. Start a broker first:

```bash
docker compose up -d          # single-node Kafka (KRaft), localhost:9092
```

**Decoupled pricing** — producer renders pages in parallel and publishes each to
`points.pages`; a consumer group of LLM judges reads them and emits verdicts to
`points.verdicts`; a collector applies the guardrails and writes the report:

```bash
./points stream --consumers 4 --render-workers 6
```

**Continuous + real-time alerts** — a watcher keeps one logged-in session open,
re-polls the feed every N minutes, and publishes only *newly-appeared* offers to
`points.new_offers`; an alerter consumer fires an instant macOS notification the
moment a new flat deal shows up:

```bash
./points watch --interval 30 --alerters 1
```

```
                       ┌── judge consumers ──▶ points.verdicts ──▶ collector ──▶ report
 producer ▶ points.pages
                       
 watcher  ▶ points.new_offers ──▶ alerter consumer ──▶ 🔔 notification
```

Streaming source files live in `src/stream_*.py`; topic config in `src/stream_bus.py`.

### Phone alerts (ntfy)

By default the alerter fires a **macOS notification** (local dev). For an
always-on deployment (e.g. a cloud VM), get the alert on your **phone** via
[ntfy.sh](https://ntfy.sh) — free, no account:

1. Install the **ntfy** app (iOS/Android) and subscribe to a unique topic, e.g.
   `points-<yourname>-<random>`.
2. Add it to your `.env`:
   ```bash
   echo "NTFY_TOPIC=points-<yourname>-<random>" >> .env
   ```

Now every new flat deal pushes to your phone (`🎯 New Capital One flat offer —
Pinter · 10,500 miles`), tappable straight to the offers feed. On a Linux VM the
macOS notification is skipped automatically and the phone push carries it.
