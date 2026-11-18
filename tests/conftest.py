"""Offline-suite environment. Every box is self-hosted since 2026-09-02, and a
self-hosted box refuses to boot without a key file — so the suite gets one
(a syntactically valid owner key naming the offline test tenant) before any
api module is imported. Tests that need a different shape set their own env."""
import os

os.environ.setdefault(
    "BRAIN_V2_KEYS",
    "0000000000000000000000000000000000000000000000000000000000000000:t_offline_test")
os.environ.pop("BRAIN_CONTROL_URL", None)

# The gate ships compiled (gate/<arch>/); put this machine's build on the path.
import platform, sys
_arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), platform.machine())
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gate", _arch))
