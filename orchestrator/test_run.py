"""Offline tests for run.py: a fake Managed Agents session drives the real event loop."""

from __future__ import annotations

from types import SimpleNamespace as NS

import run


def ev(i, type_, **kw):
    return NS(id=f"sevt_{i}", type=type_, **kw)


class FakeStream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)


class FakeClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)  # one list of events per stream opened
        self.history = []
        self.sent = []
        self.updates = []

        def stream(session_id):
            events = self.scripts.pop(0)
            self.history.extend(events)
            return FakeStream(events)

        def send(session_id, events):
            self.sent.extend(events)

        self.beta = NS(
            sessions=NS(
                events=NS(stream=stream, send=send, list=lambda session_id: list(self.history)),
                retrieve=lambda session_id: NS(usage=NS(list_cost=NS(amount="4012"))),
                update=lambda session_id, budget: self.updates.append(budget),
            )
        )


def test_outcome_flow_answers_confirmations_and_stops_on_idle(monkeypatch):
    phase1 = [
        ev(1, "session.thread_created", agent_name="Vendor researcher"),
        ev(2, "agent.tool_use", name="bash", input={"command": "rm -rf /tmp/x"}, evaluated_permission="ask"),
        ev(3, "agent.message", content=[NS(type="text", text="Recommendation: Siemens S7-1500")]),
        ev(4, "span.outcome_evaluation_end", result="satisfied", iteration=1, explanation=""),
        ev(5, "session.usage", list_cost=NS(amount="1234")),
        ev(6, "session.status_idle", stop_reason=NS(type="end_turn")),
        ev(7, "agent.message", content=[NS(type="text", text="never reached")]),
    ]
    client = FakeClient([phase1])
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    runner = run.SessionRunner(client, "sesn_1", unattended=False)

    assert runner.run_outcome("desc", "rubric", 3) == "satisfied"
    assert client.sent[0]["type"] == "user.define_outcome"  # kickoff sent after the stream opened
    assert client.sent[1] == {"type": "user.tool_confirmation", "tool_use_id": "sevt_2", "result": "deny"}
    assert runner.list_cost == "$12.34"


def test_second_outcome_ignores_first_outcomes_history():
    phase1 = [ev(1, "span.outcome_evaluation_end", result="satisfied", iteration=1), ev(2, "session.status_idle", stop_reason=NS(type="end_turn"))]
    phase2 = [ev(3, "span.outcome_evaluation_end", result="max_iterations_reached", iteration=5, explanation="gaps"),
              ev(4, "session.status_idle", stop_reason=NS(type="end_turn"))]
    client = FakeClient([phase1, phase2])
    runner = run.SessionRunner(client, "sesn_1", unattended=True)
    assert runner.run_outcome("p1", "r1", 3) == "satisfied"
    # Phase 1's idle event is replayed from history but must not end phase 2.
    assert runner.run_outcome("p2", "r2", 5) == "max_iterations_reached"
    assert [e["type"] for e in client.sent] == ["user.define_outcome", "user.define_outcome"]


def test_requires_action_keeps_waiting_and_budget_can_be_raised(monkeypatch):
    events = [
        ev(1, "session.status_idle", stop_reason=NS(type="requires_action")),
        ev(2, "session.status_idle", stop_reason=NS(type="budget_reached")),
        ev(3, "span.outcome_evaluation_end", result="satisfied", iteration=2),
        ev(4, "session.status_idle", stop_reason=NS(type="end_turn")),
    ]
    client = FakeClient([events])
    monkeypatch.setattr("builtins.input", lambda prompt="": "15")
    runner = run.SessionRunner(client, "sesn_1", unattended=False)
    assert runner.run_outcome("d", "r", 3) == "satisfied"
    assert client.updates == [{"type": "limit", "max_list_cost": {"amount": "5512", "currency": "USD"}}]


def test_unattended_denies_and_stops_at_budget():
    events = [
        ev(1, "agent.tool_use", name="bash", input={}, evaluated_permission="ask"),
        ev(2, "session.status_idle", stop_reason=NS(type="budget_reached")),
        ev(3, "agent.message", content=[NS(type="text", text="never reached")]),
    ]
    client = FakeClient([events])
    runner = run.SessionRunner(client, "sesn_1", unattended=True)
    runner.run_outcome("d", "r", 3)
    assert client.sent[1]["result"] == "deny" and client.updates == []


def test_load_ids_from_lockfile(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTROLS_AGENT_ID", raising=False)
    monkeypatch.delenv("CONTROLS_ENV_ID", raising=False)
    (tmp_path / "claude-lock.json").write_text(
        '{"resources": {"./agents/controls-lead.md": {"id": "agent_1"}, "./environments/controls-env.yaml": {"id": "env_1"}}}'
    )
    monkeypatch.setattr(run, "HERE", tmp_path)
    assert run.load_ids() == ("agent_1", "env_1")
