from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from businessmap.core import CoreError, Store, revision


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    instance.open_vault(str(tmp_path / "vault"))
    yield instance
    instance.close()


def legacy_read(store, path):
    content = (store.vault / path).read_text(encoding="utf-8-sig")
    return {"path": path, "content": content, "revision": revision(content)}


def legacy_write(store, path, content, *_prior_revision):
    # Simulate bytes left by an older build/human editor. Public save/read APIs
    # correctly refuse technical documents; migration still owns private CAS.
    target = store.vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    store.refresh()
    return legacy_read(store, path)


def test_business_graph_excludes_source_material_and_does_not_dangle_edges(store):
    # Including raw/source nodes turns a code-file inventory into a business map.
    legacy_write(store, "代码索引/repo/版本-abc.md", "# 代码索引\n\n[[业务/规则]]\n")
    legacy_write(store, "00-Inbox/代码资料/repo/source.md", "---\ntype: code\n---\n# Source\n")
    legacy_write(store, "资料.md", "---\ntype: raw\n---\n# Raw\n")
    legacy_write(store, "业务/规则.md", "# 规则\n\n[[业务/流程]] [[资料]]\n")
    legacy_write(store, "业务/流程.md", "# 流程\n")
    graph = store.graph()
    assert {node["id"] for node in graph["nodes"]} == {"业务/规则.md", "业务/流程.md"}
    assert graph["edges"] == [{"source": "业务/规则.md", "target": "业务/流程.md"}]
    assert store.workspace()["stats"] == {
        "notes": 2, "business_notes": 2, "source_documents": 0,
        "links": 1, "repositories": 0, "projects": 0,
    }
    for path in ("代码索引/repo/版本-abc.md", "00-Inbox/代码资料/repo/source.md", "资料.md"):
        assert (store.vault / path).exists()
        with pytest.raises(CoreError) as error:
            store.read_note(path)
        assert error.value.status_code == 404


def legacy_content(store, *, edited=False, conflict=False):
    repo = {"id": "repo", "name": "Arena", "vault_path": str(store.vault), "commit": "abc"}
    with store._db() as db:
        db.execute("INSERT INTO repositories VALUES (?,?)", ("repo", json.dumps(repo)))
    source = legacy_write(store, "代码索引/repo/source.md", "---\ntype: code\n---\n# Service.java\n")
    business = legacy_write(store, "业务地图/repo/overview.md", "---\ntype: business\n---\n# 地图\n\n[[代码索引/repo/source|代码]] [[业务地图/repo/rules]]\n")
    rules = legacy_write(store, "业务地图/repo/rules.md", "---\ntype: business\n---\n# 规则\n\n[[业务地图/repo/overview]]\n")
    legacy_write(store, "代码索引/repo/版本-abc.md", "# 无已知 revision 的旧清单\n")
    legacy_write(store, "人工笔记.md", "# 人工说明\n\n[[业务地图/repo/overview]]\n")
    with store._db() as db:
        db.execute("INSERT INTO managed_notes VALUES (?,?,?,?,?)", ("repo", "Service.java", source["path"], "blob", source["revision"]))
    state = {"vault_path": str(store.vault), "last_analyzed_commit": "abc", "auto_enabled": False,
             "notes": {"overview": {"path": business["path"], "revision": business["revision"], "commit": "abc"},
                       "rules": {"path": rules["path"], "revision": rules["revision"], "commit": "abc"}}}
    store.config_set("ai_analysis:repo", state)
    if edited:
        legacy_write(store, source["path"], source["content"] + "人工补充\n", source["revision"])
    if conflict:
        legacy_write(store, "02-Areas/业务地图/repo/业务地图.md", "# 人工先创建的地图\n")
    return source, business, rules


def test_layout_migration_moves_only_owned_content_and_keeps_database_revisions_consistent(store):
    # Moving bytes without updating managed metadata loses future edit protection.
    source, business, rules = legacy_content(store)
    human = legacy_read(store, "人工笔记.md")["content"]
    result = store.migrate_content_layout()
    assert result["moved"] == 3
    new_source = legacy_read(store, "00-Inbox/代码资料/repo/source.md")
    new_map = legacy_read(store, "02-Areas/业务地图/repo/业务地图.md")
    new_rules = legacy_read(store, "01-Wiki/业务知识/repo/rules.md")
    assert new_source["content"] == source["content"]
    assert "[[00-Inbox/代码资料/repo/source|代码]]" in new_map["content"]
    assert "[[01-Wiki/业务知识/repo/rules]]" in new_map["content"]
    assert "[[02-Areas/业务地图/repo/业务地图]]" in new_rules["content"]
    assert legacy_read(store, "人工笔记.md")["content"] == human
    assert legacy_read(store, "代码索引/repo/版本-abc.md")
    for old in (source, business, rules):
        assert not (store.vault / old["path"]).exists()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT note_path,revision FROM managed_notes").fetchone() == (new_source["path"], new_source["revision"])
    state = store.config_get("ai_analysis:repo")
    assert state["notes"]["overview"]["path"] == new_map["path"]
    assert state["notes"]["overview"]["revision"] == new_map["revision"]
    assert state["notes"]["rules"]["revision"] == new_rules["revision"]
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["status"] == "completed"
    assert (Path(result["manifest_path"]).parent / "workspace.sqlite").exists()
    assert store.migrate_content_layout()["moved"] == 0


def test_layout_migration_preserves_human_changes_and_existing_destinations(store):
    source, business, rules = legacy_content(store, edited=True, conflict=True)
    edited = legacy_read(store, source["path"])["content"]
    result = store.migrate_content_layout()
    assert result["moved"] == 1
    assert {item["path"] for item in result["skipped"]} == {source["path"], business["path"]}
    assert legacy_read(store, source["path"])["content"] == edited
    assert legacy_read(store, business["path"])["content"] == business["content"]
    assert legacy_read(store, "02-Areas/业务地图/repo/业务地图.md")["content"] == "# 人工先创建的地图\n"
    assert "[[业务地图/repo/overview]]" in legacy_read(store, "01-Wiki/业务知识/repo/rules.md")["content"]
    assert not (store.vault / "00-Inbox/代码资料/repo/source.md").exists()


@pytest.mark.parametrize("phase", ["before_metadata", "during_cleanup"])
def test_layout_migration_resumes_after_interrupted_file_and_database_phases(store, monkeypatch, phase):
    from businessmap import layout_migration
    legacy_content(store)
    if phase == "before_metadata":
        original = layout_migration._update_metadata
        def interrupted(*args):
            raise OSError("simulated power failure before metadata")
        monkeypatch.setattr(layout_migration, "_update_metadata", interrupted)
    else:
        original = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path.name == "overview.md":
                raise OSError("simulated power failure during cleanup")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(OSError, match="simulated power failure"):
        store.migrate_content_layout()
    assert (store.vault / "02-Areas/业务地图/repo/业务地图.md").exists()
    if phase == "before_metadata":
        monkeypatch.setattr(layout_migration, "_update_metadata", original)
    else:
        monkeypatch.setattr(Path, "unlink", original)
    result = store.migrate_content_layout()
    assert result["moved"] == 3
    assert len(list((store.data_dir / "layout-migrations").glob("*/*/manifest.json"))) == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT note_path,revision FROM managed_notes").fetchone() == (
            "00-Inbox/代码资料/repo/source.md", legacy_read(store, "00-Inbox/代码资料/repo/source.md")["revision"])
    assert not (store.vault / "代码索引/repo/source.md").exists()
    assert not (store.vault / "业务地图/repo/overview.md").exists()
    assert not (store.vault / "业务地图/repo/rules.md").exists()


def test_layout_migration_rejects_shared_vault_switch_before_publishing(store, tmp_path, monkeypatch):
    legacy_content(store)
    original_vault = store.vault
    selected = tmp_path / "different-vault"
    selected.mkdir()
    other = Store(store.data_dir)
    write = Path.write_text
    switched = False
    def write_text(path, *args, **kwargs):
        nonlocal switched
        result = write(path, *args, **kwargs)
        if not switched and path.suffix == ".tmp" and "代码资料" in path.parts:
            switched = True
            other.config_set("vault_path", str(selected))
        return result
    monkeypatch.setattr(Path, "write_text", write_text)
    try:
        with pytest.raises(CoreError) as error:
            store.migrate_content_layout()
        assert error.value.status_code == 409
        assert switched
        assert (original_vault / "代码索引/repo/source.md").exists()
        assert not (original_vault / "00-Inbox/代码资料/repo/source.md").exists()
        assert not list(selected.rglob("*.md"))
        with sqlite3.connect(store.db_path) as db:
            assert db.execute("SELECT note_path FROM managed_notes").fetchone()[0] == "代码索引/repo/source.md"
    finally:
        other.close()


def test_layout_migration_does_not_recreate_a_user_deleted_original_after_interruption(store, monkeypatch):
    from businessmap import layout_migration
    legacy_content(store)
    update = layout_migration._update_metadata
    def interrupted(*args):
        raise OSError("simulated interruption")
    monkeypatch.setattr(layout_migration, "_update_metadata", interrupted)
    with pytest.raises(OSError):
        store.migrate_content_layout()
    monkeypatch.setattr(layout_migration, "_update_metadata", update)
    (store.vault / "业务地图/repo/overview.md").unlink()
    with pytest.raises(CoreError) as error:
        store.migrate_content_layout()
    assert error.value.status_code == 409
    assert store.config_get("ai_analysis:repo")["notes"]["overview"]["path"] == "业务地图/repo/overview.md"


def test_layout_migration_keeps_literal_code_examples_unchanged(store):
    _, business, _ = legacy_content(store)
    examples = "\n`[[业务地图/repo/rules]]`\n\n```md\n[[业务地图/repo/rules]]\n```\n"
    saved = legacy_write(store, business["path"], business["content"] + examples, business["revision"])
    state = store.config_get("ai_analysis:repo")
    state["notes"]["overview"]["revision"] = saved["revision"]
    store.config_set("ai_analysis:repo", state)
    store.migrate_content_layout()
    migrated = legacy_read(store, "02-Areas/业务地图/repo/业务地图.md")["content"]
    assert "[[01-Wiki/业务知识/repo/rules]]" in migrated
    assert examples in migrated


def test_layout_migration_does_not_restore_a_user_deleted_committed_target(store, monkeypatch):
    legacy_content(store)
    unlink = Path.unlink
    def interrupted(path, *args, **kwargs):
        if path.name == "overview.md":
            raise OSError("simulated interruption during cleanup")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(OSError):
        store.migrate_content_layout()
    monkeypatch.setattr(Path, "unlink", unlink)
    (store.vault / "02-Areas/业务地图/repo/业务地图.md").unlink()
    with pytest.raises(CoreError) as error:
        store.migrate_content_layout()
    assert error.value.status_code == 409
    assert not (store.vault / "02-Areas/业务地图/repo/业务地图.md").exists()
    assert (store.vault / "业务地图/repo/overview.md").exists()


def test_layout_migration_aborts_when_a_target_is_deleted_during_the_same_attempt(store, monkeypatch):
    source, business, rules = legacy_content(store)
    write = store._write_note
    deleted = False
    def delete_published_map(path, *args, **kwargs):
        nonlocal deleted
        saved = write(path, *args, **kwargs)
        if path == "02-Areas/业务地图/repo/业务地图.md":
            (store.vault / path).unlink()
            deleted = True
        return saved
    monkeypatch.setattr(store, "_write_note", delete_published_map)
    with pytest.raises(CoreError) as error:
        store.migrate_content_layout()
    assert error.value.status_code == 409
    assert deleted
    for original in (source, business, rules):
        assert legacy_read(store, original["path"])["content"] == original["content"]
    assert store.config_get("ai_analysis:repo")["notes"]["overview"]["path"] == business["path"]
    assert (store.data_dir / "layout-migrations").exists()


@pytest.mark.parametrize("evidence", [
    "````md\n[[业务地图/repo/rules]]\n`````\n",
    "~~~md\n[[业务地图/repo/rules]]\n~~~~\n",
    "````md\n[[业务地图/repo/rules]]\n",
    "~~~md\n[[业务地图/repo/rules]]\n",
])
def test_layout_migration_preserves_long_closing_or_unclosed_fenced_evidence(store, evidence):
    _, business, _ = legacy_content(store)
    content = business["content"] + "\n" + evidence
    saved = legacy_write(store, business["path"], content, business["revision"])
    state = store.config_get("ai_analysis:repo")
    state["notes"]["overview"]["revision"] = saved["revision"]
    store.config_set("ai_analysis:repo", state)
    store.migrate_content_layout()
    migrated = legacy_read(store, "02-Areas/业务地图/repo/业务地图.md")["content"]
    assert "[[01-Wiki/业务知识/repo/rules]]" in migrated
    assert evidence in migrated
