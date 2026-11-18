"""CLOUD AGENTS SIGN IN TO THE HUB (2026-11-17, the local-first brain).

claude.ai, Grok and the agents behind them reach a memory as a remote MCP
connector, and speak only the MCP authorization spec (OAuth 2.1 + PKCE): on
a 401 they look for the server's metadata and send their human to approve.
On a local hub, the hub IS the authorization server — nothing goes through
Brethof. Approving is the same act as pairing a device: the owner gives the
settings password on the approve page, the connector becomes a device (one
seat of the licence), and the token it receives is that device's own key.
Removing the device on the settings page revokes the connector.

  GET  /.well-known/oauth-protected-resource[/<path>]   where to sign in
  GET  /.well-known/oauth-authorization-server          the endpoints
  POST /oauth/register     RFC 7591 dynamic registration (public clients)
  GET  /oauth/authorize    the approve page
  POST /oauth/approve      the owner's answer -> redirect with a code
  POST /oauth/token        code + PKCE verifier -> the device key

Codes and requests live in memory for five minutes: a hub restart in that
window costs one more click, never a stray key. A code is good once.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import gate
from hub import models, store

router = APIRouter()
TTL_S = 300
_requests: dict[str, dict] = {}
_codes: dict[str, dict] = {}

_DDL = """CREATE TABLE IF NOT EXISTS oauth_client (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL, created REAL NOT NULL)"""


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _prune() -> None:
    now = time.time()
    for d in (_requests, _codes):
        for k in [k for k, v in d.items() if now - v["at"] > TTL_S]:
            d.pop(k, None)


def _ensure() -> None:
    store.connect().execute(_DDL)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource(request: Request, path: str = "") -> Response:
    base = _base(request)
    return JSONResponse({"resource": base + ("/" + path if path else ""),
                         "authorization_servers": [base], "bearer_methods_supported": ["header"]})


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request) -> Response:
    base = _base(request)
    return JSONResponse({"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                         "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register",
                         "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
                         "code_challenge_methods_supported": ["S256"],
                         "token_endpoint_auth_methods_supported": ["none"],
                         "client_id_metadata_document_supported": True})


def _ok_redirect(uri: str) -> bool:
    p = urllib.parse.urlparse(uri)
    if p.scheme == "https" and p.netloc:
        return True
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


@router.post("/oauth/register")
async def register(request: Request) -> Response:
    _ensure()
    try:
        b = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if not isinstance(b, dict):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = [str(u) for u in (b.get("redirect_uris") or [])][:10]
    if not uris or not all(_ok_redirect(u) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    name = str(b.get("client_name") or "connector")[:80]
    cid = "oc_" + secrets.token_urlsafe(16)
    store.connect().execute("INSERT INTO oauth_client (id, name, redirect_uris, created) VALUES (?,?,?,?)",
                            (cid, name, json.dumps(uris), time.time()))
    return JSONResponse({"client_id": cid, "client_name": name, "redirect_uris": uris,
                         "token_endpoint_auth_method": "none",
                         "grant_types": ["authorization_code"], "response_types": ["code"]}, status_code=201)


CLIENT_DOC_MAX = 64 * 1024


async def _public_host(url: str) -> bool:
    """A client id URL is fetched only when its host is on the public
    internet: anyone can type a client id, and the hub must not be made to
    call the owner's own network (found 2026-11-18). A name that later
    resolves elsewhere (DNS rebinding) is not covered; the fetch is blind —
    nothing it reads goes back unless the document names itself."""
    import asyncio
    import ipaddress
    import socket
    host = urllib.parse.urlparse(url).hostname or ""
    if not host:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return bool(infos)


async def _client(cid: str) -> dict | None:
    """A registered client, or a client id that is an https URL to its own
    metadata document (how claude.ai identifies itself)."""
    _ensure()
    row = store.connect().execute("SELECT name, redirect_uris FROM oauth_client WHERE id = ?", (cid,)).fetchone()
    if row:
        return {"name": row[0], "redirect_uris": json.loads(row[1])}
    if cid.startswith("https://") and await _public_host(cid):
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as c:
                async with c.stream("GET", cid) as r:
                    raw = b""
                    async for chunk in r.aiter_bytes():
                        raw += chunk
                        if len(raw) > CLIENT_DOC_MAX:
                            return None
            doc = json.loads(raw)
            if not isinstance(doc, dict):
                return None
            if r.status_code == 200 and doc.get("client_id") == cid:
                return {"name": str(doc.get("client_name") or urllib.parse.urlparse(cid).hostname)[:80],
                        "redirect_uris": [str(u) for u in doc.get("redirect_uris") or []]}
        except (httpx.HTTPError, ValueError):
            return None
    return None


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>:root{{--bg:#f7f7f5;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e2e2dd;--acc:#2f5bd3;--bad:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--card:#1d1d1b;--fg:#ecece8;--mut:#9a9a94;--line:#33332f;--acc:#7d9cf0;--bad:#f2867e}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}main{{max-width:460px;margin:48px auto;padding:0 16px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px}}h1{{font-size:20px;margin:0 0 12px}}
label{{display:block;font-size:13px;color:var(--mut);margin:12px 0 4px}}input,select{{font:inherit;width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);box-sizing:border-box}}
button{{font:inherit;padding:8px 14px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;margin:16px 8px 0 0}}
button.p{{background:var(--acc);border-color:var(--acc);color:#fff}}.mut{{color:var(--mut);font-size:13px}}.bad{{color:var(--bad)}}</style></head>
<body><main><section>{body}</section></main></body></html>""",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'"})


@router.get("/oauth/authorize")
async def authorize(request: Request) -> Response:
    _prune()
    q = request.query_params
    cid, redirect, challenge = q.get("client_id", ""), q.get("redirect_uri", ""), q.get("code_challenge", "")
    client = await _client(cid) if cid else None
    if client is None:
        return _page("Not connected", "<h1>Unknown connector</h1><p>This connector is not registered with this hub.</p>")
    if redirect not in client["redirect_uris"]:
        return _page("Not connected", "<h1>Wrong return address</h1><p>The connector asked to return somewhere it did not register.</p>")
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or len(challenge) < 43:
        sep = "&" if "?" in redirect else "?"
        return RedirectResponse(redirect + sep + urllib.parse.urlencode(
            {"error": "invalid_request", "error_description": "PKCE S256 and response_type=code are required",
             **({"state": q["state"]} if q.get("state") else {})}), status_code=302)
    rid = secrets.token_urlsafe(18)
    _requests[_sha(rid)] = {"at": time.time(), "cid": cid, "name": client["name"], "redirect": redirect,
                            "challenge": challenge, "state": q.get("state", "")}
    mems = [s["name"] for s in gate.storages()]
    opts = "".join(f'<option value="{html.escape(m)}">{html.escape(m)}</option>' for m in mems)
    lic = gate.licence()
    seat = (f'<p class="mut">It takes one of your devices: {lic["in_use"]} of {lic["devices"]} in use.</p>'
            if lic["state"] == "valid" else '<p class="bad">This hub has no valid licence, so no new device can be approved.</p>')
    return _page("Approve connector", f"""<h1>Let {html.escape(client['name'])} use your memory?</h1>
<p>It will be able to read and save memories, like any device you approve. You can remove it at any time on the settings page.</p>{seat}
<form method="post" action="/oauth/approve"><input type="hidden" name="request" value="{html.escape(rid)}">
<label>Memory</label><select name="memory">{opts}</select>
<label>Settings password</label><input type="password" name="password" autocomplete="current-password" required>
<button class="p" name="answer" value="approve">Approve</button><button name="answer" value="deny" formnovalidate>Deny</button></form>""")


@router.post("/oauth/approve")
async def approve(request: Request) -> Response:
    _prune()
    form = await request.form()
    req = _requests.pop(_sha(str(form.get("request") or "")), None)
    if req is None:
        return _page("Expired", "<h1>This request expired</h1><p>Start connecting again from the agent.</p>")
    sep = "&" if "?" in req["redirect"] else "?"
    back = {"state": req["state"]} if req["state"] else {}
    if form.get("answer") != "approve":
        return RedirectResponse(req["redirect"] + sep + urllib.parse.urlencode({"error": "access_denied", **back}),
                                status_code=302)
    want = models.get("_admin_hash")
    pw = str(form.get("password") or "")
    if not (want and pw and hmac.compare_digest(_sha(pw), want)):
        await asyncio.sleep(1)
        _requests[_sha(str(form.get("request")))] = req           # let them try again
        return _page("Wrong password", '<h1>Wrong password</h1><p class="bad">The settings password did not match.</p>'
                                       '<p>Go back and try again.</p>')
    try:
        p = gate.pair_start("oauth:" + req["cid"], req["name"], str(form.get("memory") or ""))
        gate.approve(p["pair"], [str(form.get("memory"))] if form.get("memory") else None)
        got = gate.pair_poll(p["pair"], p["secret"])
    except Exception as e:                                          # noqa: BLE001
        return _page("Not approved", f"<h1>Not approved</h1><p class=\"bad\">{html.escape(str(e))}</p>")
    code = secrets.token_urlsafe(32)
    _codes[_sha(code)] = {"at": time.time(), "cid": req["cid"], "redirect": req["redirect"],
                          "challenge": req["challenge"], "key": got["key"]}
    return RedirectResponse(req["redirect"] + sep + urllib.parse.urlencode({"code": code, **back}), status_code=302)


@router.post("/oauth/token")
async def token(request: Request) -> Response:
    _prune()
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    c = _codes.pop(_sha(str(form.get("code") or "")), None)
    verifier = str(form.get("code_verifier") or "")
    if c is None or c["cid"] != form.get("client_id") or c["redirect"] != form.get("redirect_uri"):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if not hmac.compare_digest(calc, c["challenge"]):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE check failed"}, status_code=400)
    return JSONResponse({"access_token": c["key"], "token_type": "Bearer"},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
