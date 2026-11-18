"""THE BOX ASKS THE HUB TO JUDGE — design step 3 (founder 2026-09-10: "the
hub is the brain, our secret and everything; the worker … is in the
container").

Every judgement the box used to run through a model of the customer's
choosing, or through the relay with a prompt of its own, is now one ask up
the pipe: the data goes up ({rid, op, body}), the hub renders the prompt
it alone holds, judges on our key, and the answer comes back as data the
box applies with the guards it already has. There is no mode: a box with
a pipe judges through the hub; a box without one judges nothing and says
so. A refusal is a plain outcome the caller reads — the provider's window
with its seconds, the graph add-on not on the plan, the allowance spent —
never an exception on a queue loop.
"""
from __future__ import annotations

import contextvars
import os

from . import pipeclient

# THE DAY A REPLAYED CONVERSATION WAS SAID (the records replay, founder
# 2026-09-26: "the only thing which we need is a date"). A thread replaying
# old chat sets it per window; every judgement it asks — the records call
# and the heal after the save — carries it as `today`, and the hub renders
# TODAY from it. Unset, the hub uses its clock.
TODAY: contextvars.ContextVar[str] = contextvars.ContextVar("brain_judge_today", default="")

# A judgement can wait on the provider for minutes (a dense stretch at
# afternoon provider speed ran 900 s under the old relay's ticket); the ask
# waits that long, the pipe idle meanwhile costs nothing.
JUDGE_TIMEOUT_S = float(os.environ.get("BRAIN_PIPE_JUDGE_TIMEOUT_S", "900"))
# (The old inventory graph's asks — graph_read, fold_judge, movement_judge,
#  gloss_judge, fact_close_judge, takeover_judge, same_judge, same_family,
#  describe — left the box on 2026-09-27; the hub still answers them for
#  boxes that run older code.)
OPS = ("heal_judge", "heal_cluster", "split_judge", "propose",
       "curate_probe", "decision_check", "curate_window", "retire_judge")


class NoAnswer(RuntimeError):
    """The hub did not judge: down, refused, or late. `retry_s` carries the
    provider window's wait when the hub sent one; `off` says the graph
    add-on is not on this plan (stop asking for a while)."""

    def __init__(self, why: str, retry_s: int = 0, off: bool = False) -> None:
        super().__init__(why)
        self.why = why
        self.retry_s = int(retry_s or 0)
        self.off = bool(off)


def ask(op: str, body: dict, timeout: float | None = None) -> dict:
    """The hub's answer for one judgement: {"answer": dict|None, "fault":
    str, "input_tokens", "output_tokens"}. Raises NoAnswer otherwise."""
    if op not in OPS:
        raise NoAnswer(f"unknown judgement {op!r}")
    if TODAY.get() and "today" not in body:
        body = dict(body, today=TODAY.get())
    try:
        ans = pipeclient.ask(op, body, timeout=timeout or JUDGE_TIMEOUT_S)
    except pipeclient.PipeDown as e:
        raise NoAnswer(str(e)) from e
    if not isinstance(ans, dict) or not ans.get("ok"):
        why = str((ans or {}).get("why") or "refused") if isinstance(ans, dict) else "malformed answer"
        try:
            retry_s = max(0, int((ans or {}).get("retry_s") or 0)) if isinstance(ans, dict) else 0
        except (TypeError, ValueError):
            retry_s = 0
        raise NoAnswer(why, retry_s, bool(isinstance(ans, dict) and ans.get("off")))
    return {"answer": ans.get("answer") if isinstance(ans.get("answer"), dict) else None,
            "fault": str(ans.get("fault") or ""),
            "model": str(ans.get("model") or ""), "seconds": float(ans.get("seconds") or 0),
            "input_tokens": int(ans.get("input_tokens") or 0),
            "output_tokens": int(ans.get("output_tokens") or 0)}
