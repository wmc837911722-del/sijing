"""Offline adapter timing boundaries; no actual CLI or model calls."""
import json
from types import SimpleNamespace

import pytest
from businessmap import codex_provider as c
from businessmap.providers import ToolError
from test_codex_provider import service, cli, save


def run_adapter(monkeypatch, *, guard_action=None, observation=.6, events=None, exit_code=0, parse_delay=0, deadline=1.0):
    clock = [0.0]
    sink = [None]
    calls = []
    stopped = []
    class Process:
        returncode = exit_code
        def poll(self):
            return exit_code if sink[0] is not None and clock[0] >= observation else None
    def spawn(*args, stdout, **kwargs):
        sink[0] = stdout
        return Process()
    default_events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": "ok"})}},
        {"type": "turn.completed", "usage": {}},
    ]
    def sleep(_):
        clock[0] = observation
        raw = events if events is not None else b"".join((json.dumps(event) + "\n").encode() for event in default_events)
        sink[0].write(raw)
        sink[0].flush()
    def guard():
        calls.append(clock[0])
        if len(calls) == 2 and guard_action:
            guard_action(clock, sink[0])
    real_loads = json.loads
    def loads(value, *args, **kwargs):
        result = real_loads(value, *args, **kwargs)
        if parse_delay and isinstance(value, bytes):
            clock[0] += parse_delay
        return result
    monkeypatch.setattr(c, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(c, "status", lambda: {"status": "ready", "version": "fixture"})
    monkeypatch.setattr(c, "find_codex", lambda: "/fake/codex")
    monkeypatch.setattr(c, "_text_only_catalog", lambda *_: "{}")
    monkeypatch.setattr(c.subprocess, "Popen", spawn)
    monkeypatch.setattr(c, "_stop", lambda process: stopped.append(True))
    monkeypatch.setattr(c.json, "loads", loads)
    def invoke():
        return c.complete([], "default", max_output=64, before_send=guard, absolute_deadline=deadline)
    return invoke, calls, stopped, clock


def test_completed_result_observed_before_slow_due_guard_is_returned(monkeypatch):
    invoke, calls, stopped, clock = run_adapter(monkeypatch, guard_action=lambda clock, _: clock.__setitem__(0, 1.1))
    assert invoke()["text"] == "ok"
    assert calls == [0.0, .6, 1.1]
    assert stopped == [True]


@pytest.mark.parametrize("observation,parse_delay", [(1.0, 0), (.6, .2)])
def test_first_observation_or_parse_at_deadline_fails(monkeypatch, observation, parse_delay):
    invoke, _, stopped, _ = run_adapter(monkeypatch, observation=observation, parse_delay=parse_delay)
    with pytest.raises(ToolError) as error:
        invoke()
    assert error.value.codex_reason == "timeout"
    assert stopped == [True]


def test_due_guard_can_revoke_timely_candidate(monkeypatch):
    def revoke(clock, output):
        raise ToolError("revoked", 409)
    invoke, _, stopped, _ = run_adapter(monkeypatch, guard_action=revoke)
    with pytest.raises(ToolError, match="revoked"):
        invoke()
    assert stopped == [True]


def test_trailing_bytes_after_candidate_are_rejected(monkeypatch):
    def append(clock, output):
        output.write(b'{"type":"error"}\n')
        output.flush()
        clock[0] = 1.1
    invoke, _, stopped, _ = run_adapter(monkeypatch, guard_action=append)
    with pytest.raises(ToolError):
        invoke()
    assert stopped == [True]


@pytest.mark.parametrize("limits", [
    {"absolute_deadline": 1.0},
    {"timeout": 1.0, "execution_timeout": 360, "output_schema": {"type": "object"}},
], ids=["absolute", "relative-with-execution-window"])
def test_initial_guard_expiry_never_spawns(monkeypatch, limits):
    clock = [0.0]
    monkeypatch.setattr(c, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(c, "status", lambda: {"status": "ready", "version": "fixture"})
    monkeypatch.setattr(c, "find_codex", lambda: "/fake/codex")
    monkeypatch.setattr(c, "_text_only_catalog", lambda *_: "{}")
    monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned after deadline"))
    with pytest.raises(ToolError) as error:
        c.complete([], "default", max_output=64, before_send=lambda: clock.__setitem__(0, 1.0), **limits)
    assert error.value.status_code == 408 and error.value.codex_reason == "timeout"


@pytest.mark.parametrize("raw,exit_code,reason", [
    (b'{"type":"turn.completed"', 0, "incomplete"),
    (b'{"type":"item.completed","item":{"type":"command_execution"}}\n', 0, "unexpected_tool"),
    (b'{"type":"turn.failed","error":{"message":"failure"}}\n', 0, "provider_error"),
    (b'not-json\n', 0, "invalid_event"),
    (b'{"type":"turn.completed","usage":{}}\n', 1, "process_exit"),
    (b'{"type":"item.completed","item":{"type":"agent_message","text":"{\\"answer\\":\\"ok\\"}"}}\n'
     b'{"type":"turn.completed","usage":{}}\n{"type":"thread.started"}\n', 0, "invalid_event"),
])
def test_bad_or_trailing_events_and_nonzero_exit_fail_closed(monkeypatch, raw, exit_code, reason):
    invoke, _, stopped, _ = run_adapter(monkeypatch, events=raw, exit_code=exit_code)
    with pytest.raises(ToolError) as error:
        invoke()
    assert error.value.codex_reason == reason
    assert stopped == [True]


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1, 0, True, "1"])
def test_invalid_absolute_deadline_is_rejected_before_spawn(monkeypatch, deadline):
    monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected spawn"))
    with pytest.raises(ToolError) as error:
        c.complete([], "default", max_output=64, before_send=lambda: None, absolute_deadline=deadline)
    assert error.value.codex_reason == "timeout"


def test_final_guard_can_revoke_timely_candidate(monkeypatch):
    invoke, calls, stopped, _ = run_adapter(monkeypatch)
    # The second check is periodic; final check must independently deny.
    original = c.complete
    def fail_final():
        if len(calls) == 2:
            raise ToolError("final revoked", 409)
        calls.append(0.0)
    with pytest.raises(ToolError, match="final revoked"):
        original([], "default", max_output=64, before_send=fail_final, absolute_deadline=1.0)
    assert stopped == [True]


@pytest.mark.parametrize("execution_window", [{}, {"_codex_execution_timeout": 360}])
def test_service_absolute_deadline_includes_first_guard(service, cli, monkeypatch, execution_window):
    selected = save(service)
    clock = [10.0]
    monkeypatch.setattr(c, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(c, "status", lambda: {"status": "ready", "version": "fixture"})
    monkeypatch.setattr(c, "find_codex", lambda: "/fake/codex")
    monkeypatch.setattr(c, "_text_only_catalog", lambda *_: "{}")
    monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **k: pytest.fail("expired guard spawned"))
    with pytest.raises(ToolError) as error:
        service.complete("system", "user", provider_id=selected["id"], _deadline=11.0,
                         before_send=lambda: clock.__setitem__(0, 11.0), **execution_window)
    assert error.value.codex_reason == "timeout"
    assert service.usage()["requests"] == 0
