from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from businessmap.core import CoreError, DEMO_NOTES, Store


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


LEGACY_ORDER_NOTE = """---
title: 订单流程
type: process
tags: [示例, 订单]
demo: true
---
# 订单流程

> 示例业务规则，不代表真实项目。

订单的示例状态为 `pending`（待支付）、`paid`（已支付）、`completed`（已完成）、`cancelled`（已取消）。

创建订单 → 等待支付 → [[业务/支付与成交额]] → 完成订单。已取消订单不计入有效销售。

订单保存在 [[数据/订单表]]。退款流程见 [[业务/退款规则]]。
"""


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_original_default_demo_is_displayed_without_rewriting_files(tmp_path, line_ending):
    data = tmp_path / "installed"
    setup = Store(data)
    name = "业务/订单流程.md"
    default = setup.vault
    old = LEGACY_ORDER_NOTE.replace("\n", line_ending).encode()
    target = default / name
    target.write_bytes(old)
    edited = default / "业务/退款规则.md"
    edited.write_bytes(b"# My refund notes\n")
    deleted = default / "开始阅读.md"
    deleted.unlink()
    personal = tmp_path / "personal"
    setup.open_vault(str(personal))
    (personal / name).parent.mkdir()
    (personal / name).write_bytes(old)
    setup.close()

    reopened = Store(data)
    try:
        assert (personal / name).read_bytes() == old and (default / name).read_bytes() == old
        with pytest.raises(CoreError) as hidden:
            reopened.read_note(name)
        assert hidden.value.status_code == 404
        target.chmod(0o444)
        before = target.stat()
        assert reopened.open_vault(str(default))["demo"]
        note = reopened.read_note(name)
        assert note["content"] == DEMO_NOTES[name]
        assert note["revision"] == hashlib.sha256(old).hexdigest()
        assert [result["path"] for result in reopened.notes("未完成的订单可以取消")] == [name]
        assert not reopened.notes("pending")
        assert any(edge["source"] == name and edge["target"] == "业务/退款规则.md"
                   for edge in reopened.graph()["edges"])
        assert target.read_bytes() == old
        after = target.stat()
        assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (before.st_ino, before.st_mtime_ns, before.st_mode)
        assert edited.read_bytes() == b"# My refund notes\n"
        assert reopened.read_note("业务/退款规则.md")["content"] == "# My refund notes\n"
        assert not deleted.exists() and (personal / name).read_bytes() == old
    finally:
        target.chmod(0o600)
        reopened.close()


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_edit_displayed_demo_uses_original_disk_revision(tmp_path, line_ending):
    data = tmp_path / "demo"
    initial = Store(data)
    name = "业务/订单流程.md"
    target = initial.vault / name
    old = LEGACY_ORDER_NOTE.replace("\n", line_ending).encode()
    target.write_bytes(old)
    initial.close()
    store = Store(data)
    try:
        original = store.read_note(name)
        content = original["content"] + "\n会员订单可以提前预约。\n"
        edited = store.save_note(name, content, original["revision"])
        assert target.read_bytes() == content.encode() and edited["content"] == content
        assert edited["revision"] == hashlib.sha256(content.encode()).hexdigest()
        assert [result["path"] for result in store.notes("提前预约")] == [name]
    finally:
        store.close()


@pytest.mark.parametrize("external", [b"# Customer policy\n", b"\xef\xbb\xbf" + LEGACY_ORDER_NOTE.encode()])
def test_edit_displayed_demo_preserves_external_changes(store, external):
    name = "业务/订单流程.md"
    target = store.vault / name
    target.write_bytes(LEGACY_ORDER_NOTE.encode())
    opened = store.read_note(name)
    target.write_bytes(external)
    with pytest.raises(CoreError) as conflict:
        store.save_note(name, opened["content"] + "\n申请须确认。", opened["revision"])
    assert conflict.value.status_code == 409 and target.read_bytes() == external


def test_internal_runtime_can_defer_initial_index_refresh_without_config_changes(tmp_path):
    data = tmp_path / "deferred-index"
    first = Store(data)
    target = first.vault / "新增业务.md"
    target.write_text("# 新增业务\n\n外部编辑保留。\n", encoding="utf-8")
    version = first.version
    first.close()
    with sqlite3.connect(data / "workspace.sqlite") as db:
        before = list(db.execute("SELECT key,value FROM config ORDER BY key"))
    deferred = Store(data, refresh_index_on_init=False)
    try:
        with sqlite3.connect(data / "workspace.sqlite") as db:
            assert list(db.execute("SELECT key,value FROM config ORDER BY key")) == before
        assert deferred.version == version
        deferred.refresh()
        assert deferred.version == version + 1
        assert deferred.read_note("新增业务.md")["content"] == "# 新增业务\n\n外部编辑保留。\n"
    finally:
        deferred.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_default_store_still_indexes_existing_markdown_on_construction(tmp_path, explicit):
    data = tmp_path / "default-index"
    first = Store(data)
    version = first.version
    first.close()
    second = Store(data, **({"refresh_index_on_init": True} if explicit else {}))
    try:
        assert second.version == version + 1
        assert second.read_note("业务/退款规则.md")["title"]
    finally:
        second.close()


def test_deferred_refresh_keeps_first_time_seed_and_demo_database_initialization(tmp_path):
    value = Store(tmp_path / "first-deferred", refresh_index_on_init=False)
    try:
        assert value.config_get("vault_path") == str(value.vault)
        assert (value.vault / "业务/退款规则.md").is_file()
        with sqlite3.connect(value.data_dir / "demo.sqlite") as db:
            assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 8
        value.refresh()
        assert "示例" in value.read_note("业务/退款规则.md")["content"]
    finally:
        value.close()


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
    internal = "# 链接\n\n[[规则]]\n\n`[[one/规则]]`\n\n```md\n[[two/规则]]\n```\n"
    assert store._links("链接.md", internal) == []
    with pytest.raises(CoreError) as error:
        store.save_note("链接.md", internal)
    assert error.value.status_code == 422
    source = store.save_note("链接.md", "# 链接\n[[规则]]\n[[one/规则|唯一目标]]\n")
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
    index = store.config_get(f"repository_file_index:{repo['id']}")
    assert {item['path'] for item in index['files']} == {'orders.py', 'refund.py'}
    assert not any(note['type'] == 'code' for note in store.notes())
    manual = store.save_note('业务/人工说明.md', '# 人工说明\n人工解释必须保留。\n')
    original_paths = {path.relative_to(store.vault).as_posix() for path in store.vault.rglob('*.md')}
    (repo_path / "orders.py").write_text("def calculate_total():\n    return 200\n", encoding="utf-8")
    git(repo_path, "mv", "refund.py", "returns.py")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-m", "change and rename")
    result = store.sync_repository(repo["id"])
    await_task(store, result["task_id"])
    assert store.read_note(manual['path'])['revision'] == manual['revision']
    index = store.config_get(f"repository_file_index:{repo['id']}")
    assert {item['path'] for item in index['files']} == {'orders.py', 'returns.py'}
    assert {path.relative_to(store.vault).as_posix() for path in store.vault.rglob('*.md')} == original_paths
    assert notifications[-1] == (repo["id"], ["orders.py", "refund.py", "returns.py"])
    before = len(notifications)
    (repo_path / "orders.py").write_text("uncommitted change\n", encoding="utf-8")
    await_task(store, store.sync_repository(repo["id"])["task_id"])
    assert len(notifications) == before
    context = store.repository_context(repo["id"])
    assert "return 200" in next(item["content"] for item in context["files"] if item["path"] == "orders.py")
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
