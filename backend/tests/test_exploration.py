from __future__ import annotations

import json

import httpx
import pytest

from businessmap.core import Store
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService

HTTP_CLIENT = httpx.Client

@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    tools = ToolsService(store)
    tools.providers.save({"name": "Controlled model", "kind": "openai-compatible",
                          "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    yield tools
    store.close()


def model(monkeypatch, steps):
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert len(requests) <= len(steps), "unexpected extra model call"
        step = steps[len(requests) - 1]
        if callable(step):
            step = step(payload)
        name, args = step
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": f"call-{len(requests)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 50}})
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw))
    return requests


def finish(path, text="依据已找到"):
    return ("finish_answer", {"answer": text, "citations": [path], "kind": "answer"})


def test_model_follows_relation_then_reads_late_section(service, monkeypatch):
    service.store.save_note("流程/入口.md", "# 黑曜入口\n详见 [[规则/收费]]。")
    service.store.save_note("规则/收费.md", "# 收费\n## 背景\n" + "背景资料\n" * 2000 + "\n## 特殊豁免\n内部单免收服务费。")
    steps = [("knowledge_search", {"query": "黑曜入口"}),
             ("knowledge_links", {"path": "流程/入口.md", "direction": "outgoing"}),
             ("knowledge_outline", {"path": "规则/收费.md"}),
             ("knowledge_read", {"path": "规则/收费.md", "heading": "特殊豁免"}),
             finish("规则/收费.md", "内部单免收服务费。[收费](规则/收费.md) [假来源](不存在.md)")]
    requests = model(monkeypatch, steps)
    monkeypatch.setattr(service.queries, "schema", lambda *_: pytest.fail("knowledge mode accessed database"))
    result = service.chat("黑曜入口是否有豁免？")
    assert len(requests) == 5 and result["completion"] == "complete"
    assert "内部单免收服务费" not in json.dumps(requests[:4], ensure_ascii=False)
    assert "内部单免收服务费" in json.dumps(requests[4], ensure_ascii=False)
    assert "不存在.md" not in result["answer"]
    assert [s["path"] for s in result["sources"]] == ["规则/收费.md"]
    assert all(a["status"] == "complete" for a in result["activities"])
    assert result["usage"]["requests"] == 5 and result["usage"]["cached_tokens"] == 250
    # Actual native transcripts retain the stable prefix and previous tool receipts.
    assert all(r["tools"] == requests[0]["tools"] for r in requests)
    assert requests[3]["messages"] == requests[4]["messages"][:len(requests[3]["messages"])]


def test_search_summary_cannot_be_submitted_as_read_evidence(service, monkeypatch):
    model(monkeypatch, [("knowledge_search", {"query": "退款"}), finish("业务/退款规则.md"),
                        ("knowledge_read", {"path": "业务/退款规则.md"}), finish("业务/退款规则.md", "退款需满足规则。")])
    result = service.chat("退款规则是什么？")
    assert result["activities"][1]["status"] == "error"
    assert result["sources"][0]["revision"]


def test_data_tools_require_schema_and_read_evidence_and_execute_once(service, monkeypatch):
    requests = model(monkeypatch, [("query_readonly", {"sql": "SELECT COUNT(*) FROM orders", "evidence_paths": []}),
        ("datasource_schema", {}), ("knowledge_read", {"path": "数据/订单表.md"}),
        ("query_readonly", {"sql": "SELECT COUNT(*) FROM orders", "evidence_paths": ["数据/订单表.md"]})])
    observed = []
    original = service.queries.execute
    def execute(*args):
        observed.append(args)
        return original(*args)
    monkeypatch.setattr(service.queries, "execute", execute)
    result = service.chat("订单总行数", "data", "demo")
    assert len(observed) == 1 and result["result"]["rows"] == [[8]]
    assert result["activities"][0]["status"] == "error"
    assert "rows" not in json.dumps(requests)  # query results aren't sent back to provider


def test_data_mode_can_explain_business_without_database(service, monkeypatch):
    model(monkeypatch, [("knowledge_read", {"path": "业务/退款规则.md"}), finish("业务/退款规则.md")])
    monkeypatch.setattr(service.queries, "schema", lambda *_: pytest.fail("unnecessary schema request"))
    assert "result" not in service.chat("退款规则是什么", "data", "demo")


def test_knowledge_mode_rejects_model_database_tool(service, monkeypatch):
    model(monkeypatch, [("query_readonly", {"sql": "SELECT 1", "evidence_paths": []}),
        ("finish_answer", {"answer": "请切换只读问数模式。", "kind": "clarification", "citations": []})])
    monkeypatch.setattr(service.queries, "execute", lambda *_: pytest.fail("forbidden database access"))
    result = service.chat("查数据库")
    assert result["activities"][0]["status"] == "error" and not result["sources"]


def test_revision_change_blocks_next_transmission(service, monkeypatch):
    path = "业务/退款规则.md"
    def progress(receipt):
        if receipt["status"] == "complete" and receipt["tool"] == "knowledge_read":
            note = service.store.read_note(path)
            service.store.save_note(path, note["content"] + "\n新规则", note["revision"])
    requests = model(monkeypatch, [("knowledge_read", {"path": path})])
    with pytest.raises(ToolError, match="版本已变化"):
        service.chat("退款", progress=progress)
    assert len(requests) == 1


def test_cancel_after_model_returns_prevents_sql(service, monkeypatch):
    stopped = [False]
    def cancel(payload):
        stopped[0] = True
        return ("query_readonly", {"sql": "SELECT 1", "evidence_paths": []})
    model(monkeypatch, [cancel])
    monkeypatch.setattr(service.queries, "execute", lambda *_: pytest.fail("cancelled query executed"))
    with pytest.raises(ToolError, match="已停止"):
        service.chat("数据", "data", "demo", cancelled=lambda: stopped[0])


def test_repeated_calls_and_budget_do_not_loop_forever(service, monkeypatch):
    monkeypatch.setattr("businessmap.exploration.MAX_TURNS", 3)
    model(monkeypatch, [("knowledge_search", {"query": "不存在"})] * 3)
    result = service.chat("不存在")
    assert result["completion"] == "limited" and not result["sources"]
    assert len([a for a in result["activities"] if a["status"] == "error"]) == 2


def test_readonly_validator_still_rejects_write(service, monkeypatch):
    model(monkeypatch, [("knowledge_read", {"path": "数据/订单表.md"}), ("datasource_schema", {}),
        ("query_readonly", {"sql": "DELETE FROM orders", "evidence_paths": ["数据/订单表.md"]}),
        ("query_readonly", {"sql": "SELECT COUNT(*) FROM orders", "evidence_paths": ["数据/订单表.md"]}),
        ("finish_answer", {"answer": "不能删除数据。", "citations": [], "kind": "clarification"})])
    result = service.chat("删除订单", "data", "demo")
    assert "result" not in result and result["activities"][2]["status"] == "error"
    assert "不会自动重试" in result["activities"][3]["detail"]
    assert service.queries.execute("demo", "SELECT COUNT(*) FROM orders")["rows"] == [[8]]


def test_non_tool_model_stops_after_one_repair_opportunity(service, monkeypatch):
    count = [0]
    def plain(*args, **kwargs):
        count[0] += 1
        return {"text": "unsupported answer", "tool_calls": [], "usage": {},
                "assistant_message": {"role": "assistant", "content": "unsupported answer"}}
    monkeypatch.setattr(service.providers, "tool_turn", plain)
    result = service.chat("退款规则")
    assert count[0] == 2 and "支持工具调用的模型" in result["answer"]
    assert result["completion"] == "limited" and result["sources"] == []
