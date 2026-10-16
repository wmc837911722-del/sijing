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


def test_selected_repository_can_read_and_cite_source_beyond_markdown_index(service, code_repository, monkeypatch):
    repo = code_repository
    reference = f"source://{repo['id']}/{repo['commit']}/z-business/Stars.py#L1-L3"
    requests = model(monkeypatch, [
        ("repository_search", {"query": "LOSER_STARS"}),
        ("repository_read", {"path": "z-business/Stars.py", "line_start": 1, "line_end": 3}),
        finish(reference, f"代码定义败者扣1星，优先使用保星。[结算源码]({reference})"),
    ])
    result = service.chat("结算源码如何定义？", repository_id=repo["id"])
    assert result["completion"] == "complete"
    assert result["sources"][0]["path"] == reference
    assert result["sources"][0]["kind"] == "code"
    assert result["sources"][0]["commit"] == repo["commit"]
    assert f"]({reference})" in result["answer"]
    assert "2: LOSER_STARS = -1" in json.dumps(requests[2], ensure_ascii=False)
    assert result["activities"][0]["detail"].find("502") >= 0


def test_source_search_excerpt_is_not_a_read_receipt(service, code_repository, monkeypatch):
    repo = code_repository
    reference = f"source://{repo['id']}/{repo['commit']}/z-business/Stars.py#L1-L3"
    model(monkeypatch, [("repository_search", {"query": "LOSER_STARS"}), finish(reference),
                        ("repository_read", {"path": "z-business/Stars.py", "line_start": 1, "line_end": 3}), finish(reference, "依据已读源码")])
    result = service.chat("结算", repository_id=repo["id"])
    assert result["activities"][1]["status"] == "error"
    assert result["sources"][0]["revision"]


def test_missing_source_can_finish_with_honest_clarification(service, code_repository, monkeypatch):
    model(monkeypatch, [("repository_search", {"query": "UNKNOWN_POLICY"}),
                       ("finish_answer", {"answer": "当前源码未找到该规则，需要补充对应模块。", "citations": [], "kind": "clarification"})])
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


def test_source_viewer_requires_auth_and_rejects_forged_references(service, code_repository):
    from fastapi.testclient import TestClient
    from businessmap.app import create_app
    repo = code_repository
    app = create_app(service.store.data_dir, "test-session-token-123")
    headers = {"authorization": "Bearer test-session-token-123"}
    reference = f"source://{repo['id']}/{repo['commit']}/z-business/Stars.py#L1-L3"
    with TestClient(app) as client:
        assert client.get("/api/source-evidence", params={"reference": reference}).status_code == 401
        response = client.get("/api/source-evidence", params={"reference": reference}, headers=headers)
        assert response.status_code == 200
        assert "2: LOSER_STARS = -1" in response.json()["content"]
        assert response.json()["commit"] == repo["commit"]
        for invalid in ("https://example.invalid/source.py", reference.replace("z-business/Stars.py", "../escape.py"),
                        reference.replace(repo["commit"], "0" * 40)):
            assert client.get("/api/source-evidence", params={"reference": invalid}, headers=headers).status_code in {400, 404, 409}
        service.store.config_set(f"ai_analysis_status:{repo['id']}",
            {"status": "failed", "summary": "模型引用校验失败，未写入。", "category": "citation",
             "vault_path": str(service.store.vault), "raw_response": "must-not-leak"})
        state = client.get("/api/analysis-status", headers=headers).json()
        assert state["statuses"][repo["id"]]["category"] == "citation"
        assert "raw_response" not in json.dumps(state)


def test_canonical_material_folders_keep_repository_model_authorization(service, code_repository):
    repo = code_repository
    note = service.store.save_note(f"00-Inbox/代码资料/{repo['id']}/版本-fixture.md", "# 代码资料\nprivate material")
    other = service.providers.save({"name": "Other", "kind": "openai-compatible",
                                    "base_url": "http://127.0.0.1:2/v1", "model": "other"})
    assert service.note_repositories(note) == {repo["id"]}
    assert service.note_allowed(note, other) is False


@pytest.mark.parametrize("note_type", ["code", "raw"])
def test_moved_source_material_cannot_alone_prove_a_business_answer(service, monkeypatch, note_type):
    path = "01-Wiki/整理/代码资料.md"
    service.store.save_note(path, f"---\ntype: {note_type}\n---\n# 原始资料\n此处只是文件定位，未读取实现。")
    model(monkeypatch, [("knowledge_read", {"path": path}), finish(path, "已核实业务规则。"),
                       ("finish_answer", {"answer": "索引不足以核实业务规则，需要读取具体实现。",
                                          "citations": [], "kind": "clarification"})])
    result = service.chat("请核实业务规则")
    assert result["activities"][1]["status"] == "error"
    assert "需要补充或确认" in result["answer"]


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


def test_reported_prompt_usage_leaves_budget_to_read_late_english_source_helper(service, code_repository, monkeypatch):
    """Actual source receipts grow in bytes much faster than reported prompt tokens."""
    import subprocess
    from pathlib import Path

    repo = code_repository
    source = Path(repo["path"])
    path = "arena/JdbcStarServiceImpl.java"
    (source / "arena").mkdir()
    lines = ["package arena.settlement;", "public final class JdbcStarServiceImpl {",
             "    public void settle(StarAdjustment adjustment) {"]
    lines += [f'        settlementSteps.add(new StarAdjustment("round-{index:03d}", adjustment.getOriginalStars(), adjustment.getProtectionCard()));'
              for index in range(150)]
    lines.append("    }")
    helper_start = len(lines) + 1
    lines += ["    private int deductAfterProtection(StarAdjustment adjustment) {",
              "        if (adjustment.consumeProtectionCard()) return adjustment.getOriginalStars();",
              "        return Math.max(0, adjustment.getOriginalStars() - 1);", "    }", "}"]
    (source / path).write_text("\n".join(lines) + "\n")
    for index in range(7):
        (source / f"arena/SettlementPlan{index}.java").write_text(
            "package arena.settlement;\npublic final class SettlementPlan {\n" +
            "    // settlement operation follows the configured business rule and preserves original adjustment details.\n" * 12 + "}\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "Large English implementation with a later helper"], check=True)
    repo["commit"] = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    service.store._write_repo(repo)
    reference = f"source://{repo['id']}/{repo['commit']}/{path}#L{helper_start}-L{len(lines)}"

    def read_helper(payload):
        assert "repository_read" in {tool["function"]["name"] for tool in payload["tools"]}, \
            "full English transcript bytes must not force synthesis before the helper can be read"
        return "repository_read", {"path": path, "line_start": helper_start, "line_end": len(lines)}

    def final(payload):
        assert "consumeProtectionCard" in json.dumps(payload["messages"], ensure_ascii=False)
        return finish(reference, "实际实现先消耗保星卡，否则扣一星且不低于零。")

    reported_inputs = (4000, 6000, 8000, 10000, 12000, 13500)
    requests = model(monkeypatch, [("repository_search", {"query": "settlement"}),
        ("repository_read", {"path": path, "line_start": 1, "line_end": 43}),
        ("repository_read", {"path": path, "line_start": 44, "line_end": 86}),
        ("repository_read", {"path": path, "line_start": 87, "line_end": 129}), read_helper, final],
        usage=lambda count: {"prompt_tokens": reported_inputs[count - 1], "completion_tokens": 100})
    result = service.chat("从结算实际实现和后部helper解释保星与扣星顺序。", repository_id=repo["id"])
    assert len(requests) == 6 and result["completion"] == "complete"
    assert result["sources"][0]["path"] == reference
    assert result["usage"]["input_tokens"] == sum(reported_inputs)
    assert result["usage"]["input_tokens"] + result["usage"]["output_tokens"] < 60000
    # This must exercise the original failure, not a tiny synthetic tool result.
    fifth = requests[4]
    full_bytes = len(json.dumps({"system": fifth["messages"][0]["content"],
        "messages": fifth["messages"][1:], "tools": fifth["tools"]}, ensure_ascii=False).encode())
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
