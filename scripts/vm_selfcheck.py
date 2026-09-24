#!/usr/bin/env python3
"""Deployment self-check: fire a phone push + verify Kafka from this host."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import stream_alerter as a
import stream_bus as bus

pushed = a._phone_push("✅ Point Tracker deployed on VM",
                       "openclaw is live — cloud → phone works")
print("ntfy push from VM:", "OK" if pushed else "FAILED (check NTFY_TOPIC in .env)")
bus.ensure_topics()
print("kafka on VM: OK (topics ready)")
