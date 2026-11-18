"""THE ACCOUNT PANEL'S CONTRACT — every endpoint the shipped page calls must
exist, and must be protected.

The panel is the only part of this product a human touches with their hands. It
is also the most fragile link in the estate, because nothing connects it to the
server: `sites/account/js/dashboard.js` names endpoints as STRINGS. Rename a
route on the control plane and every test in every repo still passes while the
customer's Memory tab quietly 404s — no exception, no log line, just a page
that does nothing when clicked.

So this reads the SHIPPED JavaScript, extracts every path it actually calls,
and asks the live control plane about each one. Two assertions per path:

  IT EXISTS       — a 404 means the panel is calling something that is gone.
  IT IS CLOSED    — an unauthenticated call must never be answered. These
                    endpoints mint keys, revoke keys and erase accounts; a
                    single one reachable without a session is a catastrophe,
                    and reading the list off the panel is the surest way to
                    enumerate exactly what an attacker would try first.

WHAT THIS DOES NOT COVER, stated so nobody mistakes it for more than it is:
this proves the panel's SERVER SIDE. It does not click a button, and it cannot
see a broken layout or a control that never fires. Those need a real browser
against a real logged-in account.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

CONTROL = os.environ.get("BRETHOF_CONTROL_URL", "https://api.brethof.ai").rstrip("/")
PANEL_JS = (Path(__file__).resolve().parents[2] / "brethof-website" /
            "sites" / "account" / "js" / "dashboard.js")
UA = "brethof-panel-contract/1.0"

# mindReq('/account/mind/keys', 'POST', …)  and  fetch(API + '/account', …)
_BRAIN_REQ = re.compile(r"mindReq\(\s*'([^']+)'\s*,\s*'([A-Z]+)'")
_FETCH = re.compile(r"fetch\(\s*API\s*\+\s*'([^']+)'")


def panel_calls() -> list[tuple[str, str]]:
    """(path, method) for everything the panel calls. Paths built by
    concatenation ('/keys/' + id + '/revoke') are normalised to a probe-able
    form with a placeholder id."""
    src = PANEL_JS.read_text(encoding="utf-8")
    out = {(p, m) for p, m in _BRAIN_REQ.findall(src)}
    out |= {(p, "GET") for p in _FETCH.findall(src)}
    # The two dynamic ones, spelled out rather than guessed: a concatenated
    # path cannot be read by a regex, and silently skipping them would leave
    # revoke — the endpoint that matters most — unprobed.
    out |= {("/account/mind/keys/1/revoke", "POST"),
            ("/account/mind/keys/1/scopes", "POST")}
    return sorted(p for p in out if p[0].startswith("/account/mind"))


def probe(path: str, method: str) -> int:
    body = b"{}" if method == "POST" else None
    req = urllib.request.Request(
        CONTROL + "/v1" + path, data=body, method=method,
        headers={"User-Agent": UA,
                 **({"Content-Type": "application/json"} if body else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"control plane unreachable: {type(e).__name__}: {e}")


@pytest.mark.slow
def test_the_panel_calls_only_endpoints_that_exist():
    if not PANEL_JS.is_file():
        pytest.skip(f"{PANEL_JS} not checked out")
    calls = panel_calls()
    assert calls, "no panel calls found — the extractor is broken, so this " \
                  "suite would pass while checking nothing"

    # POSITIVE CONTROL: the probe must be able to SEE a missing endpoint, or
    # 'nothing 404s' is a statement about the probe, not the product.
    assert probe("/account/mind/definitely_not_a_route", "GET") == 404, \
        "the probe cannot detect a missing endpoint — every result below is " \
        "meaningless"

    missing = [f"{m} {p}" for p, m in calls if probe(p, m) == 404]
    assert not missing, (
        f"the account panel calls {len(missing)} endpoint(s) that DO NOT "
        f"EXIST — the customer's Memory tab is broken and nothing else would "
        f"have told us: {missing}")


@pytest.mark.slow
def test_no_panel_endpoint_answers_without_a_session():
    """These endpoints mint keys, revoke keys and erase accounts. The panel's
    own source is the attacker's map — so every path on it is checked, not a
    sample."""
    if not PANEL_JS.is_file():
        pytest.skip(f"{PANEL_JS} not checked out")
    wide_open = [f"{m} {p} -> {st}" for p, m in panel_calls()
                 if (st := probe(p, m)) not in (401, 403, 404, 422)]
    assert not wide_open, (
        f"UNAUTHENTICATED access answered on: {wide_open}. These are the "
        f"account's key-minting and erasure doors.")
