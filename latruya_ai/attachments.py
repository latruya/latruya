"""Turns uploaded files into Claude content blocks (images, PDFs, text)."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 400_000


class AttachmentError(ValueError):
    pass


def guess_type(filename: str, declared: str | None = None) -> str:
    if declared and declared != "application/octet-stream":
        return declared
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def to_content_block(filename: str, data: bytes, media_type: str | None = None) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise AttachmentError(f"{filename} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    media_type = guess_type(filename, media_type)
    b64 = base64.standard_b64encode(data).decode("ascii")

    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type in IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    if media_type == "application/pdf":
        return {
            "type": "document",
            "title": filename,
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentError(
            f"{filename}: unsupported file type ({media_type}). Use images, PDFs or text files."
        ) from exc
    if len(text) > MAX_TEXT_CHARS:
        raise AttachmentError(f"{filename} has more than {MAX_TEXT_CHARS:,} characters.")
    return {
        "type": "document",
        "title": filename,
        "source": {"type": "text", "media_type": "text/plain", "data": text},
    }


def from_path(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise AttachmentError(f"File not found: {p}")
    return to_content_block(p.name, p.read_bytes())
