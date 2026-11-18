"""Multi-box endpoint guard (2026-08-19).

The control plane's validate-key answer now names the data-plane box the
key's tenant lives on. A box that knows its own name (BRAIN_BOX_NAME) must
reject a key that belongs elsewhere AT AUTH, with a clear line — not
validate it and let the request die later on a database this box does not
hold. Either side staying silent (old control plane, unset box name) must
keep the pre-multi-box behaviour: accept.

Offline: the control plane's HTTP answer is faked at the urlopen seam.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from core import auth


class _Resp:
    """Just enough of an HTTP response for json.load() in a with-block."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def control(monkeypatch):
    """Wire _control_lookup to a fake control plane; returns a setter that
    installs the next validate-key answer."""
    monkeypatch.setattr(auth, "CONTROL_URL", "http://control.test")
    monkeypatch.setattr(auth, "internal_token", lambda: "test-token")

    def _serve(payload: dict):
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=0.0: _Resp(payload))
    return _serve


_VALID = {"valid": True, "tenant_id": "t_boxtest", "plan": "free",
          "caps": {}}


def test_matching_box_is_accepted(control, monkeypatch):
    monkeypatch.setattr(auth, "BOX_NAME", "dp1")
    control({**_VALID, "data_plane": "dp1"})
    assert auth._control_lookup("k")[0] == "boxtest"


def test_foreign_box_is_rejected(control, monkeypatch):
    monkeypatch.setattr(auth, "BOX_NAME", "dp1")
    control({**_VALID, "data_plane": "acme"})
    assert auth._control_lookup("k") is None, (
        "a key whose tenant lives on another box was validated here — the "
        "request will die later on a database this box does not hold")


def test_old_control_plane_without_data_plane_is_accepted(control, monkeypatch):
    # An upgraded box in front of a not-yet-upgraded control plane must not
    # lock every customer out.
    monkeypatch.setattr(auth, "BOX_NAME", "dp1")
    control(dict(_VALID))
    assert auth._control_lookup("k")[0] == "boxtest"


def test_unset_box_name_accepts_everything(control, monkeypatch):
    # Single-box deploys never set BRAIN_BOX_NAME; the guard must be inert.
    monkeypatch.setattr(auth, "BOX_NAME", "")
    control({**_VALID, "data_plane": "acme"})
    assert auth._control_lookup("k")[0] == "boxtest"
