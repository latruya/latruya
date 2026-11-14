"""Runs one chat turn against a saved conversation; used by both the CLI and the web app."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .agent import Assistant, friendly_error
from .store import Conversation, ConversationStore


def _title_from(text: str) -> str:
    title = " ".join(text.split())
    return (title[:57] + "…") if len(title) > 60 else (title or "New chat")


def run_turn(
    assistant: Assistant,
    store: ConversationStore,
    conv: Conversation,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
    attachment_names: list[str] | None = None,
    effort: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Add the user's message, stream Claude's reply, and save the conversation.

    Yields the same events as `Assistant.respond`, plus {"type": "error"} on failure.
    On failure the conversation is left exactly as it was before the turn.
    """
    content: list[dict[str, Any]] = list(attachments or [])
    if text:
        content.append({"type": "text", "text": text})
    if not content:
        yield {"type": "error", "text": "Type a message or attach a file."}
        return

    before_messages = len(conv.messages)
    before_transcript = len(conv.transcript)
    conv.messages.append({"role": "user", "content": content})
    conv.transcript.append({"role": "user", "text": text, "files": attachment_names or []})

    reply: dict[str, Any] = {"role": "assistant", "text": "", "thinking": "", "sources": [], "notices": []}
    try:
        for event in assistant.respond(conv.messages, effort=effort):
            kind = event["type"]
            if kind == "text":
                reply["text"] += event["text"]
            elif kind == "thinking":
                reply["thinking"] += event["text"]
            elif kind == "sources":
                reply["sources"].extend(event["items"])
            elif kind == "notice":
                reply["notices"].append(event["text"])
            elif kind == "done":
                conv.add_usage(event["usage"])
            yield event
    except Exception as exc:  # roll back so the user can simply retry
        del conv.messages[before_messages:]
        del conv.transcript[before_transcript:]
        yield {"type": "error", "text": friendly_error(exc)}
        return
    except BaseException:  # e.g. Ctrl+C or a client disconnect mid-stream
        del conv.messages[before_messages:]
        del conv.transcript[before_transcript:]
        raise

    conv.transcript.append(reply)
    if conv.title == "New chat":
        conv.title = _title_from(text or ", ".join(attachment_names or []))
    store.save(conv)
