"""Offline tests: a fake Anthropic client drives the real agent loop, memory and web app."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage

from latruya_ai.agent import Assistant
from latruya_ai.attachments import AttachmentError, to_content_block
from latruya_ai.chat import run_turn
from latruya_ai.config import Settings
from latruya_ai.memory import MemoryError_, MemoryStore
from latruya_ai.store import ConversationStore


# ---------------------------------------------------------------- fakes
def make_message(content: list[dict], stop_reason: str) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
        }
    )


class FakeStream:
    def __init__(self, message: BetaMessage) -> None:
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            if block.type == "thinking":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking=block.thinking))
            elif block.type == "text":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text))
            yield SimpleNamespace(type="content_block_stop", content_block=block)

    def get_final_message(self) -> BetaMessage:
        return self.message


class FakeClient:
    def __init__(self, replies: list[BetaMessage]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append({**params, "messages": list(params["messages"])})
        return FakeStream(self.replies.pop(0))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


# ---------------------------------------------------------------- agent loop
def test_request_uses_best_defaults(settings):
    params = Assistant(settings, client=FakeClient([])).request_params([{"role": "user", "content": "hi"}])
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "high"}
    assert params["fallbacks"] == "default"
    assert set(params["betas"]) == {"server-side-fallback-2026-07-01", "compact-2026-01-12"}
    assert {t["type"] for t in params["tools"]} == {"web_search_20260209", "web_fetch_20260209", "memory_20250818"}
    assert "temperature" not in params


def test_memory_tool_round_trip(settings):
    client = FakeClient(
        [
            make_message(
                [
                    {"type": "thinking", "thinking": "Save the name.", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "memory",
                        "input": {"command": "create", "path": "/memories/profile.md", "file_text": "Name: Ana\n"},
                    },
                ],
                "tool_use",
            ),
            make_message([{"type": "text", "text": "Nice to meet you, Ana!"}], "end_turn"),
        ]
    )
    assistant = Assistant(settings, client=client)
    store = ConversationStore(settings.conversations_dir)
    conv = store.create()

    events = list(run_turn(assistant, store, conv, "My name is Ana"))
    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    assert "status" in types and types[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Nice to meet you, Ana!"

    # The memory file was written inside the data folder.
    assert (settings.memory_dir / "profile.md").read_text() == "Name: Ana\n"

    # The second request replays the thinking block unchanged and returns the tool result.
    second = client.calls[1]["messages"]
    assert second[1]["content"][0] == {"type": "thinking", "thinking": "Save the name.", "signature": "sig"}
    assert second[2]["content"][0]["tool_use_id"] == "toolu_1"

    saved = store.get(conv.id)
    assert saved.title == "My name is Ana"
    assert saved.transcript[-1]["text"] == "Nice to meet you, Ana!"
    assert saved.usage["output_tokens"] == 10


def test_api_error_rolls_back(settings):
    class BrokenClient(FakeClient):
        def _stream(self, **params):
            raise RuntimeError("boom")

    assistant = Assistant(settings, client=BrokenClient([]))
    store = ConversationStore(settings.conversations_dir)
    conv = store.create()
    events = list(run_turn(assistant, store, conv, "hello"))
    assert events[-1]["type"] == "error"
    assert conv.messages == [] and conv.transcript == []


def test_truncated_tool_call_is_not_run(settings):
    client = FakeClient(
        [
            make_message(
                [{"type": "tool_use", "id": "t", "name": "memory", "input": {"command": "delete", "path": "/memories/x"}}],
                "max_tokens",
            )
        ]
    )
    (settings.memory_dir).mkdir(parents=True)
    (settings.memory_dir / "x").write_text("keep me")
    events = list(Assistant(settings, client=client).respond([{"role": "user", "content": "hi"}]))
    assert (settings.memory_dir / "x").exists()
    assert any(e["type"] == "notice" for e in events)


def test_pause_turn_resumes(settings):
    client = FakeClient(
        [make_message([{"type": "text", "text": "Searching… "}], "pause_turn"), make_message([{"type": "text", "text": "Done."}], "end_turn")]
    )
    messages = [{"role": "user", "content": "news?"}]
    list(Assistant(settings, client=client).respond(messages))
    assert len(client.calls) == 2
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]


# ---------------------------------------------------------------- memory safety
def test_memory_rejects_escapes(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    for bad in ["/etc/passwd", "/memories/../secret", "/memoriesX/a", "relative.txt"]:
        with pytest.raises(MemoryError_):
            store.execute({"command": "create", "path": bad, "file_text": "x"})
    assert not (tmp_path / "secret").exists()


def test_memory_commands(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    store.execute({"command": "create", "path": "/memories/a.md", "file_text": "one\nthree\n"})
    store.execute({"command": "insert", "path": "/memories/a.md", "insert_line": 1, "insert_text": "two"})
    store.execute({"command": "str_replace", "path": "/memories/a.md", "old_str": "three", "new_str": "3"})
    assert "     2\ttwo" in store.execute({"command": "view", "path": "/memories/a.md"})
    store.execute({"command": "rename", "old_path": "/memories/a.md", "new_path": "/memories/sub/b.md"})
    assert "/memories/sub/b.md" in store.execute({"command": "view", "path": "/memories"})
    assert store.list_files()[0]["content"] == "one\ntwo\n3\n"
    store.execute({"command": "delete", "path": "/memories/sub"})
    assert store.list_files() == []


# ---------------------------------------------------------------- attachments
def test_attachments():
    assert to_content_block("a.png", b"\x89PNG")["type"] == "image"
    assert to_content_block("a.pdf", b"%PDF")["source"]["media_type"] == "application/pdf"
    assert to_content_block("notes.txt", b"hello")["source"]["data"] == "hello"
    with pytest.raises(AttachmentError):
        to_content_block("x.bin", b"\xff\xfe\x00\x81")


# ---------------------------------------------------------------- web app
def test_web_app_streams_reply(settings):
    from fastapi.testclient import TestClient

    from latruya_ai.web import create_app

    client = FakeClient([make_message([{"type": "text", "text": "Hello from Claude"}], "end_turn")])
    app = create_app(settings, Assistant(settings, client=client))
    http = TestClient(app)

    assert http.get("/").status_code == 200
    conv_id = http.post("/api/conversations").json()["id"]
    res = http.post(
        f"/api/conversations/{conv_id}/messages",
        data={"text": "hi", "effort": "low"},
        files=[("files", ("note.txt", b"some notes", "text/plain"))],
    )
    assert res.status_code == 200 and "Hello from Claude" in res.text
    assert client.calls[0]["output_config"] == {"effort": "low"}
    assert client.calls[0]["messages"][0]["content"][0]["type"] == "document"

    conv = http.get(f"/api/conversations/{conv_id}").json()
    assert conv["transcript"][1]["text"] == "Hello from Claude"
    assert http.get("/api/conversations/not-an-id").status_code == 404
    assert http.post(f"/api/conversations/{conv_id}/messages", data={"text": "x", "effort": "huge"}).status_code == 400


def test_web_password(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from latruya_ai.web import create_app

    monkeypatch.setenv("LATRUYA_PASSWORD", "s3cret")
    http = TestClient(create_app(settings, Assistant(settings, client=FakeClient([]))))
    assert http.get("/api/config").status_code == 401
    assert http.get("/api/config", auth=("me", "s3cret")).status_code == 200
