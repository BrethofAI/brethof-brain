"""Run the hub on the customer's machine (2026-11-17, local-first).

Two doors in one process, sharing one store:
  internal  HUB_INTERNAL_PORT (8601) — the storages' pipes, rules, curation
            (hub.server). Published on the private network only.
  front     HUB_FRONT_PORT (8600) — the one door devices and the owner use
            (hub.front): pairing, settings, forwarding.

    python -m hub.local                 run both
    python -m hub.local reset-password  new settings-page password
    python -m hub.local connect         connect this hub to a Brethof account
                                        (the installer runs it once)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import signal
import sys
import threading
import time
import urllib.request

import uvicorn


class _Server(uvicorn.Server):
    # Two servers in one loop: neither may take the process's signals for
    # itself, or a stop reaches only one of them. main() owns the signals.
    @contextlib.contextmanager
    def capture_signals(self):
        yield


# THE WATCHDOG (2026-11-19): a hub that hangs is a memory that is down, and
# neither Docker nor Podman restarts a container whose health check fails
# (Docker only marks it unhealthy; Podman's default action is none). So the
# hub watches itself: a THREAD, not a task, because a stuck event loop would
# stop a task with it. Both doors are asked every WATCHDOG_EVERY seconds; after
# WATCHDOG_MISSES misses in a row the process says why and exits non-zero, and
# the restart policy (unless-stopped, or on-failure under podman) brings it back.
WATCHDOG_EVERY = float(os.environ.get("HUB_WATCHDOG_EVERY", "30"))
WATCHDOG_TIMEOUT = float(os.environ.get("HUB_WATCHDOG_TIMEOUT", "10"))
WATCHDOG_MISSES = int(os.environ.get("HUB_WATCHDOG_MISSES", "3"))
WATCHDOG_EXIT = 70


def _probe(url: str) -> str | None:
    """None when the door answered 200, else what went wrong."""
    try:
        with urllib.request.urlopen(url, timeout=WATCHDOG_TIMEOUT) as r:
            return None if r.status == 200 else f"answered {r.status}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def watchdog(urls: list[str], exit=os._exit, sleep=time.sleep) -> None:
    misses = 0
    while True:
        sleep(WATCHDOG_EVERY)
        bad = [f"{u} {why}" for u in urls if (why := _probe(u))]
        if not bad:
            misses = 0
            continue
        misses += 1
        print(f"hub watchdog: miss {misses}/{WATCHDOG_MISSES}: {'; '.join(bad)}",
              file=sys.stderr, flush=True)
        if misses >= WATCHDOG_MISSES:
            print("hub watchdog: the hub is not answering — exiting so it is restarted",
                  file=sys.stderr, flush=True)
            exit(WATCHDOG_EXIT)
            return


async def main() -> None:
    from hub import front, server
    internal = _Server(uvicorn.Config(server.app, host=os.environ.get("HUB_INTERNAL_HOST", "0.0.0.0"),
                                      port=int(os.environ.get("HUB_INTERNAL_PORT", "8601")),
                                      ws_max_size=server.PIPE_MAX_BYTES, log_level="warning"))
    door = _Server(uvicorn.Config(front.front, host=os.environ.get("HUB_FRONT_HOST", "0.0.0.0"),
                                  port=int(os.environ.get("HUB_FRONT_PORT", "8600")),
                                  proxy_headers=True, forwarded_allow_ips="*", log_level="warning"))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [setattr(s, "should_exit", True) for s in (internal, door)])
    srv = asyncio.create_task(internal.serve())
    while not internal.started and not srv.done():
        await asyncio.sleep(0.05)
    await front.boot()
    fsrv = asyncio.create_task(door.serve())
    while not door.started and not fsrv.done():
        await asyncio.sleep(0.05)
    if WATCHDOG_EVERY > 0:
        ports = (os.environ.get("HUB_INTERNAL_PORT", "8601"), os.environ.get("HUB_FRONT_PORT", "8600"))
        threading.Thread(target=watchdog, args=([f"http://127.0.0.1:{p}/health" for p in ports],),
                         name="hub-watchdog", daemon=True).start()
    # either door ending ends the hub: a half-alive hub is a down memory
    done, _ = await asyncio.wait({srv, fsrv}, return_when=asyncio.FIRST_COMPLETED)
    if not (internal.should_exit or door.should_exit):
        print("hub: a door stopped on its own — exiting so the hub is restarted",
              file=sys.stderr, flush=True)
        os._exit(WATCHDOG_EXIT)
    await asyncio.gather(srv, fsrv)


def reset_password() -> None:
    from hub import store
    tok = "ha_" + secrets.token_urlsafe(24)
    store.connect().execute("INSERT INTO settings (k, v) VALUES ('_admin_hash', ?)"
                            " ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                            (hashlib.sha256(tok.encode()).hexdigest(),))
    print(f"new settings page password: {tok}\n(restart the hub for a running page to accept it)")


def connect(return_url: str = "", wait_s: int = 900) -> int:
    """Connect this hub to a Brethof account, through the running hub's own
    settings API (2026-11-19: the installer does it once, at install time).
    Prints `LINK <url>` and `CODE <code>` for the installer, then waits for
    the owner to approve on brethof.ai. Exit 0 connected, 1 not."""
    import json as _json
    import urllib.request

    port = os.environ.get("HUB_FRONT_PORT", "8600")
    tok = open(os.environ.get("HUB_ADMIN_TOKEN_FILE", "/srv/hub/admin-token")).read().strip()
    if tok.startswith("#"):
        print("FAILED the settings password was changed by its owner; "
              "connect from the settings page instead", flush=True)
        return 1

    def call(path: str) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/api/{path}", data=b"{}",
                                     headers={"authorization": "Bearer " + tok,
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return _json.loads(r.read())
        except Exception as e:                                       # noqa: BLE001
            return {"ok": False, "why": f"{type(e).__name__}: {e}"}

    st = call("activate/poll")
    if (st.get("licence") or {}).get("state") == "valid" and not (st.get("licence") or {}).get("refused"):
        print("CONNECTED already", flush=True)
        return 0
    r = call("activate/start")
    if not r.get("ok"):
        print(f"FAILED could not start: {r.get('why')}", flush=True)
        return 1
    from urllib.parse import quote
    link = f"{r['verification_uri']}?code={quote(r['user_code'])}"
    if return_url:
        link += "&return=" + quote(return_url, safe="")
    print(f"LINK {link}", flush=True)
    print(f"CODE {r['user_code']}", flush=True)
    until = time.monotonic() + min(wait_s, int(r.get("expires_in") or 900))
    while time.monotonic() < until:
        time.sleep(int(r.get("interval") or 5))
        p = call("activate/poll")
        if p.get("status") == "ok":
            lic = p.get("licence") or {}
            print(f"CONNECTED {lic.get('devices')} devices", flush=True)
            return 0
        if p.get("status") in ("denied", "expired"):
            print(f"FAILED {p['status']}", flush=True)
            return 1
    print("FAILED timed out", flush=True)
    return 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["connect"]:
        sys.exit(connect(sys.argv[2] if len(sys.argv) > 2 else ""))
    if sys.argv[1:] == ["reset-password"]:
        reset_password()
    else:
        asyncio.run(main())
