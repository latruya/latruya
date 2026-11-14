"""Saves conversations as JSON files so chats survive restarts."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "New chat"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Exactly what is sent to the API (thinking, tool and compaction blocks included).
    messages: list[dict[str, Any]] = field(default_factory=list)
    # A simplified copy for display: [{"role", "text", "thinking", "sources", "files"}].
    transcript: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "updated_at": self.updated_at}

    def add_usage(self, usage: dict[str, int]) -> None:
        for key, value in usage.items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value


class ConversationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, conv_id: str) -> Path:
        if not _ID_RE.match(conv_id):
            raise KeyError(conv_id)
        return self.root / f"{conv_id}.json"

    def create(self) -> Conversation:
        conv = Conversation()
        self.save(conv)
        return conv

    def get(self, conv_id: str) -> Conversation:
        path = self._path(conv_id)
        if not path.exists():
            raise KeyError(conv_id)
        return Conversation(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, conv: Conversation) -> None:
        conv.updated_at = time.time()
        path = self._path(conv.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(conv)), encoding="utf-8")
        tmp.replace(path)

    def delete(self, conv_id: str) -> None:
        self._path(conv_id).unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        items = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append({k: data.get(k) for k in ("id", "title", "updated_at")})
        return sorted(items, key=lambda c: c["updated_at"] or 0, reverse=True)
