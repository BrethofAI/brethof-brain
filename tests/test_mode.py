"""The self-hosted cut, asserted rather than trusted.

These tests exist because the failure they guard against is silent: a control
route left mounted on a customer's box looks exactly like a working box until
someone finds it."""
import importlib
import os

import pytest


def _mode(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import core.mode
    return importlib.reload(core.mode)


def test_defaults_to_selfhosted_and_only_that(monkeypatch):
    """One mode since 2026-09-02: every box is a self-hosted box."""
    m = _mode(monkeypatch, BRAIN_MODE=None)
    assert m.is_selfhosted()
    assert m.runs_worker("curate_queue") and m.runs_worker("drainer")
    assert not m.runs_worker("judge")          # no inference key on a box


def test_cloud_mode_is_refused_loudly(monkeypatch):
    with pytest.raises(SystemExit):
        _mode(monkeypatch, BRAIN_MODE="cloud")


def test_selfhosted_refuses_without_a_local_key_file(monkeypatch):
    m = _mode(monkeypatch, BRAIN_MODE="selfhosted",
              BRAIN_V2_KEYS_FILE=None, BRAIN_V2_KEYS=None,
              BRAIN_CONTROL_URL=None)
    with pytest.raises(SystemExit):
        m.validate()


def test_selfhosted_refuses_to_phone_home(monkeypatch):
    m = _mode(monkeypatch, BRAIN_MODE="selfhosted",
              BRAIN_V2_KEYS_FILE="/run/keys/v2keys",
              BRAIN_CONTROL_URL="https://brethof.ai")
    with pytest.raises(SystemExit):
        m.validate()


def test_unknown_mode_is_not_silently_cloud(monkeypatch):
    with pytest.raises(SystemExit):
        _mode(monkeypatch, BRAIN_MODE="local")


def test_selfhosted_skips_inference_and_billing_workers(monkeypatch):
    m = _mode(monkeypatch, BRAIN_MODE="selfhosted",
              BRAIN_V2_KEYS_FILE="/run/keys/v2keys", BRAIN_CONTROL_URL=None)
    m.validate()
    assert m.runs_worker("drainer")
    # the prune lane runs everywhere: cleanup_history is the customer's own
    # tool and its queue must drain on their box (2026-09-02)
    assert m.runs_worker("curate_queue")
    assert not m.runs_worker("judge")          # the one inference worker


def test_control_routes_are_removed_not_guarded(monkeypatch):
    from fastapi import FastAPI
    m = _mode(monkeypatch, BRAIN_MODE="selfhosted",
              BRAIN_V2_KEYS_FILE="/run/keys/v2keys", BRAIN_CONTROL_URL=None)
    app = FastAPI()
    for p in ("/v1/health", "/v1/recall", "/v1/mcp", "/v1/hooks/stop",
              "/v1/export", "/v1/internal/provision", "/v1/internal/revoke",
              "/v1/admin/stats", "/v1/fleet"):
        app.get(p)(lambda: None)
    assert m.strip_control_plane(app) == 4
    left = {r.path for r in app.router.routes if hasattr(r, "path")}
    assert "/v1/internal/provision" not in left
    assert "/v1/admin/stats" not in left and "/v1/fleet" not in left
    # the memory itself survives intact
    for keep in ("/v1/health", "/v1/recall", "/v1/mcp", "/v1/hooks/stop",
                 "/v1/export"):
        assert keep in left


