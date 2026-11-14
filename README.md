## Hi there 👋

<!--
**latruya/latruya** is a ✨ _special_ ✨ repository because its `README.md` (this file) appears on your GitHub profile.

Here are some ideas to get you started:

- 🔭 I’m currently working on ...
- 🌱 I’m currently learning ...
- 👯 I’m looking to collaborate on ...
- 🤔 I’m looking for help with ...
- 💬 Ask me about ...
- 📫 How to reach me: ...
- 😄 Pronouns: ...
- ⚡ Fun fact: ...
-->

---

## 🤖 Latruya AI: my personal assistant

A personal AI assistant built on **Claude Opus 5.5**, Anthropic's current flagship model. It runs as a chat page in your browser or as a terminal program.

**What it can do**

- 🧠 **Think before answering.** It uses adaptive reasoning, and you can expand a short summary of that reasoning under each answer. A selector lets you trade speed for depth (Fast, Balanced, Thorough, Deep, Maximum).
- 🌐 **Search the web and read pages**, so answers about news, prices and other recent facts are current, with source links shown.
- 💾 **Remember you across conversations.** Your name, preferences and ongoing projects are saved as plain files on your own computer. You can see or erase them at any time ("What I remember" in the sidebar, or `/memory` and `/forget` in the terminal). It is told never to store passwords or ID numbers.
- 📎 **Read your files**: photos, screenshots, PDFs and text or code files (drag and drop works too).
- 💬 **Keep your chats.** Conversations are saved and can be reopened. Very long chats are summarised automatically so they never hit a length limit.
- ⚡ **Stay fast and affordable.** Replies stream as they're written, and earlier parts of a chat are cached so long conversations cost less.
- 🛟 **Recover on its own.** If a request is declined by a safety filter, it is retried on Anthropic's recommended fallback model. Errors are explained in plain language and your chat is never corrupted.

### Setup (about 5 minutes)

1. **Get an API key** at <https://platform.claude.com/> (Settings → API keys). Usage is billed by Anthropic per message.
2. **Install** (needs Python 3.10 or newer):

   ```bash
   git clone https://github.com/latruya/latruya.git
   cd latruya
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Add your key** for this terminal session:

   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."     # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
   ```

4. **Run it:**

   ```bash
   python -m latruya_ai web     # then open http://localhost:8000 in your browser
   python -m latruya_ai         # or chat right in the terminal
   ```

### Settings

All optional, set as environment variables:

| Variable | Default | What it does |
|---|---|---|
| `LATRUYA_EFFORT` | `high` | Default thinking depth: `low`, `medium`, `high`, `xhigh`, `max` |
| `LATRUYA_MODEL` | `claude-opus-5-5` | Claude model to use |
| `LATRUYA_USER_NAME` | – | Your name, so it always knows who it's talking to |
| `LATRUYA_DATA_DIR` | `~/.latruya_ai` | Where chats and memories are saved |
| `LATRUYA_WEB_SEARCH` | `true` | Turn web search and page reading on or off |
| `LATRUYA_MEMORY` | `true` | Turn long-term memory on or off |
| `LATRUYA_SHOW_THINKING` | `true` | Show the reasoning summary in the terminal |
| `LATRUYA_PASSWORD` | – | Require a password for the web app (**set this before putting it online**) |
| `PORT` / `LATRUYA_HOST` | `8000` / `127.0.0.1` | Where the web app listens. Use host `0.0.0.0` to reach it from your phone on the same Wi-Fi (with a password set) |

### Project layout

```
latruya_ai/
  agent.py        the assistant: Claude API calls, streaming, tools, memory
  chat.py         one chat turn: saves the chat and undoes it cleanly on errors
  memory.py       the memory folder (kept safely inside your data folder)
  attachments.py  turns images, PDFs and text files into Claude input
  store.py        saves conversations as JSON
  web.py          web server (FastAPI) and static/index.html chat page
  cli.py          terminal interface
tests/            offline tests (no API key needed): pip install -r requirements-dev.txt && python -m pytest
```
