"""Local file storage behind Claude's memory tool (memory_20250818).

Claude sees a virtual directory called /memories. Every path it sends is
mapped into one folder on disk, and anything that would escape that folder
is rejected.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

VIRTUAL_ROOT = "/memories"
MAX_FILE_CHARS = 200_000


class MemoryError_(Exception):
    """A memory command that cannot be carried out; reported back to Claude."""


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- path handling -------------------------------------------------
    def _resolve(self, virtual_path: str) -> Path:
        if not isinstance(virtual_path, str) or not virtual_path:
            raise MemoryError_("A path is required.")
        path = virtual_path.strip()
        if path != VIRTUAL_ROOT and not path.startswith(VIRTUAL_ROOT + "/"):
            raise MemoryError_(f"Paths must start with {VIRTUAL_ROOT}, got {virtual_path!r}.")
        relative = path[len(VIRTUAL_ROOT):].lstrip("/")
        target = (self.root / relative).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise MemoryError_(f"Path {virtual_path!r} is outside {VIRTUAL_ROOT}.")
        return target

    def _virtual(self, real: Path) -> str:
        rel = real.relative_to(self.root).as_posix()
        return VIRTUAL_ROOT if rel == "." else f"{VIRTUAL_ROOT}/{rel}"

    # ---- dispatch ------------------------------------------------------
    def execute(self, command: dict[str, Any]) -> str:
        name = command.get("command")
        handler = {
            "view": self.view,
            "create": self.create,
            "str_replace": self.str_replace,
            "insert": self.insert,
            "delete": self.delete,
            "rename": self.rename,
        }.get(name)  # type: ignore[arg-type]
        if handler is None:
            raise MemoryError_(f"Unknown memory command {name!r}.")
        return handler(command)

    # ---- commands ------------------------------------------------------
    def view(self, cmd: dict[str, Any]) -> str:
        target = self._resolve(cmd.get("path", VIRTUAL_ROOT))
        if not target.exists():
            raise MemoryError_(f"{cmd.get('path')} does not exist.")
        if target.is_dir():
            entries = sorted(target.rglob("*"))
            lines = [f"Directory {self._virtual(target)}:"]
            for entry in entries:
                if entry.is_file():
                    lines.append(f"- {self._virtual(entry)} ({entry.stat().st_size} bytes)")
            if len(lines) == 1:
                lines.append("(empty)")
            return "\n".join(lines)
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        view_range = cmd.get("view_range")
        start = 1
        if view_range:
            start = max(1, int(view_range[0]))
            end = int(view_range[1]) if len(view_range) > 1 and int(view_range[1]) != -1 else len(lines)
            lines = lines[start - 1:end]
        return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, start=start)) or "(empty file)"

    def create(self, cmd: dict[str, Any]) -> str:
        target = self._resolve(cmd.get("path", ""))
        if target == self.root:
            raise MemoryError_("Cannot overwrite the memory directory itself.")
        text = cmd.get("file_text", "")
        if not isinstance(text, str):
            raise MemoryError_("file_text must be a string.")
        if len(text) > MAX_FILE_CHARS:
            raise MemoryError_(f"File too large (limit {MAX_FILE_CHARS} characters).")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return f"Saved {self._virtual(target)}."

    def str_replace(self, cmd: dict[str, Any]) -> str:
        target = self._require_file(cmd.get("path", ""))
        old, new = cmd.get("old_str", ""), cmd.get("new_str", "")
        text = target.read_text(encoding="utf-8")
        count = text.count(old) if old else 0
        if count == 0:
            raise MemoryError_("old_str was not found in the file.")
        if count > 1:
            raise MemoryError_(f"old_str appears {count} times; make it unique.")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Updated {self._virtual(target)}."

    def insert(self, cmd: dict[str, Any]) -> str:
        target = self._require_file(cmd.get("path", ""))
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        line_no = int(cmd.get("insert_line", 0))
        if not 0 <= line_no <= len(lines):
            raise MemoryError_(f"insert_line must be between 0 and {len(lines)}.")
        new_text = cmd.get("insert_text", "")
        if not new_text.endswith("\n"):
            new_text += "\n"
        if line_no > 0 and not lines[line_no - 1].endswith("\n"):
            lines[line_no - 1] += "\n"
        lines.insert(line_no, new_text)
        target.write_text("".join(lines), encoding="utf-8")
        return f"Inserted text into {self._virtual(target)} after line {line_no}."

    def delete(self, cmd: dict[str, Any]) -> str:
        target = self._resolve(cmd.get("path", ""))
        if target == self.root:
            raise MemoryError_("Cannot delete the memory directory itself.")
        if not target.exists():
            raise MemoryError_(f"{cmd.get('path')} does not exist.")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"Deleted {cmd.get('path')}."

    def rename(self, cmd: dict[str, Any]) -> str:
        source = self._resolve(cmd.get("old_path", ""))
        dest = self._resolve(cmd.get("new_path", ""))
        if source == self.root or dest == self.root:
            raise MemoryError_("Cannot rename the memory directory itself.")
        if not source.exists():
            raise MemoryError_(f"{cmd.get('old_path')} does not exist.")
        if dest.exists():
            raise MemoryError_(f"{cmd.get('new_path')} already exists.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        return f"Renamed {cmd.get('old_path')} to {cmd.get('new_path')}."

    def _require_file(self, path: str) -> Path:
        target = self._resolve(path)
        if not target.is_file():
            raise MemoryError_(f"{path} is not an existing file.")
        return target

    # ---- for the UI ----------------------------------------------------
    def list_files(self) -> list[dict[str, Any]]:
        return [
            {"path": self._virtual(p), "content": p.read_text(encoding="utf-8", errors="replace")}
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        ]

    def clear(self) -> None:
        for child in self.root.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
