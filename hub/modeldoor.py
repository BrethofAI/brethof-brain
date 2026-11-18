"""THE MODEL DOOR — memory for any app that lets you set its model's address,
on the app's own subscription sign-in (founder 2026-11-18: "the application
would have to work with the app login through oauth… There is no other way").

An app is connected on the settings page (it becomes a device, like a paired
computer) and is given ONE address to use as its model's base URL:

    http://<hub>/model/<device key>/<service>/v1

<service> names the real model service the call goes on to (UPSTREAMS below,
or one the owner adds). Every byte goes through to it — the app's own
Authorization header included, untouched; the hub never reads or keeps it —
except that on a chat call the door

  - adds the memory to each NEW user message before the model reads it: the
    brief on a conversation's first message, the recall for that message
    (parts 1-7, the history of decisions included) on every one. Agents call
    the model many times per message (after every tool run); the same block
    is re-attached on every later call, so the prompt stays stable;
  - archives the turn — the person's own words and the model's final answer —
    once the model answers without asking for a tool;
  - hands the memory a conversation's waiting turns when it goes quiet.

Formats: OpenAI chat/completions, OpenAI Responses (Grok's subscription
route), Anthropic Messages (Claude Code). The project comes from the working
folder the agent names in its prompt ("Workspace Path:", "Primary working
directory:", "cwd:"); a call that names none (a chat's title, a summary)
passes through untouched. Ported from the client's modelproxy.py (proven on
Grok's subscription route, 2026-11-18), which needed a process run by hand.

FAIL-OPEN: the memory slow or down, a body we cannot read — the call goes
through untouched. The model's answer never waits for the archive.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import gate
from hub import models, store

router = APIRouter()

# The services an app's calls go on to. The owner adds others on the
# settings page (settings key "upstream:<name>"). Base URLs WITHOUT /v1: the
# app's own path (/v1/responses, /v1/messages, ...) is kept as it is.
UPSTREAMS = {
    "grok": "https://cli-chat-proxy.grok.com",     # Grok Build / Grok App on a Grok subscription
    "xai": "https://api.x.ai",
    "anthropic": "https://api.anthropic.com",      # Claude Code, on a plan or a key
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai/api",
    "ollama-cloud": "https://ollama.com",
}
HEADER = ("Context from the user's long-term memory (Brethof Brain), added before this "
          "message. Use what bears on the request; ignore the rest. Not part of the user's words.")
MARK = "<memory>\n"
PARTS = tuple(range(1, 8))
MEMORY_TIMEOUT_S = float(os.environ.get("HUB_MODELDOOR_MEMORY_TIMEOUT_S", "9"))
UPSTREAM_TIMEOUT_S = float(os.environ.get("HUB_MODELDOOR_UPSTREAM_TIMEOUT_S", "900"))
QUIET_S = float(os.environ.get("HUB_MODELDOOR_QUIET_S", "1800"))      # a conversation this quiet is handed over
KEEP_S = float(os.environ.get("HUB_MODELDOOR_KEEP_S", str(7 * 86400)))
HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "accept-encoding",
       "cookie", "upgrade", "te", "trailers", "proxy-authorization", "proxy-authenticate"}
HOP_OUT = HOP | {"content-encoding", "set-cookie"}
_client: httpx.AsyncClient | None = None


def log(msg: str) -> None:
    print(f"[modeldoor] {msg}", flush=True)


DDL = """
CREATE TABLE IF NOT EXISTS modeldoor_conv (
  conv TEXT PRIMARY KEY, device TEXT, project TEXT, next_index INTEGER NOT NULL DEFAULT 0,
  briefed INTEGER NOT NULL DEFAULT 0, handed INTEGER NOT NULL DEFAULT 0, last_seen REAL);
CREATE TABLE IF NOT EXISTS modeldoor_block (
  k TEXT PRIMARY KEY, conv TEXT, block TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, at REAL);
"""


def ensure() -> None:
    c = store.connect()
    c.executescript(DDL)


# ── reading the request ──────────────────────────────────────────────────
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def _with_block(content, block: str):
    if isinstance(content, str):
        return block + "\n\n" + content
    if isinstance(content, list):
        kind = next((p.get("type") for p in content if isinstance(p, dict)
                     and p.get("type") in ("text", "input_text")), "text")
        return [{"type": kind, "text": block}] + content
    return content


_LABELLED = re.compile(r"(?im)^[\s\-*]*(?:primary\s+)?(?:workspace path|working directory|current working directory|"
                       r"cwd|workspace|project (?:root|path|directory))\s*[:=]\s*(/[^\s'\"`<>|;,]+)")
_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_PROJECT = re.compile(r"[^a-z0-9_]+")


def said_of(text: str) -> str:
    """The person's own words in a user message: inside Grok's <user_query>
    wrapper when there is one, else the message without the app's reminders."""
    m = _QUERY.search(text or "")
    if m:
        return m.group(1)
    return _REMINDER.sub("", text or "").strip()


def folder_of(messages: list) -> str:
    """The working folder the agent names (labelled lines only: the hub runs
    in a container and cannot look at the app's disk)."""
    for m in messages:
        for p in _LABELLED.findall(_text(m.get("content"))):
            p = p.rstrip(".)]}/")
            if len(p) > 1 and p not in ("/tmp",):
                return p
    return ""


def project_of(folder: str) -> str:
    """The project a folder files under: its last part, as a project key."""
    name = _PROJECT.sub("_", folder.rstrip("/").rsplit("/", 1)[-1].lower()).strip("_")
    if not name or not name[0].isalpha():
        name = "p" + name
    return name[:16].rstrip("_") or "global"


def conversation(device: str, messages: list) -> str:
    system = "".join(_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))[:4000]
    users = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    first = next((u for u in users if _QUERY.search(u)), users[0] if users else "")
    return "md-" + hashlib.sha256((device + "\x00" + system + "\x00" + first)
                                  .encode("utf-8", "replace")).hexdigest()[:24]


def _key(conv: str, i: int, text: str) -> str:
    return conv + ":" + str(i) + ":" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# ── the memory side: storage's own hook doors, through the gate's route ──
class Memory:
    def __init__(self, a: dict):
        self.a = a

    async def _post(self, path: str, body: dict, timeout: float = MEMORY_TIMEOUT_S) -> dict:
        assert _client is not None
        try:
            r = await _client.post(self.a["url"] + path, json=body, timeout=timeout,
                                   headers={"authorization": "Bearer " + self.a["storage_key"],
                                            "x-brain-device": self.a["device"]})
            if r.status_code != 200:
                log(f"memory call {path} answered {r.status_code}")
                return {}
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log(f"memory call {path} failed: {type(e).__name__}")
            return {}

    async def block(self, conv: str, project: str, said: str) -> str:
        row = await store.fetchone("SELECT briefed FROM modeldoor_conv WHERE conv = ?", (conv,))
        parts = []
        calls = [self._post("/v1/hooks/prompt-submit", {"project": project, "prompt": said,
                                                         "session_id": conv, "part": p}) for p in PARTS]
        if not (row and row[0]):
            calls.insert(0, self._post("/v1/hooks/session-start", {"project": project, "session_id": conv}))
            await store.execute("UPDATE modeldoor_conv SET briefed = 1 WHERE conv = ?", (conv,))
        got = await asyncio.gather(*calls)
        for g in got:
            t = str((g or {}).get("injection") or "").strip()
            if t:
                parts.append(t)
        return (MARK + HEADER + "\n\n" + "\n\n".join(parts) + "\n</memory>") if parts else ""

    async def archive(self, conv: str, project: str, said: str, answer: str) -> None:
        row = await store.fetchone("SELECT next_index FROM modeldoor_conv WHERE conv = ?", (conv,))
        i = int(row[0]) if row else 0
        turns = []
        if said.strip():
            turns.append({"index": i, "line_type": "user", "text": said, "embed": True}); i += 1
        if answer.strip():
            turns.append({"index": i, "line_type": "assistant", "text": answer, "embed": True}); i += 1
        if not turns:
            return
        # the turn numbers are reserved FIRST: a hub that dies mid-call must
        # not have the next turn reuse them (the archive upserts on them)
        await store.execute("UPDATE modeldoor_conv SET next_index = ? WHERE conv = ?", (i, conv))
        res = await self._post("/v1/hooks/stop", {"project": project, "session_id": conv, "turns": turns}, 30)
        if res.get("status") not in ("ok", None) or not res:
            log(f"archive of {conv} answered {str(res)[:120] or 'nothing'}")


async def _conv_row(conv: str, device: str, project: str) -> None:
    await store.execute(
        "INSERT INTO modeldoor_conv (conv, device, project, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT (conv) DO UPDATE SET last_seen = excluded.last_seen, handed = 0",
        (conv, device, project, time.time()))


async def prepare(mem: Memory, device: str, msgs: list) -> tuple | None:
    """Attach the memory to the user messages of a chat (msgs, edited in
    place). Returns what to archive once the answer is final:
    (conv, project, said, key) — or None."""
    if not isinstance(msgs, list) or not msgs:
        return None
    folder = folder_of(msgs)
    if not folder:
        return None                       # NO FOLDER, NO MEMORY: an app's side call
    project = project_of(folder)
    conv = conversation(device, msgs)
    await _conv_row(conv, device, project)
    users = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"]
    marked = [i for i in users if _QUERY.search(_text(msgs[i].get("content")))]
    if marked:
        users = marked
    users = [i for i in users if said_of(_text(msgs[i].get("content"))).strip()]
    if not users:
        return None
    last = users[-1]
    # only a reply (assistant, tool) after the person's words makes this an old
    # turn; Claude Code 2.1.325 sends its environment as a system message AFTER
    # the question (2026-11-18: read as a tool loop, so no memory was fetched)
    new_turn = all(isinstance(m, dict) and m.get("role") in ("user", "system", "developer")
                   for m in msgs[last + 1:])
    pending = None
    for i in users:
        said = said_of(_text(msgs[i].get("content")))
        if said.startswith(MARK):
            continue
        k = _key(conv, i, said)
        row = await store.fetchone("SELECT block FROM modeldoor_block WHERE k = ?", (k,))
        block = row[0] if row else None
        if i == last and new_turn:
            if block is None:
                try:
                    block = await asyncio.wait_for(mem.block(conv, project, said), timeout=MEMORY_TIMEOUT_S + 2)
                except asyncio.TimeoutError:
                    block = ""
                    log(f"memory for {conv} ran out of time — the message goes without it")
                await store.execute("INSERT OR REPLACE INTO modeldoor_block (k, conv, block, at) VALUES (?,?,?,?)",
                                    (k, conv, block, time.time()))
            pending = (conv, project, said, k)          # a retry of the same message still archives
        if block:
            msgs[i] = dict(msgs[i], content=_with_block(msgs[i].get("content"), block))
    if pending is None and not new_turn:
        said = said_of(_text(msgs[last].get("content")))
        pending = (conv, project, said, _key(conv, last, said))   # a tool loop: archived when it ends
    return pending


def view_responses(body: dict) -> tuple[list, int]:
    """Responses' `input` as chat messages (a tool item is a non-user turn)."""
    items = body.get("input")
    view = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("type", "message") == "message":
            view.append({"role": it.get("role"), "content": it.get("content")})
        else:
            view.append({"role": "tool", "content": ""})
    shift = 0
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        view.insert(0, {"role": "system", "content": body["instructions"]})
        shift = 1
    return view, shift


def view_anthropic(body: dict) -> list:
    """Anthropic Messages as chat messages: the system prompt first; a user
    message that carries tool results is a tool turn, not the person."""
    view = []
    sysp = body.get("system")
    if sysp:
        view.append({"role": "system", "content": sysp if isinstance(sysp, str) else _text(sysp)})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            view.append({"role": "tool", "content": ""})
            continue
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, list) and any(
                isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
            view.append({"role": "tool", "content": ""})
        else:
            view.append({"role": m.get("role"), "content": c})
    return view


async def prepare_body(mem: Memory, device: str, path: str, body: dict) -> tuple | None:
    if path.endswith("/chat/completions"):
        return await prepare(mem, device, body.get("messages"))
    if path.endswith("/responses"):
        view, shift = view_responses(body)
        before = [v.get("content") for v in view]
        pending = await prepare(mem, device, view)
        for i, it in enumerate(body.get("input") or []):
            v = view[i + shift]
            if isinstance(it, dict) and it.get("type", "message") == "message" and v.get("content") is not before[i + shift]:
                it["content"] = v["content"]
        return pending
    if path.endswith("/messages"):
        view = view_anthropic(body)
        shift = 1 if body.get("system") else 0
        before = [v.get("content") for v in view]
        pending = await prepare(mem, device, view)
        for i, m in enumerate(body.get("messages") or []):
            v = view[i + shift]
            if isinstance(m, dict) and v.get("content") is not before[i + shift]:
                m["content"] = v["content"]
        return pending
    return None


# ── reading the answer ───────────────────────────────────────────────────
def read_piece(line: str, stream: bool, answer: list, final):
    """Collect the answer's text and whether the turn is over, from one line
    of the model's reply (an SSE line when streaming, else the whole body)."""
    s = line.strip()
    if stream:
        if not s.startswith("data:") or s == "data: [DONE]":
            return final
        s = s[5:].strip()
    if not s.startswith("{"):
        return final
    try:
        d = json.loads(s)
    except ValueError:
        return final
    t = d.get("type")
    # Anthropic Messages
    if t == "content_block_delta":
        dl = d.get("delta") or {}
        if dl.get("type") == "text_delta":
            answer.append(str(dl.get("text") or ""))
        return final
    if t == "message_delta":
        sr = (d.get("delta") or {}).get("stop_reason")
        return {"tool_use": "tool_calls", None: final}.get(sr, "stop") if sr else final
    if t == "message" or (d.get("stop_reason") is not None and "content" in d):
        if not stream:
            for c in d.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    answer.append(str(c.get("text") or ""))
        sr = d.get("stop_reason")
        return "tool_calls" if sr == "tool_use" else ("stop" if sr else final)
    # OpenAI Responses
    if t == "response.output_text.delta" and isinstance(d.get("delta"), str):
        answer.append(d["delta"])
        return final
    r = d.get("response") if t in ("response.completed", "response.done") else (d if "output" in d else None)
    if isinstance(r, dict):
        out = r.get("output") or []
        if not stream and not answer:
            for it in out:
                for c in (it.get("content") or []) if isinstance(it, dict) else []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        answer.append(str(c.get("text") or ""))
        calls = any(isinstance(it, dict) and it.get("type") in ("function_call", "custom_tool_call",
                                                                "local_shell_call") for it in out)
        return "tool_calls" if calls else "stop"
    # OpenAI chat
    for c in d.get("choices") or []:
        part = c.get("delta") if stream else c.get("message")
        if isinstance(part, dict) and isinstance(part.get("content"), str):
            answer.append(part["content"])
        if c.get("finish_reason"):
            final = c["finish_reason"]
    return final


async def _archive_once(mem: Memory, pending: tuple, answer: str) -> None:
    conv, project, said, k = pending
    n = await store.execute("UPDATE modeldoor_block SET archived = 1 WHERE k = ? AND archived = 0", (k,))
    if not n:
        row = await store.fetchone("SELECT archived FROM modeldoor_block WHERE k = ?", (k,))
        if row:
            return                        # already archived
        await store.execute("INSERT OR IGNORE INTO modeldoor_block (k, conv, block, archived, at) VALUES (?,?,?,1,?)",
                            (k, conv, "", time.time()))
    await mem.archive(conv, project, said, answer)


# ── the door ─────────────────────────────────────────────────────────────
def upstream_of(name: str) -> str:
    own = models.get("upstream:" + name)
    return (own or UPSTREAMS.get(name, "")).rstrip("/")


@router.api_route("/model/{key}/{service}/{path:path}", methods=["GET", "POST"])
async def door(key: str, service: str, path: str, request: Request) -> Response:
    a = gate.admit(key, "")
    if a is None:
        return JSONResponse({"error": {"message": "this app is not connected to this hub — connect it on the "
                                                  "hub's settings page"}}, status_code=401)
    base = upstream_of(service)
    if not base:
        return JSONResponse({"error": {"message": f"no model service called '{service}' on this hub"}}, status_code=404)
    raw = await request.body()
    pending, stream = None, False
    mem = Memory(a)
    if request.method == "POST" and raw and path.rstrip("/").endswith(("/chat/completions", "/responses", "/messages")):
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                stream = bool(body.get("stream"))
                pending = await prepare_body(mem, a["device"], "/" + path.rstrip("/"), body)
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except Exception as e:                                       # noqa: BLE001 — fail open
            log(f"a chat body passed through unread ({type(e).__name__}: {str(e)[:120]})")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    url = base + "/" + path + (("?" + request.url.query) if request.url.query else "")
    assert _client is not None
    try:
        r = await _client.send(_client.build_request(request.method, url, headers=headers, content=raw,
                                                     timeout=UPSTREAM_TIMEOUT_S), stream=True)
    except httpx.HTTPError as e:
        return JSONResponse({"error": {"message": f"the model service did not answer: {type(e).__name__}"}},
                            status_code=502)
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_OUT}

    async def relay():
        answer: list[str] = []
        final = None
        buf = ""
        try:
            # aiter_bytes, not aiter_raw: content-encoding is dropped on the way
            # out, so what we pass on must be decoded (2026-11-18: Anthropic
            # gzips its answers; raw bytes reached Claude Code unreadable)
            async for chunk in r.aiter_bytes():
                yield chunk
                if pending:
                    buf += chunk.decode("utf-8", "replace")
                    if stream:
                        *lines, buf = buf.split("\n")
                        for line in lines:
                            final = read_piece(line, True, answer, final)
            if pending and not stream:
                final = read_piece(buf, False, answer, final)
            elif pending and buf:
                final = read_piece(buf, True, answer, final)
        finally:
            await r.aclose()
            if pending and final == "stop" and r.status_code < 400:
                text = "".join(answer).strip()
                if text:
                    asyncio.get_running_loop().create_task(_archive_once(mem, pending, text))
    return StreamingResponse(relay(), status_code=r.status_code, headers=out_headers)


# ── handing quiet conversations over, and keeping the tables small ───────
async def sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            rows = await store.fetchall(
                "SELECT conv, device, project, next_index FROM modeldoor_conv "
                "WHERE handed = 0 AND next_index > 0 AND last_seen < ?", (now - QUIET_S,))
            for conv, device, project, idx in rows:
                a = gate.admit_device(device)
                if a is not None:
                    await Memory(a)._post("/v1/hooks/pre-compact", {"project": project, "session_id": conv,
                                                                    "last_index": int(idx) - 1}, 30)
                await store.execute("UPDATE modeldoor_conv SET handed = 1 WHERE conv = ?", (conv,))
            await store.execute("DELETE FROM modeldoor_block WHERE at < ?", (now - KEEP_S,))
            await store.execute("DELETE FROM modeldoor_conv WHERE last_seen < ?", (now - KEEP_S,))
        except Exception as e:                                       # noqa: BLE001 — logged, never fatal
            log(f"sweep failed: {type(e).__name__}: {str(e)[:160]}")


def boot(client: httpx.AsyncClient) -> None:
    global _client
    _client = client
    ensure()
