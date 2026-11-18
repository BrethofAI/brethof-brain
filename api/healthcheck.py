#!/usr/bin/env python3
"""The box's container health command.

Answers one question a container runtime can act on: is this box able to do
its job? Unhealthy means the database is unreachable, or the box cannot
RESOLVE the hub — the one outbound failure a container fixes by being
restarted onto a live network. A hub that is merely down, refusing or
rate-limiting is NOT unhealthy here: that is somebody else's outage and
restarting this container would not touch it.

Why it exists: 2026-08-23, twenty-six agent lanes fed nothing for a whole run
because of a podman DNS quirk while every health door said ok; 2026-09-06, our
own hub went deaf for 45 hours the same way. The box knew both times and told
only its log.
"""
import json
import os
import sys
import urllib.request

# THE DOOR IS /v1/health. Written as /health first, which answers 404 — a
# health command pointed at the wrong path marks every container
# unhealthy forever, and it would have shipped to customers. The test
# beside this pins the path to the route in api/main.py.
URL = os.environ.get("BRAIN_HEALTH_URL", "http://127.0.0.1:8610/v1/health")
try:
    d = json.load(urllib.request.urlopen(URL, timeout=5))
except Exception as e:  # noqa: BLE001 — a box that cannot answer is unhealthy
    print(f"health unreachable: {type(e).__name__}")
    sys.exit(1)
if not d.get("db", False):
    print("database unreachable")
    sys.exit(1)
if not d.get("resolver_ok", True):
    print(f"cannot resolve the hub: {d.get('hub_err')} since {d.get('hub_err_at')} "
          f"— exchanges are being dropped uncurated (podman: set BRAIN_DNS)")
    sys.exit(1)
sys.exit(0)
