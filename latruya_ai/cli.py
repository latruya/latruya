"""Terminal interface:  python -m latruya_ai  (chat)  or  python -m latruya_ai web  (browser)."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .agent import Assistant
from .attachments import AttachmentError, from_path
from .chat import run_turn
from .config import EFFORT_LEVELS, Settings
from .store import ConversationStore

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


DIM, CYAN, YELLOW, RED, BOLD, GREEN = "2", "36", "33", "31", "1", "32"

HELP = """\
Commands:
  /new                 start a new conversation
  /chats               list saved conversations
  /open <number>       reopen a conversation from /chats
  /attach <path>       attach an image, PDF or text file to your next message
  /effort <level>      how hard to think: low, medium, high, xhigh, max
  /thinking            show or hide the reasoning summary
  /memory              show what the assistant remembers about you
  /forget              erase all memories
  /help                show this help
  /quit                exit
"""


def chat_loop(settings: Settings) -> None:
    assistant = Assistant(settings)
    store = ConversationStore(settings.conversations_dir)
    conv = store.create()
    pending: list[tuple[str, dict]] = []
    effort = settings.effort
    show_thinking = settings.show_thinking
    listed: list[dict] = []

    print(_c(BOLD, f"Latruya AI {__version__}") + _c(DIM, f"  ·  {settings.model}  ·  effort {effort}"))
    print(_c(DIM, "Type /help for commands. Ctrl+D to exit.\n"))

    while True:
        try:
            prompt = _c(GREEN, "you › ") if not pending else _c(GREEN, f"you [{len(pending)} file(s)] › ")
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
            if cmd in ("/quit", "/exit"):
                return
            if cmd == "/help":
                print(HELP)
            elif cmd == "/new":
                conv, pending = store.create(), []
                print(_c(DIM, "Started a new conversation.\n"))
            elif cmd == "/chats":
                listed = [c for c in store.list() if c["id"] != conv.id][:20]
                if not listed:
                    print(_c(DIM, "No other saved conversations.\n"))
                for i, c in enumerate(listed, 1):
                    print(f"  {i:2d}. {c['title']}")
                print()
            elif cmd == "/open":
                try:
                    conv = store.get(listed[int(arg) - 1]["id"])
                except (ValueError, IndexError, KeyError):
                    print(_c(RED, "Run /chats first, then /open <number>.\n"))
                    continue
                print(_c(DIM, f"Opened “{conv.title}”.\n"))
                for entry in conv.transcript[-6:]:
                    who = "you" if entry["role"] == "user" else "ai"
                    print(_c(DIM, f"{who}: {entry['text'][:300]}"))
                print()
            elif cmd == "/attach":
                try:
                    pending.append((os.path.basename(arg), from_path(arg)))
                    print(_c(DIM, f"Attached {arg}. It will be sent with your next message.\n"))
                except AttachmentError as exc:
                    print(_c(RED, f"{exc}\n"))
            elif cmd == "/effort":
                if arg in EFFORT_LEVELS:
                    effort = arg
                    print(_c(DIM, f"Effort set to {effort}.\n"))
                else:
                    print(_c(RED, f"Choose one of: {', '.join(EFFORT_LEVELS)}\n"))
            elif cmd == "/thinking":
                show_thinking = not show_thinking
                print(_c(DIM, f"Reasoning summary {'shown' if show_thinking else 'hidden'}.\n"))
            elif cmd == "/memory":
                if assistant.memory is None:
                    print(_c(DIM, "Memory is turned off.\n"))
                    continue
                files = assistant.memory.list_files()
                if not files:
                    print(_c(DIM, "Nothing remembered yet.\n"))
                for f in files:
                    print(_c(BOLD, f["path"]))
                    print(f["content"].rstrip() + "\n")
            elif cmd == "/forget":
                if assistant.memory is not None and input("Erase all memories? [y/N] ").lower() == "y":
                    assistant.memory.clear()
                    print(_c(DIM, "All memories erased.\n"))
            else:
                print(_c(RED, "Unknown command. Type /help.\n"))
            continue

        names = [n for n, _ in pending]
        blocks = [b for _, b in pending]
        pending = []
        _stream_reply(run_turn(assistant, store, conv, line, blocks, names, effort), show_thinking)


def _stream_reply(events, show_thinking: bool) -> None:
    mode = None  # "thinking" | "text"
    print()
    try:
        for event in events:
            kind = event["type"]
            if kind == "thinking" and show_thinking:
                if mode != "thinking":
                    print(_c(DIM, "thinking › "), end="")
                    mode = "thinking"
                print(_c(DIM, event["text"]), end="", flush=True)
            elif kind == "text":
                if mode != "text":
                    if mode is not None:
                        print("\n")
                    print(_c(CYAN, "ai › "), end="")
                    mode = "text"
                print(event["text"], end="", flush=True)
            elif kind == "status":
                print(("\n" if mode else "") + _c(YELLOW, f"  ⋯ {event['text']}"))
                mode = None
            elif kind == "sources":
                print(("\n" if mode else "") + _c(DIM, "  sources: " + ", ".join(s["url"] for s in event["items"][:5])))
                mode = None
            elif kind == "notice":
                print("\n" + _c(YELLOW, f"  ! {event['text']}"))
            elif kind == "error":
                print("\n" + _c(RED, f"  ✗ {event['text']}"))
            elif kind == "done":
                usage = event["usage"]
                cached = usage.get("cache_read_input_tokens", 0)
                print("\n" + _c(DIM, f"  [{usage.get('output_tokens', 0)} tokens out, {cached} read from cache]"))
    except KeyboardInterrupt:
        print("\n" + _c(YELLOW, "  ! Stopped. That message wasn't saved."))
    print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="latruya_ai", description="Latruya AI, a personal assistant built on Claude.")
    sub = parser.add_subparsers(dest="mode")
    sub.add_parser("chat", help="chat in the terminal (default)")
    web = sub.add_parser("web", help="start the web app")
    web.add_argument("--host", default=os.environ.get("LATRUYA_HOST", "127.0.0.1"))
    web.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="reasoning effort (default: high)")
    parser.add_argument("--model", help="Claude model ID (default: claude-opus-5-5)")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.effort:
        settings.effort = args.effort
    if args.model:
        settings.model = args.model

    if args.mode == "web":
        from .web import serve

        serve(settings, host=args.host, port=args.port)
    else:
        chat_loop(settings)
