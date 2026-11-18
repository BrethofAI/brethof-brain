"""Session notes (founder 2026-09-03): keyed by session AND project, a ring of
five per project, a session rewrites its own note, a cross-project note
counts against the target project's ring. These pin the ring's mechanics on
the real SQL through a fake cursor, and the render."""
from unittest import mock

from core import notes


class _Cur:
    """Executes nothing; records SQL and answers the two SELECTs."""
    def __init__(self, evicted=(), size=1):
        self.sql = []
        self._evicted = [(e,) for e in evicted]
        self._size = size
        self.rowcount = 1

    def execute(self, sql, args=None):
        self.sql.append((sql, args))

    def fetchall(self):
        return self._evicted

    def fetchone(self):
        return (self._size,)


def _conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def test_save_upserts_on_project_and_session_and_trims_the_ring():
    cur = _Cur(evicted=["old-session"], size=5)
    with mock.patch.object(notes, "ensure"):
        out = notes.save(_conn(cur), "marketing", "sess-1", "thread", "where it stands")
    upsert, trim = cur.sql[0], cur.sql[1]
    assert "ON CONFLICT (project, session_id)" in upsert[0]
    assert upsert[1][:2] == ["marketing", "sess-1"]
    assert "OFFSET %s" in trim[0] and trim[1][-1] == notes.RING
    assert out == {"evicted": ["old-session"], "size": 5}


def test_a_note_is_capped_to_a_pickup_not_an_essay():
    cur = _Cur()
    with mock.patch.object(notes, "ensure"):
        notes.save(_conn(cur), "p", "s", "t" * 500, "x" * 10_000)
    _, args = cur.sql[0]
    assert len(args[2]) <= notes.TITLE_CHARS + 1
    assert len(args[3]) <= notes.BODY_CHARS + 1 and args[3].endswith("…")


def test_empty_note_or_missing_key_is_refused():
    cur = _Cur()
    with mock.patch.object(notes, "ensure"):
        for bad in [("p", "", "t", "b"), ("", "s", "t", "b"), ("p", "s", "t", "   ")]:
            try:
                notes.save(_conn(cur), *bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad!r}")


def test_render_is_newest_first_headed_by_time_and_thread_and_marks_own_note():
    text = notes.render([
        {"session_id": "b", "title": "pricing", "body": "B body", "at": "2026-09-03 10:00"},
        {"session_id": "a", "title": "", "body": "A body", "at": "2026-09-02 22:00"},
    ], this_session="a")
    assert text.index("B body") < text.index("A body")
    assert "— 2026-09-03 10:00 · pricing\nB body" in text
    assert "— 2026-09-02 22:00 (this session's own note)\nA body" in text


def test_ring_is_five():
    assert notes.RING == 5


def test_owner_surface_has_notes_and_no_hand_written_records():
    from core import tools
    assert "save_note" in tools.TOOLS and "delete_note" in tools.TOOLS
    assert "save_memory" in tools.TOOLS and "save_record" not in tools.TOOLS
    assert "supersede_memory" in tools.TOOLS and "delete_memory" in tools.TOOLS
    assert "update_memory" in tools.TOOLS and "list_saved" in tools.TOOLS


def test_customer_surface_has_notes_and_no_hand_written_records():
    from api import customer_tools as ct
    assert "save_note" in ct.CUSTOMER_TOOLS and "delete_note" in ct.CUSTOMER_TOOLS
    assert "save_general" in ct.CUSTOMER_TOOLS and "save_project" in ct.CUSTOMER_TOOLS
    assert {"save_note", "delete_note"} <= ct.LIMITED_TOOLS
    assert "update_record" in ct.CUSTOMER_TOOLS and "list_saved" in ct.LIMITED_TOOLS


def test_session_start_hands_the_session_id_back_for_the_note():
    from core import injection
    line = injection._note_line("brain", "abc-123")
    assert "abc-123" in line and "save_note(project=\"brain\"" in line
    assert injection._note_line("brain", "") == ""


def test_update_memory_never_creates():
    from core import tools
    cur = mock.MagicMock(); cur.fetchone.return_value = None
    conn = mock.MagicMock(); conn.cursor.return_value = cur
    with mock.patch.object(tools.store, "connection") as c:
        c.return_value.__enter__.return_value = conn
        out = tools.update_memory("brain", "nope", "T", "body")
    assert out.startswith("Error: no record brain:nope")


def test_seeds_teach_the_four_doors_and_no_hand_saves():
    from hub import jobs
    text = " ".join(r["content"] for r in jobs.SEED_RULES.values())
    assert "save_note" in text and "save_playbook" in text and "save_rule" in text
    assert "save_memory" in text and "SAY IT" not in text
    assert "seed_four_doors" in jobs.SEED_RULES


def test_save_memory_goes_into_history_and_to_the_curator_never_a_record():
    from core import tools, archive, hubfeed
    with mock.patch.object(archive, "archive_turns", return_value=1) as arch, \
         mock.patch.object(hubfeed, "active", return_value=True), \
         mock.patch.object(hubfeed, "feed") as feed, \
         mock.patch("core.quota.bump_exchanges"):
        out = tools.save_memory("brain", "the hub db is in /srv/hub/db", title="hub db path")
    assert out.startswith("Remembered in brain's history")
    (project, sid, turns), _ = arch.call_args
    assert project == "brain" and sid.startswith("save-") and turns[0]["role"] == "saved"
    assert "SAVED TO MEMORY — hub db path" in turns[0]["text"] and "/srv/hub/db" in turns[0]["text"]
    feed.assert_called_once()
    assert feed.call_args[0][0] == "brain" and feed.call_args[0][1] == sid


def test_save_memory_refuses_rules_and_empty():
    from core import tools
    assert tools.save_memory("rules", "x").startswith("Error")
    assert tools.save_memory("brain", "   ").startswith("Error")


def test_hubfeed_labels_a_saved_turn_as_saved():
    from core import hubfeed
    pieces = hubfeed._pieces([{"role": "saved", "text": "SAVED TO MEMORY — x\nbody", "index": 7}])
    assert pieces == [("[saved] SAVED TO MEMORY — x\nbody", 7, 7)]


def test_add_project_puts_the_purpose_on_the_hub_not_through_the_record_door():
    """Broken from 2698f69 to 2026-09-03: the purpose went through
    save_record into the box's rules project and the rule-door guard refused
    it — every add_project failed and no project got a purpose."""
    from core import tools, hubclient
    put = mock.MagicMock(return_value={"ok": True})
    conn = mock.MagicMock()
    with mock.patch.object(hubclient, "put_rule", put), \
         mock.patch.object(tools.store, "connection") as c, \
         mock.patch.object(tools.archive, "ensure_project"), \
         mock.patch.object(tools, "save_record") as sr, \
         mock.patch.object(tools, "write_record") as wr:
        c.return_value.__enter__.return_value = conn
        out = tools.add_project("doorcheck", purpose="A test project.", rules="TRACK: doors.")
    assert out.startswith("Project 'doorcheck' is ready"), out
    args = put.call_args[0]
    assert args[0] == "purpose_doorcheck" and args[3] == "doorcheck" and args[4] is True
    assert "A test project." in args[2] and "CURATE — TRACK: doors." in args[2]
    sr.assert_not_called(); wr.assert_not_called()
