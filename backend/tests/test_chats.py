from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from businessmap.app import create_app
from businessmap.core import Store
from businessmap.providers import ProviderService, ToolError
from businessmap.query import QueryService
from businessmap.tools_api import ToolsService


TOKEN = "chat-fixture-token-at-least-sixteen"


def tool_reply(*calls):
    normalized = [{"id": uuid.uuid4().hex, "name": name, "arguments": args} for name, args in calls]
    wire = [{"id": call["id"], "type": "function", "function": {
        "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}} for call in normalized]
    return {"text": "", "tool_calls": normalized, "usage": {}, "provider": "fixture",
            "assistant_message": {"role": "assistant", "content": "", "tool_calls": wire}}


def finished(text="请补充业务范围。", citations=None):
    return tool_reply(("finish_answer", {"answer": text, "citations": citations or [],
                                        "kind": "answer" if citations else "clarification"}))


@pytest.fixture
def app_client(tmp_path):
    app = create_app(tmp_path / "data", TOKEN)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield app.state.store, client


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    yield instance
    instance.close()


def provider(service, name="Fixture"):
    return service.save({"name": name, "kind": "openai-compatible", "base_url": "https://example.invalid/v1", "model": name})


def repository(store, name):
    repo_id = uuid.uuid4().hex
    commit = ("a" if name == "first" else "b") * 40
    store._write_repo({"id": repo_id, "name": name, "vault_path": str(store.vault), "commit": commit,
                       "status": "ready", "auto_sync": False, "url": "fixture", "path": "fixture"})
    path = f"业务地图/{repo_id}/rules.md"
    saved = store.save_note(path, f"# 订单业务规则 {name}\n订单规则 {name}-only-evidence\n")
    store.config_set(f"ai_analysis:{repo_id}", {"notes": {"rules": {
        "path": path, "commit": commit, "revision": saved["revision"], "stale": False, "conflict": False}}})
    store.save_note(f"代码索引/{repo_id}/fixture.md", f"# 订单代码 {name}\nsource_commit: older-unchanged-blob\n")
    store.save_note(f"代码索引/{repo_id}/版本-{commit[:12]}.md", "# 订单当前版本\n")
    store.save_note(f"代码索引/{repo_id}/版本-old.md", "# 订单历史版本不可用于当前依据\n")
    store.save_note(f"代码索引/{repo_id}/更新建议/proposal.md", "# 订单未确认建议不可用于当前依据\n")
    return repo_id


def test_authenticated_empty_session_auto_title_and_reopen(app_client):
    store, client = app_client
    assert client.get("/api/chats", headers={"Authorization": ""}).status_code == 401
    session = client.post("/api/chats", json={}).json()
    assert session["messages"] == [] and session["title"] == "新对话"
    assert client.get("/api/chats").json()["chats"][0]["message_count"] == 0
    response = client.post(f"/api/chats/{session['id']}/messages", json={"message": "订单 的状态规则", "mode": "knowledge"})
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["title"] == "订单 的状态规则" and not session["pending"]
    assert [message["role"] for message in session["messages"]] == ["user", "assistant"]
    assert "未配置模型" in session["messages"][1]["content"]
    assert client.get(f"/api/chats/{session['id']}").json() == session
    reopened = Store(store.data_dir)
    try:
        assert ToolsService(reopened).chats.get(session["id"]) == session
    finally:
        reopened.close()
    named = client.post("/api/chats", json={"title": "人工标题"}).json()
    named = client.post(f"/api/chats/{named['id']}/messages", json={"message": "another", "mode": "knowledge"}).json()
    assert named["title"] == "人工标题"


def test_selected_provider_reaches_wire_without_changing_global(app_client, monkeypatch):
    store, client = app_client
    service = ProviderService(store)
    first = provider(service, "first-model")
    selected = provider(service, "selected-model")
    service.activate(first["id"])
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": finished()["assistant_message"],
                                                      "finish_reason": "tool_calls"}], "usage": {}})
    original = httpx.Client
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    session = client.post("/api/chats", json={}).json()
    result = client.post(f"/api/chats/{session['id']}/messages", json={
        "message": "订单规则", "mode": "knowledge", "provider_id": selected["id"]}).json()
    assert seen[0]["model"] == "selected-model"
    assert result["messages"][-1]["provider"] == "selected-model"
    assert store.config_get("active_provider_id") == first["id"]
    result = client.post(f"/api/chats/{session['id']}/messages", json={
        "message": "继续", "mode": "knowledge", "provider_id": None}).json()
    assert seen[1]["model"] == "first-model" and result["provider_id"] is None


def test_repository_selection_scopes_real_notes_and_code_index(store, monkeypatch):
    tools = ToolsService(store)
    first, second = repository(store, "first"), repository(store, "second")
    store.save_note("手工/订单.md", "# 订单\nmanual-outside-repository\n")
    provider(tools.providers)
    tools.bind_model(first, tools.providers.get()["id"])
    tools.bind_model(second, tools.providers.get()["id"])
    captured = []
    def turn(system, messages, tools, **kwargs):
        receipts = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        if not receipts:
            return tool_reply(("knowledge_search", {"query": "订单", "limit": 20}))
        if "notes" in receipts[-1]:
            return tool_reply(*[("knowledge_read", {"path": note["path"]}) for note in receipts[-1]["notes"]])
        captured.append(json.dumps(receipts, ensure_ascii=False))
        return finished("有依据的答复", [note["path"] for note in receipts if "content" in note])
    monkeypatch.setattr(tools.providers, "tool_turn", turn)
    response = tools.chat("订单", repository_id=first)
    assert len(response["sources"]) == 1  # Code inventories are locators, not business evidence.
    assert all(f"/{first}/" in source["path"] for source in response["sources"])
    assert "first-only-evidence" in captured[0] and "older-unchanged-blob" not in captured[0]
    assert "second-only-evidence" not in captured[0] and "manual-outside-repository" not in captured[0]
    assert "未确认建议" not in captured[0] and "历史版本" not in captured[0]
    assert "older-unchanged-blob" in tools.search("fixture", repository_id=first)[0]["content"]
    unrestricted = tools.chat("订单")
    assert any(f"/{second}/" in source["path"] for source in unrestricted["sources"])
    assert any(source["path"] == "手工/订单.md" for source in unrestricted["sources"])


def test_bounded_server_history_isolated_between_chats_and_selections(store, monkeypatch):
    tools = ToolsService(store)
    provider(tools.providers)
    repo_id = repository(store, "first")
    tools.bind_model(repo_id, tools.providers.get()["id"])
    observed = []
    def turn(system, messages, tools, **kwargs):
        observed.append(messages[:-1])
        return finished("reply:" + messages[-1]["content"])
    monkeypatch.setattr(tools.providers, "tool_turn", turn)
    first, second = tools.chats.create(), tools.chats.create()
    for index in range(7):
        tools.chats.send(first["id"], f"问题 {index}" + "x" * 2100)
    assert len(observed[-1]) == 8 and all(len(m["content"]) <= 2000 for m in observed[-1])
    assert "问题 2" in observed[-1][0]["content"] and "问题 0" not in json.dumps(observed[-1], ensure_ascii=False)
    tools.chats.send(second["id"], "另一会话")
    assert observed[-1] == []
    tools.chats.send(first["id"], "订单", repository_id=repo_id)
    assert observed[-1] == []
    tools.chats.send(first["id"], "继续订单", repository_id=repo_id)
    assert len(observed[-1]) == 2 and observed[-1][0]["content"] == "订单"
    tools.chats.send(first["id"], "当前知识库", repository_id=None)
    assert observed[-1] == []


def test_invalid_ids_and_client_supplied_history_rejected_without_message(app_client):
    store, client = app_client
    session = client.post("/api/chats", json={}).json()
    endpoint = f"/api/chats/{session['id']}/messages"
    for field in ("provider_id", "repository_id", "datasource_id"):
        result = client.post(endpoint, json={"message": "订单", "mode": "knowledge", field: "missing"})
        assert result.status_code == 404, result.text
    assert client.post(endpoint, json={"message": "订单", "mode": "knowledge", "history": []}).status_code == 422
    assert client.get("/api/chats/invalid-id").status_code == 404
    assert client.get(f"/api/chats/{session['id']}").json()["messages"] == []
    old_vault = str(store.vault)
    store.open_vault(str(store.data_dir / "new-vault"))
    assert client.get("/api/chats").json() == {"chats": []}
    assert client.get(f"/api/chats/{session['id']}").status_code == 404
    store.open_vault(old_vault)
    assert client.get(f"/api/chats/{session['id']}").status_code == 200


def test_failure_keeps_question_readable_error_and_clears_pending(app_client, monkeypatch):
    store, client = app_client
    provider(ProviderService(store))
    def fail(*args, **kwargs):
        raise ToolError("模型服务超时，请稍后重试。", 502)
    monkeypatch.setattr(ProviderService, "tool_turn", fail)
    session = client.post("/api/chats", json={}).json()
    response = client.post(f"/api/chats/{session['id']}/messages", json={"message": "保留问题", "mode": "knowledge"})
    assert response.status_code == 200
    saved = response.json()
    assert saved["messages"][0]["content"] == "保留问题"
    assert saved["messages"][-1]["error"] == "模型服务超时，请稍后重试。" and not saved["pending"]
    assert client.get(f"/api/chats/{session['id']}").json() == saved
    def unexpected(*args, **kwargs):
        raise RuntimeError("secret-database-password")
    monkeypatch.setattr(ProviderService, "tool_turn", unexpected)
    response = client.post(f"/api/chats/{session['id']}/messages", json={"message": "再次", "mode": "knowledge"})
    assert "secret-database-password" not in response.text
    assert response.json()["messages"][-1]["error"]


def test_concurrent_same_chat_rejected_across_stores_but_other_chats_work(store, monkeypatch):
    primary = ToolsService(store)
    provider(primary.providers)
    second_store = Store(store.data_dir)
    secondary = ToolsService(second_store)
    chat, other = primary.chats.create(), primary.chats.create()
    started, finish = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        started.set()
        assert finish.wait(5), "test did not release provider"
        return finished("first finished")
    monkeypatch.setattr(primary.providers, "tool_turn", blocked)
    monkeypatch.setattr(secondary.providers, "tool_turn", lambda *a, **k: finished("other finished"))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(primary.chats.send, chat["id"], "first pending")
            try:
                assert started.wait(5)
                pending = secondary.chats.get(chat["id"])
                assert pending["pending"] and len(pending["messages"]) == 1
                with pytest.raises(ToolError) as error:
                    secondary.chats.send(chat["id"], "must never append")
                assert error.value.status_code == 409
                result = secondary.chats.send(other["id"], "other question")
                assert result["messages"][-1]["content"].endswith("other finished")
            finally:
                finish.set()
            completed = future.result(timeout=5)
            assert len(completed["messages"]) == 2 and not completed["pending"]
    finally:
        second_store.close()


def test_crashed_pending_request_recovers_on_reload(store):
    tools = ToolsService(store)
    session = tools.chats.create()
    raw = store.config_get(f"chat:{session['id']}")
    raw["pending"] = True
    raw["pending_activity"] = [
        {"id": "read-1", "tool": "knowledge_read", "label": "读取笔记内容", "status": "complete", "path": "业务/规则.md"},
        {"id": "read-2", "tool": "knowledge_links", "label": "追查双向关联", "status": "running"},
    ]
    raw["messages"].append({"id": uuid.uuid4().hex, "role": "user", "content": "crash question"})
    tools.chats._save(raw)  # Process died after committing the user message; no live OS lock remains.
    summaries = tools.chats.list()
    assert not summaries[0]["pending"]
    result = tools.chats.get(session["id"])
    assert result["messages"][0]["content"] == "crash question"
    assert "中断" in result["messages"][-1]["error"]
    assert result["pending_activity"] == [] and not result["cancel_requested"]
    assert [item["status"] for item in result["messages"][-1]["activities"]] == ["complete", "error"]


def test_progress_is_live_persisted_and_cancel_survives_concurrent_receipts(store, monkeypatch):
    primary = ToolsService(store)
    second_store = Store(store.data_dir)
    secondary = ToolsService(second_store)
    session = primary.chats.create()
    started, finish = threading.Event(), threading.Event()
    seen_cancel = []

    def progressing(*args, progress, cancelled, **kwargs):
        assert not cancelled()
        progress({"id": "1", "tool": "knowledge_search", "label": "搜索业务笔记", "status": "complete", "raw": "private context"})
        progress({"id": "2", "tool": "knowledge_read", "label": "读取笔记内容", "status": "running"})
        started.set()
        assert finish.wait(5)
        progress({"id": "2", "tool": "knowledge_read", "label": "读取笔记内容", "status": "complete", "path": "业务/规则.md"})
        seen_cancel.append(cancelled())
        raise ToolError("已停止本次回答。", 409)

    monkeypatch.setattr(primary, "chat", progressing)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(primary.chats.send, session["id"], "逐步查证")
            try:
                assert started.wait(5)
                live = secondary.chats.get(session["id"])
                assert live["pending"] and len(live["messages"]) == 1
                assert [item["status"] for item in live["pending_activity"]] == ["complete", "running"]
                assert "private context" not in json.dumps(live)
                cancelled = secondary.chats.cancel(session["id"])
                assert cancelled["pending"] and cancelled["cancel_requested"]
            finally:
                finish.set()
            completed = future.result(timeout=5)
        assert seen_cancel == [True]
        assert not completed["pending"] and not completed["cancel_requested"] and completed["pending_activity"] == []
        assistant = completed["messages"][-1]
        assert assistant["completion"] == "limited" and assistant["error"] == "已停止本次回答。"
        assert len(assistant["activities"]) == 2 and assistant["activities"][1]["path"] == "业务/规则.md"
        assert secondary.chats.get(session["id"])["messages"][-1] == assistant
        assert secondary.chats.cancel(session["id"]) == completed  # Completed conversations remain unchanged.

        def next_reply(*args, progress, cancelled, **kwargs):
            assert not cancelled()
            progress({"id": "next", "tool": "finish_answer", "label": "整理回答依据", "status": "complete"})
            return {"answer": "请补充业务范围。", "completion": "complete"}

        monkeypatch.setattr(primary, "chat", next_reply)
        next_session = primary.chats.send(session["id"], "继续")
        assert len(next_session["messages"]) == 4
        assert [item["id"] for item in next_session["messages"][-1]["activities"]] == ["next"]
        assert len(next_session["messages"][1]["activities"]) == 2
    finally:
        second_store.close()


def test_activity_receipts_survive_unexpected_error_without_private_fields(store, monkeypatch):
    tools = ToolsService(store)

    def fail(*args, progress, **kwargs):
        progress({"id": "read", "tool": "knowledge_read", "label": "读取笔记内容", "status": "running", "arguments": {"password": "secret"}})
        raise RuntimeError("private provider payload")

    monkeypatch.setattr(tools, "chat", fail)
    completed = tools.chats.send(tools.chats.create()["id"], "保留步骤")
    assistant = completed["messages"][-1]
    assert assistant["activities"][0]["status"] == "error"
    assert "private provider payload" not in json.dumps(completed)
    assert "secret" not in json.dumps(completed)


def test_cancel_endpoint_is_authenticated_and_vault_scoped(app_client):
    store, client = app_client
    session = client.post("/api/chats", json={}).json()
    endpoint = f"/api/chats/{session['id']}/cancel"
    assert client.post(endpoint, headers={"Authorization": ""}).status_code == 401
    assert client.post(endpoint).json() == session
    store.open_vault(str(store.data_dir / "another-vault"))
    assert client.post(endpoint).status_code == 404


def test_kernel_lock_blocks_another_process(store):
    tools = ToolsService(store)
    chat = tools.chats.create()
    script = """
import sys
from businessmap.core import Store
from businessmap.tools_api import ToolsService
from businessmap.providers import ToolError
store = Store(sys.argv[1])
try:
    try:
        ToolsService(store).chats.send(sys.argv[2], 'second process')
    except ToolError as exc:
        print(exc.status_code)
    else:
        print('unexpected success')
finally:
    store.close()
"""
    with tools.chats._lock(chat["id"]):
        result = subprocess.run([sys.executable, "-c", script, str(store.data_dir), chat["id"]],
                                capture_output=True, text=True, timeout=15, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "409"
    assert tools.chats.get(chat["id"])["messages"] == []


def test_retention_bound_and_vault_switch_during_reply(store, monkeypatch):
    tools = ToolsService(store)
    provider(tools.providers)
    monkeypatch.setattr("businessmap.chat_sessions.MAX_MESSAGES", 6)
    monkeypatch.setattr(tools.providers, "tool_turn", lambda *a, **k: finished("reply"))
    chat = tools.chats.create()
    for index in range(4):
        result = tools.chats.send(chat["id"], f"question-{index}")
    assert len(result["messages"]) == 6 and result["earlier_messages_removed"] == 2
    assert result["messages"][0]["content"] == "question-1"
    old_vault = str(store.vault)
    def switch_vault(*args, **kwargs):
        store.open_vault(str(store.data_dir / "changed-vault"))
        return finished("must not become a successful answer")
    monkeypatch.setattr(tools.providers, "tool_turn", switch_vault)
    result = tools.chats.send(chat["id"], "pending on old vault")
    assert "切换" in result["messages"][-1]["error"]
    assert tools.chats.list() == []
    store.open_vault(old_vault)
    assert tools.chats.get(chat["id"])["messages"][-1]["error"]


def test_data_session_retains_real_result_and_knowledge_never_queries(app_client, monkeypatch):
    store, client = app_client
    provider(ProviderService(store))
    store.save_note("业务/查询口径.md", "# 订单查询口径\norders 每行代表一笔订单；统计所有行得到订单总数。\n")
    def turn(self, system, messages, tools, **kwargs):
        names = {tool["function"]["name"] for tool in tools}
        if "query_readonly" not in names:
            return finished("知识答复，需要补充业务范围。")
        if not any(item["role"] == "tool" for item in messages):
            return tool_reply(("knowledge_read", {"path": "业务/查询口径.md"}), ("datasource_schema", {}))
        return tool_reply(("query_readonly", {"sql": "SELECT COUNT(*) AS total FROM orders", "evidence_paths": ["业务/查询口径.md"]}))
    monkeypatch.setattr(ProviderService, "tool_turn", turn)
    session = client.post("/api/chats", json={}).json()
    endpoint = f"/api/chats/{session['id']}/messages"
    with monkeypatch.context() as guarded:
        guarded.setattr(QueryService, "schema", lambda *_: (_ for _ in ()).throw(AssertionError("knowledge used schema")))
        guarded.setattr(QueryService, "execute", lambda *_: (_ for _ in ()).throw(AssertionError("knowledge used DB")))
        knowledge = client.post(endpoint, json={"message": "订单规则", "mode": "knowledge", "datasource_id": "demo"}).json()
        assert "error" not in knowledge["messages"][-1] and "result" not in knowledge["messages"][-1]
    data = client.post(endpoint, json={"message": "订单行数", "mode": "data", "datasource_id": "demo"}).json()
    result = data["messages"][-1]["result"]
    assert result["rows"] == [[8]] and result["row_count"] == 1
    saved = client.get(f"/api/chats/{session['id']}").json()
    assert saved["messages"][-1]["result"] == result
    report = client.post("/api/reports", json={"result_id": result["id"], "title": "chat result"})
    assert report.json()["result"] == result
