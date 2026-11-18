"""NUL bytes, in every path that writes arbitrary text to Postgres.

Postgres text cannot hold 0x00 and psycopg raises DataError on the whole
statement — so one stray byte from a binary paste or a truncated file fails a
save with "memory backend error", which tells an agent nothing.

core/archive.archive_turns already chose to STRIP rather than refuse, because
a bad byte must never cost a turn of the immutable archive. These tests hold
every other write path to the same answer: found 2026-08-28 by hammering, and
found twice, because the first fix guarded save_record while save_memory
writes on its own path."""
from core import tools


def test_helper_strips_and_leaves_everything_else_alone():
    assert tools._no_nul("a\x00b") == "ab"
    assert tools._no_nul("plain") == "plain"
    assert tools._no_nul("🧠 unicode ✓") == "🧠 unicode ✓"
    # non-strings pass through untouched — meta carries ints and bools
    assert tools._no_nul(42) == 42
    assert tools._no_nul(None) is None
    assert tools._no_nul(True) is True


def test_every_arbitrary_text_writer_scrubs():
    """Grep for the BEHAVIOUR: who accepts caller text and writes it?"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for path, needle in [
            ("core/tools.py", "_no_nul"),          # save_memory + save_record
            ("core/archive.py", '\\x00'),          # turns and commit messages
    ]:
        src = (root / path).read_text(encoding="utf-8")
        assert needle in src, f"{path} lost its NUL handling"
    # archive handles BOTH of its writers, not just the turn one
    arch = (root / "core/archive.py").read_text(encoding="utf-8")
    assert arch.count('replace("\\x00", "")') >= 2, \
        "archive has a text writer with no NUL scrub"


def test_save_memory_scrubs_at_the_door():
    """The signature itself must scrub, so no later branch can leak a NUL
    into a statement — save_memory writes on its own path, separate from
    save_record."""
    import inspect
    src = inspect.getsource(tools.write_record)
    first = [ln for ln in src.splitlines() if "_no_nul" in ln]
    assert first, "write_record does not scrub its inputs"
    # the history door (save_memory, 2026-09-03) scrubs too, before archiving
    door = inspect.getsource(tools.save_memory)
    assert 'replace("\\x00", "")' in door and door.index("x00") < door.index("archive_turns")
    # and it happens before any database work
    assert src.index("_no_nul") < src.index("store.connection")
