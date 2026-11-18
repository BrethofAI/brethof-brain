"""THE BENCH — scratch tenants for measuring the curator and the decision
lines (founder 2026-09-20: "we've got 30 cards, so I think we can test…").

One scratch tenant per VARIANT — a database named t_bench_<variant> that
the workers never iterate. Each rig reads the real archive read-only and
writes only into its variant: `decisions` (the curator with the decision
lines over a fixed chunk), `decision_find` / `decision_dump` (the lines,
read back), `heal_replay` / `heal_dump`, `record_pairs`, `record_cross`,
`status`, `drop`; the records replay and the backfill (core/records_replay,
core/backfill) take a variant as their destination. (The old inventory
graph's rigs — load, drain, cards, fold, close, facts, fact_cards,
describe, probe — left with that graph on 2026-09-27.)

Door: POST /v1/tools/bench {action, variant, ...} (owner key).
"""
from __future__ import annotations

import json
import traceback

from . import store

PREFIX = "bench_"


def tenant_of(variant: str) -> str:
    v = (variant or "").strip().lower()
    if not v or not v.replace("_", "").isalnum():
        raise ValueError(f"bad variant name: {variant!r}")
    return PREFIX + v


def is_bench(tenant: str) -> bool:
    return str(tenant or "").startswith(PREFIX)


def _dsn(db: str) -> str:
    return (f"host={store.PG_HOST} port={store.PG_PORT} dbname={db} "
            f"user={store.PG_USER} password={store.PG_PASS}")


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", [table])
    return bool(cur.fetchone()[0])


def status(variant: str) -> dict:
    """What the variant's tenant holds: the decision lines, their threads,
    the probe records and the curated records."""
    tenant = tenant_of(variant)
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            counts = {}
            for tbl in ("decision", "decision_thread", "probe_record", "document"):
                if _exists(cur, tbl):
                    cur.execute(f"SELECT count(*) FROM {tbl};")
                    counts[tbl] = int(cur.fetchone()[0])
            conn.rollback()
    finally:
        store.unbind(tok)
    return {"tenant": tenant, "counts": counts}


def _cut(piece: str, head: int = 12000, tail: int = 28000) -> str:
    """An exchange too long for one call keeps its opening (the prompt, where
    a no is usually said) and its end (where it lands)."""
    if len(piece) <= head + tail:
        return piece
    return piece[:head] + "\n\n[… the middle of this exchange is left out …]\n\n" + piece[-tail:]


PROBE_RECORD_DDL = """CREATE TABLE IF NOT EXISTS probe_record (id bigserial PRIMARY KEY, project text, session_id text,
turn_lo integer, type text, title text, content text);
ALTER TABLE probe_record ADD COLUMN IF NOT EXISTS replaced integer NOT NULL DEFAULT 0;"""


def runaway_guard(records: list, exchanges: int = 1) -> tuple[list, int]:
    """A degenerate answer never lands (2026-09-23: one window of the simple
    prompt wrote 219 records, 173 of them 'the merchant must configure <word>
    settings'). Mechanical, not a judgement of quality: records whose titles
    open with the same five words keep the first three; and one answer keeps
    at most twelve records per exchange it read. The model never sees these
    numbers — they are a fuse, not a target. Returns (kept, dropped)."""
    seen: dict[str, int] = {}
    kept = []
    for r in records:
        if not isinstance(r, dict):
            continue
        key = " ".join(str(r.get("title") or "").lower().split()[:5])
        seen[key] = seen.get(key, 0) + 1
        if key and seen[key] > 3:
            continue
        kept.append(r)
    kept = kept[:12 * max(1, exchanges)]
    return kept, len([r for r in records if isinstance(r, dict)]) - len(kept)


def decisions(variant: str, exchanges: list[dict], workers: int = 8, source: str = "memory",
              nxt: bool = False, seen_records: bool = False, window: int = 1,
              prompt: str = "decisions", window_chars: int = 40000, headers: bool = False,
              json_mode: bool = False) -> str:
    """THE MEMORY OF DECISIONS, measured on a FIXED CHUNK (founder 2026-09-23:
    "one chunk of the chat for testing … we have to test it many times"):
    each {project, session, turn} names the exchange holding that turn in
    the SOURCE tenant's archive (read only); the curator with the decisions
    section reads it and its lines go to the variant's decision table, which
    every run starts empty. nxt=True hands the curator the person's NEXT
    prompt as context (founder 2026-09-23: "my prompt — your answer — my
    prompt"). The records the curator wrote go to probe_record, for reading.
    window=N (long runs only): one call reads up to N exchanges in a row, as
    long as together they fit what one exchange may carry today (40k chars);
    the next prompt is the one after the window's last exchange (2026-09-23:
    the curator prompt is sent once per window, and it sees the progression).
    In a thread; log: [decisions] …"""
    import threading
    import psycopg
    tenant = tenant_of(variant)
    # V9 UNDER THREE BRIEFS (founder 2026-09-26: "I think they need them —
    # test it"): thV9B = no project description (what the rig measured all
    # week — the bench hub held no rules), thV9 = the description (as live),
    # thV9R = the description and the project's filtered rule lines
    # liveR (2026-09-26): the per-exchange curator that ran live until the
    # switch, asked with the project's rules as it was live
    prompt, probe_flags = {"thV9R": ("thV9", {"with_rules": True}), "thV9B": ("thV9", {"no_purpose": True}),
                           "liveR": ("live", {"with_rules": True}),
                           # switchNT (2026-09-27): the production pair with the records text
                           # that keeps no test results
                           "switchNT": ("switch", {"records_prompt": "nt", "records_only": True}),
                           # switchNR: switchNT plus the line on run reports
                           "switchNR": ("switch", {"records_prompt": "nr", "records_only": True}),
                           # switchR: the live records text alone (no lines call) — the fair
                           # baseline beside switchNT / switchNR, and cheaper on GLM
                           "switchR": ("switch", {"records_only": True}),
                           # THE CURATOR'S THREE FIXES (2026-09-28), both calls each:
                           # cfBase = production (nr + V9); cfP = the sorting in both;
                           # cfG = one record per finding + the lines' keep-bar; cfPG = all
                           "cfBase": ("switch", {"records_prompt": "nr"}),
                           "cfP": ("switch", {"records_prompt": "nP", "lines_prompt": "P"}),
                           "cfG": ("switch", {"records_prompt": "nG", "lines_prompt": "K"}),
                           "cfPG": ("switch", {"records_prompt": "nPG", "lines_prompt": "PK"}),
                           # round two: the records with the sorting and one finding per
                           # record, the lines with P2
                           "cfP2": ("switch", {"records_prompt": "nPG", "lines_prompt": "P2"}),
                           # round three: production's lines text with one carve-out
                           "cfP3": ("switch", {"records_prompt": "nPG", "lines_prompt": "P3"}),
                           # WHAT IS WORTH KEEPING (2026-09-28): the core of both
                           # texts replaced by what is kept and what is not;
                           # keepB also writes each candidate's verdict first
                           "keepA": ("switch", {"records_prompt": "kA", "lines_prompt": "kA"}),
                           "keepB": ("switch", {"records_prompt": "kB", "lines_prompt": "kB"}),
                           # kC: the lesson a fix found is kept (records); no push
                           # to silence, a measurement a choice rests on (lines)
                           "keepC": ("switch", {"records_prompt": "kC", "lines_prompt": "kC"}),
                           # THE TARGET (2026-09-28): records the important truths of
                           # now (decisions in force as what + why; preferences always);
                           # lines the decisions' history (kC)
                           "target": ("switch", {"records_prompt": "kT", "lines_prompt": "kC"}),
                           # the target trimmed: four licensing sentences out, one
                           # subject = one record (2026-09-28)
                           "targetU": ("switch", {"records_prompt": "kU", "lines_prompt": "kC"})}.get(prompt, (prompt, {}))
    # rec_<name> (2026-09-28, eight at once): the records call alone under
    # TEXTS["batch_records_<name>"], so many records texts run side by side
    if prompt.startswith("rec_"):
        prompt, probe_flags = "switch", {"records_prompt": prompt[4:], "records_only": True}
    elif prompt.startswith("lin_"):
        # the lines call alone under TEXTS["batch_lines_<name>"] (2026-09-29)
        prompt, probe_flags = "switch", {"lines_prompt": prompt[4:], "lines_only": True}

    def _run() -> None:
        from concurrent.futures import ThreadPoolExecutor
        from . import decisionline, hubfeed, judge
        store.provision_tenant(tenant)
        tok = store.bind(tenant)
        try:
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute(decisionline.DDL)
                cur.execute("DELETE FROM decision;")
                cur.execute(PROBE_RECORD_DDL)
                cur.execute("DELETE FROM probe_record;")
                conn.commit()
            todo: dict[tuple, dict] = {}
            with psycopg.connect(_dsn(store.tenant_db(source))) as src:
                scur = src.cursor()
                for ex in exchanges:
                    sid, turn = str(ex.get("session") or ""), int(ex.get("turn") or 0)
                    if ex.get("span"):
                        # A LONG RUN (2026-09-23): every exchange of one session
                        # from span[0] on, at most ex.max of them, in order —
                        # what the curator meets live, duplicates included
                        a0, a1 = int(ex["span"][0]), int(ex["span"][1])
                        scur.execute("SELECT turn_index, role, text, ts FROM chat WHERE session_id = %s "
                                     "AND turn_index BETWEEN %s AND %s ORDER BY turn_index;", [sid, a0, a1 + 400])
                        rows = scur.fetchall()
                        day = {int(i): t for i, _r, _x, t in rows}
                        pieces = hubfeed._pieces([{"index": int(i), "role": r, "text": x or ""} for i, r, x, _t in rows])
                        run = [(k, p) for k, p in enumerate(pieces) if a0 <= p[1] <= a1][:int(ex.get("max") or 30)]
                        groups: list[list] = []
                        for k, p in run:
                            g = groups[-1] if groups else None
                            if (g and len(g) < max(1, window)
                                    and sum(len(_cut(q[0])) for _, q in g) + len(_cut(p[0])) <= window_chars):
                                g.append((k, p))
                            else:
                                groups.append([(k, p)])
                        for g in groups:
                            k, lo, hi = g[-1][0], g[0][1][1], g[-1][1][2]
                            ts = day.get(lo)
                            after = ""
                            if nxt and k + 1 < len(pieces):
                                after = pieces[k + 1][0].split("\n\n[", 1)[0][:6000]
                            todo.setdefault((sid, lo), {"project": str(ex.get("project") or ""), "sid": sid,
                                                        "lo": lo, "hi": hi, "n": len(g),
                                                        "ranges": [[q[1], q[2]] for _, q in g],
                                                        "piece": ("\n\n".join(
                                                            f"===== EXCHANGE {j + 1} of {len(g)} — {str(day.get(q[1]) or '')[:16]} =====\n\n{_cut(q[0])}"
                                                            for j, (_, q) in enumerate(g))
                                                            if headers and len(g) > 1 else "\n\n".join(_cut(q[0]) for _, q in g)),
                                                        "day": ts.date() if ts else None, "next": after})
                        continue
                    scur.execute("SELECT turn_index, role, text, ts FROM chat WHERE session_id = %s "
                                 "AND turn_index BETWEEN %s AND %s ORDER BY turn_index;", [sid, turn - 600, turn + 600])
                    rows = scur.fetchall()
                    day = {int(i): t for i, _r, _x, t in rows}
                    turns = [{"index": int(i), "role": r, "text": x or ""} for i, r, x, _t in rows]
                    pieces = hubfeed._pieces(turns)
                    for j, (piece, lo, hi) in enumerate(pieces):
                        if lo <= turn <= hi:
                            # prev=1 also reads the exchange before: the chunk's
                            # ordinary talk, where most pieces settle nothing
                            # prev=1 the exchange before; after=1 the one after
                            # (the founder's reply and what answered it — where a
                            # push-back is read as its own piece, 2026-09-23)
                            lo0 = max(0, j - int(ex.get("prev") or 0))
                            hi0 = j + 1 + int(ex.get("after") or 0)
                            for k, (piece, lo, hi) in enumerate(pieces[lo0:hi0], lo0):
                                ts = day.get(lo)
                                after = ""
                                if nxt and k + 1 < len(pieces):
                                    # the person's next prompt only: the first
                                    # turn of the next exchange, never its answer
                                    after = pieces[k + 1][0].split("\n\n[", 1)[0][:6000]
                                todo.setdefault((sid, lo), {"project": str(ex.get("project") or ""), "sid": sid,
                                                            "lo": lo, "hi": hi, "piece": _cut(piece),
                                                            "day": ts.date() if ts else None, "next": after})
                            break
                    else:
                        print(f"[decisions] {sid[:8]}#{turn}: no exchange holds that turn", flush=True)
            print(f"[decisions] bench {variant}: {len(todo)} exchanges", flush=True)
            tot = {"asked": 0, "lines": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0}

            def one(it: dict) -> None:
                t = store.bind(tenant)
                try:
                    # THREADS: what this session wrote before this exchange,
                    # and the project's thread names
                    with store.connection() as c:
                        cc = c.cursor()
                        shown = decisionline.earlier(cc, it["project"], it["sid"], it["lo"])
                        names = decisionline.thread_names(cc, it["project"])
                        near_ = None
                        if prompt == "thV9":
                            opened, near_ = decisionline.view_near(cc, it["project"], it["piece"])
                        else:
                            opened = (decisionline.open_threads(cc, it["project"]) if prompt in ("thV", "thNV")
                                      else decisionline.open_threads_near(cc, it["project"], it["piece"]) if prompt in ("thV2", "thV3", "thV4", "thV5", "thV6", "thV7", "thV8", "switch")
                                      else None)
                        so_far = []
                        if seen_records:
                            # brec2: the WHOLE session's records, the newest 20 with
                            # content and the older by title (2026-09-23)
                            cc.execute("SELECT title, content FROM probe_record WHERE session_id = %s AND turn_lo < %s "
                                       "ORDER BY turn_lo DESC, id DESC LIMIT %s;",
                                       [it["sid"], it["lo"], 200 if prompt in ("brec2", "switch") else 20])
                            so_far = [{"title": t_, "content": c_ if j < 20 else ""}
                                      for j, (t_, c_) in enumerate(cc.fetchall())][::-1]
                        c.rollback()
                    earlier_ = [{k: e[k] for k in ("n", "thread", "kind", "what")} for e in shown]
                    # SPLIT (founder 2026-09-23: "one prompt will do one job"):
                    # the records call sees only the records so far, the lines
                    # call only the earlier lines and threads
                    calls = ([("records", {"records_so_far": so_far}),
                              ("lines", {"earlier": earlier_, "threads": names})] if prompt == "split"
                             # the batch records call alone (2026-09-23): the key
                             # scores records, the decision call is judged apart
                             else [("brec", {"records_so_far": so_far})] if prompt in ("brec", "bclean", "bsplitrec")
                             else [("brec2", {"records_so_far": so_far})] if prompt == "brec2"
                             else [("brec3", {"records_so_far": so_far})] if prompt == "brec3"
                             else [("brec4", {"records_so_far": so_far})] if prompt == "brec4"
                             else [("brec5", {"records_so_far": so_far})] if prompt == "brec5"
                             else [("brec", {"records_so_far": so_far}),
                                   ("blines", {"earlier": earlier_, "threads": names})] if prompt == "bsplit"
                             # the lines call alone, with its notes step (2026-09-24):
                             # the decisions key scores lines, the records are ticked
                             else [("blines2", {"earlier": earlier_, "threads": names})] if prompt == "blines2"
                             else [("blines3", {"earlier": earlier_, "threads": names})] if prompt == "blines3"
                             else [("blines4", {"earlier": earlier_, "threads": names})] if prompt == "blines4"
                             else [("blines5", {"earlier": earlier_, "threads": names})] if prompt == "blines5"
                             # THREADS, THREE WAYS (2026-09-25): N the old view; V and NV
                             # see the project's open questions instead of the last lines
                             # THE SWITCH (2026-09-26): production's own judgement, hub/judge.curate_window —
                             # the records call with the ledger and the V6 lines call with the view, one ask
                             else [("switch", {"open_threads": opened, "records_so_far": so_far})] if prompt == "switch"
                             else [("thN", {"earlier": earlier_, "threads": names})] if prompt == "thN"
                             else [(prompt, {"open_threads": opened})] if prompt in ("thV", "thNV")
                             # V2: the same prompt as V; half the view is the questions nearest this piece
                             else [("thV", {"open_threads": opened})] if prompt == "thV2"
                             # V3: the nudge, with V2's view
                             else [("thV3", {"open_threads": opened})] if prompt == "thV3"
                             # V4: the piece's new questions listed once (V2's view)
                             else [("thV4", {"open_threads": opened})] if prompt == "thV4"
                             # V5: holds and ruled-out options are moves (V2's view)
                             else [("thV5", {"open_threads": opened})] if prompt == "thV5"
                             # V6: V5 plus the look back at the whys (V2's view)
                             else [("thV6", {"open_threads": opened})] if prompt == "thV6"
                             # V7: V6 with the holds sentence narrowed; V8: the look-back alone
                             else [("thV7", {"open_threads": opened})] if prompt == "thV7"
                             else [("thV8", {"open_threads": opened})] if prompt == "thV8"
                             # V9: V6 with the nearest existing lines in view (2026-09-26)
                             else [("thV9", {"open_threads": opened, "near_lines": near_})] if prompt == "thV9"
                             # both texts in one read (founder 2026-09-25)
                             else [("both", {"earlier": earlier_, "threads": names, "records_so_far": so_far})] if prompt == "both"
                             else [("both2", {"earlier": earlier_, "threads": names, "records_so_far": so_far})] if prompt == "both2"
                             else [("both3", {"earlier": earlier_, "threads": names, "records_so_far": so_far})] if prompt == "both3"
                             else [("blines", {"earlier": earlier_, "threads": names})] if prompt == "blines"
                             else [(prompt, {"earlier": earlier_, "threads": names, "records_so_far": so_far})])
                    a: dict = {}

                    def _fail(why: str) -> None:
                        """A lost window is counted AND named in the run's
                        result, so the done line says which and why."""
                        tot["failed"] += 1
                        tot.setdefault("failures", []).append(why[:200])
                        print(f"[decisions] {why}", flush=True)

                    if prompt == "split3":
                        # THE SPLIT (2026-09-24): one read lists and routes the
                        # items; the writers get only theirs; "nothing" is
                        # dropped here and logged for reading
                        try:
                            ans = judge.ask("curate_probe", {"project": it["project"], "piece": it["piece"],
                                                              "prompt": "route", "next": it["next"], "json": json_mode,
                                                              "max_tokens": 12000})
                        except judge.NoAnswer as e:
                            tot["failed"] += 1
                            print(f"[decisions] {it['sid'][:8]}#{it['lo']} route: no answer — {e.why}", flush=True)
                            return
                        tot["asked"] += 1
                        tot["tokens_in"] += int(ans.get("input_tokens") or 0)
                        tot["tokens_out"] += int(ans.get("output_tokens") or 0)
                        got = ans.get("answer")
                        if not isinstance(got, dict):
                            tot["failed"] += 1
                            print(f"[decisions] {it['sid'][:8]}#{it['lo']} route: unparseable — {str(ans.get('fault'))[:120]}", flush=True)
                            return
                        routed: dict[str, list] = {"record": [], "line": [], "nothing": []}
                        for note in got.get("notes") or []:
                            if not isinstance(note, dict):
                                continue
                            for x in note.get("items") or []:
                                if isinstance(x, dict) and str(x.get("what") or "").strip():
                                    routed.setdefault(str(x.get("to") or "nothing").strip().lower(), []).append(
                                        {"ex": note.get("ex"), "what": x["what"]})
                        for k, v in routed.items():
                            tot["routed_" + k] = tot.get("routed_" + k, 0) + len(v)
                            for x in v[:60]:
                                print(f"[route] {it['sid'][:8]}#{it['lo']} {k} ex{x['ex']}: {' '.join(str(x['what']).split())[:400]}", flush=True)
                        calls = ([("wrecs", {"records_so_far": so_far, "items": routed["record"]})] if routed["record"] else []) \
                            + ([("wlines", {"earlier": earlier_, "threads": names, "items": routed["line"]})] if routed["line"] else [])
                    for pr, extra in calls:
                        if pr == "switch":
                            try:
                                ans = judge.ask("curate_window", {"project": it["project"], "piece": it["piece"],
                                                                   "next": it["next"], "n": int(it.get("n") or 1),
                                                                   "threads": extra["open_threads"] or [],
                                                                   "records_so_far": extra["records_so_far"], **probe_flags})
                            except judge.NoAnswer as e:
                                _fail(f"{it['sid'][:8]}#{it['lo']} switch: no answer — {e.why}")
                                continue
                            tot["asked"] += 1
                            tot["tokens_in"] += int(ans.get("input_tokens") or 0)
                            tot["tokens_out"] += int(ans.get("output_tokens") or 0)
                            got = ans.get("answer") or {}
                            for f in got.get("failed") or []:
                                _fail(f"{it['sid'][:8]}#{it['lo']} switch: {f}")
                            tot["ledger"] = tot.get("ledger", 0) + len(got.get("ledger") or [])
                            for key in ("records", "decisions", "questions"):
                                if isinstance(got.get(key), list):
                                    a[key] = got[key]
                            continue
                        # ONE RETRY (2026-09-25): a lost window is a ~3-point
                        # hole in a 300-item key, and the loss is a runaway
                        # answer (notes looping to "ex":860 until the 20000-
                        # token ceiling) — the second draw usually lands
                        got = None
                        for attempt in (1, 2):
                            try:
                                ans = judge.ask("curate_probe", {"project": it["project"], "piece": it["piece"],
                                                                  "prompt": pr, "next": it["next"], "json": json_mode,
                                                                  "max_tokens": 20000 if pr in ("both", "both2", "both3", "blines5", "thN", "thV", "thNV", "thV3", "thV4", "thV5", "thV6", "thV7", "thV8", "thV9") else 12000, **extra, **probe_flags})
                            except judge.NoAnswer as e:
                                why = f"{it['sid'][:8]}#{it['lo']} {pr}: no answer — {e.why}"
                                ans, got = {}, None
                            else:
                                tot["asked"] += 1
                                tot["tokens_in"] += int(ans.get("input_tokens") or 0)
                                tot["tokens_out"] += int(ans.get("output_tokens") or 0)
                                got = ans.get("answer")
                                why = f"{it['sid'][:8]}#{it['lo']} {pr}: unparseable — {str(ans.get('fault'))[:120]}"
                                if (isinstance(got, dict) and pr not in ("wrecs", "route")
                                        and not got.get("decisions") and not got.get("records")):
                                    # parsed, but nothing written: the notes ran
                                    # to the ceiling and the decisions (or the
                                    # records) never came — a lost window too
                                    # (2026-09-25; 09-26: a RECORDS answer has no
                                    # decisions list and is not lost)
                                    why = f"{it['sid'][:8]}#{it['lo']} {pr}: no decisions in the answer — keys {sorted(got)[:8]}"
                                    got = None
                            if isinstance(got, dict):
                                break
                            if attempt == 1:
                                tot["retried"] = tot.get("retried", 0) + 1
                                print(f"[decisions] {why} — retrying once", flush=True)
                        if not isinstance(got, dict):
                            _fail(why)
                            continue
                        for key in ("records", "decisions", "questions"):
                            if isinstance(got.get(key), list):
                                a[key] = got[key]
                        # the notes, for reading how a window was judged (2026-09-24)
                        for note in (got.get("notes") or [])[:40]:
                            print(f"[notes] {it['sid'][:8]}#{it['lo']}: {' '.join(str(note).split())[:700]}", flush=True)
                    if not a:
                        return
                    # brec5: one list, each entry marked keep true/false — a SKIP entry
                    # is dropped here, mechanically (2026-09-24)
                    if prompt == "brec5":
                        skipped = [r for r in (a.get("records") or []) if isinstance(r, dict) and r.get("keep") is False]
                        tot["skipped"] = tot.get("skipped", 0) + len(skipped)
                        for r in skipped[:40]:
                            print(f"[skip] {it['sid'][:8]}#{it['lo']}: {str(r.get('title'))[:200]}", flush=True)
                        a["records"] = [r for r in (a.get("records") or []) if isinstance(r, dict) and r.get("keep") is not False]
                    if prompt == "bsplitrec" and a.get("records"):
                        # THE SPLIT AFTER WRITING (2026-09-24): each written record
                        # routed as it is — record stays, graph and nothing leave
                        recs_ = [r for r in a["records"] if isinstance(r, dict) and r.get("content")]
                        listing = "RECORDS:\n" + "\n".join(
                            f"{i + 1}. {' '.join(str(r.get('title') or '').split())} — {' '.join(str(r['content']).split())}"
                            for i, r in enumerate(recs_))
                        try:
                            ans = judge.ask("curate_probe", {"project": it["project"], "piece": listing, "prompt": "splitrec",
                                                              "json": json_mode, "max_tokens": 12000})
                            tot["asked"] += 1
                            tot["tokens_in"] += int(ans.get("input_tokens") or 0)
                            tot["tokens_out"] += int(ans.get("output_tokens") or 0)
                            got = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
                            verdict = {int(x.get("n") or 0): x for x in got.get("records") or [] if isinstance(x, dict)}
                            if not verdict:
                                tot["failed"] += 1
                                print(f"[decisions] {it['sid'][:8]}#{it['lo']} splitrec: no verdict — records kept as written", flush=True)
                            out_ = []
                            for i, r in enumerate(recs_):
                                to = str((verdict.get(i + 1) or {}).get("to") or "record").strip().lower()
                                tot["split_" + to] = tot.get("split_" + to, 0) + 1
                                if to != "record":
                                    print(f"[split] {it['sid'][:8]}#{it['lo']} {to}: {str(r.get('title'))[:120]} — "
                                          f"{' '.join(str(r.get('content')).split())[:300]}", flush=True)
                                    continue
                                out_.append(r)
                            a["records"] = out_
                        except judge.NoAnswer as e:
                            tot["failed"] += 1
                            print(f"[decisions] {it['sid'][:8]}#{it['lo']} splitrec: no answer — {e.why}", flush=True)
                    if prompt == "bclean" and a.get("records"):
                        # THE CLEANUP (2026-09-24): the window's records, no chat —
                        # keep / rewrite as the lasting state / drop
                        recs_ = [r for r in a["records"] if isinstance(r, dict) and r.get("content")]
                        listing = "RECORDS:\n" + "\n".join(
                            f"{i + 1}. {' '.join(str(r.get('title') or '').split())} — {' '.join(str(r['content']).split())}"
                            for i, r in enumerate(recs_))
                        try:
                            ans = judge.ask("curate_probe", {"project": it["project"], "piece": listing, "prompt": "clean",
                                                              "json": json_mode, "max_tokens": 12000})
                            tot["asked"] += 1
                            tot["tokens_in"] += int(ans.get("input_tokens") or 0)
                            tot["tokens_out"] += int(ans.get("output_tokens") or 0)
                            got = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
                            verdict = {int(x.get("n") or 0): x for x in got.get("records") or [] if isinstance(x, dict)}
                            if not verdict:
                                tot["failed"] += 1
                                print(f"[decisions] {it['sid'][:8]}#{it['lo']} clean: no verdict — records kept as written", flush=True)
                            out_ = []
                            for i, r in enumerate(recs_):
                                v_ = verdict.get(i + 1) or {}
                                do = str(v_.get("do") or "keep").strip().lower()
                                tot["clean_" + do] = tot.get("clean_" + do, 0) + 1
                                if do == "drop":
                                    print(f"[clean] {it['sid'][:8]}#{it['lo']} drop: {str(r.get('title'))[:160]}", flush=True)
                                    continue
                                if do == "rewrite" and str(v_.get("content") or "").strip():
                                    r = dict(r, title=str(v_.get("title") or r.get("title")), content=str(v_["content"]))
                                out_.append(r)
                            a["records"] = out_
                        except judge.NoAnswer as e:
                            tot["failed"] += 1
                            print(f"[decisions] {it['sid'][:8]}#{it['lo']} clean: no answer — {e.why}", flush=True)
                    kept, dropped = runaway_guard(a.get("records") or [], int(it.get("n") or 1))
                    if dropped:
                        tot["runaway_dropped"] = tot.get("runaway_dropped", 0) + dropped
                        print(f"[decisions] {it['sid'][:8]}#{it['lo']}: runaway guard dropped {dropped} records", flush=True)
                    a["records"] = kept
                    with store.connection() as c:
                        cc = c.cursor()
                        n = decisionline.write(cc, it["project"], a.get("decisions") or [], it["sid"],
                                               it["lo"], it["hi"], it["day"], it["piece"], shown=shown,
                                               threads=opened, questions=a.get("questions"),
                                               ranges=it.get("ranges"))
                        for r in a.get("records") or []:
                            if isinstance(r, dict) and r.get("content"):
                                title = str(r.get("title") or "")[:300]
                                rep = " ".join(str(r.get("replaces") or "").split())[:300]
                                if seen_records and rep and rep.lower() != title.lower():
                                    # "replaces" names a listed record the new one overtakes
                                    cc.execute("UPDATE probe_record SET title = %s, content = %s, turn_lo = %s, type = %s, "
                                               "replaced = replaced + 1 WHERE id = (SELECT id FROM probe_record "
                                               "WHERE session_id = %s AND lower(title) = lower(%s) ORDER BY id DESC LIMIT 1) RETURNING id;",
                                               [title, str(r.get("content") or "")[:2000], it["lo"], str(r.get("type") or "")[:40],
                                                it["sid"], rep])
                                    if cc.fetchone():
                                        tot["replaced_by_name"] = tot.get("replaced_by_name", 0) + 1
                                        continue
                                if seen_records:
                                    # the same title in the same session replaces the older record
                                    cc.execute("UPDATE probe_record SET content = %s, turn_lo = %s, type = %s, replaced = replaced + 1 "
                                               "WHERE session_id = %s AND lower(title) = lower(%s) RETURNING id;",
                                               [str(r.get("content") or "")[:2000], it["lo"], str(r.get("type") or "")[:40],
                                                it["sid"], title])
                                    if cc.fetchone():
                                        tot["replaced"] = tot.get("replaced", 0) + 1
                                        continue
                                cc.execute("INSERT INTO probe_record (project, session_id, turn_lo, type, title, content) "
                                           "VALUES (%s, %s, %s, %s, %s, %s);",
                                           [it["project"], it["sid"], it["lo"], str(r.get("type") or "")[:40],
                                            title, str(r.get("content") or "")[:2000]])
                        c.commit()
                    tot["lines"] += n
                    print(f"[decisions] {it['project']} {it['sid'][:8]}#{it['lo']}-{it['hi']}: {n} lines", flush=True)
                finally:
                    store.unbind(t)

            # one session's exchanges in order (a line can only follow what
            # was written before it); sessions side by side
            by_sid: dict[str, list[dict]] = {}
            for it in todo.values():
                by_sid.setdefault(it["sid"], []).append(it)

            def session(items: list[dict]) -> None:
                for it in sorted(items, key=lambda x: x["lo"]):
                    one(it)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(session, list(by_sid.values())))
            print(f"[decisions] bench {variant}: done — {tot}", flush=True)
        except Exception as e:                                 # noqa: BLE001
            print(f"[decisions] bench {variant} FAILED: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        finally:
            store.unbind(tok)

    threading.Thread(target=_run, name=f"bench-decisions-{variant}", daemon=True).start()
    return f"bench: decisions started over {tenant}"


def decision_find(variant: str, prompts: list[dict], check: bool = False, workers: int = 8,
                  version: str = "", k: int = 10, tenant: str = "") -> list[dict]:
    """What the hook would put first for each {id, project, prompt}: the
    decisions the words touch, rendered. check=False: the mechanical find
    (core/decisionline.find, the hot path's shape); check=True: the six
    candidates asked of the model (hub op decision_check), the proposal
    check's shape. tenant="memory" (2026-09-27): the live graph, read only."""
    from concurrent.futures import ThreadPoolExecutor
    from . import decisionline, judge
    tenant = tenant or tenant_of(variant)

    def one(q: dict) -> dict:
        tok = store.bind(tenant)
        try:
            with store.connection() as conn:
                cur = conn.cursor()
                project, words = str(q.get("project") or ""), str(q.get("prompt") or "")
                if not check:
                    lines = decisionline.find(cur, project, words)
                    chains = {d["id"]: decisionline.chain(cur, d["id"]) for d in lines}
                    conn.rollback()
                    return {"id": q.get("id"), "lines": lines, "shown": decisionline.render(lines, chains)}
                cand = decisionline.candidates(cur, project, words, k=k)
                conn.rollback()
            if not cand:
                return {"id": q.get("id"), "lines": [], "shown": "", "asked": []}
            try:
                ans = judge.ask("decision_check", {"project": project, "words": words, "lines": cand,
                                                   "version": version})
                a = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
                pick = {int(n) for n in (a.get("lines") or []) if str(n).strip().lstrip("-").isdigit()}
            except judge.NoAnswer as e:
                return {"id": q.get("id"), "lines": [], "shown": "", "asked": cand, "fault": e.why}
            lines = sorted([d for d in cand if d["n"] in pick], key=lambda d: (d["day"], d["id"]), reverse=True)
            with store.connection() as conn:
                cur = conn.cursor()
                chains = {d["id"]: decisionline.chain(cur, d["id"]) for d in lines}
                conn.rollback()
            return {"id": q.get("id"), "lines": lines, "shown": decisionline.render(lines, chains), "asked": cand}
        finally:
            store.unbind(tok)
    with ThreadPoolExecutor(max_workers=workers if check else 1) as ex:
        return list(ex.map(one, prompts))


def decision_dump(variant: str) -> list[dict]:
    """Every line the run wrote, for reading."""
    tok = store.bind(tenant_of(variant))
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            # a run made before a column was added still dumps (2026-09-25:
            # reading thread_id from an older run's table failed, and the
            # dump came back empty with no word said)
            from . import decisionline
            cur.execute(decisionline.DDL)
            conn.commit()
            cur.execute("SELECT project, day, kind, what, replaced, why, things, said, session_id, turn_lo, "
                        "id, thread, follows, how, thread_id FROM decision ORDER BY session_id, turn_lo, id;")
            cols = ["project", "day", "kind", "what", "replaced", "why", "things", "said", "session", "turn",
                    "id", "thread", "follows", "how", "thread_id"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            if _exists(cur, "probe_record"):
                cur.execute("SELECT project, session_id, turn_lo, type, title, content FROM probe_record "
                            "ORDER BY session_id, turn_lo, id;")
                rows += [{"record": True, "project": p, "session": sid, "turn": lo, "type": t, "title": ti,
                          "content": co} for p, sid, lo, t, ti, co in cur.fetchall()]
            conn.rollback()
        for r in rows:
            if r.get("record"):
                continue
            r["day"] = r["day"].isoformat() if r["day"] else ""
            r["things"] = list(r["things"] or [])
        return rows
    finally:
        store.unbind(tok)


def record_pairs(variant: str, near: float = 0.20) -> list[dict]:
    """The records a run wrote that say nearly the same thing — the same
    project, cosine distance under `near` between title + content. The
    shortlist only; whether a pair is one fact written twice is read."""
    from . import embed
    tok = store.bind(tenant_of(variant))
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, project, session_id, turn_lo, type, title, content FROM probe_record ORDER BY id;")
            rows = cur.fetchall()
            conn.rollback()
    finally:
        store.unbind(tok)
    # in batches: one call for a whole run times out and came back None —
    # which read as "0 pairs" (2026-09-23); a failed batch fails loudly
    texts = [f"{r[5]}. {r[6]}" for r in rows]
    vecs: list = []
    for i in range(0, len(texts), 32):
        got = embed.embed(texts[i:i + 32], timeout=90.0, kind="document")
        if not got or len(got) != len(texts[i:i + 32]):
            raise RuntimeError(f"record_pairs: embedding batch {i // 32} failed — no pairs can be judged")
        vecs += got
    import math
    def cos(a, b):
        return 1 - sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if rows[i][1] != rows[j][1] or not vecs or vecs[i] is None or vecs[j] is None:
                continue
            d = cos(vecs[i], vecs[j])
            if d < near:
                out.append({"d": round(d, 3), "a": {"turn": rows[i][3], "title": rows[i][5], "content": rows[i][6][:300]},
                            "b": {"turn": rows[j][3], "title": rows[j][5], "content": rows[j][6][:300]},
                            "project": rows[i][1], "same_exchange": rows[i][3] == rows[j][3] and rows[i][2] == rows[j][2]})
    return sorted(out, key=lambda x: x["d"])


FILED = "bench_filed"


def filed_build(sections: list[dict]) -> str:
    """HEAL WITH EYES (2026-09-29): the rules and playbook sections a heal
    replay may show beside the records, embedded once into the bench tenant
    `bench_filed` — {project ('all' = every project), kind, name, text}."""
    from . import embed, recall_pg
    secs = [x for x in sections if isinstance(x, dict) and str(x.get("text") or "").strip()]
    texts = [f"{x.get('name')}. {x.get('text')}" for x in secs]
    vecs: list = []
    for i in range(0, len(texts), 32):
        got = embed.embed(texts[i:i + 32], timeout=180.0, kind="document")
        if not got or len(got) != len(texts[i:i + 32]):
            raise RuntimeError(f"filed_build: embedding batch {i // 32} failed")
        vecs += got
    store.provision_tenant(FILED)
    tok = store.bind(FILED)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS filed_section (id serial PRIMARY KEY, project text, kind text, "
                        "name text, text text, embedding halfvec(768));")
            cur.execute("DELETE FROM filed_section;")
            for x, v in zip(secs, vecs):
                cur.execute("INSERT INTO filed_section (project, kind, name, text, embedding) VALUES (%s,%s,%s,%s,%s::halfvec);",
                            [str(x.get("project") or "all"), str(x.get("kind") or "playbook"), str(x.get("name") or ""),
                             str(x.get("text")), recall_pg._vec_lit(v)])
            conn.commit()
    finally:
        store.unbind(tok)
    return f"filed: {len(secs)} sections embedded into {FILED}"


def _filed_load() -> list[dict]:
    tok = store.bind(FILED)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT project, kind, name, text, embedding::text FROM filed_section;")
            rows = cur.fetchall()
            conn.rollback()
    finally:
        store.unbind(tok)
    if not rows:
        raise RuntimeError("heal replay with eyes: bench_filed holds no sections — run filed_build first")
    return [{"project": p, "kind": k, "name": n, "text": t, "vec": json.loads(v)} for p, k, n, t, v in rows]


def _filed_near(secs: list[dict], project: str, vecs: list, each: int = 3, cap: int = 8) -> list[dict]:
    """The sections nearest to a save's arrivals: each arrival's `each`
    nearest of its project's and the every-project sections, best first."""
    import math

    def dist(a, b):
        return 1 - sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)
    pool = [x for x in secs if x["project"] in (project, "all")]
    best: dict[int, float] = {}
    for v in vecs:
        if v is None:
            continue
        near = sorted((dist(v, x["vec"]), i) for i, x in enumerate(pool))[:each]
        for d, i in near:
            best[i] = min(best.get(i, 9.0), d)
    return [{"kind": pool[i]["kind"], "name": pool[i]["name"], "text": pool[i]["text"]}
            for i, _ in sorted(best.items(), key=lambda kv: kv[1])[:cap]]


def heal_replay(source: str, variant: str, prompt: str = "", batch: int = 0, eyes: int = 0,
                cap: int = 0, body: int = 0, sessions: list | None = None) -> str:
    """PRODUCTION HEAL OVER A BENCH RUN'S RECORDS (2026-09-23: "run it through
    the heal and see what we get"). The source run's records are stored in a
    fresh tenant's `document` table in the order they were written, one SAVE
    (one curator call) at a time, and after each save the production heal —
    core.reconcile.reconcile_batch, up to BATCH_MAX arrivals per call, the
    hub's heal_judge under TEXTS['reconcile'], heal._apply's guards — runs on
    what just landed, exactly as the box does after a save. The survivors are
    read back with bench `decision_dump`-style rows by `heal_dump`.
    In a thread; log: [healreplay] …"""
    import threading
    import re as _re
    tenant = tenant_of(variant)

    def _run() -> None:
        from . import embed, reconcile, recall_pg
        store.provision_tenant(tenant)
        tok = store.bind(tenant)
        try:
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM document;")
                conn.commit()
            stok = store.bind(tenant_of(source))
            try:
                with store.connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT project, session_id, turn_lo, type, title, content FROM probe_record "
                                "ORDER BY session_id, turn_lo, id;")
                    rows = cur.fetchall()
                    conn.rollback()
            finally:
                store.unbind(stok)
            # sessions (2026-09-29, founder: "smaller samples"): only the
            # sessions whose id starts with one of these prefixes — a paid
            # model's check on one session instead of four
            if sessions:
                rows = [r for r in rows if any(str(r[1]).startswith(p) for p in sessions)]
            saves: dict[tuple, list] = {}
            for r in rows:
                saves.setdefault((r[1], r[2]), []).append(r)
            with store.connection() as conn:
                cur = conn.cursor()
                for p in sorted({r[0] for r in rows}):
                    cur.execute("INSERT INTO project (name) VALUES (%s) ON CONFLICT DO NOTHING;", [p])
                conn.commit()
            tot = {"saves": 0, "records": 0, "calls": 0, "applied": 0, "dropped": 0, "failed": 0}
            secs = _filed_load() if eyes else []
            n = 0
            for (sid, lo), recs in saves.items():
                ids = []
                vecs = embed.embed([f"{r[4]}. {r[5]}" for r in recs], timeout=90.0, kind="document") or [None] * len(recs)
                with store.connection() as conn:
                    cur = conn.cursor()
                    for r, v in zip(recs, vecs):
                        n += 1
                        stem = "_".join(_re.findall(r"[a-z0-9]+", (r[4] or "").lower())[:8])[:60] or "note"
                        rid = f"{stem}_{n}"
                        cur.execute("INSERT INTO document (project, record_id, type, title, content, source, captured_at, meta, embedding) "
                                    "VALUES (%s, %s, %s, %s, %s, 'curator', now(), %s::jsonb, %s::halfvec);",
                                    [r[0], rid, r[3] or "note", r[4] or "", r[5] or "",
                                     json.dumps({"session": sid, "turn_lo": lo}),
                                     recall_pg._vec_lit(v) if v else None])
                        ids.append((r[0], rid))
                    conn.commit()
                tot["saves"] += 1
                tot["records"] += len(recs)
                by_p: dict[str, list] = {}
                vec_of: dict[str, list] = {}
                for (p, rid), v in zip(ids, vecs):
                    by_p.setdefault(p, []).append(rid)
                    vec_of[rid] = v
                for p, rids in by_p.items():
                    step = batch or reconcile.BATCH_MAX     # batch=1: one record per call
                    for i in range(0, len(rids), step):
                        # eyes=1: 3 nearest per arrival, 8 at most; eyes=2: 4 and 12
                        filed = (_filed_near(secs, p, [vec_of[r] for r in rids[i:i + step]],
                                             *((4, 12) if eyes >= 2 else (3, 8))) if secs else [])
                        # cap/body: stored records fetched per arrival, and how many of them in full
                        # (0 = production's CAP_BATCH/BODY_BATCH; the heal round three, 2026-09-29)
                        res = reconcile.reconcile_batch(p, rids[i:i + step], prompt=prompt, filed=filed,
                                                        **({"cap_each": cap} if cap else {}),
                                                        **({"body_each": body} if body else {}))
                        tot["tokens_in"] = tot.get("tokens_in", 0) + int(res.get("input_tokens") or 0)
                        tot["tokens_out"] = tot.get("tokens_out", 0) + int(res.get("output_tokens") or 0)
                        tot["calls"] += 1
                        if res.get("note") == "records are gone":
                            # not a failure — an earlier call merged them away,
                            # as core.reconcile.reconcile_pending counts it
                            tot["gone"] = tot.get("gone", 0) + 1
                        elif not res.get("ok"):
                            tot["failed"] += 1
                            print(f"[healreplay] {sid[:8]}#{lo}: {res.get('note')}", flush=True)
                        tot["applied"] += int(res.get("applied") or 0)
                        tot["dropped"] += int(res.get("dropped") or 0)
                print(f"[healreplay] {variant} {sid[:8]}#{lo}: {len(recs)} landed", flush=True)
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM document;")
                tot["left"] = cur.fetchone()[0]
            print(f"[healreplay] bench {variant}: done — {tot}", flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"[healreplay] bench {variant} FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            store.unbind(tok)

    threading.Thread(target=_run, daemon=True).start()
    return f"heal replay of {source} started over {tenant}"


def heal_dump(variant: str) -> list[dict]:
    """The records a heal replay left, as {session, turn, title, content}."""
    tok = store.bind(tenant_of(variant))
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT project, meta, type, title, content FROM document ORDER BY id;")
            out = [{"project": p, "session": (m or {}).get("session", ""), "turn": (m or {}).get("turn_lo", 0),
                    "type": t, "title": ti, "content": c, "record": True} for p, m, t, ti, c in cur.fetchall()]
            conn.rollback()
            return out
    finally:
        store.unbind(tok)


def record_cross(a: str, b: str) -> list[dict]:
    """For every record run `a` wrote, the nearest record run `b` wrote in
    the same session (cosine distance of title + content) — what one variant
    kept that the other has nothing close to is read from the far end of
    this list (2026-09-23: did curating a window lose records?)."""
    from . import embed
    import math

    def rows_of(v: str) -> list:
        tok = store.bind(tenant_of(v))
        try:
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, project, session_id, turn_lo, type, title, content FROM probe_record ORDER BY id;")
                out = cur.fetchall()
                conn.rollback()
                return out
        finally:
            store.unbind(tok)

    def vecs_of(rows: list) -> list:
        texts, vs = [f"{r[5]}. {r[6]}" for r in rows], []
        for i in range(0, len(texts), 32):
            got = embed.embed(texts[i:i + 32], timeout=90.0, kind="document")
            if not got or len(got) != len(texts[i:i + 32]):
                raise RuntimeError(f"record_cross: embedding batch {i // 32} failed")
            vs += got
        return vs

    def cos(x, y):
        return 1 - sum(p * q for p, q in zip(x, y)) / (math.sqrt(sum(p * p for p in x)) * math.sqrt(sum(q * q for q in y)) or 1)

    ra, rb = rows_of(a), rows_of(b)
    va, vb = vecs_of(ra), vecs_of(rb)
    out = []
    for i, r in enumerate(ra):
        best = min(((cos(va[i], vb[j]), j) for j in range(len(rb)) if rb[j][2] == r[2]), default=(1.0, -1))
        o = rb[best[1]] if best[1] >= 0 else None
        out.append({"d": round(best[0], 3), "project": r[1],
                    "a": {"turn": r[3], "title": r[5], "content": r[6][:300]},
                    "b": {"turn": o[3], "title": o[5], "content": o[6][:300]} if o else None})
    return sorted(out, key=lambda x: -x["d"])


def kept(db: str) -> str:
    """The reason a database must not be dropped, or "" (2026-10-05). A records
    rebuild is not a disposable bench: it is a day of the card and the founder's
    quota, and it cannot be recovered — the nightly dump saves only the live
    memory, and the backup cannot read the encrypted cluster. The 2026-10-03
    cleanup of 357 "unregistered" bench databases dropped the 09-29/30 rebuild
    (t_bench_tgt) this way. A database holding a `keep_reason` row is kept."""
    import psycopg
    try:
        with psycopg.connect(_dsn(db), connect_timeout=5) as conn:
            row = conn.execute("SELECT to_regclass('keep_reason') IS NOT NULL;").fetchone()
            if not (row and row[0]):
                return ""
            got = conn.execute("SELECT reason FROM keep_reason ORDER BY at DESC LIMIT 1;").fetchone()
            return str(got[0]) if got else ""
    except psycopg.OperationalError:
        return ""                                           # no such database: nothing to keep


def drop(variant: str, force: bool = False) -> str:
    """Remove the variant's database (a bench is disposable — kopia and the
    source archive hold everything it was built from) — unless it is KEPT
    (`kept`): then only `force`, said in so many words, drops it."""
    import psycopg
    tenant = tenant_of(variant)
    db = store.tenant_db(tenant)
    why = kept(db)
    if why and not force:
        return f"bench: {db} is KEPT — {why}. Not dropped; drop it with force only on the founder's word."
    p = store._pools.pop(db, None)
    if p is not None:
        try:
            p.close()
        except Exception:                                      # noqa: BLE001
            pass
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE);')
    return f"bench: {db} dropped"
