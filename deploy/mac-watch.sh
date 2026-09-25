#!/usr/bin/env bash
# mac-watch.sh — launchd wrapper to run the continuous watcher on the Mac.
#
# This is the residential-IP FALLBACK for the Brev VM: login and the watcher live
# on the same machine, so there's no session-copying and Cloudflare sees a normal
# home connection instead of a datacenter IP.
#
# Ensures the Kafka broker is up (needs Docker Desktop running), then execs the
# watcher. launchd (com.pointstracker.watch) keeps it alive and restarts on crash.
set -uo pipefail
cd "$(dirname "$0")/.."   # repo root

# launchd hands us a bare PATH; add Homebrew + Docker so `docker` resolves.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Best-effort: bring up the local broker. If Docker Desktop isn't running this
# fails loudly in the log and the watcher will report BROKER DOWN via ./doctor.
docker compose up -d >/dev/null 2>&1 \
  || echo "$(date -u '+%FT%TZ') warning: could not start Kafka (is Docker Desktop running?)"

exec ./points watch --interval 30 --alerters 1
