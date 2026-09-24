"""
Kafka bus config + helpers shared by the streaming pipeline.

Topics:
  points.pages    — rendered merchant pages (producer -> consumers). Partitioned
                    so multiple consumers in a group judge in parallel.
  points.verdicts — judged results (consumers -> collector).
"""
from __future__ import annotations

import os

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
TOPIC_PAGES = "points.pages"
TOPIC_VERDICTS = "points.verdicts"
TOPIC_NEW_OFFERS = "points.new_offers"   # continuous watcher -> real-time alerter
PAGES_PARTITIONS = 6      # up to this many consumers can judge in parallel
CONSUMER_GROUP = "points.judges"
ALERTER_GROUP = "points.alerters"


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
    if not want:
        return
    for topic, fut in admin.create_topics(want).items():
        try:
            fut.result()
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise
