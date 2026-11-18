"""A PERIOD OF A MEMORY (2026-10-23, founder: "using the brain to measure how
much work we've done … with some simple command").

Three things, all computed in the database, never by reading the archive's
text into a model — a month of a busy memory is hundreds of millions of
tokens, so the counting has to happen here and an agent reads the result:

  * parse()          — a from–until window, dates or ISO times, `until`
                       inclusive when it is a bare date;
  * the date-only listings behind search_history / search_brain with no
    topic: the period's decision lines in order, its changed records;
  * report()         — the period's OUTPUT, per project: decisions by kind,
    the reversals (a line the writer linked as overturning an earlier one),
    the reopened ones (a line whose links lead back to something rejected or
    dropped on an earlier day), records
    written, changed and retired, ledger rows opened and closed, playbooks
    changed, messages and sessions, tokens. Facts only — the judgement of
    what was wasted is the caller's (playbook wasted-time-estimator).

Every read is fenced to `projects` (a scoped key's list) when given.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

MAX_DAYS = 400            # one call never spans more than this
LIST_LIMIT = 50


def parse(since: str = "", until: str = "") -> tuple[datetime | None, datetime | None, str]:
    """(since, until_exclusive, error). Both empty → (None, None, "")."""
    def one(raw: str, end: bool):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            if len(raw) == 10:                      # YYYY-MM-DD — a whole day
                d = date.fromisoformat(raw)
                t = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                return t + timedelta(days=1) if end else t
            t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"'{raw}' is not a date (YYYY-MM-DD) or an ISO time")
    try:
        s, u = one(since, False), one(until, True)
    except ValueError as e:
        return None, None, str(e)
    if s and u and s >= u:
        return None, None, "since must be before until"
    if s and u and (u - s).days > MAX_DAYS:
        return None, None, f"a period is at most {MAX_DAYS} days"
    return s, u, ""


def label(since, until) -> str:
    a = since.date().isoformat() if since else "the beginning"
    b = (until - timedelta(seconds=1)).date().isoformat() if until else "now"
    return f"{a} → {b}"


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", [table])
    return bool(cur.fetchone()[0])


def _where(col: str, since, until, projects, pcol: str = "project") -> tuple[str, list]:
    sql, args = "TRUE", []
    if since:
        sql, args = sql + f" AND {col} >= %s", args + [since]
    if until:
        sql, args = sql + f" AND {col} < %s", args + [until]
    if projects is not None:
        sql, args = sql + f" AND {pcol} = ANY(%s)", args + [list(projects)]
    return sql, args


def _days(since, until) -> tuple[date | None, date | None]:
    """The window as days for the decision table's `day` column."""
    return (since.date() if since else None,
            until.date() if until else None)


# ── date-only listings ──────────────────────────────────────────────────────
def lines(cur, projects, since, until, limit: int = LIST_LIMIT) -> list[str]:
    """The period's decision lines, oldest first."""
    if not _exists(cur, "decision"):
        return []
    d0, d1 = _days(since, until)
    w, a = _where("day", d0, d1, projects)
    cur.execute(f"SELECT day, kind, project, what, why, replaced, id FROM decision WHERE {w} "
                f"ORDER BY day, turn_lo NULLS LAST, id LIMIT %s;", a + [int(limit)])
    out = []
    for day, kind, p, what, why, replaced, lid in cur.fetchall():
        tail = (f" — replaced: {' '.join(replaced.split())[:160]}" if replaced else "") + \
               (f" — why: {' '.join(why.split())[:160]}" if why else "")
        out.append(f"[line {day or '?'} #{lid}] {kind} ({p}): {' '.join((what or '').split())[:280]}{tail}")
    return out


def records(cur, projects, since, until, limit: int = LIST_LIMIT) -> list[str]:
    """Records written or changed in the period, newest first."""
    w, a = _where("updated_at", since, until, projects)
    cur.execute(f"SELECT project, record_id, type, title, created_at, updated_at FROM document "
                f"WHERE {w} AND record_id <> 'memory_index' ORDER BY updated_at DESC LIMIT %s;",
                a + [int(limit)])
    out = []
    for p, rid, typ, title, created, updated in cur.fetchall():
        new = since is None or (created and created >= since)
        out.append(f"[{'new' if new else 'upd'} {updated:%Y-%m-%d}] {p}:{rid} [{typ}] {title}")
    return out


def counts(cur, projects, since, until) -> str:
    """One line: how much conversation the period holds."""
    w, a = _where("ts", since, until, projects)
    cur.execute(f"SELECT count(*), count(DISTINCT session_id) FROM chat WHERE {w};", a)
    n, s = cur.fetchone()
    return f"{n} messages in {s} sessions in this period."


# ── the report ──────────────────────────────────────────────────────────────
def report(cur, projects, since, until, top: int = 8) -> str:
    """The period's output per project, facts only."""
    d0, d1 = _days(since, until)
    per: dict[str, dict] = {}

    def slot(p: str) -> dict:
        return per.setdefault(p, {"msgs": 0, "user": 0, "sessions": 0, "days": 0,
                                  "decided": 0, "tried": 0, "rejected": 0, "dropped": 0,
                                  "reversals": 0, "reopened": 0, "rec_new": 0, "rec_upd": 0,
                                  "retired": 0, "ledger_open": 0, "ledger_closed": 0})

    w, a = _where("ts", since, until, projects)
    cur.execute(f"SELECT project, count(*), count(*) FILTER (WHERE role = 'user'), "
                f"count(DISTINCT session_id), count(DISTINCT ts::date) FROM chat WHERE {w} "
                f"GROUP BY project;", a)
    for p, n, u, s, d in cur.fetchall():
        x = slot(p)
        x.update(msgs=n, user=u, sessions=s, days=d)

    reversals, reopened = [], []
    if _exists(cur, "decision"):
        w, a = _where("day", d0, d1, projects)
        cur.execute(f"SELECT project, kind, count(*) FROM decision WHERE {w} GROUP BY project, kind;", a)
        for p, kind, n in cur.fetchall():
            x = slot(p)
            if kind in x:
                x[kind] = n
        # A REVERSAL (2026-10-23, marketing's read of the first run): a line
        # whose link to an earlier line is "overturns" — the writer's own
        # mark, not the free-text "replaced", which also carries refinements
        # ("same-minute became same-day") and declined proposals. Both ends
        # are shown, so a judge sees what was undone and when.
        w2, a2 = _where("d.day", d0, d1, projects, "d.project")
        cur.execute(f"""SELECT d.project, d.day, d.kind, d.what, e.day, e.kind, e.what, d.id, e.id
                        FROM decision d JOIN decision e ON e.id = d.follows
                        WHERE {w2} AND d.how = 'overturns'
                        ORDER BY d.day DESC, d.id DESC;""", a2)
        rows = cur.fetchall()
        for p, *_ in rows:
            slot(p)["reversals"] += 1
        reversals = [f"[{day} #{did}] ({p}) {kind}: {' '.join((what or '').split())[:180]} — OVERTURNS "
                     f"[{e_day} #{eid}] {e_kind}: {' '.join((e_what or '').split())[:140]}"
                     for p, day, kind, what, e_day, e_kind, e_what, did, eid in rows[:top]]
        # A REOPENED QUESTION: a decided or tried line in the period whose
        # chain of links (up to four steps back) reaches a rejected or
        # dropped line written on an EARLIER day — the same thing brought
        # back, by the writer's links, not by a shared topic word.
        cur.execute(f"""WITH RECURSIVE back(start, id, depth) AS (
                            SELECT d.id, d.follows, 1 FROM decision d
                            WHERE {w2} AND d.kind IN ('decided', 'tried') AND d.follows IS NOT NULL
                            UNION ALL
                            SELECT b.start, e.follows, b.depth + 1 FROM back b JOIN decision e ON e.id = b.id
                            WHERE b.depth < 4 AND e.follows IS NOT NULL)
                        SELECT DISTINCT ON (d.id) d.project, d.day, d.what, e.day, e.kind, e.what, d.id, e.id
                        FROM back b JOIN decision d ON d.id = b.start JOIN decision e ON e.id = b.id
                        WHERE e.kind IN ('rejected', 'dropped') AND e.day < d.day
                        ORDER BY d.id DESC, b.depth;""", a2)
        rows = sorted(cur.fetchall(), key=lambda r: r[1], reverse=True)
        for p, *_ in rows:
            slot(p)["reopened"] += 1
        reopened = [f"[{day} #{did}] ({p}) {' '.join((what or '').split())[:180]} — BACK AFTER "
                    f"{e_kind.upper()} [{e_day} #{eid}]: {' '.join((e_what or '').split())[:140]}"
                    for p, day, what, e_day, e_kind, e_what, did, eid in rows[:top]]

    w, a = _where("created_at", since, until, projects)
    cur.execute(f"SELECT project, count(*) FROM document WHERE {w} AND record_id <> 'memory_index' "
                f"GROUP BY project;", a)
    for p, n in cur.fetchall():
        slot(p)["rec_new"] = n
    w, a = _where("updated_at", since, until, projects)
    cur.execute(f"SELECT project, count(*) FROM document WHERE {w} AND record_id <> 'memory_index' "
                f"AND created_at < coalesce(%s, '-infinity'::timestamptz) GROUP BY project;", a + [since])
    for p, n in cur.fetchall():
        slot(p)["rec_upd"] = n
    if _exists(cur, "retire_log"):
        w, a = _where("deleted_at", since, until, projects)
        cur.execute(f"SELECT project, count(*) FROM retire_log WHERE {w} GROUP BY project;", a)
        for p, n in cur.fetchall():
            slot(p)["retired"] = n
    if _exists(cur, "ledger"):
        w, a = _where("created_at", since, until, projects)
        cur.execute(f"SELECT project, count(*) FROM ledger WHERE {w} GROUP BY project;", a)
        for p, n in cur.fetchall():
            slot(p)["ledger_open"] = n
    if _exists(cur, "ledger_clean_log"):
        w, a = _where("at", since, until, projects)
        cur.execute(f"SELECT project, count(*) FROM ledger_clean_log WHERE {w} GROUP BY project;", a)
        for p, n in cur.fetchall():
            slot(p)["ledger_closed"] = n

    pb = 0
    if _exists(cur, "playbook") and projects is None:
        w, a = _where("updated_at", since, until, None)
        cur.execute(f"SELECT count(*) FROM playbook WHERE {w};", a)
        pb = cur.fetchone()[0]
    tokens = None
    if _exists(cur, "usage_event") and projects is None:
        w, a = _where("ts", since, until, None)
        cur.execute(f"SELECT coalesce(sum(tokens_in), 0), coalesce(sum(tokens_out), 0) "
                    f"FROM usage_event WHERE {w};", a)
        tokens = cur.fetchone()

    per = {p: x for p, x in per.items() if p not in ("rules", "state") and any(x.values())}
    if not per:
        return f"PERIOD {label(since, until)}: nothing happened in this memory in this period."
    order = sorted(per, key=lambda p: -(per[p]["msgs"] + 10 * per[p]["decided"]))
    tot = {k: sum(per[p][k] for p in per) for k in next(iter(per.values()))}
    out = [f"PERIOD {label(since, until)} — facts only, counted in the memory "
           f"(what was wasted is a judgement: read the reversals and reopened questions below)",
           f"TOTAL: {tot['decided']} decided, {tot['tried']} tried, {tot['rejected']} rejected, "
           f"{tot['dropped']} dropped; {tot['reversals']} reversals, {tot['reopened']} reopened "
           f"questions; records {tot['rec_new']} new, {tot['rec_upd']} changed, {tot['retired']} "
           f"retired; ledger {tot['ledger_open']} opened, {tot['ledger_closed']} closed by the "
           f"cleaner; {tot['msgs']} messages ({tot['user']} from the user) in {tot['sessions']} sessions"
           + (f"; {pb} playbooks changed" if pb else "")
           + (f"; {tokens[0]:,} tokens in, {tokens[1]:,} out" if tokens and any(tokens) else ""),
           "",
           "PER PROJECT (decided/tried/rejected/dropped · reversals · reopened · records new/changed/"
           "retired · ledger opened/closed · messages in sessions over days):"]
    for p in order:
        x = per[p]
        out.append(f"- {p}: {x['decided']}/{x['tried']}/{x['rejected']}/{x['dropped']} · "
                   f"{x['reversals']} · {x['reopened']} · {x['rec_new']}/{x['rec_upd']}/{x['retired']} · "
                   f"{x['ledger_open']}/{x['ledger_closed']} · {x['msgs']} in {x['sessions']} over "
                   f"{x['days']} days")
    if reversals:
        out += ["", f"REVERSALS (newest {len(reversals)} — a line the writer marked as overturning an "
                f"earlier one; the decider is not recorded, so an agent's undone mistake and a real "
                f"change of course both land here):"]
        out += [f"- {r}" for r in reversals]
    if reopened:
        out += ["", f"REOPENED (newest {len(reopened)} — linked back to a line rejected or dropped "
                f"on an earlier day):"]
        out += [f"- {r}" for r in reopened]
    out += ["", "NOTE: messages and days before 2026-10-23 count by batch write time, not the "
            "moment each was said; decision lines carry their own day. For the lines behind any "
            "number: search_history with this since/until and a topic."]
    return "\n".join(out)


# ── verdicts: a judge's calls, kept so the next run can re-judge them ──────
# Founder 2026-10-23 (marketing's wasted-time-estimator playbook): periodic
# runs judge the gap AND re-judge earlier verdicts — "it wasn't wasted, it
# just needed more time to bring the effect". So every call a judge makes is
# stored with the period it judged, when, the item, the verdict, the reason
# and the decision lines it rests on; a re-judgement is a NEW row on the same
# item, never an edit, so the history of how a call changed is kept.
VERDICT_DDL = """
CREATE TABLE IF NOT EXISTS verdict (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL DEFAULT '',
    period_from date,
    period_to   date,
    item        text NOT NULL,
    verdict     text NOT NULL,
    reason      text NOT NULL DEFAULT '',
    line_ids    bigint[] NOT NULL DEFAULT '{}',
    judged_by   text NOT NULL DEFAULT '',
    judged_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS verdict_item ON verdict (project, item);
CREATE INDEX IF NOT EXISTS verdict_period ON verdict (period_from, period_to);
"""
MAX_VERDICTS = 200


def save_verdicts(cur, items: list, since, until, judged_by: str = "",
                  writable=None) -> tuple[int, list[str]]:
    """Store a run's calls. `writable(project) -> bool` fences a scoped key.
    Returns (saved, problems)."""
    if not _exists(cur, "verdict"):
        cur.execute(VERDICT_DDL)
    if not isinstance(items, list) or not items:
        return 0, ["items must be a non-empty list"]
    if len(items) > MAX_VERDICTS:
        return 0, [f"at most {MAX_VERDICTS} verdicts per call"]
    d0, d1 = since.date() if since else None, (until - timedelta(days=1)).date() if until else None
    saved, problems = 0, []
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            problems.append(f"#{n}: not an object")
            continue
        item = " ".join(str(it.get("item") or "").split())[:500]
        verdict = " ".join(str(it.get("verdict") or "").split())[:80]
        if not item or not verdict:
            problems.append(f"#{n}: item and verdict are required")
            continue
        project = str(it.get("project") or "").strip().lower()
        if writable is not None and not writable(project):
            problems.append(f"#{n}: no write access to project '{project}'")
            continue
        try:
            lids = [int(x) for x in (it.get("line_ids") or [])][:50]
        except (TypeError, ValueError):
            problems.append(f"#{n}: line_ids must be numbers")
            continue
        cur.execute("INSERT INTO verdict (project, period_from, period_to, item, verdict, reason, "
                    "line_ids, judged_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
                    [project, d0, d1, item, verdict, str(it.get("reason") or "")[:2000], lids,
                     str(judged_by or "")[:80]])
        saved += 1
    return saved, problems


def list_verdicts(cur, projects, since, until, limit: int = 200) -> list[str]:
    """Calls made on periods overlapping [since, until), each item's history
    together, oldest call first — what the next run re-judges."""
    if not _exists(cur, "verdict"):
        return []
    sql, args = "TRUE", []
    if since:
        sql, args = sql + " AND (period_to IS NULL OR period_to >= %s)", args + [since.date()]
    if until:
        sql, args = sql + " AND (period_from IS NULL OR period_from < %s)", args + [until.date()]
    if projects is not None:
        sql, args = sql + " AND project = ANY(%s)", args + [list(projects)]
    cur.execute(f"SELECT project, item, period_from, period_to, verdict, reason, line_ids, "
                f"judged_by, judged_at FROM verdict WHERE {sql} "
                f"ORDER BY project, item, judged_at LIMIT %s;", args + [int(limit)])
    out = []
    for p, item, f, t, v, why, lids, by, at in cur.fetchall():
        out.append(f"[{at:%Y-%m-%d}] ({p or '-'}) {item} — {v}"
                   + (f" — why: {' '.join(why.split())[:300]}" if why else "")
                   + (f" — lines {','.join(str(x) for x in lids)}" if lids else "")
                   + f" — period {f or '?'}→{t or '?'}" + (f" — by {by}" if by else ""))
    return out
