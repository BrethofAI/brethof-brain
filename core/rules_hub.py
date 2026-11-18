"""RULES LIVE ON THE HUB AND IN THE SESSION'S CONTEXT — nowhere else.

Founder 2026-09-02/03: "Rules are loaded into message context. Those rules
are living in the context of the session. That's it." So the box keeps NO
copy — not a table, not a file, not a minute's memo. Session start asks the
hub for the set and puts it in the context; the start-of-message hook asks
the hub which rule the prompt touches and injects that one; save and delete
go to the hub. Without the hub there is no curation, no heal, no teaching, so
a rule has nothing to do on the box — and a box that cannot reach the hub is,
in nearly every case, one that is not paying.

Every failure is said out loud, never swallowed:
  - hub unreachable  -> the session hears "no rules this session"; a prompt
                        goes on without its reminder (our outage is not the
                        customer's fault — fail open)
  - key refused      -> the box is not active on the hub: rules are not
                        served, and neither is the ambient block (fail closed
                        — that is the gate on the paid feature).
"""
from __future__ import annotations

from . import hubclient

UNREACHABLE = ("hub unreachable — no rules this session. Recall and history "
               "keep working; the law returns with the hub.")
from .brieftext import REFUSED  # noqa: E402 — one text for the box and the hub


def fetch(project: str) -> dict:
    """{"rules": [...], "purpose": str, "note": str} — one call, no cache.
    `note` is non-empty exactly when no rules could be had, and says why."""
    try:
        data = hubclient.rules(project, timeout=5.0)
        rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
        _backfill_vectors([r for r in rules if not r.get("has_vector", True)])
        return {"rules": rules, "purpose": _purpose_of(rules, project), "note": ""}
    except hubclient.HubRefused:
        note = REFUSED
    except hubclient.HubUnavailable:
        note = UNREACHABLE
    except Exception as e:                                     # noqa: BLE001
        note = f"rules could not be fetched ({type(e).__name__})"
    print(f"[rules] {project or '(all)'}: {note}", flush=True)
    return {"rules": [], "purpose": "", "note": note}


def match(project: str, prompt: str, vector: list | None = None) -> tuple[dict | None, str]:
    """(the rule this prompt touches or None, verdict) with verdict one of
    "ok" | "refused" | "unreachable". The hub answers from its own table —
    by cosine when the prompt's vector travels with the question."""
    try:
        d = hubclient.match_rule(project, prompt, vector=vector)
        r = d.get("rule")
        return (r if isinstance(r, dict) else None), "ok"
    except hubclient.HubRefused:
        return None, "refused"
    except hubclient.HubUnavailable:
        return None, "unreachable"
    except Exception as e:                                     # noqa: BLE001
        print(f"[rules] match failed: {type(e).__name__}: {e}", flush=True)
        return None, "unreachable"


def _purpose_of(rules: list[dict], project: str) -> str:
    for r in rules:
        rid = str(r.get("rule_id") or "")
        if rid == f"purpose_{project}" or (rid.startswith("purpose_")
                                           and r.get("area") == project):
            return " ".join(str(r.get("content") or "").split())
    return ""


def by_id(rule_id: str) -> dict | None:
    """One rule, for get_memory('rules:<id>')."""
    for r in fetch("").get("rules") or []:
        if r.get("rule_id") == rule_id:
            return r
    return None


def forget(project: str = "") -> None:
    """Kept as a name for the callers that wrote a rule; there is nothing to
    forget any more — the next hook asks the hub."""
    return None


_backfilling: set = set()


def _backfill_vectors(missing: list[dict]) -> None:
    """Rules the hub holds without a vector get one from THIS box's embedder,
    in a background thread, once per process. The hub has no embedder; every
    box does. Ours and the seeds included."""
    if not missing:
        return
    import threading

    def run() -> None:
        try:
            from . import embed
            vectors = {}
            for r in missing:
                rid = str(r.get("rule_id") or "")
                if not rid or rid in _backfilling:
                    continue
                _backfilling.add(rid)
                v = embed.embed_one(f"{r.get('title') or ''} [rule] {r.get('content') or ''}",
                                    kind="document", timeout=20.0)
                if v:
                    vectors[rid] = v
            if vectors:
                d = hubclient.put_rule_vectors(vectors)
                print(f"[rules] vectors sent for {d.get('stored', 0)} rules", flush=True)
        except Exception as e:                                 # noqa: BLE001
            print(f"[rules] vector backfill failed: {type(e).__name__}: {e}", flush=True)

    threading.Thread(target=run, name="rule-vectors", daemon=True).start()
