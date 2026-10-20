"""Exercise the actual Codex subprocess -> full-run receipt -> status path offline."""
import json

import pytest

from test_codex_provider import cli
from test_full_analysis import full_project
from businessmap.project_analysis import ProjectAnalysis
from businessmap.providers import ToolError


@pytest.mark.parametrize("mode,reason,action", [
    ("stderr", "process_exit", "退出"), ("bad-envelope", "invalid_json", "JSON"),
    ("quota", "quota", "额度"), ("stream-error", "stream_error", "中断"),
])
def test_full_analysis_persists_codex_reason_without_raw_provider_output(full_project, cli, mode, reason, action):
    full, service, project = full_project
    full.CONCURRENCY = 1
    provider = service.providers.save({"name": "Codex fixture", "kind": "codex-local", "model": "default"})
    ProjectAnalysis(service).bind_model(project["id"], provider["id"])
    (cli / "mode").write_text(mode)
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    assert "Codex" in result["error"] and action in result["error"]
    assert "HTTP" not in result["error"]
    failed = next(chunk for chunk in full._chunks(full._load(project["id"])) if chunk["status"] == "failed")
    assert failed["details"]["codex_reason"] == reason
    assert "http_status" not in failed["details"]
    assert "raw-secret" not in json.dumps(failed, ensure_ascii=False)
    status = full.status(project["id"])
    assert status["error"] == result["error"]


def test_codex_reason_rejects_untrusted_classification_before_persistence(full_project):
    full, _, _ = full_project
    error = ToolError("raw-secret", 502)
    error.codex_reason = "raw-secret"
    assert full._failure_details(error) == {"exception_type": "ToolError", "status_code": 502}


def test_codex_configuration_failure_survives_full_run_status_readback(full_project, cli):
    full, service, project = full_project
    full.CONCURRENCY = 1
    provider = service.providers.save({"name": "Codex fixture", "kind": "codex-local", "model": "default"})
    ProjectAnalysis(service).bind_model(project["id"], provider["id"])
    (cli / "bad-catalog").touch()
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    failed = next(chunk for chunk in full._chunks(full._load(project["id"])) if chunk["status"] == "failed")
    assert failed["details"]["codex_reason"] == "config"
    status = full.status(project["id"])
    assert "Codex" in status["error"] and "配置" in status["error"]
    assert "HTTP" not in status["error"] and not (cli / "invocation.json").exists()


@pytest.mark.parametrize("reason", ["config", "login"])
def test_codex_local_state_409_is_model_failure_without_scope_change_or_publication(full_project, cli, monkeypatch, reason):
    from businessmap.codex_provider import _codex_error
    full, service, project = full_project
    full.CONCURRENCY = 1
    provider = service.providers.save({"name": "Codex fixture", "kind": "codex-local", "model": "default"})
    ProjectAnalysis(service).bind_model(project["id"], provider["id"])
    snapshot_id = full._snapshot_id(full.project_analysis.snapshot(project["id"]))
    notes_before = {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")}
    def unavailable(*args, **kwargs):
        kwargs["before_send"]()
        raise _codex_error(reason, "Controlled local model state is unavailable.", 409)
    monkeypatch.setattr(service.providers, "complete", unavailable)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and "Codex" in result["error"]
    assert full._snapshot_id(full.project_analysis.snapshot(project["id"])) == snapshot_id
    status = full.status(project["id"])
    assert status["status"] == "failed" and not status.get("stale")
    assert not status.get("publication_complete")
    failed = next(chunk for chunk in full._chunks(full._load(project["id"])) if chunk["status"] == "failed")
    assert failed["details"]["codex_reason"] == reason
    assert {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")} == notes_before
