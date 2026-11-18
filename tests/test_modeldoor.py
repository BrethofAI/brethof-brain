"""The model door (2026-11-18): an app on its own plan points its model's
address at the hub; the memory goes into each new user message, stays on it
through the tool loop, the turn is archived once, and the app's own sign-in
goes through to the model service untouched."""
import asyncio
import base64
import os
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


@pytest.fixture
def door(tmp_path, monkeypatch):
    sk = Ed25519PrivateKey.generate()
    monkeypatch.setenv("GATE_TEST_PUBKEY", b64(sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)))
    from hub import front, models, store, modeldoor
    import gate
    if gate._pubkey_b64() != os.environ["GATE_TEST_PUBKEY"]:
        # the published gate is compiled with Brethof's licence key, and a test
        # cannot sign a licence for it (that is the point of the gate)
        pytest.skip("compiled gate holds the production licence key")
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setattr(store, "_conn", None)
    store.apply_schema(open("hub/schema.sql", encoding="utf-8").read())
    gate.init(str(tmp_path / "gate.sqlite"))
    asyncio.run(models.load())
    asyncio.run(models.set_many({"_admin_hash": hashlib.sha256(b"pw").hexdigest(),
                                 "upstream:test": "http://up"}))
    front._pair_bucket.clear()
    memory, model = [], []
    world = FastAPI()

    @world.api_route("/{path:path}", methods=["GET", "POST"])
    async def any_(path: str, request: Request):
        body = json.loads(await request.body() or b"{}")
        if request.url.hostname == "main":                       # the memory
            memory.append((path, body))
            if path == "v1/hooks/session-start":
                return {"injection": "BRIEF-1"}
            if path == "v1/hooks/prompt-submit":
                return {"injection": f"RECALL {body['part']} for {body['prompt']}" if body["part"] in (1, 7) else ""}
            return {"status": "ok"}
        model.append({"path": path, "auth": request.headers.get("authorization"), "body": body})
        text = json.dumps(body)
        tool = "use a tool" in text and "tool_result" not in text and '"role": "tool"' not in text \
            and "function_call_output" not in text
        if path.endswith("chat/completions"):
            if body.get("stream"):
                async def sse():
                    for t in ("The answer ", "is 42."):
                        yield b"data: " + json.dumps({"choices": [{"delta": {"content": t}}]}).encode() + b"\n\n"
                    yield b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n"
                    yield b"data: [DONE]\n\n"
                return StreamingResponse(sse(), media_type="text/event-stream")
            if tool:
                return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "t"}]},
                                     "finish_reason": "tool_calls"}]}
            return {"choices": [{"message": {"role": "assistant", "content": "Done: SQLite."}, "finish_reason": "stop"}]}
        if path.endswith("messages") and request.headers.get("x-test-gzip"):
            import gzip
            done = {"type": "message", "content": [{"type": "text", "text": "Zipped SQLite."}],
                    "stop_reason": "end_turn"}
            return Response(gzip.compress(json.dumps(done).encode()), media_type="application/json",
                            headers={"content-encoding": "gzip"})
        if path.endswith("messages"):
            if tool:
                return {"type": "message", "content": [{"type": "tool_use", "id": "x", "name": "read", "input": {}}],
                        "stop_reason": "tool_use"}
            async def sse():
                for e in ({"type": "message_start"}, {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": "Claude says "}},
                          {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "SQLite."}},
                          {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}):
                    yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
            return StreamingResponse(sse(), media_type="text/event-stream")
        if path.endswith("responses"):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Grok says SQLite."}]}]}
        return JSONResponse({"models": []})

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=world))
    front._client = client
    modeldoor.boot(client)
    gate.add_storage("main", "http://main")

    def lic():
        p = {"v": 1, "lic": "l", "account": "a", "hub": gate.hub_id(), "devices": 5, "plan": "free",
             "issued": int(time.time()), "expires": int(time.time()) + 86400}
        return {"payload": p, "sig": b64(sk.sign(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()))}
    gate.install_licence(lic())
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=front.front), base_url="http://hub")

    async def connect(service="test"):
        r = await c.post("/admin/api/app/add", json={"name": "grok laptop", "service": service},
                         headers={"Authorization": "Bearer pw"})
        return r.json()
    yield {"c": c, "memory": memory, "model": model, "connect": connect, "gate": gate, "modeldoor": modeldoor}


def grok_input(folder, q, extra=()):
    return [{"type": "message", "role": "system", "content": "You are Grok."},
            {"type": "message", "role": "user", "content": f"<user_info>\nWorkspace Path: {folder}\n</user_info>"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"<user_query>\n{q}\n</user_query>"}]},
            *extra]


async def _settle():
    await asyncio.sleep(0.05)


def test_an_app_is_a_device_and_its_address_needs_its_key(door):
    async def go():
        got = await door["connect"]()
        assert got["ok"] and got["path"].startswith("/model/bd_") and got["path"].endswith("/test/v1")
        assert [d["name"] for d in door["gate"].devices()] == ["grok laptop"]
        r = await door["c"].post("/model/bd_wrong/test/v1/chat/completions", json={"messages": []})
        assert r.status_code == 401
        r = await door["c"].post(got["path"].replace("/test/", "/nope/") + "/chat/completions", json={})
        assert r.status_code == 404
    asyncio.run(go())


def test_responses_get_the_memory_the_sign_in_passes_and_the_turn_is_archived(door):
    async def go():
        p = (await door["connect"]())["path"]
        r = await door["c"].post(p + "/responses", headers={"Authorization": "Bearer GROK-SUB-TOKEN"},
                                 json={"model": "grok-4.7", "store": False, "input": grok_input("/home/x/shop", "which db?")})
        assert r.status_code == 200 and "Grok says SQLite." in r.text
        sent = door["model"][-1]
        assert sent["auth"] == "Bearer GROK-SUB-TOKEN" and sent["path"] == "v1/responses"
        part = sent["body"]["input"][2]["content"][0]
        assert part["type"] == "input_text" and "BRIEF-1" in part["text"] and "RECALL 7 for which db?" in part["text"]
        await _settle()
        stops = [b for pth, b in door["memory"] if pth == "v1/hooks/stop"]
        assert len(stops) == 1 and [t["text"] for t in stops[0]["turns"]] == ["which db?", "Grok says SQLite."]
        assert stops[0]["project"] == "shop"
    asyncio.run(go())


def test_a_tool_loop_keeps_the_same_block_and_archives_once(door):
    async def go():
        p = (await door["connect"]())["path"]
        msgs = [{"role": "system", "content": "Agent.\nWorking directory: /srv/proj/kestrel"},
                {"role": "user", "content": "which database? use a tool"}]
        await door["c"].post(p + "/chat/completions", json={"messages": msgs})
        first = door["model"][-1]["body"]["messages"][1]["content"]
        assert "RECALL 1 for which database? use a tool" in first
        assert not [x for x, _ in door["memory"] if x == "v1/hooks/stop"]
        loop = msgs + [{"role": "assistant", "tool_calls": [{"id": "t"}]}, {"role": "tool", "content": "db.sqlite"}]
        await door["c"].post(p + "/chat/completions", json={"messages": loop})
        assert door["model"][-1]["body"]["messages"][1]["content"] == first
        assert len([x for x, _ in door["memory"] if x == "v1/hooks/session-start"]) == 1
        await _settle()
        stops = [b for x, b in door["memory"] if x == "v1/hooks/stop"]
        assert len(stops) == 1 and stops[0]["turns"][-1]["text"] == "Done: SQLite." and stops[0]["project"] == "kestrel"
    asyncio.run(go())


def test_claude_code_messages_stream_reminders_are_not_the_persons_words(door):
    async def go():
        p = (await door["connect"]("test"))["path"]
        body = {"model": "claude", "stream": True,
                "system": [{"type": "text", "text": "You are Claude Code.\n - Primary working directory: /mnt/AI/Programming/shop"}],
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "<system-reminder>\nskills are available\n</system-reminder>"},
                    {"type": "text", "text": "what did we pick?"}]}]}
        r = await door["c"].post(p + "/messages", json=body, headers={"Authorization": "Bearer sk-ant-oat-PLAN"})
        assert "Claude says " in r.text
        recall = [b for x, b in door["memory"] if x == "v1/hooks/prompt-submit"]
        assert recall and {b["prompt"] for b in recall} == {"what did we pick?"}
        sent = door["model"][-1]
        assert sent["auth"] == "Bearer sk-ant-oat-PLAN"
        assert sent["body"]["messages"][0]["content"][0]["text"].startswith("<memory>")
        await _settle()
        stop = [b for x, b in door["memory"] if x == "v1/hooks/stop"]
        assert stop and [t["text"] for t in stop[0]["turns"]] == ["what did we pick?", "Claude says SQLite."]
    asyncio.run(go())


def test_a_compressed_answer_reaches_the_app_readable_and_is_archived(door):
    """2026-11-18: Anthropic gzips; the door dropped content-encoding but passed
    the raw gzip bytes on, and Claude Code read garbage ("JSON Parse error")."""
    async def go():
        p = (await door["connect"]("test"))["path"]
        body = {"model": "claude", "system": "Primary working directory: /mnt/AI/Programming/shop",
                "messages": [{"role": "user", "content": "which database?"}]}
        r = await door["c"].post(p + "/messages", json=body,
                                 headers={"Authorization": "Bearer sk-ant-oat-PLAN", "x-test-gzip": "1"})
        assert r.json()["content"][0]["text"] == "Zipped SQLite."
        await _settle()
        stop = [b for x, b in door["memory"] if x == "v1/hooks/stop"]
        assert stop and [t["text"] for t in stop[0]["turns"]] == ["which database?", "Zipped SQLite."]
    asyncio.run(go())


def test_claude_code_environment_after_the_question_still_gets_the_memory(door):
    """Claude Code 2.1.325 puts its environment in a system message AFTER the
    person's words; the door took that for a reply and fetched no memory."""
    async def go():
        p = (await door["connect"]("test"))["path"]
        body = {"model": "claude", "stream": True, "system": [{"type": "text", "text": "You are Claude Code."}],
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "<system-reminder>\nsome context\n</system-reminder>"},
                        {"type": "text", "text": "which database?"}]},
                    {"role": "system", "content": [{"type": "text", "text":
                        "# Environment\n - Primary working directory: /mnt/AI/Programming/shop"}]}]}
        r = await door["c"].post(p + "/messages", json=body, headers={"Authorization": "Bearer sk-ant-oat-PLAN"})
        assert "Claude says " in r.text
        assert [b["prompt"] for x, b in door["memory"] if x == "v1/hooks/prompt-submit"]
        assert door["model"][-1]["body"]["messages"][0]["content"][0]["text"].startswith("<memory>")
        await _settle()
        stop = [b for x, b in door["memory"] if x == "v1/hooks/stop"]
        assert stop and stop[0]["project"] == "shop" and stop[0]["turns"][0]["text"] == "which database?"
    asyncio.run(go())


def test_a_tool_result_turn_is_not_a_new_message(door):
    async def go():
        p = (await door["connect"]())["path"]
        first = {"system": "Primary working directory: /x/shop",
                 "messages": [{"role": "user", "content": "use a tool please"}]}
        await door["c"].post(p + "/messages", json=json.loads(json.dumps(first)))
        block = door["model"][-1]["body"]["messages"][0]["content"]
        loop = json.loads(json.dumps(first))
        loop["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "r", "input": {}}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}]
        await door["c"].post(p + "/messages", json=loop)
        recall = [b for x, b in door["memory"] if x == "v1/hooks/prompt-submit"]
        assert len(recall) == 7 and {b["prompt"] for b in recall} == {"use a tool please"}   # one recall
        sent = door["model"][-1]["body"]["messages"]
        assert sent[0]["content"] == block and sent[2]["content"][0]["type"] == "tool_result"   # block kept, result untouched
    asyncio.run(go())


def test_a_side_call_naming_no_folder_passes_untouched(door):
    async def go():
        p = (await door["connect"]())["path"]
        msgs = [{"role": "system", "content": "Write a short title."}, {"role": "user", "content": "which db?"}]
        await door["c"].post(p + "/chat/completions", json={"messages": msgs})
        assert door["model"][-1]["body"]["messages"] == msgs and door["memory"] == []
    asyncio.run(go())


def test_a_retried_message_reuses_its_block_and_archives_once(door):
    async def go():
        p = (await door["connect"]())["path"]
        body = {"input": grok_input("/h/shop", "retry me")}
        await door["c"].post(p + "/responses", json=json.loads(json.dumps(body)))
        await door["c"].post(p + "/responses", json=json.loads(json.dumps(body)))
        await _settle()
        assert len([x for x, b in door["memory"] if x == "v1/hooks/prompt-submit"]) == 7    # one recall
        assert len([x for x, b in door["memory"] if x == "v1/hooks/stop"]) == 1
    asyncio.run(go())


def test_a_removed_app_is_refused_at_once(door):
    async def go():
        p = (await door["connect"]())["path"]
        did = door["gate"].devices()[0]["id"]
        await door["c"].post("/admin/api/device/remove", json={"id": did}, headers={"Authorization": "Bearer pw"})
        r = await door["c"].post(p + "/chat/completions", json={"messages": []})
        assert r.status_code == 401
        assert door["gate"].admit_device(did) is None
    asyncio.run(go())


def test_project_names_from_folders():
    from hub import modeldoor
    assert modeldoor.project_of("/mnt/AI/Programming/brethof-brain") == "brethof_brain"
    assert modeldoor.project_of("/home/x/2026-notes") == "p2026_notes"
    assert modeldoor.folder_of([{"role": "system", "content": " - Primary working directory: /a/b/shop"}]) == "/a/b/shop"
