"""THE FRONT DOOR (2026-11-17, the local-first redesign).

The hub is the ONLY thing a device talks to. Every agent, harness and cloud
connection sends its own device key here; the gate (the closed licence and
device module) says whether that key may reach which memory and where it
lives, and the request is forwarded to that storage with the storage's
internal key. Storages and the embedder sit on the hub's private network and
are never published.

    device ──bearer device key──▶ front door :8600 ──storage key──▶ storage
                                     │ gate.admit()
    owner  ──admin token──▶ /settings, /admin/api/*

Paths:
  /pair/start, /pair/poll   a device asks to be let in; the owner approves it
                            by its code on the settings page
  /settings                 the settings page (devices, memories, models,
                            licence); /admin/api/* is its API
  /m/<memory>/<path>        forwarded to that memory
  /<path>                   forwarded to the device's first memory
  /health                   open

The internal hub (server.py: the storages' pipes, rules, curation) listens on
its own port on the private network only.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import gate
from hub import models, store

front = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# cloud agents' sign-in (hub/oauth.py) — registered before the catch-all
# forward, which would otherwise take /.well-known and /oauth too
from hub import oauth as _oauth  # noqa: E402
front.include_router(_oauth.router)
# apps on their own subscription (hub/modeldoor.py) — before the catch-all too
from hub import modeldoor as _modeldoor  # noqa: E402
front.include_router(_modeldoor.router)

ADMIN_TOKEN_FILE = os.environ.get("HUB_ADMIN_TOKEN_FILE", "/srv/hub/admin-token")
HUB_NAME = os.environ.get("HUB_NAME", "") or os.uname().nodename
COOKIE = "hub_admin"
HOP = {"host", "authorization", "connection", "keep-alive", "proxy-authenticate",
       "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "content-length"}
# never forwarded either way: the settings cookie IS the admin password, and
# a memory must not set cookies on the hub's own origin (found 2026-11-18)
HOP_IN = HOP | {"cookie"}
HOP_OUT = HOP | {"set-cookie"}
_client: httpx.AsyncClient | None = None
_pair_bucket: dict[str, list] = {}


def log(msg: str) -> None:
    print(f"[front] {msg}", flush=True)


# ── boot ─────────────────────────────────────────────────────────────────
async def ensure_tenant(name: str, key: str) -> None:
    """A storage is a tenant of the internal hub: it dials the pipe with the
    same key the front door forwards with. Internal tier = never capped."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    await store.execute(
        "INSERT INTO tenants (id, name, key_hash, plan, tier, active) VALUES (?, ?, ?, 'selfhosted', 'internal', 1)"
        " ON CONFLICT (id) DO UPDATE SET key_hash = excluded.key_hash, active = 1",
        ("t_" + name, name, digest))


async def boot() -> None:
    """Called once by the local runner, after the internal hub's schema."""
    global _client
    gate.init()
    await models.load()
    if not models.get("_admin_hash"):
        tok = "ha_" + secrets.token_urlsafe(24)
        await models.set_many({"_admin_hash": hashlib.sha256(tok.encode()).hexdigest()})
        try:
            fd = os.open(ADMIN_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(tok + "\n")
            log(f"settings page password written to {ADMIN_TOKEN_FILE}")
        except OSError as e:
            log(f"ERROR: cannot write the settings page password ({e}) — it is lost; "
                f"reset it with: python -m hub.local reset-password")
    # THE FIRST MEMORY: the installer names one storage and its key in the
    # environment, so a one-machine install works without a settings visit.
    name, url, key = (os.environ.get("HUB_BOOT_STORAGE", ""), os.environ.get("HUB_BOOT_STORAGE_URL", ""),
                      os.environ.get("HUB_BOOT_STORAGE_KEY", ""))
    if name and url and key:
        if not any(s["name"] == name for s in gate.storages()):
            gate.add_storage(name, url, key=key)
            log(f"memory '{name}' registered at {url}")
        await ensure_tenant(name, key)
    _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=120, pool=30))
    asyncio.create_task(_renewer())
    _modeldoor.boot(_client)
    asyncio.create_task(_modeldoor.sweeper())


async def _renewer() -> None:
    while True:
        try:
            if gate.renew_due():
                lic = await asyncio.to_thread(gate.renew)
                log(f"licence renewed: {lic['state']}, {lic['in_use']}/{lic['devices']} devices")
        except Exception as e:                                      # noqa: BLE001
            log(f"licence renewal failed ({e}) — existing devices are unaffected; will retry")
        await asyncio.sleep(1800)


# ── admin auth ───────────────────────────────────────────────────────────
# THE LOGIN IS THE OWNER'S TO SWITCH OFF (founder 2026-11-19): it guards the
# page from the outside world only; the owner always has the container itself
# (python -m hub.local reset-password). With it off, the page is still not
# open to websites the owner happens to visit: a request is taken only when
# its Host is this machine or a private address (or listed in
# HUB_ADMIN_HOSTS), which stops DNS rebinding, and a POST only as JSON from
# that same origin, which a cross-site form cannot send.
ADMIN_HOSTS = {h.strip().lower() for h in os.environ.get("HUB_ADMIN_HOSTS", "").split(",") if h.strip()}


def _login_off() -> bool:
    return models.get("_admin_login") == "off"


def _host_of(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _own_host(host: str) -> bool:
    import ipaddress
    if host in ("localhost",) or host in ADMIN_HOSTS or host.endswith((".local", ".lan", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _same_site(request: Request) -> bool:
    host = _host_of(request.headers.get("host", ""))
    if not _own_host(host):
        return False
    if request.method in ("GET", "HEAD"):
        return True
    if not (request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"):
        return False
    origin = request.headers.get("origin", "")
    if origin:
        from urllib.parse import urlparse
        if _host_of(urlparse(origin).netloc) != host:
            return False
    return True


def _is_admin(request: Request) -> bool:
    if _login_off() and _same_site(request):
        return True
    want = models.get("_admin_hash")
    if not want:
        return False
    auth = request.headers.get("authorization", "")
    tok = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
    return bool(tok) and hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), want)


async def _body(request: Request) -> dict:
    """The request's JSON object; anything else (not JSON, an array, empty)
    is ValueError, which every caller answers as a 400 — never a 500."""
    try:
        b = json.loads(await request.body() or b"{}")
    except ValueError:
        raise ValueError("the body must be a JSON object") from None
    if not isinstance(b, dict):
        raise ValueError("the body must be a JSON object")
    return b


def _deny() -> JSONResponse:
    return JSONResponse({"ok": False, "why": "settings password required"}, status_code=401)


def _err(e: Exception) -> JSONResponse:
    code = 503
    for t, c in ((PermissionError, 403), (LookupError, 404), (ValueError, 400)):
        if isinstance(e, t):
            code = c
            break
    return JSONResponse({"ok": False, "why": str(e)}, status_code=code)


@front.post("/admin/login")
async def admin_login(request: Request) -> Response:
    try:
        body = await _body(request)
    except ValueError as e:
        return _err(e)
    tok = str(body.get("password") or "").strip()
    want = models.get("_admin_hash")
    if not (tok and want and hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), want)):
        await asyncio.sleep(1)
        return JSONResponse({"ok": False, "why": "wrong password"}, status_code=401)
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, tok, httponly=True, samesite="strict",
                 secure=request.url.scheme == "https", max_age=30 * 86400)
    return r


@front.post("/admin/logout")
async def admin_logout() -> Response:
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@front.get("/admin/api/state")
async def admin_state(request: Request) -> Response:
    if not _is_admin(request):
        return _deny()
    return JSONResponse({"ok": True, "hub": {"id": gate.hub_id(), "name": HUB_NAME},
                         "licence": gate.licence(), "devices": gate.devices(),
                         "pending": gate.pending(), "memories": gate.storages(),
                         "models": models.public(), "login": "off" if _login_off() else "on"})


@front.post("/admin/api/password")
async def admin_password(request: Request) -> Response:
    """A settings password the owner chooses (replaces the installer's)."""
    if not _is_admin(request):
        return _deny()
    try:
        body = await _body(request)
    except ValueError as e:
        return _err(e)
    pw = str(body.get("password") or "")
    if len(pw) < 12:
        return JSONResponse({"ok": False, "why": "use at least 12 characters"}, status_code=400)
    await models.set_many({"_admin_hash": hashlib.sha256(pw.encode()).hexdigest()})
    # the installer's password file must not go on claiming to be the password
    try:
        fd = os.open(ADMIN_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("# the owner set their own password on the settings page; to reset it: "
                     "python -m hub.local reset-password\n")
    except OSError as e:
        log(f"settings password changed, but {ADMIN_TOKEN_FILE} could not be cleared: {e}")
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, pw, httponly=True, samesite="strict",
                 secure=request.url.scheme == "https", max_age=30 * 86400)
    return r


@front.post("/admin/api/login")
async def admin_login_switch(request: Request) -> Response:
    """On or off. Off is the owner's call for a page only they can reach, or
    one they protect themselves; the container is always the way back in."""
    if not _is_admin(request):
        return _deny()
    try:
        body = await _body(request)
    except ValueError as e:
        return _err(e)
    on = bool(body.get("on"))
    if on and not models.get("_admin_hash"):
        return JSONResponse({"ok": False, "why": "set a password first"}, status_code=400)
    await models.set_many({"_admin_login": "on" if on else "off"})
    return JSONResponse({"ok": True, "login": "on" if on else "off"})


async def _admin_call(request: Request, fn):
    if not _is_admin(request):
        return _deny()
    try:
        body = await _body(request)
        out = fn(body)
        if asyncio.iscoroutine(out):
            out = await out
        return JSONResponse({"ok": True, **(out if isinstance(out, dict) else {"result": out})})
    except Exception as e:                                          # noqa: BLE001
        return _err(e)


@front.post("/admin/api/approve")
async def admin_approve(request: Request) -> Response:
    return await _admin_call(request, lambda b: gate.approve(str(b.get("pair", "")), b.get("memories") or None))


@front.post("/admin/api/deny")
async def admin_deny(request: Request) -> Response:
    return await _admin_call(request, lambda b: {"denied": gate.deny(str(b.get("pair", "")))})


@front.post("/admin/api/device/remove")
async def admin_device_remove(request: Request) -> Response:
    return await _admin_call(request, lambda b: {"removed": gate.remove_device(str(b.get("id", "")))})


@front.post("/admin/api/device/memories")
async def admin_device_memories(request: Request) -> Response:
    return await _admin_call(request, lambda b: {"set": gate.set_memories(str(b.get("id", "")),
                                                                         list(b.get("memories") or []))})


@front.post("/admin/api/device/rename")
async def admin_device_rename(request: Request) -> Response:
    return await _admin_call(request, lambda b: {"renamed": gate.rename_device(str(b.get("id", "")),
                                                                              str(b.get("name", "")))})


@front.post("/admin/api/memory/add")
async def admin_memory_add(request: Request) -> Response:
    async def add(b):
        name = str(b.get("name", "")).strip().lower()
        key = gate.add_storage(name, str(b.get("url", "")))
        await ensure_tenant(name, key)
        return {"name": name, "key": key,
                "note": "put this key in that memory's settings as BRAIN_HUB_KEY; it is shown once"}
    return await _admin_call(request, add)


@front.post("/admin/api/memory/remove")
async def admin_memory_remove(request: Request) -> Response:
    async def rm(b):
        name = str(b.get("name", ""))
        await store.execute("UPDATE tenants SET active = 0, revoked_at = datetime('now') WHERE id = ?",
                            ("t_" + name,))
        return {"removed": gate.remove_storage(name)}
    return await _admin_call(request, rm)


@front.post("/admin/api/models")
async def admin_models(request: Request) -> Response:
    async def put(b):
        await models.set_many({k: b[k] for k in models.KEYS if k in b})
        return {"models": models.public()}
    return await _admin_call(request, put)


_SVC = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@front.get("/admin/api/apps")
async def admin_apps(request: Request) -> Response:
    if not _is_admin(request):
        return _deny()
    own = {k.split(":", 1)[1]: v for k, v in models.all_settings().items() if k.startswith("upstream:")}
    svcs = {**_modeldoor.UPSTREAMS, **own}
    return JSONResponse({"ok": True, "services": [{"name": k, "url": v} for k, v in sorted(svcs.items())]})


@front.post("/admin/api/app/add")
async def admin_app_add(request: Request) -> Response:
    """An app on its own plan becomes a device; its model address carries
    that device's key (shown once, like any device key)."""
    def add(b):
        name = str(b.get("name") or "app").strip()[:60] or "app"
        svc = str(b.get("service") or "")
        if not _modeldoor.upstream_of(svc):
            raise ValueError(f"no model service called '{svc}'")
        p = gate.pair_start("app:" + secrets.token_hex(8), name, str(b.get("memory") or ""))
        gate.approve(p["pair"], [str(b["memory"])] if b.get("memory") else None)
        got = gate.pair_poll(p["pair"], p["secret"])
        if not got.get("key"):
            raise ValueError("the app could not be approved: " + str(got.get("why") or got.get("state")))
        return {"path": f"/model/{got['key']}/{svc}/v1"}
    return await _admin_call(request, add)


@front.post("/admin/api/upstream/set")
async def admin_upstream_set(request: Request) -> Response:
    async def put(b):
        name, url = str(b.get("name") or "").strip().lower(), str(b.get("url") or "").strip().rstrip("/")
        if not _SVC.match(name):
            raise ValueError("a service name is lowercase letters, digits and dashes")
        if not url.startswith(("https://", "http://")):
            raise ValueError("the address starts with https:// (or http:// on your own network)")
        await models.set_many({"upstream:" + name: url})
        return {}
    return await _admin_call(request, put)


@front.post("/admin/api/models/test")
async def admin_models_test(request: Request) -> Response:
    async def test(_b):
        from hub import jev, server
        t0 = time.time()
        ai = await server.infer({"id": "_test", "name": "test"},
                                {"prompt": "Answer with the single word OK.", "max_tokens": 20})
        out = {"ai": {"ok": bool(ai.get("ok")), "why": ai.get("why", ""),
                      "answer": (ai.get("text") or "")[:40], "seconds": round(time.time() - t0, 2)}}
        if models.decision():
            t1 = time.time()
            ps, _ = await jev.noul({"sky": "The sky is blue today."},
                                   {"q": ("Is the sky described as blue?",
                                          {"true": "the state says the sky is blue",
                                           "false": "it does not"})})
            out["decision"] = {"ok": ps is not None, "p_true": (ps or {}).get("q"),
                               "seconds": round(time.time() - t1, 2)}
        return out
    return await _admin_call(request, test)


@front.post("/admin/api/activate/start")
async def admin_activate_start(request: Request) -> Response:
    return await _admin_call(request, lambda b: asyncio.to_thread(gate.activate_start,
                                                                 str(b.get("name") or HUB_NAME)))


@front.post("/admin/api/activate/poll")
async def admin_activate_poll(request: Request) -> Response:
    async def poll(_b):
        st = await asyncio.to_thread(gate.activate_poll)
        return {"status": st, "licence": gate.licence()}
    return await _admin_call(request, poll)


@front.post("/admin/api/renew")
async def admin_renew(request: Request) -> Response:
    return await _admin_call(request, lambda _b: asyncio.to_thread(gate.renew))


# ── pairing (open: a device cannot have a key before it is approved) ─────
def _pair_ok(ip: str) -> bool:
    now = time.time()
    b = _pair_bucket.setdefault(ip, [5.0, now])
    b[0] = min(5.0, b[0] + (now - b[1]) / 60.0)
    b[1] = now
    if b[0] < 1:
        return False
    b[0] -= 1
    return True


@front.post("/pair/start")
async def pair_start(request: Request) -> Response:
    ip = request.client.host if request.client else "?"
    if not _pair_ok(ip):
        return JSONResponse({"ok": False, "why": "too many pairing requests — wait a minute"}, status_code=429)
    try:
        b = await _body(request)
        got = gate.pair_start(str(b.get("client_id", "")), str(b.get("name", "")), str(b.get("memory", "")))
    except Exception as e:                                          # noqa: BLE001
        return _err(e)
    log(f"device '{b.get('name', '')}' asks to pair — code {got['code']}")
    return JSONResponse({"ok": True, **got,
                         "approve_at": str(request.base_url).rstrip("/") + "/settings"})


@front.post("/pair/poll")
async def pair_poll(request: Request) -> Response:
    try:
        b = await _body(request)
    except ValueError as e:
        return _err(e)
    return JSONResponse({"ok": True, **gate.pair_poll(str(b.get("pair", "")), str(b.get("secret", "")))})


@front.get("/health")
async def health() -> dict:
    return {"ok": True, "hub": gate.hub_id(), "licence": gate.licence()["state"],
            "memories": len(gate.storages())}


@front.get("/settings")
async def settings_page() -> Response:
    from hub.settings_page import PAGE
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store",
                                       "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline';"
                                                                  " script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})


# ── the forward ──────────────────────────────────────────────────────────
@front.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def forward(path: str, request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    memory, rest = "", path
    if path.startswith("m/"):
        parts = path.split("/", 2)
        memory, rest = parts[1], (parts[2] if len(parts) > 2 else "")
    a = gate.admit(key, memory) if key else None
    if a is None:
        why = ("this device is not approved on this hub — pair it first"
               if not key or gate.authorize(key) is None
               else f"this device may not use the memory '{memory}'")
        # THE POINTER CLOUD AGENTS FOLLOW (MCP authorization spec): where
        # this resource's sign-in lives — the hub itself (hub/oauth.py)
        meta = str(request.base_url).rstrip("/") + "/.well-known/oauth-protected-resource" + (
            "/" + path if path else "")
        return JSONResponse({"ok": False, "why": why}, status_code=401 if "pair" in why else 403,
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_IN}
    headers["authorization"] = "Bearer " + a["storage_key"]
    headers["x-brain-device"] = a["device"]
    url = a["url"] + "/" + rest + (("?" + request.url.query) if request.url.query else "")
    body = await request.body()
    assert _client is not None
    try:
        r = await _client.send(_client.build_request(request.method, url, headers=headers, content=body),
                               stream=True)
    except httpx.HTTPError as e:
        log(f"memory '{a['memory']}' unreachable at {a['url']}: {type(e).__name__}")
        return JSONResponse({"ok": False, "why": f"memory '{a['memory']}' is not answering"}, status_code=502)
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_OUT}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out_headers,
                             background=BackgroundTask(r.aclose))
