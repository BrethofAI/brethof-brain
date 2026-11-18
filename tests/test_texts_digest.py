"""The hub's /health carries a hash of every text so a bench run can refuse a
stale hub image (2026-09-29: a stale hub-bench ran production's old texts for
a GLM round that was read as C4 + kC2)."""
from core import prompts


def test_digest_is_stable_and_moves_with_any_text(monkeypatch):
    a = prompts.texts_digest()
    assert a == prompts.texts_digest() and len(a) == 16
    monkeypatch.setitem(prompts.TEXTS, "batch_lines_kC2", prompts.TEXTS["batch_lines_kC2"] + " ")
    assert prompts.texts_digest() != a
