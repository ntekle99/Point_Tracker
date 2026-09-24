# points_tracker

Finds Capital One Offers (Venture X) where a **fixed miles reward** beats the
cost of the cheapest qualifying purchase by at least **2x** — the "spend $8, get
7,000 miles" arbitrage.

## The one idea that makes this work
- **Flat lump-sum offers** ("7,000 miles for shopping at X") net positive when a
  cheap qualifying purchase exists. These are the target.
- **Multiplier offers** ("14X miles per $1") never do — you always spend more
  than the miles are worth. These are filtered out.

## Mile valuation (Venture X)
- `1.0¢`/mile — conservative floor (cash/statement credit)
- `~1.85¢`/mile — best case via transfer partners

Both are shown in the report. Tune in `config.yaml`.

## How to run (Playwright — self-contained, no extension)
Runs its own Chromium with a persistent login profile, so it sidesteps the
NVIDIA-managed-Chrome extension block. Credentials never touch this code.

**First time — log in once (saved to `pw_profile/`):**
```bash
./run.sh --login
```
A browser opens; sign into Capital One, reach your Offers feed, press Enter.

**Every day after — one command scrapes + scores + notifies:**
```bash
./run.sh
```
Writes `data/offers_<date>.json` (+ `raw_<date>.json`) and
`reports/report_<date>.md`, and fires a macOS notification if any offer
clears your ratio.

Score any scraped file directly:
```bash
.venv/bin/python score.py data/offers_sample.json
```

### Alternate path: agent-driven (Claude in Chrome)
If run on a **non-managed** Chrome profile with the Claude extension installed,
say **"check points deals"** and the agent drives your live session instead —
see `RUN.md`. Blocked on NVIDIA-managed Chrome, which is why Playwright is the
default here.

## The full daily flow
1. **Scrape + score** (you run this):
   ```bash
   cd ~/Desktop/points_tracker && ./run.sh
   ```
   Captures the Capital One **feed API** (reliable: real merchant names, domains,
   reward headlines), classifies every offer, and scores flat ones against
   `finds.json`. Writes `reports/report_<date>.md` + a notification.

2. **Find the item — Stage B** (ask the agent): for each flat offer that has no
   entry in `finds.json` yet, the agent visits the merchant's store, finds the
   **cheapest in-stock qualifying item**, and writes it into `finds.json`. Then
   re-run `./run.sh` (or `score.py`) and the report names the exact item + true
   ratio. Say: *"find items for today's flat offers."*

3. **Verify + buy** (you): open the offer in your portal, confirm it's any-purchase
   / no-minimum / not new-customer-only, check the item's still in stock, buy it.

### Offer types (only one nets positive)
- **flat** — "10,500 miles" for any purchase → the target (Pinter).
- **capped** — "Up to 12,000 miles" → a tiered menu; the flat tiers usually need
  a service signup (phone line, subscription). Excluded.
- **multiplier** — "14X miles" → scales with spend, never wins. Excluded.

## Files
| File | Role |
|---|---|
| `run.sh` | One command: scrape → score (`--login` for first-time sign-in) |
| `scrape.py` | Playwright scraper, persistent login profile |
| `parse.py` | Pure text→offer parsing (regex), `python parse.py` self-tests |
| `score.py` | Deterministic scoring + report + notification (no deps) |
| `config.yaml` | Thresholds and mile valuation |
| `offers.schema.json` | Shape of the scraped offers JSON |
| `RUN.md` | Playbook for the alternate agent-driven (Chrome extension) path |
| `data/` | Scraped offers + raw dumps (dated) |
| `reports/` | Dated markdown reports |
| `.venv/`, `pw_profile/` | Python env and saved browser session (not committed) |

## Reality check
- The portal needs your login; there's no public API. Scraping drives your own
  Chrome session — no credentials are stored.
- Terms are the whole game: minimum spend, new-customer-only, one-time, expiry.
- Items can be **sold out** (they were for Pinter). "Deal exists" ≠ "purchasable".
- Capital One can **claw back** offers it considers gamed. Your risk.
- v1 does **not** auto-purchase.
# Point_Tracker
