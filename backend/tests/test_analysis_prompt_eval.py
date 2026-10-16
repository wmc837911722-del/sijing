from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

from businessmap.core import Store
from businessmap.providers import ProviderService


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/eval-analysis-prompt.py"
spec = importlib.util.spec_from_file_location("analysis_prompt_eval", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def answer(path="rule.py", start=1, end=2, content="金额必须为正。（rule.py:L1-L2）"):
    return {"notes": [{"key": "rules", "title": "金额规则", "content": content,
                       "sources": [{"path": path, "line_start": start, "line_end": end}]}]}


INPUT = {"files": [{"path": "rule.py", "content": "if amount <= 0:\n    raise ValueError()\n"}],
         "note_paths": {"rules": "业务/rules.md", "flows": "业务/flows.md"}}


@pytest.mark.parametrize("response", [answer("invented.py"), answer(start=0), answer(end=100), answer(start=True)])
def test_fabricated_sources_or_invalid_ranges_fail_contract(response):
    result = evaluation.validate_response(json.dumps(response), INPUT)
    assert result["schema_errors"] and not result["runtime_compatible"]


def test_valid_reference_does_not_prove_the_business_claim():
    # A deliberately false claim can have valid source coordinates. Never score
    # this as semantically correct just because the structural checks pass.
    response = answer(content="金额可以为负数。（rule.py:L1-L2）")
    result = evaluation.validate_response(json.dumps(response), INPUT)
    assert not result["schema_errors"] and not result["inline_citation_errors"]
    assert result["semantic_review"] == "pending_rubric_review"


def test_citation_coverage_and_uncreated_wiki_targets_are_separate_checks():
    response = answer(end=1, content="规则。（rule.py:L1-L2） 参见 [[业务/flows]]")
    result = evaluation.validate_response(json.dumps(response), INPUT)
    assert result["inline_citation_errors"] and result["schema_errors"]


def test_empty_evidence_is_explicitly_incompatible_with_current_product():
    result = evaluation.validate_response('{"notes":[]}', INPUT)
    assert not result["schema_errors"] and result["empty_evidence_response"]
    assert not result["runtime_compatible"]


def test_offline_default_never_calls_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderService, "complete", lambda *a, **k: pytest.fail("offline called model"))
    output = tmp_path / "offline.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--output", str(output)])
    assert evaluation.main() == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["mode"] == "offline_preflight" and result["provider_call_attempts"] == 0
    assert result["semantic_review"] == "pending" and not result["runs"]


def test_import_missing_inline_citations_returns_failure(tmp_path, monkeypatch):
    case = evaluation.load_cases()[0]
    source = tmp_path / "answers.json"
    source.write_text(json.dumps([{"id": case["id"], "response": answer(
        path=case["input"]["files"][0]["path"], start=1, end=1, content="No inline citation") }]), encoding="utf-8")
    output = tmp_path / "checked.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--responses", str(source), "--prompt", "v2",
        "--case-id", case["id"], "--output", str(output)])
    assert evaluation.main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["import_source"]["sha256"] == evaluation.digest(source)
    assert report["runs"][0]["checks"]["inline_citation_errors"]


def test_live_transport_excludes_rubric_and_does_not_write_app_settings(tmp_path, monkeypatch):
    store = Store(tmp_path / "user")
    try:
        selected = ProviderService(store).save({"name": "Fixture", "kind": "openai-compatible",
            "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
        case = evaluation.load_cases()[0]
        filename = case["input"]["files"][0]["path"]
        received = []
        def response(request):
            payload = json.loads(request.content)
            received.append(payload)
            assert json.loads(payload["messages"][-1]["content"]) == case["input"]
            assert "required_facts" not in request.content.decode() and "forbidden_claims" not in request.content.decode()
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(
                path=filename, start=1, end=1, content=f"可见片段。（{filename}:L1-L1）"))}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 5}})
        original = httpx.Client
        monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: original(transport=httpx.MockTransport(response), **kw))
        with sqlite3.connect(store.db_path) as db:
            before = list(db.iterdump())
        output = tmp_path / "live-contract.json"
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--live", "--data-dir", str(store.data_dir),
            "--provider-id", selected["id"], "--case-id", case["id"], "--output", str(output)])
        assert evaluation.main() == 0
        with sqlite3.connect(store.db_path) as db:
            assert list(db.iterdump()) == before
        assert len(received) == 2
        assert received[0]["messages"][-1] == received[1]["messages"][-1]
        assert received[0]["messages"][0] != received[1]["messages"][0]
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["provider_call_attempts"] == 2
        assert all(r["checks"]["semantic_review"] == "pending_rubric_review" for r in report["runs"])
    finally:
        store.close()


def test_budget_prevents_unapproved_extra_requests(tmp_path, monkeypatch):
    store = Store(tmp_path / "user")
    try:
        selected = ProviderService(store).save({"name": "Fixture", "kind": "openai-compatible",
            "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
        monkeypatch.setattr(ProviderService, "complete", lambda *a, **k: pytest.fail("budget exceeded"))
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--live", "--data-dir", str(store.data_dir),
            "--provider-id", selected["id"], "--max-calls", "1", "--output", str(tmp_path / "budget.json")])
        with pytest.raises(SystemExit) as exc:
            evaluation.main()
        assert exc.value.code == 2
    finally:
        store.close()
