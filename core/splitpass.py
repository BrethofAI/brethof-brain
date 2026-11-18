"""The one-time SPLIT pass over a project's long curated records (founder
2026-09-18: "we cannot have curated records which keep many different things
under one record ... they need to be split").

Every record of the project over `min_len` characters is shown WHOLE to the
hub's split judgement (core/prompts 'split', hub op split_judge). The answer
is the records it becomes: [] keeps it exactly as it is; one record is the
same subject rewritten short; several are one record per subject. The box
writes the parts through tools.write_record with the original's origin, then
deletes the original. A snapshot table (document_snapshot_split) holds every
record shown, so anything can be put back.

Runs in a daemon thread inside the api process — the judgement travels the
pipe, which lives only there — and says everything it does on stdout under
"[split] <project>:" so the pass is proven by READING the log, never by its
counts. Guards are the applier's, not the judge's: at most MAX_PARTS parts,
every part at least MIN_PART chars, the parts together at least MIN_TOTAL of
the original (a rewrite may drop the route, never the substance); a
malformed answer changes nothing.
"""
from __future__ import annotations

import datetime
import threading
import traceback

from . import curate_apply, healqueue, judge, store, tools

MAX_PARTS = 8
MIN_PART = 80
MIN_TOTAL = 0.30
LONG_PART = 900        # one part longer than this is not "one subject told short"
_running: set[str] = set()
_lock = threading.Lock()


def _say(project: str, line: str) -> None:
    print(f"[split] {project}: {line}", flush=True)


def _take_snapshot(conn, project: str, min_len: int) -> None:
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS document_snapshot_split AS SELECT * FROM document WHERE false;")
    cur.execute("INSERT INTO document_snapshot_split SELECT d.* FROM document d WHERE d.project = %s "
                "AND length(d.content) > %s AND NOT EXISTS (SELECT 1 FROM document_snapshot_split s "
                "WHERE s.project = d.project AND s.record_id = d.record_id);", [project, min_len])
    conn.commit()


def _long_records(conn, project: str, min_len: int, limit: int) -> list[tuple]:
    cur = conn.cursor()
    cur.execute("SELECT record_id, type, title, content, updated_at::date::text, created_at::date::text, meta "
                "FROM document WHERE project = %s AND length(content) > %s AND record_id <> 'memory_index' "
                "ORDER BY length(content) DESC LIMIT %s;", [project, min_len, limit])
    return cur.fetchall()


def _guard(parts: list[dict], original_len: int) -> str:
    if len(parts) > MAX_PARTS:
        return f"{len(parts)} parts"
    if len(parts) == 1 and len(str(parts[0]["content"])) > LONG_PART:
        return f"one part of {len(str(parts[0]['content']))} chars is not short"
    if min(len(str(p["content"])) for p in parts) < MIN_PART:
        return f"a part under {MIN_PART} chars"
    total = sum(len(str(p["content"])) for p in parts)
    if total < MIN_TOTAL * original_len:
        return f"parts total {total} of {original_len} chars"
    return ""


def run(tenant: str, project: str, min_len: int = 800, limit: int = 100000,
        dry: bool = False) -> dict:
    """The pass, synchronously. Reads on one short connection, asks the hub
    holding nothing (the 2026-09-17 pool lesson), writes through the tools'
    own connections."""
    tok = store.bind(tenant)
    counts = {"shown": 0, "split": 0, "kept": 0, "refused": 0, "failed": 0,
              "tokens_in": 0, "tokens_out": 0}
    try:
        with store.connection() as conn:
            _take_snapshot(conn, project, min_len)
            rows = _long_records(conn, project, min_len, limit)
            known = curate_apply._known_projects(conn)
        counts["shown"] = len(rows)
        _say(project, f"{len(rows)} records over {min_len} chars; snapshot document_snapshot_split holds them"
                      + (" (DRY — nothing written)" if dry else ""))
        for rid, typ, title, content, upd, created, meta in rows:
            rec = {"record_id": rid, "type": typ, "title": title, "content": content, "upd": upd}
            note = ""
            parts = None
            for attempt in range(2):
                body = {"project": project, "record": rec}
                if note:
                    body["note"] = note
                try:
                    a = judge.ask("split_judge", body, timeout=180)
                except judge.NoAnswer as e:
                    counts["failed"] += 1
                    _say(project, f"FAILED {rid}: {e}")
                    parts = None
                    break
                counts["tokens_in"] += a.get("input_tokens", 0)
                counts["tokens_out"] += a.get("output_tokens", 0)
                ans = a.get("answer") or {}
                reason = str(ans.get("reason") or "")[:200]
                parts = ans.get("records") if isinstance(ans.get("records"), list) else None
                if parts is None:
                    counts["failed"] += 1
                    _say(project, f"FAILED {rid}: no records list ({a.get('fault') or 'malformed'})")
                    break
                parts = [p for p in parts if isinstance(p, dict)
                         and str(p.get("title") or "").strip() and str(p.get("content") or "").strip()]
                # One long part is the judge calling a bloated record "one
                # subject told at length" and handing it back nearly whole
                # (the dry run of 2026-09-18: 2,312 → 2,213 chars). Ask once
                # more, saying so; a second long answer is refused below.
                if len(parts) == 1 and len(str(parts[0]["content"])) > LONG_PART and not note:
                    note = (f"Your previous answer kept this as ONE record of "
                            f"{len(str(parts[0]['content']))} characters — too long to be one subject "
                            f"told short. Split it by subject, or drop what is route and history.")
                    continue
                break
            if parts is None:
                continue
            if not parts:
                counts["kept"] += 1
                _say(project, f"KEEP {rid} — {reason}")
                continue
            why = _guard(parts, len(content))
            if why:
                counts["refused"] += 1
                _say(project, f"REFUSED {rid}: {why} — {reason}")
                continue
            origin = meta.get("origin") if isinstance(meta, dict) else None
            day = created or upd or datetime.date.today().isoformat()
            _say(project, f"SPLIT {rid} ({len(content)} chars) → {len(parts)} — {reason}")
            new_ids = []
            for p in parts:
                nid = curate_apply.mint_id(str(p["title"]), day)
                want = str(p.get("project") or "").strip()
                target = want if want and want in known else project
                where = f" → {target}" if target != project else ""
                if want and target == project and want != project:
                    where = f" (project {want!r} unknown — filed here)"
                _say(project, f"    + {target}:{nid} [{p.get('type') or typ or 'note'}, {len(str(p['content']))} chars] "
                              f"{str(p['title'])[:90]}{where}")
                _say(project, f"      {p['content']}")
                if dry:
                    continue
                res = tools.write_record(target, nid, str(p.get("type") or typ or "note"),
                                         str(p["title"])[:200], str(p["content"]), dedup=False, origin=origin)
                if res.startswith("Error"):
                    _say(project, f"      WRITE FAILED {nid}: {res[:160]}")
                    continue
                new_ids.append((target, nid))
            if not dry:
                if any(nid == rid for _, nid in new_ids):
                    # A part minted the original's own id and overwrote it in
                    # place — deleting the id now would delete the part
                    # (2026-09-18: four parts lost that way, restored from the
                    # log). The original IS the part; leave it.
                    _say(project, f"    = {rid}: rewritten in place by its own part, not deleted")
                elif new_ids:
                    r = tools.delete_memory(project, rid)
                    _say(project, f"    - {rid}: {r[:100]}")
                    # The parts are ARRIVALS: heal judges them against their
                    # neighbours like any landed record (a dead name carried
                    # over from the original is heal's to retire, not the
                    # split's to guess at).
                    try:
                        with store.connection() as conn:
                            for tgt, nid in new_ids:
                                healqueue.enqueue(conn, tgt, nid)
                            conn.commit()
                    except Exception as e:                     # noqa: BLE001
                        _say(project, f"    heal enqueue failed: {type(e).__name__}: {e}")
                else:
                    _say(project, f"    - {rid} KEPT: no part could be written")
            counts["split"] += 1
        _say(project, f"done: {counts['split']} split, {counts['kept']} kept, {counts['refused']} refused, "
                      f"{counts['failed']} failed; tokens in {counts['tokens_in']} out {counts['tokens_out']}"
                      + (" (DRY — nothing written)" if dry else ""))
    except Exception as e:                                     # noqa: BLE001
        traceback.print_exc()
        _say(project, f"done: FAILED {type(e).__name__}: {e}")
    finally:
        store.unbind(tok)
    return counts


def trigger(tenant: str, project: str, min_len: int = 800, limit: int = 100000,
            dry: bool = False) -> str:
    """Start the pass in a daemon thread, one per project at a time."""
    key = f"{tenant}/{project}"
    with _lock:
        if key in _running:
            return "split: already running for this project"
        _running.add(key)

    def _run() -> None:
        try:
            run(tenant, project, min_len=min_len, limit=limit, dry=dry)
        finally:
            with _lock:
                _running.discard(key)

    threading.Thread(target=_run, name=f"split-{project}", daemon=True).start()
    return "split: started"
