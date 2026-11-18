"""APPLY what the new curator writes — the create-only `records` shape.

The curator prompt was rewritten on 2026-08-29 to emit
{"records":[...], "entities":[...]}, but every applier in the tree still
read the old {"ops":[...]} shape, so the new curator could not write at all.
This is that missing half.

THREE THINGS THE OLD APPLIER DID NOT HAVE TO DO.

1. MINT THE ID. The old shape carried one; this one deliberately does not.
   Measured on 127 real records: the model emits type/title/content/reason
   and nothing else, every time. That is the design — the curator's whole
   job is to state facts, and reconciling a fact that is already known
   happens AFTER it, at the save door. So the id is derived here, from the
   title, and a collision is not an error: it is the merge door doing its
   job.

2. ROUTE BY THE `project` FIELD. The LAW says a fact about another project
   belongs to that project. Until 2026-08-29 it said to express that as a
   qualified id — in a schema with no id field, so it was dead text and
   127/127 records came back unrouted. The field exists now; this reads it,
   and REFUSES a project that does not exist rather than creating one. A
   curator using its kind vocabulary as a filing cabinet is how the junk
   project 'claim' appeared [[brain:curator_entity_kind_qualifier_bug]].

3. LINK THE ENTITY TO ITS RECORD. Records and entities arrive in the SAME
   object, from the same piece, so the link is free — no second call, no
   inference. It exists because the graph carries no content: `entity` is
   name/type/status and nothing else, so the session brief could only ever
   print "do not resurrect: neighbourhood (08-29)" — a label pointing at
   nothing. With the link the same line can print the record's title, which
   says what was actually decided.
"""
from __future__ import annotations

import datetime as _dt
import re
import traceback

from . import store, tools

# (entity_record — the record-to-thing link — left on 2026-09-17: the curated
#  records and the graph are two systems; a record points only into the archive.)
TITLE_MAX = 300
_WORD = re.compile(r"[a-z0-9]+")
# Words that carry no identity — dropping them keeps the id about the fact.
_STOP = {"the", "a", "an", "of", "for", "to", "and", "in", "on", "is", "are",
         "was", "were", "it", "its", "that", "this", "with", "by", "from"}


def mint_id(title: str, today: str = "") -> str:
    """A record id from its title: snake_case, dated, bounded.

    Dated because the store's own ids are (`teachers_rebuilt_2026_08_29`),
    and because two facts about the same subject weeks apart are two
    records — the merge door decides whether they fold, not the id.
    """
    words = [w for w in _WORD.findall((title or "").lower())
             if w not in _STOP][:8]
    stem = "_".join(words)[:60].strip("_") or "note"
    day = today or _dt.date.today().isoformat()
    return f"{stem}_{day.replace('-', '_')}"


def _known_projects(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT project FROM document;")
    return {r[0] for r in cur.fetchall()}


def apply_records(conn, project: str, records: list, log: list,
                  today: str = "", origin: dict | None = None) -> tuple[int, int]:
    """Save each record. Returns (applied, dropped). Never raises.
    `origin` is the piece's provenance — session_id, turn_lo, turn_hi —
    stamped into each record's meta (2026-09-09)."""
    applied = dropped = 0
    projects = None
    for r in records or []:
        if not isinstance(r, dict):
            log.append(f"MALFORMED record dropped: {str(r)[:100]}")
            dropped += 1
            continue
        title = str(r.get("title") or "").strip()
        content = str(r.get("content") or "").strip()
        if not title or not content:
            # A record with no body is a title pretending to be knowledge.
            # (The 2026-09-04 stubs — "(flagged for reading — no body was
            # sent)" — were the HEALER writing its own note as a body; that
            # is refused where it happens, in heal._apply, structurally.)
            log.append(f"EMPTY record dropped: {title[:80]!r}")
            dropped += 1
            continue
        target = project
        want = str(r.get("project") or "").strip()
        if want and want != project:
            if projects is None:
                try:
                    projects = _known_projects(conn)
                except Exception:
                    conn.rollback()
                    projects = set()
            if want in projects:
                target = want
            else:
                # Never CREATE a project from a curator's guess.
                log.append(f"ROUTE REFUSED '{want}' (no such project) — "
                           f"filed under {project}: {title[:60]}")
        rid = mint_id(title, today)
        try:
            res = tools.write_record(target, rid,
                                    str(r.get("type") or "note"),
                                    title[:TITLE_MAX], content, origin=origin)
        except Exception as e:                                 # noqa: BLE001
            log.append(f"REC {rid} FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
            dropped += 1
            continue
        if res.startswith("Error"):
            log.append(f"REC {rid} REJECTED — {res[:120]}")
            dropped += 1
            continue
        applied += 1
        r["_id"] = f"{target}:{rid}"          # for the entity link below
        log.append(f"REC {target}:{rid} — {res.splitlines()[0][:110]} "
                   f"| {str(r.get('reason') or '')[:100]}")
    # HEAL STARTS WHERE THE RECORD LANDS. This is the ONE door every curated
    # record comes through — the hub's pipe and the box's own curate job both
    # end here — so the queue is filled here and not at each call site, where
    # one of them would eventually be forgotten.
    #
    # Only what actually landed is queued, in the order it landed: the queue id
    # IS the timeline, which is why nothing downstream has to infer recency from
    # a timestamp a hand pass can restamp.
    #
    # It cannot feed itself: reconcile's own writes go through tools.save_memory
    # and never reach this function.
    if applied:
        try:
            from . import healqueue
            for r in records or []:
                rid = str(r.get("_id") or "") if isinstance(r, dict) else ""
                if not rid:
                    continue
                proj, _, bare = rid.partition(":")
                healqueue.enqueue(conn, proj or project, bare)
        except Exception as e:                                 # noqa: BLE001
            # Never fail the SAVE over the queue: the record is written, and a
            # record that lands unreconciled is worth more than a lost one.
            log.append(f"HEAL QUEUE skipped: {type(e).__name__}: {e}")
    return applied, dropped


def bare_id(rid: str) -> str:
    """"project:id" -> "id"; a bare id passes through."""
    s = str(rid or "")
    return s.partition(":")[2] if ":" in s else s


