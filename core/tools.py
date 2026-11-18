"""The MCP tool surface — 15 tools, same names and same arguments as v1.

Identical signatures are the point: the client's tool definitions do not change,
so switching a session from v1 to v2 is a base-URL change and nothing else.

THREE THINGS ARE DELIBERATELY BETTER THAN v1, not merely ported:

1. **A failed embed is queued, never lost.** v1 wrote `embedding = NONE` when
   the sidecar timed out and nothing ever revisited it — that is how 30% of the
   chat archive ended up unsearchable. Here a save still succeeds with a NULL
   embedding, and the partial index means the drainer picks it up. The tool says
   so in its reply rather than reporting a clean success.

2. **`query_raw` is read-only and bounded.** In v1 it was full SurrealQL on a
   customer key: it leaked the whole schema and one bad aggregate could OOM the
   box for every tenant — which already caused a 20-minute outage. Here it is
   SELECT/WITH only, with a statement timeout and a row cap, so the worst case
   is one slow query for one tenant.

3. **`recent_records`' `where` is parsed, not interpolated.** v1 spliced the
   caller's fragment straight into the query.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
import os
import re
import traceback

from . import archive, embed, recall_pg, store

_VALID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ID = re.compile(r"^[A-Za-z0-9_]{1,120}$")
EMBED_CONTENT_CAP = embed.CONTENT_CAP     # ONE cap, owned by embed.py
RAW_ROW_CAP = 500
RAW_TIMEOUT_MS = 5000
# Near-duplicate trigger for the merge lane, MEASURED 2026-08-09 on the ms
# store's real duplicate pairs: true dupes sat at 0.009-0.17 cosine distance,
# genuinely-distinct neighbors started at 0.10. No single cut separates them,
# so the trigger is deliberately WIDE and the merge model holds the
# "genuinely distinct" verdict — a false trigger costs one cheap model call,
# never a wrong merge.


def _resolve(record_id: str, project: str = "") -> tuple[str, str]:
    """'mind:foo' -> ('mind','foo'); 'foo' + project -> (project,'foo')."""
    rid = (record_id or "").strip()
    if ":" in rid:
        p, k = rid.split(":", 1)
    else:
        p, k = (project or "").strip(), rid
    p, k = p.strip().lower(), k.strip().strip("`⟨⟩")
    if not _VALID.match(p):
        raise ValueError(f"bad project '{p}' (pass project= or use 'project:id')")
    if not _ID.match(k):
        raise ValueError(f"bad record id '{k}'")
    return p, k


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fmt(r: dict) -> str:
    content = " ".join((r.get("content") or "").split())[:200]
    return (f"[{r['project']}/{r.get('type','')}] {r['project']}:{r['record_id']} — "
            f"{r.get('title') or ''}: {content} "
            f"(upd {str(r.get('updated_at') or '')[:10]})")


# ── search ────────────────────────────────────────────────────────────────
def recall(query_text: str, project: str = "", top_k: int = 10,
           include_chat: bool = True, since: str = "", until: str = "") -> str:
    from . import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    vec = embed.embed_one(query_text, timeout=12.0)
    with store.connection() as conn:
        return recall_pg.recall_text(conn, vec, query_text, project=project,
                                     top_k=top_k, include_chat=include_chat,
                                     since=s, until=u)


def save_verdicts(items: list, since: str, until: str = "", judged_by: str = "") -> str:
    """Keep a judge's calls on a period for the next run to re-judge."""
    from . import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    with store.connection() as conn:
        n, problems = period.save_verdicts(conn.cursor(), items, s, u, judged_by)
        conn.commit()
    return f"Saved {n} verdict(s)." + ("" if not problems else " Not saved: " + "; ".join(problems))


def list_verdicts(since: str = "", until: str = "", project: str = "") -> str:
    """Earlier calls on periods overlapping since–until, each item's history together."""
    from . import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    p = (project or "").strip().lower()
    with store.connection() as conn:
        rows = period.list_verdicts(conn.cursor(), [p] if p else None, s, u)
    return "\n".join(rows) if rows else "No verdicts kept for this period yet."


def period_report(since: str, until: str = "", project: str = "") -> str:
    """What a period produced, counted in the memory — facts only."""
    from . import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    if not s:
        return "Error: since is required (YYYY-MM-DD)."
    p = (project or "").strip().lower()
    with store.connection() as conn:
        return period.report(conn.cursor(), [p] if p else None, s, u)


def semantic_search(query_text: str, project: str = "", top_k: int = 5) -> str:
    vec = embed.embed_one(query_text, timeout=12.0)
    if not vec:
        return "Embedder unavailable — use search_memory (keyword) instead."
    where, args = ("AND project = %s", [project]) if project else ("", [])
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT project, record_id, type, title, content, updated_at, "
            f"       embedding <=> %s::halfvec AS dist FROM document "
            f"WHERE embedding IS NOT NULL {where} "
            f"ORDER BY embedding <=> %s::halfvec LIMIT %s;",
            [recall_pg._vec_lit(vec)] + args + [recall_pg._vec_lit(vec), top_k])
        rows = _rows(cur)
    return "\n".join(f"[{r['dist']:.3f}] {_fmt(r)}" for r in rows) or "No hits."


def search_memory(query_text: str, project: str = "") -> str:
    kws = recall_pg.fts_keywords(query_text)
    if not kws:
        return "No searchable keywords in that query."
    where, args = ("AND project = %s", [project]) if project else ("", [])
    with store.connection() as conn:
        cur = conn.cursor()
        # _idf_fts answers (rows, evidence) since 2026-08-31; iterating the
        # pair walked a float (2026-09-11: "'float' object is not iterable")
        rows, _ = recall_pg._idf_fts(
            cur, "document", kws, where, args, 20,
            "project, record_id, type, title, content, updated_at")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in rows]
    return "\n".join(_fmt(r) for r in rows) or "No hits."


def _chat_scope(cur, project: str) -> tuple[str, list]:
    """The chat rows that are `project`'s TODAY — the archived column
    corrected by the split door's moves (core/ownership, 2026-09-09).
    Returns an "AND …" fragment and its params; nothing for no project."""
    from .chatkind import NOT_RECAP
    # A RECAP IS NEVER SEARCHED (founder 2026-09-22)
    if not project:
        return f"AND {NOT_RECAP}", []
    from . import ownership
    w, a = ownership.chat_where(cur, project)
    return f"AND ({w}) AND {NOT_RECAP}", list(a)


def search_chat(query_text: str, project: str = "", top_k: int = 8) -> str:
    from . import ownership
    vec = embed.embed_one(query_text, timeout=12.0)
    if not vec:
        return "Embedder unavailable — use search_chat_text instead."
    lit = recall_pg._vec_lit(vec)
    with store.connection() as conn:
        cur = conn.cursor()
        where, args = _chat_scope(cur, project)
        cur.execute(
            f"SELECT project, role, session_id, text, ts, "
            f"       embedding <=> %s::halfvec AS dist FROM chat "
            f"WHERE embedding IS NOT NULL {where} "
            f"ORDER BY embedding <=> %s::halfvec LIMIT %s;",
            [lit] + args + [lit, top_k])
        rows = ownership.relabel(cur, _rows(cur))
    return "\n".join(
        f"[{r['dist']:.3f}] [{r['project']}/{r['role']}] {str(r['ts'])[:16]} "
        f"{(r['session_id'] or '')[:8]}: {' '.join((r['text'] or '').split())[:240]}"
        for r in rows) or "No hits."


def search_chat_text(query_text: str, project: str = "", top_k: int = 12) -> str:
    """EXACT strings — paths, errors, hashes, flags. `websearch_to_tsquery`
    keeps quoted phrases together, which is the whole reason this tool exists
    alongside search_chat."""
    from . import ownership
    with store.connection() as conn:
        cur = conn.cursor()
        where, args = _chat_scope(cur, project)
        cur.execute(
            f"SELECT project, role, session_id, text, ts FROM chat "
            f"WHERE tsv @@ websearch_to_tsquery('simple', %s) {where} "
            f"ORDER BY ts DESC LIMIT %s;", [query_text] + args + [top_k])
        rows = ownership.relabel(cur, _rows(cur))
    return "\n".join(
        f"[{r['project']}/{r['role']}] {str(r['ts'])[:16]} "
        f"{(r['session_id'] or '')[:8]}: {' '.join((r['text'] or '').split())[:240]}"
        for r in rows) or "No hits."


# ── read ──────────────────────────────────────────────────────────────────
def get_memory(record_id: str, project: str = "") -> str:
    try:
        p, k = _resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT project, record_id, type, title, content, source, "
                    "captured_at, updated_at, meta FROM document "
                    "WHERE project = %s AND record_id = %s;", [p, k])
        rows = _rows(cur)
    if not rows:
        return f"No record {p}:{k}."
    r = rows[0]
    # meta is flattened INTO the payload so a caller sees exactly the fields v1
    # returned — pin/area/status/platform and the rest live there now.
    out = {"id": f"{p}:{k}", "type": r["type"], "title": r["title"],
           "content": r["content"], "source": r["source"],
           "captured_at": str(r["captured_at"] or ""),
           "updated_at": str(r["updated_at"] or "")}
    out.update(r.get("meta") or {})
    return json.dumps(out, indent=1, ensure_ascii=False, default=str)


def session_context(project: str) -> str:
    """The complete session-start payload, on demand — the recovery path for
    the harness dropping a '(context N/M)' part (seen live 2026-08-03: both
    SessionStart hooks succeeded, the desktop app injected only one)."""
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    from . import injection            # lazy: tools loads before injection
    return injection.session_blob(project, 0)


def load_project(project: str) -> str:
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT project, record_id, type, title, content, updated_at "
                    "FROM document WHERE project = %s "
                    "ORDER BY updated_at DESC LIMIT 60;", [project])
        rows = _rows(cur)
    return "\n".join(_fmt(r) for r in rows) or f"No records in '{project}'."


def list_memory(project: str, memory_type: str = "", limit: int = 50) -> str:
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    where, args = ("AND type = %s", [memory_type]) if memory_type else ("", [])
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT project, record_id, type, title, updated_at "
                    f"FROM document WHERE project = %s {where} "
                    f"ORDER BY updated_at DESC LIMIT %s;",
                    [project] + args + [max(1, min(int(limit), 500))])
        rows = _rows(cur)
    return "\n".join(f"{r['project']}:{r['record_id']} [{r['type']}] "
                     f"{r['title']} (upd {str(r['updated_at'])[:10]})"
                     for r in rows) or "No records."


_WHERE_RE = re.compile(r"^\s*([A-Za-z0-9_]{1,40})\s*=\s*(.+?)\s*$")


def recent_records(project: str, days: int = 0, where: str = "",
                   limit: int = 50) -> str:
    """`where` accepts ONE `field=value` filter, parsed — never interpolated.
    v1 spliced the caller's fragment into the query text directly."""
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    sql = ["SELECT project, record_id, type, title, content, updated_at "
           "FROM document WHERE project = %s"]
    args: list = [project]
    if days and int(days) > 0:
        sql.append("AND updated_at > now() - make_interval(days => %s)")
        args.append(int(days))
    if where:
        m = _WHERE_RE.match(where)
        if not m:
            return "Error: `where` must be a single field=value filter."
        field, val = m.group(1), m.group(2).strip().strip("'\"")
        if field in ("type", "title", "record_id", "source"):
            sql.append(f"AND {field} = %s")
        else:
            sql.append("AND meta->>%s = %s")
            args.append(field)
        args.append(val)
    sql.append("ORDER BY updated_at DESC LIMIT %s")
    args.append(max(1, min(int(limit), 500)))
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(" ".join(sql), args)
        rows = _rows(cur)
    return "\n".join(_fmt(r) for r in rows) or "No records."


_RAW_OK = re.compile(r"^\s*(select|with)\b", re.I)
_RAW_BAD = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|reindex|pg_read_file|pg_ls_dir|dblink|lo_import|lo_export)\b", re.I)


_RAW_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _outside_literals(q: str) -> str:
    """The query with its string literals blanked out.

    THE GUARDS READ SQL, NOT PROSE (2026-09-10). Both checks below used to
    run over the raw text, so a perfectly read-only query was refused for
    the words INSIDE its own quotes: `... WHERE text ILIKE '%delete%'` came
    back as "no writes, no DDL", and `SELECT 'a;b'` as "one statement only".
    An engineering memory is FULL of those words — delete_project, CREATE
    TABLE, a sed expression with a semicolon — so the agent met a dead end
    with a reason that was not true. Blanking the literals first keeps the
    guard exactly as strict about SQL and silent about content. (The guard
    was never the only safety: the statement runs inside a subquery, under a
    timeout and a row cap, and the transaction is rolled back either way.)"""
    return _RAW_LITERAL.sub("''", q or "")


def query_raw(sql: str) -> str:
    """Read-only SQL, bounded. See the module docstring for why v1's version
    was a shared-fate risk rather than just a schema leak."""
    q = (sql or "").strip().rstrip(";")
    bare = _outside_literals(q)
    if not _RAW_OK.match(bare) or _RAW_BAD.search(bare):
        return "Error: query_raw is SELECT/WITH only — no writes, no DDL."
    if ";" in bare:
        return "Error: one statement only."
    with store.connection() as conn:
        cur = conn.cursor()
        try:
            # Timeout + row cap: one tenant's bad aggregate must not become
            # everyone's outage, which is exactly what happened on v1.
            cur.execute(f"SET LOCAL statement_timeout = {RAW_TIMEOUT_MS};")
            cur.execute(f"SELECT * FROM ({q}) _q LIMIT {RAW_ROW_CAP};")
            rows = _rows(cur)
        except Exception as e:
            conn.rollback()
            return f"Query error: {str(e).splitlines()[0][:200]}"
        conn.rollback()          # read-only: never leave a transaction open
    if not rows:
        return "(no rows)"
    out = "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows)
    if len(rows) >= RAW_ROW_CAP:
        out += f"\n-- truncated at {RAW_ROW_CAP} rows"
    return out


def memory_health(project: str = "") -> str:
    """Coverage per project, plus the embedding backlog — the number that went
    unwatched in v1 until a third of the archive was invisible."""
    where, args = ("WHERE project = %s", [project]) if project else ("", [])
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT project, count(*) AS records, "
                    f"       count(embedding) AS embedded FROM document {where} "
                    f"GROUP BY project ORDER BY count(*) DESC;", args)
        docs = _rows(cur)
        cur.execute(f"SELECT project, count(*) AS rows, "
                    f"       count(embedding) AS embedded FROM chat {where} "
                    f"GROUP BY project ORDER BY count(*) DESC;", args)
        chats = _rows(cur)
    lines = ["curated (records / embedded):"]
    lines += [f"  {r['project']:<14} {r['records']:>6} / {r['embedded']:>6}"
              + ("   <-- BACKLOG" if r["embedded"] < r["records"] else "")
              for r in docs]
    lines.append("chat (rows / embedded):")
    lines += [f"  {r['project']:<14} {r['rows']:>6} / {r['embedded']:>6}"
              + ("   <-- BACKLOG" if r["embedded"] < r["rows"] else "")
              for r in chats]
    return "\n".join(lines)


# ── write ─────────────────────────────────────────────────────────────────
_LINK_RE = re.compile(r"\[\[([A-Za-z0-9_:]+)\]\]")


def _link_edges(conn, p: str, k: str, content: str) -> int:
    links = list(dict.fromkeys(_LINK_RE.findall(content or "")))[:20]
    cur = conn.cursor()
    cur.execute("DELETE FROM edge WHERE src_project=%s AND src_record=%s "
                "AND kind='refs';", [p, k])
    n = 0
    for l in links:
        try:
            lp, lk = _resolve(l, p)
        except ValueError:
            continue
        cur.execute("INSERT INTO edge (src_project, src_record, dst_project, "
                    "dst_record, kind) VALUES (%s,%s,%s,%s,'refs') "
                    "ON CONFLICT DO NOTHING;", [p, k, lp, lk])
        n += 1
    return n


def _embed_or_queue(text: str) -> tuple[str | None, str]:
    """Returns (halfvec literal or None, note). A failed embed is NOT an error:
    the row saves with NULL and the drainer catches it. The note is what stops
    that being a silent degradation."""
    if len(text) > EMBED_CONTENT_CAP:
        # Longer than one model window: NOT a degradation — the drainer
        # embeds ALL its windows (row vector + chunk vectors) within
        # seconds, which is strictly better than an inline first-window-
        # only vector. No note; nothing is lost.
        try:
            from core import drainer
            drainer.note_work_later(store.current_tenant(), 5.0)
        except Exception:
            pass
        return None, ""
    # interactive=True: an agent is waiting and the near-dup MERGE CHECK
    # needs this vector NOW — query-class admission at the sidecar, so our
    # own drainer can never shed a customer's save (gate caught the fork
    # live during the 2026-08-12 re-embed drain).
    vec = embed.embed_one(text[:EMBED_CONTENT_CAP], kind="document",
                          timeout=12.0, interactive=True)
    if vec:
        return recall_pg._vec_lit(vec), ""
    # poke_later, not poke: the caller has not committed the NULL row yet.
    try:
        from core import drainer
        drainer.note_work_later(store.current_tenant(), 5.0)
    except Exception:
        pass
    return None, ("\nNOTE: embedder unavailable — saved WITHOUT a vector and "
                  "queued; the drainer will embed it. Keyword search works now, "
                  "meaning search will once it drains.")


# These two sit ABOVE _kind_hint on purpose: twice (2026-09-02, both times)
# a slice that removed a dead function above them ran "to the next def" and
# took them too, and save_memory raised after every write. Keep them here,
# right next to their only reader.
_NORMATIVE = re.compile(
    r"\b(never|always|must|do not|don't|forbidden|required|only ever|"
    r"under no circumstances)\b", re.I)
_FINDING = re.compile(
    r"\b(cfg|steps?|seed|strength|denoise|resolution|\d+x\d+|px|tokens?/s|"
    r"[\d.]+\s?(gb|mb|ms|s|%)|v\d+\.\d+|measured|benchmark|throughput)\b",
    re.I)


def _kind_hint(title: str, body: str, as_rule: bool,
               rule_tool: str = "save_rule",
               data_tool: str = "save_memory") -> str:
    """Tell the caller which KIND it just wrote, and flag the likely mix-up.

    Founder 2026-08-07: agents must be told what is a rule and what is data
    AT THE MOMENT OF SAVING — the MCP handshake carries the same lesson but
    a stateless server cannot guarantee any agent ever read it."""
    text = f"{title} {body}"
    if as_rule:
        if _FINDING.search(text) and not _NORMATIVE.search(text):
            return ("\n⚠ This reads like a MEASURED FINDING (settings, "
                    "numbers, a result), not a standing constraint. Law is "
                    "injected into every session forever and teaches the "
                    "curator; a finding belongs in the conversation — say it "
                    "and the curator files it, found when it is needed. "
                    "Delete this rule if that is what it is.")
        return ""
    if _NORMATIVE.search(text) and len(body) < 1200:
        return ("\nNOTE: filed as KNOWLEDGE (found when searched). If it "
                "must bind behavior in EVERY session without being looked "
                f"up, that is a RULE — {rule_tool}.")
    return ""


_NOT_A_RULE_DOOR = (
    "Error: writing a RULE goes through save_rule(), not this tool.\n"
    "THE TEST: a RULE changes what an agent DOES in every session, without "
    "looking anything up — a standing constraint. A setting, a measured "
    "result, a config, a 'this is how X behaves' — that is KNOWLEDGE: say "
    "it in the conversation and the curator files it, found when it is "
    "needed.\n"
    "If it really is a standing constraint: save_rule(rule_id=..., "
    "title=..., rule=..., project=<when known>) — it asks one question, "
    "then files it where it belongs."
)

# ── the rule question gate (founder design 2026-08-14) ─────────────────────
# One door, one question. The agent is not handed a scope PARAMETER to fill
# mechanically — where the text lands is the OUTCOME of a question it must
# answer in a second call. Advisory text demonstrably does not work (the
# _kind_hint nag was ignored eight times in one day by the strongest model
# available); a forced choice in a separate turn is the mechanism that makes
# the agent actually think. The token binds the answer to the exact content
# asked about, so the question cannot be skipped by answering pre-emptively:
# it is an HMAC over the content, stateless on purpose — nothing to store,
# nothing to expire, safe across workers.
_GATE_KEY = (os.environ.get("BRAIN_INTERNAL_TOKEN")
             or "mind-rule-gate-v1").encode("utf-8")


def _gate_tok(*parts: str) -> str:
    return hmac.new(_GATE_KEY, "|".join(parts).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


def _content_sig(*texts: str) -> str:
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:16]


def rule_question(tok: str, project: str = "",
                  tool: str = "save_rule") -> str:
    """THE question — shared verbatim by the owner and customer doors."""
    p = f" ('{project}')" if project else " (pass project=<name>)"
    return (
        "NOT saved yet — first, one question.\n\n"
        "Does this text need to be LOADED INTO EVERY SESSION's context?\n"
        "  1 — always, in EVERY project the user runs\n"
        f"  2 — always, in THIS project{p}\n"
        "  3 — no: it only needs to be FOUND when it is searched for\n\n"
        f"Answer by calling {tool} again with the same arguments plus "
        f"answer='1'|'2'|'3' and token='{tok}'.")


def save_rule(scope: str = "", rule_id: str = "", title: str = "",
              rule: str = "", project: str = "", pin: bool = True,
              answer: str = "", token: str = "", merge: str = "") -> str:
    """SAVE A RULE — the one door, gated by ONE question (founder 2026-08-14).

    Call 1 (no answer): returns the question + a token.
    Call 2 (answer + token): 1 → general law, 2 → this project's law,
    3 → it was never a rule; filed as knowledge, costing no session.
    If a very similar rule already exists, one more question comes back:
    merge into it (resubmit the merged text under the EXISTING rule_id with
    merge='yes') or keep separate (repeat the call with merge='no').

    `scope` is accepted for backward compatibility and IGNORED — the answer
    decides. Rules were once written by parameter-filling; fourteen Krea2
    workflow configs became injected law that way.
    """
    if not (rule_id or "").strip() or not _ID.match(rule_id.strip()):
        return f"Error: bad rule_id '{rule_id}'."
    rule_id = rule_id.strip()
    if not (title or "").strip() or not (rule or "").strip():
        return "Error: title and rule are both required."
    proj = (project or "").strip().lower()
    q1 = _gate_tok("q1", rule_id, _content_sig(title, rule))

    a = (answer or "").strip()
    if not a:
        return rule_question(q1, proj)
    if a not in ("1", "2", "3"):
        return "Error: answer must be '1', '2' or '3'."

    from . import hubclient, rules_hub
    m = (merge or "").strip().lower()
    if m and m not in ("yes", "no"):
        return "Error: merge must be 'yes' or 'no'."
    if not m and not hmac.compare_digest(token or "", q1):
        return ("Error: bad token — call save_rule without answer= to get "
                "the question and its token.")

    if a == "3":
        # The agent chose it: this text is knowledge, not law. File it where
        # it will be FOUND, in the project it was born in.
        p = proj if proj and proj not in ("rules", "state") else "global"
        res = write_record(p, rule_id, "fact", title, rule)
        if res.startswith("Error"):
            return res
        return ("Filed as KNOWLEDGE, not law — found when searched, costs "
                "no session context. " + res)

    area = "all" if a == "1" else proj
    if a == "2":
        if not area:
            return "Error: answer 2 (this project) needs project=<name>."
        if not _VALID.match(area):
            return f"Error: bad project name '{area}'."

    # THE MERGE QUESTION (Q2, conditional): a very similar rule in the same
    # pool blocks the save with a directed choice. The near-identical band
    # stays a plain refusal inside rulecap (the async gate parses its exact
    # wording); this question covers the merely-similar band that used to be
    # a passive NOTE — which was ignorable, and ignored.
    twin = None
    law = rules_hub.fetch("")
    if not any(r.get("rule_id") == rule_id for r in law["rules"]):
        # a new id: is there a rule in the same pool that is nearly the same?
        # By words, over the set the hub holds — nothing is stored here.
        mine = {w for w in re.findall(r"[a-z0-9]+", f"{title} {rule}".lower())
                if len(w) >= 5}
        best, score = None, 0.0
        for r in law["rules"]:
            if r.get("seed") or (r.get("area") or "all") != area:
                continue
            theirs = {w for w in re.findall(
                r"[a-z0-9]+", f"{r.get('title') or ''} {r.get('content') or ''}".lower())
                if len(w) >= 5}
            if not mine or not theirs:
                continue
            j = len(mine & theirs) / len(mine | theirs)
            if j > score:
                best, score = r, j
        if best is not None and score >= 0.45:
            twin = (str(best["rule_id"]), str(best.get("title") or ""),
                    str(best.get("content") or ""))

    if m:
        expected = (_gate_tok("mg", rule_id, a) if m == "yes" else
                    (_gate_tok("mg", twin[0], a) if twin else ""))
        if not (expected and hmac.compare_digest(token or "", expected)):
            return ("Error: bad or stale merge token — call save_rule "
                    "without answer= to restart.")
    elif twin:
        mtok = _gate_tok("mg", twin[0], a)
        return (
            "NOT saved — one more question. The Brain already has this "
            "rule:\n"
            f"  rules:{twin[0]} — '{twin[1][:80]}'\n"
            f"  {twin[2][:600]}\n"
            "It is very similar to the rule you are adding. Do you want to "
            "MERGE them, to save the context and tokens of every session?\n"
            f"  MERGE: call save_rule again with rule_id='{twin[0]}', the "
            f"merged text as ONE rule (cap 1000 chars), the "
            f"same answer, merge='yes', token='{mtok}'.\n"
            "  KEEP SEPARATE: repeat your original call with merge='no', "
            f"token='{mtok}'.")

    if len(rule) > 1000:
        return (f"Error: rule exceeds 1000 chars ({len(rule)}) — merge or "
                f"trim it. The cap is what forces consolidation.")
    try:
        d = hubclient.put_rule(rule_id, title.strip(), rule.strip(), area, pin)
        if d.get("ok"):
            # its vector, so the prompt hook can match it semantically
            from . import embed
            v = embed.embed_one(f"{title.strip()} [rule] {rule.strip()}", kind="document", timeout=20.0)
            if v:
                hubclient.put_rule_vectors({rule_id: v})
    except hubclient.HubRefused as e:
        return f"Error: the hub refused the rule — {e}"
    except hubclient.HubUnavailable as e:
        return (f"Error: the hub is unreachable ({e}); rules live only on the "
                f"hub, so nothing was saved. Try again shortly.")
    if not d.get("ok"):
        return f"Error: the hub refused the rule — {d.get('why')}"
    rules_hub.forget()
    scope_line = ("\nThis rule binds EVERY project — it costs every session "
                  "everywhere." if area == "all" else
                  f"\nThis rule binds '{area}' only.")
    return (f"Saved rule {rule_id} on the hub." + scope_line
            + _kind_hint(title, rule, as_rule=True))


def _no_nul(text):
    """Postgres text cannot hold 0x00, and one stray byte fails the whole
    statement with an opaque "memory backend error".

    core/archive.archive_turns already made this choice for the chat path —
    strip rather than refuse, because a binary paste or a truncated file must
    not cost a turn. Records answer the same byte the same way. This lives in
    ONE function because the previous version guarded save_record only, and
    save_memory writes on its own path: a guard on one door covers only the
    callers who use that door (the same shape as the seed-rule sweep bug the
    same day)."""
    return text.replace("\x00", "") if isinstance(text, str) else text


def write_record(project: str, record_id: str, memory_type: str,
                title: str, content: str, dedup: bool = True,
                origin: dict | None = None) -> str:
    """`origin` (2026-09-09): where this record came from — {"session_id",
    "turn_lo", "turn_hi"} of the piece the curator read — kept under
    meta.origin so a record points at its turns in the archive. A rewrite
    of an existing record without an origin keeps the one it has."""
    title, content = _no_nul(title), _no_nul(content)
    meta_add = json.dumps({"origin": origin}) if origin else None
    try:
        p, k = _resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    if p == "rules" and k != "memory_index":
        return _NOT_A_RULE_DOOR
    # TRIPWIRE, not a guard (curation_truncation_bug_2026_08_09): a record
    # whose LONG content is exactly its title carries no information beyond
    # the title — the shape of the one confirmed silent-data-loss stub. Short
    # title==content is legitimate (the gate derives titles from short
    # facts), so only the long twin is suspect. Save proceeds; the log line
    # turns the next occurrence into evidence with a writer attached.
    if (content or "").strip() == (title or "").strip() \
            and len((content or "").strip()) > 100:
        print(f"[tools] SUSPECT STUB SAVE content==title "
              f"({len(content.strip())} chars) at {p}:{k}", flush=True)
    lit, note = _embed_or_queue(f"{title} [{memory_type}] {content}")
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM project WHERE name=%s;", [p])
        new_project = not cur.fetchone()
        archive.ensure_project(conn, p)
        # NEAR-DUPLICATE on CREATE: the door ACTS, it does not advise.
        # (2026-08-09: the old door appended "very similar record — UPDATE it
        # instead" to a save that succeeded anyway. The advisory was ignored
        # eight times in one day by the strongest model available; text a
        # model can skim is not a mechanism. Now the save goes to the merge
        # lane: a small model updates the existing record in place, or files
        # the incoming one under its own id if genuinely distinct. Scan is
        # SAME-PROJECT only — the old scan compared across all projects, the
        # scoping bug rulecap had. `dedup=False` is the merge worker's own
        # write path; it is not exposed in the MCP schema. A save whose
        # embedding got queued (lit None) skips the check — heal's near-dup
        # pass catches anything that slips through embedder downtime.)
        # THE MERGE DOOR IS GONE (founder 2026-08-29), and the measurement
        # is why: every real twin sat at 0.24-0.31 vector distance while this
        # fired at 0.20, so it caught none of the ones that mattered. Our own
        # granularity fix caused that — short single-fact records about one
        # thing land further apart than the long blobs they replaced, so
        # making records better pushed their duplicates out of the detector's
        # reach. And duplicates arrive in CLUSTERS (three records about one
        # measurement); a pairwise door can only ever fuse two and leaves the
        # third standing as a fork.
        #
        # Fast heal reads the whole cluster at the end of a session and folds
        # it properly. A door costing 1,448 tokens a catch, catching the wrong
        # things, is worse than no door.
        cur.execute(
            "INSERT INTO document (project, record_id, type, title, content, "
            "  source, embedding, captured_at, updated_at, meta) "
            "VALUES (%s,%s,%s,%s,%s,'conversation',%s::halfvec,current_date,now(), "
            "        coalesce(%s::jsonb, '{}'::jsonb)) "
            "ON CONFLICT (project, record_id) DO UPDATE SET "
            "  type=EXCLUDED.type, title=EXCLUDED.title, content=EXCLUDED.content, "
            "  embedding=EXCLUDED.embedding, updated_at=now(), "
            "  meta = CASE WHEN EXCLUDED.meta = '{}'::jsonb THEN document.meta "
            "              ELSE document.meta || EXCLUDED.meta END;",
            [p, k, memory_type, title, content, lit, meta_add])
        linked = _link_edges(conn, p, k, content)
        conn.commit()
    if new_project and p not in ("rules", "state", "global"):
        note += (f"\nNEW PROJECT '{p}' created from this save — memory will "
                 f"guess what it is. One sentence teaches it: "
                 f"add_project('{p}', purpose=\"...\").")
    return (f"Saved {p}:{k}" + (f" (+{linked} [[link]] edges)" if linked else "")
            + note + _kind_hint(title, content, as_rule=False))


def save_record(project: str, record_id: str, fields: str,
                embed_text: str = "", allow_rules: bool = False) -> str:
    """Arbitrary typed fields. Known names map to columns, everything else to
    `meta` — which is why v1's 300+ ad-hoc fields survive here at all.

    allow_rules is NOT exposed in the MCP schema: writing LAW goes through
    save_rule(), so making a rule is a deliberate act and not a side effect
    of knowing the filing convention (founder 2026-08-07)."""
    try:
        p, k = _resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    # RULES ARE NOT A PLACE ON THIS BOX (founder 2026-09-02): the hub is the
    # only store, and save_rule is the only door to it. Nothing under
    # project 'rules' is ever written here, whoever asks.
    if p == "rules" and k != "memory_index":
        return _NOT_A_RULE_DOOR
    try:
        data = json.loads(fields) if isinstance(fields, str) else dict(fields)
        if not isinstance(data, dict):
            raise ValueError("fields must be a JSON object")
    except (ValueError, TypeError) as e:
        return f"Error: bad fields JSON — {e}"

    # CURRENT TRUTH ONLY — no flag survives any door (founder law, restated
    # hard 2026-08-09: "superseded, on-hold and whatever flag doesn't exist
    # in curated records"). A dead record is a DELETED record; state lives in
    # the graph and the chat archive, never as a marker on curated data. The
    # refusal is at the door, in code, because prose in three prompts did not
    # stop the strongest model from writing these.
    _FLAG_KEYS = {"superseded_by", "superseded", "supersedes", "obsolete",
                  "deprecated", "on_hold", "onhold", "parked", "stale",
                  "archived", "flag", "status_flag"}
    banned = _FLAG_KEYS & {str(key).lower() for key in data}
    if banned:
        return (f"Error: NOT saved — {sorted(banned)} are flag fields, and "
                "curated memory holds CURRENT TRUTH ONLY. A record that is no "
                "longer true is DELETED (delete_memory) or replaced "
                "(supersede_memory) — never marked. History lives in the "
                "chat archive and the graph.")

    core = {"type": data.pop("type", "note"), "title": data.pop("title", ""),
            "content": data.pop("content", ""), "source": data.pop("source", None)}
    # STRIP NULs, exactly as the archive path does (core/archive.archive_turns).
    # Postgres text cannot hold 0x00, so one stray byte — from a binary paste,
    # a truncated file, a tool that dumped raw bytes — turned the whole save
    # into "memory backend error", which tells the agent nothing and looks
    # like the database is broken. The archive already chose to strip rather
    # than refuse; a curated record should not answer differently to the same
    # byte. Measured 2026-08-28 by hammering the save path.
    core["title"], core["content"] = _no_nul(core["title"]), _no_nul(core["content"])
    data = {_k: _no_nul(_v) for _k, _v in data.items()}
    # ALWAYS embed something. v1 shipped structured records with no vector at
    # all until 2026-07-25 because nothing derived a text to embed from them.
    text = embed_text or " ".join(
        str(v) for v in [core["title"], core["content"]] + list(data.values())
        if isinstance(v, (str, int, float)))[:EMBED_CONTENT_CAP]
    lit, note = _embed_or_queue(text or k)
    with store.connection() as conn:
        cur0 = conn.cursor()
        cur0.execute("SELECT 1 FROM project WHERE name=%s;", [p])
        new_project = not cur0.fetchone()
        archive.ensure_project(conn, p)
        conn.cursor().execute(
            "INSERT INTO document (project, record_id, type, title, content, "
            "  source, meta, embedding, captured_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::halfvec,current_date,now()) "
            "ON CONFLICT (project, record_id) DO UPDATE SET "
            "  type=EXCLUDED.type, title=EXCLUDED.title, content=EXCLUDED.content, "
            "  meta=document.meta || EXCLUDED.meta, "
            "  embedding=EXCLUDED.embedding, updated_at=now();",
            [p, k, core["type"], core["title"], core["content"], core["source"],
             json.dumps(data, default=str, ensure_ascii=False), lit])
        linked = _link_edges(conn, p, k, str(core["content"]))
        conn.commit()
    if new_project and p not in ("rules", "state", "global"):
        note += (f"\nNEW PROJECT '{p}' created from this save — memory will "
                 f"guess what it is. One sentence teaches it: "
                 f"add_project('{p}', purpose=\"...\").")
    return (f"Saved {p}:{k} ({len(data)} extra fields)"
            + (f" (+{linked} [[link]] edges)" if linked else "") + note
            + (_kind_hint(str(core["title"] or ""), str(core["content"] or ""),
                          as_rule=False) if p != "rules" else ""))


def add_project(project: str, purpose: str = "", rules: str = "") -> str:
    """Deliberate project creation WITH onboarding (founder 2026-08-06:
    'when create project there has to be onboarding... thats the greatest
    miss'). PURPOSE is mandatory — the measured result behind the law: one
    owner purpose sentence flipped an entire compiled brief where all the
    sampling engineering could not (aurora u_*, 2026-08-06).

    A name ending in '%' creates a PATTERN TEMPLATE instead of a project:
    every project matching it inherits ONE compiled curator brief — the
    app-tenant case (a project per end-user must not mean a compile per
    end-user). Templates get no project row; they exist as a purpose rule
    plus, minutes later, a compiled brief keyed by the pattern."""
    p = (project or "").strip().lower()
    template = p.endswith("%")
    stem = p[:-1].rstrip("_") if template else p
    if not stem or not _VALID.match(stem) or "%" in stem:
        return (f"Error: bad project name '{project}' — lowercase letters, "
                "digits and underscores; a template ends in a single '%' "
                "(e.g. 'u_%').")
    if p in ("global", "rules", "state"):
        return f"Error: '{p}' is a reserved name."
    if not (purpose or "").strip():
        return ("Error: a project needs a PURPOSE before it exists — one or "
                "two sentences saying what it IS and what its memory is for, "
                "plus optional rules (always track / never record). Two "
                "shapes to copy:\n"
                "  add_project('payment_api', purpose='Our payment service "
                "and its integrations.', rules='TRACK: API decisions, "
                "incident root causes, config gotchas. NEVER RECORD: routine "
                "test output.')\n"
                "  add_project('acme_client', purpose='Client work for "
                "ACME.', rules='TRACK: deliverables, deadlines, their stated "
                "preferences. NEVER RECORD: draft iterations.')\n"
                "Call again with purpose=\"...\".")
    content = purpose.strip()
    if (rules or "").strip():
        content += f"\n\nCURATE — {rules.strip()}"
    if not template:
        with store.connection() as conn:
            archive.ensure_project(conn, p)
            conn.commit()
    rid = f"purpose_{stem}" + ("_template" if template else "")
    # THE PURPOSE IS A RULE, AND RULES LIVE ONLY ON THE HUB (2026-09-02).
    # This used to write it into the box's rules project through
    # save_record — which the rule-door guard then refused (2698f69), so
    # from that night until 2026-09-03 every add_project answered "writing a
    # RULE goes through save_rule()" and no project got a purpose. Found on
    # the hosted door test. The hub warms the project's brief on a purpose
    # PUT, so the curator reads it on the next exchange.
    from . import hubclient, rules_hub
    try:
        d = hubclient.put_rule(rid, f"★ PROJECT PURPOSE — {p}", content, p,
                               not template)
    except hubclient.HubUnavailable as e:
        return (f"Project '{p}' exists on this box, but its purpose could "
                f"not reach the hub ({e}) — rules live only there. Call "
                f"add_project again with the same purpose when the hub is "
                f"back; nothing else was lost.")
    except hubclient.HubRefused as e:
        return f"Error: the hub refused the purpose: {e}"
    if not d.get("ok"):
        return f"Error: the hub did not take the purpose: {d.get('why')}"
    rules_hub.forget()
    if template:
        return (f"Template '{p}' recorded — every project matching it "
                f"inherits one compiled curator brief (compile queued). "
                f"Projects like '{p.replace('%', 'alice')}' are covered the "
                f"moment they exist.")
    return (f"Project '{p}' is ready and onboarded — its purpose is on the "
            f"hub and its curator reads it from the next exchange.\n"
            f"Note: what you set here are PROJECT rules — '{p}''s own rules "
            f"only. Conventions that must hold in EVERY project are GENERAL "
            f"rules — save_rule offers that choice as answer 1.")




# ── SAVE MEMORY — remembered into HISTORY, promoted by the curator ─────────
# Founder 2026-09-03: "put save memory back and it's saving in the chat
# history memory... it's really saved in the memory, and it stays, and you
# can find it easily because it's separated." An explicit save is one turn
# appended to the project's archive (immutable, embedded, searchable within
# a minute) and fed to the hub as an exchange, where the curator applies the
# push test and decides whether it becomes a record every session is handed.
# Nothing here writes a record; the curator does, or nothing does. What a
# tool showed the agent is never archived unless the agent hands it over —
# this is the door for that.
def save_memory(project: str, content: str, title: str = "") -> str:
    """Remember something into the project's history now; the Brain decides
    whether it becomes a curated record."""
    from . import archive, hubfeed
    p = (project or "").strip()
    body = (content or "").replace("\x00", "").strip()
    if not p or p in ("rules", "state"):
        return "Error: a memory belongs to a project ('global' for facts true everywhere)."
    if not body:
        return "Error: nothing to remember."
    head = " ".join((title or "").split())[:200]
    text = f"SAVED TO MEMORY{' — ' + head if head else ''}\n{body}"[:60000]
    sid = "save-" + uuid.uuid4().hex[:16]
    # ROLE 'saved' (founder 2026-09-03: "some flag saved by the agent, so
    # when the agent is looking for the thing it saved it can easily find
    # them"): recall labels the hit as saved memory, list_saved lists them.
    n = archive.archive_turns(p, sid, [{"index": 0, "role": "saved", "text": text}])
    if not n:
        return "Error: the archive did not take it — nothing was saved."
    # whose words these are, for the hold-back (2026-09-28): the one session
    # prompting in this project now, when there is exactly one
    try:
        from . import injection
        with store.connection() as conn:
            injection.attribute_save(conn, conn.cursor(), p, sid)
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    try:
        from . import quota
        quota.bump_exchanges(1)
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    fed = hubfeed.active()
    if fed:
        hubfeed.feed(p, sid, [{"role": "saved", "text": text}], now=True)
    return (f"Remembered in {p}'s history (searchable within a minute). "
            + ("The Brain is reading it now and will keep a record if a "
               "future session must be handed it." if fed else
               "No hub key on this box: it is history and searchable, but "
               "nothing will curate it."))



def list_saved(project: str, limit: int = 20) -> str:
    """The things agents put away with save_memory in this project, newest
    first — the archived turns with role 'saved'. Read-only: history is
    never edited; a correction is another save."""
    p = (project or "").strip()
    if not p:
        return "Error: project required."
    n = max(1, min(int(limit or 20), 100))
    with store.connection() as conn:
        cur = conn.cursor()
        where, args = _chat_scope(cur, p)
        cur.execute(f"SELECT to_char(ts, 'YYYY-MM-DD HH24:MI'), text FROM chat "
                    f"WHERE role = 'saved' {where} "
                    f"ORDER BY ts DESC LIMIT %s;", args + [n])
        rows = cur.fetchall()
    if not rows:
        return f"Nothing saved in {p} yet (save_memory puts things here)."
    out = []
    for at, text in rows:
        head, _, body = (text or "").partition("\n")
        head = head.replace("SAVED TO MEMORY", "").strip(" —")
        out.append(f"— {at}" + (f" · {head}" if head else "") + "\n"
                   + " ".join(body.split())[:600])
    return "\n\n".join(out)


def update_memory(project: str, record_id: str, title: str, content: str,
                  memory_type: str = "") -> str:
    """EDIT ONE EXISTING record in place — the correction door (founder
    2026-09-03: "leave edit and delete"). It never creates: a record that
    does not exist is the curator's to write, from what you say."""
    try:
        p, k = _resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    if p == "rules":
        return "Error: rules are edited with save_rule."
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT type FROM document WHERE project=%s AND record_id=%s;",
                    [p, k])
        row = cur.fetchone()
    if not row:
        return (f"Error: no record {p}:{k} to edit — records are written by "
                f"the curator from what you say; this door only corrects one.")
    res = write_record(p, k, memory_type or row[0] or "note", title, content)
    return res if res.startswith("Error") else f"Edited {p}:{k}."


def delete_memory(project: str, record_id: str) -> str:
    """Delete ONE curated record and its outgoing edges.

    This tool exists because making `query_raw` read-only removed the only way
    v1 had to delete anything — and curation is not optional pruning, it is the
    thing that stops memory rotting. A dead record is a DELETED record.

    An explicit id-scoped tool is better than re-opening raw writes: it cannot
    take out a table by accident, it says exactly what it removed, and it
    REFUSES to touch `chat`, which is the one immutable store. Everything
    curated can be rebuilt from the archive; nothing can rebuild the archive.
    """
    try:
        p, k = _resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    with store.connection() as conn:
        cur = conn.cursor()
        if p == "rules":
            # Law lives only on the hub (2026-09-02): the delete goes there,
            # and the seeds — the service's own teaching — are read-only.
            from . import hubclient, rules_hub
            if k.startswith("seed_"):
                return (f"Error: rules:{k} is a ★SEED rule — the service's "
                        "own teaching, read-only and fleet-managed. It "
                        "cannot be deleted.")
            try:
                ok = hubclient.delete_rule(k)
            except hubclient.HubUnavailable as e:
                return (f"Error: the hub is unreachable ({e}); rules live only "
                        f"on the hub, so nothing was deleted.")
            rules_hub.forget()
            return (f"Deleted rule {k}." if ok else f"No rule {k} on the hub.")
        # Freshness read BEFORE the row goes — a target updated seconds ago
        # is very likely the caller's own save just filed or merged (wind
        # tunnel 2026-08-10: an agent corrected a fact, the gate merged it
        # into the existing record, and the agent then deleted that record
        # believing it was removing the OLD version — the corrected fact
        # died with it). The delete stays FREE — no gate, no confirm — the
        # door just says what probably happened.
        cur.execute("SELECT updated_at > now() - interval '90 seconds' "
                    "FROM document WHERE project=%s AND record_id=%s;", [p, k])
        fresh_row = cur.fetchone()
        cur.execute("DELETE FROM document WHERE project=%s AND record_id=%s;",
                    [p, k])
        gone = cur.rowcount
        cur.execute("DELETE FROM edge WHERE (src_project=%s AND src_record=%s) "
                    "OR (dst_project=%s AND dst_record=%s);", [p, k, p, k])
        edges = cur.rowcount
        conn.commit()
    if not gone:
        return f"No record {p}:{k} — nothing deleted."
    warn = ""
    if fresh_row and fresh_row[0]:
        warn = ("\n⚠ This record was written or updated MOMENTS ago — if "
                "that was your own correction being filed or merged, you "
                "just deleted the corrected fact. Corrections merge into "
                "the existing record automatically; re-save it if this "
                "delete was unintended.")
    # Dangling [[links]] are reported, not silently left: a reference pointing
    # at a deleted record is exactly what makes an index lie.
    return (f"DELETED {p}:{k}"
            + (f" (+{edges} edges removed)" if edges else "")
            + warn
            + "\nCheck for inbound [[links]] that now dangle.")


def supersede_memory(project: str, old_record_id: str,
                     new_record_id: str) -> str:
    """The successor replaces the old record — the old record is DELETED.

    Founder law (stated 2026-07-28, restated hard 2026-08-09): curated memory
    holds CURRENT TRUTH ONLY — "no superseded, no old, no backup-on-flag. The
    safety net is full chat history." This tool used to hedge against that
    law: it stamped meta.superseded_by on the old record and KEPT it, and
    search then served six-month-old corpses as truth to any agent that
    navigated badly. The flag state is dead. History lives where it always
    did — the immutable chat archive."""
    try:
        op, ok = _resolve(old_record_id, project)
        np_, nk = _resolve(new_record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    if (op, ok) == (np_, nk):
        return "Error: a record cannot supersede itself."
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM document WHERE project=%s AND record_id=%s;",
                    [np_, nk])
        if not cur.fetchone():
            return (f"Error: successor {np_}:{nk} does not exist — save the "
                    f"current-truth record first, then supersede.")
        cur.execute("SELECT coalesce(title,'') FROM document "
                    "WHERE project=%s AND record_id=%s;", [op, ok])
        row = cur.fetchone()
        if not row:
            return f"Error: no record {op}:{ok} to supersede."
        old_title = (row[0] or ok).strip()[:80]
        if op == "rules":
            if str(ok).startswith("seed_"):
                return (f"Error: rules:{ok} is a ★SEED rule — the service's "
                        "own teaching, read-only and fleet-managed. It "
                        "cannot be superseded.")
        cur.execute("SELECT coalesce(title,'') FROM document "
                    "WHERE project=%s AND record_id=%s;", [np_, nk])
        new_title = ((cur.fetchone() or [""])[0] or nk).strip()[:80]
        cur.execute("DELETE FROM document WHERE project=%s AND record_id=%s;",
                    [op, ok])
        cur.execute("DELETE FROM edge WHERE (src_project=%s AND src_record=%s) "
                    "OR (dst_project=%s AND dst_record=%s);", [op, ok, op, ok])
        conn.commit()
    return (f"{np_}:{nk} is now the ONLY record — {op}:{ok} DELETED "
            f"('{new_title}' replaced '{old_title}'); full history in chat.")


def cleanup_history(project: str, older_than_days: int = 180,
                    confirm: str = "") -> str:
    """Prune old chat history — the owner's deliberate exception to the
    immutable-archive rule. DELETE-ONLY (founder 2026-08-29: no summarize,
    no digest — record hygiene is heal's job). The 90-day floor is the
    protection: through our software nothing younger can be deleted, so a
    misbehaving or injected agent cannot strip the recent archive memory
    depends on. Dry-run preview by default; deletion only past
    confirm=<project>. Queued: delete → VACUUM FULL (measured: without the
    vacuum, freed space is imaginary)."""
    from core import prune                        # local: keeps import cheap
    from api import curate_queue
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    try:
        days = int(older_than_days)
    except (TypeError, ValueError):
        return "Error: older_than_days must be a number."
    if days < prune.MIN_AGE_DAYS:
        return (f"Error: cutoff must be at least {prune.MIN_AGE_DAYS} days — "
                f"the last 3 months of history always stay; memory needs "
                f"them to work properly.")
    status = curate_queue.last_prune_result(project)
    if confirm != project:
        p = prune.preview(project, days)
        if not p["rows"]:
            return (f"Nothing to clean: no {project} history older than "
                    f"{days} days." + (f"\n{status}" if status else ""))
        return (f"PREVIEW — would remove {p['rows']} archived rows across "
                f"{p['sessions']} conversations ({p['text_mb']} MB of text, "
                f"{p['oldest']} → {p['newest']}). Database now "
                f"{p['db_size_now']}.\n"
                f"Removal is outright and free; saved memories are "
                f"untouched. To proceed, call again with "
                f"confirm=\"{project}\"."
                + (f"\n{status}" if status else ""))
    # Nothing in range = nothing to queue. Without this check a confirmed
    # call on an empty range still ran a pointless VACUUM FULL (caught in
    # the 2026-07-30 soak E2E — the preview said so, the confirm didn't).
    p = prune.preview(project, days)
    if not p["rows"]:
        return (f"Nothing to clean: no {project} history older than "
                f"{days} days.")
    notice = curate_queue.enqueue_prune(project, days)
    if notice:
        return notice
    return (f"Cleanup queued: {project} history older than {days} days. "
            f"Runs in the background — then compaction reclaims the disk. "
            f"Call cleanup_history again (no confirm) to see the result.")


def dismiss_proposal(session_id: str, why: str = "") -> str:
    """THE PROPOSER'S OTHER ANSWER (2026-09-10): the session stays where it
    is filed; the proposal is marked declined and stops showing."""
    from . import proposer
    sid = (session_id or "").strip()
    if not sid:
        return "Error: pass a session_id."
    with store.connection() as conn:
        cur = conn.cursor()
        ok = proposer.decide(cur, sid, f"dismissed{': ' + why[:30] if why else ''}")
        conn.commit()
    return (f"Proposal for {sid[:12]} dismissed — the session stays where it is filed."
            if ok else f"No open proposal for session {sid[:12]}.")


def move_session(session_id: str, project: str = "", turn_lo: str = "",
                 turn_hi: str = "", why: str = "") -> str:
    """THE PROJECT-SPLIT DOOR (founder 2026-09-09): move an archived
    session, or a turn range of it, to the project it belongs to. The chat
    rows never change; an ownership row says where they belong now and
    every reader consults it; records (by origin), ledger rows and the
    session's note follow. With no
    project: where the session is today and its move history."""
    from . import ownership
    sid = (session_id or "").strip()
    if not sid:
        return "Error: pass a session_id (search_history shows them)."
    with store.connection() as conn:
        cur = conn.cursor()
        if not (project or "").strip():
            cur.execute("SELECT project, count(*), min(turn_index), max(turn_index), "
                        "min(ts)::date, max(ts)::date FROM chat WHERE session_id = %s "
                        "GROUP BY project;", [sid])
            rows = cur.fetchall()
            if not rows:
                return f"No archived turns for session {sid}."
            out = [f"archived under {p}: {n} turns {lo}..{hi} ({a} → {b})"
                   for p, n, lo, hi, a, b in rows]
            owned = ownership.resolve(ownership.rows(cur, fresh=True)).get(sid, [])
            for lo, hi, p in owned:
                span = "whole session" if lo <= ownership.LO and hi >= ownership.HI \
                    else f"turns {lo}-{hi}"
                out.append(f"belongs to {p} today: {span}")
            for h in ownership.history(cur, sid)[:10]:
                out.append(f"  move #{h['id']} {str(h['moved_at'])[:16]} "
                           f"{h['from_project'] or '?'} -> {h['project']}"
                           + ("" if h['turn_lo'] <= ownership.LO else f" turns {h['turn_lo']}-{h['turn_hi']}")
                           + (f" — {h['why']}" if h['why'] else ""))
            return "\n".join(out)
        log: list[str] = []
        try:
            r = ownership.move(conn, sid, project.strip(),
                               turn_lo or None, turn_hi or None,
                               who="owner tool", why=why, log=log)
            conn.commit()
        except ValueError as e:
            conn.rollback()
            return f"Error: {e}"
        except Exception as e:                                 # noqa: BLE001
            conn.rollback()
            return f"Error: {type(e).__name__}: {e}"
    for line in log:
        print(f"[move] {sid[:12]}: {line}", flush=True)
    span = "whole" if r["whole"] else f"turns {r['turn_lo']}-{r['turn_hi']}"
    return (f"Moved session {sid[:12]} ({span}) "
            f"{r['from'] or '?'} -> {r['project']}: {r['turns']} turns; followed: "
            f"{r.get('records', 0)} records, {r.get('ledger', 0)} ledger rows, "
            f"{r.get('notes', 0)} notes. "
            f"Reversible: move it back the same way.")



def save_commit(project: str, commit_hash: str, message: str,
                files_changed: str, branch: str = "main") -> str:
    if not _VALID.match(project or ""):
        return f"Error: bad project name '{project}'."
    archive.record_commit(project, {
        "sha": commit_hash,
        "message": f"{message}\n\nbranch={branch} files={files_changed}"})
    return f"Saved commit {project}:commit_{str(commit_hash)[:12]}"


# ── Brain Ledger + Brain Playbooks, owner tier (no scope/quota gates —
# the owner surface is ours). Implementations live in core.ledger /
# core.playbooks; these wrappers only format.

def list_ledger(project: str) -> str:
    from . import ledger as _ledger
    rows = _ledger.pile((project or "").strip().lower())
    if not rows:
        return f"Brain Ledger '{project}': empty."
    lines = [f"BRAIN LEDGER — '{project}': {len(rows)} undecided row(s), "
             f"oldest first."]
    marks = _ledger.marks((project or "").strip().lower())
    if marks:
        lines[0] += (f" The cleaner (report mode) marks {len(marks)} it would "
                     f"drop — judge them like any other row.")
    for rid, content, origin, created in rows:
        lines.append(f"[{rid}] ({str(created)[:10]}, {origin or 'manual'}) "
                     f"{content}{_ledger.mark_line(marks.get(rid))}")
    return "\n".join(lines)


def remove_from_ledger(ids: str, project: str) -> str:
    from . import ledger as _ledger
    try:
        want = sorted({int(p) for p in re.split(r"[,\s]+", str(ids or ""))
                       if p.strip()})
    except ValueError:
        return "Error: ids must be row numbers, e.g. \"3\" or \"3,7,12\"."
    if not want:
        return "Error: no row ids given."
    removed, missing = _ledger.remove_rows((project or "").strip().lower(),
                                           want)
    out = []
    if removed:
        out.append(f"Removed: {', '.join(map(str, removed))}.")
    if missing:
        out.append(f"Not found (nothing done): "
                   f"{', '.join(map(str, missing))}.")
    return " ".join(out)


def save_playbook(name: str, content: str, annotation: str = "") -> str:
    from . import playbooks as _playbooks
    try:
        verb = _playbooks.save(name, content, annotation)
    except ValueError as e:
        return f"Error: {e}"
    loud = ("created" if verb == "created"
            else "UPDATED — previous version overwritten")
    return f"Brain playbook '{name.strip().lower()}' {loud}."


def get_playbook(name: str) -> str:
    from . import playbooks as _playbooks
    row = _playbooks.get(name)
    if not row:
        return f"No playbook named '{(name or '').strip().lower()}'."
    nm, content, annotation, updated = row
    note = f" — {annotation}" if annotation else ""
    return (f"BRAIN PLAYBOOK '{nm}' (updated {str(updated)[:10]}){note}\n\n"
            f"{content}")


def list_playbooks() -> str:
    from . import playbooks as _playbooks
    rows = _playbooks.listing()
    if not rows:
        return "No Brain playbooks saved."
    return "\n".join(
        f"- {nm} ({chars} chars, upd {str(updated)[:10]})"
        + (f" — {annotation}" if annotation else "")
        for nm, annotation, chars, updated in rows)


def delete_playbook(name: str) -> str:
    from . import playbooks as _playbooks
    nm = (name or "").strip().lower()
    return (f"Brain playbook '{nm}' deleted." if _playbooks.delete(nm)
            else f"No playbook named '{nm}'.")





# ── SESSION NOTES — the handover (founder 2026-09-03) ──────────────────────
# Agents no longer hand-write curated records. What they find they SAY, and
# the curator files what passes the push test. What a session leaves for the
# next one is a NOTE: keyed by session and project, a ring of five per
# project, injected whole at session start. See core/notes.py.
def save_note(project: str, session_id: str, title: str, body: str) -> str:
    """Write or refresh THIS session's handover for a project — where the
    thread stands, what is live, what is next. One per session per project;
    writing again replaces it. A note for another project files there."""
    from . import notes as _notes
    p = (project or "").strip()
    if not p or p in ("rules", "state"):
        return "Error: a note belongs to a project."
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM project WHERE name=%s;", [p])
        if not cur.fetchone():
            return f"Error: no project '{p}' — add_project first."
        try:
            out = _notes.save(conn, p, session_id, title, body)
        except ValueError as e:
            return f"Error: {e}"
    ev = f"; pushed off the ring: {', '.join(x[:8] for x in out['evicted'])}" \
        if out["evicted"] else ""
    return (f"Note saved for {p} ({out['size']} of {_notes.RING} kept{ev}). "
            f"Every {p} session starts with it until five newer sessions "
            f"replace it. Writing again from this session replaces this note.")


def delete_note(project: str, session_id: str) -> str:
    from . import notes as _notes
    p = (project or "").strip()
    with store.connection() as conn:
        gone = _notes.delete(conn, p, (session_id or "").strip())
    return (f"Deleted the note for {p} from session {session_id[:8]}."
            if gone else f"No note for that session in {p}.")


# next_task/complete_task are added below, self-hosted only.
TOOLS = {
    "recall": recall, "period_report": period_report, "save_verdicts": save_verdicts,
    "list_verdicts": list_verdicts, "load_project": load_project,
    "session_context": session_context,
    "search_memory": search_memory, "semantic_search": semantic_search,
    "search_chat": search_chat, "search_chat_text": search_chat_text,
    "get_memory": get_memory, "list_memory": list_memory,
    "recent_records": recent_records, "query_raw": query_raw,
    "cleanup_history": cleanup_history,
    "memory_health": memory_health, "save_rule": save_rule,
    # save_record left the surface 2026-09-03; save_memory is back with a new
    # meaning (into history, the curator promotes) — see its docstring.
    "save_note": save_note, "delete_note": delete_note,
    "save_memory": save_memory, "update_memory": update_memory,
    "list_saved": list_saved,
    "move_session": move_session,
    "dismiss_proposal": dismiss_proposal,
    "supersede_memory": supersede_memory,
    "delete_memory": delete_memory, "save_commit": save_commit,
    "add_project": add_project,
    "list_ledger": list_ledger,
    "remove_from_ledger": remove_from_ledger,
    "save_playbook": save_playbook, "get_playbook": get_playbook,
    "list_playbooks": list_playbooks, "delete_playbook": delete_playbook,
}

# SELF-HOSTED ONLY. On our cloud a worker with an inference key does this
# work, so these tools would be dead weight on the surface; on a customer's
# box they ARE the judgment path. Registering conditionally keeps each shape's
# tool list honest about what it can actually do.
from . import mode as _mode                                    # noqa: E402
if _mode.is_selfhosted():
    if not _mode.panel_ops():
        # true-local only — see the spec in mcp_app.py for the why.
        # Imported at call time: customer_tools imports this module.
        def _local_delete_project(project: str, confirm: str = "") -> str:
            from api.customer_tools import delete_project
            return delete_project(project, confirm=confirm)
        TOOLS["delete_project"] = _local_delete_project

