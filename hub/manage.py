#!/usr/bin/env python3
"""Tenant registry CLI. Runs INSIDE the hub container (podman exec) — the
registry has no HTTP admin surface on purpose: fewer doors on a public box.

  python -m hub.manage mint <name> [plan]   -> prints the key ONCE
  python -m hub.manage list
  python -m hub.manage revoke <name>
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys



def main() -> int:
    from hub import store
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    conn = store.connect()
    if True:
        if cmd == "mint":
            name = sys.argv[2]
            plan = sys.argv[3] if len(sys.argv) > 3 else "selfhosted"
            key = "bhk_" + secrets.token_hex(24)
            digest = hashlib.sha256(key.encode()).hexdigest()
            conn.execute(
                "INSERT INTO tenants (id, name, key_hash, plan)"
                " VALUES (?, ?, ?, ?)",
                (store.new_id(), name, digest, plan))
            # A write reporting success is not evidence the value landed —
            # read the row back and prove the stored hash opens for this key.
            row = conn.execute(
                "SELECT key_hash, active FROM tenants WHERE name = ?",
                (name,)).fetchone()
            assert row and row[0] == digest and row[1], "read-back FAILED"
            print(f"tenant {name} plan={plan}")
            print(f"key (SHOWN ONCE, not stored): {key}")
        elif cmd == "revoke":
            name = sys.argv[2]
            conn.execute(
                "UPDATE tenants SET active = 0,"
                " revoked_at = datetime('now') WHERE name = ?", (name,))
            row = conn.execute(
                "SELECT active FROM tenants WHERE name = ?", (name,)).fetchone()
            if row is None:
                print(f"no such tenant: {name}", file=sys.stderr)
                return 1
            assert not row[0], "read-back FAILED"
            print(f"revoked {name}")
        else:
            for r in conn.execute(
                    "SELECT name, plan, active, created_at::date FROM tenants"
                    " ORDER BY created_at"):
                print(f"{r[0]:24} {r[1]:12} {'active' if r[2] else 'REVOKED'} {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
