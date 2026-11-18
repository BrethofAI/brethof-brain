"""Backup erasure — the half of "deleted" that lives off this machine.

Founder, 2026-08-24: "someone delete the project or whole thing — gone
from borg. We can't say deleted and keep copy in borg, that's nonsense
and we can have big trouble if we do it."

WHAT WAS ACTUALLY WRONG (proved by dry-run the same day): borg's prune
keeps the N most recent archives that EXIST, not the last N days. The
moment a tenant's database is dropped no new archives are created, so the
prune decision freezes and its dumps persist FOREVER. The backup script's
own comment claimed they "age out on the normal curve"; they do not. So a
deleted account's memory outlived the lifetime we promise it — and the
privacy page says copies age out, which is why this is urgent rather than
tidy.

WHY A QUEUE AND NOT A DIRECT CALL: borg lives on the host, with the
repository passphrase. Giving the API container a delete-capable key
would mean a compromised API could erase every backup we own — the whole
estate, not one tenant. So the API writes an INTENT to the shared archive
mount and the nightly backup script (which already holds the key, and
whose job this is) drains it. Deletion of the live database is immediate;
the backup copies go at the next nightly run, which is the window the
privacy page must state.

The queue is append-only text, one tenant id per line, deliberately
boring: it survives a container restart, needs no database, and a
half-written line is skipped by the drain's own validation.
"""
from __future__ import annotations

import os
import re
import traceback

QUEUE_PATH = os.environ.get("BRAIN_BACKUP_PURGE_QUEUE",
                            "/archive/purge-backups.queue")
_TENANT_RE = re.compile(r"^[a-z0-9_]{3,40}$")


def queue(tenant: str) -> bool:
    """Mark this tenant's backup archives for deletion at the next nightly
    run. Idempotent in effect (the drain is a no-op on an absent family)
    and best-effort by contract: erasure of the LIVE data must never fail
    because a queue file could not be written — but it is loud, because a
    silent miss here is a promise quietly broken."""
    t = (tenant or "").strip().lower()
    t = t[2:] if t.startswith("t_") else t
    if not _TENANT_RE.match(t):
        print(f"[backup-purge] refusing to queue bad tenant id {tenant!r}",
              flush=True)
        return False
    try:
        os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
        with open(QUEUE_PATH, "a", encoding="utf-8") as fh:
            fh.write(t + "\n")
        print(f"[backup-purge] queued {t} — backup archives are deleted at "
              f"the next nightly run", flush=True)
        return True
    except OSError:
        traceback.print_exc()
        print(f"[backup-purge] *** COULD NOT QUEUE {t} *** — its backup "
              f"archives will survive until this is done by hand",
              flush=True)
        return False
