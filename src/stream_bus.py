"""
Kafka bus config + helpers shared by the streaming pipeline.

Topics:
  points.pages    — rendered merchant pages (producer -> consumers). Partitioned
                    so multiple consumers in a group judge in parallel.
  points.verdicts — judged results (consumers -> collector).
  points.new_offers — continuous watcher -> real-time alerter.

Two-stage pipeline (stream_pipeline2 / ./points stream2):
  points.offers     — raw expanded offers (feed producer -> classifiers).
  points.candidates — single-purchase flat opportunities (classifier -> pricers).
                      Partitioned + KEYED BY DOMAIN so every candidate for one
                      merchant lands on one partition -> one pricer -> same-merchant
                      renders are serialized (never concurrent).
  points.rated      — priced + judged opportunities (pricers -> collector).
"""
from __future__ import annotations

import os

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "127.0.0.1:9092")
TOPIC_PAGES = "points.pages"
TOPIC_VERDICTS = "points.verdicts"
TOPIC_NEW_OFFERS = "points.new_offers"   # continuous watcher -> real-time alerter
PAGES_PARTITIONS = 6      # up to this many consumers can judge in parallel
CONSUMER_GROUP = "points.judges"
ALERTER_GROUP = "points.alerters"

# --- two-stage pipeline (stream_pipeline2) ---
TOPIC_OFFERS = "points.offers"
TOPIC_CANDIDATES = "points.candidates"
TOPIC_RATED = "points.rated"
CANDIDATES_PARTITIONS = 6   # keyed by domain -> per-merchant serialization
CLASSIFIER_GROUP = "points.classifiers"
PRICER_GROUP = "points.pricers"
COLLECTOR2_GROUP = "points.collector2"


def ensure_topics(bootstrap: str = BOOTSTRAP) -> None:
    """Create the topics if they don't exist (idempotent)."""
    from confluent_kafka.admin import AdminClient, NewTopic
    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing = set(admin.list_topics(timeout=10).topics)
    want = []
    if TOPIC_PAGES not in existing:
        want.append(NewTopic(TOPIC_PAGES, num_partitions=PAGES_PARTITIONS,
                             replication_factor=1))
    if TOPIC_VERDICTS not in existing:
        want.append(NewTopic(TOPIC_VERDICTS, num_partitions=1, replication_factor=1))
    if TOPIC_NEW_OFFERS not in existing:
        want.append(NewTopic(TOPIC_NEW_OFFERS, num_partitions=1, replication_factor=1))
    if TOPIC_OFFERS not in existing:
        want.append(NewTopic(TOPIC_OFFERS, num_partitions=1, replication_factor=1))
    if TOPIC_CANDIDATES not in existing:
        want.append(NewTopic(TOPIC_CANDIDATES, num_partitions=CANDIDATES_PARTITIONS,
                             replication_factor=1))
    if TOPIC_RATED not in existing:
        want.append(NewTopic(TOPIC_RATED, num_partitions=1, replication_factor=1))
    if not want:
        return
    for topic, fut in admin.create_topics(want).items():
        try:
            fut.result()
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise
