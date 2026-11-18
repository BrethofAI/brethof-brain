"""API key → tenant (+ tier). Deliberately minimal for this phase.

SCOPE, STATED PLAINLY: keys live in an env file as `key:tenant[:tier]` lines.
There is no control-plane lookup, no plan, no caps, no per-table scopes yet.

TIER is the customer-protection boundary (founder, 2026-07-28): an `owner` key
sees the full toolset — raw ids, raw SQL, index surgery. A `customer` key sees
only the intent-level surface (save/search/read/delete) and never learns the
filing scheme: placement, id naming and index upkeep happen server-side behind
the write gate. Omitted tier = `owner`, which keeps every existing key file
valid unchanged.

What is NOT deferred, because deferring it is how tenants leak into each other:
a request that fails to resolve a key gets no tenant bound at all, and
`store.connection()` raises rather than falling back to anything ambient.

STATIC ENTRIES ARE SHA-256 DIGESTS, NOT KEYS (2026-08-25). The file is bind-
mounted from `/opt/brain/keys` and `brain-backup.sh` copies it into every
nightly borg archive, so a plaintext key there is a plaintext key in seven
daily, four weekly and three monthly backups — next to the repo passphrase on
the same box. A plaintext entry is still accepted (it is hashed on load, so no
deploy can lock the fleet out of its own doors) but it announces itself on
every boot until converted. `scripts/hash_owner_keys.py` does the conversion.

Lookup is a dict hit on the digest of the presented key: constant work, and
nothing to compare in non-constant time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

TIERS = ("owner", "customer")

# A static entry's first field is the SHA-256 hex digest of the key. Anything
# else is a plaintext key from before 2026-08-25 — accepted, hashed on load,
# and reported loudly by `_load`.
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# ── control-plane keys (panel-minted, 2026-07-28) ──────────────────────────
# Keys the account panel issues live in the control plane's database, not in
# the static file. v2 resolves them the way v1 did: ask the control plane
# (validate-key), cache the answer, honour revoke/refresh pushes. Every
# panel key is CUSTOMER tier by construction and carries project scopes —
# {"project": "rw"|"read"|"none", "*": default} — None = full access.
CONTROL_URL = os.environ.get("BRAIN_CONTROL_URL", "")
# This box's name in the control plane's box registry (multi-box,
# 2026-08-19). Unset = accept any tenant (single-box world).
BOX_NAME = os.environ.get("BRAIN_BOX_NAME", "").strip()
_TOKEN_FILE = os.environ.get("BRAIN_INTERNAL_TOKEN_FILE", "")
CACHE_TTL_S = float(os.environ.get("BRAIN_KEY_CACHE_TTL_S", "300"))
NEG_TTL_S = 30.0
# Bound the positive-cache entry count so a flood of distinct bogus keys can't
# grow it without limit (security audit 2026-08-08). Negatives already expire
# in 30s; the cap is a hard ceiling on top of TTL. FIFO eviction — the oldest
# inserted entry goes first; a still-hot key simply re-validates on its next
# miss (one control round-trip), which is exactly the cold-start cost.
CACHE_MAX = int(os.environ.get("BRAIN_KEY_CACHE_MAX", "4096"))

_said: set[str] = set()


def _say_once(tag: str, msg: str) -> None:
    """Loud, but once per process per condition — a config fault that repeats
    per request drowns the log it is trying to appear in."""
    if tag not in _said:
        _said.add(tag)
        print(msg, flush=True)


_cache: "OrderedDict[str, tuple[float, tuple | None]]" = OrderedDict()
_cache_lock = threading.Lock()


def internal_token() -> str:
    """The shared secret the control plane checks on /v1/internal/*.

    A missing or unreadable token file used to return "" in silence, and the
    caller then declined to ask the control plane AT ALL — so every
    panel-minted customer key came back "unknown or missing API key" while the
    control plane's log stayed empty and nothing anywhere said why. Measured
    2026-08-25. An empty token now says so, once, naming the path and the
    errno."""
    if _TOKEN_FILE:
        try:
            with open(_TOKEN_FILE, encoding="utf-8") as fh:
                tok = fh.read().strip()
            if not tok:
                _say_once("token-empty",
                          f"[auth] internal token file {_TOKEN_FILE} is EMPTY "
                          f"— every control-plane key will be refused")
            return tok
        except OSError as e:
            _say_once("token-unreadable",
                      f"[auth] CANNOT READ internal token {_TOKEN_FILE}: "
                      f"{e.__class__.__name__} {e} — every control-plane key "
                      f"will be refused until this is fixed")
            return ""
    return os.environ.get("BRAIN_INTERNAL_TOKEN", "")


def _control_lookup(api_key: str) -> tuple[str, str, dict | None] | None:
    """One validate-key round trip. Control plane down = deny (fail closed);
    the 5-min cache keeps a working key working through a control blip."""
    token = internal_token()
    if not CONTROL_URL or not token:
        # NEVER SILENT. This is the branch that turned a readable-config
        # problem into "unknown or missing API key" with no trace on either
        # plane — the data plane simply stopped asking.
        _say_once("no-control",
                  f"[auth] control lookup DISABLED: CONTROL_URL="
                  f"{'set' if CONTROL_URL else 'EMPTY'} token="
                  f"{'present' if token else 'EMPTY'} — every panel-minted "
                  f"key is refused while this holds")
        return None
    req = urllib.request.Request(
        CONTROL_URL.rstrip("/") + "/v1/internal/mind/validate-key",
        data=json.dumps({"key": api_key}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "brethof-brain-v2/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8.0) as r:
            d = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[auth] control lookup {type(e).__name__}: {str(e)[:120]}",
              flush=True)
        return None
    if not d.get("valid"):
        return None
    # Multi-box (2026-08-19): the control plane says which box the key's
    # tenant lives on. When THIS box knows its own name (BRAIN_BOX_NAME) and
    # they disagree, the key is aimed at the wrong endpoint — reject it here
    # with a clear line, instead of validating it and dying later on a
    # database this box does not hold. Either side unset = pre-multi-box
    # behaviour, accept (old control plane / single-box deploys).
    home = str(d.get("data_plane") or "").strip()
    if BOX_NAME and home and home != BOX_NAME:
        print(f"[auth] key belongs to data plane '{home}' but this box is "
              f"'{BOX_NAME}' — wrong endpoint, rejecting", flush=True)
        return None
    tenant = str(d.get("tenant_id") or "").strip()
    tenant = tenant[2:] if tenant.startswith("t_") else tenant
    if not tenant:
        return None
    scopes = (d.get("caps") or {}).get("scopes")
    if scopes is not None and not isinstance(scopes, dict):
        scopes = {}                        # corrupt caps FAIL CLOSED
    # The plan rides along for quota enforcement (2026-07-30). Missing plan
    # on a valid customer key is a control-plane bug — fail CLOSED to free
    # caps rather than exempting the tenant from enforcement entirely.
    plan = str(d.get("plan") or "free").strip().lower()
    # Purchased top-ups (Lemon Squeezy, 2026-08-20): pack_credits is the
    # LIFETIME purchased exchange-credit total (consumption is ledgered in
    # this tenant's own quota_usage.pack_used), extra_storage_mb widens the
    # storage cap. Absent fields (old control plane) = zero, which is the
    # fail-closed direction.
    ents = {"pack_credits": int(d.get("pack_credits") or 0),
            "extra_storage_mb": int(d.get("extra_storage_mb") or 0)}
    # SUBSCRIPTION STATE (2026-08-24, the billing tail): active | past_due |
    # frozen. The control plane has always sent it and the data plane threw
    # it away — so a lapsed account kept full service. Missing state on a
    # valid key means an old control plane: treat as active (the fail-OPEN
    # direction, because a billing-state bug must never lock out a paying
    # customer; the quota door still holds the usage line).
    state = str(d.get("state") or "active").strip().lower()
    return (tenant, "customer", scopes, plan, ents, state)


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def evict(kh: str) -> None:
    """Revoke/refresh push from the control plane — drop the cached answer."""
    with _cache_lock:
        _cache.pop(kh, None)


_plaintext_keys = 0
_plaintext_warned = False


SCOPE_MODES = ("rw", "read", "none")


def encode_scopes(scopes: dict) -> str:
    """A key-file field for project scopes: `s=<base64url JSON>` — no ':' or
    ',' inside, so the line's own splitting can never cut it."""
    raw = json.dumps(scopes, separators=(",", ":"), sort_keys=True).encode()
    return "s=" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_scopes(field: str) -> dict:
    """The scopes of an `s=` field. Anything unreadable or out of shape is
    DENY-EVERYTHING ({}), never full access — a damaged line must fail closed."""
    try:
        b = field[2:]
        d = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:                                        # noqa: BLE001
        return {}
    if not isinstance(d, dict) or not all(isinstance(k, str) and v in SCOPE_MODES for k, v in d.items()):
        return {}
    return d


def _load() -> dict[str, tuple]:
    """`BRAIN_V2_KEYS=<sha256>:tenant[:tier[:label][:s=<scopes>]],...`, or a
    file via BRAIN_V2_KEYS_FILE so the value never appears in `docker inspect`.

    Returns DIGEST -> (tenant, tier, scopes). SCOPED KEYS IN THE KEY FILE
    (2026-10-05): since every box is self-hosted (2026-09-02) a key is read
    only from this file, and a line with no `s=` field is full access (None);
    `s=` carries {"project": "rw"|"read"|"none", "*": default}, enforced by
    api/customer_tools exactly as panel keys were in July. A first field that is not a 64-char hex
    digest is a plaintext key: hashed here so it keeps working, counted, and
    announced once per process. It must not silently pass — that file rides
    into every nightly backup archive."""
    global _plaintext_keys, _plaintext_warned
    raw = os.environ.get("BRAIN_V2_KEYS", "")
    path = os.environ.get("BRAIN_V2_KEYS_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            pass
    out: dict[str, tuple[str, str]] = {}
    plaintext = 0
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item or ":" not in item or item.startswith("#"):
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        tier = parts[2].lower() if len(parts) > 2 and parts[2] else "owner"
        if tier not in TIERS:
            # An unknown tier must fail CLOSED to the restricted surface — a
            # typo in a key file must never hand a customer the owner tools.
            tier = "customer"
        ident = parts[0]
        if _HASH_RE.match(ident):
            digest = ident
        else:
            digest = key_hash(ident)
            plaintext += 1
        scopes = None
        for extra in parts[3:]:
            if extra.startswith("s="):
                scopes = decode_scopes(extra)
        out[digest] = (parts[1], tier, scopes)
    _plaintext_keys = plaintext
    if plaintext and not _plaintext_warned:
        _plaintext_warned = True
        print(f"[auth] SECURITY: {plaintext} static key(s) stored in PLAINTEXT "
              f"in {path or 'BRAIN_V2_KEYS'}. They work, but that file is "
              f"copied into every nightly borg archive. Convert in place with "
              f"scripts/hash_owner_keys.py — the keys themselves do not "
              f"change and nothing needs reissuing.", flush=True)
    return out


_KEYS = _load()
_KEYS_STAMP: tuple | None = None


def _keys_file_stamp() -> tuple | None:
    path = os.environ.get("BRAIN_V2_KEYS_FILE", "")
    if not path:
        return None
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def _fresh_keys() -> dict:
    """THE KEYS FILE IS READ AGAIN WHEN IT CHANGES (2026-10-03): the host
    mints and revokes keys in a running tenant's file (device login), and a
    key read only at process start would wait for a restart. One stat per
    lookup; a file that vanished keeps the keys already loaded."""
    global _KEYS, _KEYS_STAMP
    stamp = _keys_file_stamp()
    if stamp is not None and stamp != _KEYS_STAMP:
        _KEYS, _KEYS_STAMP = _load(), stamp
    return _KEYS


_KEYS_STAMP = _keys_file_stamp()


def _lookup(api_key: str) -> tuple | None:
    """(tenant, tier, scopes, plan, entitlements). Static file first — those
    are the owner keys and must keep working with the control plane down —
    then the cached control-plane path for panel-minted customer keys.
    plan=None means EXEMPT from quota (static keys only, by construction);
    entitlements=None likewise."""
    if not api_key:
        return None
    kh = key_hash(api_key)
    bound = _fresh_keys().get(kh)
    if bound is not None:
        return (bound[0], bound[1], bound[2] if len(bound) > 2 else None, None, None)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(kh)
        if hit and hit[0] > now:
            return hit[1]
    res = _control_lookup(api_key)
    with _cache_lock:
        _cache.pop(kh, None)          # move-to-end on refresh (FIFO by insert)
        _cache[kh] = (now + (CACHE_TTL_S if res else NEG_TTL_S), res)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)   # evict oldest inserted
    return res


def resolve(api_key: str) -> str | None:
    """Tenant id for a key, or None. Never logs or returns the key."""
    bound = _lookup(api_key)
    return bound[0] if bound else None


def resolve_tier(api_key: str) -> str | None:
    """'owner' | 'customer' for a known key, None for an unknown one."""
    bound = _lookup(api_key)
    return bound[1] if bound else None


def resolve_scopes(api_key: str) -> dict | None:
    """Project scopes for a key — None means full access (static keys and
    full-access panel keys). {} means deny-everything (corrupt caps)."""
    bound = _lookup(api_key)
    return bound[2] if bound else None


def resolve_plan(api_key: str) -> str | None:
    """The key's billing plan, or None for quota-exempt static keys."""
    bound = _lookup(api_key)
    return bound[3] if bound else None


def resolve_entitlements(api_key: str) -> dict | None:
    """Purchased top-ups riding on the key: {"pack_credits", "extra_storage_mb"}.
    None for static keys (quota-exempt) and unknown keys. Tolerates cached
    4-tuples from a pre-2026-08-20 deploy living out their TTL."""
    bound = _lookup(api_key)
    if not bound or len(bound) < 5:
        return None
    return bound[4]


def resolve_state(api_key: str) -> str | None:
    """The key's SUBSCRIPTION state: 'active' | 'past_due' | 'frozen'.
    None for static keys (billing-exempt) and unknown keys. Tolerates
    cached 5-tuples from a pre-2026-08-24 deploy living out their TTL —
    those read as active, the fail-open direction."""
    bound = _lookup(api_key)
    if not bound or len(bound) < 6:
        return None
    return bound[5]


def tenants() -> list[str]:
    """Sorted unique tenant ids — what the background workers iterate."""
    return sorted({v[0] for v in _load().values()})


def key_count() -> int:
    """For /health — proves keys loaded without revealing any of them."""
    return len(_fresh_keys())


def plaintext_key_count() -> int:
    """Static keys still stored as plaintext rather than a digest. For
    /health, so the sentinel can see the condition instead of a human
    having to notice one boot line. Zero is the only acceptable value."""
    return _plaintext_keys
