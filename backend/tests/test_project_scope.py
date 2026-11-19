"""Project knowledge keeps each repository's authorization and version boundary."""
import pytest
import yaml

from businessmap import content_layout
from businessmap.core import Store
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService


@pytest.fixture
def project_store(tmp_path, monkeypatch):
    store = Store(tmp_path)
    for rid in ("frontend", "backend", "unrelated"):
        store._write_repo({"id": rid, "name": rid, "vault_path": str(store.vault),
                           "commit": "a" * 40, "status": "ready", "connection": {"auth_type": "none"}})
    project = {"id": "arena", "name": "Arena", "repository_ids": ["frontend", "backend"], "revision": "v1", "vault_path": str(store.vault)}
    store.config_set("business_projects", [project])
    monkeypatch.setattr(store, "get_project", lambda _id: project, raising=False)
    monkeypatch.setattr(store, "project_for_repository", lambda rid: project if rid in project["repository_ids"] else None, raising=False)
    yield store, ToolsService(store), project
    store.close()


def project_note(store):
    from businessmap.business_content import render_public_note
    path = "02-Areas/业务地图/天梯赛事/业务地图.md"
    note = store.save_note(path, render_public_note("业务地图", "# 业务地图\n公开业务主题。", content_role="business_map"))
    sources = [{"repository_id": rid, "commit": "a" * 40} for rid in ("frontend", "backend")]
    store.config_set("project_analysis:arena", {"vault_path": str(store.vault), "project_revision": "v1",
        "notes": {"overview": {"path": path, "revision": note["revision"], "source_repositories": sources,
                               "stale": False, "conflict": False}}})
    return store.read_note(path)


def test_project_paths_do_not_impersonate_repository_ids():
    assert hasattr(content_layout, "project_business_paths"), "Project outputs need a separate namespace"
    paths = content_layout.project_business_paths("arena")
    assert paths["overview"] == "02-Areas/业务地图/项目/arena/业务地图.md"
    assert content_layout.repository_id_from_path(paths["overview"]) is None
    assert content_layout.project_id_from_path(paths["overview"]) == "arena"


def test_project_note_authorization_checks_all_original_repositories(project_store, monkeypatch):
    store, service, _ = project_store
    note = project_note(store)
    assert service.note_repositories(note) == {"frontend", "backend"}
    def authorization(rid, _selected=None):
        if rid == "frontend":
            raise ToolError("not bound", 403)
        return {"id": "model"}
    monkeypatch.setattr(service, "repository_provider", authorization)
    assert service.note_allowed(note, {"id": "model"}) is False


def test_project_note_becomes_ineligible_when_any_member_commit_changes(project_store):
    store, service, _ = project_store
    note = project_note(store)
    assert service._eligible(note)
    store.update_repository("frontend", {"commit": "b" * 40})
    assert service._eligible(note) is False


def test_project_note_requires_managed_state_even_after_move(project_store):
    store, service, _ = project_store
    note = project_note(store)
    store.config_set("project_analysis:arena", {})
    assert service._eligible(note) is False
    moved = {**note, "path": "01-Wiki/manual-moved.md"}
    assert service._eligible(moved) is False


def test_grouped_repositories_cannot_generate_standalone_maps(project_store):
    _, service, _ = project_store
    with pytest.raises(ToolError, match="项目"):
        service.analyze("backend")
    with pytest.raises(ToolError, match="项目"):
        service.set_auto_analysis("backend", True)


def test_project_history_scope_changes_with_any_member_version(project_store):
    store, service, project = project_store
    before = service.history_scope("knowledge", None, None, project_id="arena")
    store.update_repository("frontend", {"commit": "b" * 40})
    assert service.history_scope("knowledge", None, None, project_id="arena") != before
    changed = service.history_scope("knowledge", None, None, project_id="arena")
    project["revision"] = "v2"
    assert service.history_scope("knowledge", None, None, project_id="arena") != changed


def test_project_and_repository_scopes_are_mutually_exclusive(project_store):
    _, service, _ = project_store
    with pytest.raises(ToolError, match="同时"):
        service.validate_selections(repository_id="backend", project_id="arena")
