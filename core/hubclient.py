"""The container's one call home.

DIRECTION MATTERS. Every remaining hub interaction goes container -> hub:
fetch a prompt, fetch rules, check the licence. Nothing needs the hub to reach
INTO a customer's box any more — recall, archive and curation are all local —
so this is plain HTTPS, not a held-open pipe. A request/response client has no
connection to babysit, works through any corporate proxy, and cannot leave a
box quietly disconnected while looking fine.

DEGRADE, DON'T FAIL. The hub being unreachable must never cost a customer
their memory: recall and archive do not call here at all. What stops is
judgment — merges wait as tasks, heal is not offered. That is the licence
working as designed, and it is also exactly what an outage looks like, so this
module says WHICH it is rather than letting a customer read a network blip as
an expired subscription.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HUB_URL = os.environ.get("BRAIN_HUB_URL", "https://hub.brethof.ai")
HUB_KEY = os.environ.get("BRAIN_HUB_KEY", "")
TIMEOUT_S = float(os.environ.get("BRAIN_HUB_TIMEOUT_S", "15"))
# Prompts change on our release schedule, not the customer's, so a short
# in-process cache removes almost every call without going stale in practice.
CACHE_TTL_S = float(os.environ.get("BRAIN_HUB_CACHE_TTL_S", "900"))

_cache: dict[str, tuple[float, str]] = {}


class HubUnavailable(RuntimeError):
    """The hub could not be reached. NOT the same as being refused."""


class HubRefused(RuntimeError):
    """The hub answered and said no — an unknown or inactive key."""


def _get(path: str) -> dict:
    if not HUB_KEY:
        raise HubRefused(
            "no BRAIN_HUB_KEY set — this box has no licence configured, so "
            "the judgment features (merge, heal, teaching) are off. Recall "
            "and archiving are unaffected.")
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path,
        headers={"Authorization": f"Bearer {HUB_KEY}",
                 "User-Agent": "brethof-brain-selfhosted/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HubRefused(
                "the hub refused this box's key. Check the subscription at "
                "brethof.ai/account — memory itself keeps working.") from e
        raise HubUnavailable(f"hub returned HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        # NAME RESOLUTION IS ITS OWN DIAGNOSIS. On podman, a container on a
        # custom network resolves other containers through aardvark-dns but
        # gets NO upstream when the host runs systemd-resolved: resolv.conf
        # points at the 127.0.0.53 stub, which means nothing inside the
        # container's netns. The symptom is "cannot reach the hub", which
        # sends people looking at firewalls and subscriptions. Say the real
        # cause instead. Docker's embedded DNS does not have this problem.
        if "name resolution" in str(e).lower() or "-3" in str(e):
            raise HubUnavailable(
                "the container cannot resolve DNS at all — this is a "
                "container networking problem, not the hub and not your "
                "subscription. On podman with systemd-resolved, a custom "
                "network needs explicit upstream resolvers: set BRAIN_DNS in "
                "your .env to your resolver(s), e.g. BRAIN_DNS=192.168.1.1. "
                "Docker does not need this.") from e
        raise HubUnavailable(
            f"could not reach the hub ({type(e).__name__}). This is a "
            f"connection problem, not a licence problem.") from e


def _send(method: str, path: str, body: dict | None = None,
          timeout: float | None = None) -> dict:
    """`timeout` overrides TIMEOUT_S for one call. The default (15 s) is right
    for a rule PUT or an index fetch; a JUDGMENT is a model call and can run
    a minute when the provider is slow — the box gave up at 15 s three times
    in a row on 2026-09-02 and dropped a record from heal for good."""
    if not HUB_KEY:
        raise HubRefused(
            "no BRAIN_HUB_KEY set — this box has no licence configured.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path, data=data, method=method,
        headers={"Authorization": f"Bearer {HUB_KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "brethof-brain-selfhosted/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # WHO SAID NO — the hub, or something in front of it? Every refusal we
        # issue is JSON with an "ok" field; an edge (Cloudflare WAF, a proxy,
        # a captive portal) answers with HTML. Measured 2026-08-28: Cloudflare
        # 403s a plain Python-urllib User-Agent, and treating that as a
        # licence refusal would send a PAYING customer to their billing page
        # over a bot rule. Never let someone else's 403 speak for our billing.
        body, ours = "", False
        try:
            payload = json.load(e)
            ours = isinstance(payload, dict) and "ok" in payload
            body = str(payload.get("why", "")) if ours else ""
        except Exception:                                    # noqa: BLE001
            pass
        if not ours:
            raise HubUnavailable(
                f"something between this box and the hub returned HTTP "
                f"{e.code} (not the hub itself — the reply was not ours). "
                f"A proxy, firewall or CDN rule is the usual cause. Your "
                f"subscription is NOT the problem.") from e
        if e.code in (401, 403):
            raise HubRefused(
                body or "the hub refused this box's key — check the "
                        "subscription at brethof.ai/account.") from e
        if 400 <= e.code < 500:
            # The hub answering with a reason: a rule over the cap, a ceiling
            # hit. Surface it rather than flattening it into "unavailable",
            # which sends someone debugging their network over a typo. The
            # payload rides along (2026-09-09: a closed provider window
            # answers with the seconds to wait — `retry_s`).
            exc = HubRefused(body or f"hub refused: HTTP {e.code}")
            exc.payload = payload if ours else {}
            exc.status = e.code
            raise exc from e
        raise HubUnavailable(f"hub returned HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        if "name resolution" in str(e).lower() or "-3" in str(e):
            raise HubUnavailable(
                "the container cannot resolve DNS at all — set BRAIN_DNS in "
                "your .env (podman with systemd-resolved needs it; Docker "
                "does not).") from e
        raise HubUnavailable(
            f"could not reach the hub ({type(e).__name__}).") from e


def rules(project: str = "", timeout: float | None = None) -> dict:
    """The rules a session needs, straight from the hub — the only store.
    `project` narrows to that project's law + the general law + the seeds;
    the hook path passes a short timeout so a slow hub delays a session by
    seconds, never minutes."""
    q = f"?project={urllib.parse.quote(project)}" if project else ""
    return _send("GET", f"/v1/rules{q}", timeout=timeout)


def match_rule(project: str, prompt: str, timeout: float = 2.0,
               vector: list | None = None) -> dict:
    """The start-of-message hook's one question to the hub. Two seconds: a
    slow hub costs a prompt at most that, then the hook goes on without.
    With the prompt's vector the hub answers semantically (the calibrated
    match); without it, by words."""
    body = {"prompt": prompt[:4000]}
    if vector:
        body["vector"] = [round(float(x), 5) for x in vector]
    return _send("POST", f"/v1/rules/match/{urllib.parse.quote(project)}",
                 body, timeout=timeout)


def delete_project(project: str) -> dict:
    """Take a deleted project's footprint off the hub: rules + purpose,
    verdicts, kind list, name index."""
    return _send("DELETE", f"/v1/project/{urllib.parse.quote(project)}",
                 timeout=15.0)


def put_rule_vectors(vectors: dict) -> dict:
    return _send("POST", "/v1/rules/vectors", {"vectors": vectors}, timeout=30.0)


def put_rule(rule_id: str, title: str, content: str,
             area: str = "all", pin: bool = True) -> dict:
    return _send("PUT", f"/v1/rules/{rule_id}",
                 {"title": title, "content": content, "area": area,
                  "pin": pin})


def delete_rule(rule_id: str) -> bool:
    try:
        return bool(_send("DELETE", f"/v1/rules/{rule_id}").get("ok"))
    except HubRefused as e:
        if "no such rule" in str(e):
            return False
        raise


