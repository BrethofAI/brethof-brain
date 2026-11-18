"""THE OP APPLIER — heal's guards, on the box.

Not a healer any more. Three healers used to live here — a Sunday full-corpus
sweep, a fast pass over a watermark, and a graph repair pass — and all three
are gone (2026-09-01/02). core/reconcile.py replaced the first two with one job
that reads ONE arriving record against the records it might collide with. The
graph pass went last, and for a structural reason rather than a quality one:
NAMES ARE SET ABOVE THE GRAPH. The registrar decides them from the index, so
the graph only ever contains what was put there — a pass that renames things
locally can only drift from the index it cannot reach, since the box has no
route to it. If graph repair ever looks necessary again, that is a registrar
bug to fix at the index, not a graph to heal.

What is left is the applier reconcile uses (design step 3, 2026-09-10: the
model call and the prompt are the hub's — hub/judge, hub/judgetext):

  _apply     the op applier, and where the guards live: an id must have been
             shown, a no-op update is refused, and REFUSED / NO-OP / applied
             are told apart — which is what stops "0 ops" meaning three
             different things.

SAFETY BOUNDARIES (unchanged): 'rules' and 'state' are founder-owned and never
edited here; memory_index rows are mechanical; *_chat is untouchable; and a
delete only ever runs against an id the model was actually shown.
"""
from __future__ import annotations

import datetime as _dt
import os
import traceback

from core import tools









# ── mechanical lane (free) ──────────────────────────────────────────────────


# ── batching: similarity-grouped, full corpus ───────────────────────────────








def _current_body(project: str, rid: str):
    """The record's content as it is now, or None when unreadable (then the
    guard stands aside — an unreadable row is not evidence of a change)."""
    try:
        from . import store
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT content FROM document WHERE project = %s AND record_id = %s;",
                        [project, rid])
            row = cur.fetchone()
            return (row[0] or "") if row else None
    except Exception:                                          # noqa: BLE001
        return None


def _apply(ops: list[dict], project: str, shown: set[str],
           dry: bool, log: list[str],
           current: dict[str, str] | None = None,
           gone: set[str] | None = None,
           readable: set[str] | None = None,
           exists=None) -> tuple[int, int]:
    """Apply one batch's ops. `gone` accumulates ids deleted EARLIER IN THIS
    RUN, across batches.

    Why it must span batches (found 2026-08-29 by reading a fast-heal log):
    clusters overlap, so a record absorbed in batch 4 can be offered again in
    batch 5 — and worse, can be offered as the SURVIVOR of another merge,
    which would fold new content into a row that no longer exists and lose
    it. seen_ids only ever guarded within a single batch.
    """
    import difflib
    applied = dropped = 0
    seen_ids: set[str] = set()
    # A DELETE NAMES ITS SURVIVOR (2026-09-17). Read from one night's run on
    # 32 arrivals: the judge deleted a gotcha "already stated in the
    # standing-gotchas record" — no such record existed, the content was
    # gone; and it deleted "rule cooldown set to 20" as folded into the
    # injection-config record while the update to that record was SKIPPED
    # below as a no-op (0.93 similar — the 10 → 20 was the whole change),
    # so the store went on saying ten. Both are the same hole: a delete
    # applied on the strength of a reason nobody checks. So a delete carries
    # `into` — the id that now carries what it said — and the applier
    # checks it: an id updated in this batch, or one that exists; anything
    # else is refused. An empty `into` (a passed event, nothing folded) is
    # still allowed and still logged as unpaired.
    updates = {str(o.get("id") or "").strip().split(":")[-1]
               for o in ops or [] if o.get("op") == "update"}
    has_delete = any(o.get("op") == "delete" for o in ops or [])
    # An UNPAIRED delete — no update anywhere in the same batch — is a fold
    # with nothing to fold into: the content simply stops existing. It is not
    # always wrong (two records saying the identical thing need no rewrite of
    # the survivor), so this warns and never refuses. It exists because the
    # model can and does drop a record while stating in its own reason that
    # part of it was a separate fact.
    if any(o.get("op") == "delete" for o in ops or []) \
            and not any(o.get("op") == "update" for o in ops or []):
        log.append("UNPAIRED DELETES: this batch deletes without updating "
                   "any survivor — nothing was folded, only removed")
    for o in ops or []:
        op = str(o.get("op") or "")
        oid = str(o.get("id") or "").strip().split(":")[-1]
        if op not in ("update", "delete") or oid not in shown:
            log.append(f"DROPPED (malformed or outside batch): {str(o)[:120]}")
            dropped += 1
            continue
        if gone is not None and oid in gone:
            # An op on a record an earlier batch already absorbed. Dropping
            # an update here is the point: it would write content into a
            # deleted row and the content would vanish with no error.
            log.append(f"DROPPED (already absorbed this run): {op} {oid}")
            dropped += 1
            continue
        if oid in seen_ids:
            log.append(f"DROPPED (second op on same id): {op} {oid}")
            dropped += 1
            continue
        seen_ids.add(oid)
        # A RECORD SHOWN AS A TITLE ONLY CANNOT BE UPDATED. The model never
        # read it, so any "update" is a guess — on 2026-09-04 it wrote its
        # own note ("flagged for reading — no body was sent") INTO three
        # records as their body. The prompt already forbids it; this is the
        # structural half: `readable` is exactly the set of ids whose bodies
        # were in the prompt (arrivals plus the body tier).
        if op == "update" and readable is not None and oid not in readable:
            log.append(f"REFUSED update of a title-only record: {oid} "
                       f"— it was never read")
            dropped += 1
            continue
        # Output verification, not a cage: an "update" whose content is
        # near-identical to what is stored is a no-op the prompt forbids —
        # applying it would only drift wording.
        if op == "update" and current and oid in current and not has_delete:
            # A near-identical rewrite is a no-op the prompt forbids — unless
            # the batch also DELETES: then this update is the fold's
            # survivor and its small change is the folded fact (2026-09-17:
            # a skipped 0.93-similar update lost "rule cooldown 20").
            ratio = difflib.SequenceMatcher(
                None, current[oid], str(o.get("content") or "")).ratio()
            if ratio > 0.92:
                log.append(f"SKIPPED no-op update ({ratio:.2f} similar): {oid}")
                continue
        if op == "update" and (not str(o.get("title") or "").strip() or not str(o.get("content") or "").strip()):
            # AN UPDATE WITH NO TEXT IS NEVER WRITTEN (2026-09-27: the records
            # replay found 10 brain records blanked — title and content empty,
            # the id left standing — by updates the model sent without their
            # fields; one was the EmbeddingGemma decision, so a search for the
            # box's embedder found nothing). The record keeps what it said.
            log.append(f"REFUSED empty update: {oid} (no title or no content) — the record keeps its text")
            dropped += 1
            continue
        if op == "update" and current and oid in current and not dry:
            # THE EDITOR MUST HAVE READ THE CURRENT TEXT (the study, 2026-09-20:
            # Letta's memory_replace writes nothing unless the quoted text is
            # there exactly once). Here the whole body is rewritten, so the
            # guard is: the record still reads as it was shown to the model.
            # A record that moved meanwhile (a session saved, another heal
            # landed) is not overwritten from a stale view — the next round
            # sees the new text.
            now_body = _current_body(project, oid)
            if now_body is not None and now_body != current[oid]:
                log.append(f"REFUSED update from a stale view: {oid} changed since it was shown")
                dropped += 1
                continue
        if op == "delete":
            into = str(o.get("into") or "").strip().split(":")[-1]
            if into and into not in updates and not (exists(into) if exists else into in shown):
                log.append(f"DELETE refused: {oid} says its content lives in "
                           f"{into!r}, which does not exist")
                dropped += 1
                continue
        log.append(f"{'DRY ' if dry else ''}{op.upper()} {project}:{oid} — "
                   f"{str(o.get('reason') or '')[:110]}")
        if dry:
            applied += 1
            continue
        try:
            if op == "delete":
                res = tools.delete_memory(project, oid)
            else:
                res = tools.write_record(
                    project, oid, str(o.get("type") or "note"),
                    str(o.get("title") or "")[:300],
                    str(o.get("content") or ""))
            # "No record ... - nothing deleted." does NOT start with Error,
            # so it counted as APPLIED and the report said work happened that
            # had not. A run that cannot tell a real delete from a no-op
            # cannot tell anyone when heal is confused.
            if str(res).startswith("Error"):
                log.append(f"  REFUSED: {str(res)[:120]}")
                dropped += 1
            elif op == "delete" and str(res).startswith("No record"):
                log.append(f"  NO-OP: {oid} was already gone")
                dropped += 1
            else:
                applied += 1
                if op == "delete" and gone is not None:
                    gone.add(oid)
        except Exception:
            log.append(f"  FAILED: {traceback.format_exc(limit=1)}")
            dropped += 1
    return applied, dropped


# ── the pass ────────────────────────────────────────────────────────────────






