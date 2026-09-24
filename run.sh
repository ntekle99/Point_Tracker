#!/usr/bin/env bash
# Daily run: scrape the Capital One Offers feed, then score it.
# First time only, run:  ./run.sh --login
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

if [[ "${1:-}" == "--login" ]]; then
  "$PY" scrape.py --login
  exit 0
fi

"$PY" scrape.py "$@"
latest="$(ls -t data/offers_*.json | head -1)"
echo
"$PY" score.py "$latest"
