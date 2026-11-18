"""The embedder's bearer token (2026-11-15). The embedder had no auth and
served only its own bridge network; the 2026-08-27 decision puts auth before
the first off-box caller (the Almanac on prod). The check is executed alone:
embedder/server.py loads the model at import, which no unit test should."""
import os
import pathlib


def _authorized(token: str, auth_port: str = ""):
    src = pathlib.Path("embedder/server.py").read_text(encoding="utf-8")
    block = src.split("EMBED_TOKEN = os.environ", 1)[1].split("class Handler", 1)[0]
    ns = {"os": os}
    old = os.environ.get("EMBED_TOKEN")
    os.environ["EMBED_TOKEN"] = token
    os.environ["EMBED_AUTH_PORT"] = auth_port
    try:
        exec("EMBED_TOKEN = os.environ" + block, ns)
    finally:
        if old is None:
            os.environ.pop("EMBED_TOKEN", None)
        else:
            os.environ["EMBED_TOKEN"] = old
        os.environ.pop("EMBED_AUTH_PORT", None)
    return ns["authorized"]


def test_no_token_set_leaves_the_embedder_open_as_every_local_install_is():
    ok = _authorized("")
    assert ok("") and ok("Bearer anything")


def test_a_set_token_admits_only_its_own_bearer_header():
    ok = _authorized("s3cret")
    assert ok("Bearer s3cret")
    assert not ok("") and not ok("Bearer wrong") and not ok("s3cret") and not ok("Bearer s3cret ")


def test_with_an_auth_port_only_that_door_is_gated_and_the_bridge_port_is_unchanged():
    ok = _authorized("s3cret", "8203")
    assert ok("", 8202) and ok("Bearer wrong", 8202)          # the boxes on the bridge
    assert ok("Bearer s3cret", 8203)
    assert not ok("", 8203) and not ok("Bearer wrong", 8203)  # the off-box door


def test_embed_is_gated_and_health_is_not():
    src = pathlib.Path("embedder/server.py").read_text(encoding="utf-8")
    post = src.split("def do_POST", 1)[1].split("def log_message", 1)[0]
    get = src.split("def do_GET", 1)[1].split("def do_POST", 1)[0]
    assert 'authorized(self.headers.get("Authorization", ""), self.server.server_port)' in post
    assert "authorized" not in get


def test_the_box_sends_the_token_only_when_it_has_one(monkeypatch):
    import json
    from core import embed
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return json.dumps({"vectors": [[0.0]]}).encode()

    def fake(req, timeout=0):
        seen["h"] = dict(req.header_items())
        return R()
    monkeypatch.setattr(embed.urllib.request, "urlopen", fake)
    monkeypatch.setattr(embed, "_who", lambda: {})
    monkeypatch.setattr(embed, "EMBEDDER_TOKEN", "")
    embed._call_sidecar(["x"], 1, "query", True)
    assert "Authorization" not in seen["h"]
    monkeypatch.setattr(embed, "EMBEDDER_TOKEN", "tok")
    embed._call_sidecar(["x"], 1, "query", True)
    assert seen["h"]["Authorization"] == "Bearer tok"
