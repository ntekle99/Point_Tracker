# Daily run playbook (for the agent)

When the user says **"check points deals"** (or similar), do this:

## 1. Scrape the portal (Claude-in-Chrome)
1. Confirm Chrome is open and the user is logged into Capital One.
2. Navigate to the Capital One Offers feed:
   `https://capitaloneoffers.com/feed`
3. Scroll / paginate through the offer cards. For **each** offer capture:
   - `merchant`
   - headline reward → classify `reward_type`:
     - contains "X miles" / "X%" per dollar → **multiplier**, `reward_miles` = the multiple
     - a fixed lump ("7,000 miles") → **flat**, `reward_miles` = the number
   - open the offer / terms and extract **minimum spend** → `min_spend_usd`
     (null if none stated). This is the single most important field.
   - `url` (merchant link) and raw `terms` text.
4. Write the array to `data/offers_<YYYY-MM-DD>.json` matching `offers.schema.json`.

> Only **flat** offers can net positive. Still capture multipliers so the JSON
> is complete, but they'll be filtered out by scoring.

## 2. Score
```bash
python3 score.py data/offers_<YYYY-MM-DD>.json
```
This writes `reports/report_<date>.md`, prints it, and fires a macOS
notification if anything clears the ratio.

## 3. Confirm the cheap purchase (the human-in-the-loop step)
For each ✅ winner flagged **"confirm cheapest item"**:
- Open the merchant store, find the cheapest item that satisfies any minimum
  spend and is actually **in stock** (the Pinter yeast packets were sold out).
- If a real qualifying purchase exists, the deal is live. If not, note it.

## 4. Report back to the user
Summarize the winners in chat: merchant, miles, est. value, ratio, and whether
a cheap in-stock qualifying item exists. Flag any "new-customer-only" or
"one-time" terms.

## Guardrails
- Never store Capital One credentials. Drive the already-logged-in session only.
- Do **not** auto-purchase in v1.
- Surface clawback risk on anything that looks like an obvious loophole.
