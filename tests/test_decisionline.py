"""The memory of decisions (core/decisionline, 2026-09-23): what one
exchange's lines become, and how a line is found and shown."""
import datetime as dt

from core import decisionline, embed


class Cur:
    def __init__(self, rows=(), total=0):
        self.sql, self.rows, self.total, self._next, self._one = [], list(rows), total, [], None

    def execute(self, q, a=None):
        self.sql.append((q, a))
        self._next = list(self.rows) if q.startswith("SELECT t, count(*)") else []
        self._one = (self.total,) if q.startswith("SELECT count(*) FROM decision") else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._next


def test_write_keeps_real_kinds_and_the_replaced_thing(monkeypatch):
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    cur = Cur()
    n = decisionline.write(cur, "p", [
        {"kind": "dropped", "what": "Percona was replaced by stock pgvector.", "replaced": "Percona",
         "why": "keys only protected a powered-off disk", "who": "user", "things": ["stock pgvector"],
         "said": "not in the words"},
        {"kind": "considered", "what": "an option weighed"},          # not a kind: dropped
        {"kind": "decided", "what": ""},                              # no line: dropped
    ], "s1", 3, 9, dt.date(2026, 8, 27), "the exchange words")
    assert n == 1
    ins = [a for q, a in cur.sql if q.startswith("INSERT")][0]
    assert ins[2] == "dropped" and "Percona" in ins[6] and ins[7] == ""   # quote not in the words: blank
    assert ins[1] == dt.date(2026, 8, 27)


def test_a_name_counts_only_as_a_whole_word():
    cur = Cur(rows=[("Borg", 1), ("pg", 1), ("Kopia", 1)], total=10)
    assert decisionline._named(cur, ["global"], "let's back up with borg tonight") == ["Borg"]
    assert decisionline._named(cur, ["global"], "the borgmatic wrapper") == []


def test_a_plural_names_the_thing_and_a_common_thing_names_nothing():
    cur = Cur(rows=[("MetaHuman", 4), ("graph", 60)], total=200)
    assert decisionline._named(cur, ["ms"], "build them as MetaHumans in UE5") == ["MetaHuman"]
    assert decisionline._named(cur, ["brain"], "on the graph card") == []


def test_render_puts_the_reason_on_the_line():
    out = decisionline.render([{"kind": "rejected", "day": "2026-09-22", "what": "Hide open lines on the card.",
                                "why": "they may be used someday"}])
    assert out.splitlines()[1] == ("- rejected 2026-09-22: Hide open lines on the card. — why: they may be "
                                   "used someday")
    assert decisionline.render([]) == ""


def test_a_line_follows_only_an_earlier_line_it_was_shown(monkeypatch):
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    cur = Cur()
    shown = [{"n": 1, "id": 41, "thread": "curator prompt", "kind": "decided", "what": "Add a push-back line."},
             {"n": 2, "id": 42, "thread": "hook testing", "kind": "tried", "what": "Fired the hooks by hand."}]
    decisionline.write(cur, "p", [
        {"kind": "rejected", "what": "The push-back line did not help.", "thread": "curator prompt",
         "follows": 1, "how": "overturns"},
        {"kind": "decided", "what": "Keep the ledger out.", "thread": "curator prompt", "follows": 7, "how": "continues"},
        {"kind": "tried", "what": "Ran three rounds.", "thread": "curator prompt", "follows": 1, "how": "whatever"},
        {"kind": "decided", "what": "Rename the product.", "thread": "product naming", "follows": 2, "how": "continues"},
    ], "s1", 3, 9, None, "", shown=shown)
    ins = [a for q, a in cur.sql if q.startswith("INSERT")]
    assert ins[0][11:14] == ["curator prompt", 41, "overturns"]
    assert ins[1][12:14] == [None, ""]                 # line 7 was never shown: links nowhere
    assert ins[2][12:14] == [41, "continues"]          # an unknown 'how' on a real link: continues
    assert ins[3][12:14] == [None, ""]                 # a link into another thread: dropped


def test_render_shows_a_found_line_with_what_came_after_it():
    lines = [{"id": 7, "kind": "decided", "day": "2026-09-22", "what": "Naming moves to the healer.", "why": "",
              "who": "user", "thread": "graph naming"}]
    chains = {7: [
        {"id": 9, "kind": "tried", "day": "2026-09-22", "what": "Healer-only naming made the cards worse.",
         "why": "13 right against 18", "overturned_by": None},
        {"id": 7, "kind": "decided", "day": "2026-09-22", "what": "Naming moves to the healer.", "why": "",
         "overturned_by": 9}]}
    out = decisionline.render(lines, chains).splitlines()
    assert out[1] == "- 'graph naming', newest first:"
    assert "worse" in out[2] and out[3].endswith("[overturned later]")
