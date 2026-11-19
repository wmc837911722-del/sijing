from __future__ import annotations

import json
import os
import subprocess
import sys
import yaml

import pytest

from businessmap.providers import ToolError
from businessmap.core import Store, CoreError
from test_project_analysis import project_tools, fake_model as sampled_model
from test_full_analysis import full_project, model as source_model


@pytest.fixture(autouse=True)
def semantic_stage_fixture(monkeypatch):
    from businessmap.business_semantics import BusinessSemanticIntegration
    from business_projection_fixture import projection_from_units
    monkeypatch.setattr(BusinessSemanticIntegration, "build", lambda self, snapshot, run, **kwargs: projection_from_units(snapshot, run, **kwargs))


def test_project_document_keeps_evidence_private_instead_of_exporting_code(project_tools):
    analysis, tools, project, _ = project_tools
    snapshot = analysis.snapshot(project["id"])
    source = {"repository_id": "front", "commit": "a" * 40, "path": "src/private/RefundService.java",
              "line_start": 2, "line_end": 2, "excerpt": "return amount > 0;"}
    content, private_sources = analysis._document(snapshot, {"truncated": True}, "rules", "退款条件",
                                                 "# 退款条件\n退款金额必须为正数。", [source])
    assert "source://" not in content
    assert "return amount" not in content and "RefundService" not in content
    assert "a" * 40 not in content and project["id"] not in content
    metadata = yaml.safe_load(content.split("---", 2)[1])
    assert set(metadata) <= {"title", "type", "content_role", "tags", "generated"}
    assert metadata["generated"] is True
    assert private_sources == ["front/src/private/RefundService.java"]


@pytest.mark.parametrize("text", [
    "退款由 RefundServiceImpl.refund() 处理。", "来源 src/private/rule.py。",
    "[规则](source://repo/" + "a" * 40 + "/rules.py#L2-L4)",
    "待确认：repository_id: secret-project", "```java\nreturn true;\n```",
    "[规则](source%3A%2F%2Frepo%2Fsecret)", "[规则](source&#58;//repo/secret)",
    "未知项来自 /Users/private/work/rules.ts。", "数据库字段 refund_amount 表示金额。",
    "当前版本" + "a" * 40 + "行为待确认。", "资料src/private/rule.py待确认。", "使用InternalRefund管理退款。",
    "[规则](source%25253A%25252F%25252Frepo)", "[规则](source&amp;amp;#58;//repo)",
])
def test_public_validator_rejects_technical_content_without_echoing_it(text):
    from businessmap.business_content import validate_public_business_text
    with pytest.raises(ToolError) as error:
        validate_public_business_text(text)
    assert error.value.status_code == 422
    assert text not in str(error.value) and "secret" not in str(error.value)


def test_public_renderer_supports_business_markdown_and_only_public_metadata():
    from businessmap.business_content import render_public_note, validate_public_business_text
    body = "# 退款条件\n\n退款金额必须为正数；超过可退金额时禁止退款。\n\n[[01-Wiki/天梯赛事/规则/退款条件|退款条件]]\n\n## 待确认\n实际审批流程尚未确认。"
    content = render_public_note("退款条件", body, tags=["退款"])
    assert body in content
    assert yaml.safe_load(content.split("---", 2)[1]) == {
        "title": "退款条件", "type": "business", "content_role": "business_knowledge", "tags": ["退款"], "generated": True}
    validate_public_business_text(content)


def test_public_business_markdown_preserves_relative_note_navigation():
    from businessmap.business_content import validate_public_business_text
    validate_public_business_text("[表](../数据/订单.md#字段)；[返回](../业务/入口.md)。")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_handwritten_business_frontmatter_roundtrips_and_checks_comments(tmp_path, newline):
    from businessmap.business_content import public_business_metadata
    content = newline.join(["---", "# 冬季档期说明", "title: 报名规则", "type: rule",
                            "content_role: business_knowledge  # 此处面向会员", "tags: [报名]", "---", "",
                            "VIP（会员）可提前报名。", "SKU（商品规格）按门店统计。", ""])
    assert public_business_metadata(content)["tags"] == ["报名"]
    for phrase in ("冬季档期说明", "此处面向会员"):
        with pytest.raises(ToolError):
            public_business_metadata(content, forbidden_tokens=[phrase])
    store = Store(tmp_path / "data")
    try:
        store.open_vault(str(tmp_path / "vault"))
        path = "规则/报名规则.md"
        store.save_note(path, content)
        assert store.read_note(path)["content"].splitlines() == content.splitlines()
        assert (store.vault / path).read_bytes() == content.encode()
    finally:
        store.close()


@pytest.mark.parametrize("text", ["Q3（第3季度）营收增长。", "VIP（会员，含试用会员）可报名。"])
def test_business_gloss_allows_numbers_and_chinese_punctuation(text):
    from businessmap.business_content import validate_public_business_text
    validate_public_business_text(text)
    with pytest.raises(ToolError):
        validate_public_business_text(text, forbidden_tokens=[text])


@pytest.mark.parametrize("text", ["SKU(商品规格)", "Q3(第3季度)", "SKU（123）", "refund（订单）", "RefundService（订单）"])
def test_business_gloss_keeps_method_names_restricted(text):
    from businessmap.business_content import validate_public_business_text
    with pytest.raises(ToolError):
        validate_public_business_text(text)


@pytest.mark.parametrize("title", ["../退款条件", "源码.py", "[[内部链接]]", "退款#条件"])
def test_public_topic_paths_reject_technical_or_navigation_syntax(title):
    from businessmap.content_layout import public_topic_path
    with pytest.raises(ToolError):
        public_topic_path("天梯赛事", "rules", title)


def test_public_validator_blocks_known_identifiers_even_inside_unknowns():
    from businessmap.business_content import validate_public_business_text
    with pytest.raises(ToolError):
        validate_public_business_text("# 退款条件\n\n待确认：InternalRefund 的行为。", ["InternalRefund"])


@pytest.mark.parametrize("text", ["新增教练由 createCoach 处理。", "依据提交7531b864。"])
def test_lower_camel_and_short_commit_are_not_public_business_text(text):
    from businessmap.business_content import validate_public_business_text
    with pytest.raises(ToolError):
        validate_public_business_text(text)


@pytest.mark.parametrize("word,count", [("业务规则", 150000), ("businessrule", 150000)])
def test_long_unbroken_business_prose_validation_finishes_in_bounded_time(word, count):
    code = f"from businessmap.business_content import validate_public_business_text; validate_public_business_text({word!r}*{count}); print('ok')"
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=3,
                                env={**os.environ, "PYTHONPATH": "backend"})
    except subprocess.TimeoutExpired:
        pytest.fail("public validation stalled on ordinary business prose")
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_all_analyzable_source_suffixes_are_rejected_in_public_prose():
    from businessmap.business_content import validate_public_business_text
    from businessmap.full_source import FullSourceInventory
    suffixes = Store.SOURCE_SUFFIXES | FullSourceInventory.EXTRA_SOURCE_SUFFIXES | FullSourceInventory.CONTRACT_SUFFIXES
    for suffix in sorted(suffixes):
        with pytest.raises(ToolError):
            validate_public_business_text("资料位于规则" + suffix + "，待确认。")


@pytest.mark.parametrize("disposition", ["public", "internal"])
def test_safe_business_fact_cannot_lose_condition_or_hide_in_technical_classification(disposition):
    from businessmap.l0_legacy import LegacyL0
    text = "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额。"
    unit = {"kind": "finding", "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = ({"fact_id": "f0", "disposition": "public", "category": "rules", "title": "退款条件",
             "text": "订单仅在已支付时允许退款；退款金额不得超过已支付金额。"} if disposition == "public" else
            {"fact_id": "f0", "disposition": "internal", "classification": "technical", "reason": "内部实现条件"})
    if disposition == "internal":
        with pytest.raises(ToolError):
            LegacyL0._validate({"assignments": [item]}, [unit])
    else:
        result = LegacyL0._validate({"assignments": [item]}, [unit])
        assert result["assignments"][0]["text"] == text


@pytest.mark.parametrize("kind,text", [
    ("unknown", "尚未看到业务入口及调用方，无法确认段位写操作和超管专属操作是否均接入对应守卫，也无法确认门店可写校验的具体规则。"),
    ("finding", "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额；超过30天是否可退尚未确认。"),
])
@pytest.mark.parametrize("model_text", [None, "段位操作已核验。", "src/private/Rules.java", {"raw": "RAW-PRIVATE"}])
def test_clean_public_text_is_source_owned_despite_missing_or_rewritten_model_text(kind, text, model_text):
    from businessmap.l0_legacy import LegacyL0
    unit = {"kind": kind, "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = {"fact_id": "f0", "disposition": "public", "category": "rules", "title": "业务规则"}
    if model_text is not None:
        item["text"] = model_text
    result = LegacyL0._validate({"assignments": [item]}, [unit])
    assert result["assignments"][0]["text"] == text


def test_source_owned_text_still_rejects_unsafe_model_topic():
    from businessmap.full_synthesis import FullBusinessSynthesis
    unit = {"kind": "unknown", "text": "退款条件尚未确认。", "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = {"fact_id": "f0", "disposition": "public", "category": "rules", "title": "规则src/private/Rules.java"}
    with pytest.raises(ToolError) as error:
        FullBusinessSynthesis._validate({"assignments": [item]}, [unit])
    assert error.value.reason_code == "public_content"


def test_mixed_technical_fact_rewrite_keeps_every_business_clause():
    from businessmap.l0_legacy import LegacyL0
    unit = {"kind": "finding", "text": "createCoach仅在已支付且未退款时允许退款；退款金额不得超过已支付金额。", "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = {"fact_id": "f0", "disposition": "public", "category": "rules", "title": "退款条件", "text": "仅在已支付时允许退款；退款金额不得超过已支付金额。"}
    with pytest.raises(ToolError):
        LegacyL0._validate({"assignments": [item]}, [unit])
    item["text"] = "仅在已支付且未退款时允许退款；退款金额不得超过已支付金额。"
    accepted = LegacyL0._validate({"assignments": [item]}, [unit])
    assert accepted["assignments"][0]["text"] == item["text"]


@pytest.mark.parametrize("model_text", [None, "createCoach仅在已支付且未退款时允许退款。", "仅在已支付时允许退款。"])
def test_source_bearing_text_still_requires_safe_complete_rewrite(model_text):
    from businessmap.l0_legacy import LegacyL0
    unit = {"kind": "finding", "text": "createCoach仅在已支付且未退款时允许退款。",
            "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = {"fact_id": "f0", "disposition": "public", "category": "rules", "title": "退款条件", "text_policy": "source_owned"}
    if model_text is not None:
        item["text"] = model_text
    with pytest.raises(ToolError):
        LegacyL0._validate({"assignments": [item]}, [unit])


@pytest.mark.parametrize("path", ["repo/service/RefundService.java", "repo/tests/RefundServiceTest.java"])
@pytest.mark.parametrize("disposition", ["public", "internal"])
def test_test_keyword_or_test_path_cannot_hide_mixed_business_conditions(path, disposition):
    from businessmap.full_synthesis import FullBusinessSynthesis
    text = "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额；该规则尚无测试覆盖。"
    unit = {"kind": "finding", "text": text, "_source_paths": [path], "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = ({"fact_id": "f0", "disposition": "public", "category": "rules", "title": "退款条件"}
            if disposition == "public" else {"fact_id": "f0", "disposition": "internal", "classification": "test", "reason": "测试说明"})
    if disposition == "internal":
        with pytest.raises(ToolError):
            FullBusinessSynthesis._validate({"assignments": [item]}, [unit])
    else:
        result = FullBusinessSynthesis._validate({"assignments": [item]}, [unit])
        assert "text" not in result["assignments"][0]


def test_negative_test_coverage_boundary_remains_a_public_unknown():
    from businessmap.full_synthesis import FullBusinessSynthesis
    text = "该规则尚无测试覆盖。"
    unit = {"kind": "unknown", "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    with pytest.raises(ToolError):
        FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "internal", "classification": "test", "reason": "测试说明"}]}, [unit])
    result = FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "public", "category": "rules", "title": "退款规则待确认"}]}, [unit])
    assert "text" not in result["assignments"][0]


@pytest.mark.parametrize("text", ["测试验证序列化与构建处理。", "测试覆盖序列化与构建处理。", "单元测试说明构建配置。"])
def test_pure_serialization_and_build_test_keeps_explicit_private_classification(text):
    from businessmap.full_synthesis import FullBusinessSynthesis
    unit = {"kind": "finding", "text": text, "_source_paths": ["repo/tests/SerializationTest.java"],
            "_evidence_ids": ["private-evidence"], "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    result = FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "internal", "classification": "test", "reason": "纯序列化与构建测试"}]}, [unit])
    basis = result["assignments"][0]["classification_basis"]
    assert basis == {"supported_categories": ["test"], "evidence_ids": ["private-evidence"]}


@pytest.mark.parametrize("text,paths,classification", [
    ("内部表单样式将输入示例文字设为灰色，实际填值及已选值设为深色；下拉框占位文字也用深色，选中选项以橙色突出。",
     ["repo/src/styles/el-override.css"], "technical"),
    ("内部界面规范以橙色作为主色，统一配置文字、背景、间距与字号，并提供六种标签配色；未见配色与具体业务状态的对应关系。",
     ["repo/src/styles/base.css", "repo/src/styles/design-tokens.css", "repo/src/styles/prototype.css"], "configuration"),
    ("页面样式将占位文字设为蓝色；选中选项以红色突出。", ["repo/styles/theme.scss"], "technical"),
])
def test_stylesheet_presentation_statements_have_grounded_internal_classification(text, paths, classification):
    from businessmap.full_synthesis import FullBusinessSynthesis
    unit = {"kind": "finding", "text": text, "_source_paths": paths,
            "_evidence_ids": ["private-style-evidence"], "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    item = {"fact_id": "f0", "disposition": "internal", "classification": classification, "reason": "纯界面呈现说明"}
    result = FullBusinessSynthesis._validate({"assignments": [item]}, [unit])
    assert classification in result["assignments"][0]["classification_basis"]["supported_categories"]
    assert result["assignments"][0]["classification_basis"]["evidence_ids"] == ["private-style-evidence"]
    public = {"fact_id": "f0", "disposition": "public", "category": "rules", "title": "界面配色"}
    with pytest.raises(ToolError):
        FullBusinessSynthesis._validate({"assignments": [public]}, [unit])


@pytest.mark.parametrize("paths", [[], ["repo/src/page.ts"], ["repo/styles/theme.css", "repo/src/page.ts"]])
def test_presentation_classification_requires_only_supported_stylesheet_evidence(paths):
    from businessmap.full_synthesis import FullBusinessSynthesis
    unit = {"kind": "finding", "text": "选中选项以橙色突出。", "_source_paths": paths,
            "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    with pytest.raises(ToolError):
        FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "internal",
            "classification": "technical", "reason": "纯界面呈现说明"}]}, [unit])


@pytest.mark.parametrize("path", ["repo/styles/theme.css", "repo/tests/theme.test.css"])
@pytest.mark.parametrize("suffix", [
    "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额；该规则尚无测试覆盖。",
    "选中选项时仅管理员可提交。", "颜色与订单状态的对应关系尚未确认。", "退款条件尚未确认。",
])
def test_stylesheet_origin_cannot_hide_mixed_business_conditions_or_unknowns(path, suffix):
    from businessmap.full_synthesis import FullBusinessSynthesis
    text = "选中选项以橙色突出；" + suffix
    unit = {"kind": "unknown", "text": text, "_source_paths": [path],
            "_fact_id": "private-fact", "_module_ids": ["private-module"]}
    with pytest.raises(ToolError):
        FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "internal",
            "classification": "technical", "reason": "纯界面呈现说明"}]}, [unit])
    result = FullBusinessSynthesis._validate({"assignments": [{"fact_id": "f0", "disposition": "public",
        "category": "rules", "title": "业务条件待确认"}]}, [unit])
    assert "text" not in result["assignments"][0]


@pytest.mark.parametrize("role", [[], {}])
def test_malformed_role_is_rejected_without_breaking_workspace_enumeration(tmp_path, role):
    from businessmap.business_content import validate_public_business_text
    content = "---\n" + yaml.safe_dump({"title": "异常资料", "content_role": role}, allow_unicode=True) + "---\n\n正文。"
    with pytest.raises(ToolError):
        validate_public_business_text(content)
    store = Store(tmp_path / "data")
    try:
        store.open_vault(str(tmp_path / "vault"))
        (store.vault / "异常资料.md").write_text(content)
        store.save_note("正常业务.md", "# 正常业务\n\n退款需审批。")
        assert {note["path"] for note in store.notes()} == {"正常业务.md"}
        assert store.workspace()["stats"]["business_notes"] == 1
        with pytest.raises(CoreError) as error:
            store.read_note("异常资料.md")
        assert error.value.status_code == 404
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["rule", "entity", "process", "note", "overview", "data"])
def test_legacy_business_types_remain_readable_without_accepting_source_metadata(tmp_path, kind):
    store = Store(tmp_path / "data")
    try:
        store.open_vault(str(tmp_path / "vault"))
        content = f"---\ntitle: 退款条件\ntype: {kind}\n---\n\n# 退款条件\n退款金额不得超过已支付金额。"
        (store.vault / "退款条件.md").write_text(content)
        assert store.read_note("退款条件.md")["content"] == content
        assert store.workspace()["stats"]["business_notes"] == 1
        polluted = content.replace(f"type: {kind}", f"type: {kind}\nsource_commit: private")
        (store.vault / "不公开资料.md").write_text(polluted)
        assert {note["path"] for note in store.notes()} == {"退款条件.md"}
        with pytest.raises(CoreError):
            store.read_note("不公开资料.md")
    finally:
        store.close()


def synthesis_fixture(project_tools):
    analysis, service, project, _ = project_tools
    snapshot = analysis.snapshot(project["id"])
    run = {"run_id": "private-run", "modules": [{"id": "private-module"}], "files": []}
    chunks = [{"id": "private-chunk", "module_id": "private-module", "status": "completed", "result": {
        "title": "退款业务", "summary": "订单退款应遵守支付状态和金额条件。", "findings": [
            {"text": "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额。", "evidence_ids": ["private-evidence"]},
            {"text": "RefundServiceImpl.refund() 使用内部缓存。", "evidence_ids": ["private-evidence"]}],
        "unknowns": ["超过 30 天的订单是否允许退款尚未确认。"]}}]
    return service, snapshot, run, chunks


def public_model(monkeypatch, service, *, omit=False, pollution=None, title_pollution=None):
    requests = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        requests.append(payload)
        if any("text" not in item for item in payload["items"]):
            return {"text": json.dumps({"domains": [{"name": "退款条件", "summary": pollution or "退款依据支付状态处理。",
                "module_ids": [item["module_id"] for item in payload["items"]]}], "flows": [], "unknowns": []}), "usage": {}}
        assignments = []
        for item in payload["items"]:
            if "RefundServiceImpl" in item["text"]:
                assignments.append({"fact_id": item["fact_id"], "disposition": "internal", "classification": "technical", "reason": "纯实现细节"})
            else:
                assignments.append({"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
                                    "title": title_pollution or "退款条件"})
        if omit:
            assignments.pop()
        return {"text": json.dumps({"assignments": assignments}, ensure_ascii=False), "usage": {"input_tokens": 20, "output_tokens": 30}}
    monkeypatch.setattr(service.providers, "complete", complete)
    return requests


def test_every_fact_and_unknown_is_public_or_explicitly_internal_without_module_cards(project_tools, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_fixture(project_tools)
    public_model(monkeypatch, service)
    result = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert result["complete"]
    documents = result.get("public_documents")
    assert documents is not None, "complete projection must account for each source fact"
    assert len(documents) == 1 and documents[0]["title"] == "退款条件"
    assert chunks[0]["result"]["findings"][0]["text"] in documents[0]["body"]
    assert chunks[0]["result"]["unknowns"][0] in documents[0]["body"]
    coverage = result["semantic_coverage"]
    assert len(coverage["input_ids"]) == 4
    assert set(coverage["input_ids"]) == set(coverage["decisions"])
    internal = {fid: row for fid, row in coverage["decisions"].items() if row["parts"][0]["outcome"] == "technical"}
    assert len(internal) == 1
    internal_id = next(iter(internal))
    assert coverage["inputs"][internal_id]["text"] == "RefundServiceImpl.refund() 使用内部缓存。"
    assert "module:" not in result["overview_body"] and "private-" not in json.dumps(documents)
    assert "private-evidence" in json.dumps(result["source_mapping"])


def test_missing_fact_assignment_refuses_complete_projection(project_tools, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_fixture(project_tools)
    public_model(monkeypatch, service, omit=True)
    with pytest.raises(ToolError, match="事实"):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)


def test_nonempty_source_receipts_cannot_be_complete_with_no_analyzed_statements(project_tools, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_fixture(project_tools)
    chunks[0]["result"].update(summary="", findings=[], unknowns=[])
    public_model(monkeypatch, service)
    with pytest.raises(ToolError, match="事实|说明"):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)


def test_projection_does_not_copy_whole_repository_file_list_per_fact(project_tools, monkeypatch):
    import tracemalloc
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, _ = synthesis_fixture(project_tools)
    run["files"] = [{"path": f"src/config/setting-{index}.yml"} for index in range(5000)]
    chunks = [{"id": f"chunk-{index}", "module_id": "private-module", "status": "completed", "result": {
        "title": "内部配置", "summary": "", "findings": [{"text": "内部技术配置占位。", "evidence_ids": []}], "unknowns": []}} for index in range(200)]
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "internal",
            "classification": "configuration", "reason": "内部配置"} for item in json.loads(user)["items"]]}), "usage": {}}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    tracemalloc.start()
    try:
        result = synthesis.build(snapshot, run, chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["complete"] and sum(row["parts"][0]["outcome"] == "technical" for row in result["semantic_coverage"]["decisions"].values()) == 200
    assert peak < 7_000_000, f"whole-source metadata multiplied across facts: {peak} bytes"


def test_technical_pollution_in_public_topic_is_rejected_before_checkpoint(project_tools, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_fixture(project_tools)
    public_model(monkeypatch, service, title_pollution="src/private/RefundService.java")
    with pytest.raises(ToolError):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)
    with service.store._db() as db:
        assert db.execute("SELECT COUNT(*) FROM full_analysis_synthesis").fetchone()[0] == 0


def one_public_projection(monkeypatch, body=None):
    from businessmap.full_synthesis import FullBusinessSynthesis
    def build(_self, snapshot, run, chunks):
        from business_projection_fixture import projection_from_units, seal
        from businessmap.full_synthesis import source_fact_units
        from businessmap.business_semantics import semantic_input_hash
        units = source_fact_units(snapshot, run, chunks)
        routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "rules", "title": "订单支付条件", "text": unit["text"]} for unit in units]
        result = projection_from_units(snapshot, {**run, "semantic_input_hash": semantic_input_hash(snapshot, run, chunks, [])}, units, routing)
        old_key = result["public_documents"][0]["key"]
        result["public_documents"][0]["key"] = "topic:payments"
        if body:
            result["public_documents"][0]["body"] = body
        result["semantic_coverage"]["catalogue"][0]["id"] = "operation:payments"
        for claim in result["semantic_coverage"]["claims"]:
            claim["operation_id"] = "operation:payments"
        result["source_mapping"]["topic:payments"] = result["source_mapping"].pop(old_key)
        return seal(result)
    monkeypatch.setattr(FullBusinessSynthesis, "build", build)


def test_full_publication_writes_semantic_topics_and_private_provenance(full_project, monkeypatch):
    full, service, project = full_project
    source_model(monkeypatch, service)
    one_public_projection(monkeypatch)
    result = full.analyze(project["id"])
    assert result["status"] == "completed" and result["publication_complete"]
    state = service.store.config_get("project_analysis:" + project["id"])
    assert set(state["notes"]) == {"topic:payments", "overview", "summary"}
    assert state["notes"]["topic:payments"]["path"] == "01-Wiki/完整赛事/规则/订单支付条件.md"
    assert state["notes"]["overview"]["path"] == "02-Areas/业务地图/完整赛事/业务地图.md"
    assert state["notes"]["summary"]["path"] == "03-Projects/业务梳理/完整赛事/分析总结.md"
    for entry in state["notes"].values():
        content = (service.store.vault / entry["path"]).read_text()
        assert "source://" not in content and "src/" not in content and project["id"] not in content
        assert "完整源码" not in content and "模块" not in content
        assert set(yaml.safe_load(content.split("---", 2)[1])) <= {"title", "type", "content_role", "tags", "generated"}
    assert state["notes"]["topic:payments"]["source_paths"]


def test_full_publication_refuses_legacy_module_only_synthesis(full_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    full, service, project = full_project
    source_model(monkeypatch, service)
    monkeypatch.setattr(FullBusinessSynthesis, "build", lambda *_args: {"complete": True,
        "overview_body": "# 业务地图\n[[module:old|旧模块]]", "module_titles": {}, "domains": [], "usage": {}})
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not result.get("publication_complete")
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))


def test_topic_cannot_claim_reserved_map_identity_and_drop_business_facts(full_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    full, service, project = full_project
    source_model(monkeypatch, service)
    one_public_projection(monkeypatch)
    original = FullBusinessSynthesis.build
    def forged(self, snapshot, run, chunks):
        result = original(self, snapshot, run, chunks)
        result["public_documents"][0]["key"] = "overview"
        result["source_mapping"]["overview"] = result["source_mapping"].pop("topic:payments")
        return result
    monkeypatch.setattr(FullBusinessSynthesis, "build", forged)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not result.get("publication_complete")
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))


def test_publication_blocks_actual_short_commit_even_without_technical_label(full_project):
    full, _, project = full_project
    snapshot = full.project_analysis.snapshot(project["id"])
    commit = snapshot["repositories"][0]["commit"]
    with pytest.raises(ToolError):
        full._render(snapshot, {"run_id": "private-run", "files": []}, "overview", "赛事地图", "# 赛事地图\n本次依据" + commit[:8] + "确认业务。", [])


def test_full_topic_human_edit_is_preserved_and_map_excludes_candidate(full_project, monkeypatch):
    full, service, project = full_project
    source_model(monkeypatch, service)
    one_public_projection(monkeypatch)
    assert full.analyze(project["id"])["status"] == "completed"
    topic = service.store.vault / "01-Wiki/完整赛事/规则/订单支付条件.md"
    human = topic.read_text() + "\n人工补充：高额支付需业务复核。\n"
    topic.write_text(human)
    result = full.analyze(project["id"])
    assert result["status"] == "completed" and not result["publication_complete"]
    assert topic.read_text() == human
    assert any(item["path"] == "01-Wiki/完整赛事/规则/订单支付条件.md" for item in result["conflicts"])
    assert "[[01-Wiki/完整赛事/规则/订单支付条件" not in (service.store.vault / "02-Areas/业务地图/完整赛事/业务地图.md").read_text()


def test_sampled_legacy_entry_also_exports_only_semantic_business_documents(project_tools, monkeypatch):
    analysis, service, project, _ = project_tools
    sampled_model(monkeypatch, service)
    result = analysis.analyze_sampled(project["id"])
    assert result["notes_created"] == 3
    state = service.store.config_get("project_analysis:" + project["id"])
    assert state["notes"]["rules"]["path"] == "01-Wiki/天梯赛事/规则/支付规则.md"
    for entry in state["notes"].values():
        content = (service.store.vault / entry["path"]).read_text()
        assert "source://" not in content and "commit" not in content
        assert "front" not in content and "back" not in content and project["id"] not in content
        assert set(yaml.safe_load(content.split("---", 2)[1])) <= {"title", "type", "content_role", "tags", "generated"}
