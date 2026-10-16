from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from businessmap.business_projects import BusinessProjects
from businessmap.core import CoreError, Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "data")
    for name in ("frontend", "backend", "other"):
        store._write_repo({"id": name, "name": name, "vault_path": str(store.vault), "status": "ready",
                           "commit": "a" * 40, "path": "unused", "url": "fixture", "auto_sync": False})
    yield store
    store.close()


def test_project_is_explicit_and_repository_has_one_owner(store):
    projects = BusinessProjects(store)
    project = projects.create("天梯赛事", "业务范围", ["frontend", "backend"])
    assert project["repository_ids"] == ["frontend", "backend"]
    assert project["vault_path"] == str(store.vault) and project["revision"]
    assert store.workspace()["projects"] == [project]
    assert store.workspace()["stats"]["projects"] == 1
    with pytest.raises(CoreError, match="已属于"):
        projects.create("同名也不能自动合并", "", ["backend"])
    assert projects.create("天梯赛事", "", ["other"])["id"] != project["id"]
    assert projects.for_repository("backend")["id"] == project["id"]


def test_project_member_update_is_cas_and_atomic(store):
    projects = BusinessProjects(store)
    project = projects.create("赛事", "", ["frontend"])
    updated = projects.update(project["id"], project["revision"], repository_ids=["frontend", "backend"])
    assert updated["revision"] != project["revision"]
    with pytest.raises(CoreError) as error:
        projects.update(project["id"], project["revision"], name="旧客户端")
    assert error.value.status_code == 409
    assert projects.get(project["id"])["name"] == "赛事"
    with pytest.raises(CoreError):
        projects.update(project["id"], updated["revision"], repository_ids=["other", "missing"])
    assert projects.get(project["id"]) == updated


def test_membership_ownership_serializes_across_store_connections(store):
    second = Store(store.data_dir)
    def create(connection):
        try:
            return BusinessProjects(connection).create("业务", "", ["frontend"])
        except CoreError as error:
            return error.status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (store, second)))
        assert sum(isinstance(result, dict) for result in results) == 1
        assert results.count(409) == 1
        assert len(BusinessProjects(store).list()) == 1
    finally:
        second.close()


def test_projects_and_members_are_vault_scoped(store, tmp_path):
    projects = BusinessProjects(store)
    project = projects.create("旧业务", "", ["frontend"])
    store.open_vault(str(tmp_path / "another-vault"))
    assert not projects.list() and projects.for_repository("frontend") is None
    with pytest.raises(CoreError) as error:
        projects.get(project["id"])
    assert error.value.status_code == 404
    with pytest.raises(CoreError):
        projects.create("越界", "", ["frontend"])


@pytest.mark.parametrize("members", [[], ["frontend", "frontend"], ["missing"], [1], "frontend"])
def test_invalid_members_do_not_persist_project(store, members):
    with pytest.raises(CoreError):
        BusinessProjects(store).create("赛事", "", members)
    assert not BusinessProjects(store).list()


def test_roles_are_explicit_and_revalidated_on_membership_change(store):
    projects = BusinessProjects(store)
    project = projects.create("业务", "", ["frontend", "backend"], {"frontend": "frontend", "backend": "backend"})
    updated = projects.update(project["id"], project["revision"], repository_ids=["backend"])
    assert updated["repository_roles"] == {"backend": "backend"}
    with pytest.raises(CoreError):
        projects.update(project["id"], updated["revision"], repository_roles={"frontend": "frontend"})
    assert projects.get(project["id"]) == updated


@pytest.mark.parametrize("invalid", [
    {"url": "https://valid.example.invalid/back.git", "branch": "bad branch"},
    {"url": "https://user:secret@example.invalid/back.git"},
    {"url": "https://valid.example.invalid/back.git", "connection": {"auth_type": "https", "username": "user", "secret": "bad\nsecret"}},
    {"url": "ssh://git@example.invalid/back.git", "connection": {"auth_type": "https", "username": "user"}},
])
def test_import_validates_every_endpoint_before_creating_any_repository(store, monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(store, "add_repository", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(CoreError):
        BusinessProjects(store).import_repositories("完整业务", "", [{"url": "https://valid.example.invalid/front.git"}], [invalid])
    assert not calls and not BusinessProjects(store).list()


def test_project_import_starts_each_source_sync_and_assigns_two_roles_without_model(store, monkeypatch):
    monkeypatch.setattr(store, "run_task", lambda task_id, callback: None)
    project = BusinessProjects(store).import_repositories("完整业务", "", [{"url": "https://valid.example.invalid/front.git"}], [{"url": "https://valid.example.invalid/back.git"}])
    assert set(project["repository_roles"].values()) == {"frontend", "backend"}
    members = [store.get_repository(repository_id) for repository_id in project["repository_ids"]]
    assert len(members) == 2 and all(member["status"] == "syncing" for member in members)
    tasks = [task for task in store.tasks() if task.get("payload", {}).get("repository_id") in project["repository_ids"]]
    assert len(tasks) == 2 and all(task["status"] == "queued" for task in tasks)
    for member in members:
        assert not store.config_get(f"repository_model_policy:{member['id']}")
        assert not member["ai_auto_sync"]


def test_import_rejects_duplicate_front_back_without_mutation(store, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "add_repository", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(CoreError, match="不能重复"):
        BusinessProjects(store).import_repositories("完整业务", "", [{"url": "https://valid.example.invalid/same.git"}], [{"url": "https://valid.example.invalid/same.git"}])
    assert not calls


def test_unexpected_later_import_failure_keeps_successful_source_owned(store, monkeypatch):
    original = store.add_repository
    calls = 0
    monkeypatch.setattr(store, "run_task", lambda task_id, callback: None)
    def add(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CoreError(503, "凭据存储暂不可用")
        return original(**kwargs)
    monkeypatch.setattr(store, "add_repository", add)
    with pytest.raises(CoreError):
        BusinessProjects(store).import_repositories("保留业务", "", [{"url": "https://valid.example.invalid/front.git"}], [{"url": "https://valid.example.invalid/back.git"}])
    projects = BusinessProjects(store).list()
    assert len(projects) == 1 and len(projects[0]["repository_ids"]) == 1
    assert store.get_repository(projects[0]["repository_ids"][0])["status"] == "syncing"


def test_project_api_requires_auth_and_returns_cas_conflicts(tmp_path):
    from fastapi.testclient import TestClient
    from businessmap.app import create_app
    app = create_app(tmp_path / "api-data", "fixture-local-token-is-long")
    store = app.state.store
    for repository_id in ("front", "back"):
        store._write_repo({"id": repository_id, "name": repository_id, "vault_path": str(store.vault), "status": "ready",
                           "commit": "a" * 40, "path": "unused", "url": "fixture", "auto_sync": False})
    try:
        with TestClient(app) as client:
            assert client.get("/api/projects").status_code == 401
            headers = {"Authorization": "Bearer fixture-local-token-is-long"}
            body = {"name": "赛事", "repository_ids": ["front", "back"], "repository_roles": {"front": "frontend", "back": "backend"}}
            created = client.post("/api/projects", json=body, headers=headers)
            assert created.status_code == 200, created.text
            project = created.json()
            assert client.get("/api/projects", headers=headers).json()["projects"] == [project]
            assert client.get(f"/api/projects/{project['id']}/model-policy", headers=headers).json()["status"] == "unbound"
            assert client.get(f"/api/projects/{project['id']}/analysis-status", headers=headers).json()["status"] == "never"
            assert client.put(f"/api/projects/{project['id']}", json={"revision": "stale", "name": "wrong"}, headers=headers).status_code == 409
            updated = client.put(f"/api/projects/{project['id']}", json={"revision": project["revision"], "description": "业务"}, headers=headers)
            assert updated.status_code == 200 and updated.json()["description"] == "业务"
            assert client.post(f"/api/projects/{project['id']}/analyze", headers=headers).status_code == 409
            invalid = client.post("/api/projects/import", json={"name": "invalid", "frontend": [{"url": "https://example.invalid/front", "connection": {"api_key": "private-value"}}], "backend": [{"url": "https://example.invalid/back"}]}, headers=headers)
            assert invalid.status_code == 422 and "private-value" not in invalid.text
    finally:
        store.close()
