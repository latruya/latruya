"""Settings, read from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    model: str = field(default_factory=lambda: os.environ.get("LATRUYA_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.environ.get("LATRUYA_EFFORT", "high"))
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("LATRUYA_MAX_TOKENS", "64000")))
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("LATRUYA_DATA_DIR", Path.home() / ".latruya_ai")).expanduser()
    )
    web_search: bool = field(default_factory=lambda: _env_bool("LATRUYA_WEB_SEARCH", True))
    memory: bool = field(default_factory=lambda: _env_bool("LATRUYA_MEMORY", True))
    compaction: bool = field(default_factory=lambda: _env_bool("LATRUYA_COMPACTION", True))
    refusal_fallback: bool = field(default_factory=lambda: _env_bool("LATRUYA_FALLBACK", True))
    show_thinking: bool = field(default_factory=lambda: _env_bool("LATRUYA_SHOW_THINKING", True))
    user_name: str = field(default_factory=lambda: os.environ.get("LATRUYA_USER_NAME", ""))

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got {self.effort!r}")

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"
