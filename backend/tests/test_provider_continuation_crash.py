"""A killed migration must leave its original paid checkpoint resumable."""
import subprocess
import sys

import pytest

from businessmap.full_analysis import FullProjectAnalysis
from test_full_analysis import full_project
from test_provider_continuation_failures import paused_checkpoint


CRASH_MIGRATION = r'''
import os, sys
from contextlib import contextmanager
import keyring
keyring.get_keyring = lambda: (_ for _ in ()).throw(RuntimeError("fixture locked"))
from businessmap.core import Store
from businessmap.tools_api import ToolsService
from businessmap.full_analysis import FullProjectAnalysis
store = Store(sys.argv[1])
full = FullProjectAnalysis(ToolsService(store))
full.SOURCE_BYTES = 5000
stage = sys.argv[5]
method = {"bound": "_bind_continuation", "planned": "_plan", "adopted": "_adopt_receipts", "saving": "_save", "saved": "_save", "committing": "_save"}[stage]
original = getattr(full, method)
def interrupted(*args, **kwargs):
    final = method != "_save" or bool(args[0].get("receipt_migration"))
    if stage == "saving" and final:
        os._exit(71)
    if stage == "committing" and final:
        original_db = store._db
        @contextmanager
        def interrupted_commit():
            with original_db() as db:
                db.set_trace_callback(lambda sql: os._exit(71) if sql.startswith("DELETE FROM config") and "full_analysis_migration:" in sql else None)
                yield db
        store._db = interrupted_commit
    result = original(*args, **kwargs)
    if stage == "committing" and final:
        raise AssertionError("fixture missed final transaction boundary")
    if final and (method != "_adopt_receipts" or len(args) > 2):
        os._exit(71)
    return result
setattr(full, method, interrupted)
full.continue_with_provider(sys.argv[2], sys.argv[3], sys.argv[4])
raise AssertionError("fixture did not reach crash boundary")
'''


def crash_migration(service, project, destination, old, stage):
    result = subprocess.run([sys.executable, "-c", CRASH_MIGRATION,
                             str(service.store.data_dir), project["id"], destination["id"], old["run_id"], stage],
                            capture_output=True, timeout=20)
    assert result.returncode == 71, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("stage", ["bound", "planned", "adopted", "saving", "committing"])
def test_process_death_restores_exact_old_authorization_and_checkpoint(full_project, monkeypatch, stage):
    full, service, project, old, original, old_json, old_chunks = paused_checkpoint(full_project, monkeypatch)
    service.store.save_note("01-Wiki/human.md", "# Human\n\nKeep this exact text.")
    notes = {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")}
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    crash_migration(service, project, destination, old, stage)
    restarted = FullProjectAnalysis(service)
    restarted.SOURCE_BYTES = 5000
    status = restarted.status(project["id"])
    assert not status.get("stale")
    assert status["run_id"] == old["run_id"]
    assert restarted.project_analysis.snapshot(project["id"])["policies"] == original["policies"]
    with service.store._db() as db:
        assert db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0] == old_json
        assert db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall() == old_chunks
        assert db.execute("SELECT id FROM full_analysis_runs WHERE project_id=?", (project["id"],)).fetchall() == [(old["run_id"],)]
    assert {path.relative_to(service.store.vault): path.read_bytes() for path in service.store.vault.rglob("*.md")} == notes
    retried = restarted.continue_with_provider(project["id"], destination["id"], old["run_id"])
    assert retried["status"] == "paused" and retried["reused_chunks"] == 2


def test_recovery_process_death_can_be_recovered_again(full_project, monkeypatch):
    full, service, project, old, original, old_json, old_chunks = paused_checkpoint(full_project, monkeypatch)
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    crash_migration(service, project, destination, old, "planned")
    recovery = r'''
import os, sys
from contextlib import contextmanager
from businessmap.core import Store
from businessmap.tools_api import ToolsService
from businessmap.full_analysis import FullProjectAnalysis
store = Store(sys.argv[1])
full = FullProjectAnalysis(ToolsService(store))
original_db = store._db
@contextmanager
def interrupted_recovery():
    with original_db() as db:
        db.set_trace_callback(lambda sql: os._exit(72) if sql.startswith("DELETE FROM config") and "full_analysis_migration:" in sql else None)
        yield db
store._db = interrupted_recovery
full.status(sys.argv[2])
raise AssertionError("fixture did not enter recovery transaction")
'''
    interrupted = subprocess.run([sys.executable, "-c", recovery, str(service.store.data_dir), project["id"]],
                                 capture_output=True, timeout=20)
    assert interrupted.returncode == 72, interrupted.stderr.decode(errors="replace")
    # An interrupted rollback did not partially restore consent or discard its journal.
    assert {service.store.config_get("repository_model_policy:" + rid)["provider_id"] for rid in ("front", "back")} == {destination["id"]}
    status = FullProjectAnalysis(service).status(project["id"])
    assert status["run_id"] == old["run_id"] and not status.get("stale")
    assert full.project_analysis.snapshot(project["id"])["policies"] == original["policies"]
    with service.store._db() as db:
        assert db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0] == old_json
        assert db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall() == old_chunks


@pytest.mark.parametrize("newer", ["different", "same", "revoked"])
def test_crash_recovery_never_overwrites_later_authorization(full_project, monkeypatch, newer):
    full, service, project, old, _, old_json, old_chunks = paused_checkpoint(full_project, monkeypatch)
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    crash_migration(service, project, destination, old, "planned")
    if newer == "revoked":
        service.store.config_set("repository_model_policy:front", {})
    else:
        selected = destination if newer == "same" else service.providers.save({"name": "Third", "kind": "openai-compatible", "base_url": "http://127.0.0.1:3/v1", "model": "third"})
        full.project_analysis.bind_model(project["id"], selected["id"])
    policies = {rid: service.store.config_get("repository_model_policy:" + rid) for rid in ("front", "back")}
    FullProjectAnalysis(service).status(project["id"])
    assert {rid: service.store.config_get("repository_model_policy:" + rid) for rid in ("front", "back")} == policies
    with service.store._db() as db:
        assert db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0] == old_json
        assert db.execute("SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position", (old["run_id"],)).fetchall() == old_chunks


def test_process_death_after_final_save_keeps_complete_migration(full_project, monkeypatch):
    full, service, project, old, *_ = paused_checkpoint(full_project, monkeypatch)
    destination = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    crash_migration(service, project, destination, old, "saved")
    restarted = FullProjectAnalysis(service)
    status = restarted.status(project["id"])
    assert status["status"] == "paused" and not status.get("stale")
    assert status["receipt_migration"]["source_run_id"] == old["run_id"]
    assert status["reused_chunks"] == 2
    assert {policy["provider_id"] for policy in restarted.project_analysis.snapshot(project["id"])["policies"].values()} == {destination["id"]}
