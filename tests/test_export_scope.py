"""Export honors project scope — the read door the scoped-key pass missed.

GLM white-box audit, 2026-08-09: /v1/export authenticated the key but
streamed EVERY project's chat regardless of the key's scope. In the
app-tenant model (a project per end-user) that is one user pulling every
other user's history. An unrestricted key still exports the whole tenant
(cross-project breadth is the product); a scoped key exports only what it
can see.
"""
import unittest
from unittest import mock

import api.main as m


def _projects_exported(scopes):
    """Drive _export_response with a stub DB + zip and capture which projects
    it actually opened a chat/*.jsonl member for."""
    written = []

    conn = mock.MagicMock()
    cur = conn.cursor.return_value
    cur.fetchall.return_value = [("global",), ("u_alice",), ("u_bob",)]
    # the per-project server-side cursor (conn.cursor(name=...)) yields no rows
    conn.cursor.return_value.__enter__.return_value.__iter__ = \
        lambda self: iter(())

    zf = mock.MagicMock()

    def _open(name, mode="r"):
        if name.startswith("chat/") and name.endswith(".jsonl"):
            written.append(name[len("chat/"):-len(".jsonl")])
        return mock.MagicMock()
    zf.open.side_effect = _open

    with mock.patch.object(m.store, "bind", return_value=object()), \
         mock.patch.object(m.store, "unbind"), \
         mock.patch.object(m.store, "connection") as sc, \
         mock.patch("tempfile.NamedTemporaryFile") as tf, \
         mock.patch("zipfile.ZipFile") as zc, \
         mock.patch("os.remove"):
        tf.return_value.name = "/tmp/x.zip"
        sc.return_value.__enter__.return_value = conn
        zc.return_value.__enter__.return_value = zf
        try:
            m._export_response("tnt", scopes)
        except Exception:
            pass  # FileResponse plumbing is not under test
    return set(written)


class ExportScope(unittest.TestCase):
    def test_unrestricted_key_exports_every_project(self):
        self.assertEqual(_projects_exported(None),
                         {"global", "u_alice", "u_bob"})

    def test_scoped_key_exports_only_visible_projects(self):
        got = _projects_exported({"u_alice": "rw", "*": "none"})
        self.assertEqual(got, {"u_alice"})

    def test_default_read_scope_sees_all(self):
        self.assertEqual(_projects_exported({"*": "read"}),
                         {"global", "u_alice", "u_bob"})


if __name__ == "__main__":
    unittest.main()
