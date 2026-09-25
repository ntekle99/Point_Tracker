# RUNBOOK — continuous watcher (read this first if you're an agent on this box)

You are on the **openclaw VM** (`ntekle-openclaw-practice`), the always-on box that
runs the Capital One deal watcher 24/7. If something looks wrong, your first move is
almost always:

```bash
cd /home/ubuntu/Point_Tracker && ./doctor
```

`doctor` prints a single `VERDICT:` and `RECOMMENDED ACTION:` line. Follow that. Do
not improvise around it unless it clearly misdiagnosed.

## What this system does (30-second model)

- `points-watch` (systemd, `Restart=always`) runs `src/stream_watch.py`.
- Every **30 min** it re-opens the Capital One offers feed in a headless browser,
  diffs against `data/seen_offers.json`, and publishes newly-appeared offers to a
  Kafka topic. An alerter consumer prices the good ones and pushes them to the
  phone via **ntfy** (`NTFY_TOPIC` in `.env`).
- Healthy = `seen_offers.json` mtime advances every ~30 min and the log shows
  `[HH:MM] N offers, M new` lines. No `not logged in`.

## The commands you have

```bash
./doctor            # full diagnosis (default) — START HERE
./doctor restart    # restart the service, then re-diagnose
./doctor heal       # restart + diagnose + print the fix if it can't self-heal
./doctor logs       # live log stream
systemctl status points-watch
tail -30 data/watch.log
```

`sudo systemctl restart points-watch` is passwordless here, so `./doctor restart`
works unattended.

## Failure modes → fix

| VERDICT | Meaning | Fix |
|---|---|---|
| `SERVICE DOWN` | systemd unit not active | `./doctor restart` |
| `BROKER DOWN` | Kafka not reachable on :9092 | `docker compose up -d` then `./doctor restart` |
| `STALLED` | service up but no fresh poll in >70 min | `./doctor heal`; if it recurs, check `data/watch.log` for tracebacks |
| `SESSION EXPIRED` | log shows `not logged in` | **You cannot fix this here — see below.** |

## The one thing this box CANNOT do: log in

Capital One sign-in needs a real screen (credentials + 2FA). This VM is headless,
so `./points login` here just hangs. When `doctor` says `SESSION EXPIRED`, the
session must be refreshed **from the Mac** and copied over:

```bash
# on the Mac (has a screen):
cd ~/Desktop/points_tracker && ./points login          # sign in when the browser opens
brev copy ./pw_state.json ntekle-openclaw-practice:/home/ubuntu/Point_Tracker/pw_state.json

# then back on this box:
./doctor restart
```

Once it authenticates once, the **live `pw_profile/` self-refreshes** (the watcher
re-navigates each cycle and writes rotated cookies back), so re-login should only
be needed when Capital One forces a full re-auth — not every hour.

## Known risk

This is a **datacenter IP**. Cloudflare trusts it less than a home connection, so
it may re-challenge the headless browser aggressively. If the session keeps dying
within ~1 hour even right after a fresh copy, the code can't beat that — the
durable fix is to move the watcher to a residential-IP machine (the Mac itself).
Flag this to the human; don't keep re-copying sessions in a loop.
