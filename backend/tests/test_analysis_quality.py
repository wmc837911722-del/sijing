from __future__ import annotations

import json
import subprocess
import time

import httpx
import pytest

from businessmap.core import Store
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService
from businessmap.content_layout import business_paths, summary_path


CODE = "class RefundServiceImpl {\n    boolean refund(int amount) {\n        return amount > 0;\n    }\n}\n"
PATH = "src/RefundServiceImpl.java"
HTTP_CLIENT = httpx.Client


@pytest.fixture
def analysis(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / PATH).write_text(CODE, encoding="utf-8")
    for args in (("init",), ("add", "."), ("commit", "-m", "Fixture")):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
                       cwd=source, check=True, capture_output=True)
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    tools = ToolsService(store)
    repo = store.add_repository(str(source))
    deadline = time.monotonic() + 10
    while store.get_repository(repo["id"])["status"] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    selected = tools.providers.save({"name": "Fixture", "kind": "openai-compatible",
        "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    tools.bind_model(repo["id"], selected["id"])
    yield tools, repo
    store.close()


def model(monkeypatch, response):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        result = response(requests[-1]) if callable(response) else response
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}],
                                         "usage": {"prompt_tokens": 20, "completion_tokens": 10}})
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return requests


def answer(content="# 退款规则\n金额为正时返回true。", quote="return amount > 0;", **source):
    return {"notes": [{"key": "rules", "title": "退款规则", "content": content,
        "sources": [{"path": PATH, "line_start": 1, "line_end": 1, "quote": quote, **source}]}]}


def assert_no_business_output(tools, repo_id):
    assert all(not (tools.store.vault / path).exists()
               for path in [*business_paths(repo_id).values(), summary_path(repo_id)])


def test_analysis_locates_exact_quote_instead_of_trusting_in_range_wrong_lines(analysis, monkeypatch):
    tools, repo = analysis
    requests = model(monkeypatch, answer())
    tools.analyze(repo["id"])
    note = tools.store.read_note(business_paths(repo["id"])["rules"])["content"]
    assert "L3–L3" in note
    assert "return amount > 0;" in note
    assert f"commit `{tools.store.get_repository(repo['id'])['commit']}`" in note
    supplied = json.loads(requests[0]["messages"][-1]["content"])["files"][0]
    assert supplied["line_count"] == 5
    assert "3:         return amount > 0;" in supplied["content"]
    assert supplied["truncated"] is False


@pytest.mark.parametrize("source", [{"quote": "return amount >= 0;"}, {"quote": ""}, {"path": "invented.java"},
                                    {"line_start": 0}, {"line_end": 99}, {"line_start": "1"}])
def test_analysis_rejects_unverifiable_evidence_before_writing(analysis, monkeypatch, source):
    tools, repo = analysis
    model(monkeypatch, answer(**source))
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    assert_no_business_output(tools, repo["id"])


def test_model_markdown_repairs_structure_but_preserves_code_and_escapes(analysis, monkeypatch):
    tools, repo = analysis
    content = r'# 退款规则\n\n## 条件\n- 金额为正\n- 例外待确认\n\n```json\n{"newline":"\n","path":"C:\\new"}\n```\n\n路径 `C:\notes\new`；正则 `\n+`。'
    model(monkeypatch, answer(content=content))
    tools.analyze(repo["id"])
    note = tools.store.read_note(business_paths(repo["id"])["rules"])["content"]
    assert "# 退款规则\n\n## 条件\n- 金额为正\n- 例外待确认\n\n```json\n" in note
    assert r'{"newline":"\n","path":"C:\\new"}' in note
    assert r'路径 `C:\notes\new`；正则 `\n+`。' in note


def test_markdown_heading_to_prose_separator_is_repaired_without_decoding_paths(analysis, monkeypatch):
    tools, repo = analysis
    model(monkeypatch, answer(content=r"# 退款规则\n金额为正。\n\n## 待确认\n接口声明不能证明已实现。"))
    tools.analyze(repo["id"])
    note = tools.store.read_note(business_paths(repo["id"])["rules"])["content"]
    assert "# 退款规则\n金额为正。\n\n## 待确认\n接口声明不能证明已实现。" in note


@pytest.mark.parametrize("content", [
    '# 正常\n\n正文。\n```regex\n\\n+\\n\\n\n```\n',
    '路径 C:\\notes\\new；JSON {"separator":"\\n\\n"}；正则 `\\n+`。',
    '# 正常\n\n```text\n未闭合围栏中的 \\n\\n 保持原样。',
])
def test_valid_markdown_escapes_and_unclosed_code_are_not_reinterpreted(analysis, monkeypatch, content):
    tools, repo = analysis
    model(monkeypatch, answer(content=content))
    tools.analyze(repo["id"])
    assert content.strip() in tools.store.read_note(business_paths(repo["id"])["rules"])["content"]


def test_analysis_failure_is_persisted_safely_and_success_clears_it(analysis, monkeypatch):
    tools, repo = analysis
    requests = model(monkeypatch, answer(quote="PRIVATE model response api_key=secret"))
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    state = tools.store.config_get(f"ai_analysis_status:{repo['id']}")
    assert state["status"] == "failed" and state["category"] == "citation"
    assert state["error_details"]["reason_code"] == "quote_not_found"
    assert state["error_details"]["file_index"] == 1
    assert state["error_details"]["source_index"] == 1
    assert state["error_details"]["note_key"] == "rules"
    assert "PRIVATE" not in json.dumps(state) and "secret" not in json.dumps(state)
    assert CODE not in json.dumps(state)
    assert len(requests) == 1
    model(monkeypatch, answer())
    tools.analyze(repo["id"])
    restored = tools.store.config_get(f"ai_analysis_status:{repo['id']}")
    assert restored["status"] == "completed" and "category" not in restored and "error_details" not in restored
    assert restored["scope"]["files"] == 1


def test_missing_quote_and_ambiguous_quote_fail_closed(analysis, monkeypatch):
    tools, repo = analysis
    missing = answer()
    missing["notes"][0]["sources"][0].pop("quote")
    model(monkeypatch, missing)
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    model(monkeypatch, answer(quote="}", line_start=1, line_end=5))
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    assert_no_business_output(tools, repo["id"])


def test_model_can_cite_only_a_current_request_evidence_id(analysis, monkeypatch):
    tools, repo = analysis
    def reply(payload):
        supplied = json.loads(payload["messages"][-1]["content"])
        evidence = supplied["files"][0]["evidence"]
        assert sum(len(item["quote"]) for item in evidence) <= 300
        assert evidence[0]["quote"] in CODE
        response = answer()
        response["notes"][0]["sources"] = [{"evidence_id": evidence[0]["evidence_id"]}]
        return response
    requests = model(monkeypatch, reply)
    tools.analyze(repo["id"])
    note = tools.store.read_note(business_paths(repo["id"])["rules"])["content"]
    assert "return amount > 0;" in note and "L3–L3" in note
    assert len(requests) == 1


def test_unknown_evidence_id_is_rejected_with_safe_reason(analysis, monkeypatch):
    tools, repo = analysis
    response = answer()
    response["notes"][0]["sources"] = [{"evidence_id": "not-in-this-request"}]
    model(monkeypatch, response)
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    state = tools.store.config_get(f"ai_analysis_status:{repo['id']}")
    assert state["error_details"]["reason_code"] == "unknown_evidence_id"
    assert_no_business_output(tools, repo["id"])


def test_native_evidence_id_retains_its_known_position_for_repeated_same_line_text():
    from businessmap.analysis_output import prepare_file, verify_sources
    content = ("return amount > 0; " * 60).rstrip() + "\n"
    supplied, _ = prepare_file({"path": PATH, "content": content}, 6000, 37000)
    candidate = supplied["evidence"][0]
    checked = verify_sources([{"evidence_id": candidate["evidence_id"]}], [supplied])
    assert checked == [{"path": PATH, "line_start": 1, "line_end": 1, "excerpt": content.rstrip("\n")}]


def test_failed_provider_records_safe_summary_and_does_not_retry(analysis, monkeypatch):
    tools, repo = analysis
    requests = model(monkeypatch, httpx.Response(503, text="PRIVATE-RESPONSE api_key=fixture-secret original source"))
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    state = tools.store.config_get(f"ai_analysis_status:{repo['id']}")
    assert state["status"] == "failed" and state["category"] == "model"
    assert "PRIVATE" not in json.dumps(state) and "fixture-secret" not in json.dumps(state)
    assert len(requests) == 1
    assert_no_business_output(tools, repo["id"])


@pytest.mark.parametrize("response", [{"notes": []}, {"notes": [{"key": []}]},
                                     {"notes": [answer()["notes"][0], {"key": []}]}])
def test_invalid_model_schema_has_persistent_format_error(analysis, monkeypatch, response):
    tools, repo = analysis
    model(monkeypatch, response)
    with pytest.raises(ToolError):
        tools.analyze(repo["id"])
    state = tools.store.config_get(f"ai_analysis_status:{repo['id']}")
    assert state["status"] == "failed" and state["category"] == "format"
    assert_no_business_output(tools, repo["id"])


def test_numbered_prompt_stays_within_context_budget_and_reports_partial_coverage(analysis, monkeypatch):
    tools, repo = analysis
    from pathlib import Path
    source = Path(repo["path"])
    for index in range(8):
        (source / f"Large{index}.java").write_text(f"class Large{index} {{\n" + "    int value;\n" * 1500 + "}\n", encoding="utf-8")
    for args in (("add", "."), ("commit", "-m", "Large fixture")):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
                       cwd=source, check=True, capture_output=True)
    task_id = tools.store.sync_repository(repo["id"])["task_id"]
    deadline = time.monotonic() + 10
    while next(task for task in tools.store.tasks() if task["id"] == task_id)["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    def reply(payload):
        first = json.loads(payload["messages"][-1]["content"])["files"][0]
        quote = first["content"].splitlines()[0].split(": ", 1)[-1]
        return answer(quote=quote, path=first["path"])
    requests = model(monkeypatch, reply)
    tools.analyze(repo["id"])
    assert sum(len(message["content"]) for message in requests[0]["messages"]) <= 44000
    supplied = json.loads(requests[0]["messages"][-1]["content"])
    assert supplied["scope_limited"] is True
    assert supplied["coverage"]["total_files"] == 9
    assert all("numbered_content" not in item for item in supplied["files"])


def test_analysis_separates_business_cards_map_and_summary_from_raw_source(analysis, monkeypatch):
    tools, repo = analysis
    requests = model(monkeypatch, answer())
    result = tools.analyze(repo["id"])
    paths = business_paths(repo["id"])
    rules = tools.store.read_note(paths["rules"])["content"]
    business_map = tools.store.read_note(paths["overview"])["content"]
    summary = tools.store.read_note(summary_path(repo["id"]))["content"]
    commit = tools.store.get_repository(repo["id"])["commit"]
    assert result["notes_created"] == 3
    assert "content_role: business_knowledge" in rules
    assert "content_role: business_map" in business_map
    assert "content_role: analysis_summary" in summary
    assert f"source://{repo['id']}/{commit}/{PATH}#L3-L3" in rules
    assert "[[代码索引/" not in rules
    assert f"[[{paths['rules'][:-3]}|退款规则]]" in business_map
    map_body = business_map.split("---", 2)[-1]
    assert "代码文件清单" not in map_body and PATH not in map_body
    assert f"[[{paths['rules'][:-3]}|退款规则]]" in summary
    assert commit in summary and "1 / 1" in summary and "待确认" in summary
    assert "return amount > 0;" not in summary and "```" not in summary
    state = tools.store.config_get(f"ai_analysis:{repo['id']}")
    assert state["notes"]["summary"]["path"] == summary_path(repo["id"])
    assert set(state["notes"]) == {"rules", "overview", "summary"}
    supplied = json.loads(requests[0]["messages"][-1]["content"])
    assert supplied["note_paths"] == paths


def test_model_overview_keeps_verified_business_meaning_and_links_only_written_cards(analysis, monkeypatch):
    tools, repo = analysis
    response = answer()
    response["notes"].insert(0, {**response["notes"][0], "key": "overview", "title": "退款业务地图",
                                  "content": "# 退款业务地图\n正金额是退款方法返回true的条件。"})
    model(monkeypatch, response)
    tools.analyze(repo["id"])
    paths = business_paths(repo["id"])
    business_map = tools.store.read_note(paths["overview"])["content"]
    assert "正金额是退款方法返回true的条件。" in business_map
    assert "source://" in business_map
    assert f"[[{paths['rules'][:-3]}|退款规则]]" in business_map
    assert f"[[{paths['entities'][:-3]}" not in business_map
    assert "知识阅读关系" in business_map


def test_analysis_preserves_human_map_and_summary_and_offers_revision_guarded_candidates(analysis, monkeypatch):
    tools, repo = analysis
    paths = business_paths(repo["id"])
    originals = {paths["overview"]: "# 我的业务地图\n人工关系。\n",
                 summary_path(repo["id"]): "# 我的总结\n人工结论。\n"}
    revisions = {path: tools.store.save_note(path, content)["revision"] for path, content in originals.items()}
    model(monkeypatch, answer())
    result = tools.analyze(repo["id"])
    assert result["notes_created"] == 1 and len(result["conflicts"]) == 2
    for path, content in originals.items():
        assert tools.store.read_note(path)["content"] == content
    candidates = {candidate["path"]: candidate for candidate in tools.candidates()}
    assert set(candidates) == set(originals)
    assert all(candidate["current_revision"] == revisions[path] for path, candidate in candidates.items())
    assert "退款规则" in candidates[summary_path(repo["id"])]["content"]


def test_map_and_summary_do_not_claim_conflicting_card_was_generated(analysis, monkeypatch):
    tools, repo = analysis
    paths = business_paths(repo["id"])
    human = "# 我的退款规则\n人工业务口径。\n"
    tools.store.save_note(paths["rules"], human)
    model(monkeypatch, answer())
    result = tools.analyze(repo["id"])
    assert len(result["conflicts"]) == 1
    assert tools.store.read_note(paths["rules"])["content"] == human
    business_map = tools.store.read_note(paths["overview"])["content"]
    summary = tools.store.read_note(summary_path(repo["id"]))["content"]
    assert f"[[{paths['rules'][:-3]}" not in business_map
    assert f"[[{paths['rules'][:-3]}" not in summary
    assert "未成功保存业务知识卡片" in business_map
    assert "更新建议" in summary and "待确认" in summary


def test_map_and_summary_detect_human_edits_during_model_request(analysis, monkeypatch):
    tools, repo = analysis
    model(monkeypatch, answer())
    tools.analyze(repo["id"])
    paths = business_paths(repo["id"])
    edited = {}
    def response(_payload):
        for path in (paths["overview"], summary_path(repo["id"])):
            previous = tools.store.read_note(path)
            edited[path] = previous["content"] + "\n人工请求期间修改。\n"
            tools.store.save_note(path, edited[path], previous["revision"])
        return answer()
    model(monkeypatch, response)
    result = tools.analyze(repo["id"])
    assert {conflict["path"] for conflict in result["conflicts"]} == set(edited)
    assert all(tools.store.read_note(path)["content"] == content for path, content in edited.items())


def test_overview_without_cards_does_not_invent_business_knowledge(analysis, monkeypatch):
    tools, repo = analysis
    response = answer(content="# 退款业务\n正数条件已声明，完整业务流程待确认。")
    response["notes"][0]["key"] = "overview"
    model(monkeypatch, response)
    result = tools.analyze(repo["id"])
    assert result["notes_created"] == 2
    paths = business_paths(repo["id"])
    business_map = tools.store.read_note(paths["overview"])["content"]
    assert "完整业务流程待确认" in business_map and "source://" in business_map
    assert "未成功保存业务知识卡片" in business_map
    assert not (tools.store.vault / f"01-Wiki/业务知识/{repo['id']}").exists()
    assert set(tools.store.config_get(f"ai_analysis:{repo['id']}")["notes"]) == {"overview", "summary"}


def test_map_removes_prospective_model_links_to_conflicting_or_missing_cards(analysis, monkeypatch):
    tools, repo = analysis
    paths = business_paths(repo["id"])
    tools.store.save_note(paths["rules"], "# 人工规则\n保留。\n")
    response = answer()
    response["notes"].append({**response["notes"][0], "key": "overview", "title": "退款业务地图",
        "content": f"# 退款业务\n查看[[{paths['rules'][:-3]}|退款规则]]与[[{paths['entities'][:-3]}|实体]]。"})
    model(monkeypatch, response)
    tools.analyze(repo["id"])
    business_map = tools.store.read_note(paths["overview"])["content"]
    assert "查看退款规则与实体。" in business_map
    assert f"[[{paths['rules'][:-3]}" not in business_map
    assert f"[[{paths['entities'][:-3]}" not in business_map


def test_source_reference_encodes_markdown_and_uri_sensitive_paths():
    from businessmap.analysis_output import render_excerpt
    source = {"path": "src/退款 [v2]#rule.java", "excerpt": "return true;", "line_start": 2, "line_end": 2}
    rendered = render_excerpt(source, "a" * 40, "repo")
    assert "source://repo/" + "a" * 40 + "/src/%E9%80%80%E6%AC%BE%20%5Bv2%5D%23rule.java#L2-L2" in rendered
    assert r"退款 \[v2\]#rule.java" in rendered
    assert "[[" not in rendered


def test_analysis_evidence_uses_git_lf_lines_not_unicode_paragraph_breaks(analysis, monkeypatch):
    from pathlib import Path
    tools, repo = analysis
    source = Path(repo["path"])
    physical_line = '    String label = "前\u2028后"; boolean refund(int amount) { return amount > 0; }'
    (source / PATH).write_text("class RefundServiceImpl {\n" + physical_line + "\n}\n", encoding="utf-8")
    for args in (("add", "."), ("commit", "-m", "Unicode line fixture")):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
                       cwd=source, check=True, capture_output=True)
    task_id = tools.store.sync_repository(repo["id"])["task_id"]
    deadline = time.monotonic() + 10
    while next(task for task in tools.store.tasks() if task["id"] == task_id)["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    requests = model(monkeypatch, answer())
    tools.analyze(repo["id"])
    supplied = json.loads(requests[0]["messages"][-1]["content"])["files"][0]
    assert supplied["line_count"] == 3
    assert "2: " + physical_line in supplied["content"]
    assert "3: }" in supplied["content"]
    note = tools.store.read_note(business_paths(repo["id"])["rules"])["content"]
    assert "#L2-L2" in note and physical_line in note
    assert tools.store.config_get(f"ai_analysis_status:{repo['id']}")["scope"]["line_count"] == 3


def test_analysis_summary_distinguishes_cards_map_and_summary_documents(analysis, monkeypatch):
    tools, repo = analysis
    model(monkeypatch, answer())
    summary = tools.analyze(repo["id"])["summary"]
    assert "1 篇业务知识卡片" in summary
    assert "业务地图已保存" in summary and "分析总结已保存" in summary
    assert "3 篇" in summary and "3 篇业务知识卡片" not in summary


def test_overview_filters_only_prose_links_without_changing_metadata_or_verified_code(analysis, monkeypatch):
    from pathlib import Path
    import yaml
    tools, repo = analysis
    source = Path(repo["path"])
    literal = 'String pattern = "[[literal]]";'
    (source / PATH).write_text("class RefundServiceImpl {\n    " + literal + "\n}\n", encoding="utf-8")
    for args in (("add", "."), ("commit", "-m", "Literal wiki syntax fixture")):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
                       cwd=source, check=True, capture_output=True)
    task_id = tools.store.sync_repository(repo["id"])["task_id"]
    deadline = time.monotonic() + 10
    while next(task for task in tools.store.tasks() if task["id"] == task_id)["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    paths = business_paths(repo["id"])
    protected = ('`[[inline literal]]`\n\n~~~text\n[[fenced literal]]\n~~~~\n\n'
                 '    [[indented literal]]\n\n> ```text\n> [[quoted literal]]\n> ```\n')
    body = f"# 业务地图\n\n主题[[{paths['entities'][:-3]}|实体]]待确认。\n\n" + protected
    response = answer(quote=literal)
    response["notes"].append({**response["notes"][0], "key": "overview", "title": "[[原始标题]]", "content": body})
    model(monkeypatch, response)
    tools.analyze(repo["id"])
    overview = tools.store.read_note(paths["overview"])["content"]
    assert literal in overview
    assert literal in tools.store.read_note(paths["rules"])["content"]
    assert yaml.safe_load(overview.split("---", 2)[1])["title"] == "[[原始标题]]"
    assert "主题实体待确认。" in overview
    assert f"[[{paths['entities'][:-3]}" not in overview
    assert protected in overview
