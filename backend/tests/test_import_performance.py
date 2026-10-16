from __future__ import annotations

import hashlib
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


def test_import_writes_at_most_500_notes_without_repeated_vault_scans(store, tmp_path, monkeypatch):
    # This catches per-file save/read/backlink work over an ever-growing vault.
    source, commit = repository(tmp_path, 505)
    original_scan = Path.rglob
    vault = store.vault
    scans = 0

    def rglob(path, pattern):
        nonlocal scans
        if path == vault and pattern == "*.md":
            scans += 1
            assert scans <= 3, "Import repeatedly scanned the whole vault"
        return original_scan(path, pattern)

    monkeypatch.setattr(Path, "rglob", rglob)
    started = time.monotonic()
    repo = store.add_repository(str(source))
    completed_task(store)
    print(f"500-note import: {time.monotonic() - started:.3f}s, {scans} full-vault scans")
    assert scans <= 3, f"Import scanned the entire vault {scans} times"
    assert store.get_repository(repo["id"])["commit"] == commit
    with sqlite3.connect(store.db_path) as db:
        rows = db.execute(
            "SELECT source_path,note_path,blob,revision FROM managed_notes WHERE repository_id=? ORDER BY source_path",
            (repo["id"],),
        ).fetchall()
    assert len(rows) == 500
    assert rows[0][0] == "module_0000.py"
    assert rows[-1][0] == "module_0499.py"
    blobs = {line.split("\t", 1)[1]: line.split()[2]
             for line in git(source, "ls-tree", "-r", commit).splitlines()}
    for source_path, note_path, blob, saved_revision in rows:
        content = (store.vault / note_path).read_text(encoding="utf-8")
        assert f"source_commit: {commit}" in content
        assert f"source_path: {source_path}" in content
        assert hashlib.sha256(content.encode()).hexdigest() == saved_revision
        assert blob == blobs[source_path]
    summaries = list((store.vault / "00-Inbox" / "代码资料" / repo["id"]).glob("版本-*.md"))
    assert len(summaries) == 1
    assert "已索引 500 / 505" in summaries[0].read_text(encoding="utf-8")
    assert len(list(store.vault.rglob("*.md"))) == 501


def test_tasks_remain_readable_between_index_writes(store, tmp_path, monkeypatch):
    # Slow real atomic filesystem replacements model a busy local disk. A task
    # reader must not wait for the entire batch of otherwise independent writes.
    source, _ = repository(tmp_path, 40)
    original_replace = Path.replace
    first_write = threading.Event()

    def replace(path, target):
        if path.suffix == ".tmp" and "代码资料" in path.parts:
            first_write.set()
            time.sleep(0.04)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        imported = executor.submit(store.add_repository, str(source))
        assert first_write.wait(5), "Import did not start writing its code index"
        started = time.monotonic()
        reading = executor.submit(store.tasks)
        tasks = reading.result(timeout=1)
        print(f"Task read during slow-disk import: {time.monotonic() - started:.3f}s")
        assert any(task["status"] == "running" for task in tasks)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = store.tasks()[0]
            if task["status"] == "running" and 35 < task["progress"] < 95:
                assert "源文件，写入" in task["message"]
                break
            time.sleep(0.01)
        else:
            pytest.fail("No intermediate write progress was visible during import")
        imported.result(timeout=10)
    completed_task(store)


def test_import_keeps_human_changes_and_proposes_updates_or_marks_deleted_sources(store, tmp_path):
    source, _ = repository(tmp_path, 3)
    repo = store.add_repository(str(source))
    completed_task(store)
    notes = {note["title"]: store.read_note(note["path"])
             for note in store.notes() if note["type"] == "code"}
    edited = {}
    for name in ("module_0000.py", "module_0001.py"):
        note = notes[name]
        edited[name] = store.save_note(note["path"], note["content"] + "\n人工确认：此规则需要审批。\n", note["revision"])
    (source / "module_0000.py").write_text("VALUE = 9000\n", encoding="utf-8")
    git(source, "rm", "module_0001.py", "module_0002.py")
    git(source, "add", ".")
    git(source, "commit", "-m", "Change one source and remove two")
    commit = git(source, "rev-parse", "HEAD")
    completed_task(store, store.sync_repository(repo["id"])["task_id"])
    assert store.read_note(notes["module_0000.py"]["path"])["content"] == edited["module_0000.py"]["content"]
    removed = store.read_note(notes["module_0001.py"]["path"])["content"]
    assert removed.startswith(edited["module_0001.py"]["content"])
    assert removed.count("本笔记因包含人工修改而保留") == 1
    assert not (store.vault / notes["module_0002.py"]["path"]).exists()
    proposals = [note for note in store.notes() if "/更新建议/" in note["path"]]
    assert len(proposals) == 1
    assert proposals[0]["title"] == "module_0000.py"
    assert f"source_commit: {commit}" in store.read_note(proposals[0]["path"])["content"]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT source_path FROM managed_notes WHERE repository_id=?", (repo["id"],)).fetchall() == [("module_0000.py",)]


def test_import_cannot_follow_an_index_directory_symlink_outside_the_vault(store, tmp_path):
    source, _ = repository(tmp_path, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    material = store.vault / "00-Inbox" / "代码资料"
    material.parent.mkdir(parents=True)
    material.symlink_to(outside, target_is_directory=True)
    store.add_repository(str(source))
    task = completed_task(store, expected_status="failed")
    assert "知识库之外" in task["error"]
    assert list(outside.rglob("*.md")) == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM managed_notes").fetchone()[0] == 0


def test_import_aborts_before_publish_if_another_process_changes_the_selected_vault(store, tmp_path, monkeypatch):
    source, _ = repository(tmp_path, 2)
    original_vault = store.vault
    selected = tmp_path / "different-vault"
    selected.mkdir()
    other = Store(store.data_dir)
    original_write = Path.write_text
    switched = False

    def write_text(path, *args, **kwargs):
        nonlocal switched
        result = original_write(path, *args, **kwargs)
        if not switched and path.suffix == ".tmp" and "代码资料" in path.parts:
            switched = True
            other.config_set("vault_path", str(selected))
        return result

    monkeypatch.setattr(Path, "write_text", write_text)
    try:
        try:
            store.add_repository(str(source))
        except CoreError as error:
            # The asynchronous switch can also invalidate add_repository's final
            # public readback; it must still leave the original job safely failed.
            assert switched and error.status_code == 404
        task = completed_task(store, expected_status="failed")
        assert switched
        assert list(original_vault.rglob("*.md")) == []
        assert list(selected.rglob("*.md")) == []
        with sqlite3.connect(store.db_path) as db:
            assert db.execute("SELECT count(*) FROM managed_notes").fetchone()[0] == 0
    finally:
        other.close()
