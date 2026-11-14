"""Controls project orchestrator - runs one project through Claude Managed Agents.

    python run.py spec.pdf [more files...] --title "Pump station upgrade"

Phase 1  The lead engineer reads your spec, researches platforms in parallel and writes
         requirements, a vendor comparison, an I/O list and a preliminary bill of materials.
         -> You review the recommendation and approve it or pick another platform.
Phase 2  The team writes the functional design, PLC code, alarm list, test plan and draft
         supplier emails for the platform you approved.

Results are downloaded to ./projects/<title>/. Nothing is ever emailed or sent anywhere.
Set up the agents once first (see README.md).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).resolve().parent
LEAD_FILE = "./agents/controls-lead.md"
ENV_FILE = "./environments/controls-env.yaml"
MA_BETA = "managed-agents-2026-04-01"
INPUT_DIR = "/workspace/inputs"

PHASE1_DESCRIPTION = """\
Phase 1 - platform selection. Read the project specification in /workspace/inputs/ and
produce the platform recommendation package in /mnt/session/outputs/. Do not start PLC
development yet; the customer must approve the platform first."""

PHASE1_RUBRIC = """\
# Phase 1 rubric - platform selection
- `01_requirements.md` lists every control requirement in the spec, each with the spec
  section or page it came from, grouped by area (process, I/O, networks, HMI/SCADA, safety,
  environment, standards, schedule/commercial).
- `01_requirements.md` ends with an "Assumptions and open questions" section; no requirement
  appears that the spec does not support unless it is listed there as an assumption.
- `02_vendor_comparison.md` compares at least three platforms, including Siemens and
  Rockwell, in a table covering: I/O and CPU fit, required networks/protocols, safety
  capability, HMI/SCADA options, engineering software and licensing, availability and local
  support, and relative cost.
- Every product fact in `02_vendor_comparison.md` (part numbers, ratings, protocol support,
  lifecycle status) has a manufacturer source URL; unconfirmed part numbers say so.
- `02_vendor_comparison.md` ends with one clear recommendation, a runner-up, and the
  specific spec requirements that decided it.
- `03_io_list.xlsx` has one row per I/O point with tag, description, type (DI/DO/AI/AO/
  other), signal (e.g. 24 VDC, 4-20 mA), range and units where analog, and the spec
  reference; a summary sheet totals points by type and adds a stated spare allowance.
- `04_bill_of_materials.xlsx` lists the recommended platform's controller, I/O modules,
  network and HMI hardware with quantities that cover the I/O totals plus spares; part
  numbers that must be verified are marked.
- `00_summary.md` states the recommendation in plain language in under 300 words and lists
  every file produced."""

PHASE2_DESCRIPTION = """\
Phase 2 - development. The customer approved this platform: {platform}.
Using the phase 1 files in /mnt/session/outputs/ and the spec in /workspace/inputs/, produce
the engineering package for that platform in /mnt/session/outputs/. Update
00_summary.md to cover both phases."""

PHASE2_RUBRIC = """\
# Phase 2 rubric - development package
- `05_functional_design.docx` describes, per process area: the equipment, the control
  sequence step by step, operating modes (auto/manual/maintenance), interlocks, alarms and
  their responses, and how each requirement in 01_requirements.md is met (with references).
- `plc/` contains source files in the approved platform's importable text format (SCL for
  Siemens, Structured Text for Rockwell or IEC 61131-3), with one reusable function block or
  AOI per equipment type, instantiated per device, and a tag table/list for import.
- Tag names in the code match `03_io_list.xlsx` exactly; `plc/io_coverage.md` shows every
  I/O point is used or listed as a spare, produced by a script, not by hand.
- No safety function is implemented in standard logic; each safety function has a marked
  placeholder stating it needs design and validation by a qualified safety engineer.
- `06_alarm_list.xlsx` lists every alarm in the code with tag, message text, priority and
  operator response.
- `07_test_plan.md` has simulation/FAT test cases for every sequence, interlock and alarm,
  with steps and expected results, and says how to run them in the platform's simulator
  (PLCSIM for Siemens, Emulate for Rockwell, CODESYS simulation for IEC).
- `08_supplier_emails.md` contains draft quote-request emails for the bill of materials;
  they are drafts only and nothing has been sent.
- `00_summary.md` lists every deliverable, the open items, and what a qualified engineer must
  review before anything runs on real equipment."""


# --------------------------------------------------------------------------- setup
def load_ids() -> tuple[str, str]:
    agent_id = os.environ.get("CONTROLS_AGENT_ID")
    env_id = os.environ.get("CONTROLS_ENV_ID")
    lock = HERE / "claude-lock.json"
    if (not agent_id or not env_id) and lock.exists():
        resources = json.loads(lock.read_text()).get("resources", {})
        agent_id = agent_id or resources.get(LEAD_FILE, {}).get("id")
        env_id = env_id or resources.get(ENV_FILE, {}).get("id")
    if not agent_id or not env_id:
        sys.exit(
            "Agent IDs not found. Run the one-time setup in README.md (ant apply), "
            "or set CONTROLS_AGENT_ID and CONTROLS_ENV_ID."
        )
    return agent_id, env_id


def upload_inputs(client: anthropic.Anthropic, paths: list[str]) -> list[dict]:
    resources, used = [], set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            sys.exit(f"File not found: {path}")
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)
        if name in used:
            name = f"{path.stem}_{len(used)}{path.suffix}"
        used.add(name)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            uploaded = client.files.upload(file=(name, fh, mime))
        print(f"  uploaded {path.name}")
        resources.append({"type": "file", "file_id": uploaded.id, "mount_path": f"{INPUT_DIR}/{name}"})
    return resources


# --------------------------------------------------------------------------- streaming
class SessionRunner:
    def __init__(self, client: anthropic.Anthropic, session_id: str, unattended: bool) -> None:
        self.client = client
        self.session_id = session_id
        self.unattended = unattended
        self.seen: set[str] = set()
        self.list_cost = "?"
        self.last_outcome: str | None = None

    def _events(self, on_open=None):
        """Stream events; after a dropped connection, reconnect and replay what was missed.

        `on_open` runs once, right after the first stream opens (stream-first ordering).
        """
        while True:
            try:
                with self.client.beta.sessions.events.stream(session_id=self.session_id) as stream:
                    if on_open is not None:
                        on_open()
                        on_open = None
                    for ev in self.client.beta.sessions.events.list(session_id=self.session_id):
                        if ev.id not in self.seen:
                            self.seen.add(ev.id)
                            yield ev
                    for ev in stream:
                        if ev.id not in self.seen:
                            self.seen.add(ev.id)
                            yield ev
                return
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                print(f"\n  (connection dropped: {exc.__class__.__name__}; reconnecting)")
                time.sleep(3)

    def run_outcome(self, description: str, rubric: str, max_iterations: int) -> str | None:
        """Send one outcome, show progress, and return the grader's final result."""
        self.last_outcome = None

        def kickoff() -> None:
            self.client.beta.sessions.events.send(
                session_id=self.session_id,
                events=[{
                    "type": "user.define_outcome",
                    "description": description,
                    "rubric": {"type": "text", "content": rubric},
                    "max_iterations": max_iterations,
                }],
            )

        for ev in self._events(on_open=kickoff):
            if self._handle(ev):
                return self.last_outcome
        return self.last_outcome

    def _handle(self, ev) -> bool:
        """Print one event and answer it if needed. Returns True when the turn is over."""
        kind = ev.type
        if kind == "agent.message":
            for block in getattr(ev, "content", []) or []:
                if getattr(block, "type", "") == "text":
                    print(block.text, end="", flush=True)
            print()
        elif kind == "session.thread_created":
            print(f"  ▸ {getattr(ev, 'agent_name', 'agent')} started")
        elif kind == "agent.thread_message_sent":
            print(f"  → task for {getattr(ev, 'to_agent_name', 'agent')}")
        elif kind == "agent.thread_message_received":
            print(f"  ← report from {getattr(ev, 'from_agent_name', 'agent')}")
        elif kind == "workflow_run.created":
            print("  ▸ parallel workflow started")
        elif kind == "workflow_run.status_ended":
            print(f"  ▸ parallel workflow ended ({getattr(ev, 'result', '')})")
        elif kind == "span.outcome_evaluation_end":
            self.last_outcome = getattr(ev, "result", None)
            print(f"\n  ✔ review round {getattr(ev, 'iteration', '?')}: {self.last_outcome}")
            explanation = getattr(ev, "explanation", "") or ""
            if explanation and self.last_outcome != "satisfied":
                print("    " + explanation[:600].replace("\n", "\n    "))
        elif kind in ("agent.tool_use", "agent.mcp_tool_use") and getattr(ev, "evaluated_permission", None) == "ask":
            self._confirm(ev)
        elif kind == "agent.custom_tool_use":
            self.client.beta.sessions.events.send(
                session_id=self.session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": ev.id,
                    "content": [{"type": "text", "text": "No custom tools are available."}],
                    "is_error": True,
                }],
            )
        elif kind == "session.usage":
            cost = getattr(ev, "list_cost", None)
            if cost is not None:
                self.list_cost = f"${int(cost.amount) / 100:.2f}"
        elif kind == "session.error":
            print(f"\n  ! session error: {getattr(ev, 'error', ev)}")
        elif kind == "session.status_idle":
            reason = getattr(getattr(ev, "stop_reason", None), "type", None)
            if reason == "requires_action":
                return False  # waiting on a confirmation we answer as it arrives
            if reason == "budget_reached":
                return not self._raise_budget()
            return True
        elif kind == "session.status_terminated":
            print("\n  ! The session ended.")
            return True
        return False

    def _confirm(self, ev) -> None:
        name = getattr(ev, "name", "tool")
        detail = json.dumps(getattr(ev, "input", {}), default=str)[:400]
        if self.unattended:
            allow = False
            print(f"\n  ✗ denied {name} (unattended run): {detail}")
        else:
            answer = input(f"\n  ? The agent wants to run {name}: {detail}\n    Allow? [y/N] ")
            allow = answer.strip().lower() == "y"
        self.client.beta.sessions.events.send(
            session_id=self.session_id,
            events=[{"type": "user.tool_confirmation", "tool_use_id": ev.id, "result": "allow" if allow else "deny"}],
        )

    def _raise_budget(self) -> bool:
        session = self.client.beta.sessions.retrieve(session_id=self.session_id)
        spent = int(session.usage.list_cost.amount) if getattr(session.usage, "list_cost", None) else 0
        print(f"\n  ! Budget reached (spent ${spent / 100:.2f}).")
        if self.unattended:
            return False
        extra = input("    Add how many dollars to continue? (Enter to stop) ").strip()
        if not extra.isdigit() or int(extra) <= 0:
            return False
        new_cap = spent + int(extra) * 100
        self.client.beta.sessions.update(
            session_id=self.session_id,
            budget={"type": "limit", "max_list_cost": {"amount": str(new_cap), "currency": "USD"}},
        )
        print(f"    Budget raised to ${new_cap / 100:.2f}; continuing.")
        return True


# --------------------------------------------------------------------------- outputs
def download_outputs(client: anthropic.Anthropic, session_id: str, out_dir: Path) -> list[Path]:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for attempt in range(3):  # outputs are indexed a few seconds after the session idles
        files = list(client.beta.files.list(scope_id=session_id, betas=[MA_BETA]))
        if files:
            break
        time.sleep(3)
    for f in files:
        name = (f.filename or f.id).lstrip("/").replace("mnt/session/outputs/", "")
        target = (out_dir / name).resolve()
        if not target.is_relative_to(out_dir):
            continue  # never write outside the project folder
        target.parent.mkdir(parents=True, exist_ok=True)
        client.beta.files.download(f.id, betas=[MA_BETA]).write_to_file(target)
        saved.append(target)
    return saved


def show_summary(out_dir: Path) -> None:
    summary = out_dir / "00_summary.md"
    if summary.exists():
        print("\n" + "─" * 70 + "\n" + summary.read_text(encoding="utf-8").strip() + "\n" + "─" * 70)


# --------------------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run a controls project through the orchestrator.")
    parser.add_argument("files", nargs="*", help="specification files (PDF, Word, Excel, text, drawings)")
    parser.add_argument("--title", default="Controls project", help="project name")
    parser.add_argument("--budget", type=int, default=40, help="spending cap in US dollars (default 40)")
    parser.add_argument("--platform", help="skip the approval prompt and use this platform in phase 2")
    parser.add_argument("--phase1-only", action="store_true", help="stop after the platform recommendation")
    parser.add_argument("--unattended", action="store_true", help="never prompt; deny anything that needs approval")
    parser.add_argument("--download", metavar="SESSION_ID", help="only download the files of an earlier session")
    args = parser.parse_args()

    client = anthropic.Anthropic()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", args.title).strip("-").lower() or "project"
    out_dir = Path("projects") / slug

    if args.download:
        saved = download_outputs(client, args.download, out_dir)
        print(f"Downloaded {len(saved)} files to {out_dir}/")
        return
    if not args.files:
        parser.error("give at least one specification file")
    if args.unattended and not (args.platform or args.phase1_only):
        parser.error("--unattended needs --platform or --phase1-only (someone has to approve the platform)")

    agent_id, env_id = load_ids()
    print(f"Project: {args.title}  (budget ${args.budget})")
    resources = upload_inputs(client, args.files)
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=env_id,
        title=args.title,
        resources=resources,
        budget={"type": "limit", "max_list_cost": {"amount": str(args.budget * 100), "currency": "USD"}},
    )
    print(f"Session {session.id}")
    print(f"Watch live: https://platform.claude.com/workspaces/default/sessions/{session.id}\n")
    runner = SessionRunner(client, session.id, args.unattended)

    print("═══ Phase 1: requirements and platform selection ═══")
    result = runner.run_outcome(PHASE1_DESCRIPTION, PHASE1_RUBRIC, max_iterations=4)
    saved = download_outputs(client, session.id, out_dir)
    print(f"\nPhase 1 {result or 'finished'}; {len(saved)} files in {out_dir}/  (spent {runner.list_cost})")
    show_summary(out_dir)
    if args.phase1_only:
        return

    platform = args.platform
    if not platform:
        answer = input(
            "\nApprove the recommended platform? Press Enter to approve,\n"
            "type a different platform (e.g. 'Rockwell ControlLogix'), or 'q' to stop: "
        ).strip()
        if answer.lower() in ("q", "quit", "stop"):
            print(f"Stopped after phase 1. Your files are in {out_dir}/")
            return
        platform = answer or "the platform recommended in 02_vendor_comparison.md"

    print("\n═══ Phase 2: development package ═══")
    result = runner.run_outcome(PHASE2_DESCRIPTION.format(platform=platform), PHASE2_RUBRIC, max_iterations=5)
    saved = download_outputs(client, session.id, out_dir)
    print(f"\nPhase 2 {result or 'finished'}; {len(saved)} files in {out_dir}/  (spent {runner.list_cost})")
    show_summary(out_dir)
    print("\nReminder: import, compile and test in the simulator first; have safety logic reviewed by a qualified engineer.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. The session keeps its work; download it later with --download SESSION_ID.")
    except anthropic.AuthenticationError:
        sys.exit("Your Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY or run `ant auth login`.")
