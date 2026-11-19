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


def model(monkeypatch, steps, usage=None):
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
            "usage": usage(len(requests)) if usage else {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 50}})
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
    assert result["sources"][0]["path"] == "业务/退款规则.md"
    assert "revision" not in result["sources"][0]


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


@pytest.fixture
def code_repository(service, tmp_path):
    import subprocess
    source = tmp_path / "source"
    source.mkdir()
    path = source / "z-business" / "Stars.py"
    path.parent.mkdir()
    path.write_text("# settlement implementation\nLOSER_STARS = -1\nSHIELD_USED_FIRST = True\n")
    for index in range(501):
        (source / f"a{index:04d}.py").write_text("VALUE = 0\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    repo = {"id": "selected-source", "name": "Selected", "path": str(source), "url": str(source),
            "remote": False, "status": "ready", "branch": "main", "commit": commit,
            "vault_path": str(service.store.vault), "connection": {"auth_type": "none"}}
    service.store._write_repo(repo)
    service.bind_model(repo["id"], service.providers.get()["id"])
    return repo


def test_selected_repository_exposes_business_documents_without_source_tools(service, code_repository, monkeypatch):
    repo = code_repository
    path = "01-Wiki/赛事/规则/扣星条件.md"
    note = service.store.save_note(path, "# 扣星条件\n败者扣一星，保护条件适用时维持原星数。\n")
    service.store.config_set(f"ai_analysis:{repo['id']}", {"vault_path": str(service.store.vault), "notes": {
        "rules": {"path": path, "revision": note["revision"], "commit": repo["commit"], "stale": False, "conflict": False}}})
    requests = model(monkeypatch, [("knowledge_read", {"path": path}), finish(path, "败者扣一星，保护条件适用时维持原星数。")])
    result = service.chat("扣星规则如何定义？", repository_id=repo["id"])
    assert result["completion"] == "complete"
    assert result["sources"] == [{"path": path, "title": "扣星条件"}]
    assert not {"repository_read", "repository_search"} & {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert "LOSER_STARS" not in json.dumps(requests) and repo['commit'] not in json.dumps(result)

def test_source_tools_cannot_create_public_read_receipts(service, code_repository):
    from businessmap.exploration import Exploration
    exploration = Exploration(service, service.providers.get(), "knowledge", None, lambda _: True, lambda: None,
                              repository_id=code_repository["id"])
    for name, arguments in (("repository_search", {"query": "LOSER_STARS"}), ("repository_read", {"path": "z-business/Stars.py"})):
        with pytest.raises(ToolError) as error:
            exploration.execute(name, arguments)
        assert error.value.status_code == 403
    assert not exploration.reads

def test_missing_business_knowledge_can_finish_with_honest_clarification(service, code_repository, monkeypatch):
    model(monkeypatch, [("knowledge_search", {"query": "未知的业务规则"}),
                       ("finish_answer", {"answer": "现有业务知识未找到该规则，需要补充已确认说明。", "citations": [], "kind": "clarification"})])
    result = service.chat("未知规则", repository_id=code_repository["id"])
    assert result["completion"] == "complete" and not result["sources"]
    assert result["activities"][0]["status"] == "complete"
    assert "补充" in result["answer"]


def test_budget_reserves_a_bounded_finish_call_instead_of_dropping_read_evidence(service, monkeypatch):
    monkeypatch.setattr("businessmap.exploration.MAX_TOTAL_TOKENS", 15000)
    def final(payload):
        assert {tool["function"]["name"] for tool in payload["tools"]} == {"finish_answer"}
        assert "退款" in json.dumps(payload["messages"], ensure_ascii=False)
        return finish("业务/退款规则.md", "已支付订单可按退款规则处理；其他边界需补充。")
    requests = model(monkeypatch, [("knowledge_read", {"path": "业务/退款规则.md"}), final],
                     usage=lambda count: {"prompt_tokens": 10000 if count == 1 else 100, "completion_tokens": 10})
    result = service.chat("退款条件？")
    assert result["completion"] == "complete" and len(requests) == 2
    assert result["sources"][0]["path"] == "业务/退款规则.md"


def test_source_viewer_is_disabled_for_authenticated_business_api(service, code_repository):
    from fastapi.testclient import TestClient
    from businessmap.app import create_app
    app = create_app(service.store.data_dir, "source-boundary-token-long-enough")
    reference = f"source://{code_repository['id']}/{code_repository['commit']}/z-business/Stars.py#L1-L3"
    try:
        with TestClient(app) as client:
            assert client.get("/api/source-evidence", params={"reference": reference}).status_code == 401
            client.headers['Authorization'] = 'Bearer source-boundary-token-long-enough'
            result = client.get("/api/source-evidence", params={"reference": reference})
            assert result.status_code == 403 and 'Stars.py' not in result.text
    finally:
        app.state.store.close()

def test_canonical_material_folders_are_not_public_business_evidence(service, code_repository):
    from businessmap.knowledge import KnowledgeNavigator
    path = f"00-Inbox/代码资料/{code_repository['id']}/资料.md"
    service.store._write_note(path, "# 内部资料\n不可作为业务知识公开。\n")
    service.store.refresh()
    assert service.search("内部资料") == []
    assert KnowledgeNavigator(service.store).search("内部资料")["notes"] == []

@pytest.mark.parametrize("note_type", ["code", "raw"])
def test_moved_source_material_cannot_alone_prove_a_business_answer(service, monkeypatch, note_type):
    path = "整理/来源材料.md"
    service.store._write_note(path, f"---\ntype: {note_type}\n---\n# 原始材料\n尚未提炼业务事实。\n")
    service.store.refresh()
    model(monkeypatch, [("knowledge_read", {"path": path}),
                       ("finish_answer", {"answer": "缺少可查阅的业务知识，请补充已确认规则。", "citations": [], "kind": "clarification"})])
    result = service.chat("请核实业务规则")
    assert result["activities"][0]["status"] == "error"
    assert not result['sources'] and "需要补充或确认" in result["answer"]

def test_closing_does_not_drop_a_late_exception_and_then_claim_a_complete_answer(service, monkeypatch):
    monkeypatch.setattr("businessmap.exploration.MAX_TOTAL_TOKENS", 15000)
    service.store.save_note("规则/例外.md", "# 规则\n" + "ordinary condition " * 230 + "\n例外：特殊订单禁止退款。\n")
    def final(payload):
        content = json.loads(payload["messages"][-1]["content"])
        assert ("例外：特殊订单禁止退款" in json.dumps(content, ensure_ascii=False)
                or content["omitted_evidence"] > 0)
        return ("finish_answer", {"answer": "当前预算内无法完整核对例外，请补充或缩小规则范围。",
                                  "citations": [], "kind": "clarification"})
    model(monkeypatch, [("knowledge_read", {"path": "规则/例外.md"}), final],
          usage=lambda count: {"prompt_tokens": 10000 if count == 1 else 100, "completion_tokens": 10})
    result = service.chat("所有订单都能退款吗？")
    assert "补充" in result["answer"]


def test_reported_prompt_usage_leaves_budget_to_read_late_business_exception(service, monkeypatch):
    path = "规则/结算说明.md"
    service.store.save_note(path, "# Settlement rules\n" + "Ordinary settlement follows the configured business condition and retains the original amount.\n" * 240 +
                            "\n## Special protection\n保护条件成立时维持原星数，否则扣一星且不低于零。\n")
    for index in range(20):
        service.store.save_note(f"规则/结算参考{index}.md", f"# Settlement policy {index}\n" + "Ordinary settlement business condition. " * 20)
    def read_exception(payload):
        assert "knowledge_read" in {tool["function"]["name"] for tool in payload["tools"]}
        return "knowledge_read", {"path": path, "heading": "Special protection"}
    def final(payload):
        assert "保护条件成立时维持原星数" in json.dumps(payload['messages'], ensure_ascii=False)
        return finish(path, "保护条件成立时维持原星数，否则扣一星且不低于零。")
    reported_inputs = (4000, 6000, 8000, 10000, 12000, 13500)
    requests = model(monkeypatch, [("knowledge_search", {"query": "settlement", "limit": 20}),
        ("knowledge_read", {"path": path, "offset": 0, "limit": 6000}),
        ("knowledge_read", {"path": path, "offset": 6000, "limit": 6000}),
        ("knowledge_read", {"path": path, "offset": 12000, "limit": 6000}), read_exception, final],
        usage=lambda count: {"prompt_tokens": reported_inputs[count-1], "completion_tokens": 100})
    result = service.chat("完整解释结算条件与后面的保护例外。")
    assert len(requests) == 6 and result["completion"] == "complete"
    assert result['sources'] == [{"path": path, "title": "Settlement rules"}]
    assert result['usage']['input_tokens'] == sum(reported_inputs)
    fifth = requests[4]
    full_bytes = len(json.dumps({"system": fifth["messages"][0]["content"], "messages": fifth["messages"][1:], "tools": fifth["tools"]}, ensure_ascii=False).encode())
    remaining = 60000 - sum(reported_inputs[:4]) - 400
    assert full_bytes + 2300 >= remaining

@pytest.mark.parametrize("change", ["unknown", "zero", "boolean", "negative", "system", "tools", "message", "removed"])
def test_prompt_estimate_falls_back_to_full_wire_when_usage_or_append_contract_is_unknown(change):
    from businessmap.exploration import _prompt_input_bound, _prompt_snapshot

    system, messages, tools = "stable policy", [{"role": "user", "content": "original evidence"}], []
    previous = _prompt_snapshot(system, messages, tools)
    actual = {"unknown": None, "zero": 0, "boolean": True, "negative": -1}.get(change, 10)
    current_messages = messages + [{"role": "assistant", "content": "new observation"}]
    if change == "system":
        system += " changed"
    elif change == "tools":
        tools = [{"type": "function", "function": {"name": "changed"}}]
    elif change == "message":
        current_messages[0] = {"role": "user", "content": "replaced evidence"}
    elif change == "removed":
        current_messages = []
    current = _prompt_snapshot(system, current_messages, tools)
    assert _prompt_input_bound(current, (previous, actual)) == current["bytes"]


def test_technical_answers_are_rejected_without_automatic_paid_rewrite(service, monkeypatch):
    from businessmap.exploration import Exploration
    exploration = Exploration(service, service.providers.get(), "knowledge", None, lambda _: True, lambda: None)
    with pytest.raises(ToolError) as error:
        exploration.response("详见 source://private/" + "a" * 40 + "/src/RefundServiceImpl.java#L1-L2")
    assert error.value.status_code == 422
    assert not exploration.requests

def test_short_note_links_expand_only_citations_selected_for_the_answer(service, monkeypatch):
    from urllib.parse import quote

    first, second = "业务/退款规则.md", "数据/订单表.md"
    def final(payload):
        receipts = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        assert all("citation_id" in receipt for receipt in receipts)
        chosen, unused = (receipt["citation_id"] for receipt in receipts)
        return finish(chosen, f"[已引用]({chosen}) [未引用]({unused}) [不存在](不存在.md)")
    model(monkeypatch, [("knowledge_read", {"path": first}), ("knowledge_read", {"path": second}), final])
    result = service.chat("读取业务规则")
    assert result["answer"] == f"[已引用]({quote(first, safe='/')}) 未引用 不存在"
    assert [receipt["path"] for receipt in result["sources"]] == [first]


def test_short_ids_reject_unknown_and_previous_run_citations(service, monkeypatch):
    captured = []
    def finish_current(payload):
        receipt = json.loads(next(m["content"] for m in payload["messages"] if m["role"] == "tool"))
        assert "citation_id" in receipt
        captured.append(receipt["citation_id"])
        return finish(receipt["citation_id"])
    model(monkeypatch, [("knowledge_read", {"path": "业务/退款规则.md"}), finish_current])
    assert service.chat("退款规则")["completion"] == "complete"
    previous = captured[0]
    model(monkeypatch, [("knowledge_read", {"path": "数据/订单表.md"}), finish(previous),
                       finish("r_unknown_9"), finish_current])
    result = service.chat("订单表", history=[{"role": "assistant", "content": f"之前引用 {previous}"}])
    assert captured[1] != previous
    assert [activity["status"] for activity in result["activities"]] == ["complete", "error", "error", "complete"]
    assert [receipt["path"] for receipt in result["sources"]] == ["数据/订单表.md"]


def test_closing_context_preserves_short_id_to_actual_read_mapping(service, monkeypatch):
    import re

    monkeypatch.setattr("businessmap.exploration.MAX_TOTAL_TOKENS", 24000)
    paths = ["规则/已读甲.md", "规则/已读乙.md"]
    for path, text in zip(paths, ["甲的真实依据", "乙的真实依据"]):
        service.store.save_note(path, "# 依据\n" + text)
    def final(payload):
        assert {tool["function"]["name"] for tool in payload["tools"]} == {"finish_answer"}
        evidence = json.loads(payload["messages"][-1]["content"])["read_evidence"]
        ids = [item["reference"] for item in evidence]
        assert len(set(ids)) == 2 and all(re.fullmatch(r"r_[0-9a-f]{8}_\d+", ref) for ref in ids)
        chosen = next(item["reference"] for item in evidence if "乙的真实依据" in item["content"])
        return finish(chosen, f"乙的依据已核对。[依据]({chosen})")
    model(monkeypatch, [("knowledge_read", {"path": paths[0]}), ("knowledge_read", {"path": paths[1]}), final],
          usage=lambda count: {"prompt_tokens": (1000, 14000, 100)[count - 1], "completion_tokens": 10})
    result = service.chat("只引用乙的依据")
    assert result["completion"] == "complete"
    assert [receipt["path"] for receipt in result["sources"]] == [paths[1]]
