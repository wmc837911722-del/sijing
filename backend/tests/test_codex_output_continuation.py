"""Completed Codex output failures are independent, with no automatic retry."""
from collections import Counter

import pytest

from businessmap.codex_provider import _codex_error
from test_full_analysis import full_project, model


@pytest.mark.parametrize("reason", ["invalid_json", "empty_response"])
def test_codex_output_failure_attempts_other_inputs_once_and_resumes_only_failure(full_project, monkeypatch, reason):
    full, service, project = full_project
    full.CONCURRENCY = 1
    plan = full.plan(project["id"])
    assert plan["coverage"]["total_chunks"] == 10
    before = {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")}
    def malformed_first(payload, count):
        if count == 1:
            raise _codex_error(reason, "fixture completed output invalid")
    sent = model(monkeypatch, service, malformed_first)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and not failed.get("publication_complete")
    assert len(sent) == 10
    assert set(Counter(payload["chunk_id"] for payload in sent).values()) == {1}
    assert failed["coverage"]["completed_chunks"] == 9
    original = {chunk["id"]: chunk for chunk in full._chunks(full._load(project["id"]))}
    assert original[sent[0]["chunk_id"]]["details"]["codex_reason"] == reason
    assert {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")} == before
    resumed_calls = model(monkeypatch, service)
    resumed = full.analyze(project["id"])
    assert resumed["status"] == "completed"
    assert [payload["chunk_id"] for payload in resumed_calls] == [sent[0]["chunk_id"]]
    for chunk in full._chunks(full._load(project["id"])):
        if original[chunk["id"]]["status"] == "completed":
            assert chunk == original[chunk["id"]]


@pytest.mark.parametrize("reason", ["invalid_event", "unexpected_tool", "communication", "login", "input_limit"])
def test_codex_non_output_failures_still_stop_new_dispatch(full_project, monkeypatch, reason):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def unavailable(payload, count):
        raise _codex_error(reason, "fixture transport or authorization error", 409 if reason == "login" else 502)
    sent = model(monkeypatch, service, unavailable)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and len(sent) == 1
    assert failed["coverage"]["completed_chunks"] == 0
    assert not failed.get("publication_complete")
