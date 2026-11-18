"""Customer-tier tool surface — intent in, results out, mechanism invisible.

FOUNDER'S DESIGN, 2026-07-28; saves went DIRECT on 2026-08-30 when the gate
died. The customer's agent gets verbs, not schema:
- SAVE writes now, to the box's own Postgres, id minted mechanically from
  the title. The agent has full freedom on curated records; overlaps are
  folded after the fact by heal, the fixer that holds the memory.
- READ is total: "client need to be able read all rules and also all curated
  info and decide if something is wrong." Everything except the service's own
  bookkeeping records (the auto-index, internal state) — those are the
  mechanism being hidden, not the customer's data.
- DELETE is real: "need to have delete because curate is just good and never
  be perfect." The chat archive stays untouchable, as everywhere.
- SEARCH is split on the founder's trust line: "curated data are truth and
  chat history can be noise full of outdated info." Two tools, honest names,
  so the calling agent knows which one it is trusting.

Descriptions on these tools (mcp_app.CUSTOMER_SPECS) state capability only.
No retrieval or storage vocabulary crosses the wire.
"""
from __future__ import annotations

import json
import re
import traceback
from contextvars import ContextVar

from core import (embed, ledger, playbooks, quota, recall_pg, store,
                  tools)
from . import curate_queue

# The service's own records — never listed, fetched, or deleted by a customer.
_SYSTEM_IDS = {"memory_index", "changelog", "open_loops"}
_SYSTEM_PROJECTS = {"state"}

# A project name is a bare identifier: lowercase word, digits, underscores, up
# to 32 chars (matches the control plane's scope validator). Anything else —
# slashes, dots, spaces, path-traversal shapes — is refused at the door, not
# quietly filed as a garbage project the customer never meant to create.
_PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _bad_project(name: str) -> str:
    """Return a refusal string if `name` is not a valid project id, else ''."""
    if not _PROJECT_NAME_RE.match(name or ""):
        return ("Error: invalid project name — use lowercase letters, digits "
                "and underscores (e.g. 'my_app'), up to 32 characters.")
    return ""

# ── per-key project scopes (2026-07-28, founder: "3 stage read/write/not
# seen") ────────────────────────────────────────────────────────────────────
# Set per request by the MCP dispatcher. None = full access (static keys).
# {"projname": "rw"|"read"|"none", "*": default-for-the-rest}; a project at
# "none" does not exist for this key — every refusal reads like absence.
SCOPES: ContextVar[dict | None] = ContextVar("customer_scopes", default=None)
# The caller's billing plan (None = quota-exempt static keys), set per
# dispatch alongside SCOPES — quota checks read it on the tool thread.
PLAN: ContextVar[str | None] = ContextVar("customer_plan", default=None)
# Purchased top-ups riding on the key (pack credits, extra storage) —
# Lemon Squeezy, 2026-08-20. None = static key or pre-top-up control plane.
ENTS: ContextVar[dict | None] = ContextVar("customer_ents", default=None)
# SUBSCRIPTION state (active | past_due | frozen) — the door reads it
# before the meters; None for billing-exempt static keys.
STATE: ContextVar[str | None] = ContextVar("customer_state", default=None)


def _mode(project: str) -> str:
    sc = SCOPES.get()
    if sc is None:
        return "rw"
    return sc.get(project, sc.get("*", "none"))


def _visible(project: str) -> bool:
    return _mode(project) != "none"


def _writable(project: str) -> bool:
    return _mode(project) == "rw"


def _allowed_projects() -> list[str] | None:
    """Concrete visible-project list for search filters. None = unrestricted."""
    if SCOPES.get() is None:
        return None
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM project;")
        names = {r[0] for r in cur.fetchall()}
    names |= {p for p in SCOPES.get() if p != "*"}
    allowed = sorted(p for p in names if _visible(p))
    # Rules ride the general permission — visible as a block or not at all.
    if _visible("global") and "rules" not in allowed:
        allowed.append("rules")
    return allowed


def _is_system(project: str, record_id: str) -> bool:
    return record_id in _SYSTEM_IDS or project in _SYSTEM_PROJECTS


def _guarded(record_id: str, project: str) -> tuple[str, str] | str:
    try:
        p, k = tools._resolve(record_id, project)
    except ValueError as e:
        return f"Error: {e}"
    if _is_system(p, k) or not _visible(p):
        return "No such record."          # indistinguishable from absent
    return p, k


def _refresh_index(project: str) -> None:
    """Mechanical per-project index — counts by type + newest titles.

    NEVER touches a hand-written index: only writes when the existing
    `memory_index` is absent or was written by this function (meta.auto).
    The owner tenant's prose routers stay sacred. (Lived in api/gate.py
    until the gate died, 2026-08-30 — this was its one mechanical part.)"""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT meta FROM document WHERE project=%s "
                        "AND record_id='memory_index';", [project])
            row = cur.fetchone()
            if row and (row[0] or {}).get("auto") != "true":
                return                              # hand-written — hands off
            cur.execute("SELECT type, count(*) FROM document WHERE project=%s "
                        "AND record_id <> 'memory_index' GROUP BY type "
                        "ORDER BY count(*) DESC;", [project])
            counts = ", ".join(f"{t or 'note'}: {n}" for t, n in
                               cur.fetchall())
            cur.execute("SELECT record_id, coalesce(title,'') FROM document "
                        "WHERE project=%s AND record_id <> 'memory_index' "
                        "ORDER BY updated_at DESC LIMIT 20;", [project])
            newest = "\n".join(f"- {rid} — {title[:100]}" for rid, title in
                               cur.fetchall())
        body = (f"AUTO-INDEX for '{project}' (maintained by the memory "
                f"service)\nCounts: {counts or 'empty'}\n"
                f"Newest:\n{newest or '(none)'}")
        tools.save_record(project=project, record_id="memory_index",
                          fields=json.dumps({"type": "reference",
                                             "title": f"{project} index (auto)",
                                             "content": body,
                                             "auto": "true"}))
    except Exception:                                        # noqa: BLE001
        traceback.print_exc()      # an index is a convenience, never a gate


# ── saves: DIRECT filing (founder 2026-08-30 — the gate is dead) ───────────
# The agent has full freedom on curated records; a save writes NOW, to the
# box's own Postgres, with no model in between. A fact that duplicates an
# existing record is caught and reconciled AFTER by heal — the fixer that
# actually holds the memory. The id is minted mechanically from the title.
def _mint_title(content: str) -> str:
    """First sentence-ish of the content, as the record's title."""
    line = " ".join(content.split())
    for sep in (". ", "! ", "? ", "; "):
        cut = line.find(sep)
        if 20 <= cut <= 120:
            return line[:cut + 1].strip()
    return line[:100].strip()


def _queue_save(kind: str, scope: str, project: str, content: str) -> str:
    from core import tools as _core_tools
    from core.curate_apply import mint_id
    content = (content or "").strip()
    if not content:
        return "Error: nothing to remember — content is empty."
    if scope == "project" and not (project or "").strip():
        return "Error: project is required."
    target = (project or "global").strip().lower()
    if scope == "project" and (bad := _bad_project(target)):
        return bad
    gatekeeper = target if scope == "project" else "global"
    if not _visible(gatekeeper):
        return "No such project."         # a hidden project stays hidden
    if not _writable(gatekeeper):
        return ("This key has read-only access here — it cannot save. "
                "Use a key with write access.")
    # QUOTA: storage refusals still apply; no exchange is charged — a direct
    # save spends no inference (the gate that did is gone).
    v = quota.check(PLAN.get(), ents=ENTS.get())
    if not v["learn"]:
        return f"Cannot save right now: {v['warn']}"
    capnote = ""
    title = _mint_title(content)
    if kind == "rule":
        # Rules live only on the hub (2026-09-02); the hub enforces the cap
        # and the per-tenant ceiling and answers with its own reason.
        area = "all" if scope == "global" else target
        rid = mint_id(title)
        from core import hubclient, rules_hub
        try:
            d = hubclient.put_rule(rid, title, content, area, True)
        except hubclient.HubUnavailable as e:
            return (f"Cannot save the rule: the hub is unreachable ({e}). "
                    f"Rules live only on the hub; nothing was saved.")
        except hubclient.HubRefused as e:
            return f"Cannot save the rule: {e}"
        if not d.get("ok"):
            return f"Cannot save the rule: {d.get('why')}"
        rules_hub.forget()
        res = f"Saved rule {rid} on the hub."
    else:
        rid = mint_id(title)
        res = _core_tools.write_record(target, rid, "note", title, content)
        if not res.startswith("Error"):
            _refresh_index(target)
    if res.startswith("Error"):
        return res
    note = ""
    if scope == "project" and kind != "rule":
        # Teach-by-mirror (2026-08-06): implicit creation stays allowed but
        # never passes silently — the lazy path still teaches.
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM project WHERE name=%s;", [target])
            if not cur.fetchone():
                note += (f"\nThis creates NEW project '{target}' without a "
                         "purpose — the Brain will guess what matters there. "
                         "One sentence makes it sharply better: "
                         f"add_project(\"{target}\", purpose=\"...\").")
    if kind == "rule":
        note += ("\nRules TEACH the Brain: "
                 + (f"the '{target}' project's Brain specialist re-learns "
                    "from this within minutes."
                    if scope == "project" else
                    "every agent sees it at session start, and every "
                    "project's Brain specialist re-learns from it."))
    note += _core_tools._kind_hint(
        "", content, as_rule=(kind == "rule"),
        # the customer tier's own tool names — save_rule/save_memory are
        # owner-tier and do not exist on this connection
        rule_tool="save_rule",
        data_tool="the conversation")
    where = "rules" if kind == "rule" else target
    return (f"Saved as {where}:{rid} — findable in search_brain right now. "
            f"If it overlaps an existing record, the Brain's hygiene folds "
            f"them together on its next pass."
            + note + capnote)


def save_general(content: str) -> str:
    """Remember a fact true across all projects — into history now; the
    Brain keeps a record if a future session must be handed it."""
    return tools.save_memory("global", content)

def save_project(content: str, project: str) -> str:
    """Remember something about one project — into its history now; the
    Brain keeps a record if a future session must be handed it."""
    target = (project or "").strip().lower()
    if not _visible(target):
        return "No such project."
    if not _writable(target):
        return "This key has read-only access here — it cannot save."
    return tools.save_memory(target, content)

def save_rule(content: str, project: str = "", answer: str = "",
              token: str = "") -> str:
    """One rule door, gated by ONE question (founder design 2026-08-14).

    The customer agent is never handed a scope to fill — it is asked where
    the text must live, and its answer routes the save: 1 → every-project
    law, 2 → one project's law, 3 → knowledge (it was never a rule). The
    token binds the answer to the exact content, so the question cannot be
    skipped. The law budget refuses over-cap and near-duplicate rules."""
    c = (content or "").strip()
    if not c:
        return "Error: content is required."
    p = (project or "").strip().lower()
    q1 = tools._gate_tok("cq1", tools._content_sig(c))
    a = (answer or "").strip()
    if not a:
        return tools.rule_question(q1, p, tool="this tool")
    if a not in ("1", "2", "3"):
        return "Error: answer must be '1', '2' or '3'."
    import hmac as _hmac
    if not _hmac.compare_digest(token or "", q1):
        return ("Error: bad token — call again without answer= to get the "
                "question and its token.")
    if a == "1":
        return _queue_save("rule", "global", "global", c)
    if a == "2":
        if not p:
            return "Error: answer 2 (this project) needs project=<name>."
        return _queue_save("rule", "project", p, c)
    res = (_queue_save("knowledge", "project", p, c) if p
           else _queue_save("knowledge", "global", "global", c))
    if res.startswith("Error") or res.startswith("Cannot"):
        return res
    return ("Filed as KNOWLEDGE, not a rule — found when searched, costs "
            "no session context. " + res)


# Legacy doors (pre-2026-08-14 sessions still hold these tool names). Same
# gate, same question — the split surface is gone, not the compatibility.
def save_general_rule(content: str, answer: str = "", token: str = "") -> str:
    return save_rule(content, "", answer, token)


def save_project_rule(content: str, project: str, answer: str = "",
                      token: str = "") -> str:
    return save_rule(content, project, answer, token)


# ── reads: everything that is theirs ───────────────────────────────────────
def list_rules(project: str = "") -> str:
    from core import rules_hub
    law = rules_hub.fetch(project.strip().lower())
    if law["note"]:
        return f"Rules are not available: {law['note']}"
    rows = [(r["rule_id"], r.get("title") or "", r.get("content") or "",
             r.get("area") or "all") for r in law["rules"]
            if r["rule_id"] not in _SYSTEM_IDS]
    # Scoped keys: general rules ride the general permission; a rule scoped to
    # a hidden project stays hidden with it.
    rows = [r for r in rows
            if _visible("global") and (r[3] == "all" or _visible(r[3]))]
    if not rows:
        return "No rules yet."
    # TWO KINDS, shown as two kinds (founder 2026-08-07): the flat list is
    # how a cleanup script deleted another project's law without noticing.
    general, per_proj = [], {}
    for rid, title, content, area in rows:
        line = (f"  rules:{rid} — {title}: "
                f"{' '.join(content.split())[:200]}")
        if area == "all":
            general.append(line)
        else:
            per_proj.setdefault(area, []).append(line)
    parts = []
    if general:
        parts.append("GENERAL RULES — apply in every project:")
        parts += general
    for area in sorted(per_proj):
        parts.append(f"PROJECT RULES — '{area}' only:")
        parts += per_proj[area]
    # HOW TO CLEAN (founder 2026-08-07: the system never touches anyone's
    # rules — it explains, the owner's agent does the work): shown here
    # because this list is where a cleanup starts.
    parts.append(
        "\nCLEANING RULES — yours to do, and cheap: two rules saying the "
        "same rule = update ONE with the merged wording, delete the other "
        "(delete_memory rules:<id> — deletes are always free). Dead rules = "
        "delete it. Rules are injected every session and teach each "
        "project's curator, so every char here is paid for on every "
        "session start — short, current rules are what make memory sharp.")
    return "\n".join(parts)


def list_projects() -> str:
    """Every project this key can see, WITH memory's own one-line
    understanding of each (the compiled fingerprint) — the mirror that makes
    a wrong belief visible and correctable (teach-by-mirror, 2026-08-06)."""
    # The understanding shown per project is its PURPOSE — the rule its
    # owner wrote, fetched from the hub (2026-09-02); the compiled
    # fingerprint died with the box-side teacher.
    from core import rules_hub
    purposes = {}
    for r in rules_hub.fetch("")["rules"]:
        if str(r.get("rule_id") or "").startswith("purpose_"):
            purposes[str(r.get("area") or "")] = str(r.get("content") or "")
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT p.name, count(d.record_id) "
                    "FROM project p "
                    "LEFT JOIN document d "
                    "  ON d.project = p.name AND d.record_id <> 'memory_index' "
                    "WHERE p.name NOT IN ('global','rules','state') "
                    "GROUP BY p.name ORDER BY p.name;")
        rows = [(n, cnt, purposes.get(n, "")) for n, cnt in cur.fetchall()
                if _visible(n)]
    if not rows:
        return ("No projects yet — create one with "
                "add_project(project=\"<name>\", purpose=\"...\").")
    out = []
    for n, c, fp in rows:
        line = f"{n} — {c} memories"
        if fp:
            line += f"\n   purpose: {' '.join(fp.split())[:150]}"
        out.append(line)
    out.append("\n(A project's purpose is its description — the agent working "
               "in it keeps it current with save_rule.)")
    return "\n".join(out)


def add_project(project: str, purpose: str = "", rules: str = "") -> str:
    """Explicit project creation WITH onboarding (2026-08-06). The purpose
    statement is mandatory — it is what teaches the memory service what
    matters in this project, and one owner sentence outweighs anything the
    service could infer on its own (measured)."""
    p = (project or "").strip().lower()
    if p.endswith("%") and SCOPES.get() is not None:
        # A template governs projects that do not exist yet — no scoped
        # key can hold rights over future names.
        return "Templates need a full-access key."
    if not _writable(p.rstrip("%").rstrip("_") or p):
        return "This key cannot create projects."
    # Validate the base name (a trailing '%' template marker is allowed).
    if bad := _bad_project(p[:-1] if p.endswith("%") else p):
        return bad
    # (the creation tripwire and its judge flag left with the box-side
    #  judge, 2026-09-02; the hub judge sees project churn from usage)
    return tools.add_project(p, purpose, rules)


def delete_project(project: str, confirm: str = "") -> str:
    """Deletes an ENTIRE project — everything about it, permanently. Founder
    2026-08-09: a full project delete means the project is gone, so its
    curated memory, graph AND raw conversation history all go with it —
    there is no reason to keep an archive for a project that no longer
    exists.

    NOT A CUSTOMER TOOL since 2026-08-12 (founder ruling) — EXCEPT on a
    true-local box since 2026-08-31 (founder ruling): there the agent owns
    the machine, the wall is decorative, and a clean tool beats the
    improvised SQL its absence produces. Hosted stacks (panel_ops) keep the
    panel as the only surface. This is the one
    verb that reaches the chat archive at any age, and on the agent surface
    its confirm token was typed by the agent itself — a typo guard, not an
    authority check. Destructive power belongs on the surface where a HUMAN
    acts: the panel, where the account owner types the project name and the
    control plane authenticates them. The only caller is now
    /v1/internal/project_delete (main.py). With this door moved, nothing an
    agent can reach touches the archive or anything under the 90-day
    cleanup floor. A confirmed delete is final — deleted data is not
    restored for the customer, full stop.

    This and account deletion and cleanup_history are the ONLY paths that
    remove chat, and all three are explicit owner acts. The chat FK is
    RESTRICT (schema.sql), so chat can never be destroyed by an incidental
    cascade from some unrelated `DELETE FROM project` — only by the explicit
    `DELETE FROM chat` below. That is the guard from the 2026-08-09 audit:
    the accident (silent cascade) is impossible; the intent (this function)
    is honoured."""
    project = (project or "").strip().lower()
    if project in ("global", "rules", "state") or not _visible(project):
        return "No such project."
    if not _writable(project):
        return "This key has read-only access here — it cannot delete."
    if confirm.strip().lower() != project:
        return (f"This PERMANENTLY deletes everything under '{project}' — "
                f"saved memory, graph, AND all conversation history — with no "
                f"undo. To proceed, call again with confirm=\"{project}\".")
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM document WHERE project=%s;", [project])
        gone = cur.rowcount            # the RECORDS — what the caller is told
        # the project's handover notes go with it (2026-09-03). The table
        # is created on first use, so a box that never saved a note has
        # none — checked in SQL, not by ensure_ddl, so this needs no lock.
        cur.execute("SELECT to_regclass('note');")
        if (cur.fetchone() or [None])[0]:
            cur.execute("DELETE FROM note WHERE project=%s;", [project])
        cur.execute("DELETE FROM edge WHERE src_project=%s OR dst_project=%s;",
                    [project, project])
        # EVERY PROJECT-KEYED TABLE, ASKED FOR BY NAME AT RUN TIME (2026-09-10
        # night). A hand-written list drifts and has now failed twice: the
        # 09-10 delete left 119 entity_link rows, a record link row and 15
        # batches, and the fix for THAT still left ledger rows, entity_type,
        # graph_heal_state and heal_queue behind — measured on this box the
        # same night, together with 166 entity_type and 30 ledger rows from
        # long-dead 'harbor*' projects. So the sweep asks the catalogue which
        # tables carry a `project` column instead of remembering: a table
        # added tomorrow is cleaned the day it is added.
        #
        # chat is excluded because it is RESTRICT and needs the ownership-aware
        # delete below; session_owner because that delete READS it (dropping
        # the move rows first would strand every stretch moved INTO this
        # project); project is the last row of all. Per-tenant metering is
        # never project-keyed (usage_event, tool_call, quota_usage) and so
        # cannot appear here — if one ever gains the column, name it here.
        cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'project' "
            "  AND table_name NOT IN ('chat', 'project', 'session_owner') "
            "ORDER BY table_name;")
        for (t,) in cur.fetchall():
            cur.execute(f"DELETE FROM {t} WHERE project=%s;", [project])
        # The proposer's rows name projects in their own columns.
        cur.execute("SELECT to_regclass('session_proposal');")
        if (cur.fetchone() or [None])[0]:
            cur.execute("DELETE FROM session_proposal "
                        "WHERE filed_project=%s OR proposed_project=%s;",
                        [project, project])
        # EXPLICIT, INTENTIONAL chat deletion — the one deliberate door that
        # may touch the immutable archive (chat.project is RESTRICT, so this
        # must run BEFORE the project row is removed, and no accidental
        # cascade can ever reach it). A full project delete takes its
        # history; that is the founder's intent, stated in the confirm text.
        from core import ownership
        w, a = ownership.chat_where(cur, project)
        cur.execute(f"DELETE FROM chat WHERE {w};", a)
        chat_gone = cur.rowcount
        cur.execute("SELECT to_regclass('session_owner');")
        if (cur.fetchone() or [None])[0]:
            cur.execute("DELETE FROM session_owner WHERE project=%s OR from_project=%s;",
                        [project, project])
            ownership.invalidate()
        # The gate, the teach lane, the turn queue and the stored brief are
        # gone (2026-09-02); nothing per project is queued or compiled on
        # this box any more, so there is nothing to cancel.
        purged = 0
        # The project's OWN law goes too (2026-08-09, GLM-audit follow-on):
        # rules live in the 'rules' project keyed by meta.area, so they are
        # not caught by the deletes above. Without this, every deleted
        # project leaked its ★PURPOSE rule and any project-scoped rules into
        # the rules pool forever — which is how the conformance test tenant
        # accumulated dozens of stale rules and eventually flaked the gate.
        # GENERAL rules (area='all') are NOT project-scoped and stay.
        # (its rules live on the hub; deleted there below, after the row)
        # LAST: the project row itself. Safe now — chat is already gone, so
        # the RESTRICT FK is satisfied; state (CASCADE) clears with it.
        cur.execute("DELETE FROM project WHERE name=%s;", [project])
    # THE PROJECT'S FOOTPRINT ON THE HUB (founder 2026-09-03): its rules and
    # purpose, its verdicts, its kind list and its name index go in one call.
    # General law stays. Best-effort and loud — the box's delete is done.
    try:
        from core import hubclient
        d = hubclient.delete_project(project)
        print(f"[delete_project] {project}: hub footprint {d.get('gone')}", flush=True)
    except Exception as e:                                     # noqa: BLE001
        print(f"[delete_project] {project}: hub footprint NOT removed: "
              f"{type(e).__name__}: {e}", flush=True)
        conn.commit()
    return (f"Deleted project '{project}' permanently — {gone} memories and "
            f"{chat_gone} conversation turns removed"
            + (f", {purged} queued saves cancelled" if purged else "")
            + ". This cannot be undone.")


def cleanup_history(project: str, older_than_days: int = 180,
                    confirm: str = "") -> str:
    """Customer surface over the owner tool: same preview/confirm contract,
    scoped to projects this key can WRITE (pruning is a write). The tool's
    language stays capability-only — what it does, never how."""
    project = (project or "").strip().lower()
    if not _visible(project):
        return "No such project."
    if not _writable(project):
        return "This key has read-only access here — it cannot clean up."
    return tools.cleanup_history(project, older_than_days, confirm)


def list_memory(project: str, memory_type: str = "", limit: int = 50) -> str:
    project = (project or "").strip().lower()
    if project in _SYSTEM_PROJECTS or not _visible(project):
        return "No such project."
    if not tools._VALID.match(project):
        return f"Error: bad project name '{project}'."
    where, args = ("AND type = %s", [memory_type]) if memory_type else ("", [])
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT project, record_id, type, title, updated_at "
            f"FROM document WHERE project = %s AND record_id <> ALL(%s) "
            f"{where} ORDER BY updated_at DESC LIMIT %s;",
            [project, list(_SYSTEM_IDS)] + args
            + [max(1, min(int(limit), 500))])
        rows = tools._rows(cur)
    return "\n".join(f"{r['project']}:{r['record_id']} [{r['type']}] "
                     f"{r['title']} (upd {str(r['updated_at'])[:10]})"
                     for r in rows) or "No records."


def get_memory(record_id: str, project: str = "") -> str:
    g = _guarded(record_id, project)
    if isinstance(g, str):
        return g
    return tools.get_memory(f"{g[0]}:{g[1]}")


def delete_memory(record_id: str, project: str = "") -> str:
    g = _guarded(record_id, project)
    if isinstance(g, str):
        return g
    if not _writable(g[0]):
        return "This key has read-only access here — it cannot delete."
    out = tools.delete_memory(project=g[0], record_id=g[1])
    if out.startswith("DELETED"):
        # Keep the service-maintained index true, and answer in the
        # customer's vocabulary — the graph wording is the owner surface's.
        _refresh_index(g[0])
        return f"Deleted {g[0]}:{g[1]}."
    return out


# ── search: split on the trust line ────────────────────────────────────────
def _search(query_text: str, project: str, top_k: int,
            include_chat: bool, include_curated: bool, since=None, until=None) -> str:
    """Same mechanical pipeline as recall — with the service's own records
    filtered out of what a customer sees (the live test 2026-07-28 caught the
    auto-index outranking the customer's actual record)."""
    vec = embed.embed_one(query_text, timeout=12.0)
    with store.connection() as conn:
        hits = recall_pg.recall_hits(conn, vec, query_text, project=project,
                                     top_k=min(int(top_k) + 5, 30),
                                     include_chat=include_chat,
                                     include_curated=include_curated,
                                     fts="idf",
                                     allowed_projects=_allowed_projects(),
                                     since=since, until=until)
    hits = [(s, rid, e) for s, rid, e in hits
            if e["chat"] or not _is_system(e["row"].get("project") or "",
                                           e["row"].get("record_id") or "")]
    if not hits:
        return f"Nothing found for '{query_text}'."
    return "\n".join(recall_pg.line(s, rid, e)
                     for s, rid, e in hits[:max(1, int(top_k))])


def _period_scope(project: str):
    """(projects list or None, error) for a period read: one project, or every
    project this key may see."""
    p = (project or "").strip().lower()
    if p:
        return ([p], "") if _visible(p) else (None, "No such project.")
    return _allowed_projects(), ""


def search_memory(query_text: str = "", project: str = "", top_k: int = 10,
                  since: str = "", until: str = "") -> str:
    """Curated knowledge only — the current truth. With since/until: records
    changed in that period; with a period and no topic: those records listed."""
    from core import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    if not (query_text or "").strip():
        if not (s or u):
            return "Error: give query_text, or a period (since/until) to list it."
        projs, err = _period_scope(project)
        if err:
            return err
        with store.connection() as conn:
            rows = [r for r in period.records(conn.cursor(), projs, s, u, max(1, min(int(top_k), 50)))
                    if not _is_system(r.split("] ", 1)[1].split(":", 1)[0], "")]
        return (f"RECORDS CHANGED {period.label(s, u)} (newest first):\n" + "\n".join(rows)
                if rows else f"No records changed {period.label(s, u)}.")
    return _search(query_text, project, top_k, include_chat=False, include_curated=True,
                   since=s, until=u)


def search_history(query_text: str = "", project: str = "", top_k: int = 10,
                   since: str = "", until: str = "") -> str:
    """Raw conversation history only — complete, but may be outdated. With
    since/until: only what was said in that period; with a period and no
    topic: the period's decision lines in order."""
    from core import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    if not (query_text or "").strip():
        if not (s or u):
            return "Error: give query_text, or a period (since/until) to list it."
        projs, err = _period_scope(project)
        if err:
            return err
        with store.connection() as conn:
            cur = conn.cursor()
            head = period.counts(cur, projs, s, u)
            ls = period.lines(cur, projs, s, u, max(1, min(int(top_k) if int(top_k) > 10 else 50, 200)))
        return (f"HISTORY {period.label(s, u)} — {head}\n"
                + ("\n".join(ls) if ls else "(no decision lines in this period)")
                + "\nFor what was said about one thing: search_history with a topic and this period.")
    text = ("NOTE: raw history — statements here may be outdated; "
            "search_brain holds the current truth.\n"
            + _search(query_text, project, top_k,
                      include_chat=True, include_curated=False, since=s, until=u))
    # the decision lines are history too (2026-09-28): a dated section under
    # the raw rows, fenced to what this key may see
    try:
        vec = embed.embed_one(query_text, timeout=12.0)
        with store.connection() as conn:
            hist = recall_pg.history_section(conn.cursor(), vec, query_text, project=project,
                                             allowed_projects=_allowed_projects(),
                                             since=s.date() if s else None,
                                             until=u.date() if u else None)
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        hist = ""
    return text + ("\n" + hist if hist else "")


def save_verdicts(items: list, since: str, until: str = "", judged_by: str = "") -> str:
    """Keep a judge's calls on a period so the next run can re-judge them.
    Each item: {project, item, verdict, reason, line_ids}. A re-judgement is
    a new row on the same item, never an edit."""
    from core import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    with store.connection() as conn:
        n, problems = period.save_verdicts(conn.cursor(), items, s, u, judged_by,
                                           writable=lambda p: _writable(p) if p else True)
        conn.commit()
    return f"Saved {n} verdict(s)." + ("" if not problems else " Not saved: " + "; ".join(problems))


def list_verdicts(since: str = "", until: str = "", project: str = "") -> str:
    """Earlier calls on periods overlapping since–until, each item's history
    together — what a new run re-judges."""
    from core import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    projs, err = _period_scope(project)
    if err:
        return err
    with store.connection() as conn:
        rows = period.list_verdicts(conn.cursor(), projs, s, u)
    return "\n".join(rows) if rows else "No verdicts kept for this period yet."


def period_report(since: str, until: str = "", project: str = "") -> str:
    """What a period produced, counted in the memory — facts only."""
    from core import period
    s, u, err = period.parse(since, until)
    if err:
        return f"Error: {err}"
    if not s:
        return "Error: since is required (YYYY-MM-DD)."
    projs, err = _period_scope(project)
    if err:
        return err
    with store.connection() as conn:
        return period.report(conn.cursor(), projs, s, u)


def session_context(project: str) -> str:
    """Recovery path for the harness dropping a '(context N/M)' session-start
    part (Claude Code desktop, seen 2026-08-03): the same payload the hooks
    deliver, in one call. Scope-guarded like every read — a hidden project
    stays hidden."""
    p = (project or "").strip().lower()
    if not _visible(p):
        return "No such project."
    from core import injection
    # SCOPED KEYS ARE REFUSED (2026-08-08 pre-release audit). The briefing is
    # tenant-wide by construction: it lists EVERY project with record counts
    # and every general rule. /v1/hooks/session-start already refuses scoped
    # keys for exactly this reason (require_full_access); this tool reached
    # the same builder without the check, so a key fenced to one project could
    # enumerate the whole tenant — on an app tenant with a project per
    # end-user, that is one user enumerating the user base. Until the builder
    # itself is scope-aware, the fence wins over the convenience.
    if SCOPES.get() is not None:
        return ("session_context needs a full-access key — this briefing "
                "covers every project. Use list_memory / search_memory / "
                "list_rules for the projects this key can see.")
    return injection.session_blob(p, 0, tier="customer")


# ── the BRAIN LEDGER: the pile of undecided ideas, per project ─────────────
# Statusless by design (founder 2026-08-22): a row's existence means
# "undecided". Review decides each row's fate — stays, deleted, or promoted
# to a goal (and deleted). Writes are mechanical (no AI work): free of
# exchange cost, refused only when storage is hard-over.

def _storage_ok() -> str:
    v = quota.check(PLAN.get(), ents=ENTS.get())
    if not v["archive"]:
        return f"Cannot write right now: {v['warn']}"
    return ""


def list_ledger(project: str) -> str:
    project = (project or "").strip().lower()
    bad = _bad_project(project)
    if bad:
        return bad
    if project in _SYSTEM_PROJECTS or not _visible(project):
        return "No such project."
    rows = ledger.pile(project)
    if not rows:
        return (f"The Brain Ledger for '{project}' is empty — no undecided "
                f"ideas waiting.")
    head = (f"BRAIN LEDGER — '{project}': {len(rows)} undecided idea(s), "
            f"oldest first. Walk them WITH a verdict on each (done / "
            f"pointless now / still worth doing); the human decides. A "
            f"decision removes the row: drop it (remove_from_ledger), or "
            f"promote it — write the goal, hand it to whoever executes, "
            f"then remove the row. Undecided rows just stay.")
    lines = [head]
    marks = ledger.marks(project)
    for rid, content, _origin, created in rows:
        lines.append(f"[{rid}] ({str(created)[:10]}) {content}{ledger.mark_line(marks.get(rid))}")
    return "\n".join(lines)


def remove_from_ledger(ids: str, project: str) -> str:
    project = (project or "").strip().lower()
    bad = _bad_project(project)
    if bad:
        return bad
    if project in _SYSTEM_PROJECTS or not _visible(project):
        return "No such project."
    if not _writable(project):
        return "This key has read-only access here — it cannot remove."
    try:
        want = sorted({int(p) for p in re.split(r"[,\s]+", str(ids or ""))
                       if p.strip()})
    except ValueError:
        return "Error: ids must be row numbers, e.g. \"3\" or \"3,7,12\"."
    if not want:
        return "Error: no row ids given."
    removed, missing = ledger.remove_rows(project, want)
    parts = []
    if removed:
        parts.append(f"Removed {len(removed)} row(s) from the Brain Ledger: "
                     f"{', '.join(map(str, removed))}.")
    if missing:
        parts.append(f"Not on this project's ledger (nothing done): "
                     f"{', '.join(map(str, missing))}.")
    return " ".join(parts)


# ── BRAIN PLAYBOOKS: the user's own procedures, tenant-wide ────────────────
# Ride the general permission like rules do: visible with any general read,
# writable only for keys with general write.

def save_playbook(name: str, content: str, annotation: str = "") -> str:
    if not _visible("global"):
        return "No such tool for this key."
    if not _writable("global"):
        return "This key has read-only access — it cannot save playbooks."
    stop = _storage_ok()
    if stop:
        return stop
    try:
        verb = playbooks.save(name, content, annotation)
    except ValueError as e:
        return f"Error: {e}"
    name = name.strip().lower()
    if verb == "created":
        return f"Brain playbook '{name}' created ({len(content)} chars)."
    return (f"Brain playbook '{name}' UPDATED — the previous version is "
            f"overwritten ({len(content)} chars).")


def get_playbook(name: str) -> str:
    if not _visible("global"):
        return "No such playbook."
    row = playbooks.get(name)
    if not row:
        return (f"No playbook named '{(name or '').strip().lower()}'. "
                f"list_playbooks shows what exists.")
    nm, content, annotation, updated = row
    head = f"BRAIN PLAYBOOK '{nm}' (updated {str(updated)[:10]})"
    if annotation:
        head += f" — {annotation}"
    return (f"{head}\nYou run it — fetch means execute in your own "
            f"environment; the Brain only stores it.\n\n{content}")


def list_playbooks() -> str:
    if not _visible("global"):
        return "No playbooks."
    rows = playbooks.listing()
    if not rows:
        return ("No Brain playbooks saved yet. save_playbook stores a "
                "procedure any of your projects can fetch and run.")
    lines = [f"BRAIN PLAYBOOKS — {len(rows)} stored, tenant-wide "
             f"(any project can fetch and run them):"]
    for nm, annotation, chars, updated in rows:
        note = f" — {annotation}" if annotation else ""
        lines.append(f"- {nm} ({chars} chars, upd {str(updated)[:10]}){note}")
    return "\n".join(lines)


def delete_playbook(name: str) -> str:
    if not _visible("global"):
        return "No such playbook."
    if not _writable("global"):
        return "This key has read-only access — it cannot delete playbooks."
    nm = (name or "").strip().lower()
    if playbooks.delete(nm):
        return f"Brain playbook '{nm}' deleted."
    return f"No playbook named '{nm}' — nothing deleted."



# ── SESSION NOTES (founder 2026-09-03): the handover, not a record ─────────
def save_note(project: str, session_id: str, title: str, body: str) -> str:
    target = (project or "").strip()
    if not _writable(target):
        return "This key has read-only access here — it cannot save a note."
    return tools.save_note(target, session_id, title, body)


def delete_note(project: str, session_id: str) -> str:
    target = (project or "").strip()
    if not _writable(target):
        return "This key has read-only access here — it cannot delete a note."
    return tools.delete_note(target, session_id)



def update_record(record_id: str, title: str, content: str, project: str = "") -> str:
    """Edit one existing record in place — the correction door."""
    g = _guarded(record_id, project)
    if isinstance(g, str):
        return g
    if not _writable(g[0]):
        return "This key has read-only access here — it cannot edit."
    out = tools.update_memory(g[0], g[1], title, content)
    if out.startswith("Edited"):
        _refresh_index(g[0])
    return out



def list_saved(project: str, limit: int = 20) -> str:
    target = (project or "").strip().lower()
    if not _visible(target):
        return "No such project."
    return tools.list_saved(target, limit)


# ── THE TURN TOOLS (2026-10-23): hooks for a harness that has none ──────────
# A cloud agent (Grok, claude.ai, Cowork) reaches a memory only as an MCP
# connector, with no hooks and — measured 2026-10-23 — without the server
# instructions reaching its model either. So the hooks' three jobs become two
# tools the agent is told (in the account's own instructions field) to call on
# every message: begin_turn before answering — the session brief on a new
# session, then the per-prompt recall, exactly what the hooks inject — and
# end_turn after, with its reply, so the archive holds the whole exchange and
# the hub curates it like any other. They call the SAME doors the hooks call
# (api.main), so the door, metering and curation are one code path.
_PENDING: dict[tuple[str, str], str] = {}      # (tenant, session) -> the user's message


def _turn_project(project: str) -> str:
    p = (project or "").strip().lower()
    return p if p and p.replace("_", "").isalnum() else "chat"


def _hook(fn_name: str, payload: dict) -> dict:
    from api import main as hooks                # lazy: main imports this module
    tenant = store.current_tenant()
    kw = {"tenant": tenant, "_rl": None, "plan": PLAN.get(), "sub_state": STATE.get(), "_": None}
    if fn_name == "session_start":
        kw["tier"] = "customer"
    if fn_name == "stop":
        kw["ents"] = ENTS.get()
    return getattr(hooks, fn_name)(payload, **kw)


def _next_index(session_id: str) -> int:
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT coalesce(max(turn_index), -1) + 1 FROM chat WHERE session_id = %s;",
                    [session_id])
        return int(cur.fetchone()[0])


def begin_turn(message: str, project: str = "", session_id: str = "") -> str:
    """Call FIRST, with the user's message, before answering it."""
    import uuid
    msg = (message or "").strip()
    if not msg:
        return "begin_turn needs the user's message."
    if SCOPES.get() is not None:
        return "begin_turn needs a full-access key."
    proj = _turn_project(project)
    new = not session_id
    sid = session_id or f"conn-{uuid.uuid4()}"
    parts = []
    if new:
        env = _hook("session_start", {"project": proj, "session_id": sid, "source": "startup"})
        if env.get("injection"):
            parts.append(env["injection"])
    env = _hook("prompt_submit", {"project": proj, "session_id": sid, "prompt": msg})
    if env.get("injection"):
        parts.append(env["injection"])
    key = (store.current_tenant(), sid)
    # The last message's end_turn never came: its reply is gone, but the
    # user's own words still go into the archive, and the model is told —
    # a skipped end_turn otherwise loses the message without a trace.
    missed = _PENDING.pop(key, "")
    if missed:
        _hook("stop", {"project": proj, "session_id": sid, "turns": [
            {"index": _next_index(sid), "line_type": "user", "text": missed[:50000], "embed": True}]})
    _PENDING[key] = msg
    head = (f"session_id={sid} project={proj} — pass both to end_turn after you answer, "
            f"and session_id to every later begin_turn in this conversation.")
    if missed:
        head += ("\nYour previous answer in this conversation was NOT saved: end_turn was not "
                 "called after it. Call end_turn after every answer.")
    body = "\n\n".join(parts) if parts else "(nothing in memory bears on this message)"
    # Repeated last, where a long brief would otherwise bury it (2026-10-23:
    # Grok skipped end_turn on every message of one chat out of four).
    tail = (f"LAST STEP — after you have written your full answer in the chat, call end_turn "
            f"with a copy of that answer, session_id=\"{sid}\", project=\"{proj}\". "
            f"Without it this exchange is not remembered.")
    return f"{head}\n\nMEMORY FOR THIS MESSAGE — use it in your answer:\n\n{body}\n\n{tail}"


def end_turn(reply: str, session_id: str, project: str = "") -> str:
    """Call LAST, with your full reply, after answering."""
    text = (reply or "").strip()
    sid = (session_id or "").strip()
    if not text or not sid:
        return "end_turn needs your reply and the session_id begin_turn gave you."
    if SCOPES.get() is not None:
        return "end_turn needs a full-access key."
    proj = _turn_project(project)
    msg = _PENDING.pop((store.current_tenant(), sid), "")
    n = _next_index(sid)
    turns = []
    if msg:
        turns.append({"index": n, "line_type": "user", "text": msg[:50000], "embed": True})
        n += 1
    turns.append({"index": n, "line_type": "assistant", "text": text[:50000], "embed": True})
    env = _hook("stop", {"project": proj, "session_id": sid, "turns": turns})
    status = env.get("status", "ok")
    if status != "ok":
        return f"Not saved ({env.get('notice') or status})."
    return f"Saved this exchange to memory ({len(turns)} turns)."


CUSTOMER_TOOLS = {
    "list_projects": list_projects,
    "begin_turn": begin_turn,
    "end_turn": end_turn,
    "session_context": session_context,
    "add_project": add_project,
    # delete_project is deliberately ABSENT (founder 2026-08-12): full
    # project deletion — the one act that reaches the chat archive — is a
    # human act, done in the panel. See delete_project's docstring.
    # (graph, cypher and correct_graph left with the old inventory graph,
    #  2026-09-27)
    # THE PROJECT-SPLIT DOOR IS THE AGENT'S (founder 2026-09-10: "the moving
    # session should be done by agent … an MCP server call, not a button"):
    # the customer's agent names the session (or turn range) and the project;
    # our side is mechanical; the archive is never edited. The proposer's
    # lines in the brief and the prompt block name these two calls.
    "move_session": tools.move_session,
    "dismiss_proposal": tools.dismiss_proposal,
    "save_general": save_general,
    "save_project": save_project,
    "save_note": save_note,
    "delete_note": delete_note,
    "list_saved": list_saved,
    "save_rule": save_rule,
    # legacy names, hidden from the listed surface but still dispatchable —
    # a session opened before the one-door change completes its saves.
    "save_general_rule": save_general_rule,
    "save_project_rule": save_project_rule,
    "list_rules": list_rules,
    "list_brain": list_memory,
    "get_record": get_memory,
    "delete_record": delete_memory,
    "update_record": update_record,
    "search_brain": search_memory,
    "search_history": search_history,
    "period_report": period_report,
    "save_verdicts": save_verdicts,
    "list_verdicts": list_verdicts,
    "cleanup_history": cleanup_history,
    "list_ledger": list_ledger,
    "remove_from_ledger": remove_from_ledger,
    "save_playbook": save_playbook,
    "get_playbook": get_playbook,
    "list_playbooks": list_playbooks,
    "delete_playbook": delete_playbook,
}

# WHAT SURVIVES A LIMIT (founder 2026-08-25). Two kinds of tool keep
# working when the door is closed, and for two different reasons.
#
# READING, because that is the candy: "it's mechanical, only CPU, that's
# their memory." A limit never takes a customer's own memory away from
# them; only the database leaving the box on the retention ladder does.
#
# DELETING, because it is their data and their right — and because it is
# the ONE action that makes a full store smaller. An account that cannot
# write because it is out of space, and cannot delete either, is trapped
# by us. (We do not ADVERTISE this as the fix on the wall — deletion is
# deliberately gated behind confirmation, login and 2FA, so pointing a
# frustrated customer at it is not help. Refusing it would be worse.)
#
# Everything that WRITES needs a fully open door: out of allowance means
# out of allowance.
LIMITED_TOOLS = {
    # read
    "list_projects", "session_context", "list_rules",
    # the turn tools: their hook doors apply the door themselves (recall
    # always, archiving by storage) — exactly as the hooks do
    "begin_turn", "end_turn",
    "list_brain", "get_record", "search_brain", "search_history", "period_report", "list_verdicts",
    "list_ledger", "get_playbook", "list_playbooks", "list_saved",
    # delete — never blocked
    "delete_record", "delete_playbook", "cleanup_history",
    "remove_from_ledger", "delete_note",
    # the handover is the customer's own state, mechanical, no inference —
    # a limited account still leaves it for its next session
    "save_note", "save_verdicts",
    # the split door is mechanical too — no inference, the customer's own rows
    "move_session", "dismiss_proposal",
}


# TRUE-LOCAL ONLY (founder 2026-08-31): the agent owns that machine, so a
# clean deletion tool beats the improvised SQL its absence produces. Hosted
# stacks run selfhosted mode WITH panel_ops — there the panel is the surface.
from core import mode as _shape_mode      # customer_tools has its own _mode()
if _shape_mode.is_selfhosted() and not _shape_mode.panel_ops():
    CUSTOMER_TOOLS["delete_project"] = delete_project
