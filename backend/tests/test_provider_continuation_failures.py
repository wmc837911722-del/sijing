"""Failed model changes retain a directly resumable, untouched old checkpoint."""
import pytest
from collections import Counter

from businessmap.providers import ToolError
from test_full_analysis import full_project, model


def paused_checkpoint(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def fail_third(payload, count):
        if count == 3:
            raise ToolError("fixture stop", 502)
    model(monkeypatch, service, fail_third)
    full.analyze(project["id"])
    old = full._load(project["id"])
    assert old["coverage"]["completed_chunks"] == 2
    original = full.project_analysis.snapshot(project["id"])
    with service.store._db() as db:
        old_json = db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0]
        old_chunks = db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall()
    return full, service, project, old, original, old_json, old_chunks


@pytest.mark.parametrize("stage", ["plan", "adopt"])
def test_failed_provider_change_restores_binding_and_can_retry(full_project, monkeypatch, stage):
    full, service, project, old, original, old_json, old_chunks = paused_checkpoint(full_project, monkeypatch)
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    with monkeypatch.context() as failure:
        if stage == "plan":
            real_plan = full._plan
            def failed_plan(*args, **kwargs):
                result = real_plan(*args, **kwargs)
                service.store.config_set("full_analysis_control:" + result["run_id"], {"paused": True})
                raise ToolError("fixture migration failed", 500)
            failure.setattr(full, "_plan", failed_plan)
        else:
            real_adopt = full._adopt_receipts
            def failed_adopt(snapshot, run, migration=None):
                real_adopt(snapshot, run, migration)
                if migration:
                    raise ToolError("fixture migration failed", 500)
            failure.setattr(full, "_adopt_receipts", failed_adopt)
        with pytest.raises(ToolError, match="fixture migration failed"):
            full.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert full.project_analysis.snapshot(project["id"])["policies"] == original["policies"]
    assert full._load(project["id"])["run_id"] == old["run_id"]
    with service.store._db() as db:
        assert db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0] == old_json
        assert db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall() == old_chunks
        assert db.execute("SELECT id FROM full_analysis_runs WHERE project_id=?", (project["id"],)).fetchall() == [(old["run_id"],)]
        assert db.execute("SELECT key FROM config WHERE key LIKE 'full_analysis_control:%'").fetchall() == []
    # The exact original source run is usable after a transient local failure.
    retried = full.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert retried["status"] == "paused" and retried["reused_chunks"] == 2


@pytest.mark.parametrize("same_destination", [False, True])
def test_failed_provider_change_does_not_overwrite_newer_concurrent_consent(full_project, monkeypatch, same_destination):
    full, service, project, old, original, old_json, old_chunks = paused_checkpoint(full_project, monkeypatch)
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    newer = destination if same_destination else service.providers.save({"name": "Third", "kind": "openai-compatible", "base_url": "http://127.0.0.1:3/v1", "model": "third"})
    real_plan, concurrent = full._plan, {}
    def failed_after_new_consent(*args, **kwargs):
        real_plan(*args, **kwargs)
        full.project_analysis.bind_model(project["id"], newer["id"])
        concurrent.update(full.project_analysis.snapshot(project["id"])["policies"])
        raise ToolError("fixture migration failed", 500)
    monkeypatch.setattr(full, "_plan", failed_after_new_consent)
    with pytest.raises(ToolError, match="fixture migration failed"):
        full.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert full.project_analysis.snapshot(project["id"])["policies"] == concurrent
    assert {policy["provider_id"] for policy in concurrent.values()} == {newer["id"]}
    with service.store._db() as db:
        assert db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0] == old_json
        assert db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall() == old_chunks


def test_multiple_provider_changes_describe_each_original_author(full_project, monkeypatch):
    full, service, project, old, original, *_ = paused_checkpoint(full_project, monkeypatch)
    second = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    full.continue_with_provider(project["id"], second["id"], old["run_id"])
    full.set_paused(project["id"], False)
    def fail_second(payload, count):
        if count == 2:
            raise ToolError("fixture stop", 502)
    model(monkeypatch, service, fail_second)
    second_run = full.analyze(project["id"])
    assert second_run["coverage"]["completed_chunks"] == 3
    third = service.providers.save({"name": "Third", "kind": "openai-compatible", "base_url": "http://127.0.0.1:3/v1", "model": "third"})
    full.continue_with_provider(project["id"], third["id"], second_run["run_id"])
    full.set_paused(project["id"], False)
    model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    from businessmap.content_layout import public_project_paths
    summary = service.store.read_note(public_project_paths(project["name"])["summary"])["content"]
    assert "Fixture" not in summary and "Second" not in summary and "Third" not in summary
    private_run = full._load(project["id"])
    authors = {chunk["origin_provider"]["id"] for chunk in full._chunks(private_run)}
    assert authors == {original["provider"]["id"], second["id"], third["id"]}
    assert private_run["receipt_migration"]["target_provider_name"] == "Third"


def test_receipt_validation_reuses_sanitized_blob_across_chunks(full_project, monkeypatch):
    from businessmap import full_source
    full, service, project, old, *_ = paused_checkpoint(full_project, monkeypatch)
    successful = [chunk for chunk in full._chunks(old) if chunk["status"] == "completed"]
    # The two successful pieces are distinct intervals in one real Git blob.
    assert len({segment["blob"] for chunk in successful for segment in chunk["segments"]}) == 1
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    opened, redactions, at_binding = Counter(), [], {}
    real_init, real_redact, real_bind = full_source.FullSourceInventory.__init__, full_source.redact_source, full._bind_continuation
    def counted_init(inventory, store, repository):
        opened[repository["id"]] += 1
        real_init(inventory, store, repository)
    def counted_redact(content, path=""):
        redactions.append(path)
        return real_redact(content, path)
    def measured_binding(*args):
        at_binding.update(opened=dict(opened), redactions=list(redactions))
        return real_bind(*args)
    monkeypatch.setattr(full_source.FullSourceInventory, "__init__", counted_init)
    monkeypatch.setattr(full_source, "redact_source", counted_redact)
    monkeypatch.setattr(full, "_bind_continuation", measured_binding)
    continued = full.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert continued["reused_chunks"] == 2
    assert at_binding["opened"] == {"front": 1}
    assert at_binding["redactions"] == ["src/views/orders/index.ts"]


def test_receipt_adoption_uses_linear_source_identity_work(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    full.analyze(project["id"])
    old = full._load(project["id"])
    segments = sum(len(chunk["segments"]) for chunk in full._chunks(old))
    assert segments >= 6  # Several chunks are needed to expose repeated scans.
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    real_identity, calls = full._segment_identity, []
    def counted_identity(segment):
        calls.append(None)
        return real_identity(segment)
    monkeypatch.setattr(full, "_segment_identity", counted_identity)
    result = full.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert result["reused_chunks"] == old["coverage"]["completed_chunks"]
    # Count actual source identity work, not wall time or source text. A small
    # constant number of visits per segment covers planning and adoption;
    # scanning the full planned corpus for every old receipt exceeds it.
    assert len(calls) <= 10 * segments
