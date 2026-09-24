#!/usr/bin/env bash
# Fully autonomous daily run: scrape -> price (render+judge) -> report + notify.
# Loads ANTHROPIC_API_KEY from .env (never committed). Schedule this once/day.
set -euo pipefail
cd "$(dirname "$0")"

# load local secrets (NVIDIA_API_KEY=...)
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
if [[ -z "${NVIDIA_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "NVIDIA_API_KEY not set — put it in .env (see .env.example)" >&2
  exit 1
fi

PY=".venv/bin/python"

echo "== [1/2] scraping offers (gentle) =="
"$PY" scrape.py

echo "== [2/2] pricing opportunities (render + judge) =="
"$PY" autohunt.py

echo "done — see reports/report_$(date +%F).md"
