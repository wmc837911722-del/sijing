from __future__ import annotations

import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from businessmap.core import CoreError, Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "data")
    yield value
    value.close()


def test_demo_is_real_markdown_and_deterministic_database(store):
    workspace = store.workspace()
    assert workspace["demo"] is True
    assert workspace["stats"]["notes"] >= 6
    assert workspace["stats"]["links"] >= 8
    assert "示例" in store.read_note("业务/退款规则.md")["content"]
    assert (store.vault / "业务/退款规则.md").is_file()
    with sqlite3.connect(store.data_dir / "demo.sqlite") as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 8
        assert db.execute("SELECT sum(amount) FROM refunds WHERE status='completed'").fetchone()[0] == 190


def test_external_edit_refreshes_search_graph_and_rejects_stale_save(store):
    original = store.create_note("退款边界")
    other = store.create_note("售后处理")
    linked = store.save_note(original["path"], "# 退款边界\n\n[[售后处理]]\n", original["revision"])
    assert linked["links"][0]["path"] == other["path"]
    assert store.read_note(other["path"])["backlinks"][0]["path"] == original["path"]
    target = store.vault / original["path"]
    target.write_text("# 外部修改\n\n独立编辑器修改的内容。\n", encoding="utf-8")
    assert store.notes("独立编辑器")[0]["path"] == original["path"]
    assert store.read_note(other["path"])["backlinks"] == []
    with pytest.raises(CoreError) as error:
        store.save_note(original["path"], "覆盖外部修改", linked["revision"])
    assert error.value.status_code == 409
    assert "独立编辑器" in target.read_text(encoding="utf-8")


def test_existing_note_requires_revision_and_new_note_refuses_stale_revision(store):
    note = store.create_note("版本保护")
    with pytest.raises(CoreError, match="已变化"):
        store.save_note(note["path"], "overwrite")
    (store.vault / note["path"]).unlink()
    with pytest.raises(CoreError) as error:
        store.save_note(note["path"], "resurrect", note["revision"])
    assert error.value.status_code == 409


@pytest.mark.parametrize("path", ["../outside.md", "/absolute.md", "C:/escape.md", "nested/../../escape.md", ".git/config.md", "note.txt", "nested\\escape.md"])
def test_vault_paths_cannot_escape(store, path):
    with pytest.raises(CoreError) as error:
        store.save_note(path, "escape")
    assert error.value.status_code == 400


def test_wikilinks_in_code_are_not_edges_and_ambiguous_names_are_not_guessed(store):
    a = store.create_note("规则", "one")
    b = store.create_note("规则", "two")
    source = store.save_note("链接.md", "# 链接\n\n[[规则]]\n\n`[[one/规则]]`\n\n```md\n[[two/规则]]\n```\n")
    assert source["links"] == []
    source = store.save_note(source["path"], source["content"] + "\n[[one/规则|唯一目标]]\n", source["revision"])
    assert [item["path"] for item in source["links"]] == [a["path"]]
    assert store.read_note(b["path"])["backlinks"] == []


def test_open_vault_does_not_seed_user_content_and_repositories_are_scoped(store, tmp_path):
    vault = tmp_path / "user-vault"
    vault.mkdir()
    (vault / "自己的知识.md").write_text("# 自己的知识\n", encoding="utf-8")
    result = store.open_vault(str(vault))
    assert result["demo"] is False
    assert result["stats"]["notes"] == 1
    assert (vault / "自己的知识.md").read_text(encoding="utf-8") == "# 自己的知识\n"
    assert not (vault / "开始阅读.md").exists()


def test_interrupted_tasks_are_persisted_as_failed_on_service_start(tmp_path):
    first = Store(tmp_path / "restart")
    task = first.create_task("test", {})
    first.close()
    second = Store(tmp_path / "restart")
    try:
        second.start()
        restored = next(item for item in second.tasks() if item["id"] == task["id"])
        assert restored["status"] == "failed"
        assert "中断" in restored["message"]
    finally:
        second.close()


def test_mcp_style_store_construction_does_not_fail_desktop_tasks(tmp_path):
    desktop = Store(tmp_path / "running-desktop")
    task = desktop.create_task("analysis")
    other = Store(tmp_path / "running-desktop")
    try:
        assert next(item for item in other.tasks() if item["id"] == task["id"])["status"] == "queued"
    finally:
        other.close()
        desktop.close()


def test_existing_mcp_store_follows_desktop_vault_switch(tmp_path):
    desktop = Store(tmp_path / "shared-vault")
    mcp = Store(tmp_path / "shared-vault")
    try:
        assert mcp.workspace()["demo"] is True
        selected = tmp_path / "new-vault"
        selected.mkdir()
        (selected / "用户业务.md").write_text("# 新空间业务\n\n只在新空间出现。\n", encoding="utf-8")
        desktop.open_vault(str(selected))
        assert mcp.vault == selected.resolve()
        assert [item["path"] for item in mcp.notes()] == ["用户业务.md"]
        assert mcp.read_note("用户业务.md")["title"] == "新空间业务"
        with pytest.raises(CoreError) as error:
            mcp.read_note("开始阅读.md")
        assert error.value.status_code == 404
    finally:
        mcp.close()
        desktop.close()


def test_config_updates_are_atomic_across_independent_store_instances(tmp_path):
    left = Store(tmp_path / "shared")
    right = Store(tmp_path / "shared")
    try:
        def increment(store):
            for _ in range(20):
                store.config_update("counter", lambda value: value + 1, 0)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(increment, value) for value in (left, right)]
            for future in futures:
                future.result()
        assert left.config_get("counter") == 40
    finally:
        left.close()
        right.close()


def git(directory: Path, *args: str):
    result = subprocess.run(["git", "-c", "user.name=BusinessMap Test", "-c", "user.email=test@example.invalid", *args],
                            cwd=directory, check=True, capture_output=True, timeout=10)
    return result.stdout.decode().strip()


def await_task(store, task_id=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        tasks = store.tasks()
        task = next((task for task in tasks if task_id is None or task["id"] == task_id), None)
        if task and task["status"] in {"completed", "failed"}:
            assert task["status"] == "completed", task
            return task
        time.sleep(0.025)
    pytest.fail("repository task did not finish")


def test_git_changes_publish_real_index_preserve_manual_edits_and_notify(store, tmp_path):
    repo_path = tmp_path / "source"
    repo_path.mkdir()
    git(repo_path, "init")
    (repo_path / "orders.py").write_text("def calculate_total():\n    return 100\n", encoding="utf-8")
    (repo_path / "refund.py").write_text("def refund():\n    return True\n", encoding="utf-8")
    (repo_path / ".env").write_text("PASSWORD=do-not-send\n", encoding="utf-8")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-m", "initial")
    notifications = []
    store.on_repository_synced = lambda repo_id, paths: notifications.append((repo_id, paths))
    repo = store.add_repository(str(repo_path))
    await_task(store)
    assert store.get_repository(repo["id"])["status"] == "ready"
    context = store.repository_context(repo["id"])
    assert {item["path"] for item in context["files"]} == {"orders.py", "refund.py"}
    assert notifications[-1] == (repo["id"], ["orders.py", "refund.py"])
    code_notes = [store.read_note(item["path"]) for item in store.notes() if item["type"] == "code"]
    order_note = next(note for note in code_notes if note["title"] == "orders.py")
    refund_note = next(note for note in code_notes if note["title"] == "refund.py")
    modified = store.save_note(order_note["path"], order_note["content"] + "\n人工解释必须保留。\n", order_note["revision"])
    (repo_path / "orders.py").write_text("def calculate_total():\n    return 200\n", encoding="utf-8")
    git(repo_path, "mv", "refund.py", "returns.py")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-m", "change and rename")
    result = store.sync_repository(repo["id"])
    await_task(store, result["task_id"])
    assert store.read_note(order_note["path"])["content"] == modified["content"]
    assert not (store.vault / refund_note["path"]).exists()
    assert any("更新建议" in item["path"] for item in store.notes())
    assert any(item["title"] == "returns.py" for item in store.notes())
    assert notifications[-1] == (repo["id"], ["orders.py", "refund.py", "returns.py"])
    before = len(notifications)
    (repo_path / "orders.py").write_text("uncommitted change\n", encoding="utf-8")
    await_task(store, store.sync_repository(repo["id"])["task_id"])
    assert len(notifications) == before
    assert "return 200" in store.repository_context(repo["id"])["files"][0]["content"]
    task_count, activity_count = len(store.tasks()), len(store.activities())
    for _ in range(3):
        store._poll_repository(repo["id"])
    assert len(store.tasks()) == task_count
    assert len(store.activities()) == activity_count
    assert len(notifications) == before


def test_api_authorizes_every_core_and_tool_route_without_cors(tmp_path):
    from fastapi.testclient import TestClient
    from businessmap.app import create_app

    token = "test-session-token-with-32-characters"
    app = create_app(tmp_path / "http-service", token)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        routes_checked = 0
        for path, operations in app.openapi()["paths"].items():
            if not path.startswith("/api"):
                continue
            for key in ("repo_id", "provider_id", "datasource_id", "candidate_id"):
                path = path.replace("{" + key + "}", "missing")
            for method in operations:
                for headers in ({}, {"Authorization": "Bearer incorrect-token"}):
                    response = client.request(method, path, json={}, headers=headers)
                    assert response.status_code == 401, (method, path, response.text)
                routes_checked += 1
        assert routes_checked >= 25
        preflight = client.options("/api/workspace", headers={
            "Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        })
        assert preflight.status_code == 401
        assert "access-control-allow-origin" not in preflight.headers
        client.headers["Authorization"] = "Bearer " + token
        workspace = client.get("/api/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["stats"]["notes"] >= 6
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/tasks").json() == {"tasks": []}
        assert client.get("/api/activity").status_code == 200
        note = client.post("/api/notes", json={"title": "API笔记"}).json()
        assert client.get("/api/note", params={"path": note["path"]}).json()["revision"] == note["revision"]
        conflict = client.put("/api/note", json={"path": note["path"], "content": "overwrite"})
        assert conflict.status_code == 409
        saved = client.put("/api/note", json={"path": note["path"], "content": "# API笔记\n\n[[开始阅读]]\n", "revision": note["revision"]})
        assert saved.status_code == 200
        assert saved.json()["links"]
        assert client.get("/api/graph").status_code == 200
        datasources = client.get("/api/datasources").json()["datasources"]
        demo = next(item for item in datasources if item.get("demo"))
        result = client.post("/api/query", json={"datasource_id": demo["id"], "sql": "SELECT COUNT(*) AS order_count FROM orders"})
        assert result.status_code == 200, result.text
        assert result.json()["rows"] == [[8]]
        report = client.post("/api/reports", json={"result_id": result.json()["id"], "title": "示例订单统计", "chart_type": "table"})
        assert report.status_code == 200, report.text
        assert report.json()["result"]["rows"] == [[8]]


def test_task_keeps_final_progress_explanation(store):
    task = store.create_task("analysis")
    store.run_task(task["id"], lambda task_id: store.update_task(task_id, 90, "已完成有界分析；1条人工冲突待处理"))
    completed = await_task(store, task["id"])
    assert completed["message"] == "已完成有界分析；1条人工冲突待处理"
