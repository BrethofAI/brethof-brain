#!/usr/bin/env python3
"""The container's health command (deploy/hub/brain-hub.container).

Exit 1 only when the hub reports its resolver dead — the one failure a
restart heals, because the resolver belongs to the pod network and a fresh
container re-attaches to it. A provider outage on the far side is NOT this
container's fault and must not restart it: the usage buffer is in memory.
2026-09-06→08: 45 hours with a dead resolver, 200 OK on every health poll.
"""
import json
import sys
import urllib.request

try:
    d = json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5))
except Exception as e:  # noqa: BLE001 — a hub that does not answer is unhealthy too
    print(f"health unreachable: {type(e).__name__}")
    sys.exit(1)
if d.get("resolver_ok", True):
    sys.exit(0)
print(f"resolver dead: {d.get('outbound_err')} since {d.get('outbound_err_at')}")
sys.exit(1)
