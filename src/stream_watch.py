#!/usr/bin/env python3
"""
Continuous watcher (PRODUCER) for the real-time pipeline.

Keeps one logged-in browser session open and re-polls the Capital One feed API
every `--interval` minutes. It diffs against the offers it has already seen and
publishes only the *newly appeared* offers to `points.new_offers`. A separate
alerter consumer turns those into instant notifications.

Also acts as the orchestrator for `./points watch`: it starts the alerter
consumer group, then runs the poll loop.

Gentleness: it reuses ONE session and hits only the JSON feed API (paced), so a
poll is far lighter than a full scrape. Still — don't set a tiny interval; new
offers rotate roughly daily, so 30–60 min is plenty and keeps you off the radar.

    ./points watch --interval 30 --alerters 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from confluent_kafka import Producer

import parse as P
import scrape
import stream_bus as bus

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "pw_profile"
DATA = ROOT / "data"
SEEN_PATH = DATA / "seen_offers.json"
# One-shot flag so we push the "session expired" alert ONCE, not on every 60s
# systemd restart. Cleared the moment a poll succeeds again (re-arms the alert).
SESSION_ALERT_FLAG = DATA / ".session_alerted"
FEED_URL = "https://capitaloneoffers.com/feed"
BASE = "https://capitaloneoffers.com"
HDRS = {"Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/feed"}


def _sig(o: dict) -> str:
    return f"{o.get('merchant','').lower()}|{o.get('reward_type')}|{int(o.get('reward_miles') or 0)}"


def fetch_all_offers(page, feed_url: str) -> list[dict]:
    """Pull the whole feed via the cursor-paginated JSON API and parse it."""
    items, seen_pages = [], 0
    r = page.request.get(feed_url, headers=HDRS, timeout=20000)
    j = r.json()
    items.extend(j.get("data") or [])
    cursor = j.get("cursor")
    while cursor and seen_pages < 60:
        api = f"{feed_url}&cursor={quote(cursor, safe='-_')}"
        try:
            r = page.request.get(api, headers=HDRS, timeout=20000)
            j = r.json()
        except Exception:
            break
        data = j.get("data") or []
        if not data:
            break
        items.extend(data)
        nc = j.get("cursor")
        if nc == cursor:
            break
        cursor = nc
        seen_pages += 1
        time.sleep(0.8)
    offers, keys = [], set()
    for it in items:
        o = P.parse_feed_item(it)
        if not o:
            continue
        k = _sig(o)
        if k in keys:
            continue
        keys.add(k)
        offers.append(o)
    return offers


def _notify_session_dead() -> None:
    """Push ONE ntfy alert when the Capital One session has lapsed, so a re-login
    is a minutes-long chore instead of a silent multi-day blackout. De-duped via
    SESSION_ALERT_FLAG so the 60s systemd restart loop doesn't spam the phone."""
    if SESSION_ALERT_FLAG.exists():
        return
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            req = urllib.request.Request(
                f"{server}/{topic}",
                data=b"Capital One session expired on the watcher. "
                     b"Run ./points login, then copy pw_state.json to the VM.",
                method="POST",
                headers={"Title": "Point Tracker session expired",
                         "Priority": "urgent", "Tags": "warning"})
            urllib.request.urlopen(req, timeout=10)
            print("  (pushed session-expired alert)", flush=True)
        except Exception as e:
            print(f"  (session-expired push failed: {str(e)[:80]})", flush=True)
    try:
        SESSION_ALERT_FLAG.write_text(dt.datetime.now().isoformat())
    except Exception:
        pass


def _clear_session_alert() -> None:
    """A poll succeeded — re-arm the expiry alert for next time."""
    try:
        SESSION_ALERT_FLAG.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _open_feed(page, feed_url: dict) -> bool:
    """Navigate the real feed page (not just the JSON API) and capture the feed
    token. A genuine navigation each cycle lets Cloudflare reissue its short-lived
    __cf_bm bot cookie and rotates the feed token — which is what keeps the session
    alive for days instead of dying with the first 30-min cookie. Returns True if
    we landed on an authenticated feed, False if bounced to sign-in."""
    feed_url["v"] = None
    try:
        page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return False
    for _ in range(30):
        if feed_url["v"]:
            break
        try:
            page.mouse.wheel(0, 1500)
        except Exception:
            pass
        time.sleep(0.5)
    return bool(feed_url["v"]) and "signin" not in page.url.lower()


def run_watch(interval_min: int, alerters: int, once: bool = False) -> int:
    DATA.mkdir(exist_ok=True)
    seen = set(json.loads(SEEN_PATH.read_text())) if SEEN_PATH.exists() else set()
    first_run = not seen

    # start the alerter consumer group
    alerter_script = str(ROOT / "src" / "stream_alerter.py")
    try:
        bus.ensure_topics()
    except Exception as e:
        print(f"cannot reach Kafka at {bus.BOOTSTRAP}: {e}\n"
              f"start the broker:  docker compose up -d", file=sys.stderr)
        return 2
    procs = [subprocess.Popen([sys.executable, alerter_script]) for _ in range(alerters)]

    producer = Producer({"bootstrap.servers": bus.BOOTSTRAP})
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--hide-crash-restore-bubble"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        feed_url = {"v": None}
        def on_response(resp):
            try:
                if "/feed/" in resp.url and "?" in resp.url and "/offers/" not in resp.url \
                        and "json" in resp.headers.get("content-type", "") and not feed_url["v"]:
                    feed_url["v"] = resp.url
            except Exception:
                pass
        page.on("response", on_response)

        # Prefer the LIVE pw_profile: a persistent context rotates cookies back to
        # disk as the server refreshes them, so the session self-renews and lasts
        # for days. Only fall back to the frozen pw_state.json snapshot if the
        # profile has no valid session (e.g. first boot on a fresh VM) — a snapshot
        # can't self-refresh, so it's a bootstrap, not the steady state.
        ok = _open_feed(page, feed_url)
        if not ok:
            try:
                n = scrape.load_session_cookies(ctx)
                if n:
                    print(f"profile session invalid — injected {n} cookies from "
                          f"portable snapshot", flush=True)
                    ok = _open_feed(page, feed_url)
            except Exception as e:
                print(f"(cookie import skipped: {e})", flush=True)
        if not ok:
            print("not logged in — run: ./points login", file=sys.stderr)
            _notify_session_dead()
            ctx.close()
            for p in procs:
                p.terminate()
            return 2
        _clear_session_alert()

        print(f"👀 watching every {interval_min} min "
              f"({alerters} alerter(s)). Ctrl-C to stop.", flush=True)
        try:
            established = True   # startup already navigated + captured the token
            while True:
                # Re-navigate the real feed page each cycle (startup did the first
                # one) so Cloudflare reissues __cf_bm and the feed token stays fresh.
                # A bounce to sign-in here means the session finally lapsed.
                if not established:
                    if not _open_feed(page, feed_url):
                        print("session dropped mid-run — run: ./points login",
                              file=sys.stderr)
                        _notify_session_dead()
                        break
                established = False
                _clear_session_alert()

                offers = fetch_all_offers(page, feed_url["v"])
                new = [o for o in offers if _sig(o) not in seen]
                for o in offers:
                    seen.add(_sig(o))
                SEEN_PATH.write_text(json.dumps(sorted(seen)))
                # Persist the refreshed cookies back to the portable snapshot so a
                # future restart/copy starts from a live session, not a stale one.
                try:
                    ctx.storage_state(path=str(scrape.STATE_FILE))
                except Exception:
                    pass
                stamp = dt.datetime.now().strftime("%H:%M")
                if first_run:
                    print(f"  [{stamp}] baseline: {len(offers)} offers "
                          f"(no alerts on first run)", flush=True)
                    first_run = False
                else:
                    flat_new = [o for o in new if o["reward_type"] == "flat"]
                    print(f"  [{stamp}] {len(offers)} offers, {len(new)} new "
                          f"({len(flat_new)} flat)", flush=True)
                    for o in new:
                        o["detected_at"] = dt.datetime.now().isoformat()
                        producer.produce(bus.TOPIC_NEW_OFFERS,
                                         key=(o.get("domain") or "").encode(),
                                         value=json.dumps(o).encode())
                    producer.flush(10)
                if once:
                    break
                time.sleep(max(interval_min, 1) * 60)
        except KeyboardInterrupt:
            print("\nstopping watcher...", flush=True)
        finally:
            ctx.close()
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(10)
                except Exception:
                    p.kill()
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30, help="minutes between polls")
    ap.add_argument("--alerters", type=int, default=1)
    ap.add_argument("--once", action="store_true", help="poll once and exit (cron-friendly)")
    args = ap.parse_args(argv)
    return run_watch(args.interval, args.alerters, args.once)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
