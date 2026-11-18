"""The box re-reads its keys file when the file changes (2026-10-03): the
hosted host mints and revokes keys in a running tenant's file for device
login, and a key read only at process start waited for a restart."""
import hashlib
import importlib
import os
import time


def test_a_key_added_to_the_file_works_without_a_restart_and_a_removed_one_stops(tmp_path, monkeypatch):
    f = tmp_path / "v2keys"
    k1, k2 = "bmv2_" + "a" * 48, "bmv2_" + "b" * 48
    h = lambda k: hashlib.sha256(k.encode()).hexdigest()
    f.write_text(f"{h(k1)}:memory:customer\n")
    monkeypatch.setenv("BRAIN_V2_KEYS_FILE", str(f))
    monkeypatch.delenv("BRAIN_V2_KEYS", raising=False)
    monkeypatch.delenv("BRAIN_CONTROL_URL", raising=False)
    from core import auth
    auth = importlib.reload(auth)
    assert auth._lookup(k1) and not auth._lookup(k2)
    time.sleep(0.01)
    f.write_text(f"{h(k1)}:memory:customer\n{h(k2)}:memory:customer\n")
    assert auth._lookup(k2)[0] == "memory"                       # minted into the running box
    time.sleep(0.01)
    f.write_text(f"{h(k2)}:memory:customer\n")
    assert not auth._lookup(k1) and auth._lookup(k2)            # revoked in the running box


def test_a_scoped_key_in_the_file_carries_its_scopes_and_a_damaged_field_denies_everything(tmp_path, monkeypatch):
    """SCOPED KEYS IN THE KEY FILE (2026-10-05): every box reads keys only from
    its own file since 2026-09-02, so a scoped key's scopes live on its line."""
    f = tmp_path / "v2keys"
    full, scoped, broken, labelled = ("bmv2_" + c * 48 for c in "cdef")
    h = lambda k: hashlib.sha256(k.encode()).hexdigest()
    monkeypatch.setenv("BRAIN_V2_KEYS_FILE", str(f))
    monkeypatch.delenv("BRAIN_V2_KEYS", raising=False)
    monkeypatch.delenv("BRAIN_CONTROL_URL", raising=False)
    from core import auth
    auth = importlib.reload(auth)
    sc = {"web": "rw", "docs": "read", "*": "none"}
    f.write_text(f"{h(full)}:memory:customer\n"
                 f"{h(scoped)}:memory:customer:{auth.encode_scopes(sc)}\n"
                 f"{h(broken)}:memory:customer:s=!!not-base64-json\n"
                 f"{h(labelled)}:memory:customer:laptop:{auth.encode_scopes({'web': 'read'})}\n")
    auth = importlib.reload(auth)
    assert auth.resolve_scopes(full) is None                          # no field: full access
    assert auth.resolve_scopes(scoped) == sc
    assert auth.resolve_scopes(broken) == {}                          # fail closed, never full
    assert auth.resolve_scopes(labelled) == {"web": "read"}           # a label beside the scopes
    assert auth.resolve(scoped) == "memory" and auth.resolve_tier(scoped) == "customer"
    assert ":" not in auth.encode_scopes(sc) and "," not in auth.encode_scopes(sc)
    assert auth.decode_scopes(auth.encode_scopes({"web": "admin"})) == {}   # unknown mode: deny
