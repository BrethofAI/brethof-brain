"""THE UP WIRE — the box feeds each archived exchange to the hub.

Found missing 2026-09-01, while wiring the usage panel: the hub's
/v1/exchange door (curator, teachers, ledger, the QUOTA DOOR, the usage
meter) was only ever fed by the client-side parallel-run mirror — a
transition mechanism needing config no stamped customer has. So a real
customer's exchanges archived on their box and never reached curation at
all. The BOX holds the hub key; the box is the forwarder.

Contract: fire-and-forget AFTER the archive write (the archive is the
truth and never depends on us); since 2026-09-26 one POST per WINDOW of a
session's pieces (a piece = one user turn plus the assistant turns that
follow it; a window = up to five, kept in `hub_pending` until it is full
or due — core/hubwindow); a window the hub never received goes back into
`hub_pending` and leaves again, oldest first (2026-10-03: a window lags, it
is never lost); a refusal (4xx) is final and loud; a 429 is the quota door
speaking — log it once per boot, not per message.
When this wire is active the box's own agent-offered curation queue
stands down: curation is the hub's job, one system.
"""
from __future__ import annotations

import json
import time
import os
import threading
import traceback
import urllib.request

HUB_URL = os.environ.get("BRAIN_HUB_URL", "https://hub.brethof.ai").rstrip("/")
HUB_KEY = os.environ.get("BRAIN_HUB_KEY", "")
TIMEOUT_S = float(os.environ.get("BRAIN_HUBFEED_TIMEOUT_S", "20"))

_said_overcap = False

# ── WHETHER THIS BOX IS STILL BEING HEARD ───────────────────────────────────
# Founder 2026-09-08: the resolver is "the thing which is dying the most often
# from the whole system". Twice recorded on the product side: 2026-08-23, when
# all 26 novel lanes' sends failed on a podman DNS quirk while the Brain
# itself was perfectly healthy, and the custom-network trap that "looks like a
# dead subscription". Both times this wire KNEW — it printed a line per
# dropped exchange — and the only door anyone polls, /health, went on saying
# ok. A log nobody reads is not a signal.
#
# So the outcome of the last send is kept here and /health says it. Nothing
# is retried and nothing is buffered: an exchange the hub never saw is never
# curated (no-backfill), which is exactly why being able to SEE the wire go
# quiet matters more here than anywhere else.
_WIRE = {"ok_at": 0.0, "err_at": 0.0, "err": "", "resolver": False}


def _mark_ok() -> None:
    _WIRE["ok_at"] = time.time()


def _mark_err(e: BaseException) -> None:
    _WIRE["err_at"] = time.time()
    _WIRE["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    # A NAME THAT WILL NOT RESOLVE IS THE ONE FAULT THE MACHINE CAN FIX BY
    # ITSELF — a container that re-attaches to its network gets a live
    # resolver back. Said apart so a health command can act on it.
    _WIRE["resolver"] = any(m in str(e) for m in (
        "name resolution", "Name or service not known", "nodename nor servname",
        "[Errno -3]", "[Errno -2]", "getaddrinfo"))


def state() -> dict:
    """What /health publishes. A wire with no key is not a broken wire, and a
    wire that has not been used yet is not a broken wire either."""
    if not active():
        return {"hub_wire": "no key — this box does not feed the hub",
                "hub_ok": True, "resolver_ok": True,
                "hub_err": "", "hub_err_at": ""}
    ok = _WIRE["ok_at"] >= _WIRE["err_at"]
    return {"hub_wire": "feeding" if ok else "FAILING",
            "hub_ok": ok,
            "resolver_ok": ok or not _WIRE["resolver"],
            "hub_err": "" if ok else _WIRE["err"],
            "hub_err_at": "" if ok else time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_WIRE["err_at"]))}


def active() -> bool:
    """The wire exists when the box holds a hub key."""
    return bool(HUB_KEY.strip())


def _label(role, text: str = "") -> str:
    from .chatkind import kind
    role = str(role or "user")
    if role == "user":
        k = kind(role, text)
        return k if k in ("agent report", "system notice") else "user"
    return "saved" if role == "saved" else "assistant"


def _pieces(turns: list[dict]) -> list[tuple[str, int, int]]:
    """(piece, turn_lo, turn_hi) per exchange — the turn range is the
    piece's PROVENANCE (2026-09-09): the hub hands it back with the save so
    every record, movement and ledger row points at its turns."""
    exchanges: list[list[dict]] = []
    for i, t in enumerate(turns):
        if not (t.get("text") or "").strip():
            continue
        # THE SAME INDEX THE ARCHIVE WROTE: archive_turns stores
        # int(t.get("index", i)) — a turn without an index (a save_memory
        # turn) is archived at its position, so the range says the same.
        t = dict(t)
        if not isinstance(t.get("index", t.get("turn_index")), int):
            t["index"] = i
        role = t.get("line_type") or t.get("role") or "user"
        from .chatkind import kind
        k = kind(role, t.get("text"))
        if k == "recap":
            continue
        # an agent's report or a harness notice is part of the work under
        # way, never a new prompt of the founder's
        if k in ("agent report", "system notice"):
            role = "assistant"
        if role == "user" or not exchanges:
            exchanges.append([t])
        else:
            exchanges[-1].append(t)
    out = []
    for ex in exchanges:
        piece = "\n\n".join(
            f"[{_label(t.get('line_type') or t.get('role'), t.get('text', ''))}] "
            f"{t.get('text', '')}" for t in ex)
        if piece.strip():
            idx = [t.get("index", t.get("turn_index")) for t in ex]
            idx = [int(i) for i in idx if isinstance(i, int) and not isinstance(i, bool)]
            lo, hi = (min(idx), max(idx)) if idx else (-1, -1)
            out.append((piece, lo, hi))
    return out


RETRY_S = (20, 40, 60)      # a hub deploy is seconds; this covers two minutes


def _post(req) -> None:
    """One exchange, delivered or raised. A refused connection or a 5xx —
    the hub restarting behind its tunnel — is retried across two minutes
    (2026-09-03: a save posted during a deploy was lost, and the hub's queue
    is in memory by design). A 4xx is the hub's answer and is not retried.
    This is delivery of a fresh exchange, not back-curation."""
    last = None
    for i, wait in enumerate((0,) + RETRY_S):
        if wait:
            time.sleep(wait)
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
            _mark_ok()
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:                                # noqa: BLE001
            last = e
    raise last


def _send(project: str, session_id: str, piece: str, turn_lo: int = -1,
          turn_hi: int = -1) -> None:
    global _said_overcap
    req = urllib.request.Request(
        f"{HUB_URL}/v1/exchange/{project}",
        data=json.dumps({"session_id": session_id, "piece": piece,
                         "turn_lo": turn_lo, "turn_hi": turn_hi}).encode(),
        headers={"Authorization": f"Bearer {HUB_KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "brain-box-hubfeed/1.0"})
    try:
        _post(req)
    except urllib.error.HTTPError as e:
        # The hub ANSWERED. The wire is up; what it said is a separate matter,
        # and 429 is the quota door speaking, not a broken connection.
        _mark_ok()
        if e.code == 429:
            if not _said_overcap:
                _said_overcap = True
                print("[hubfeed] over monthly allowance — learning paused "
                      "until next period (archive and recall unaffected). "
                      "Said once; the hub answers 429 until the month "
                      "turns.", flush=True)
        else:
            print(f"[hubfeed] hub refused exchange ({e.code}) for "
                  f"{project}/{session_id[:8]} — this exchange is never "
                  f"curated (no-backfill)", flush=True)
    except Exception as e:  # noqa: BLE001
        _mark_err(e)
        print(f"[hubfeed] hub unreachable ({type(e).__name__}) for "
              f"{project}/{session_id[:8]} — this exchange is never "
              f"curated (no-backfill)", flush=True)


def _send_window(body: dict) -> bool:
    """One WINDOW of a session — several pieces, one post (2026-09-26). The
    same door, the same retry, the same refusals as one piece. False when the
    hub was never reached (unreachable, or a 5xx through the two minutes of
    retries): the caller puts the pieces back (2026-10-03, lag-not-lose)."""
    global _said_overcap
    project, session_id = str(body.pop("project", "")), str(body.get("session_id") or "")
    req = urllib.request.Request(
        f"{HUB_URL}/v1/exchange/{project}", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {HUB_KEY}", "Content-Type": "application/json",
                 "User-Agent": "brain-box-hubfeed/2.0"})
    try:
        _post(req)
        print(f"[hubfeed] window of {body.get('n')} sent for {project}/{session_id[:8]} "
              f"#{body.get('turn_lo')}-{body.get('turn_hi')}", flush=True)
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            _mark_err(e)
            print(f"[hubfeed] hub failing ({e.code}) for {project}/{session_id[:8]} — the window of "
                  f"{body.get('n')} waits and goes again", flush=True)
            return False
        _mark_ok()
        if e.code == 429:
            if not _said_overcap:
                _said_overcap = True
                print("[hubfeed] over monthly allowance — learning paused until next period "
                      "(archive and recall unaffected). Said once; the hub answers 429 until the month "
                      "turns.", flush=True)
        else:
            print(f"[hubfeed] hub refused window ({e.code}) for {project}/{session_id[:8]} — these "
                  f"{body.get('n')} exchanges are never curated (no-backfill)", flush=True)
    except Exception as e:  # noqa: BLE001
        _mark_err(e)
        print(f"[hubfeed] hub unreachable ({type(e).__name__}) for {project}/{session_id[:8]} — the "
              f"window of {body.get('n')} waits and goes again", flush=True)
        return False
    return True


# ── THE WINDOW (founder 2026-09-24: curate every five exchanges) ────────────
# The box keeps each archived piece in `hub_pending` (core/hubwindow) and
# sends a session's pieces as one window: at five, at a compact or at a save
# (no timers since 2026-10-02 — the founder: five messages).
# The person's next prompt pushes a full window with
# that prompt as THE NEXT MESSAGE. Tenants that fed since boot are the ones
# the flusher walks; a tenant's pending rows from before a restart go when
# it feeds again.
_TENANTS: set[str] = set()
_LOCK = threading.Lock()
_FLUSHER: threading.Thread | None = None
FLUSH_TICK_S = float(os.environ.get("BRAIN_WINDOW_TICK_S", "30"))


def _push(tenant: str, project: str, session_id: str, nxt: str = "", force: bool = False) -> int:
    """Send this session's window when it is full (or forced); answer how
    many pieces went. Runs bound to the tenant, inside its own connection."""
    from . import hubwindow, store
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            hubwindow.ensure(cur)
            cur.execute("SELECT count(*) FROM hub_pending WHERE project = %s AND session_id = %s;",
                        [project, session_id])
            n = int(cur.fetchone()[0])
            if not n or (n < hubwindow.WINDOW and not force):
                return 0
            rows = hubwindow.take(cur, project, session_id, hubwindow.WINDOW)
            body = hubwindow.build(cur, project, session_id, rows, nxt)
            conn.commit()                     # the rows are ours to send now
        body["project"] = project
        if not _send_window(body):
            with store.connection() as conn:
                hubwindow.put_back(conn.cursor(), project, session_id, rows)
                conn.commit()
            return 0
        return len(rows)
    finally:
        store.unbind(tok)


def _flush_due(tenant: str) -> None:
    from . import hubwindow, store
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            sessions = hubwindow.due(conn.cursor())
    finally:
        store.unbind(tok)
    for project, sid in sessions:
        _push(tenant, project, sid, force=True)


def _flusher() -> None:
    while True:
        time.sleep(FLUSH_TICK_S)
        with _LOCK:
            tenants = list(_TENANTS)
        for t in tenants:
            try:
                _flush_due(t)
            except Exception:  # noqa: BLE001 — the wire must never kill anything
                traceback.print_exc()


def _ensure_flusher(tenant: str) -> None:
    global _FLUSHER
    with _LOCK:
        _TENANTS.add(tenant)
        if _FLUSHER is None or not _FLUSHER.is_alive():
            _FLUSHER = threading.Thread(target=_flusher, name="hubwindow-flusher", daemon=True)
            _FLUSHER.start()


def _tenant() -> str:
    from . import store
    return store.current_tenant()


def feed(project: str, session_id: str, turns: list[dict], now: bool = False) -> None:
    """Fire-and-forget: never blocks or fails the hook that archived. The
    pieces join the session's window; a full window goes up at once.
    now=True (an explicit save, 2026-09-28): the piece is a whole unit of its
    own — it goes up at once instead of waiting out the quiet flush. The
    hosted rig found it: a save sat five minutes before the curator saw it,
    and the tool says "the Brain is reading it now"."""
    if not active() or not turns:
        return
    pieces = _pieces(turns)
    if not pieces:
        return
    tenant = _tenant()
    _ensure_flusher(tenant)

    def _run() -> None:
        from . import hubwindow, store
        try:
            tok = store.bind(tenant)
            try:
                with store.connection() as conn:
                    n = hubwindow.add(conn.cursor(), project, session_id, pieces)
                    conn.commit()
            finally:
                store.unbind(tok)
            if now or n >= hubwindow.WINDOW:
                _push(tenant, project, session_id, force=now)
        except Exception:  # noqa: BLE001 — the wire must never kill anything
            traceback.print_exc()

    threading.Thread(target=_run, name="hubfeed", daemon=True).start()


def on_prompt(project: str, session_id: str, prompt: str) -> None:
    """The person's next prompt: a full window of this session goes up with
    it as THE NEXT MESSAGE. Fire-and-forget."""
    if not active() or not session_id:
        return
    tenant = _tenant()
    threading.Thread(target=lambda: _push(tenant, project, session_id, nxt=prompt),
                     name="hubfeed-prompt", daemon=True).start()


def on_session_start(project: str, session_id: str) -> None:
    """A NEW SESSION SENDS WHAT THE LAST ONES LEFT (founder 2026-10-02: "beginning
    of new session is pretty good place to do curation … it's about the order").
    No timer sends a short window, and harnesses' session-end events are
    patchy (none on a crash; dsh, Cline's VS Code, OpenCode and Kilo have none
    on exit) — but every supported harness opens a session with the brief. So
    the pieces other sessions of this project left waiting go up now, oldest
    session first. Their age does not matter: nothing in the project came
    after them, so curating them now keeps the order. Fire-and-forget."""
    if not active():
        return
    tenant = _tenant()

    def _run() -> None:
        from . import hubwindow, store
        try:
            tok = store.bind(tenant)
            try:
                with store.connection() as conn:
                    cur = conn.cursor()
                    hubwindow.ensure(cur)
                    cur.execute("SELECT session_id FROM hub_pending WHERE project = %s AND session_id <> %s "
                                "GROUP BY session_id ORDER BY min(ts);", [project, session_id or ""])
                    left = [r[0] for r in cur.fetchall()]
                    conn.rollback()
            finally:
                store.unbind(tok)
            for sid in left:
                while _push(tenant, project, sid, force=True):
                    pass
                print(f"[hubfeed] {project}/{sid[:8]}: the leftovers went up at a new session's start", flush=True)
        except Exception:  # noqa: BLE001 — the wire must never kill anything
            traceback.print_exc()

    threading.Thread(target=_run, name="hubfeed-start", daemon=True).start()


def flush_session(project: str, session_id: str) -> None:
    """A compact: whatever this session holds goes up now, however few."""
    if not active() or not session_id:
        return
    tenant = _tenant()
    threading.Thread(target=lambda: _push(tenant, project, session_id, force=True),
                     name="hubfeed-flush", daemon=True).start()
