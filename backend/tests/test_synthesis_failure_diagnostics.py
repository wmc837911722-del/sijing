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
            response = state["failure"]()
            if response is not None:
                return response
        return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
            "title": "订单规则"} for item in payload["items"]]}), "usage": {"input_tokens": 10, "output_tokens": 10}}
    def build(synthesis, snapshot, run, chunks):
        units = [{"kind": "finding", "text": "订单规则", "position": 27, "_fact_id": "diagnostic-fact", "_module_ids": [run["modules"][0]["id"]]}]
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


@pytest.mark.parametrize("response,expected", [
    ({"text": "RAW-PRIVATE-SOURCE-RESPONSE"}, {"reason_code": "invalid_json"}),
    ({"text": "RAW-PRIVATE-SOURCE-RESPONSE", "output_limited": True}, {"reason_code": "output_limit"}),
    ({"text": json.dumps({"RAW-PRIVATE": []})}, {"reason_code": "assignment_format"}),
    ({"text": json.dumps({"assignments": [{"fact_id": "RAW-PRIVATE"}]})}, {"reason_code": "fact_reference"}),
    ({"text": json.dumps({"assignments": []})},
     {"reason_code": "fact_missing", "fact_position": 27, "fact_kind": "finding"}),
    ({"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "internal", "classification": "technical",
        "reason": "RAW-PRIVATE-SOURCE-RESPONSE"}]})},
     {"reason_code": "internal_basis_missing", "fact_position": 27, "fact_kind": "finding"}),
    ({"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "public", "category": "rules",
        "title": "规则src/private/Rules.java"}]})},
     {"reason_code": "public_content", "fact_position": 27, "fact_kind": "finding"}),
    ({"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "public", "category": [],
        "title": "订单规则"}]})},
     {"reason_code": "public_topic", "fact_position": 27, "fact_kind": "finding"}),
])
def test_validation_failure_persists_safe_diagnostics_without_raw_output_or_source_repayment(full_project, monkeypatch, response, expected):
    full, service, project = full_project
    state, source_calls = synthesis_boundary(full, service, project, monkeypatch, lambda: response)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not result.get("publication_complete")
    assert result.get("synthesis_failure") == {"status_code": 422, **expected}
    assert full.status(project["id"])["synthesis_failure"] == result["synthesis_failure"]
    failed_run = full._load(project["id"])
    original_receipts = full._chunks(failed_run)
    assert "RAW-PRIVATE" not in json.dumps(failed_run) and "src/private" not in json.dumps(failed_run)
    assert not list((service.store.vault / "02-Areas").rglob("*.md"))
    with service.store._db() as db:
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == 0
    paid_sources = len(source_calls)
    state["failure"] = None
    resumed = full.analyze(project["id"])
    assert resumed["status"] == "completed" and resumed["publication_complete"]
    assert len(source_calls) == paid_sources and state["requests"] == 2
    assert full._chunks(full._load(project["id"])) == original_receipts
    assert "synthesis_failure" not in full.status(project["id"])


def test_validation_diagnostics_ignore_spoofed_attributes_and_unbounded_values():
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap.full_synthesis import SynthesisValidationError
    spoofed = ToolError("RAW-PRIVATE", 422)
    spoofed.reason_code, spoofed.fact_position, spoofed.fact_kind = "internal_basis_missing", 0, "finding"
    assert FullProjectAnalysis._failure_details(spoofed) == {"exception_type": "ToolError", "status_code": 422}
    for position in (True, -1, 2 ** 63, "RAW-PRIVATE"):
        controlled = SynthesisValidationError("internal_basis_missing")
        controlled.fact_position, controlled.fact_kind = position, "RAW-PRIVATE"
        assert FullProjectAnalysis._failure_details(controlled) == {
            "exception_type": "ToolError", "status_code": 422, "reason_code": "internal_basis_missing"}
    controlled.reason_code = "RAW-PRIVATE"
    assert FullProjectAnalysis._failure_details(controlled) == {"exception_type": "ToolError", "status_code": 422}


def test_new_raw_extra_body_is_rejected_without_retry_and_source_receipts_survive(full_project, monkeypatch):
    full, service, project = full_project
    response = {"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "public", "category": "rules",
        "title": "订单规则", "text": "src/private/Rules.java"}]})}
    state, source_calls = synthesis_boundary(full, service, project, monkeypatch, lambda: response)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not result.get("publication_complete")
    assert result["synthesis_failure"] == {"status_code": 422, "reason_code": "assignment_format", "fact_position": 27, "fact_kind": "finding"}
    assert state["requests"] == 1
    receipts = full._chunks(full._load(project["id"]))
    assert receipts and all(chunk["status"] == "completed" for chunk in receipts)
    assert "src/private" not in json.dumps(full._load(project["id"]))
    with service.store._db() as db:
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == 0
    paid_sources = len(source_calls)
    state["failure"] = None
    resumed = full.analyze(project["id"])
    assert resumed["publication_complete"] and state["requests"] == 2
    assert len(source_calls) == paid_sources and full._chunks(full._load(project["id"])) == receipts


@pytest.mark.parametrize("damage", ["text", "member", "basis"])
def test_bad_exact_legacy_cache_persists_controlled_failure_without_provider_fallback(full_project, monkeypatch, damage):
    from businessmap.full_synthesis import _json
    from businessmap.l0_legacy import LegacyL0
    full, service, project = full_project
    def forbidden():
        raise AssertionError("a corrupt exact legacy checkpoint must not call provider")
    state, source_calls = synthesis_boundary(full, service, project, monkeypatch, forbidden)
    boundary = FullBusinessSynthesis.build
    def seed(synthesis, snapshot, run, chunks):
        units = [{"kind": "finding", "text": "订单规则", "position": 27, "_fact_id": "diagnostic-fact", "_module_ids": [run["modules"][0]["id"]]}]
        key = synthesis.cache_identity(snapshot, run, units, "diagnostic-fixture", legacy=True)
        result = LegacyL0._validate({"assignments": [{"fact_id": "f0", "disposition": "public", "category": "rules", "title": "订单规则"}]}, units)
        if damage == "text": result["assignments"][0]["text"] = "损坏的业务正文"
        if damage == "member": result["assignments"][0]["fact_id"] = "foreign"
        if damage == "basis": result["assignments"][0]["classification_basis"] = {}
        with service.store._db() as db:
            db.execute("INSERT OR IGNORE INTO full_analysis_synthesis VALUES (?,?,?,?)", (key, key, _json(result), "fixed-time"))
        return boundary(synthesis, snapshot, run, chunks)
    monkeypatch.setattr(FullBusinessSynthesis, "build", seed)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not result.get("publication_complete")
    assert result["synthesis_failure"] == {"status_code": 422, "reason_code": "cache_invalid"}
    assert full.status(project["id"])["synthesis_failure"] == result["synthesis_failure"]
    receipts = full._chunks(full._load(project["id"]))
    paid_sources = len(source_calls)
    with service.store._db() as db:
        before = db.execute("SELECT * FROM full_analysis_synthesis").fetchall()
    repeated = full.analyze(project["id"])
    assert repeated["synthesis_failure"] == result["synthesis_failure"]
    assert state["requests"] == 0 and len(source_calls) == paid_sources
    assert full._chunks(full._load(project["id"])) == receipts
    with service.store._db() as db:
        assert db.execute("SELECT * FROM full_analysis_synthesis").fetchall() == before
