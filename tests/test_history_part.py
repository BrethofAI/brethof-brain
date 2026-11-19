"""The prompt hook's history part (2026-09-28): the nearest decision lines,
for a tenant with the graph add-on, on the hook after the record slots.

What these protect: the part is silent without the add-on (the hub says,
never the box); it asks the box for the lines with the prompt as the query;
the block carries whole lines only, in the order found; nothing else of the
prompt hook changes for a client that never registers the part.
"""
import asyncio

from core import brieftext as T, composer


def _lines(n, what="Chose X over Y"):
    return [{"id": i, "day": "2026-09-0%d" % (1 + i % 9), "kind": "rejected", "what": f"{what} {i}",
             "why": "it cost too much"} for i in range(n)]


def _ambient(part, graph, lines, bears=None):
    asked = []

    async def ask(cmd, body):
        asked.append((cmd, body))
        if cmd == "history_near":
            return {"ok": True, "lines": lines}
        if cmd == "recall":
            return {"ok": True, "hits": []}
        if cmd == "stamps":
            return {"ok": True, "turn": 3, "stamps": {}}
        return {"ok": True}

    async def match(text):
        return None, "ok"

    async def purpose():
        return ""

    text = asyncio.run(composer.ambient(ask, match, purpose, "brain", "sess-1",
                                        f"let us sell an unlimited add-on {part} {graph}", part,
                                        scope="t", graph=graph, bears=bears))
    return text, asked


def test_no_add_on_no_block_and_no_ask():
    text, asked = _ambient(composer.HISTORY_PART, False, _lines(3))
    assert text == ""
    assert not any(c == "history_near" for c, _ in asked)


def test_the_add_on_hands_the_lines_in_order():
    text, asked = _ambient(composer.HISTORY_PART, True, _lines(3))
    got = [b for c, b in asked if c == "history_near"]
    assert len(got) == 1 and got[0]["k"] == composer.HISTORY_K and "unlimited" in got[0]["query"]
    assert text.startswith(T.HISTORY_HEAD)
    body = text.splitlines()[1:]
    assert [ln.split(":")[0] for ln in body] == ["1. rejected 2026-09-01", "2. rejected 2026-09-02",
                                                "3. rejected 2026-09-03"]
    assert "why: it cost too much" in body[0]


def test_no_lines_no_block():
    text, _ = _ambient(composer.HISTORY_PART, True, [])
    assert text == ""


def test_the_record_parts_never_ask_for_history():
    for part in (1, 2, composer.PARTS):
        _, asked = _ambient(part, True, _lines(3))
        assert not any(c == "history_near" for c, _ in asked), part


# ── part 0 (2026-11-19): every single-hook harness asks part 0 ──────────────
def test_part_0_carries_the_history_for_single_hook_harnesses():
    text, asked = _ambient(0, True, _lines(3))
    assert sum(c == "history_near" for c, _ in asked) == 1
    assert T.HISTORY_HEAD in text and "Chose X over Y 2" in text


def test_part_0_without_the_add_on_has_no_history():
    text, asked = _ambient(0, False, _lines(3))
    assert T.HISTORY_HEAD not in text


def test_part_0_history_stays_inside_the_hook_budget():
    text, _ = _ambient(0, True, _lines(60, what="y" * 300))
    assert T.HISTORY_HEAD in text and len(text) <= T.HOOK_BUDGET


def test_a_line_that_does_not_fit_ends_the_block_never_cut():
    lines = _lines(40, what="x" * 400)
    block = T.history_block(lines, room=3000)
    assert len(block) <= 3000
    for ln in block.splitlines()[1:]:
        assert ln.endswith("why: it cost too much")


# ── the bar (2026-10-15): only the lines Jev says bear on the prompt ─────────
def test_only_the_lines_that_bear_on_the_prompt_go_in():
    async def bears(prompt, lines):
        return [0.9, 0.1, 0.65, 0.59, 0.6]
    text, _ = _ambient(composer.HISTORY_PART, True, _lines(5), bears=bears)
    whats = [ln.split(": ", 1)[1].split(" — ")[0] for ln in text.splitlines()[1:]]
    assert whats == ["Chose X over Y 0", "Chose X over Y 2", "Chose X over Y 4"]


def test_nothing_bears_nothing_is_said():
    async def bears(prompt, lines):
        return [0.1] * len(lines)
    text, _ = _ambient(composer.HISTORY_PART, True, _lines(5), bears=bears)
    assert text == ""


def test_without_jev_the_block_is_the_nearest_few_never_all_25():
    async def down(prompt, lines):
        return None
    for bears in (None, down):
        text, _ = _ambient(composer.HISTORY_PART, True, _lines(25), bears=bears)
        assert len(text.splitlines()) - 1 == composer.HISTORY_FALLBACK_K
