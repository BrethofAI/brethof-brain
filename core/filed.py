"""HEAL'S EYES — what the project's rules and playbooks already say
(founder 2026-09-29: "build eyes … put it to production straight away").

Heal (TEXTS["reconcile"], Z4) deletes a record that only repeats a rule or a
playbook, and to do that it must SEE them: each heal call is handed the
sections nearest to the records it judges, read-only, under ALREADY FILED.
Measured on the bench before this was built (keep/round9–13): with the 8
nearest sections heal held 3–4 of 20 filed must-keeps instead of 9–11, at
~25% more heal input tokens and no measurable loss of unfiled must-keeps.

The store is one table per tenant, `filed_section`: every rule of the
project (its own and the every-project ones, from the hub) and every
playbook the project may use, cut at its headings into pieces of at most
SECTION_CHARS, each embedded once. A playbook named for ANOTHER project
("<other>-…") is that project's and is left out; every other playbook is
the tenant's and is in. Refresh is by content: a piece whose text did not
change keeps its vector, a new one is embedded, a vanished one is removed.
It runs at most once per REFRESH_S per project, inside the heal worker —
never on a request path.

Degrade, don't fail: a hub that will not give rules, an embedder that does
not answer, or any other fault logs loudly and heal runs without eyes.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time

from . import store

SECTION_CHARS = 900
EACH, CAP = 3, 8            # nearest per arriving record; at most, per call
REFRESH_S = 600

DDL = """
CREATE TABLE IF NOT EXISTS filed_section (
    project text NOT NULL,
    sha text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    text text NOT NULL,
    embedding halfvec(768),
    PRIMARY KEY (project, sha)
);
"""

_last: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def pieces(body: str, cap: int = SECTION_CHARS):
    """(heading, text) pieces of a markdown document: cut at its headings,
    then packed paragraph by paragraph up to `cap` characters."""
    head = ""
    for block in re.split(r"\n(?=#{1,4} )", body or ""):
        lines = block.strip().split("\n")
        if lines and lines[0].startswith("#"):
            head = lines[0].lstrip("# ").strip()
        buf = ""
        for p in [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]:
            if buf and len(buf) + len(p) > cap:
                yield head, buf
                buf = ""
            buf = (buf + "\n\n" + p).strip() if buf else p[:cap * 2]
        if buf:
            yield head, buf


SHARED = "*"      # the every-project sections, stored once per tenant


def _wanted(project: str) -> dict[str, list[dict]] | None:
    """{project: its own sections, SHARED: the every-project ones}, or None
    when the rules could not be had (then the stored ones stand — never
    wiped for a passing hub fault). Own: the project's rules and the
    playbooks named for it ("<project>-…"). Shared: the every-project rules
    and every playbook named for no project."""
    from . import rules_hub
    got = rules_hub.fetch(project)
    if got.get("note"):
        return None
    out: dict[str, list[dict]] = {project: [], SHARED: []}
    for r in got.get("rules") or []:
        area = r.get("area") or "all"
        if area in ("all", project) and str(r.get("content") or "").strip():
            out[SHARED if area == "all" else project].append(
                {"kind": "rule", "name": str(r.get("title") or r.get("rule_id") or ""),
                 "text": str(r.get("content") or "")})
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM project;")
        names = {n for (n,) in cur.fetchall() if n} | {project}
        books: list = []
        if _has(cur, "playbook"):
            cur.execute("SELECT name, content FROM playbook ORDER BY name;")
            books = cur.fetchall()
        conn.rollback()
    for name, body in books:
        scope = scope_of(name, project, names)
        if scope is None:
            continue                      # another project's playbook
        for head, text in pieces(body or ""):
            out[scope].append({"kind": "playbook", "name": f"{name} › {head}" if head else name, "text": text})
    return out


def scope_of(playbook: str, project: str, projects: set[str]) -> str | None:
    """Where a playbook's sections belong for `project`: its own scope when
    named for it ("<project>-…"), SHARED when named for no project or for
    the every-project layer ("global-…"), None when it is another
    project's. The longest matching project name decides ("ms-map" is ms's
    even when a project "m" exists)."""
    owner = max((n for n in projects | {project} if playbook.startswith(n + "-")), key=len, default=None)
    if owner is None or owner == "global":
        return SHARED
    return project if owner == project else None


def _has(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", [table])
    return bool(cur.fetchone()[0])


def refresh(project: str, force: bool = False) -> dict:
    """Bring the project's sections, and the shared ones, in line with its
    rules and playbooks. {scope: {"kept", "added", "removed"}} or
    {"skipped": why}."""
    key = (store.current_tenant(), project)
    with _lock:
        if not force and time.time() - _last.get(key, 0) < REFRESH_S:
            return {"skipped": "fresh"}
        _last[key] = time.time()
    want = _wanted(project)
    if want is None:
        print(f"[filed] {project}: rules could not be fetched — heal keeps the sections it has", flush=True)
        return {"skipped": "no rules"}
    res = {}
    for scope, secs in want.items():
        skey = (key[0], scope)
        if scope == SHARED and not force:
            with _lock:
                if scope != project and time.time() - _last.get(skey, 0) < REFRESH_S:
                    continue
                _last[skey] = time.time()
        res[scope] = _sync(scope, secs, key)
    return res


def _sync(scope: str, secs: list[dict], key: tuple) -> dict:
    from . import embed, recall_pg
    by_sha = {}
    for s in secs:
        by_sha[hashlib.sha256(f"{s['kind']}\n{s['name']}\n{s['text']}".encode()).hexdigest()[:32]] = s
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "filed_section", DDL)
        cur.execute("SELECT sha FROM filed_section WHERE project = %s;", [scope])
        have = {r[0] for r in cur.fetchall()}
        gone = have - set(by_sha)
        if gone:
            cur.execute("DELETE FROM filed_section WHERE project = %s AND sha = ANY(%s);", [scope, list(gone)])
        conn.commit()
    new = [(sha, s) for sha, s in by_sha.items() if sha not in have]
    added = 0
    for i in range(0, len(new), 32):
        chunk = new[i:i + 32]
        vecs = embed.embed([f"{s['name']}. {s['text']}" for _, s in chunk], timeout=180.0, kind="document")
        if not vecs or len(vecs) != len(chunk):
            print(f"[filed] {scope}: embedding failed for {len(chunk)} sections — they wait for the next refresh",
                  flush=True)
            with _lock:
                _last[key] = 0            # try again next heal
                _last[(key[0], scope)] = 0
            break
        with store.connection() as conn:
            cur = conn.cursor()
            for (sha, s), v in zip(chunk, vecs):
                cur.execute("INSERT INTO filed_section (project, sha, kind, name, text, embedding) "
                            "VALUES (%s, %s, %s, %s, %s, %s::halfvec) ON CONFLICT DO NOTHING;",
                            [scope, sha, s["kind"], s["name"][:300], s["text"], recall_pg._vec_lit(v)])
            conn.commit()
        added += len(chunk)
    res = {"kept": len(have) - len(gone), "added": added, "removed": len(gone)}
    if added or gone:
        print(f"[filed] {scope}: sections refreshed {res}", flush=True)
    return res


def near(project: str, vecs: list, each: int = EACH, cap: int = CAP) -> list[dict]:
    """The sections nearest to the arriving records: each record's `each`
    nearest, best first, `cap` at most — as {kind, name, text}."""
    from . import recall_pg
    best: dict[str, tuple[float, dict]] = {}
    with store.connection() as conn:
        cur = conn.cursor()
        if not _has(cur, "filed_section"):
            conn.rollback()
            return []
        for v in vecs:
            if not v:
                continue
            lit = recall_pg._vec_lit(v)
            cur.execute("SELECT sha, kind, name, text, embedding <=> %s::halfvec AS d FROM filed_section "
                        "WHERE project = ANY(%s) AND embedding IS NOT NULL ORDER BY d LIMIT %s;",
                        [lit, [project, SHARED], each])
            for sha, kind, name, text, d in cur.fetchall():
                if sha not in best or d < best[sha][0]:
                    best[sha] = (float(d), {"kind": kind, "name": name, "text": text})
        conn.rollback()
    return [s for _, s in sorted(best.values(), key=lambda t: t[0])[:cap]]


def for_heal(project: str, record_ids: list[str]) -> list[dict]:
    """The eyes of one heal call: refresh when due, then the sections
    nearest to these records. [] on any fault, said loudly."""
    from . import embed
    try:
        refresh(project)
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT record_id, title, content, embedding::text FROM document "
                        "WHERE project = %s AND record_id = ANY(%s);", [project, record_ids])
            rows = cur.fetchall()
            conn.rollback()
        vecs, todo = [], []
        for rid, title, content, emb in rows:
            if emb:
                vecs.append([float(x) for x in emb.strip("[]").split(",")])
            else:
                todo.append(f"{title}. {content}")
        if todo:
            got = embed.embed(todo, timeout=90.0, kind="document") or []
            vecs += [v for v in got if v]
        return near(project, vecs)
    except Exception as e:                                     # noqa: BLE001
        print(f"[filed] {project}: heal runs without eyes — {type(e).__name__}: {e}", flush=True)
        return []
