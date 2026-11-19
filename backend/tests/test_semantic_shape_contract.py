"""Mechanical generation contracts, exercised without storage or model calls."""
from copy import deepcopy
from itertools import combinations
import json

from jsonschema import Draft202012Validator
import pytest

from businessmap.business_semantics import semantic_output_schema
from businessmap.codex_provider import _request_payload, request_input_bytes


def packet(kinds=("finding", "unknown", "context")):
    return {"stage": "proposal", "items": [{"fact_id": f"f{i}", "kind": kind} for i, kind in enumerate(kinds)],
            "operations": [{"operation_id": "o0"}]}


def claim(cid="c00", kind="rule", section="conditions"):
    return {"id": cid, "operation_id": "o0", "kind": kind, "text": "业务说明", "support_ids": ["f0"],
            "scope": "门店", "conditions": [], "section": section}


def part(outcome="technical", claim_ids=(), resolved_by_ids=()):
    return {"outcome": outcome, "claim_ids": list(claim_ids), "support_ids": ["f0"],
            "resolved_by_ids": list(resolved_by_ids), "rationale": "明确逐项去向"}


def proposal():
    return {"operations": [{"operation_id": "o0", "purpose": "", "summary": "", "support_ids": []}],
            "claims": [claim(), claim("c30", "unknown", "unknowns")],
            "decisions": [{"input_id": "f0", "parts": [part("claim", ["c00"])]},
                          {"input_id": "f1", "parts": [part("resolved", ["c00"], ["f0"]), part("retained", ["c30"])]},
                          {"input_id": "f2", "parts": [part("claim", ["c00"])]}]}


@pytest.mark.parametrize("case", ["finding_resolved", "unknown_positive", "unknown_claim", "retained_positive",
    "unknown_section", "positive_section", "wrong_prefix", "legacy_generation_id", "nonresolved_resolvers",
    "nonfinding_resolver", "empty_resolvers", "private_claim_refs", "empty_claim_refs", "empty_parts"])
def test_new_proposal_schema_rejects_mechanical_counterexamples(case):
    # Each mutation is a contract violation previously left for runtime rejection.
    value = proposal()
    if case == "finding_resolved": value["decisions"][0]["parts"] = [part("resolved", (), ["f0"])]
    elif case == "unknown_positive": value["decisions"][1]["parts"] = [part("claim", ["c00"])]
    elif case == "unknown_claim": value["decisions"][1]["parts"] = [part("claim", ["c30"])]
    elif case == "retained_positive": value["decisions"][1]["parts"] = [part("retained", ["c00"])]
    elif case == "unknown_section": value["claims"][1]["section"] = "results"
    elif case == "positive_section": value["claims"][0]["section"] = "unknowns"
    elif case == "wrong_prefix": value["claims"][0]["id"] = "c10"
    elif case == "legacy_generation_id": value["claims"][0]["id"] = "c0"
    elif case == "nonresolved_resolvers": value["decisions"][0]["parts"][0]["resolved_by_ids"] = ["f0"]
    elif case == "nonfinding_resolver": value["decisions"][1]["parts"][0]["resolved_by_ids"] = ["f2"]
    elif case == "empty_resolvers": value["decisions"][1]["parts"][0]["resolved_by_ids"] = []
    elif case == "private_claim_refs": value["decisions"][2]["parts"] = [part("technical", ["c00"])]
    elif case == "empty_claim_refs": value["decisions"][0]["parts"][0]["claim_ids"] = []
    elif case == "empty_parts": value["decisions"][0]["parts"] = []
    assert not Draft202012Validator(semantic_output_schema("proposal", packet())).is_valid(value), case


def test_mixed_unknown_and_context_positive_survive_reordered_decisions():
    value = proposal()
    value["decisions"].reverse()
    Draft202012Validator(semantic_output_schema("proposal", packet())).validate(value)


@pytest.mark.parametrize("kind,prefix", [("rule", "c0"), ("step", "c1"), ("result", "c2"), ("unknown", "c3")])
@pytest.mark.parametrize("section", ["who", "inputs", "conditions", "results", "unknowns"])
def test_claim_kind_ids_and_sections_match_without_narrowing_business_sections(kind, prefix, section):
    schema = semantic_output_schema("proposal", packet())
    value = {"operations": [], "claims": [claim(prefix + "12345", kind, section)], "decisions": []}
    assert Draft202012Validator(schema).is_valid(value) == ((kind == "unknown") == (section == "unknowns"))


KIND_SUBSETS = [subset for n in range(4) for subset in combinations(("finding", "unknown", "context"), n)]


@pytest.mark.parametrize("kinds", KIND_SUBSETS)
def test_all_input_kind_subsets_and_empty_arrays_need_no_fake_references(kinds):
    schema = semantic_output_schema("proposal", packet(kinds))
    Draft202012Validator.check_schema(schema)
    value = {"operations": [], "claims": [], "decisions": [
        {"input_id": f"f{i}", "parts": [{**part(), "support_ids": [f"f{i}"]}]} for i in range(len(kinds))]}
    validator = Draft202012Validator(schema)
    validator.validate(value)
    validator.validate({"operations": [], "claims": [], "decisions": []})
    if "finding" not in kinds:
        for row in value["decisions"]:
            invalid = deepcopy(value)
            invalid["decisions"] = [{"input_id": row["input_id"], "parts": [part("resolved", (), [row["input_id"]])]}]
            assert not validator.is_valid(invalid)
    def walk(node):
        if isinstance(node, dict):
            assert node.get("enum") != [] and node.get("anyOf") != []
            for child in node.values(): walk(child)
        elif isinstance(node, list):
            for child in node: walk(child)
    walk(schema)


def test_answer_wrapper_hoists_definitions_without_mutating_caller_and_counts_wire_once():
    schema = semantic_output_schema("proposal", packet())
    frozen = deepcopy(schema)
    messages = [{"role": "user", "content": "中文资料"}]
    prompt, wire, _ = _request_payload(messages, 16000, schema)
    wrapped = json.loads(wire)
    assert "$defs" in wrapped and "$defs" not in wrapped["properties"]["answer"]
    assert wrapped["$defs"] == frozen["$defs"]
    Draft202012Validator.check_schema(wrapped)
    Draft202012Validator(wrapped).validate({"answer": proposal()})
    invalid = proposal(); invalid["claims"][0]["id"] = "c10"
    assert not Draft202012Validator(wrapped).is_valid({"answer": invalid})
    assert schema == frozen
    expected = {"type": "object", "properties": {"answer": {k: v for k, v in frozen.items() if k != "$defs"}},
                "required": ["answer"], "additionalProperties": False, "$defs": frozen["$defs"]}
    assert wire == json.dumps(expected, allow_nan=False)
    assert request_input_bytes(messages, max_output=16000, output_schema=schema) == len(prompt.encode("utf-8")) + len(json.dumps(expected, allow_nan=False).encode("utf-8"))


def test_legacy_answer_wire_is_unchanged():
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "资料"}]
    prompt, wire, budget = _request_payload(messages, 2800)
    assert budget == 44800
    assert prompt == json.dumps({"instructions": "只分析提供的消息资料，不执行命令或工具，不读取本地文件。按消息要求回答；把最终正文完整放入answer字符串。如果要求JSON，answer内容必须是该JSON原文。正文尽量不超过指定token预算。",
        "max_output_tokens": 2800, "messages": messages}, ensure_ascii=False)
    assert wire == '{"type": "object", "properties": {"answer": {"type": "string", "maxLength": 44800}}, "required": ["answer"], "additionalProperties": false}'


def test_runtime_receipt_normalization_still_accepts_legacy_claim_ids():
    from businessmap.business_semantics import BusinessSemanticIntegration
    value = {"operations": [{"operation_id": "o0", "purpose": "创建门店方案。", "summary": "须具备维护权限。", "support_ids": ["f0"]}],
             "claims": [claim("c0")], "decisions": [{"input_id": "f0", "parts": [part("claim", ["c0"])]}]}
    refs = {"f0": {"_fact_id": "fact:permission", "kind": "finding", "text": "有维护权限的人可创建门店方案。"}}
    operations = {"o0": {"fact_ids": ["fact:permission"]}}
    assert BusinessSemanticIntegration._proposal(value, refs, operations) == value
