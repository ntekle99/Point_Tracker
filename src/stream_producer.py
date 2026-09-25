"""
Producer: render merchant pages (in parallel) and publish each to the
`points.pages` topic. Rendering is decoupled from analysis — as soon as a page
is rendered it's on the bus, and consumers judge it independently.
"""
from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, as_completed

import pricecheck
from confluent_kafka import Producer

import stream_bus as bus


def _render_job(job):
    """Pool worker: render one merchant page (no judging). Module-level so it's
    picklable. Returns (offer, url, rendered|None, error|None)."""
    offer, url = job
    try:
        rendered = pricecheck.find_prices(url)
        # surface a bot-block / rate-limit as an error so it isn't published as a
        # (priceless) page to be judged; the merchant is retried on a later run.
        if rendered.get("blocked"):
            return (offer, url, None, f"blocked/throttled: {rendered.get('note','')}"[:150])
        return (offer, url, rendered, None)
    except Exception as e:
        return (offer, url, None, str(e)[:150])


def produce_pages(jobs: list[tuple[dict, str]], bootstrap: str = bus.BOOTSTRAP,
                  render_workers: int = 6) -> int:
    """jobs: list of (offer, url). Renders them across a process pool and
    publishes each result to points.pages as it finishes. Returns the count."""
    producer = Producer({"bootstrap.servers": bootstrap,
                         "linger.ms": 50, "compression.type": "gzip"})
    sent = 0

    def _delivery(err, msg):
        if err is not None:
            print(f"  (produce error: {err})", flush=True)

    with ProcessPoolExecutor(max_workers=render_workers) as ex:
        futs = [ex.submit(_render_job, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            offer, url, rendered, err = fut.result()
            payload = {"offer": offer, "url": url, "rendered": rendered, "err": err}
            producer.produce(bus.TOPIC_PAGES,
                             key=(offer.get("domain") or "").encode(),
                             value=json.dumps(payload).encode(),
                             callback=_delivery)
            producer.poll(0)          # serve delivery callbacks
            sent += 1
            n_prices = len((rendered or {}).get("prices", [])) if rendered else 0
            print(f"  [produced {i}/{len(jobs)}] {offer.get('merchant','?')} "
                  f"({n_prices} prices)", flush=True)
    producer.flush(30)
    return sent
