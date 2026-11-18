"""Which shape of the product this process is: our cloud, or a customer's box.

ONE SYSTEM, TWO DEPLOYMENTS. There is no second codebase and no second MCP
server. The same app serves both; what changes is where it is running and
therefore what it is allowed to do. A customer points their agent at their own
container's /v1/mcp exactly as a cloud customer points at ours — same tools,
same contract, different URL. The mode is OURS, set inside the image; the
customer never sets a flag and cannot get it wrong.

WHAT SELF-HOSTED TAKES AWAY, and why it is removal rather than refusal. The
control surface (/v1/internal/*, /v1/admin/*, /v1/fleet) provisions tenants,
deprovisions them, revokes keys, purges and redacts. On a box we do not
operate that surface is at best dead weight and at worst a second door onto
the customer's own memory. So the routes are DELETED from the router at boot,
not guarded — a 404 is the honest answer to "does this exist here", and a
guard is one bad `if` away from being no guard at all.

WHAT IT KEEPS. Everything the memory actually is: recall, the ambient hooks,
archive, export, health, and the MCP endpoint. Per the closing architecture
([[brain:local_recall_archive_split_2026_08_27]]) those paths are mechanical —
embed, vector+BM25+RRF, write — and must never depend on us being reachable.

WHAT IS NOT WIRED YET, stated here rather than discovered in production:
curation and the ledger need a prompt, and self-hosted has no
inference key by design; they go through the hub. Until that backend exists
those workers DO NOT START and this module says so on every boot. A worker
that starts and silently fails every job is how v1 lost embeddings for months.
"""
from __future__ import annotations

import os

SELFHOSTED = "selfhosted"
# ONE MODE (founder 2026-09-02). The cloud plane — a box that held many
# tenants, ran the judge, metered and archived them itself — died with
# api.brethof.cloud; billing lives in the control plane, lifecycle in
# brain-host, metering on the hub. Every box is a self-hosted box now, ours
# included. BRAIN_MODE is accepted for old env files; "cloud" is refused.
MODE = (os.environ.get("BRAIN_MODE") or SELFHOSTED).strip().lower()
if MODE != SELFHOSTED:
    raise SystemExit(
        f"BRAIN_MODE={MODE!r}: the cloud mode was deleted on 2026-09-02; "
        f"every box is '{SELFHOSTED}'. Remove BRAIN_MODE or set it so.")

# Paths that exist ONLY where we run the control plane.
_CONTROL_PREFIXES = ("/v1/internal", "/v1/admin", "/v1/fleet")

# Workers that need a prompt, and therefore an inference backend.
# curate_queue is the PRUNE lane now (2026-09-02): cleanup_history's delete +
# VACUUM, no model anywhere in it. Classed as inference until then, it never
# started on a self-hosted box — every confirmed cleanup queued a job nobody
# ran. briefs (the teach lane) is deleted.
_INFERENCE_WORKERS = ("judge",)


def is_selfhosted() -> bool:
    """Always true since 2026-09-02 — kept as a name so the call sites read
    as what they check (a box, not the dead cloud plane)."""
    return True


def validate() -> None:
    """Fail at boot, loudly, rather than half-work in production.

    Self-hosted authenticates against a static key file (the same owner-key
    mechanism the cloud uses for its own keys) because there is no control
    plane to ask. A box configured with neither cannot authenticate anyone,
    and one configured to phone home is not self-hosted at all."""
    if not is_selfhosted():
        return
    if not (os.environ.get("BRAIN_V2_KEYS_FILE")
            or os.environ.get("BRAIN_V2_KEYS")):
        raise SystemExit(
            "BRAIN_MODE=selfhosted needs BRAIN_V2_KEYS_FILE (or BRAIN_V2_KEYS): "
            "there is no control plane to validate keys against, so the box "
            "would refuse every request.")
    if os.environ.get("BRAIN_CONTROL_URL"):
        raise SystemExit(
            "BRAIN_MODE=selfhosted with BRAIN_CONTROL_URL set: a self-hosted "
            "box must not call home to validate keys. Unset one of them.")


def selfhosted_tenant() -> str:
    """The ONE tenant this box serves, read from the static key file.

    Self-hosted is single-tenant by construction: the customer's own memory.
    Taking the id from the key file rather than a separate env var means there
    is exactly one place to get it wrong, and a key that authenticates always
    names a database that exists."""
    raw = os.environ.get("BRAIN_V2_KEYS", "")
    path = os.environ.get("BRAIN_V2_KEYS_FILE", "")
    if path and not raw:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    raise SystemExit(
        "BRAIN_MODE=selfhosted: no usable entry in the key file. Run "
        "deploy/selfhosted/mint-key.sh to create one.")


def runs_worker(name: str) -> bool:
    return name not in _INFERENCE_WORKERS


def panel_ops() -> bool:
    """A stack on OUR container host keeps the /v1/internal/* door so the
    account panel can redact, delete a project, export and erase — the
    GDPR surface (founder 2026-08-31: 'that's a legal requirement'). The
    door only answers while the customer has unlocked the memory with
    their passphrase, and only via the host's admin bridge with the
    per-stack internal token. A true SELF-hosted box never sets this:
    there, the customer owns psql itself and needs no door of ours."""
    return is_selfhosted() and os.environ.get("BRAIN_PANEL_OPS", "") == "1"


def strip_control_plane(app) -> int:
    """Remove the control-plane routes. Returns how many went."""
    keep_internal = panel_ops()
    before = len(app.router.routes)
    app.router.routes = [
        r for r in app.router.routes
        if not (str(getattr(r, "path", "")).startswith(_CONTROL_PREFIXES)
                and not (keep_internal and str(getattr(r, "path", ""))
                         .startswith("/v1/internal/")))]
    return before - len(app.router.routes)


def announce(app) -> None:
    """One block at boot saying exactly what this process is and is not."""
    validate()
    gone = strip_control_plane(app)
    kept = " (/v1/internal KEPT — hosted stack, panel ops on)" if panel_ops() else ""
    print(f"[mode] SELF-HOSTED — {gone} control-plane routes removed"
          f"{kept}", flush=True)
    print(f"[mode] not started (no inference key here, by design): "
          f"{', '.join(_INFERENCE_WORKERS)}. Every judgement is the hub's, "
          f"asked over the pipe this box holds open.", flush=True)
    print("[mode] active: recall, ambient hooks, archive, export, MCP",
          flush=True)
