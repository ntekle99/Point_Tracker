# Point Tracker

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-headless%20Chromium-2EAD33?logo=playwright&logoColor=white)
![Kafka](https://img.shields.io/badge/Apache%20Kafka-streaming-231F20?logo=apachekafka&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-blue)

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
├── points                      # single CLI entry point (login/scan/hunt/stream/watch/run)
├── requirements.txt
├── docker-compose.yml          # single-node Kafka (KRaft) for streaming mode
├── config/
│   ├── config.yaml             # thresholds, mile valuation, your hourly rate
│   └── hunt_urls.json          # per-merchant price-page overrides
├── src/                        # the pipeline
│   ├── scrape.py               #   feed-API scraper (auth, pagination, tier detail, cache)
│   ├── parse.py                #   reward/tier classification (flat / multiplier / capped)
│   ├── pricecheck.py           #   headless-browser price + product-link extractor
│   ├── judge.py                #   LLM: cheapest qualifying purchase + plausibility + effort
│   ├── score.py                #   ranking (net pts/min), buckets, report rendering
│   ├── autohunt.py             #   orchestrator (parallel render → judge → score)
│   └── stream_*.py             #   Kafka: producer, consumers, watcher, alerter, bus
├── scripts/
│   ├── clean_auto.py           # maintenance: drop auto finds to re-validate
│   └── vm_selfcheck.py         # deployment self-check (ntfy + kafka)
├── deploy/
│   ├── points-tracker.plist    # macOS launch agent (daily run)
│   └── points-watch.service    # systemd unit (24/7 watcher on a Linux VM)
└── docs/
    └── RUN.md                  # per-run playbook
```

## Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium
echo "MODEL_API_KEY=your-llm-api-key" > .env   # any OpenAI-compatible endpoint

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

**Two-stage pipeline (chained queues)** — separates *finding flat, single-purchase
offers* from *pricing* them, so each scales and fails independently:

```bash
./points stream2 --classifiers 2 --pricers 2
```

```
 feed ▶ points.offers ─▶ [classifiers] ─▶ points.candidates ─▶ [pricers] ─▶ points.rated ─▶ collector
                          flat-only +                          plain-site render →
                          single-purchase gate                 cheapest item + deep link + rating
```

- **Classifiers** keep only offers with a *flat* reward that can be earned by a
  **single one-time purchase** — deciding from *how the reward is earned* (the tier's
  category + terms), never the merchant's name (a store called "…Insurance" with an
  *Any purchase* reward stays in). A fast heuristic drops obvious commitments; an
  LLM confirms the rest.
- **Candidates are keyed by domain**, so every candidate for a merchant lands on one
  partition → one pricer → same-merchant renders never run concurrently (per-domain
  rate-limit safety on top of the process-wide pacing in `pricecheck`).
- **Pricers** visit the merchant's *normal* site (decoupled — the feed carries no
  affiliate link), sort the catalog cheapest-first, and pick the cheapest qualifying
  item. The report ends with a **🔗 Deep links** section (exact product URLs, click-
  verified for the top picks).

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

### Deploy 24/7 (Linux VM)

Run the watcher continuously on a small always-on box so alerts reach your phone
even with your laptop closed:

```bash
git clone https://github.com/ntekle99/Point_Tracker.git && cd Point_Tracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium && sudo .venv/bin/playwright install-deps chromium
docker compose up -d                        # Kafka (restarts on reboot)
printf 'MODEL_API_KEY=...\nNTFY_TOPIC=...\n' > .env
# install the watcher as a systemd service (starts on boot, auto-restarts):
sudo cp deploy/points-watch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now points-watch
```

`journalctl -u points-watch -f` to follow it. The Capital One session is refreshed
periodically from a trusted machine (`./points login` → copy `pw_profile/`), since
a bank login can't be fully automated.
