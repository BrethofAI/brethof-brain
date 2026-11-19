"""The hub's front door (2026-11-17): pairing end to end through HTTP, the
settings API behind its password, and forwarding to the right memory with
the storage's key — never the device's."""
import asyncio
import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _forgeable():
    """The published gate is compiled with Brethof's licence key: a test
    cannot sign a licence for it (that is the point of the gate)."""
    import gate, os
    if gate._pubkey_b64() != os.environ.get("GATE_TEST_PUBKEY"):
        pytest.skip("compiled gate holds the production licence key")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    sk = Ed25519PrivateKey.generate()
    monkeypatch.setenv("GATE_TEST_PUBKEY", b64(sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)))
    from hub import front, models, store
    import gate
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setattr(store, "_conn", None)
    store.apply_schema(open("hub/schema.sql", encoding="utf-8").read())
    gate.init(str(tmp_path / "gate.sqlite"))
    run(models.load())
    run(models.set_many({"_admin_hash": hashlib.sha256(b"pw").hexdigest()}))

    seen = []
    storage = FastAPI()

    @storage.api_route("/{path:path}", methods=["GET", "POST"])
    async def echo(path: str, request: Request):
        seen.append({"path": path, "auth": request.headers.get("authorization"),
                     "cookie": request.headers.get("cookie"),
                     "device": request.headers.get("x-brain-device"),
                     "q": request.url.query, "body": (await request.body()).decode()})
        from fastapi.responses import JSONResponse as _J
        r = _J({"from": request.url.hostname, "path": path})
        r.set_cookie("planted", "x")
        return r

    front._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=storage))
    key = gate.add_storage("main", "http://main")
    run(front.ensure_tenant("main", key))
    gate.add_storage("team", "http://team")

    def lic(devices=5):
        p = {"v": 1, "lic": "l", "account": "a", "hub": gate.hub_id(), "devices": devices, "plan": "free",
             "issued": int(time.time()), "expires": int(time.time()) + 86400}
        return {"payload": p, "sig": b64(sk.sign(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()))}
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=front.front), base_url="http://hub")
    yield {"c": c, "seen": seen, "lic": lic, "gate": gate, "storage_key": key, "store": store}
    monkeypatch.setattr(store, "_conn", None)


async def _pair(c, memory=""):
    s = (await c.post("/pair/start", json={"client_id": "cid1", "name": "laptop", "memory": memory})).json()
    return s


def test_settings_api_needs_the_password(hub):
    c = hub["c"]

    async def go():
        assert (await c.get("/admin/api/state")).status_code == 401
        assert (await c.post("/admin/login", json={"password": "nope"})).status_code == 401
        r = await c.post("/admin/login", json={"password": "pw"})
        assert r.status_code == 200 and "hub_admin" in r.cookies
        st = (await c.get("/admin/api/state")).json()
        assert st["ok"] and [m["name"] for m in st["memories"]] == ["main", "team"]
        assert (await c.get("/admin/api/state", headers={"Authorization": "Bearer pw"})).json()["ok"]
    run(go())


def test_pair_approve_forward_with_storage_key(hub):
    _forgeable()
    c, seen, gate = hub["c"], hub["seen"], hub["gate"]
    gate.install_licence(hub["lic"]())

    async def go():
        s = await _pair(c)
        assert s["ok"] and s["code"]
        # unauthenticated forward refused
        assert (await c.get("/v1/anything")).status_code == 401
        r = await c.post("/admin/api/approve", json={"pair": s["pair"]}, headers={"Authorization": "Bearer pw"})
        assert r.json()["ok"], r.text
        got = (await c.post("/pair/poll", json={"pair": s["pair"], "secret": s["secret"]})).json()
        key = got["key"]
        r = await c.post("/v1/hook/prompt?x=1", json={"a": 1}, headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200 and r.json() == {"from": "main", "path": "v1/hook/prompt"}
        last = seen[-1]
        assert last["auth"] == f"Bearer {hub['storage_key']}"           # the device key never travels on
        assert last["q"] == "x=1" and json.loads(last["body"]) == {"a": 1}
        assert last["device"].startswith("dv_")
        # a memory this device was not given
        r = await c.get("/m/team/v1/x", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 403
        # given it, it routes there
        did = gate.devices()[0]["id"]
        await c.post("/admin/api/device/memories", json={"id": did, "memories": ["main", "team"]},
                     headers={"Authorization": "Bearer pw"})
        r = await c.get("/m/team/v1/x", headers={"Authorization": f"Bearer {key}"})
        assert r.json() == {"from": "team", "path": "v1/x"}
        # removed: refused at once
        await c.post("/admin/api/device/remove", json={"id": did}, headers={"Authorization": "Bearer pw"})
        assert (await c.get("/v1/x", headers={"Authorization": f"Bearer {key}"})).status_code == 401
    run(go())


def test_no_licence_approval_refused_with_reason(hub):
    c = hub["c"]

    async def go():
        s = await _pair(c)
        r = await c.post("/admin/api/approve", json={"pair": s["pair"]}, headers={"Authorization": "Bearer pw"})
        assert r.status_code == 403 and "licence" in r.json()["why"]
    run(go())


def test_models_keys_never_echoed(hub):
    c = hub["c"]

    async def go():
        h = {"Authorization": "Bearer pw"}
        await c.post("/admin/api/models", json={"ai_url": "http://ollama:11434/v1", "ai_model": "m",
                                                "ai_key": "sk-secret"}, headers=h)
        st = (await c.get("/admin/api/state", headers=h)).json()
        assert st["models"]["ai_key_set"] is True and "sk-secret" not in json.dumps(st)
        # an empty key on save keeps the old one
        await c.post("/admin/api/models", json={"ai_model": "m2", "ai_key": ""}, headers=h)
        from hub import models
        assert models.ai() == {"url": "http://ollama:11434/v1", "key": "sk-secret", "model": "m2", "reasoning": "", "extra": {}}
    run(go())


def test_storage_tenant_is_internal_and_uncapped(hub):
    row = hub["store"].connect().execute("SELECT name, tier, active FROM tenants WHERE id='t_main'").fetchone()
    assert row == ("main", "internal", 1)


def test_cloud_agent_signs_in_and_becomes_a_device(hub):
    """The MCP authorization flow end to end: 401 points at the metadata,
    register, approve with the settings password, PKCE token = a device key
    that reaches the memory; a wrong verifier gets nothing."""
    _forgeable()
    import base64 as _b, hashlib as _h, secrets as _s, urllib.parse as up
    c, gate = hub["c"], hub["gate"]
    gate.install_licence(hub["lic"]())

    async def go():
        r = await c.post("/v1/mcp", json={})
        assert r.status_code == 401
        assert 'resource_metadata="http://hub/.well-known/oauth-protected-resource/v1/mcp"' in r.headers["www-authenticate"]
        meta = (await c.get("/.well-known/oauth-protected-resource/v1/mcp")).json()
        assert meta["authorization_servers"] == ["http://hub"]
        asm = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert asm["code_challenge_methods_supported"] == ["S256"]
        assert (await c.post("/oauth/register", json={"client_name": "Grok",
                                                      "redirect_uris": ["http://evil.example/cb"]})).status_code == 400
        reg = (await c.post("/oauth/register", json={"client_name": "Grok",
                                                     "redirect_uris": ["https://grok.example/cb"]})).json()
        verifier = _s.token_urlsafe(48)
        chal = _b.urlsafe_b64encode(_h.sha256(verifier.encode()).digest()).decode().rstrip("=")

        async def start():
            page = await c.get("/oauth/authorize", params={
                "response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://grok.example/cb",
                "code_challenge": chal, "code_challenge_method": "S256", "state": "st8"})
            assert page.status_code == 200 and "Let Grok use your memory?" in page.text
            return page.text.split('name="request" value="')[1].split('"')[0]

        rid = await start()
        bad = await c.post("/oauth/approve", data={"request": rid, "memory": "main", "password": "nope",
                                                   "answer": "approve"})
        assert "Wrong password" in bad.text and gate.devices_in_use() == 0
        ok = await c.post("/oauth/approve", data={"request": rid, "memory": "main", "password": "pw",
                                                  "answer": "approve"})
        assert ok.status_code == 302
        q = up.parse_qs(up.urlparse(ok.headers["location"]).query)
        assert q["state"] == ["st8"] and gate.devices_in_use() == 1
        wrong = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                                   "client_id": reg["client_id"], "redirect_uri": "https://grok.example/cb",
                                                   "code_verifier": "x" * 50})
        assert wrong.status_code == 400
        # the code was spent by the failed attempt — a fresh approval is needed
        rid = await start()
        ok = await c.post("/oauth/approve", data={"request": rid, "memory": "main", "password": "pw",
                                                  "answer": "approve"})
        code = up.parse_qs(up.urlparse(ok.headers["location"]).query)["code"][0]
        tok = (await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                  "client_id": reg["client_id"], "redirect_uri": "https://grok.example/cb",
                                                  "code_verifier": verifier})).json()
        assert tok["token_type"] == "Bearer" and tok["access_token"].startswith("bd_")
        assert gate.devices_in_use() == 1            # the same connector again: no second seat
        r = await c.post("/v1/mcp", json={"x": 1}, headers={"Authorization": "Bearer " + tok["access_token"]})
        assert r.status_code == 200 and hub["seen"][-1]["auth"] == f"Bearer {hub['storage_key']}"
        # the code is good once
        again = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                   "client_id": reg["client_id"], "redirect_uri": "https://grok.example/cb",
                                                   "code_verifier": verifier})
        assert again.status_code == 400
        # denied: back to the agent with access_denied
        rid = await start()
        d = await c.post("/oauth/approve", data={"request": rid, "answer": "deny"})
        assert "error=access_denied" in d.headers["location"]
    run(go())


def test_extra_request_options_must_be_a_json_object(hub):
    c = hub["c"]

    async def go():
        h = {"Authorization": "Bearer pw"}
        bad = await c.post("/admin/api/models", json={"ai_extra": "enable_thinking=false"}, headers=h)
        assert bad.status_code == 400
        await c.post("/admin/api/models", json={"ai_url": "http://v:8000/v1", "ai_model": "q",
                                                "ai_extra": '{"chat_template_kwargs": {"enable_thinking": false}}'},
                     headers=h)
        from hub import models
        assert models.ai()["extra"] == {"chat_template_kwargs": {"enable_thinking": False}}
    run(go())


def test_a_body_that_is_not_a_json_object_is_a_400_never_a_500(hub):
    """Found by scripts/hammer/front_attacks.py on rc9 (2026-11-18): login and
    pair poll answered 500 to a body that was not a JSON object."""
    c = hub["c"]

    async def go():
        for path in ("/admin/login", "/pair/start", "/pair/poll", "/oauth/register"):
            for raw in (b"not json", b"[1, 2]", b'"a string"'):
                r = await c.post(path, content=raw, headers={"Content-Type": "application/json"})
                assert r.status_code == 400, (path, raw, r.status_code)
        r = await c.post("/admin/login", content=b"")
        assert r.status_code == 401                                   # an empty body is just no password
        r = await c.post("/admin/api/approve", content=b"[]", headers={"Authorization": "Bearer pw"})
        assert r.status_code == 400
    run(go())


def test_cookies_never_cross_the_hub(hub):
    """The settings cookie is the admin password: it never reaches a memory,
    and a memory never sets a cookie on the hub's origin (2026-11-18)."""
    _forgeable()
    from hub import front
    front._pair_bucket.clear()               # the pairing flood bucket is per process
    c, seen, gate = hub["c"], hub["seen"], hub["gate"]
    gate.install_licence(hub["lic"]())

    async def go():
        s = await _pair(c)
        await c.post("/admin/api/approve", json={"pair": s["pair"]}, headers={"Authorization": "Bearer pw"})
        key = (await c.post("/pair/poll", json={"pair": s["pair"], "secret": s["secret"]})).json()["key"]
        r = await c.get("/v1/x", headers={"Authorization": f"Bearer {key}", "Cookie": "hub_admin=pw"})
        assert r.status_code == 200
        assert seen[-1]["cookie"] is None
        assert "set-cookie" not in r.headers
    run(go())


def test_a_client_id_url_on_the_owners_network_is_never_fetched(hub):
    """Anyone can type a client id: the hub must not be made to call the
    owner's own network or itself (2026-11-18)."""
    from hub import oauth
    for u in ("https://127.0.0.1/c", "https://localhost/c", "https://10.0.0.5/c",
              "https://192.168.1.2/c", "https://[::1]/c", "https://169.254.169.254/c"):
        assert run(oauth._public_host(u)) is False, u
    assert run(oauth._client("https://127.0.0.1/c")) is None


# ── the owner's login switch (founder 2026-11-19) ────────────────────────────
def test_the_owner_sets_a_password_and_may_turn_the_login_off(hub):
    c = hub["c"]
    A = {"Authorization": "Bearer pw"}

    async def go():
        assert (await c.post("/admin/api/password", json={"password": "short"}, headers=A)).status_code == 400
        assert (await c.post("/admin/api/password", json={"password": "a-long-own-password"}, headers=A)).json()["ok"]
        assert (await c.get("/admin/api/state", headers=A)).status_code == 401          # the old one is gone
        B = {"Authorization": "Bearer a-long-own-password"}
        st = (await c.get("/admin/api/state", headers=B)).json()
        assert st["ok"] and st["login"] == "on"
        assert (await c.post("/admin/api/login", json={"on": False}, headers=B)).json()["login"] == "off"
        # off: the page works with no password, from this machine
        local = {"host": "127.0.0.1:8600"}
        st = (await c.get("/admin/api/state", headers=local)).json()
        assert st["ok"] and st["login"] == "off"
        assert (await c.post("/admin/api/login", json={"on": True}, headers=local)).json()["login"] == "on"
        c.cookies.clear()                      # setting the password signed this browser in
        assert (await c.get("/admin/api/state", headers=local)).status_code == 401
    run(go())


def test_with_the_login_off_websites_still_cannot_drive_the_page(hub):
    from hub import models
    c = hub["c"]
    run(models.set_many({"_admin_login": "off"}))

    async def go():
        # DNS rebinding: a page on someone's domain that resolves here
        assert (await c.get("/admin/api/state", headers={"host": "evil.example:8600"})).status_code == 401
        # a cross-site form posts text/plain, never JSON
        r = await c.post("/admin/api/login", content=b'{"on": false}',
                         headers={"host": "127.0.0.1:8600", "content-type": "text/plain"})
        assert r.status_code == 401
        # a JSON post from another origin
        r = await c.post("/admin/api/login", json={"on": True},
                         headers={"host": "127.0.0.1:8600", "origin": "https://evil.example"})
        assert r.status_code == 401
        # the page's own post goes through
        r = await c.post("/admin/api/login", json={"on": True},
                         headers={"host": "127.0.0.1:8600", "origin": "http://127.0.0.1:8600"})
        assert r.json()["login"] == "on"
    run(go())
