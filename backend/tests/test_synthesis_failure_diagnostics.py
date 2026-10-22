"""Synthesis failure diagnostics through the actual checkpoint/request boundary."""
import json

import pytest

from test_full_analysis import full_project, model
from businessmap.codex_provider import _codex_error
from businessmap.full_synthesis import FullBusinessSynthesis
from businessmap.providers import ToolError


def synthesis_boundary(full, service, project, monkeypatch, failure):
    source_calls = model(monkeypatch, service)
    source_complete = service.providers.complete
    fixture_build = FullBusinessSynthesis.build
    state = {"failure": failure, "requests": 0}
    def complete(system, user, **kwargs):
        payload = json.loads(user)
        if "items" not in payload:
            return source_complete(system, user, **kwargs)
        kwargs["before_send"]()
        state["requests"] += 1
        if state["failure"]:
            state["failure"]()
        return {"text": json.dumps({"domains": [{"name": "订单", "summary": "订单规则", "module_ids": ["u0"]}],
                                   "flows": [], "unknowns": []}), "usage": {"input_tokens": 10, "output_tokens": 10}}
    def build(synthesis, snapshot, run, chunks):
        units = [{"title": "订单", "summary": "订单规则", "_module_ids": [run["modules"][0]["id"]]}]
        synthesis._request(snapshot, run, units, "diagnostic-fixture", {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0})
        return fixture_build(synthesis, snapshot, run, chunks)
    monkeypatch.setattr(service.providers, "complete", complete)
    monkeypatch.setattr(FullBusinessSynthesis, "build", build)
    return state, source_calls


@pytest.mark.parametrize("reason,code,action", [("quota", 429, "额度"), ("login", 409, "登录"),
                                               ("timeout", 408, "超时"), ("config", 409, "配置")])
def test_synthesis_codex_failure_survives_run_and_status_and_clears_after_success(full_project, monkeypatch, reason, code, action):
    full, service, project = full_project
    def fail():
        raise _codex_error(reason, "RAW-PRIVATE-SOURCE-AND-ERROR", code)
    state, source_calls = synthesis_boundary(full, service, project, monkeypatch, fail)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and "Codex" in result["error"] and action in result["error"]
    assert result["coverage"]["completed_chunks"] == result["coverage"]["total_chunks"]
    assert result["synthesis_failure"]["codex_reason"] == reason
    assert not result.get("publication_complete") and not list((service.store.vault / "02-Areas").rglob("*.md"))
    status = full.status(project["id"])
    assert status["synthesis_failure"]["codex_reason"] == reason and status["error"] == result["error"]
    assert not status.get("stale") and "HTTP" not in status["error"]
    assert "RAW-PRIVATE" not in json.dumps(full._load(project["id"]))
    paid_sources = len(source_calls)
    state["failure"] = None
    resumed = full.analyze(project["id"])
    assert resumed["status"] == "completed" and resumed["publication_complete"]
    assert len(source_calls) == paid_sources and state["requests"] == 2
    assert "synthesis_failure" not in full._load(project["id"])
    assert "synthesis_failure" not in full.status(project["id"])


def test_synthesis_pause_wins_over_simultaneous_codex_failure(full_project, monkeypatch):
    full, service, project = full_project
    def fail():
        full.set_paused(project["id"], True)
        raise _codex_error("login", "RAW-PRIVATE", 409)
    synthesis_boundary(full, service, project, monkeypatch, fail)
    result = full.analyze(project["id"])
    assert result["status"] == "paused" and result["error"] is None
    status = full.status(project["id"])
    assert status["status"] == "paused" and status["error"] is None
    assert "synthesis_failure" not in full._load(project["id"])


def test_synthesis_real_snapshot_change_overrides_codex_local_409(full_project, monkeypatch):
    full, service, project = full_project
    def fail():
        service.store.update_repository("front", {"commit": "b" * 40})
        raise _codex_error("config", "RAW-PRIVATE", 409)
    synthesis_boundary(full, service, project, monkeypatch, fail)
    with pytest.raises(ToolError) as error:
        full.analyze(project["id"])
    assert error.value.status_code == 409 and not getattr(error.value, "codex_reason", None)
    status = full.status(project["id"])
    assert status["stale"] and not status.get("publication_complete")
    assert "RAW-PRIVATE" not in json.dumps(full._load(project["id"]))


def test_synthesis_real_guard_409_still_wins_when_user_pauses(full_project, monkeypatch):
    full, service, project = full_project
    def fail():
        full.set_paused(project["id"], True)
        service.store.update_repository("front", {"commit": "b" * 40})
        raise _codex_error("config", "RAW-PRIVATE", 409)
    synthesis_boundary(full, service, project, monkeypatch, fail)
    with pytest.raises(ToolError) as error:
        full.analyze(project["id"])
    assert error.value.status_code == 409 and not getattr(error.value, "codex_reason", None)
    assert full.status(project["id"])["stale"]


def test_synthesis_untrusted_reason_and_message_are_not_persisted(full_project, monkeypatch):
    full, service, project = full_project
    def fail():
        error = ToolError("RAW-PRIVATE-SOURCE", 502)
        error.codex_reason = "RAW-PRIVATE-REASON"
        raise error
    synthesis_boundary(full, service, project, monkeypatch, fail)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and "synthesis_failure" not in result
    assert "RAW-PRIVATE" not in json.dumps(full._load(project["id"]))
