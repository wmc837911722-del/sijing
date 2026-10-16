from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from businessmap.app import create_app
from businessmap.core import Store
from businessmap.knowledge import KnowledgeNavigator
from businessmap.providers import ProviderService, ToolError, provider_destination
from businessmap.tools_api import ToolsService


@pytest.fixture
def tools(tmp_path):
    store = Store(tmp_path / "data")
    yield ToolsService(store)
    store.close()


def provider(tools, name="company"):
    return tools.providers.save({"name": name, "kind": "openai-compatible",
                                 "base_url": f"https://{name}.example.invalid/v1", "model": name})


def repository(tools, name="company"):
    repo_id = uuid.uuid4().hex
    tools.store._write_repo({"id": repo_id, "name": name, "vault_path": str(tools.store.vault),
                            "commit": "a" * 40, "status": "ready", "auto_sync": False,
                            "url": "fixture", "path": "fixture"})
    path = f"代码索引/{repo_id}/fixture.md"
    tools.store.save_note(path, f"# 订单 {name}\n{name}-private-evidence\n")
    return repo_id


def analysis_context(tools, repo_id, monkeypatch):
    def context(_):
        repo = tools.store.get_repository(repo_id)
        return {"repository": repo, "commit": repo["commit"], "truncated": False,
                "files": [{"path": "rules.py", "content": "allow_refund = True\n"}]}
    monkeypatch.setattr(tools.store, "repository_context", context)


def network(monkeypatch):
    requests = []
    def handle(request):
        payload = json.loads(request.content)
        requests.append({"url": str(request.url), **payload})
        if "代码业务分析器" in payload["messages"][0]["content"]:
            result = {"notes": [{"key": "rules", "title": "订单规则", "content": "# 订单规则\n允许退款。",
                                  "sources": [{"path": "rules.py", "line_start": 1, "line_end": 1}]}]}
            text = json.dumps(result)
        else:
            tool_names = {tool["function"]["name"] for tool in payload["tools"]}
            replies = [json.loads(message["content"]) for message in payload["messages"] if message["role"] == "tool"]
            discovery = next((reply for reply in replies if "notes" in reply), None)
            reads = [reply for reply in replies if "path" in reply and "content" in reply]
            def call(name, arguments, call_id):
                return {"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            if "datasource_schema" in tool_names and not any("schema" in reply for reply in replies):
                calls = [call("datasource_schema", {}, "schema")]
            elif discovery is None:
                calls = [call("knowledge_search", {"query": "订单", "limit": 20}, "search")]
            elif discovery["notes"] and not reads:
                calls = [call("knowledge_read", {"path": note["path"], "revision": note["revision"]}, f"read-{index}")
                         for index, note in enumerate(discovery["notes"])]
            elif "query_readonly" in tool_names and reads:
                calls = [call("query_readonly", {"sql": "SELECT COUNT(*) FROM orders",
                                                "evidence_paths": [reads[0]["path"]]}, "query")]
            else:
                calls = [call("finish_answer", {"answer": "业务回答" if reads else "需要更多业务笔记。",
                                                "citations": [reply["path"] for reply in reads],
                                                "kind": "answer" if reads else "clarification"}, "finish")]
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": calls}}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})
    original = httpx.Client
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return requests


def test_analysis_requires_explicit_binding_and_uses_bound_destination_after_default_switch(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    analysis_context(tools, repo, monkeypatch)
    requests = network(monkeypatch)
    with pytest.raises(ToolError, match="尚未绑定"):
        tools.analyze(repo)
    assert not requests
    assert tools.bind_model(repo, company["id"])["status"] == "ready"
    tools.providers.activate(other["id"])
    assert tools.analyze(repo)["notes_created"] == 1
    assert requests[0]["url"] == company["base_url"] + "/chat/completions"
    assert requests[0]["model"] == company["model"]
    assert tools.store.config_get("active_provider_id") == other["id"]


@pytest.mark.parametrize("field,value", [("base_url", "https://changed.example.invalid/v1"),
                                         ("model", "changed-model"), ("kind", "anthropic")])
def test_destination_edit_invalidates_analysis_and_scoped_chat_until_rebound(tools, monkeypatch, field, value):
    selected = provider(tools)
    repo = repository(tools)
    tools.bind_model(repo, selected["id"])
    analysis_context(tools, repo, monkeypatch)
    requests = network(monkeypatch)
    tools.providers.save({**{k: selected[k] for k in ("id", "name", "kind", "base_url", "model")}, field: value})
    assert tools.model_policy(repo)["status"] == "changed"
    with pytest.raises(ToolError, match="重新确认并绑定"):
        tools.analyze(repo)
    with pytest.raises(ToolError, match="重新确认并绑定"):
        tools.chat("订单", repository_id=repo)
    assert not requests
    assert tools.bind_model(repo, selected["id"])["status"] == "ready"


def test_provider_name_change_does_not_invalidate_binding_but_deletion_does(tools, monkeypatch):
    selected, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, selected["id"])
    tools.providers.save({**{k: selected[k] for k in ("id", "name", "kind", "base_url", "model")}, "name": "New label"})
    assert tools.model_policy(repo)["status"] == "ready"
    tools.store.config_set("providers", [p for p in tools.store.config_get("providers") if p["id"] != selected["id"]])
    tools.providers.activate(other["id"])
    requests = network(monkeypatch)
    assert tools.model_policy(repo)["status"] == "missing"
    with pytest.raises(ToolError):
        tools.analyze(repo)
    assert not requests


def test_legacy_automatic_updates_pause_without_model_call(tools, monkeypatch):
    provider(tools)
    repo = repository(tools)
    tools.store.config_set(f"ai_analysis:{repo}", {"auto_enabled": True, "last_analyzed_commit": "b" * 40})
    requests = network(monkeypatch)
    tools.on_repository_synced(repo, ["rules.py"])
    assert not tools.store.config_get(f"ai_analysis:{repo}")["auto_enabled"]
    with pytest.raises(ToolError, match="尚未绑定"):
        tools.set_auto_analysis(repo, True)
    assert not requests


def test_automatic_update_uses_binding_and_rebinding_pauses_it(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    analysis_context(tools, repo, monkeypatch)
    requests = network(monkeypatch)
    tools.analyze(repo)
    tools.providers.activate(other["id"])
    record = tools.store.get_repository(repo)
    tools.store._write_repo({**record, "commit": "b" * 40})
    monkeypatch.setattr(tools.store, "run_task", lambda task_id, fn: fn(task_id))
    tools.on_repository_synced(repo, ["rules.py"])
    assert len(requests) == 2 and all(r["model"] == company["model"] for r in requests)
    tools.bind_model(repo, other["id"])
    assert not tools.store.config_get(f"ai_analysis:{repo}")["auto_enabled"]
    tools.on_repository_synced(repo, ["rules.py"])
    assert len(requests) == 2


def test_full_vault_chat_filters_unbound_wrong_model_and_moved_derived_notes(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    secret, public, legacy = repository(tools), repository(tools, "public"), repository(tools, "legacy")
    tools.bind_model(secret, company["id"])
    tools.bind_model(public, other["id"])
    tools.store.save_note("归档/订单.md", f"---\nrepository_id: {secret}\n---\n# 订单\ncompany-moved-secret\n")
    tools.store.save_note("手工/订单.md", "# 订单\nmanual-allowed\n")
    requests = network(monkeypatch)
    reply = tools.chat("订单", provider_id=other["id"])
    assert "private-evidence" not in json.dumps(requests[0], ensure_ascii=False)
    outbound = json.dumps(requests, ensure_ascii=False)
    assert "public-private-evidence" in outbound and "manual-allowed" in outbound
    assert "company-private-evidence" not in outbound and "company-moved-secret" not in outbound
    assert "legacy-private-evidence" not in outbound
    assert secret not in outbound and legacy not in outbound
    assert "已按仓库模型授权过滤" in reply["answer"]
    assert all(f"/{secret}/" not in source["path"] and f"/{legacy}/" not in source["path"] for source in reply["sources"])
    request_count = len(requests)
    with pytest.raises(ToolError, match="未获此仓库授权"):
        tools.chat("订单", provider_id=other["id"], repository_id=secret)
    assert len(requests) == request_count
    assert any(secret in n["path"] for n in tools.search("订单"))  # Local/MCP retrieval remains local read access.


def test_data_chat_filters_repository_evidence_before_sql_planning(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    requests = network(monkeypatch)
    answer = tools.chat("订单", "data", "demo", provider_id=other["id"])
    assert answer["result"]["rows"] == [[8]]
    assert "company-private-evidence" not in json.dumps(requests, ensure_ascii=False)


def test_chat_history_cannot_cross_provider_or_changed_repository_policy(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    requests = network(monkeypatch)
    chat = tools.chats.create()
    tools.chats.send(chat["id"], "订单 first-private-user-message", provider_id=company["id"])
    tools.chats.send(chat["id"], "second-question", provider_id=company["id"])
    assert any("first-private-user-message" in m["content"] for m in requests[-1]["messages"])
    tools.chats.send(chat["id"], "public-question", provider_id=other["id"])
    assert "first-private-user-message" not in json.dumps(requests[-1], ensure_ascii=False)
    tools.chats.send(chat["id"], "company-return", provider_id=company["id"])
    tools.bind_model(repo, other["id"])
    tools.chats.send(chat["id"], "company-after-revoke", provider_id=company["id"])
    assert "company-return" not in json.dumps(requests[-1], ensure_ascii=False)


def test_legacy_unattributed_history_is_not_sent(tools, monkeypatch):
    provider(tools)
    requests = network(monkeypatch)
    tools.chat("current question", history=[{"role": "assistant", "content": "unattributed-corporate-secret"}])
    assert "unattributed-corporate-secret" not in json.dumps(requests)


def test_default_change_between_session_scope_and_call_never_mislabels_history(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    requests = network(monkeypatch)
    chat = tools.chats.create()
    original = tools.chat
    def switch_before_call(*args, **kwargs):
        tools.providers.activate(other["id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(tools, "chat", switch_before_call)
    result = tools.chats.send(chat["id"], "company-only-question")
    assert "模型或仓库授权已变化" in result["messages"][-1]["error"]
    assert not requests
    monkeypatch.setattr(tools, "chat", original)
    tools.providers.activate(company["id"])
    tools.chats.send(chat["id"], "new-question")
    assert len(requests) == 3 and "company-only-question" not in json.dumps(requests)


def test_provider_edit_during_prompt_assembly_cannot_mix_new_key_with_old_endpoint(tools, monkeypatch):
    selected = provider(tools)
    requests = network(monkeypatch)
    tools.providers.secrets.backend = None
    original = tools.providers.messages
    def change_config(*args, **kwargs):
        result = original(*args, **kwargs)
        tools.providers.save({**{k: selected[k] for k in ("id", "name", "kind", "base_url", "model")},
                              "base_url": "https://changed.example.invalid/v1", "api_key": "new-endpoint-key"})
        return result
    monkeypatch.setattr(tools.providers, "messages", change_config)
    with pytest.raises(ToolError, match="目标已变化"):
        tools.providers.complete("system", "private input", provider_id=selected["id"],
                                 expected_destination=provider_destination(selected))
    assert not requests


def test_wire_rechecks_exact_destination_before_network(tools, monkeypatch):
    selected = provider(tools)
    expected = provider_destination(selected)
    tools.providers.save({**{k: selected[k] for k in ("id", "name", "kind", "base_url", "model")},
                          "base_url": "https://changed.example.invalid/v1"})
    requests = network(monkeypatch)
    with pytest.raises(ToolError, match="目标已变化"):
        tools.providers.complete("system", "private input", provider_id=selected["id"], expected_destination=expected)
    assert not requests


def test_rebinding_during_discovery_stops_evidence_dispatch(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    requests = network(monkeypatch)
    original = KnowledgeNavigator.call
    def changed_policy(*args, **kwargs):
        result = original(*args, **kwargs)
        tools.bind_model(repo, other["id"])
        return result
    monkeypatch.setattr(KnowledgeNavigator, "call", changed_policy)
    with pytest.raises(ToolError, match="授权已变化"):
        tools.chat("订单", provider_id=company["id"])
    # The initial model request chooses search but contains no repository evidence.
    assert len(requests) == 1
    assert "company-private-evidence" not in json.dumps(requests)


def test_rebinding_during_slow_schema_read_is_checked_at_network_boundary(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    requests = network(monkeypatch)
    original = tools.queries.schema
    def change_during_schema(*args, **kwargs):
        result = original(*args, **kwargs)
        tools.bind_model(repo, other["id"])
        return result
    monkeypatch.setattr(tools.queries, "schema", change_during_schema)
    with pytest.raises(ToolError, match="授权已变化"):
        tools.chat("订单", "data", "demo", provider_id=company["id"])
    # Schema is now requested by the model. Its result must not be sent after revocation.
    assert len(requests) == 1
    assert not any(message["role"] == "tool" for message in requests[0]["messages"])


def test_rebinding_while_analysis_is_in_flight_does_not_resume_or_persist_old_result(tools, monkeypatch):
    company, other = provider(tools), provider(tools, "public")
    repo = repository(tools)
    tools.bind_model(repo, company["id"])
    analysis_context(tools, repo, monkeypatch)
    network(monkeypatch)
    original = tools.providers.complete
    def bind_after_request(*args, **kwargs):
        result = original(*args, **kwargs)
        tools.bind_model(repo, other["id"])
        return result
    monkeypatch.setattr(tools.providers, "complete", bind_after_request)
    with pytest.raises(ToolError, match="绑定已变化"):
        tools.analyze(repo)
    assert not (tools.store.vault / "业务地图" / repo).exists()
    assert not tools.store.config_get(f"ai_analysis:{repo}")["auto_enabled"]


def test_pausing_automatic_analysis_before_dispatch_prevents_network_call(tools, monkeypatch):
    selected = provider(tools)
    repo = repository(tools)
    tools.bind_model(repo, selected["id"])
    tools.store.config_set(f"ai_analysis:{repo}", {"auto_enabled": True, "last_analyzed_commit": "b" * 40})
    analysis_context(tools, repo, monkeypatch)
    requests = network(monkeypatch)
    original = tools.providers.messages
    def pause_during_prompt(*args, **kwargs):
        result = original(*args, **kwargs)
        tools.set_auto_analysis(repo, False)
        return result
    monkeypatch.setattr(tools.providers, "messages", pause_during_prompt)
    with pytest.raises(ToolError, match="自动 AI 更新已暂停"):
        tools.analyze(repo, automatic=True)
    assert not requests
    assert not (tools.store.vault / "业务地图" / repo).exists()


def test_binding_http_contract_lists_policies_without_calling_models(tmp_path):
    token = "policy-fixture-token-at-least-sixteen"
    app = create_app(tmp_path / "http", token)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        tools = ToolsService(app.state.store)
        repo = repository(tools)
        selected = provider(tools)
        assert client.get(f"/api/repositories/{repo}/model-policy").json() == {"status": "unbound"}
        invalid = client.post(f"/api/repositories/{repo}/model-policy", json={"provider_id": "missing"})
        assert invalid.status_code == 404
        result = client.post(f"/api/repositories/{repo}/model-policy", json={"provider_id": selected["id"]})
        assert result.status_code == 200 and result.json()["status"] == "ready"
        assert result.json()["provider_id"] == selected["id"] and "revision" not in result.json()
        assert client.get("/api/repository-model-policies").json()["policies"][repo] == result.json()
