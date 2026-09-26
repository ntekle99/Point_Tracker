# Point Tracker

A real-time agent that catches arbitrage in Capital One reward offers the hour it lands, where a flat miles bonus is worth more than the cheapest qualifying purchase, and pushes it to your phone before it's gone.

Some Capital One Offers pay a flat miles bonus (for example, "7,000 miles for shopping at X") no matter how much you spend. If a merchant sells a cheap in-stock item, the miles can be worth far more than the item costs. The best flat offers also get devalued and pulled within hours, so a 10,500-mile deal is worth catching the hour it appears rather than in a daily digest. The primary mode is a continuous streaming watcher that diffs the feed every few minutes and pushes the moment a new flat, single-purchase deal shows up. A one-shot batch mode is there for on-demand sweeps.

---

## What it does (real-time)

```
watch feed  ─▶  diff for NEW offers  ─▶  flat + single-purchase gate  ─▶  price it  ─▶  🔔 instant push
   (Kafka points.new_offers ─────────────────▶ alerter consumer ─────────────────────────────▶ your phone)
```

1. Watch: one logged-in session stays open and re-polls the feed API every N minutes (paced and gentle), diffing against everything already seen.
2. Detect new: only newly-appeared offers are published to Kafka (`points.new_offers`); the rest are ignored, so you're alerted once and fast.
3. Gate: the alerter keeps only offers earnable by a single one-time purchase with a flat reward above your miles bar. Multipliers, spend thresholds, and subscriptions or service commitments are dropped.
4. Price inline: it renders the merchant's live site, finds the cheapest qualifying item and a deep link, and computes the ratio.
5. Push: an instant phone (ntfy) and desktop alert, like `💰 Pinter — buy $12 → 10,500 mi (8.7x)`, tappable straight to the product. Offers get devalued as they get popular, so it's built to move fast.

The streaming stages run on Kafka so detection, pricing, and alerting scale and fail independently. It runs 24/7 as a service and self-heals via [`./doctor`](#operations--self-healing-doctor).

### On-demand batch (secondary)

For a one-off sweep instead of a live watch, the batch pipeline (`scrape ─▶ expand tiers ─▶ render each merchant ─▶ LLM judge ─▶ score ─▶ report`) ranks every current flat opportunity by net points per minute, with a top pick, exact product links, and a "catch" column (new-customer rules, hold periods, needing a spare phone). See [On-demand batch mode](#on-demand-batch-mode).

## Highlights

- Real-time streaming detection: a Kafka-backed watcher diffs the feed continuously and pushes a priced deal to your phone the moment it lands, since the best flat offers get devalued and pulled within hours.
- Self-healing 24/7 deploy: a self-refreshing browser session lasts for days, one command (`./doctor`) diagnoses and restarts it, and it pushes a "session expired" alert instead of failing silently.
- Reliable data from the private feed API: reverse-engineered pagination and per-offer detail endpoints instead of brittle DOM scraping.
- JS-rendering price finder: a headless-browser step reads prices that `requests`/WebFetch can't.
- LLM in the loop with guardrails: plausibility gating, a confidence floor, a deterministic max-ratio backstop, category-to-product matching, and a hand-verified-vs-auto split. The judge is useful but fallible, and the tool is designed around that.
- Return-on-time ranking: deals scored in net points per minute rather than raw ratio, against your hourly rate.
- Parallel: merchant pricing runs across a process pool.
- Anti-ban: domain-keyed cache, pacing, hard caps, and 429 back-off.

## Tech stack

Python · [Apache Kafka](https://kafka.apache.org) (KRaft, single-node via Docker) for the streaming pipeline · [Playwright](https://playwright.dev) (headless Chromium) · OpenAI-compatible LLM API (runs on any endpoint, configured here for an internal inference hub) · [ntfy](https://ntfy.sh) push · `systemd` / macOS `launchd`.

---

## Project structure

```
.
├── points                      # single CLI entry point (watch/stream2/stream · scan/hunt/run)
├── doctor                      # health check + restart for the 24/7 watcher (agent-friendly)
├── RUNBOOK.md                  # operations guide (failure modes -> fix)
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
│   ├── autohunt.py             #   orchestrator (parallel render -> judge -> score)
│   ├── stream_watch.py         #   continuous watcher, publishes NEW offers (real-time)
│   ├── stream_alerter.py       #   prices new flat deals + instant phone/desktop push
│   └── stream_*.py             #   rest of the Kafka pipeline: producer, classifier, pricer, bus
├── scripts/
│   ├── clean_auto.py           # maintenance: drop auto finds to re-validate
│   └── vm_selfcheck.py         # deployment self-check (ntfy + kafka)
├── deploy/
│   ├── points-watch.service    # systemd unit, 24/7 watcher on a Linux VM
│   ├── points-watch.mac.plist  # launch agent, 24/7 watcher on the Mac (residential IP)
│   ├── mac-watch.sh            #   wrapper the Mac launch agent runs (broker + watcher)
│   └── points-tracker.plist    # launch agent, on-demand daily batch run
└── docs/
    └── RUN.md                  # batch-mode playbook
```

## Quick start: real-time watch (primary)

```bash
pip install -r requirements.txt
python -m playwright install chromium
printf 'MODEL_API_KEY=your-llm-api-key\nNTFY_TOPIC=points-you-random\n' > .env

./points login                # one-time: sign into Capital One (session saved)
docker compose up -d          # single-node Kafka (KRaft), localhost:9092
./points watch --interval 30 --alerters 1   # live watch -> instant push on new flat deals
```

`watch` polls the feed, and the moment a new flat single-purchase deal appears you get a
phone and desktop push. Leave it running, or [deploy it 24/7](#deploy-247). Add `NTFY_TOPIC`
for phone alerts; see [Phone alerts](#phone-alerts-ntfy).

## On-demand batch mode

For a one-off sweep of everything live right now instead of a continuous watch:

```bash
./points scan                 # scrape offers + score (fast, no pricing)
./points hunt --workers 6     # price opportunities (render + LLM)
./points run                  # full one-shot pipeline: scrape -> hunt -> report + notify
./points report               # print the latest report
```

Schedule a daily batch by editing the paths in `deploy/points-tracker.plist`, then:
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

and flags a deal as worth-it when `pts_per_min` clears your bar (default 200/min ≈ $120/hr in miles value, tunable in `config.yaml`). Deals are split into clean (buy and keep, or cancelable), service commitments (fiber/TV/contract), and a verify bucket for implausibly-high auto reads.

## Streaming architecture (Kafka)

Everything runs on Kafka so detection, pricing, and alerting scale and fail
independently. Start a broker first:

```bash
docker compose up -d          # single-node Kafka (KRaft), localhost:9092
```

Continuous watch and real-time alerts is the primary mode. A watcher keeps one
logged-in session open, re-polls the feed every N minutes, and publishes only
newly-appeared offers to `points.new_offers`. An alerter consumer prices each one and
fires an instant phone and desktop push the moment a new flat single-purchase deal lands:

```bash
./points watch --interval 30 --alerters 1
```

```
 watcher ▶ points.new_offers ──▶ alerter consumer ──▶ price inline ──▶ 🔔 phone + desktop push
```

The watcher prefers a self-refreshing live browser profile: it re-navigates the feed
each cycle and writes rotated cookies back, so an authenticated session lasts for days.
When the bank eventually forces a re-login, it pushes a one-time "session expired" alert
instead of failing silently.

Decoupled pricing (batch, parallel): a producer renders pages in parallel and publishes
each to `points.pages`; a consumer group of LLM judges reads them and emits verdicts to
`points.verdicts`; a collector applies the guardrails and writes the report:

```bash
./points stream --consumers 4 --render-workers 6
```

```
 producer ▶ points.pages ──▶ judge consumers ──▶ points.verdicts ──▶ collector ──▶ report
```

Two-stage pipeline (chained queues): separates finding flat, single-purchase offers
from pricing them, so each scales and fails independently:

```bash
./points stream2 --classifiers 2 --pricers 2
```

```
 feed ▶ points.offers ─▶ [classifiers] ─▶ points.candidates ─▶ [pricers] ─▶ points.rated ─▶ collector
                          flat-only +                          plain-site render →
                          single-purchase gate                 cheapest item + deep link + rating
```

- Classifiers keep only offers with a flat reward earnable by a single one-time
  purchase. The call is based on how the reward is earned (the tier's category and
  terms), not the merchant's name, so a store called "…Insurance" with an Any purchase
  reward stays in. A fast heuristic drops obvious commitments and an LLM confirms the rest.
- Candidates are keyed by domain, so every candidate for a merchant lands on one
  partition and one pricer, and same-merchant renders never run concurrently. That is
  per-domain rate-limit safety on top of the process-wide pacing in `pricecheck`.
- Pricers visit the merchant's normal site (decoupled, since the feed carries no
  affiliate link), sort the catalog cheapest-first, and pick the cheapest qualifying
  item. The report ends with a Deep links section of exact product URLs, click-verified
  for the top picks.

Streaming source files live in `src/stream_*.py`; topic config in `src/stream_bus.py`.

### Phone alerts (ntfy)

By default the alerter fires a macOS notification (local dev). For an always-on
deployment such as a cloud VM, get the alert on your phone via [ntfy.sh](https://ntfy.sh),
which is free and needs no account:

1. Install the ntfy app (iOS/Android) and subscribe to a unique topic, e.g.
   `points-<yourname>-<random>`.
2. Add it to your `.env`:
   ```bash
   echo "NTFY_TOPIC=points-<yourname>-<random>" >> .env
   ```

Now every new flat deal pushes to your phone (`🎯 New Capital One flat offer —
Pinter · 10,500 miles`), tappable straight to the offers feed. On a Linux VM the
macOS notification is skipped automatically and the phone push carries it.

### Deploy 24/7

Run the watcher continuously on a small always-on box so alerts reach your phone
even with your laptop closed.

Run each line separately (avoid pasting comment lines into an interactive shell, since
zsh treats `#` as a command, not a comment):

```bash
git clone https://github.com/ntekle99/Point_Tracker.git && cd Point_Tracker
```
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
```bash
.venv/bin/playwright install chromium && sudo .venv/bin/playwright install-deps chromium
```
```bash
docker compose up -d
```
Create `.env` with your real values (this appends, so it won't clobber an existing key):
```bash
printf 'MODEL_API_KEY=YOUR_KEY\nNTFY_TOPIC=points-you-random\n' >> .env
```
Install the watcher as a systemd service (starts on boot, auto-restarts):
```bash
sudo cp deploy/points-watch.service /etc/systemd/system/
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now points-watch
```

`journalctl -u points-watch -f` to follow it (or `tail -f data/watch.log`).

> Datacenter-IP note: a cloud VM's IP has low reputation with the portal's bot
> protection (Cloudflare), so the session can be challenged more aggressively there.
> A residential-IP machine such as your own Mac is the most reliable host: the same
> watcher runs via a launch agent (`deploy/points-watch.mac.plist`), with login and
> watcher on one box so there's no session copying at all.

Refreshing the session (needed when the bank forces a re-login, which sends you a
push): sign-in needs a real screen, so it can't happen on a headless VM. Re-login on a
machine with a display and copy the portable session across. A copied browser profile
can't decrypt cookies cross-OS, so copy `pw_state.json`, not `pw_profile/`:

```bash
./points login                         # on a machine with a screen
scp pw_state.json  user@vm:/path/Point_Tracker/pw_state.json
```
Then restart the watcher on the box (`./doctor restart`).

## Operations & self-healing (doctor)

For an always-on deployment, `./doctor` is a single self-diagnosing entry point, built
so a phone-driven agent (or you) can keep it healthy without remembering the internals:

```bash
./doctor            # health check -> one VERDICT + RECOMMENDED ACTION
./doctor restart    # restart the service, then re-diagnose
./doctor heal       # restart + diagnose + print the fix if it can't self-heal
./doctor logs       # live log stream
```

It checks the service, the Kafka broker, ntfy config, poll freshness, and session
validity, then prints a clear verdict. It separates what the box can fix on its own (a
down service or broker, or a stalled poll, which it restarts) from the one thing it
can't: an expired login, which it flags with the exact re-login steps instead of
crash-looping. See [RUNBOOK.md](RUNBOOK.md) for the full operations guide.
