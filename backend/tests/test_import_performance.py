from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from businessmap.core import CoreError, Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    instance.open_vault(str(tmp_path / "vault"))
    yield instance
    instance.close()


def git(directory, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Import Test", "-c", "user.email=import@example.invalid", *args],
        cwd=directory, check=True, capture_output=True, timeout=10,
    ).stdout.decode().strip()


def repository(tmp_path, count):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    for index in range(count):
        (source / f"module_{index:04d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial tracked source")
    return source, git(source, "rev-parse", "HEAD")


def completed_task(store, task_id=None, *, expected_status="completed"):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = next((item for item in store.tasks() if task_id is None or item["id"] == task_id), None)
        if task and task["status"] in {"completed", "failed"}:
            assert task["status"] == expected_status, task.get("error", task)
            return task
        time.sleep(0.01)
    pytest.fail("Repository import did not finish")


def test_refresh_does_not_open_configuration_database_for_every_note(store, monkeypatch):
    # Resolving vault_path per note makes ordinary reads increasingly expensive.
    for index in range(120):
        (store.vault / f"note-{index}.md").write_text(f"# Note {index}\n", encoding="utf-8")
    original_connect = sqlite3.connect
    connections = 0

    def connect(*args, **kwargs):
        nonlocal connections
        connections += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert len(store.notes()) == 120
    print(f"120-note read: {connections} SQLite connections")
    assert connections <= 4, f"One note scan opened {connections} SQLite connections"


def test_scan_rejects_stale_vault_results_when_shared_configuration_changes(store, tmp_path, monkeypatch):
    original_vault = store.vault
    (original_vault / "old-only.md").write_text("# Old private knowledge\n", encoding="utf-8")
    selected = tmp_path / "different-vault"
    selected.mkdir()
    (selected / "new-only.md").write_text("# New private knowledge\n", encoding="utf-8")
    other = Store(store.data_dir)
    original_rglob = Path.rglob
    switched = False

    def rglob(path, pattern):
        nonlocal switched
        for target in original_rglob(path, pattern):
            if path == original_vault and pattern == "*.md" and not switched:
                switched = True
                other.config_set("vault_path", str(selected))
            yield target

    monkeypatch.setattr(Path, "rglob", rglob)
    try:
        with pytest.raises(CoreError) as error:
            stale_result = store.notes()
            pytest.fail(f"A changed vault returned stale notes: {stale_result}")
        assert switched
        assert error.value.status_code == 409
        assert [note["path"] for note in store.notes()] == ["new-only.md"]
    finally:
        other.close()


def test_import_indexes_every_discovered_file_privately_without_repeated_vault_scans(store, tmp_path, monkeypatch):
    source, commit = repository(tmp_path, 505)
    original_scan = Path.rglob
    vault, scans = store.vault, 0
    def rglob(path, pattern):
        nonlocal scans
        if path == vault and pattern == "*.md":
            scans += 1
            assert scans <= 3, "Import repeatedly scanned the whole vault"
        return original_scan(path, pattern)
    monkeypatch.setattr(Path, "rglob", rglob)
    repo = store.add_repository(str(source))
    completed_task(store)
    assert store.get_repository(repo["id"])["commit"] == commit
    index = store.config_get(f"repository_file_index:{repo['id']}")
    assert index["commit"] == commit and index["total_files"] == 505
    assert len(index["files"]) == 505
    blobs = {line.split("\t", 1)[1]: line.split()[2]
             for line in git(source, "ls-tree", "-r", commit).splitlines()}
    assert {item["path"]: item["blob"] for item in index["files"]} == blobs
    assert list(store.vault.rglob("*.md")) == []
    assert store.notes() == []


def test_tasks_remain_readable_during_private_index_discovery(store, tmp_path, monkeypatch):
    source, _ = repository(tmp_path, 40)
    original = store._source_files
    started, release = threading.Event(), threading.Event()
    def source_files(repo, commit):
        files = original(repo, commit)
        started.set()
        assert release.wait(5)
        return files
    monkeypatch.setattr(store, "_source_files", source_files)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            imported = executor.submit(store.add_repository, str(source))
            assert started.wait(5)
            tasks = executor.submit(store.tasks).result(timeout=1)
            assert any(task["status"] == "running" for task in tasks)
            release.set()
            imported.result(timeout=10)
        completed_task(store)
    finally:
        release.set()


def test_sync_updates_private_blobs_and_preserves_human_business_notes(store, tmp_path):
    source, _ = repository(tmp_path, 3)
    repo = store.add_repository(str(source))
    completed_task(store)
    human = store.save_note("业务/审批规则.md", "# 审批规则\n人工确认：需要审批。\n")
    (source / "module_0000.py").write_text("VALUE = 9000\n", encoding="utf-8")
    git(source, "rm", "module_0001.py", "module_0002.py")
    git(source, "add", ".")
    git(source, "commit", "-m", "Change one source and remove two")
    commit = git(source, "rev-parse", "HEAD")
    completed_task(store, store.sync_repository(repo["id"])["task_id"])
    index = store.config_get(f"repository_file_index:{repo['id']}")
    assert index["commit"] == commit
    assert [file["path"] for file in index["files"]] == ["module_0000.py"]
    assert index["files"][0]["blob"] == git(source, "rev-parse", "HEAD:module_0000.py")
    assert store.read_note(human["path"])["content"] == human["content"]
    assert [note["path"] for note in store.notes()] == [human["path"]]


def test_import_does_not_touch_legacy_index_symlinks_outside_the_vault(store, tmp_path):
    source, _ = repository(tmp_path, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    material = store.vault / "00-Inbox" / "代码资料"
    material.parent.mkdir(parents=True)
    material.symlink_to(outside, target_is_directory=True)
    repo = store.add_repository(str(source))
    completed_task(store)
    assert list(outside.rglob("*")) == []
    assert len(store.config_get(f"repository_file_index:{repo['id']}")["files"]) == 1


def test_import_aborts_private_index_if_another_process_changes_selected_vault(store, tmp_path, monkeypatch):
    source, _ = repository(tmp_path, 2)
    original_vault, selected = store.vault, tmp_path / "different-vault"
    selected.mkdir()
    other = Store(store.data_dir)
    original = store._source_files
    def source_files(repo, commit):
        files = original(repo, commit)
        other.config_set("vault_path", str(selected))
        return files
    monkeypatch.setattr(store, "_source_files", source_files)
    try:
        repo = store.add_repository(str(source))
        task = completed_task(store, expected_status="failed")
        assert "知识库已切换" in task["error"]
        assert store.config_get(f"repository_file_index:{repo['id']}") is None
        assert list(original_vault.rglob("*.md")) == list(selected.rglob("*.md")) == []
    finally:
        other.close()


def test_note_read_does_not_scan_all_notes_for_every_dangling_link(store):
    vault = store.vault
    (vault / "target.md").write_text("# Target\nBusiness rule.\n", encoding="utf-8")
    (vault / "backlink.md").write_text("# Backlink\n[[Target]]\n", encoding="utf-8")
    for index in range(600):
        (vault / f"note-{index}.md").write_text(f"# Note {index}\n", encoding="utf-8")
    raw = vault / "00-Inbox" / "代码资料" / "repo"
    raw.mkdir(parents=True)
    for version in range(6):
        (raw / f"版本-{version}.md").write_text(
            "# Historical source list\n" + "\n".join(f"[[missing/{version}-{index}]]" for index in range(400)),
            encoding="utf-8")
    store.refresh()

    class MeasuredNotes(dict):
        visited = 0

        def items(self):
            for entry in super().items():
                self.visited += 1
                yield entry

    measured = MeasuredNotes(store._cache)
    store._cache = measured
    started = time.monotonic()
    note = store.read_note("target.md")
    elapsed = time.monotonic() - started
    assert note["links"] == []
    assert [item["path"] for item in note["backlinks"]] == ["backlink.md"]
    print(f"608-note read with 2400 dangling links: {elapsed:.3f}s, {measured.visited} scanned note entries")
    assert measured.visited <= len(measured) * 3, "Each unresolved link scanned the entire vault again"


def test_link_lookup_preserves_priority_ambiguity_and_tracks_external_changes(store, tmp_path):
    vault = store.vault
    (vault / "folder").mkdir()
    (vault / "other").mkdir()
    (vault / "root.md").write_text("# Root\n", encoding="utf-8")
    (vault / "folder" / "relative.md").write_text("# Relative\n", encoding="utf-8")
    (vault / "other" / "root.md").write_text("# Root\n", encoding="utf-8")
    (vault / "folder" / "hub.md").write_text("# Hub\n[[root]] [[relative]] [[#section]] [[Alias]]\n", encoding="utf-8")
    (vault / "other" / "unique.md").write_text("# Alias\n", encoding="utf-8")
    hub = store.read_note("folder/hub.md")
    assert {note["path"] for note in hub["links"]} == {"root.md", "folder/relative.md", "folder/hub.md", "other/unique.md"}
    (vault / "other" / "duplicate.md").write_text("# Alias\n", encoding="utf-8")
    assert "other/unique.md" not in {note["path"] for note in store.read_note("folder/hub.md")["links"]}
    (vault / "other" / "duplicate.md").write_text("# Different alias\n", encoding="utf-8")
    (vault / "other" / "unique.md").rename(vault / "other" / "renamed.md")
    links = {note["path"] for note in store.read_note("folder/hub.md")["links"]}
    assert "other/renamed.md" in links and "other/unique.md" not in links
    assert store.read_note("other/renamed.md")["backlinks"][0]["path"] == "folder/hub.md"
    selected = tmp_path / "selected-vault"
    selected.mkdir()
    (selected / "hub.md").write_text("# New hub\n[[Alias]]\n", encoding="utf-8")
    other = Store(store.data_dir)
    try:
        other.open_vault(str(selected))
        assert store.read_note("hub.md")["links"] == []
        (selected / "new.md").write_text("# Alias\n", encoding="utf-8")
        assert [note["path"] for note in store.read_note("hub.md")["links"]] == ["new.md"]
    finally:
        other.close()
