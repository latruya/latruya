"""The assistant itself: one streaming agent loop shared by the CLI and the web app.

`Assistant.respond()` sends the conversation to Claude, runs any tools Claude
asks for (memory runs here; web search and web fetch run on Anthropic's
servers), and yields small event dicts as the answer streams in:

    {"type": "thinking", "text": ...}   reasoning summary, as it streams
    {"type": "text", "text": ...}       the answer, as it streams
    {"type": "status", "text": ...}     "Searching the web for ...", "Saving to memory", ...
    {"type": "sources", "items": [...]} web pages Claude looked at
    {"type": "notice", "text": ...}     something the user should know (refusal, cut-off)
    {"type": "done", "usage": {...}}    the turn is finished
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from typing import Any

import anthropic

from .config import Settings
from .memory import MemoryError_, MemoryStore

FALLBACK_BETA = "server-side-fallback-2026-07-01"
COMPACTION_BETA = "compact-2026-01-12"
MAX_TOOL_ROUNDS = 40

SYSTEM_PROMPT = """\
You are Latruya AI, a personal assistant. You are warm, direct and genuinely useful.

How you work:
- Answer the question that was asked. Lead with the answer, then add the detail that helps.
  Match length to the request: a quick question gets a short reply; a hard problem gets a
  thorough one. Use Markdown (headings, lists, tables, code blocks) when it makes a reply
  easier to read, and plain prose when it doesn't.
- When something depends on current facts (news, prices, schedules, releases, anything that
  may have changed since your training), search the web instead of guessing, and mention
  where the information came from. Fetch a page when a search snippet isn't enough.
- If you are unsure, say so plainly and say what would settle it. Never invent facts,
  quotes, links or numbers.
- If a request is ambiguous in a way that changes the answer, ask one short question;
  otherwise make a sensible assumption, state it, and proceed.
- When the user shares images, PDFs or files, read them carefully and refer to specifics.

Memory:
- You have a long-term memory folder at /memories that persists across conversations.
- At the start of a conversation, look in /memories for what you already know about the
  user and use it naturally, without reciting it back.
- When the user tells you something worth remembering long-term (their name, preferences,
  ongoing projects, important dates, how they like answers written), save it to a
  well-organised file in /memories. Keep memories short, factual and up to date: edit or
  delete entries that become wrong.
- Never store passwords, financial account numbers, government ID numbers or similar
  secrets, even if asked. If the user asks you to forget something, delete it.
"""


def _dump_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK content blocks to plain dicts, unchanged, for replay and saving."""
    return [block.model_dump(mode="json", exclude_none=True) for block in content]


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {f: getattr(usage, f) for f in fields if isinstance(getattr(usage, f, None), int)}


def _describe_server_tool(block: Any) -> str | None:
    data = block.input if isinstance(block.input, dict) else {}
    if block.name == "web_search":
        query = data.get("query")
        return f"Searching the web for “{query}”" if query else "Searching the web"
    if block.name == "web_fetch":
        url = data.get("url")
        return f"Reading {url}" if url else "Reading a web page"
    return None


def _describe_memory(cmd: dict[str, Any]) -> str:
    action = cmd.get("command")
    path = cmd.get("path") or cmd.get("old_path") or "/memories"
    return {
        "view": f"Checking memory ({path})",
        "create": f"Saving to memory ({path})",
        "str_replace": f"Updating memory ({path})",
        "insert": f"Updating memory ({path})",
        "delete": f"Forgetting ({path})",
        "rename": f"Reorganising memory ({path})",
    }.get(action, "Using memory")


class Assistant:
    def __init__(self, settings: Settings | None = None, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings or Settings()
        self.client = client or anthropic.Anthropic()
        self.memory = MemoryStore(self.settings.memory_dir) if self.settings.memory else None

    # ---- request building ---------------------------------------------
    def _system(self) -> list[dict[str, Any]]:
        today = _dt.date.today().strftime("%A, %d %B %Y")
        extra = f"Today's date is {today}."
        if self.settings.user_name:
            extra += f" The user's name is {self.settings.user_name}."
        return [{"type": "text", "text": SYSTEM_PROMPT}, {"type": "text", "text": extra}]

    def _tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.settings.web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 8})
            tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8})
        if self.memory is not None:
            tools.append({"type": "memory_20250818", "name": "memory"})
        return tools

    def request_params(self, messages: list[dict[str, Any]], effort: str | None = None) -> dict[str, Any]:
        betas: list[str] = []
        params: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": self._system(),
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort or self.settings.effort},
            # Caches everything up to the latest message, so long chats stay fast and cheap.
            "cache_control": {"type": "ephemeral"},
        }
        tools = self._tools()
        if tools:
            params["tools"] = tools
        if self.settings.refusal_fallback:
            # If a safety classifier declines, the API retries on Anthropic's recommended model.
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if self.settings.compaction:
            # Summarises old context server-side when a conversation gets very long.
            betas.append(COMPACTION_BETA)
            params["context_management"] = {"edits": [{"type": "compact_20260112"}]}
        if betas:
            params["betas"] = betas
        return params

    # ---- the agent loop -----------------------------------------------
    def respond(self, messages: list[dict[str, Any]], effort: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream one assistant turn. `messages` must end with the user's message.

        `messages` is extended in place with everything Claude produced, so the
        caller can save it. If an exception escapes, the caller should roll
        `messages` back to its length before the call.
        """
        total_usage: dict[str, int] = {}
        seen_sources: set[str] = set()

        for _ in range(MAX_TOOL_ROUNDS):
            with self.client.beta.messages.stream(**self.request_params(messages, effort)) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "thinking_delta" and delta.thinking:
                            yield {"type": "thinking", "text": delta.thinking}
                        elif delta.type == "text_delta" and delta.text:
                            yield {"type": "text", "text": delta.text}
                    elif event.type == "content_block_stop":
                        block = event.content_block
                        if block.type == "server_tool_use":
                            label = _describe_server_tool(block)
                            if label:
                                yield {"type": "status", "text": label}
                        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                            items = []
                            for result in block.content:
                                url = getattr(result, "url", None)
                                if url and url not in seen_sources:
                                    seen_sources.add(url)
                                    items.append({"url": url, "title": getattr(result, "title", url)})
                            if items:
                                yield {"type": "sources", "items": items}
                        elif block.type == "compaction":
                            yield {"type": "status", "text": "Summarising earlier parts of this long chat"}
                response = stream.get_final_message()

            for key, value in _usage_dict(response.usage).items():
                total_usage[key] = total_usage.get(key, 0) + value
            messages.append({"role": "assistant", "content": _dump_blocks(response.content)})

            if response.stop_reason == "pause_turn":
                continue  # a long server-side search paused; send it back to resume

            if response.stop_reason == "refusal":
                yield {"type": "notice", "text": "Claude declined to continue with this request."}
                break

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    yield {"type": "notice", "text": "The reply hit the length limit and was cut off."}
                break

            results = []
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    # A truncated tool call parses as a partial object; never run it.
                    results.append(self._tool_error(block.id, "Tool call was cut off; nothing was done."))
                    continue
                if block.name == "memory" and self.memory is not None and isinstance(block.input, dict):
                    yield {"type": "status", "text": _describe_memory(block.input)}
                    try:
                        output = self.memory.execute(block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    except (MemoryError_, OSError, ValueError, TypeError) as exc:
                        results.append(self._tool_error(block.id, str(exc)))
                else:
                    results.append(self._tool_error(block.id, f"Unknown tool {block.name!r}."))
            messages.append({"role": "user", "content": results})
            if response.stop_reason == "max_tokens":
                yield {"type": "notice", "text": "The reply hit the length limit and was cut off."}
                break
        else:
            yield {"type": "notice", "text": "Stopped after too many tool steps."}

        yield {"type": "done", "usage": total_usage}

    @staticmethod
    def _tool_error(tool_use_id: str, message: str) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def friendly_error(exc: Exception) -> str:
    """A short explanation of an API failure, suitable to show the user."""
    if isinstance(exc, anthropic.AuthenticationError) or "resolve authentication" in str(exc):
        return "Your Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY and try again."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Your API key doesn't have access to this model or feature."
    if isinstance(exc, anthropic.NotFoundError):
        return "The model wasn't found. Check LATRUYA_MODEL."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limit reached. Wait a moment and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The request was rejected: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error ({exc.status_code}). Try again shortly."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your internet connection."
    return f"Unexpected error: {exc}"
