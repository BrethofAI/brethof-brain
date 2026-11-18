"""Inference client — provider-agnostic by construction.

ONE interface, swappable backends. Ollama Cloud first (its stated no-retention
policy is the privacy story we can disclose), DeepSeek direct if per-token
economics win at volume, self-hosted when volume justifies hardware. Building
this behind an interface costs nothing now and saves a rewrite later.

TWO RULES THIS FILE EXISTS TO ENFORCE:

1. DEGRADE, NEVER FAIL. Every entry point returns None on any failure. Callers
   fall back to the mechanical vector+BM25 path, which costs nothing and already
   answers in ~400 ms. Memory is an enhancement, never a gate — so a dead
   provider, an exhausted quota or a timeout must never surface as an error to
   the user.

2. NEVER TRUST THE MODEL'S OUTPUT SHAPE. Parse loosely, validate in code
   (rules:llm_schema_loose_not_strict). A measured run on 2026-07-25 returned
   valid JSON but silently added a field that was never requested; a stricter
   parser would have thrown away a perfectly good result.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Every way a hosted-provider call dies mid-flight. IncompleteRead (a chunked
# response cut mid-stream — seen live 2026-07-28 on the graph spike) is an
# http.client.HTTPException, which is NOT an OSError: it sailed past the old
# tuple and crashed the caller instead of degrading to None.
_NET_ERRORS = (urllib.error.URLError, OSError, ValueError,
               http.client.HTTPException)


@dataclass
class Completion:
    """What every backend returns. Token counts are load-bearing: they are how
    per-tenant inference is metered and how we learn real cost per customer."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    model: str = ""

    def cost_usd(self, in_per_m: float, out_per_m: float) -> float:
        return self.input_tokens / 1e6 * in_per_m + self.output_tokens / 1e6 * out_per_m


@dataclass
class OllamaBackend:
    """Ollama, cloud or local. The SAME class serves both: the local desktop app
    on 127.0.0.1:11434 needs no key, ollama.com needs a bearer token."""
    host: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    api_key: str = field(default_factory=lambda: os.environ.get("OLLAMA_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "deepseek-v4-flash:cloud"))
    timeout: float = 600.0

    def generate(self, prompt: str, system: str = "",
                 temperature: float = 0.0, num_ctx: int = 0,
                 seed: int = 42, think: bool | None = None) -> Completion | None:
        """temperature=0 (GREEDY) is the default on purpose.

        Curation is extraction and judgement, not writing. There is nothing to
        gain from sampling and a great deal to lose: at 0.2 the same session
        curated three times produced three different id sets for the same facts,
        which is what made memory look non-deterministic. Creativity here is a
        defect.

        num_ctx defaults to 0 = do not send it. MEASURED 2026-07-26: Ollama Cloud
        IGNORES num_ctx entirely — a 71k-token prompt kept its head at both 65536
        and 262144 — so sending a value only creates the false impression of a
        limit that is being enforced.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # seed + temperature 0 is the strongest determinism the
            # provider offers. It is not a guarantee — a hosted model
            # batches across requests — but without it there is none.
            "options": {"temperature": temperature, "seed": seed},
        }
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx
        # THINKING SHARES THE OUTPUT BUDGET. Measured 2026-07-26: a hard prompt
        # spent all 65,536 output tokens reasoning and returned an EMPTY
        # response — not truncated JSON, no answer at all. That is the true
        # cause of every "blank curation", and it is why this is switchable.
        if think is not None:
            payload["think"] = think
        if system:
            payload["system"] = system
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            self.host.rstrip("/") + "/api/generate",
            data=json.dumps(payload).encode("utf-8"), headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                res = json.load(r)
        except _NET_ERRORS as e:
            # Includes quota exhaustion and provider outage. Log loudly — a
            # silent inference failure is how v1 lost embeddings for months —
            # but return None so the caller degrades instead of erroring.
            print(f"[inference] {type(e).__name__}: {str(e)[:160]}", flush=True)
            return None
        return Completion(
            text=res.get("response", "") or "",
            input_tokens=int(res.get("prompt_eval_count") or 0),
            output_tokens=int(res.get("eval_count") or 0),
            seconds=time.time() - t0,
            model=self.model,
        )


    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0, seed: int = 42) -> dict | None:
        """`/api/chat` — the ONLY endpoint that supports tool calling.

        `/api/generate` has no `tools` parameter at all, so agentic curation
        cannot use the same path as the one-shot version. Returns the raw
        response dict (caller needs `message.tool_calls`, which has no useful
        `Completion` representation) or None on any failure, same contract as
        `generate`.
        """
        payload: dict = {"model": self.model, "messages": messages,
                         "stream": False,
                         "options": {"temperature": temperature, "seed": seed}}
        if tools:
            payload["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.host.rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except _NET_ERRORS as e:
            body = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            print(f"[inference] chat {type(e).__name__}: {str(e)[:120]} {body}",
                  flush=True)
            return None



# THE VALUE that means "do not think" on this endpoint's model. It is not the
# same string for every model and there is no portable one — see
# OpenAIBackend.generate for the measurements. Empty means "send nothing",
# which is right for a model whose default is already non-thinking.
REASONING_EFFORT_OFF = os.environ.get(
    "INFERENCE_REASONING_EFFORT", "none").strip()

@dataclass
class OpenAIBackend:
    """Any OpenAI-compatible /v1/chat/completions endpoint.

    WHY THIS EXISTS (founder, 2026-08-29). On a customer's box the mechanical
    judgment work — curating a piece, resolving a near-duplicate, spotting
    drift — was offered only to the agent in their harness, which means it is
    paid for with their most expensive model's tokens. Almost every provider
    and every local runner (Ollama, llama.cpp, LM Studio, vLLM, and the hosted
    APIs) speaks this one wire format, so letting them point the box at any
    endpoint costs us nothing and lets them run the cheap model for the
    bookkeeping while the expensive one does the real work.

    IT IS THEIR CHOICE, AND THE HONEST WARNING GOES WITH IT: a LOCAL endpoint
    keeps the property that nothing leaves the machine. A REMOTE one does not
    — the conversation piece goes to whoever they pointed at. That is theirs
    to make, but it must be said out loud where they make it, not discovered
    later. Our own claim is untouched either way: it still never comes to us.

    Same contract as OllamaBackend: returns None on any failure, never raises,
    so a dead endpoint degrades to the agent path instead of eating the work.
    """
    host: str = field(default_factory=lambda: os.environ.get("INFERENCE_URL", ""))
    api_key: str = field(default_factory=lambda: os.environ.get("INFERENCE_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("INFERENCE_MODEL", ""))
    timeout: float = 600.0

    def generate(self, prompt: str, system: str = "",
                 temperature: float = 0.0, num_ctx: int = 0,
                 seed: int = 42, think: bool | None = None) -> Completion | None:
        """`num_ctx` is accepted and ignored — an Ollama knob with no portable
        equivalent, and a performance hint rather than semantics.

        `think` IS honoured, through the OpenAI-standard `reasoning_effort`.
        It used to be dropped here too, on the reasoning that it was Ollama-only.
        It is not, and dropping it was never a lost performance hint — it was a
        lost INSTRUCTION. Measured 2026-09-01 against the ollama-cloud gateway:
        with nothing sent, deepseek-v4-flash reasons on every call (92 chars of
        `reasoning` to answer "OK"), and a ~10k-token reconcile prompt ran past
        the gateway into a 504. `think: false` inside an OpenAI-shaped body is
        ignored outright; `reasoning_effort: "none"` takes it to zero.

        An endpoint that REJECTS the field gets one retry without it — and a
        loud line, because the caller asked for reasoning off and is about to
        get reasoning on. Only a 400 naming the field retries: a timeout must
        never be retried into a more expensive call.
        """
        messages = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": prompt}]
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": False}
        # THE OFF SWITCH IS PER MODEL. Measured 2026-09-01 on the
        # ollama-cloud gateway, identical 7.2k-token prompt, reasoning chars
        # and output tokens for a one-line JSON answer:
        #
        #   effort   deepseek-v4-flash        glm-5.3-flash
        #   none     0 chars,   8 tok  OFF    0 chars, 118 tok, PROSE in
        #                                     content — reasoning is not
        #                                     suppressed, only un-separated
        #   low      182 chars, 56 tok ON     0 chars,   8 tok  OFF
        #   medium   147 chars, 49 tok ON     1084 chars, 269 tok ON
        #   unset    167 chars, 54 tok ON     412 chars, 109 tok ON
        #
        # They are OPPOSITE, and glm's mapping is a known Ollama bug
        # (ollama/ollama#18121: low and high behave as disabled, medium as
        # enabled; the native `think` param is ignored on /v1). `think:false`,
        # chat_template_kwargs.enable_thinking, /nothink and /no_think were all
        # tried on glm and none of them works.
        #
        # So the value is configuration, not a constant. Default "none"
        # because deepseek-v4-flash is the model this product runs; a GLM
        # endpoint sets INFERENCE_REASONING_EFFORT=low.
        if think is False and REASONING_EFFORT_OFF:
            payload["reasoning_effort"] = REASONING_EFFORT_OFF
        t0 = time.time()
        res, err, status = self._post(payload)
        if res is None and status == 400 and "reasoning" in err.lower() \
                and "reasoning_effort" in payload:
            print("[inference] openai: endpoint refused reasoning_effort — "
                  "retrying WITHOUT it, so THIS CALL REASONS despite "
                  "think=False", flush=True)
            payload.pop("reasoning_effort")
            res, err, status = self._post(payload)
        if res is None:
            # LOUD, like every other inference failure: the box falls back to
            # the agent path, and the operator must be able to see why.
            print(f"[inference] openai {err}", flush=True)
            return None
        try:
            text = res["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            print(f"[inference] openai: no choices in response "
                  f"{str(res)[:200]}", flush=True)
            return None
        usage = res.get("usage") or {}
        return Completion(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            seconds=time.time() - t0,
            model=res.get("model") or self.model,
        )


    def _post(self, payload: dict):
        """Returns (response, error_text, http_status). Status is 0 for a
        non-HTTP failure, so the caller can tell "the endpoint refused this
        field" from "the network died"."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.host.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r), "", 200
        except _NET_ERRORS as e:
            body = ""
            status = 0
            if isinstance(e, urllib.error.HTTPError):
                status = int(e.code or 0)
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:                            # noqa: BLE001
                    pass
            return None, f"{type(e).__name__}: {str(e)[:120]} {body}", status




def backend_from_env(model: str = "") -> OllamaBackend:
    """`model` overrides OLLAMA_MODEL for one call — the whole point is that
    swapping models is a parameter, not a redeploy, so they can be compared on
    identical input."""
    b = OllamaBackend()
    if model:
        b.model = model
    return b


def load_env(path: str) -> None:
    """Read a KEY=VALUE env file into os.environ. Values are never logged."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def parse_json_loose(text: str) -> dict | None:
    """Extract a JSON object from model output.

    Models wrap JSON in prose or ```json fences even when told not to. Strip to
    the outermost braces and parse. Returns None rather than raising — an
    unparseable curation must be dropped, never guessed at.
    """
    if not text:
        return None
    s = text.strip()
    # A reasoning model can leak its think block into the body — glm-5.3
    # under reasoning_effort does, on every call. Everything before the
    # closing tag is reasoning, never the answer, and the reasoning DRAFTS
    # JSON: the first "{" then sits inside the prose, the span is invalid,
    # and a good answer parses as None. Measured 2026-09-02 on the box's heal
    # path: a record was DROPPED after three verdicts that were all fine past
    # the tag. The hub's parser had this split since the GLM switch; this one
    # did not. An unclosed <think> (cut off mid-reasoning) parses as nothing,
    # which is right — there is no answer in it.
    if "</think>" in s:
        s = s.split("</think>", 1)[1].strip()
    elif s.startswith("<think>"):
        return None
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    span = s[start:end + 1]
    try:
        obj = json.loads(span)
    except ValueError:
        try:
            obj = json.loads(repair_json(span))
        except ValueError:
            return salvage_truncated(s)
    return obj if isinstance(obj, dict) else None


def json_fault(text: str) -> str:
    """WHERE a model's JSON breaks, for the log: the parser's message and
    position on the outer {...} span, before and after the mender, with the
    text around the break. A diagnosis line, never a verdict."""
    s = (text or "").strip()
    if "</think>" in s:
        s = s.split("</think>", 1)[1].strip()
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b <= a:
        # what came back instead, so a missing answer can be told from a
        # prose one (2026-09-23: windows failed this way with no clue why)
        return f"no {{...}} span in the answer — {len(s)} chars: {s[:200]!r}"
    span = s[a:b + 1]
    out = []
    for label, cand in (("raw", span), ("mended", repair_json(span))):
        try:
            json.loads(cand)
            out.append(f"{label}: parses")
        except json.JSONDecodeError as e:
            around = " ".join(cand[max(0, e.pos - 120):e.pos + 60].split())
            out.append(f"{label}: {e.msg} at {e.pos}/{len(cand)} near {around!r}")
    return "; ".join(out)


def repair_json(s: str) -> str:
    """Mend the three faults a model puts in otherwise-complete JSON — and
    nothing else. A raw double quote INSIDE a string (the graph reader quotes
    the stretch word for word in "said", and the stretch is full of quotes);
    a raw newline or tab inside a string; a trailing comma before } or ].
    Mechanical: a character walk with an in-string flag. A quote inside a
    string is a closing quote when the next non-blank character is one of
    , } ] : or the end; otherwise it is escaped in place.

    MEASURED 2026-09-09 on bv: an afternoon of batches whose answers were
    complete by head and tail and still parsed as nothing; each was bought
    again two, three, four times (tokens summed on the row) until an answer
    happened to carry no stray quote."""
    out: list[str] = []
    in_str = esc = False
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j >= n or s[j] in ",}]:":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                pass
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
                out.append(ch)
            elif ch in "}]":
                k = len(out) - 1
                while k >= 0 and out[k] in " \t\r\n":
                    k -= 1
                if k >= 0 and out[k] == ",":
                    del out[k]
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _salvage_strings(s: str, key: str) -> list[str]:
    """The closed strings of a `"key": [ "a", "b", ... ]` array in text that
    may be cut off; a partial last string is dropped."""
    i = s.find('"' + key + '"')
    if i == -1:
        return []
    start = s.find("[", i)
    if start == -1:
        return []
    out: list[str] = []
    buf: list[str] = []
    in_str = esc = False
    for ch in s[start + 1:]:
        if in_str:
            if esc:
                buf.append(ch)
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                out.append("".join(buf))
                buf = []
                in_str = False
            else:
                buf.append(ch)
            continue
        if ch == '"':
            in_str = True
        elif ch in "]{":
            break
    return out


def salvage_graph(s: str) -> dict | None:
    """The graph reader's shape — {"known":[…],"new":[…],"relations":[…]} —
    recovered from an answer that was cut off or is broken past repair: every
    closed object of each array is kept, the partial tail is dropped. The
    records-shaped salvage_truncated does not know these keys and answers
    None to every graph answer, complete or not."""
    if not s:
        return None
    if "</think>" in s:
        s = s.split("</think>", 1)[1]
    known = _salvage_strings(s, "known")
    new = _salvage_array(s, "new")
    rels = _salvage_array(s, "relations")
    moves = _salvage_array(s, "movements")
    ended = _salvage_array(s, "ended")
    cont = _salvage_array(s, "continues")
    if not (known or new or rels or moves or ended or cont):
        return None
    print(f"[inference] SALVAGED {len(known)} known + {len(new)} new + "
          f"{len(rels)} relations from a broken graph answer "
          f"({len(s):,} chars)", flush=True)
    return {"known": known, "new": new, "relations": rels, "movements": moves, "ended": ended,
            "continues": cont, "_truncated": True}


def _salvage_array(s: str, key: str) -> list:
    """Complete objects from a `"key": [ ... ]` array in a string that may be
    CUT OFF mid-JSON. Balanced-brace scan; keeps every object that closed,
    discards a partial tail; ignores braces inside strings."""
    i = s.find('"' + key + '"')
    if i == -1:
        return []
    start = s.find("[", i)
    if start == -1:
        return []
    objs, buf, depth = [], [], 0
    in_str = esc = False
    for ch in s[start + 1:]:
        if depth:
            buf.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if not depth:
                buf = ["{"]
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    objs.append(json.loads("".join(buf)))
                except ValueError:
                    pass
                buf = []
        elif ch == "]" and not depth:
            break
    return objs


def salvage_ops(s: str) -> dict | None:
    """Live-curator counterpart of salvage_truncated (2026-08-09, GLM
    engineering audit). The per-turn curator emits {"ops":[...],
    "entities":[...]} — a DIFFERENT shape from the batch curator's
    {"records":...}. salvage_truncated only knew "records", so a live
    curation that hit the 65k output ceiling mid-array parsed as nothing and
    silently dropped every op before the cut, on EVERY affected turn."""
    ops = _salvage_array(s, "ops")
    ents = _salvage_array(s, "entities")
    if not ops and not ents:
        return None
    print(f"[inference] SALVAGED {len(ops)} ops + {len(ents)} entities from a "
          f"truncated live-curator response ({len(s):,} chars)", flush=True)
    return {"ops": ops, "entities": ents, "_truncated": True}


def salvage_truncated(s: str) -> dict | None:
    """Recover the complete records from a response that was CUT OFF mid-JSON.

    MEASURED 2026-07-26 and the cause of every "blank" curation run: the model
    stops at exactly 65,536 output tokens. On a long session it is still writing
    records when it hits that ceiling, so the JSON ends mid-object and a strict
    parse throws away the whole response — 65k tokens of good work reported as
    "found nothing to curate", with no error.

    A truncated array still contains N complete objects. Scan for balanced
    braces, keep every object that closed, discard the partial tail. Losing the
    last record beats losing all of them, and it beats a silent empty result by
    a mile.
    """
    i = s.find('"records"')
    if i == -1:
        return None
    start = s.find("[", i)
    if start == -1:
        return None

    objs, buf, depth = [], [], 0
    in_str = esc = False
    for ch in s[start + 1:]:
        if depth:
            buf.append(ch)
        if in_str:                      # brace counting must ignore braces that
            if esc:                     # appear INSIDE strings, and content is
                esc = False             # full of them
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if not depth:
                buf = ["{"]
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    objs.append(json.loads("".join(buf)))
                except ValueError:
                    pass
                buf = []
        elif ch == "]" and not depth:
            break                       # array closed cleanly
    if not objs:
        return None
    print(f"[inference] SALVAGED {len(objs)} complete records from a truncated "
          f"response ({len(s):,} chars)", flush=True)
    return {"records": objs, "delete": [], "state_update": "",
            "_truncated": True}
