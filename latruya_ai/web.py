"""Web app: a FastAPI server that streams replies to the chat page in static/index.html."""

import json
import os
import secrets
import threading
from collections import defaultdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import __version__
from .agent import Assistant
from .attachments import AttachmentError, to_content_block
from .chat import run_turn
from .config import EFFORT_LEVELS, Settings
from .store import ConversationStore

STATIC = Path(__file__).parent / "static"
MAX_FILES = 10


def create_app(settings: Settings | None = None, assistant: Assistant | None = None) -> FastAPI:
    settings = settings or Settings()
    assistant = assistant or Assistant(settings)
    store = ConversationStore(settings.conversations_dir)
    locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
    password = os.environ.get("LATRUYA_PASSWORD", "")
    basic = HTTPBasic(auto_error=False)

    def check_auth(creds: Annotated[HTTPBasicCredentials | None, Depends(basic)]) -> None:
        if not password:
            return
        if creds is None or not secrets.compare_digest(creds.password.encode(), password.encode()):
            raise HTTPException(401, "Password required", headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(title="Latruya AI", version=__version__, dependencies=[Depends(check_auth)])

    @app.get("/static/{name}", include_in_schema=False)
    def static_file(name: str) -> FileResponse:
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config() -> dict:
        return {
            "version": __version__,
            "model": settings.model,
            "effort": settings.effort,
            "effort_levels": list(EFFORT_LEVELS),
            "web_search": settings.web_search,
            "memory": settings.memory,
        }

    @app.get("/api/conversations")
    def list_conversations() -> list[dict]:
        return store.list()

    @app.post("/api/conversations")
    def new_conversation() -> dict:
        return store.create().summary()

    @app.get("/api/conversations/{conv_id}")
    def get_conversation(conv_id: str) -> dict:
        try:
            conv = store.get(conv_id)
        except KeyError:
            raise HTTPException(404, "Conversation not found") from None
        return {**conv.summary(), "transcript": conv.transcript, "usage": conv.usage}

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str) -> dict:
        try:
            store.delete(conv_id)
        except KeyError:
            raise HTTPException(404, "Conversation not found") from None
        return {"ok": True}

    @app.post("/api/conversations/{conv_id}/messages")
    async def send_message(
        conv_id: str,
        text: Annotated[str, Form()] = "",
        effort: Annotated[str, Form()] = "",
        files: Annotated[list[UploadFile] | None, File()] = None,
    ) -> StreamingResponse:
        try:
            store.get(conv_id)
        except KeyError:
            raise HTTPException(404, "Conversation not found") from None
        if effort and effort not in EFFORT_LEVELS:
            raise HTTPException(400, f"effort must be one of {EFFORT_LEVELS}")
        files = files or []
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"Attach at most {MAX_FILES} files at a time.")
        blocks, names = [], []
        for upload in files:
            data = await upload.read()
            try:
                blocks.append(to_content_block(upload.filename or "file", data, upload.content_type))
            except AttachmentError as exc:
                raise HTTPException(400, str(exc)) from None
            names.append(upload.filename or "file")

        lock = locks[conv_id]

        def events():
            if not lock.acquire(blocking=False):
                busy = {"type": "error", "text": "This chat is still answering. Wait for it to finish."}
                yield f"data: {json.dumps(busy)}\n\n"
                return
            try:
                fresh = store.get(conv_id)  # reload under the lock so no turn is lost
                for event in run_turn(assistant, store, fresh, text.strip(), blocks, names, effort or None):
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                lock.release()

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.get("/api/memory")
    def get_memory() -> list[dict]:
        return assistant.memory.list_files() if assistant.memory else []

    @app.delete("/api/memory")
    def clear_memory() -> dict:
        if assistant.memory:
            assistant.memory.clear()
        return {"ok": True}

    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost") and not os.environ.get("LATRUYA_PASSWORD"):
        print("Warning: the app is reachable from other machines with no password. Set LATRUYA_PASSWORD.")
    print(f"Latruya AI is running at http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
