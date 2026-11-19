from __future__ import annotations

from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from businessmap.core import Store
from businessmap.providers import ToolError
from support_graph_fixture import positive_graph


@pytest.fixture
def semantic_case(tmp_path):
    store = Store(tmp_path / "data")
    snapshot = {"project": {"id": "project", "name": "门店运营", "revision": "project-revision"},
                "provider": {"id": "provider", "kind": "openai-compatible", "base_url": "http://127.0.0.1:1/v1", "model": "fixture"},
                "policies": {}, "vault_path": str(tmp_path / "vault")}
    run = {"run_id": "run", "snapshot_id": "snapshot", "project_revision": "project-revision", "source_repositories": []}
    units = [
        {"_fact_id": "fact:limit", "kind": "finding", "text": "有维护权限且能管理目标门店的人可创建方案；每家门店最多5套。", "chunk_id": "source-rule",
         "_evidence_ids": ["e1"], "_source_paths": ["internal/rules.java"], "_forbidden": [],
         "_observation": {"scope": "source_subset", "roles": ["backend"], "match_receipt_ids": []}},
        {"_fact_id": "fact:unknown", "kind": "unknown", "text": "本组件未见额度检查；并发创建是否仍守住5套上限尚未验证。", "chunk_id": "source-form",
         "_evidence_ids": ["e2"], "_source_paths": ["internal/form.vue"], "_forbidden": [],
         "_observation": {"scope": "source_subset", "roles": ["frontend"], "match_receipt_ids": []}},
        {"_fact_id": "fact:cache", "kind": "finding", "text": "内部权限缓存有效期60秒，读取异常时重新加载，并提供清理能力。", "chunk_id": "source-cache",
         "_evidence_ids": ["e3"], "_source_paths": ["internal/cache.java"], "_forbidden": [],
         "_observation": {"scope": "source_subset", "roles": ["backend"], "match_receipt_ids": []}},
    ]
    routing = [{"fact_id": u["_fact_id"], "disposition": "public", "category": "rules", "title": "方案创建"} for u in units]
    yield store, snapshot, run, units, routing
    store.close()


def _canonical_candidate(payload):
    facts = {item["text"]: item["fact_id"] for item in payload["items"]}
    ids = list(facts.values())
    operation = payload["operations"][0]["operation_id"]
    return {"operations": [{"operation_id": operation, "purpose": "在目标门店创建可使用的方案。", "summary": "创建受操作权限、门店范围及每店数量约束。", "support_ids": [ids[0]]}],
            "claims": [
                {"id": "c00", "operation_id": operation, "kind": "rule", "text": "有维护权限且能管理目标门店的人可创建方案；每家门店最多5套。", "support_ids": [ids[0]], "scope": "目标门店", "conditions": ["有维护权限且可管理门店"], "section": "conditions"},
                {"id": "c30", "operation_id": operation, "kind": "unknown", "text": "并发创建时能否严格保持每店5套上限，尚未验证。", "support_ids": [ids[1]], "scope": "并发创建", "conditions": [], "section": "unknowns"}],
            "decisions": [
                {"input_id": ids[0], "parts": [{"outcome": "claim", "claim_ids": ["c00"], "support_ids": [ids[0]], "resolved_by_ids": [], "rationale": "保留完整权限、范围和数量条件。"}]},
                {"input_id": ids[1], "parts": [{"outcome": "resolved", "claim_ids": ["c00"], "support_ids": [ids[0]], "resolved_by_ids": [ids[0]], "rationale": "常规额度检查已有业务实现说明。"},
                                                  {"outcome": "retained", "claim_ids": ["c30"], "support_ids": [ids[1]], "resolved_by_ids": [], "rationale": "静态检查不证明并发效果。"}]},
                {"input_id": ids[2], "parts": [{"outcome": "technical", "claim_ids": [], "support_ids": [ids[2]], "resolved_by_ids": [], "rationale": "内部缓存维护机制没有业务可操作含义。"}]}]}


def candidate(payload):
    return positive_graph(_canonical_candidate(payload), payload)


def review(payload, *, reject=False):
    proposal = payload["candidate"]
    return {"decision": "reject" if reject else "accept", "issues": [{"code": "condition_loss", "input_ids": [payload["items"][1]["fact_id"]]}] if reject else [],
            "input_checks": [{"input_id": item["fact_id"], "checks": {key: True for key in ("coverage", "conditions", "scope", "uncertainty", "technical_boundary")}} for item in payload["items"]],
            "claim_checks": [{"claim_id": item["id"], "checks": {key: True for key in ("support", "numbers", "scope", "uncertainty")}} for item in proposal["claims"]],
            "operation_checks": [{"operation_id": item["operation_id"], "checks": {key: True for key in ("business_language", "complete_flow", "relationships", "unknowns")}} for item in proposal["operations"]]}


def build_case(case, *, mutate=None, reject=False, guard=lambda: None, provider=None):
    from businessmap.business_semantics import BusinessSemanticIntegration
    store, snapshot, run, units, routing = case
    calls = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        calls.append(payload)
        if provider:
            response = provider(payload, len(calls))
            if response is not None:
                return response
        if payload["stage"] in {"catalogue", "subdivide"}:
            result = {"operations": [{"title": "创建门店方案", "category": "flows", "label_ids": [item["label_id"] for item in payload["items"]]}]}
        elif payload["stage"] == "proposal":
            result = candidate(payload)
            if mutate:
                mutate(result, payload)
        else:
            result = review(payload, reject=reject)
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {"input_tokens": 50, "output_tokens": 30}, "output_limited": False}
    service = SimpleNamespace(store=store, providers=SimpleNamespace(complete=complete))
    return BusinessSemanticIntegration(service, guard=guard), calls, snapshot, run, units, routing


def execute(setup):
    builder, _, snapshot, run, units, routing = setup
    return builder.build(snapshot, run, units=units, routing=routing)


def test_partial_unknown_resolution_preserves_concurrency(semantic_case):
    setup = build_case(semantic_case)
    result = execute(setup)
    text = result["public_documents"][0]["body"]
    assert "有维护权限且能管理目标门店" in text and "每家门店最多5套" in text
    assert "并发创建时能否严格保持每店5套上限，尚未验证" in text
    assert "本组件未见" not in text and "内部权限缓存" not in text
    assert {part["outcome"] for part in result["semantic_coverage"]["decisions"]["fact:unknown"]["parts"]} == {"resolved", "retained"}
    assert result["public_documents"][0]["fact_ids"] == ["fact:limit", "fact:unknown"]
    assert result["source_mapping"][result["public_documents"][0]["key"]]["evidence_ids"] == ["e1", "e2"]


def test_operation_document_and_overview_use_only_accepted_claims(semantic_case):
    result = execute(build_case(semantic_case))
    text = result["public_documents"][0]["body"]
    assert "## 条件与分支" in text and "## 待确认" in text
    assert "创建受操作权限、门店范围及每店数量约束" in result["summary_body"]
    assert "在目标门店创建可使用的方案" in result["overview_body"]
    assert "缓存" not in result["summary_body"]


def test_rejected_review_does_not_publish_or_repeat_paid_calls(semantic_case):
    setup = build_case(semantic_case, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "review", "proposal", "review"]
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 5


@pytest.mark.parametrize("mutation", [
    lambda r, p: r["decisions"].pop(),
    lambda r, p: r["claims"][0].update(support_ids=["forged"]),
    lambda r, p: r["decisions"][1]["parts"][0].update(resolved_by_ids=[p["items"][1]["fact_id"]]),
    lambda r, p: r["operations"][0].update(purpose="源码位于 src/service/Secret.java"),
    lambda r, p: r["claims"][0].update(text="每家门店最多500套，同时原限制为5。"),
])
def test_invalid_coverage_support_privacy_or_numbers_never_reach_review(semantic_case, mutation):
    setup = build_case(semantic_case, mutate=mutation)
    with pytest.raises(ToolError):
        execute(setup)
    assert all(call["stage"] == "proposal" for call in setup[1][1:])
    assert 2 <= len(setup[1]) <= 3


def test_accepted_semantics_are_reused_without_model_calls(semantic_case):
    setup = build_case(semantic_case)
    first = execute(setup)
    second = execute(setup)
    assert len(setup[1]) == 3
    assert first["public_documents"] == second["public_documents"]
    assert second["usage"]["requests"] == 0 and second["usage"]["cache_hits"] == 3


def test_pause_after_response_retains_proposal_without_sending_review(semantic_case):
    paused = False
    def guard():
        if paused:
            raise InterruptedError("paused")
    def provider(payload, count):
        nonlocal paused
        if payload["stage"] == "proposal":
            paused = True
        return None
    setup = build_case(semantic_case, guard=guard, provider=provider)
    with pytest.raises(InterruptedError):
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal"]
    paused = False
    result = execute(setup)
    assert result["complete"] and [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "review"]


def test_hash_ignores_runtime_state_but_binds_addenda_and_override(semantic_case):
    from businessmap.business_semantics import semantic_input_hash
    _, snapshot, run, _, _ = semantic_case
    chunks = [{"id": "chunk", "input_hash": "source-input", "result": {"findings": []}, "segments": []}]
    initial = semantic_input_hash(snapshot, run, chunks, [])
    run.update(status="completed", synthesis={"complete": True}, publication_complete=True, paused=True)
    assert semantic_input_hash(snapshot, run, chunks, []) == initial
    assert semantic_input_hash(snapshot, run, chunks, [{"addendum_id": "extra", "input_hash": "new", "result": {}}]) != initial
    run["catalogue_override_revision"] = "changed"
    assert semantic_input_hash(snapshot, run, chunks, []) != initial


def test_projection_validation_rejects_tampered_public_bytes(semantic_case):
    from businessmap.business_semantics import validate_semantic_projection
    result = execute(build_case(semantic_case))
    validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])
    result["public_documents"][0]["body"] += "\n已证明所有并发均安全。"
    with pytest.raises(ToolError):
        validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])


def test_empty_inputs_have_no_semantic_calls(semantic_case):
    setup = build_case(semantic_case)
    builder, calls, snapshot, run, _, _ = setup
    result = builder.build(snapshot, run, units=[], routing=[])
    assert result["complete"] and result["public_documents"] == [] and calls == []
    assert result["semantic_coverage"]["input_ids"] == []


def test_review_checkpoint_is_bound_to_packet_and_actual_request(semantic_case):
    from businessmap.business_semantics import semantic_task_identity_hash
    setup = build_case(semantic_case)
    result = execute(setup)
    store = semantic_case[0]
    with store._db() as db:
        key, stage, input_hash, encoded = db.execute("SELECT stage_hash,stage,input_hash,data FROM business_semantic_tasks WHERE stage='review'").fetchone()
    data = json.loads(encoded)
    assert key == semantic_task_identity_hash(setup[2], input_hash, stage, data["request"], data["context"])
    assert data["context"]["packet_hash"] == result["verification"]["packet_hashes"][0]
    assert set(data["context"]["input_ids"].values()) == {"fact:limit", "fact:unknown", "fact:cache"}
    assert result["verification"]["accepted_review_hashes"] == [key]


def test_unmatched_roles_stay_pending_when_independent_review_rejects_relation(semantic_case):
    def provider(payload, count):
        if payload["stage"] == "review":
            result = review(payload)
            result["operation_checks"][0]["checks"]["relationships"] = False
            return {"text": json.dumps(result), "usage": {}}
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError):
        execute(setup)
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3


def test_component_scope_is_resolved_by_matching_parent(semantic_case):
    case = deepcopy(semantic_case[1:])
    case[2][0].update(text="父页面接收创建请求并保存，成功后选中新方案。")
    case[2][1].update(text="本组件只提交创建资料；实际运行保存是否成功仍未验证。")
    case[2][0]["_observation"].update(scope="matched_operation", match_receipt_ids=["matched-proof"])
    case[2][1]["_observation"].update(scope="matched_operation", match_receipt_ids=["matched-proof"])
    def mutate(result, payload):
        result["claims"][0]["text"] = "父页面接收创建资料并保存，成功后选中新方案。"
        result["claims"][0]["conditions"] = []
        result["claims"][1]["text"] = "实际运行中能否成功保存，尚未验证。"
    setup = build_case((semantic_case[0], *case), mutate=mutate)
    result = execute(setup)
    text = result["public_documents"][0]["body"]
    assert "成功后选中新方案" in text and "实际运行中能否成功保存，尚未验证" in text
    assert "本组件只提交" not in text


def test_catalogue_without_progress_stops_with_all_members_preserved(semantic_case):
    semantic_case[4][1]["title"] = "方案数量核实"
    semantic_case[4][2]["title"] = "权限缓存"
    setup = build_case(semantic_case)
    setup[0].CATALOGUE_ITEMS = 1
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3 and all(call["stage"] == "catalogue" for call in setup[1])
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3


def test_large_operation_never_truncates_or_sends_partial_facts(semantic_case):
    semantic_case[3][0]["text"] = "完整条件" * 15000
    setup = build_case(semantic_case)
    with pytest.raises(ToolError):
        execute(setup)
    assert setup[1] == []


def test_review_wire_overflow_does_not_resend_proposal(semantic_case):
    setup = None
    def provider(payload, count):
        if payload["stage"] == "proposal":
            setup[0].WIRE_BYTES = 300
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError):
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal"]
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 2


def test_cache_sixty_seconds_does_not_become_business_delay(semantic_case):
    def mutate(result, payload):
        result["claims"][0].update(text="权限变更必定延迟60秒生效。", support_ids=[payload["items"][2]["fact_id"]])
        result["decisions"][0]["parts"][0].update(outcome="source_scope", claim_ids=[])
        result["decisions"][2]["parts"][0].update(outcome="claim", claim_ids=["c00"])
    def provider(payload, count):
        if payload["stage"] == "review":
            result = review(payload, reject=True)
            result["issues"] = [{"code": "unsupported_claim", "input_ids": [payload["items"][2]["fact_id"]]}]
            return {"text": json.dumps(result), "usage": {}}
    setup = build_case(semantic_case, mutate=mutate, provider=provider)
    with pytest.raises(ToolError):
        execute(setup)
    assert setup[1][-1]["stage"] == "review"


def partition_review(payload, *, reject=False):
    return {"decision": "reject" if reject else "accept",
            "issues": [{"code": "condition_loss", "input_ids": [payload["items"][0]["fact_id"]]}] if reject else [],
            "parent_checks": {key: True for key in ("complete_partition", "no_shared_conditions_lost", "no_cross_child_dependencies", "unknowns_preserved")},
            "input_checks": [{"input_id": item["fact_id"], "checks": {key: True for key in ("membership", "conditions", "scope", "uncertainty", "relationships")}} for item in payload["items"]],
            "operation_checks": [{"operation_id": item["operation_id"], "checks": {key: True for key in ("independent_operation", "complete_conditions", "complete_unknowns", "no_cross_child_dependencies")}} for item in payload["children"]]}


def _canonical_independent_candidate(payload):
    operations, claims, decisions = [], [], []
    for operation in payload["operations"]:
        operations.append({"operation_id": operation["operation_id"], "purpose": operation["title"] + "。", "summary": "操作必须满足完整业务条件。", "support_ids": operation["fact_ids"][:1]})
        for fid in operation["fact_ids"]:
            item = next(item for item in payload["items"] if item["fact_id"] == fid)
            cid = "c0" + str(len(claims))
            claims.append({"id": cid, "operation_id": operation["operation_id"], "kind": "unknown" if item["kind"] == "unknown" else "rule", "text": item["text"], "support_ids": [fid], "scope": "目标门店", "conditions": [], "section": "unknowns" if item["kind"] == "unknown" else "conditions"})
            decisions.append({"input_id": fid, "parts": [{"outcome": "retained" if item["kind"] == "unknown" else "claim", "claim_ids": [cid], "support_ids": [fid], "resolved_by_ids": [], "rationale": "完整保留本操作的业务条件。"}]})
    return {"operations": operations, "claims": claims, "decisions": decisions}


def independent_candidate(payload):
    return positive_graph(_canonical_independent_candidate(payload), payload)


def oversized_case(semantic_case, *, independent, reject=False):
    store, snapshot, run, _, _ = semantic_case
    titles = ["创建门店方案", "调整预约时间", "取消服务预约", "确认服务完成"] if independent else ["填写申请资料", "核对门店资质", "核对操作权限", "确认创建提交"]
    units = [{"_fact_id": "fact:" + str(i), "kind": "finding", "text": (titles[i // 10] if independent else "门店创建方案") + "必须满足条件" + str(i) + "，否则不允许操作。",
              "_evidence_ids": ["e" + str(i)], "_source_paths": ["private/source"], "_forbidden": []} for i in range(40)]
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "rules", "title": "方案创建"} for unit in units]
    def provider(payload, count):
        if payload["stage"] == "subdivide":
            ids = [item["label_id"] for item in payload["items"]]
            assert len(ids) == 40
            result = {"operations": [{"title": titles[i // 10], "category": "rules", "label_ids": ids[i:i + 10]} for i in range(0, 40, 10)]}
            return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
        if payload["stage"] == "partition_review":
            assert len(payload["items"]) == 40 and len(payload["children"]) == 4
            assert {item["text"] for item in payload["items"]} == {unit["text"] for unit in units}
            result = partition_review(payload, reject=reject)
            return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
        if payload["stage"] == "proposal":
            assert len(payload["items"]) <= 10
            return {"text": json.dumps(independent_candidate(payload), ensure_ascii=False), "usage": {}}
    setup = build_case((store, snapshot, run, units, routing), provider=provider)
    setup[0].CONCURRENCY = 1
    return setup


def test_forty_shared_creation_conditions_are_not_four_complete_operations(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "subdivide", "partition_review"]
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3


def test_forty_facts_form_four_independently_verified_complete_operations(semantic_case):
    setup = oversized_case(semantic_case, independent=True)
    result = execute(setup)
    assert len(result["public_documents"]) == 4
    assert [call["stage"] for call in setup[1]][:3] == ["catalogue", "subdivide", "partition_review"]
    assert len(result["semantic_coverage"]["catalogue_roots"]) == 1
    partitions = result["semantic_coverage"]["partitions"]
    assert len(partitions) == 1 and len(partitions[0]["parent"]["fact_ids"]) == 40
    assert len(partitions[0]["children"]) == 4
    assert result["verification"]["accepted_partition_review_hashes"] == [partitions[0]["review_hash"]]
    for unit in setup[4]:
        assert sum(unit["text"] in doc["body"] for doc in result["public_documents"]) == 1
    assert execute(setup)["usage"]["requests"] == 0


@pytest.mark.parametrize("mutation", [
    lambda result: result["input_checks"].pop(),
    lambda result: result["parent_checks"].update(no_shared_conditions_lost=False),
    lambda result: result["operation_checks"][0]["checks"].update(no_cross_child_dependencies=False),
    lambda result: result.update(parent_checks={"independent": True}),
])
def test_partition_requires_every_full_parent_input_and_dependency_check(semantic_case, mutation):
    setup = oversized_case(semantic_case, independent=True)
    provider = setup[0].service.providers.complete
    def incomplete_review(system, user, **kwargs):
        response = provider(system, user, **kwargs)
        if json.loads(user)["stage"] == "partition_review":
            result = json.loads(response["text"])
            mutation(result)
            response["text"] = json.dumps(result)
        return response
    setup[0].service.providers.complete = incomplete_review
    for _ in range(2):
        with pytest.raises(ToolError):
            execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "subdivide", "partition_review"]


def test_complete_partition_review_over_budget_stops_all_child_proposals(semantic_case):
    setup = oversized_case(semantic_case, independent=True)
    for unit in setup[4]:
        unit["_observation"] = {"scope": "source_subset", "private_context": "x" * 500}
    addenda = [{"matching_receipts": [{"id": "synthetic-static-proof", "private_context": "x" * 8000}]}]
    for _ in range(2):
        with pytest.raises(ToolError) as failure:
            setup[0].build(setup[2], setup[3], units=setup[4], routing=setup[5], addenda=addenda)
        assert failure.value.reason_code == "input_limit"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "subdivide"]


def test_partition_proof_binds_full_parent_and_cannot_be_removed_when_resealed(semantic_case):
    from businessmap.business_semantics import _projection_hash, semantic_task_identity_hash, validate_semantic_projection
    setup = oversized_case(semantic_case, independent=True)
    result = execute(setup)
    partition = result["semantic_coverage"]["partitions"][0]
    with semantic_case[0]._db() as db:
        key, stage, input_hash, status, encoded = db.execute("SELECT stage_hash,stage,input_hash,status,data FROM business_semantic_tasks WHERE stage='partition_review'").fetchone()
    data = json.loads(encoded)
    assert status == "accepted" and key == partition["review_hash"]
    assert key == semantic_task_identity_hash(setup[2], input_hash, stage, data["request"], data["context"])
    assert data["context"]["parent_operation"] == partition["parent"]
    assert data["context"]["subdivision_hash"] == partition["subdivision_hash"]
    assert set(data["context"]["input_ids"].values()) == {"fact:" + str(i) for i in range(40)}
    result["semantic_coverage"]["partitions"] = []
    result["verification"]["accepted_partition_review_hashes"] = []
    result["verification"]["projection_hash"] = _projection_hash(result)
    with pytest.raises(ToolError):
        validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])


def forty_four_fact_case(semantic_case, *, honor_budget):
    store, snapshot, run, _, _ = semantic_case
    titles = ["维护积分规则", "维护奖励标准", "维护预约规则", "维护通知模板"]
    units = [{"_fact_id": "fact:" + str(i), "kind": "finding",
              "text": titles[i // 11] + "需要满足业务条件" + str(i) + "，不符合时不得提交。",
              "_evidence_ids": ["e" + str(i)], "_source_paths": ["private/source"], "_forbidden": []} for i in range(44)]
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "rules", "title": "维护门店运营配置"} for unit in units]
    def provider(payload, count):
        if payload["stage"] == "subdivide":
            items = payload["items"]
            if honor_budget and "child_budget" in payload:
                budget = payload["child_budget"]
                groups = [items[i:i + 11] for i in range(0, 44, 11)]
                for group in groups:
                    assert budget["operation_overhead"] + sum(item["candidate_byte_cost"] for item in group) <= budget["candidate_bytes"]
            else:
                groups = [items[:20], items[20:28], items[28:36], items[36:]]
            result = {"operations": [{"title": title, "category": "rules", "label_ids": [item["label_id"] for item in group]} for title, group in zip(titles, groups)]}
            return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
        if payload["stage"] == "partition_review":
            return {"text": json.dumps(partition_review(payload)), "usage": {}}
        if payload["stage"] == "proposal":
            return {"text": json.dumps(independent_candidate(payload), ensure_ascii=False), "usage": {}}
    setup = build_case((store, snapshot, run, units, routing), provider=provider)
    setup[0].CONCURRENCY = 1
    return setup


def test_subdivision_capacity_signal_keeps_forty_four_facts_in_complete_bounded_operations(semantic_case):
    setup = forty_four_fact_case(semantic_case, honor_budget=True)
    result = execute(setup)
    assert len(result["public_documents"]) == 4
    assert [call["stage"] for call in setup[1]][:3] == ["catalogue", "subdivide", "partition_review"]
    request = setup[1][1]
    assert request["child_budget"] == {"candidate_bytes": 8000, "operation_overhead": 500, "input_bytes": 37000, "max_output_tokens": 6000, "review_candidate_reserve_bytes": 8000}
    assert len(request["items"]) == 44
    for item, unit in zip(request["items"], setup[4]):
        assert item["text"] == unit["text"]
        assert item["candidate_byte_cost"] == len(unit["text"].encode()) + 420
        assert sum(unit["text"] in document["body"] for document in result["public_documents"]) == 1


def test_oversized_twenty_fact_child_is_rejected_before_subdivision_checkpoint_acceptance(semantic_case):
    setup = forty_four_fact_case(semantic_case, honor_budget=False)
    for _ in range(2):
        with pytest.raises(ToolError) as failure:
            execute(setup)
        assert failure.value.reason_code == "subdivision_limit"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "subdivide"]
    with semantic_case[0]._db() as db:
        status, reason = db.execute("SELECT status,error_code FROM business_semantic_tasks WHERE stage='subdivide'").fetchone()
    assert (status, reason) == ("needs_review", "subdivision_limit")


def test_codex_keeps_forty_four_coupled_conditions_and_unknown_atomic(semantic_case):
    store, snapshot, run, _, _ = semantic_case
    snapshot["provider"].update(kind="codex-local", base_url="codex://local")
    units = [{"_fact_id": "fact:" + str(i), "kind": "finding", "text": "有维护权限且可管理目标门店的人创建方案时必须满足共同条件" + str(i) + "，不符合时不得创建。",
              "_evidence_ids": ["e" + str(i)], "_source_paths": ["private/source"], "_forbidden": []} for i in range(44)]
    units[-1].update(kind="unknown", text="并发创建能否同时守住所有条件，以及人工实际使用是否成功，尚未验证。")
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "flows", "title": "创建门店方案"} for unit in units]
    outputs = []
    def provider(payload, count):
        if payload["stage"] == "proposal":
            result = independent_candidate(payload)
            outputs.append(len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()))
            return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
    setup = build_case((store, snapshot, run, units, routing), provider=provider)
    complete = setup[0].service.providers.complete
    limits = []
    def record_limits(system, user, **kwargs):
        limits.append((json.loads(user)["stage"], kwargs["max_output"]))
        return complete(system, user, **kwargs)
    setup[0].service.providers.complete = record_limits
    result = execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "review"]
    assert limits == [("catalogue", 6000), ("proposal", 16000), ("review", 16000)]
    assert len(result["public_documents"]) == 1 and 8000 < outputs[0] <= 32000
    assert result["semantic_coverage"]["partitions"] == []
    for unit in units:
        assert unit["text"] in result["public_documents"][0]["body"]
    assert result["semantic_coverage"]["decisions"]["fact:43"]["parts"][0]["outcome"] == "retained"
    assert setup[1][1]["budget"]["candidate_bytes"] == 40000


def test_budget_profiles_use_provider_kind_and_remain_inside_codex_adapter(semantic_case):
    from businessmap.business_semantics import budget_profile, semantic_stage_limits
    from businessmap.codex_provider import MAX_INPUT_BYTES
    provider = semantic_case[1]["provider"]
    http = budget_profile(provider)
    provider.update(model="codex-large-999", budget_profile={"candidate_bytes": 999999})
    assert budget_profile(provider) == http
    assert http["candidate_bytes"] == 8000
    for stage in ("catalogue", "subdivide", "proposal", "partition_review", "review"):
        assert semantic_stage_limits(http, stage) == {"input_bytes": 37000, "max_output_tokens": 6000}
    provider.update(kind="codex-local", base_url="codex://local")
    codex = budget_profile(semantic_case[1])
    assert codex["candidate_bytes"] == 40000 and codex["id"] != http["id"]
    assert semantic_stage_limits(codex, "catalogue") == {"input_bytes": 64000, "max_output_tokens": 6000, "timeout_seconds": 180}
    assert semantic_stage_limits(codex, "subdivide") == {"input_bytes": 64000, "max_output_tokens": 6000, "timeout_seconds": 180}
    assert semantic_stage_limits(codex, "proposal") == {"input_bytes": 64000, "max_output_tokens": 16000, "timeout_seconds": 360}
    for stage in ("partition_review", "review"):
        assert semantic_stage_limits(codex, stage) == {"input_bytes": 160000, "max_output_tokens": 16000, "timeout_seconds": 360}
    assert max(semantic_stage_limits(codex, stage)["input_bytes"] for stage in codex["stages"]) < MAX_INPUT_BYTES
    provider.update(base_url="http://127.0.0.1:1")
    assert budget_profile(provider) == http
    provider.update(kind="unknown")
    assert budget_profile(provider) == http


def test_hashes_bind_all_trusted_budget_limits(semantic_case, monkeypatch):
    import businessmap.business_semantics as semantics
    _, snapshot, run, _, _ = semantic_case
    base_input = semantics.semantic_input_hash(snapshot, run, [], [])
    base_stage = semantics.semantic_task_identity_hash(snapshot, base_input, "catalogue", {"stage": "catalogue"}, {})
    profile = semantics.budget_profile(snapshot)
    original = semantics.budget_profile
    for change in ("id", "candidate_bytes", "catalogue_input", "review_output"):
        changed = deepcopy(profile)
        if change == "id":
            changed["id"] += "-new-policy"
        elif change == "candidate_bytes":
            changed["candidate_bytes"] -= 1
        elif change == "catalogue_input":
            changed["stages"]["catalogue"]["input_bytes"] -= 1
        else:
            changed["stages"]["review"]["max_output_tokens"] -= 1
        monkeypatch.setattr(semantics, "budget_profile", lambda value, changed=changed: deepcopy(changed))
        assert semantics.semantic_input_hash(snapshot, run, [], []) != base_input
        assert semantics.semantic_task_identity_hash(snapshot, base_input, "catalogue", {"stage": "catalogue"}, {}) != base_stage
    monkeypatch.setattr(semantics, "budget_profile", original)


def test_runtime_budget_overrides_can_only_lower_profile_caps(semantic_case):
    setup = build_case(semantic_case)
    setup[0].CANDIDATE_BYTES = 999999
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.reason_code == "input_invalid" and setup[1] == []


@pytest.mark.parametrize("stage,cap", [("catalogue", 64000), ("subdivide", 64000), ("proposal", 64000), ("partition_review", 160000), ("review", 160000)])
def test_codex_stage_input_cap_is_enforced_before_provider_admission(semantic_case, stage, cap):
    import businessmap.business_semantics as semantics
    from businessmap.codex_provider import request_input_bytes
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    execute(setup)
    sent = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        sent.append(request_input_bytes([{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_output=kwargs["max_output"], output_schema=kwargs["output_schema"]))
        return {"text": '{"ok":true}', "usage": {}}
    setup[0].service.providers.complete = complete
    system = {"catalogue": semantics.CATALOGUE_SYSTEM, "subdivide": semantics.CATALOGUE_SYSTEM,
              "proposal": semantics.PROPOSAL_SYSTEM, "partition_review": semantics.PARTITION_REVIEW_SYSTEM, "review": semantics.REVIEW_SYSTEM}[stage]
    context = {}
    payload = {"stage": stage, "padding": "", "items": [{"label_id": "l0", "fact_id": "f0"}],
               "operations": [{"operation_id": "o0"}], "children": [{"operation_id": "o0"}], "candidate": {"claims": []}}
    if stage in {"proposal", "review"}:
        rows = checkpoint_rows(semantic_case[0])
        row = next(row for row in rows.values() if row['stage'] == stage)
        payload, context = deepcopy(row['data']['request']), deepcopy(row['data']['context'])
        payload['padding'] = ''
        if stage == 'review':
            # Review input includes the original complete proposal packet.
            proposal = next(row for row in rows.values() if row['stage'] == 'proposal')
            parent = proposal['data']
            parent['request']['padding'] = ''
            parent['context']['packet_hash'] = semantics._hash(parent['request'])
            parent_key = semantics.semantic_task_identity_hash(setup[2], setup[0]._input_hash, 'proposal', parent['request'], parent['context'])
            parent['request_hash'] = parent_key
            with semantic_case[0]._db() as db:
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', (parent_key, 'proposal', setup[0]._input_hash, 'proposed', 1, json.dumps(parent), None, 'fixture'))
            context = {**parent['context'], 'proposal_hash': parent_key,
                'claim_ids': {c['id']: 'claim:' + semantics._hash({'packet': parent['context']['packet_hash'], 'id': c['id']})[:24] for c in parent['result']['claims']}}
            payload.update(proposal_hash=parent_key, packet_hash=context['packet_hash'])
    payload["padding"] = "x" * (cap - setup[0]._request_bytes(stage, system, payload))
    # The byte boundary itself precedes transport; avoid sending fabricated
    # padding through semantic-chain admission, which intentionally rejects it.
    if stage in {'proposal', 'review'}:
        assert setup[0]._request_bytes(stage, system, payload) == cap
        payload['padding'] += 'x'
        with pytest.raises(ToolError) as failure:
            setup[0]._call(stage, system, payload, lambda result: result, context=context)
        assert failure.value.reason_code == 'input_limit' and sent == []
    else:
        setup[0]._call(stage, system, payload, lambda result: result)
        assert sent == [cap]
        payload["padding"] += "x"
        with pytest.raises(ToolError) as failure:
            setup[0]._call(stage, system, payload, lambda result: result)
        assert failure.value.reason_code == "input_limit" and sent == [cap]


def test_model_cannot_raise_codex_candidate_limit_with_a_spoofed_profile(semantic_case):
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    def mutate(result, payload):
        result["budget_profile"] = {"candidate_bytes": 999999}
        result["claims"][0]["text"] += "业务条件" * 4000
    setup = build_case(semantic_case, mutate=mutate)
    for _ in range(2):
        with pytest.raises(ToolError) as failure:
            execute(setup)
        assert failure.value.reason_code == "candidate_limit"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]


def test_public_claim_cannot_be_disconnected_from_its_input_decision(semantic_case):
    def mutate(result, payload):
        result["decisions"][0]["parts"][0].update(outcome="technical", claim_ids=[])
        result["decisions"][1]["parts"][0].update(claim_ids=[])
    setup = build_case(semantic_case, mutate=mutate)
    with pytest.raises(ToolError):
        execute(setup)
    assert setup[1][-1]["stage"] == "proposal"


def test_cache_proof_detects_changed_request_or_bindings(semantic_case):
    setup = build_case(semantic_case)
    execute(setup)
    with semantic_case[0]._db() as db:
        key, encoded = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='review'").fetchone()
        data = json.loads(encoded)
        data["context"]["input_ids"]["f0"] = "different-input"
        db.execute("UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?", (json.dumps(data), key))
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3


@pytest.mark.parametrize("reason,status,wanted", [("stream_error", 502, "stream_error"), ("quota", 429, "quota"), ("raw-secret src/secret.java", 502, None)])
def test_controlled_codex_cause_survives_checkpoint_without_raw_errors(semantic_case, reason, status, wanted):
    def provider(payload, count):
        error = ToolError("raw-secret transport body src/secret.java", status)
        error.codex_reason = reason
        raise error
    setup = build_case(semantic_case, provider=provider)
    for _ in range(2):
        with pytest.raises(ToolError) as failure:
            execute(setup)
        assert getattr(failure.value, "codex_reason", None) == wanted
        assert failure.value.status_code == status
        assert "raw-secret" not in str(failure.value)
    assert len(setup[1]) == 1
    with semantic_case[0]._db() as db:
        encoded = db.execute("SELECT data FROM business_semantic_tasks").fetchone()[0]
    assert "raw-secret" not in encoded and "src/secret.java" not in encoded


def test_review_with_unknown_issue_cannot_silently_accept(semantic_case):
    def provider(payload, count):
        if payload["stage"] == "review":
            result = review(payload)
            result["issues"] = [{"code": "unexpected raw issue", "input_ids": []}]
            return {"text": json.dumps(result), "usage": {}}
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError):
        execute(setup)


def test_projection_cannot_drop_accepted_claim_even_with_recomputed_hash(semantic_case):
    from businessmap.business_semantics import _projection_hash, validate_semantic_projection
    result = execute(build_case(semantic_case))
    result["public_documents"][0]["body"] = "# 创建门店方案\n\n允许创建。\n"
    result["verification"]["projection_hash"] = _projection_hash(result)
    with pytest.raises(ToolError):
        validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])


@pytest.mark.parametrize('local', [False, True])
def test_worker_failure_stops_new_review_before_coordinator_drains(semantic_case, monkeypatch, local):
    if local:
        semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    import businessmap.business_semantics as module
    original_units = semantic_case[3]
    extra = deepcopy(original_units)
    for unit in extra:
        unit["_fact_id"] += "-second"
    units = original_units + extra
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "rules", "title": "创建第一类方案" if i < 3 else "创建第二类方案"} for i, unit in enumerate(units)]
    second_started, release_second = threading.Event(), threading.Event()
    def provider(payload, count):
        if payload["stage"] == "catalogue":
            return {"text": json.dumps({"operations": [
                {"title": "创建第一类方案", "category": "flows", "label_ids": ["l0"]},
                {"title": "创建第二类方案", "category": "flows", "label_ids": ["l1"]}]}), "usage": {}}
        if payload["stage"] == "proposal":
            if payload["operations"][0]["title"] == "创建第一类方案":
                assert second_started.wait(2)
                raise ToolError("controlled unavailable", 429)
            second_started.set()
            assert release_second.wait(2)
    original_wait = module.wait
    def drain_before_coordinator(*args, **kwargs):
        done = original_wait(*args, **kwargs)
        release_second.set()
        original_wait(args[0])
        return done
    monkeypatch.setattr(module, "wait", drain_before_coordinator)
    setup = build_case((semantic_case[0], semantic_case[1], semantic_case[2], units, routing), provider=provider)
    setup[0].CONCURRENCY = 2
    setup[0].CANDIDATE_BYTES = 3000
    with pytest.raises(ToolError):
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_semantic_tasks WHERE stage='proposal' AND status='proposed'").fetchone()[0] == 1


def test_invalid_peer_preserves_all_admitted_streams_and_recovery_receipts(semantic_case):
    units, routing = [], []
    for index, title in enumerate(("创建一类方案", "创建二类方案", "创建三类方案", "创建四类方案", "创建五类方案")):
        for original in semantic_case[3]:
            unit = deepcopy(original)
            unit["_fact_id"] += str(index)
            units.append(unit)
            routing.append({"fact_id": unit["_fact_id"], "disposition": "public", "category": "rules", "title": title})
    setup = build_case((*semantic_case[:3], units, routing))
    builder = setup[0]
    builder.CANDIDATE_BYTES = 3000
    admitted = threading.Barrier(4)
    unexpected_review = threading.Event()
    calls, completed = [], []
    def streaming_complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        calls.append(payload["stage"])
        if payload["stage"] == "catalogue":
            result = {"operations": [{"title": item["title"], "category": "flows", "label_ids": [item["label_id"]]} for item in payload["items"]]}
        elif payload["stage"] == "proposal":
            admitted.wait(timeout=3)
            result = candidate(payload)
            if payload["operations"][0]["title"] == "创建一类方案":
                result["decisions"].pop()
            else:
                assert builder._stopped.wait(3)
                # Codex calls the admission callback during streaming and at
                # completion. A peer validation failure must not kill these.
                kwargs["before_send"]()
                kwargs["before_send"]()
                completed.append(payload["operations"][0]["title"])
        else:
            unexpected_review.set()
            pytest.fail("A review was sent after the batch stopped")
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {"input_tokens": 50, "output_tokens": 30}, "output_limited": False}
    builder.service.providers.complete = streaming_complete
    with pytest.raises(ToolError):
        execute(setup)
    assert calls == ["catalogue", "proposal", "proposal", "proposal", "proposal"]
    assert set(completed) == {"创建二类方案", "创建三类方案", "创建四类方案"}
    with semantic_case[0]._db() as db:
        statuses = [row[0] for row in db.execute("SELECT status FROM business_semantic_tasks WHERE stage='proposal'")]
        receipts = list(db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='proposal' AND status='proposed' ORDER BY stage_hash"))
    assert sorted(statuses) == ["needs_review", "proposed", "proposed", "proposed"]
    original_call = builder._call
    def delayed_failed_cache(stage, system, payload, normalize, **kwargs):
        if stage == "proposal" and payload["operations"][0]["title"] == "创建一类方案":
            assert unexpected_review.wait(3)
        return original_call(stage, system, payload, normalize, **kwargs)
    builder._call = delayed_failed_cache
    with pytest.raises(ToolError):
        execute(setup)
    assert len(calls) == 5
    with semantic_case[0]._db() as db:
        assert list(db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='proposal' AND status='proposed' ORDER BY stage_hash")) == receipts


@pytest.mark.parametrize("interruption", [InterruptedError("paused"), InterruptedError("cancelled"), ToolError("authorization changed", 403), ToolError("snapshot changed", 409)])
def test_admitted_stream_still_rechecks_external_guard(semantic_case, interruption):
    changed = False
    def guard():
        if changed:
            raise interruption
    setup = build_case(semantic_case, guard=guard)
    def complete(system, user, **kwargs):
        nonlocal changed
        kwargs["before_send"]()
        changed = True
        kwargs["before_send"]()
        pytest.fail("External guard was ignored during an admitted stream")
    setup[0].service.providers.complete = complete
    with pytest.raises(type(interruption)):
        execute(setup)
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_semantic_tasks WHERE status IN ('received','accepted','proposed')").fetchone()[0] == 0


@pytest.mark.parametrize(("mutation", "expected"), [
    (lambda r, p: r["claims"][0].update(support_ids=["private/path/Secret.java"]),
     {"validation_code": "reference_list", "field": "support_ids", "row_index": 0}),
    (lambda r, p: r["decisions"].pop(),
     {"validation_code": "decision_coverage", "field": "decisions"}),
    (lambda r, p: r["claims"][0]["support_ids"].append(p["items"][2]["fact_id"]),
     {"validation_code": "claim_binding", "field": "support_ids", "row_index": 0}),
    (lambda r, p: r["decisions"][1]["parts"][0].update(resolved_by_ids=[p["items"][1]["fact_id"]]),
     {"validation_code": "resolution_self", "field": "resolved_by_ids", "row_index": 1, "part_index": 0}),
])
def test_candidate_failure_has_bounded_branch_diagnostic_on_recovery(semantic_case, mutation, expected):
    from businessmap.business_semantics import SemanticValidationError
    setup = build_case(semantic_case, mutate=mutation)
    for _ in range(2):
        with pytest.raises(SemanticValidationError) as failure:
            execute(setup)
        assert failure.value.safe_details() == {"reason_code": "candidate_invalid", **expected}
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal"] + (["proposal"] if expected["validation_code"] in {"claim_binding", "resolution_self"} else [])
    with semantic_case[0]._db() as db:
        receipt = json.loads(db.execute("SELECT data FROM business_semantic_tasks WHERE stage='proposal'").fetchone()[0])
    assert receipt["diagnostic"] == expected
    assert "private/path/Secret.java" not in json.dumps(receipt)


def test_candidate_diagnostic_drops_unknown_codes_fields_and_unbounded_positions():
    from businessmap.business_semantics import SemanticValidationError, _controlled_diagnostic
    unsafe = {"validation_code": "private/path/Secret.java", "field": "secret_id", "row_index": -1, "part_index": 10**100,
              "private_id": "private/path/Secret.java"}
    assert _controlled_diagnostic(unsafe) == {}
    failure = SemanticValidationError("candidate_invalid", validation_code=unsafe["validation_code"], field=unsafe["field"],
                                      row_index=True, part_index=10**100)
    assert failure.safe_details() == {"reason_code": "candidate_invalid"}


def test_codex_semantic_schemas_restrict_shapes_ids_but_allow_review_rejection(semantic_case):
    from jsonschema import Draft202012Validator
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    original = setup[0].service.providers.complete
    schemas, payloads = {}, {}
    def complete(system, user, **kwargs):
        payload = json.loads(user)
        schema = kwargs.get("output_schema")
        assert isinstance(schema, dict), "Semantic Codex stage lacks enforced structure"
        Draft202012Validator.check_schema(schema)
        schemas[payload["stage"]], payloads[payload["stage"]] = schema, payload
        result = original(system, user, **kwargs)
        Draft202012Validator(schema).validate(json.loads(result["text"]))
        return result
    setup[0].service.providers.complete = complete
    assert execute(setup)["complete"]
    validator = Draft202012Validator(schemas["proposal"])
    invalid = candidate(payloads["proposal"])
    invalid["claims"][0]["support_ids"] = ["private/fact-id"]
    assert not validator.is_valid(invalid)
    invalid = candidate(payloads["proposal"])
    invalid["decisions"][0]["parts"].append({"outcome": "claim", "claim_ids": ["c00"]})
    assert not validator.is_valid(invalid)
    invalid = candidate(payloads["proposal"])
    invalid["claims"][0]["private_source_path"] = "private/path"
    assert not validator.is_valid(invalid)
    rejection = review(payloads["review"], reject=True)
    for field in ("input_checks", "claim_checks", "operation_checks"):
        for row in rejection[field]:
            row["checks"] = dict.fromkeys(row["checks"], False)
    Draft202012Validator(schemas["review"]).validate(rejection)


def test_http_semantic_calls_keep_existing_output_contract(semantic_case):
    setup = build_case(semantic_case)
    original = setup[0].service.providers.complete
    def complete(system, user, **kwargs):
        assert "output_schema" not in kwargs
        return original(system, user, **kwargs)
    setup[0].service.providers.complete = complete
    assert execute(setup)["complete"]


def test_semantic_schema_bytes_are_admitted_and_bound_to_cache(semantic_case, monkeypatch):
    import businessmap.business_semantics as module
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    original = module.semantic_output_schema
    def enlarged(stage, payload):
        schema = original(stage, payload)
        schema["description"] = "x" * 64000
        return schema
    request = {"stage": "catalogue", "items": [{"label_id": "l0"}]}
    before = module.semantic_task_identity_hash(setup[2], "0" * 64, "catalogue", request, {})
    monkeypatch.setattr(module, "semantic_output_schema", enlarged)
    assert module.semantic_task_identity_hash(setup[2], "0" * 64, "catalogue", request, {}) != before
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.reason_code == "input_limit" and setup[1] == []


def test_codex_structured_review_allows_no_public_claims(semantic_case):
    from jsonschema import Draft202012Validator
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    for unit in semantic_case[3]:
        unit.update(kind="finding", text="内部缓存维护资料。")
    def provider(payload, count):
        if payload["stage"] == "proposal":
            result = {"operations": [{"operation_id": item["operation_id"], "purpose": "", "summary": "", "support_ids": []} for item in payload["operations"]],
                      "claims": [], "decisions": [{"input_id": item["fact_id"], "parts": [{"outcome": "technical", "rationale": "内部维护机制。"}]} for item in payload["items"]]}
            return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
    setup = build_case(semantic_case, provider=provider)
    original = setup[0].service.providers.complete
    def complete(system, user, **kwargs):
        Draft202012Validator.check_schema(kwargs["output_schema"])
        result = original(system, user, **kwargs)
        Draft202012Validator(kwargs["output_schema"]).validate(json.loads(result["text"]))
        return result
    setup[0].service.providers.complete = complete
    result = execute(setup)
    assert result["complete"] and result["public_documents"] == [] and result["semantic_coverage"]["claims"] == []


def test_codex_partition_schemas_allow_rejecting_shared_conditions(semantic_case):
    from jsonschema import Draft202012Validator
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = oversized_case(semantic_case, independent=False, reject=True)
    setup[0].CANDIDATE_BYTES = 6000
    original = setup[0].service.providers.complete
    def complete(system, user, **kwargs):
        Draft202012Validator.check_schema(kwargs["output_schema"])
        result = original(system, user, **kwargs)
        Draft202012Validator(kwargs["output_schema"]).validate(json.loads(result["text"]))
        return result
    setup[0].service.providers.complete = complete
    with pytest.raises(ToolError) as error:
        execute(setup)
    assert error.value.reason_code == "review_rejected"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "subdivide", "partition_review"]


@pytest.mark.parametrize("case,code", [
    ("finding", "resolution_kind"), ("empty", "resolution_empty"),
    ("self", "resolution_self"), ("outside_support", "resolution_membership"),
    ("non_finding", "resolution_finding"),
])
def test_resolution_diagnostic_identifies_each_existing_predicate_without_raw_result(semantic_case, case, code):
    from businessmap.business_semantics import SemanticValidationError
    if case == "non_finding":
        semantic_case[3][2]["kind"] = "context"
    def mutate(result, payload):
        ids = [item["fact_id"] for item in payload["items"]]
        part = result["decisions"][1]["parts"][0]
        if case == "finding":
            result["decisions"][0], result["decisions"][1] = result["decisions"][1], result["decisions"][0]
            part = result["decisions"][1]["parts"][0]
            part.update(outcome="resolved", resolved_by_ids=[ids[2]], support_ids=[ids[2]])
        elif case == "empty":
            part["resolved_by_ids"] = []
        elif case == "self":
            part["resolved_by_ids"] = [ids[1]]
        elif case == "outside_support":
            part["support_ids"] = [ids[1]]
        else:
            part.update(resolved_by_ids=[ids[2]], support_ids=[ids[2]])
        part["rationale"] = "rejected-private-marker"
    setup = build_case(semantic_case, mutate=mutate)
    expected = {"validation_code": code, "field": "resolved_by_ids", "row_index": 1, "part_index": 0}
    for _ in range(2):
        with pytest.raises(SemanticValidationError) as error:
            execute(setup)
        assert error.value.safe_details() == {"reason_code": "candidate_invalid", **expected}
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]
    with semantic_case[0]._db() as db:
        encoded = db.execute("SELECT data FROM business_semantic_tasks WHERE stage='proposal'").fetchone()[0]
    receipt = json.loads(encoded)
    assert receipt["diagnostic"] == expected and receipt["result"] is None
    assert "rejected-private-marker" not in encoded


@pytest.mark.parametrize("field", ["purpose", "summary", "text", "scope", "conditions"])
def test_number_diagnostic_uses_own_support_and_bounded_field_position(semantic_case, field):
    from businessmap.business_semantics import SemanticValidationError
    def mutate(result, payload):
        row = result["operations"][0] if field in {"purpose", "summary"} else result["claims"][0]
        row[field] = ["有维护权限", "上限987654321套"] if field == "conditions" else "上限987654321套"
    setup = build_case(semantic_case, mutate=mutate)
    expected = {"field": field, "row_index": 0, **({"part_index": 1} if field == "conditions" else {})}
    for _ in range(2):
        with pytest.raises(SemanticValidationError) as error:
            execute(setup)
        assert error.value.safe_details() == {"reason_code": "number_unsupported", **expected}
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]
    with semantic_case[0]._db() as db:
        encoded = db.execute("SELECT data FROM business_semantic_tasks WHERE stage='proposal'").fetchone()[0]
    receipt = json.loads(encoded)
    assert receipt["diagnostic"] == expected and receipt["result"] is None
    assert "987654321" not in encoded


def disjoint_operation_case(semantic_case, mutate=lambda result, payload: None, *, legacy=False):
    from jsonschema import Draft202012Validator
    store, snapshot, run, _, _ = semantic_case
    if not legacy:
        snapshot["provider"].update(kind="codex-local", base_url="codex://local")
    titles = ["创建预约", "取消预约"]
    units = [{"_fact_id": "fact:" + str(i), "kind": "finding", "text": title + condition,
              "_evidence_ids": [], "_source_paths": [], "_forbidden": []}
             for i, (title, condition) in enumerate((
                 (titles[0], "须有操作权限。"), (titles[0], "须选择目标门店。"),
                 (titles[1], "须有取消权限。"), (titles[1], "须确认预约未开始。")))]
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "flows", "title": titles[i // 2]}
               for i, unit in enumerate(units)]
    def provider(payload, count):
        if payload["stage"] == "catalogue":
            result = {"operations": [{"title": item["title"], "category": "flows", "label_ids": [item["label_id"]]} for item in payload["items"]]}
        elif payload["stage"] == "proposal":
            assert len(payload["operations"]) == 2
            result = _canonical_independent_candidate(payload)
            # One claim with two supports, each with an explicit model backlink.
            first, second = result["claims"][:2]
            first["support_ids"] += second["support_ids"]
            result["decisions"][1]["parts"][0]["claim_ids"].append(first["id"])
            result = positive_graph(result, payload)
            mutate(result, payload)
        else:
            return None
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
    setup = build_case((store, snapshot, run, units, routing), provider=provider)
    original = setup[0].service.providers.complete
    def complete(system, user, **kwargs):
        response = original(system, user, **kwargs)
        if "output_schema" in kwargs:
            Draft202012Validator(kwargs["output_schema"]).validate(json.loads(response["text"]))
        return response
    setup[0].service.providers.complete = complete
    return setup


def test_disjoint_operations_and_complete_claim_backlinks_pass_schema_candidate_projection(semantic_case):
    from businessmap.business_semantics import validate_semantic_projection
    result = execute(disjoint_operation_case(semantic_case))
    validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])
    assert len(result["public_documents"]) == 2
    assert [set(operation["fact_ids"]) for operation in result["semantic_coverage"]["catalogue"]] == [
        {"fact:0", "fact:1"}, {"fact:2", "fact:3"}]


@pytest.mark.parametrize("case,code", [("claim", "claim_support"), ("backlink", "claim_binding")])
def test_legacy_canonical_cross_operation_support_requires_both_backlink_directions(semantic_case, case, code):
    from businessmap.business_semantics import SemanticValidationError
    def mutate(result, payload):
        if case == "backlink":
            result["decisions"][1]["parts"][0]["claim_ids"].remove("c00")
        else:
            result["claims"][0]["support_ids"] = payload["operations"][1]["fact_ids"][:1]
    setup = disjoint_operation_case(semantic_case, mutate, legacy=True)
    with pytest.raises(SemanticValidationError) as error:
        execute(setup)
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]
    expected = {"reason_code": "candidate_invalid", "validation_code": code, "row_index": 0,
                **({"field": "claim_ids", "part_index": 0} if case == "claim" else {"field": "support_ids"})}
    assert error.value.safe_details() == expected


@pytest.mark.parametrize("mutation", [
    lambda r, p: r["claims"][1].update(kind="rule"),
    lambda r, p: r["claims"][0].update(section="unknowns"),
    lambda r, p: r["decisions"][1]["parts"][1].update(outcome="claim", claim_ids=["c00"]),
    lambda r, p: r["decisions"][0]["parts"][0].update(outcome="retained"),
    lambda r, p: r["decisions"][0]["parts"][0].update(resolved_by_ids=["f2"]),
    lambda r, p: r["claims"][0].update(support_ids=[]),
    lambda r, p: r["decisions"][0]["parts"][0].update(support_ids=[]),
    lambda r, p: r["claims"][0]["support_ids"].append("f0"),
    lambda r, p: r["operations"].append(deepcopy(r["operations"][0])),
    lambda r, p: r["claims"].append(deepcopy(r["claims"][0])),
    lambda r, p: r["decisions"].append(deepcopy(r["decisions"][0])),
    lambda r, p: r["decisions"][0].update(parts=[]),
    lambda r, p: r["decisions"][0]["parts"][0].update(rationale=" "),
    lambda r, p: r["claims"][0].update(text=" "),
    lambda r, p: r["claims"][0].update(scope=" "),
    lambda r, p: r["claims"][0].update(conditions=[" "]),
])
def test_explicit_generator_contract_keeps_existing_rejections(semantic_case, mutation):
    setup = build_case(semantic_case, mutate=mutation)
    with pytest.raises(ToolError):
        execute(setup)
    assert all(call["stage"] == "proposal" for call in setup[1][1:])
    assert 2 <= len(setup[1]) <= 3


@pytest.mark.parametrize("field", ["purpose", "summary", "text", "scope", "conditions"])
@pytest.mark.parametrize("text", ["上限60套。", "上限5.0套。"])
def test_numbers_elsewhere_in_packet_or_reformatted_do_not_support_public_fields(semantic_case, field, text):
    def mutate(result, payload):
        row = result["operations"][0] if field in {"purpose", "summary"} else result["claims"][0]
        row[field] = [text] if field == "conditions" else text
    setup = build_case(semantic_case, mutate=mutate)
    with pytest.raises(ToolError) as error:
        execute(setup)
    assert error.value.reason_code == "number_unsupported"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "proposal"]


def test_private_resolution_across_operations_preserves_mixed_unknown(semantic_case):
    from jsonschema import Draft202012Validator
    from businessmap.business_semantics import validate_semantic_projection
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    semantic_case[4][1]["title"] = semantic_case[4][2]["title"] = "核实并发创建"
    def provider(payload, count):
        if payload["stage"] == "catalogue":
            result = {"operations": [{"title": item["title"], "category": "flows", "label_ids": [item["label_id"]]} for item in payload["items"]]}
        elif payload["stage"] == "proposal":
            result = _canonical_candidate(payload)
            result["claims"][1]["operation_id"] = payload["operations"][1]["operation_id"]
            result["operations"].append({"operation_id": payload["operations"][1]["operation_id"], "purpose": "核实并发创建限制。",
                                         "summary": "并发创建的数量限制仍待确认。", "support_ids": [payload["items"][1]["fact_id"]]})
            result = positive_graph(result, payload)
        else:
            return None
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {}}
    setup = build_case(semantic_case, provider=provider)
    original = setup[0].service.providers.complete
    def complete(system, user, **kwargs):
        response = original(system, user, **kwargs)
        if "output_schema" in kwargs:
            Draft202012Validator(kwargs["output_schema"]).validate(json.loads(response["text"]))
        return response
    setup[0].service.providers.complete = complete
    result = execute(setup)
    validate_semantic_projection(result, expected_input_hash=result["semantic_input_hash"])
    assert [part["outcome"] for part in result["semantic_coverage"]["decisions"]["fact:unknown"]["parts"]] == ["retained", "resolved"]
    assert len(result["public_documents"]) == 2


def test_historical_exact_quote_still_roundtrips_and_reuses_accepted_receipt(semantic_case):
    def mutate(result, payload):
        result["decisions"][1]["parts"][0]["quote"] = "本组件未见额度检查"
    setup = build_case(semantic_case, mutate=mutate)
    result = execute(setup)
    reused = execute(setup)
    assert reused["semantic_coverage"] == result["semantic_coverage"]
    assert reused["public_documents"] == result["public_documents"]
    assert reused["usage"]["cache_hits"] == 3
    assert result["semantic_coverage"]["decisions"]["fact:unknown"]["parts"][0]["quote"] == "本组件未见额度检查"
    assert len(setup[1]) == 3


@pytest.mark.parametrize("diagnostic", [{}, {"validation_code": "resolution_support", "field": "resolved_by_ids", "row_index": 1, "part_index": 0}])
def test_legacy_failure_diagnostic_is_read_without_rewriting_receipt(semantic_case, diagnostic):
    from businessmap.business_semantics import SemanticValidationError
    setup = build_case(semantic_case, mutate=lambda r, p: r["decisions"].pop())
    with pytest.raises(SemanticValidationError):
        execute(setup)
    with semantic_case[0]._db() as db:
        key, encoded = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='proposal'").fetchone()
        receipt = json.loads(encoded)
        receipt["diagnostic"] = diagnostic
        encoded = json.dumps(receipt)
        db.execute("UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?", (encoded, key))
    with pytest.raises(SemanticValidationError) as error:
        execute(setup)
    assert error.value.safe_details() == {"reason_code": "candidate_invalid", **diagnostic}
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT data FROM business_semantic_tasks WHERE stage_hash=?", (key,)).fetchone()[0] == encoded
    assert len(setup[1]) == 2


def test_changed_generator_contract_gets_new_identity_preserving_old_receipts(semantic_case, monkeypatch):
    import businessmap.business_semantics as module
    setup = build_case(semantic_case)
    setup[3]["semantic_input_hash"] = module.semantic_input_hash(setup[2], setup[3], [], [])
    old = execute(setup)
    with semantic_case[0]._db() as db:
        receipts = dict(db.execute("SELECT stage_hash,data FROM business_semantic_tasks"))
    monkeypatch.setattr(module, "PROPOSAL_SYSTEM", module.PROPOSAL_SYSTEM + "\n合成合同变更。")
    setup[3]["semantic_input_hash"] = module.semantic_input_hash(setup[2], setup[3], [], [])
    new = execute(setup)
    assert old["semantic_input_hash"] != new["semantic_input_hash"]
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "review"] * 2
    with semantic_case[0]._db() as db:
        current = dict(db.execute("SELECT stage_hash,data FROM business_semantic_tasks"))
    assert len(current) == 6 and all(current[key] == encoded for key, encoded in receipts.items())


def test_codex_shape_contract_refuses_old_projection_and_preserves_old_receipts(semantic_case, monkeypatch):
    import businessmap.business_semantics as module
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    with monkeypatch.context() as previous:
        previous.setattr(module, "OUTPUT_CONTRACT", "semantic-json-schema-v1")
        old = execute(setup)
    with semantic_case[0]._db() as db:
        receipts = dict(db.execute("SELECT stage_hash,data FROM business_semantic_tasks"))
    current_hash = module.semantic_input_hash(setup[2], setup[3], [], [])
    assert current_hash != old["semantic_input_hash"]
    with pytest.raises(module.SemanticValidationError):
        module.validate_stored_semantic_projection(old, snapshot=setup[2], expected_input_hash=current_hash, store=semantic_case[0])
    assert len(setup[1]) == 3
    current = execute(setup)
    assert current["semantic_input_hash"] != old["semantic_input_hash"]
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal", "review"] * 2
    with semantic_case[0]._db() as db:
        saved = dict(db.execute("SELECT stage_hash,data FROM business_semantic_tasks"))
    assert len(saved) == 6 and all(saved[key] == value for key, value in receipts.items())


def shared_support_case(semantic_case, mutate=lambda result, payload: None):
    """One mixed observation belongs to refund routing, but contains two unknowns."""
    store, snapshot, run, units, routing = semantic_case
    units[0]['text'] = '有预约维护权限的人可修改预约。'
    units[1]['text'] = '修改预约时是否保留原时段，以及退款申请时是否返还积分，均尚未验证。'
    routing[0]['title'] = '修改预约'
    routing[1]['title'] = routing[2]['title'] = '申请退款'
    def provider(payload, count):
        if payload['stage'] == 'catalogue':
            result = {'operations': [{'title': item['title'], 'category': 'flows', 'label_ids': [item['label_id']]}
                                     for item in payload['items']]}
        elif payload['stage'] == 'proposal':
            result = {'operations': [
                {'operation_id': 'o0', 'purpose': '修改已有预约。', 'summary': '修改须有权限，原时段是否保留仍待确认。', 'support_ids': ['f0', 'f1']},
                {'operation_id': 'o1', 'purpose': '核实退款申请的积分处理。', 'summary': '申请退款时是否返还积分仍待确认。', 'support_ids': ['f1']}],
                'claims': [
                    {'id': 'c00', 'operation_id': 'o0', 'kind': 'rule', 'text': units[0]['text'], 'support_ids': ['f0'], 'scope': '预约修改', 'conditions': [], 'section': 'who'},
                    {'id': 'c30', 'operation_id': 'o0', 'kind': 'unknown', 'text': '修改预约时是否保留原时段，尚未验证。', 'support_ids': ['f1'], 'scope': '预约修改', 'conditions': [], 'section': 'unknowns'},
                    {'id': 'c31', 'operation_id': 'o1', 'kind': 'unknown', 'text': '申请退款时是否返还积分，尚未验证。', 'support_ids': ['f1'], 'scope': '退款申请', 'conditions': [], 'section': 'unknowns'}],
                'decisions': [
                    {'input_id': 'f0', 'parts': [{'outcome': 'claim', 'claim_ids': ['c00'], 'support_ids': ['f0'], 'resolved_by_ids': [], 'rationale': '保留修改权限。'}]},
                    {'input_id': 'f1', 'parts': [{'outcome': 'retained', 'claim_ids': [cid], 'support_ids': ['f1'], 'resolved_by_ids': [], 'rationale': '保留对应业务未知。'} for cid in ('c30', 'c31')]},
                    {'input_id': 'f2', 'parts': [{'outcome': 'technical', 'claim_ids': [], 'support_ids': ['f2'], 'resolved_by_ids': [], 'rationale': '内部维护机制。'}]}]}
            result = positive_graph(result, payload)
            mutate(result, payload)
        else:
            return None
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    return build_case(semantic_case, provider=provider)


def checkpoint_rows(store):
    with store._db() as db:
        return {row[0]: dict(zip(('stage_hash', 'stage', 'input_hash', 'status', 'attempt', 'data'),
                                (*row[:5], json.loads(row[5])))) for row in db.execute(
            'SELECT stage_hash,stage,input_hash,status,attempt,data FROM business_semantic_tasks')}


def test_mixed_input_explicitly_supports_two_complete_operation_cards(semantic_case):
    setup = shared_support_case(semantic_case)
    result = execute(setup)
    documents = result['public_documents']
    assert len(documents) == 2
    assert '修改预约时是否保留原时段，尚未验证。' in documents[0]['body']
    assert '申请退款时是否返还积分，尚未验证。' in documents[1]['body']
    assert documents[0]['fact_ids'] == ['fact:limit', 'fact:unknown']
    assert documents[1]['fact_ids'] == ['fact:unknown']
    assert [result['source_mapping'][doc['key']]['evidence_ids'] for doc in documents] == [['e1', 'e2'], ['e2']]
    public = json.dumps(documents + [result['overview_body'], result['summary_body']], ensure_ascii=False)
    assert 'internal/' not in public and '缓存' not in public and '静态匹配' not in public
    assert [len(op['fact_ids']) for op in result['semantic_coverage']['catalogue']] == [1, 2]
    assert len(result['semantic_coverage']['decisions']['fact:unknown']['parts']) == 2
    assert execute(setup)['public_documents'] == documents
    assert [call['stage'] for call in setup[1]] == ['catalogue', 'proposal', 'review']


def test_shared_projection_needs_real_review_and_proposal_context(semantic_case):
    import businessmap.business_semantics as semantics
    setup = shared_support_case(semantic_case)
    result = execute(setup)
    with pytest.raises(ToolError):
        semantics.validate_semantic_projection(result, expected_input_hash=result['semantic_input_hash'])
    rows = checkpoint_rows(semantic_case[0])
    seen = set()
    def read(key):
        seen.add(key)
        return rows.get(key)
    context = semantics.semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=read)
    semantics.validate_semantic_projection(result, expected_input_hash=result['semantic_input_hash'], trusted_support=context)
    assert seen == set(rows)
    assert sorted(row["stage"] for row in rows.values()) == ["catalogue", "proposal", "review"]
    assert set(context) == {operation["id"] for operation in result["semantic_coverage"]["catalogue"]}
    assert all(set(ids) == {'fact:limit', 'fact:unknown', 'fact:cache'} for ids in context.values())
    for stage in ('review', 'proposal'):
        missing = {key: row for key, row in rows.items() if row['stage'] != stage}
        with pytest.raises(ToolError):
            semantics.semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=missing.get)


@pytest.mark.parametrize('mutation', ['member', 'packet', 'candidate', 'proposal_result', 'review_result', 'input', 'attempt', 'status', 'budget', 'destination', 'snapshot', 'generator'])
def test_shared_support_rejects_changed_checkpoint_identity(semantic_case, monkeypatch, mutation):
    import businessmap.business_semantics as semantics
    setup = shared_support_case(semantic_case)
    result = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    receipt = next(row for row in rows.values() if row['stage'] == 'review')
    snapshot = deepcopy(setup[2])
    if mutation == 'member': receipt['data']['context']['input_ids']['f1'] = 'fact:forged'
    if mutation == 'packet': receipt['data']['context']['packet_hash'] = '0' * 64
    if mutation == 'candidate': receipt['data']['request']['candidate']['claims'][1]['text'] = '预约原时段已经保留。'
    if mutation == 'proposal_result': next(row for row in rows.values() if row['stage'] == 'proposal')['data']['result']['claims'][1]['text'] = '已确认保留。'
    if mutation == 'review_result': receipt['data']['result']['decision'] = 'reject'
    if mutation == 'input': receipt['input_hash'] = '0' * 64
    if mutation == 'attempt': receipt['attempt'] = 2
    if mutation == 'status': receipt['status'] = 'received'
    if mutation == 'budget': receipt['data']['context']['budget_profile']['candidate_bytes'] += 1
    if mutation == 'destination': snapshot['provider']['base_url'] = 'http://127.0.0.1:2/v1'
    if mutation == 'snapshot': result['semantic_input_hash'] = '0' * 64
    if mutation == 'generator': monkeypatch.setattr(semantics, 'SUPPORT_POLICY', 'future-policy')
    with pytest.raises(ToolError):
        semantics.semantic_support_context(result, snapshot=snapshot, expected_input_hash=result['semantic_input_hash'], read_checkpoint=rows.get)


def test_shared_input_missing_backlink_is_not_repaired(semantic_case):
    setup = shared_support_case(semantic_case, lambda result, payload: result['decisions'][1]['parts'].pop(0))
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.safe_details()['validation_code'] == 'claim_binding'
    assert [call['stage'] for call in setup[1]] == ['catalogue', 'proposal', 'proposal']


def test_shared_input_outside_packet_is_rejected_before_review(semantic_case):
    setup = shared_support_case(semantic_case, lambda result, payload: result['claims'][1].update(support_ids=['f999']))
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.safe_details()['validation_code'] == 'reference_list'
    assert [call['stage'] for call in setup[1]] == ['catalogue', 'proposal']


def test_changed_support_policy_never_reuses_old_receipts(semantic_case, monkeypatch):
    import businessmap.business_semantics as semantics
    setup = shared_support_case(semantic_case)
    old = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    before_input = semantics.semantic_input_hash(setup[2], setup[3], [], [])
    monkeypatch.setattr(semantics, 'SUPPORT_POLICY', 'synthetic-next-support-policy')
    assert semantics.semantic_input_hash(setup[2], setup[3], [], []) != before_input
    new = execute(setup)
    assert old['semantic_input_hash'] != new['semantic_input_hash']
    assert len(setup[1]) == 6
    current = checkpoint_rows(semantic_case[0])
    assert len(current) == 6 and all(current[key] == value for key, value in rows.items())


@pytest.mark.parametrize('mutation', ['candidate', 'members', 'review', 'proposal'])
def test_resigned_review_still_binds_actual_proposal_members_and_accepted_result(semantic_case, mutation):
    import businessmap.business_semantics as semantics
    setup = shared_support_case(semantic_case)
    projection = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    key = projection['verification']['accepted_review_hashes'][0]
    row = rows.pop(key)
    data = row['data']
    if mutation == 'candidate': data['request']['candidate']['claims'][1]['text'] = '原时段已确认保留。'
    if mutation == 'members': data['context']['input_ids']['f1'] = 'fact:outside'
    if mutation == 'review': data['result']['operation_checks'][0]['checks']['relationships'] = False
    if mutation == 'proposal': data['context']['proposal_hash'] = data['request']['proposal_hash'] = '0' * 64
    key = semantics.semantic_task_identity_hash(setup[2], row['input_hash'], row['stage'], data['request'], data['context'])
    row['stage_hash'] = data['request_hash'] = key
    data['result_hash'] = semantics._hash(data['result'])
    rows[key] = row
    projection['verification']['accepted_review_hashes'][0] = key
    with pytest.raises(ToolError):
        context = semantics.semantic_support_context(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], read_checkpoint=rows.get)
        semantics.validate_semantic_projection(projection, expected_input_hash=projection['semantic_input_hash'], trusted_support=context)


def independent_small_operations(semantic_case, *, split=False, cross_child=False):
    store, snapshot, run, _, _ = semantic_case
    titles = ['修改预约', '申请退款', '核实到店', '确认完成']
    count = 20 if split else 24
    units = [{'_fact_id': 'fact:' + str(i), 'kind': 'finding', 'text': '操作须有对应门店的维护权限。',
              '_evidence_ids': ['e' + str(i)], '_source_paths': [], '_forbidden': []} for i in range(count)]
    routing = [{'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'flows', 'title': titles[0 if split else i // 6]}
               for i, unit in enumerate(units)]
    def provider(payload, count):
        if payload['stage'] == 'catalogue':
            result = {'operations': [{'title': item['title'], 'category': 'flows', 'label_ids': [item['label_id']]} for item in payload['items']]}
        elif payload['stage'] == 'subdivide':
            groups = [payload['items'][:5], payload['items'][5:10], payload['items'][10:]]
            result = {'operations': [{'title': titles[i], 'category': 'flows', 'label_ids': [item['label_id'] for item in group]} for i, group in enumerate(groups)]}
        elif payload['stage'] == 'partition_review':
            result = partition_review(payload)
        elif payload['stage'] == 'proposal':
            result = independent_candidate(payload)
            if cross_child and len(payload['operations']) > 1:
                other = payload['operations'][1]['fact_ids'][0]
                if cross_child == 'part':
                    result['decisions'][0]['parts'][0]['support_ids'].append(other)
                else:
                    result['operations'][0]['support_ids'].append(other)
        else:
            return None
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    setup = build_case((store, snapshot, run, units, routing), provider=provider)
    setup[0].CONCURRENCY = 1
    return setup


def test_same_projection_other_packet_is_not_a_support_permission(semantic_case):
    import businessmap.business_semantics as semantics
    setup = independent_small_operations(semantic_case)
    result = execute(setup)
    assert len(result['verification']['packet_hashes']) == 2
    rows = checkpoint_rows(semantic_case[0])
    trusted = semantics.semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=rows.get)
    first = result['semantic_coverage']['catalogue'][0]
    outside = result['semantic_coverage']['catalogue'][-1]['fact_ids'][0]
    assert outside not in trusted[first['id']]
    first['support_ids'].append(outside)
    result['verification']['catalogue_hash'] = semantics._hash(result['semantic_coverage']['catalogue'])
    result['verification']['projection_hash'] = semantics._projection_hash(result)
    with pytest.raises(ToolError):
        semantics.validate_semantic_projection(result, expected_input_hash=result['semantic_input_hash'], trusted_support=trusted)
    with pytest.raises(ToolError):
        semantics.semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=rows.get)


@pytest.mark.parametrize('target,code', [('operation', 'operation_support_membership'), ('part', 'claim_membership')])
def test_independently_split_children_cannot_regain_shared_dependency_in_one_packet(semantic_case, target, code):
    setup = independent_small_operations(semantic_case, split=True, cross_child=target)
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.safe_details()['validation_code'] == code
    assert [call['stage'] for call in setup[1]] == ['catalogue', 'subdivide', 'partition_review', 'proposal', 'proposal']
    assert len(setup[1][-1]['operations']) == 2


def test_partition_child_dependency_rejected_even_with_resealed_projection_and_broad_context(semantic_case):
    import businessmap.business_semantics as semantics
    setup = independent_small_operations(semantic_case, split=True)
    result = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    trusted = semantics.semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=rows.get)
    first, second = result['semantic_coverage']['catalogue'][:2]
    assert set(trusted[first['id']]) == set(first['fact_ids'])
    first['support_ids'].append(second['fact_ids'][0])
    result['verification']['catalogue_hash'] = semantics._hash(result['semantic_coverage']['catalogue'])
    result['verification']['projection_hash'] = semantics._projection_hash(result)
    with pytest.raises(ToolError):
        semantics.validate_semantic_projection(result, expected_input_hash=result['semantic_input_hash'],
            trusted_support={operation['id']: set(result['semantic_coverage']['input_ids']) for operation in result['semantic_coverage']['catalogue']})


def resigned_packet_budget(projection, rows, snapshot, caps):
    """Re-sign the complete proposal/review chain, changing only recorded limits."""
    import businessmap.business_semantics as semantics
    projection, rows = deepcopy(projection), deepcopy(rows)
    old_review = projection['verification']['accepted_review_hashes'][0]
    reviewed = rows.pop(old_review)
    old_proposal = reviewed['data']['context']['proposal_hash']
    proposed = rows.pop(old_proposal)
    for row in (proposed, reviewed):
        for stage, cap in caps.items():
            row['data']['context']['budget_profile']['stages'][stage]['input_bytes'] = cap
    def rekey(row):
        data = row['data']
        key = semantics.semantic_task_identity_hash(snapshot, row['input_hash'], row['stage'], data['request'], data['context'])
        row['stage_hash'] = data['request_hash'] = key
        data['result_hash'] = semantics._hash(data['result'])
        rows[key] = row
        return key
    proposal_hash = rekey(proposed)
    reviewed['data']['request']['proposal_hash'] = reviewed['data']['context']['proposal_hash'] = proposal_hash
    projection['verification']['accepted_review_hashes'][0] = rekey(reviewed)
    projection['verification']['projection_hash'] = semantics._projection_hash(projection)
    return projection, rows


@pytest.mark.parametrize('local', [False, True])
@pytest.mark.parametrize('stage', ['proposal', 'review', 'both'])
def test_resigned_receipt_cannot_claim_request_fit_one_byte_budget(semantic_case, local, stage):
    import businessmap.business_semantics as semantics
    if local:
        semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    setup = shared_support_case(semantic_case)
    projection = execute(setup)
    caps = {name: 1 for name in (('proposal', 'review') if stage == 'both' else (stage,))}
    projection, rows = resigned_packet_budget(projection, checkpoint_rows(semantic_case[0]), setup[2], caps)
    with pytest.raises(ToolError) as failure:
        context = semantics.semantic_support_context(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], read_checkpoint=rows.get)
        semantics.validate_semantic_projection(projection, expected_input_hash=projection['semantic_input_hash'], trusted_support=context)
    assert failure.value.reason_code == 'cache_invalid'


@pytest.mark.parametrize('stage', ['proposal', 'review'])
@pytest.mark.parametrize('boundary', ['without_schema', 'without_wrapper', 'one_short', 'exact'])
def test_codex_receipt_budget_counts_current_schema_wrapper_and_exact_bytes(semantic_case, stage, boundary):
    import businessmap.business_semantics as semantics
    from businessmap.codex_provider import request_input_bytes
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    setup = shared_support_case(semantic_case)
    projection = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    row = next(row for row in rows.values() if row['stage'] == stage)
    payload = row['data']['request']
    system = semantics.semantic_stage_system(setup[2], stage)
    wire = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    schema = semantics.semantic_output_schema(stage, payload)
    max_output = row['data']['context']['budget_profile']['stages'][stage]['max_output_tokens']
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': wire}]
    actual = request_input_bytes(messages, max_output=max_output, output_schema=schema)
    caps = {
        'without_schema': request_input_bytes(messages, max_output=max_output, output_schema=None),
        'without_wrapper': len(system.encode()) + len(wire.encode()) + len(json.dumps(schema, allow_nan=False).encode()),
        'one_short': actual - 1,
        'exact': actual,
    }
    cap = caps[boundary]
    assert 0 < cap <= actual and (cap == actual) == (boundary == 'exact')
    projection, rows = resigned_packet_budget(projection, rows, setup[2], {stage: cap})
    def validate():
        context = semantics.semantic_support_context(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], read_checkpoint=rows.get)
        semantics.validate_semantic_projection(projection, expected_input_hash=projection['semantic_input_hash'], trusted_support=context)
    if boundary == 'exact':
        validate()
    else:
        with pytest.raises(ToolError) as failure:
            validate()
        assert failure.value.reason_code == 'cache_invalid'


def test_bounded_revision_reject_then_accept_keeps_independent_history(semantic_case):
    reviews = 0
    def provider(payload, count):
        nonlocal reviews
        if payload['stage'] == 'review':
            reviews += 1
            result = review(payload, reject=reviews == 1)
            result['private_reason'] = 'not-to-retain'
            return {'text': json.dumps(result), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    result = execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']
    assert setup[1][3]['items'] == setup[1][1]['items'] and setup[1][3]['operations'] == setup[1][1]['operations']
    assert 'revision_feedback' in setup[1][3] and 'candidate' not in setup[1][3]
    assert 'revision_feedback' not in setup[1][4]
    with semantic_case[0]._db() as db:
        rows = db.execute("SELECT stage,attempt,status,data FROM business_semantic_tasks WHERE stage IN ('proposal','review') ORDER BY created_at,rowid").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [('proposal', 1, 'proposed'), ('review', 1, 'needs_review'), ('proposal', 2, 'proposed'), ('review', 2, 'accepted')]
    assert json.loads(rows[1][3])['result']['decision'] == 'reject'
    assert 'not-to-retain' not in ''.join(r[3] for r in rows)
    assert execute(setup)['public_documents'] == result['public_documents'] and len(setup[1]) == 5


def test_bounded_revision_complete_reject_required_before_second_draft(semantic_case):
    def provider(payload, count):
        if payload['stage'] == 'review':
            return {'text': json.dumps({'decision': 'reject', 'issues': [{'code': 'condition_loss', 'input_ids': ['f0']}]}), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.reason_code == 'review_invalid'
    assert len(setup[1]) == 3


def test_bounded_revision_numbers_locate_returned_claim_not_request_order(semantic_case):
    drafts = 0
    def provider(payload, count):
        nonlocal drafts
        if payload['stage'] == 'proposal':
            drafts += 1
            result = candidate(payload)
            if drafts == 1:
                result['claims'].reverse()
                result['claims'][0]['text'] = '并发创建能否严格保持每店500套上限尚未验证。'
            return {'text': json.dumps(result), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    assert execute(setup)['complete']
    feedback = setup[1][2]['revision_feedback']
    assert feedback['reason_code'] == 'number_unsupported'
    assert feedback['input_ids'] == ['f1'] and feedback['operation_ids'] == ['o0']
    assert 'candidate' not in feedback and len(setup[1]) == 4


@pytest.mark.parametrize('local', [False, True])
def test_bounded_revision_second_failure_is_terminal_and_resumes_without_calls(semantic_case, local):
    if local:
        semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    setup = build_case(semantic_case, reject=True)
    for _ in range(2):
        with pytest.raises(ToolError):
            execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_semantic_tasks WHERE attempt=2").fetchone()[0] == 2


def test_bounded_revision_packing_target_preserves_single_complete_operation(semantic_case):
    setup = forty_four_fact_case(semantic_case, honor_budget=True)
    setup[2]['provider'].update(kind='codex-local', base_url='codex://local')
    execute(setup)
    builder = setup[0]
    one = {'id': 'first', 'title': '创建方案', 'category': 'flows', 'fact_ids': list(builder._units), 'aliases': []}
    # A complete operation above the merge target remains admissible on its own.
    for unit in builder._units.values(): unit['text'] = '条件完整且必须满足。' * 12
    assert builder._packet_fits([one])
    second = {**one, 'id': 'second', 'fact_ids': []}
    assert not builder._packet_fits([one, second])


@pytest.mark.parametrize('mutation', ['missing_parent', 'feedback', 'parent_result', 'attempt', 'extra_draft', 'running_peer'])
def test_bounded_revision_corrupt_chain_stops_resume_before_any_new_call(semantic_case, mutation):
    rounds = 0
    def provider(payload, count):
        nonlocal rounds
        if payload['stage'] == 'review':
            rounds += 1
            return {'text': json.dumps(review(payload, reject=rounds == 1)), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    result = execute(setup)
    with semantic_case[0]._db() as db:
        if mutation == 'missing_parent':
            db.execute("DELETE FROM business_semantic_tasks WHERE stage='review' AND attempt=1")
        elif mutation in {'feedback', 'parent_result'}:
            row = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='review' AND attempt=1").fetchone()
            data = json.loads(row[1]); data['feedback' if mutation == 'feedback' else 'result'] = {}
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (json.dumps(data), row[0]))
        elif mutation == 'attempt':
            db.execute("UPDATE business_semantic_tasks SET attempt=1 WHERE stage='proposal' AND attempt=2")
        else:
            row = list(db.execute("SELECT * FROM business_semantic_tasks WHERE stage='proposal' AND attempt=2").fetchone())
            if mutation == 'extra_draft':
                row[4] = 3
            else:
                row[3] = 'running'
            row[0] = 'a' * 64
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', row)
    with pytest.raises(ToolError): execute(setup)
    assert len(setup[1]) == 5
    from businessmap.business_semantics import semantic_support_context
    if mutation not in {'extra_draft', 'running_peer'}:
        with pytest.raises(ToolError):
            semantic_support_context(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=checkpoint_rows(semantic_case[0]).get)


def test_bounded_revision_atomic_claim_prevents_concurrent_same_slot_send(semantic_case, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="businessmap.business_semantics")
    from concurrent.futures import ThreadPoolExecutor
    gate, admitted = threading.Barrier(2), []
    setups = [build_case(semantic_case), build_case(semantic_case)]
    for setup in setups:
        builder = setup[0]
        original = builder._raise_cached_failure
        count = [0]
        def race(original=original, count=count):
            count[0] += 1
            original()
            if count[0] == 2: gate.wait(timeout=3)
        builder._raise_cached_failure = race
        def complete(system, user, **kwargs):
            kwargs['before_send']()
            admitted.append(json.loads(user)['stage'])
            return {'text': '{}', 'usage': {}}
        builder.service.providers.complete = complete
    def run(setup):
        with pytest.raises(ToolError): execute(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, setups))
    assert admitted == ['catalogue']
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_slot_reserved']
    assert [(r.stage, r.status, r.attempt) for r in events] == [('catalogue', 'running', 1)]
    assert not any(getattr(r, 'status', None) in {'accepted', 'proposed'} for r in caplog.records)
    with semantic_case[0]._db() as db:
        assert db.execute('SELECT COUNT(*) FROM business_semantic_tasks').fetchone()[0] == 1


def test_bounded_revision_input_overflow_records_terminal_without_send(semantic_case, monkeypatch):
    setup = build_case(semantic_case, reject=True)
    original = setup[0]._request_bytes
    def byte_count(stage, system, payload):
        if 'revision_feedback' in payload: return 37001
        return original(stage, system, payload)
    monkeypatch.setattr(setup[0], '_request_bytes', byte_count)
    for _ in range(2):
        with pytest.raises(ToolError): execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review']
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_semantic_tasks WHERE attempt=2 AND status='needs_review'").fetchone()[0] == 1


def test_bounded_revision_preflight_counts_paid_slots_after_resume(semantic_case):
    observed = []
    setup = build_case(semantic_case)
    setup[0].service.semantic_preflight = observed.append
    execute(setup)
    assert next(p for p in observed if p['stage'] == 'semantics')['cumulative_maximum_calls'] == 5
    observed.clear()
    result = execute(setup)
    last = next(p for p in observed if p['stage'] == 'semantics')
    assert last['occupied_calls'] == 3 and last['remaining_maximum_calls'] == 0
    assert result['usage']['requests'] == 0


def test_bounded_revision_inventory_revalidates_accepted_checks_before_new_admission(semantic_case):
    from businessmap.business_semantics import _hash
    setup = build_case(semantic_case)
    execute(setup)
    with semantic_case[0]._db() as db:
        key, encoded = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='review'").fetchone()
        data = json.loads(encoded)
        data['result']['input_checks'][0]['checks']['coverage'] = False
        data['result_hash'] = _hash(data['result'])
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (json.dumps(data), key))
    with pytest.raises(ToolError):
        setup[0]._raise_cached_failure()
    assert len(setup[1]) == 3


def test_bounded_revision_resume_preflight_does_not_reserve_cached_catalogue_again(semantic_case):
    setup = build_case(semantic_case)
    execute(setup)
    seen = []
    def preflight(plan):
        seen.append(plan)
        assert plan['cumulative_maximum_calls'] <= 3
    setup[0].service.semantic_preflight = preflight
    execute(setup)
    assert seen and len(setup[1]) == 3


def test_bounded_revision_inventory_refuses_duplicate_draft_slot_under_resealed_context(semantic_case):
    from businessmap.business_semantics import semantic_task_identity_hash, semantic_task_inventory
    setup = build_case(semantic_case)
    execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    extra = deepcopy(next(r for r in rows.values() if r['stage'] == 'proposal'))
    extra['data']['context']['unrecognized_slot'] = 1
    extra['stage_hash'] = semantic_task_identity_hash(setup[2], setup[0]._input_hash, 'proposal', extra['data']['request'], extra['data']['context'])
    extra['data']['request_hash'] = extra['stage_hash']
    with pytest.raises(ToolError):
        semantic_task_inventory([*rows.values(), extra], snapshot=setup[2], expected_input_hash=setup[0]._input_hash)


@pytest.mark.parametrize('bad', ['privacy', 'invalid_id', 'incomplete', 'bool', 'unknown_code'])
def test_bounded_revision_unsafe_reject_never_opens_second_draft(semantic_case, bad):
    def provider(payload, count):
        if payload['stage'] != 'review': return None
        value = review(payload, reject=True)
        if bad == 'privacy': value['issues'][0]['code'] = 'privacy'
        if bad == 'invalid_id': value['issues'][0]['input_ids'] = ['secret/path.java']
        if bad == 'incomplete': value['operation_checks'] = []
        if bad == 'bool': value['input_checks'][0]['checks']['coverage'] = 1
        if bad == 'unknown_code': value['issues'][0]['code'] = 'raw-reason'
        return {'text': json.dumps(value), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    for _ in range(2):
        with pytest.raises(ToolError): execute(setup)
    assert len(setup[1]) == 3
    with semantic_case[0]._db() as db:
        receipts = ''.join(r[0] for r in db.execute('SELECT data FROM business_semantic_tasks'))
    assert 'secret/path.java' not in receipts and 'raw-reason' not in receipts


def test_bounded_revision_pause_after_valid_reject_resumes_at_second_draft(semantic_case):
    paused = False
    def guard():
        if paused: raise InterruptedError('paused')
    rounds = 0
    def provider(payload, count):
        nonlocal paused, rounds
        if payload['stage'] == 'review':
            rounds += 1
            paused = rounds == 1
            return {'text': json.dumps(review(payload, reject=rounds == 1)), 'usage': {}}
    setup = build_case(semantic_case, provider=provider, guard=guard)
    with pytest.raises(InterruptedError): execute(setup)
    assert len(setup[1]) == 3
    paused = False
    assert execute(setup)['complete']
    assert len(setup[1]) == 5


@pytest.mark.parametrize('revise', [False, True])
@pytest.mark.parametrize('local', [False, True])
def test_bounded_revision_integrity_result_committed_rows_and_logs(semantic_case, caplog, revise, local):
    if local:
        semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    import logging
    import sqlite3
    from contextlib import closing
    checks = []
    class CommittedHandler(logging.Handler):
        def emit(self, record):
            if getattr(record, 'event', None) not in {'semantic_slot_reserved', 'semantic_checkpoint_committed'}: return
            with closing(sqlite3.connect(semantic_case[0].db_path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT stage,status,attempt,input_hash FROM business_semantic_tasks WHERE stage_hash=?', (record.stage_hash,)).fetchone()
            # Observe the real transaction boundary at log delivery time.
            assert row == (record.stage, record.status, record.attempt, record.input_hash)
            checks.append(record.event)
    logger = logging.getLogger('businessmap.business_semantics')
    handler = CommittedHandler(); logger.addHandler(handler)
    caplog.set_level(logging.INFO, logger=logger.name)
    rounds = 0
    def provider(payload, count):
        nonlocal rounds
        if payload['stage'] == 'review':
            rounds += 1
            return {'text': json.dumps(review(payload, reject=revise and rounds == 1)), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    try:
        result = execute(setup)
        assert result['complete'] and len(result['public_documents']) == 1
        assert '每家门店最多5套' in result['public_documents'][0]['body']
        expected = [('catalogue', 'accepted', 1), ('proposal', 'proposed', 1),
                    ('review', 'needs_review' if revise else 'accepted', 1)]
        if revise: expected += [('proposal', 'proposed', 2), ('review', 'accepted', 2)]
        with closing(sqlite3.connect(semantic_case[0].db_path.as_uri() + '?mode=ro', uri=True)) as db:
            rows = list(db.execute('SELECT stage,status,attempt FROM business_semantic_tasks ORDER BY rowid'))
        assert rows == expected
        events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_checkpoint_committed' and r.status != 'received']
        assert [(r.stage, r.status, r.attempt) for r in events] == expected
        assert len([r for r in caplog.records if getattr(r, 'event', None) == 'semantic_slot_reserved']) == (5 if revise else 3)
        for event in events:
            assert len(event.stage_hash) == len(event.input_hash) == 64
            assert not any(value in event.getMessage() for value in ('internal/', '每店', 'http:'))
        before = len(caplog.records)
        second = execute(setup)
        assert second['complete'] and second['usage']['requests'] == 0
        assert len(caplog.records) == before and len(setup[1]) == (5 if revise else 3)
        assert checks
    finally:
        logger.removeHandler(handler)


def test_bounded_revision_integrity_rollback_has_no_false_success(semantic_case, caplog):
    import logging
    import sqlite3
    from contextlib import closing
    setup = build_case(semantic_case)
    with semantic_case[0]._db() as db:
        db.execute("CREATE TRIGGER stop_accept BEFORE UPDATE ON business_semantic_tasks WHEN NEW.status='accepted' BEGIN SELECT RAISE(ABORT, 'injected commit refusal'); END")
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    with pytest.raises(sqlite3.IntegrityError): execute(setup)
    with closing(sqlite3.connect(semantic_case[0].db_path.as_uri() + '?mode=ro', uri=True)) as db:
        assert list(db.execute('SELECT stage,status,attempt FROM business_semantic_tasks')) == [('catalogue', 'received', 1)]
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_checkpoint_committed']
    assert [(r.stage, r.status, r.attempt) for r in events] == [('catalogue', 'received', 1)]
    assert not any(getattr(r, 'status', None) == 'accepted' for r in caplog.records)


def test_bounded_revision_resume_preflight_does_not_reserve_paid_subdivision_again(semantic_case):
    setup = oversized_case(semantic_case, independent=True)
    execute(setup)
    paid = len(setup[1])
    def preflight(plan):
        assert plan['occupied_calls'] == paid
        assert plan['remaining_maximum_calls'] == 0
    setup[0].service.semantic_preflight = preflight
    assert execute(setup)['complete']
    assert len(setup[1]) == paid


def test_bounded_revision_reject_does_not_preserve_arbitrary_claim_identifiers(semantic_case):
    def provider(payload, count):
        if payload['stage'] == 'proposal':
            value = candidate(payload)
            value['claims'][0]['id'] = 'private-arbitrary-claim-marker'
            for decision in value['decisions']:
                for part in decision['parts']:
                    part['claim_ids'] = ['private-arbitrary-claim-marker' if cid == 'c00' else cid for cid in part['claim_ids']]
            return {'text': json.dumps(value), 'usage': {}}
        if payload['stage'] == 'review':
            return {'text': json.dumps(review(payload, reject=True)), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError): execute(setup)
    assert all(p['stage'] != 'review' for p in setup[1])
    with semantic_case[0]._db() as db:
        assert all('private-arbitrary-claim-marker' not in row[0] for row in db.execute('SELECT data FROM business_semantic_tasks'))


def test_bounded_revision_inventory_refuses_stage_status_mismatch(semantic_case):
    setup = build_case(semantic_case)
    execute(setup)
    with semantic_case[0]._db() as db:
        db.execute("UPDATE business_semantic_tasks SET status='accepted' WHERE stage='proposal'")
        db.execute("DELETE FROM business_semantic_tasks WHERE stage='review'")
    with pytest.raises(ToolError): setup[0]._raise_cached_failure()
    assert len(setup[1]) == 3


def revised_case(semantic_case):
    rounds = 0
    def provider(payload, count):
        nonlocal rounds
        if payload['stage'] == 'review':
            rounds += 1
            return {'text': json.dumps(review(payload, reject=rounds == 1)), 'usage': {}}
    return build_case(semantic_case, provider=provider)


@pytest.mark.parametrize('marker', ['missing', 'old'])
@pytest.mark.parametrize('missing', ['final_review', 'final_proposal', 'parent'])
def test_final_receipt_policy_cannot_be_selected_by_projection(semantic_case, marker, missing):
    from businessmap.business_semantics import validate_stored_semantic_projection
    setup = revised_case(semantic_case) if missing == 'parent' else build_case(semantic_case)
    projection = execute(setup)
    if marker == 'missing': projection['verification'].pop('revision_policy')
    else: projection['verification']['revision_policy'] = 'old-owner-policy'
    with semantic_case[0]._db() as db:
        stage, attempt = ('proposal', 1) if missing == 'parent' else ('review' if missing == 'final_review' else 'proposal', 1)
        db.execute('DELETE FROM business_semantic_tasks WHERE stage=? AND attempt=?', (stage, attempt))
    with pytest.raises(ToolError):
        validate_stored_semantic_projection(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], store=semantic_case[0])


@pytest.mark.parametrize('hidden', ['running', 'third', 'duplicate', 'terminal', 'recoverable', 'catalogue', 'missing_catalogue', 'unconsumed_same_packet'])
def test_current_stored_projection_checks_all_slots_not_only_final_hashes(semantic_case, hidden):
    from businessmap.business_semantics import _hash, semantic_task_identity_hash, validate_stored_semantic_projection
    setup = build_case(semantic_case)
    projection = execute(setup)
    with semantic_case[0]._db() as db:
        if hidden == 'missing_catalogue':
            db.execute("DELETE FROM business_semantic_tasks WHERE stage='catalogue'")
        else:
            row = list(db.execute("SELECT * FROM business_semantic_tasks WHERE stage=?", ('catalogue' if hidden == 'catalogue' else 'proposal',)).fetchone())
            data = json.loads(row[5])
            if hidden in {'duplicate', 'unconsumed_same_packet'}:
                data['context']['unexpected_slot'] = True
            else:
                data['request']['project'] = '其他完整操作'
                if hidden != 'catalogue': data['context']['packet_hash'] = _hash(data['request'])
            if hidden in {'running', 'terminal'}:
                row[3] = 'running' if hidden == 'running' else 'needs_review'
                data['result'] = None; data['result_hash'] = _hash(None)
            if hidden == 'terminal': data['failure_code'] = 'model_call'; row[6] = 'model_call'
            if hidden == 'third': row[4] = 3; data['context']['revision']['draft_index'] = 3
            row[0] = semantic_task_identity_hash(setup[2], projection['semantic_input_hash'], row[1], data['request'], data['context'])
            data['request_hash'] = row[0]; row[5] = json.dumps(data)
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', row)
    with pytest.raises(ToolError):
        validate_stored_semantic_projection(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], store=semantic_case[0])


def test_current_stored_projection_requires_declared_partition_receipt(semantic_case):
    from businessmap.business_semantics import validate_stored_semantic_projection
    setup = oversized_case(semantic_case, independent=True)
    projection = execute(setup)
    with semantic_case[0]._db() as db: db.execute("DELETE FROM business_semantic_tasks WHERE stage='partition_review'")
    with pytest.raises(ToolError):
        validate_stored_semantic_projection(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], store=semantic_case[0])


@pytest.mark.parametrize('marker', [None, 'legacy'])
def test_current_final_gate_rejects_missing_policy_even_with_all_receipts(semantic_case, marker):
    from businessmap.business_semantics import validate_current_semantic_projection
    setup = build_case(semantic_case); projection = execute(setup)
    projection['verification']['revision_policy'] = marker
    with pytest.raises(ToolError):
        validate_current_semantic_projection(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], task_rows=list(checkpoint_rows(semantic_case[0]).values()))


def test_current_final_gate_rejects_valid_but_unconsumed_recoverable_packet(semantic_case):
    from businessmap.business_semantics import _hash, _review_feedback, semantic_task_identity_hash, semantic_task_inventory, validate_current_semantic_projection
    setup = build_case(semantic_case); projection = execute(setup)
    rows = checkpoint_rows(semantic_case[0]); extra = deepcopy(next(r for r in rows.values() if r['stage'] == 'proposal'))
    data = extra['data']; data['request']['project'] = '其他完整操作'
    data['context']['packet_hash'] = packet = _hash(data['request'])
    extra['stage_hash'] = proposal_key = semantic_task_identity_hash(setup[2], projection['semantic_input_hash'], 'proposal', data['request'], data['context'])
    data['request_hash'] = proposal_key
    payload = {**data['request'], 'stage': 'review', 'candidate': data['result'], 'proposal_hash': proposal_key, 'packet_hash': packet}
    context = {**data['context'], 'proposal_hash': proposal_key, 'claim_ids': {c['id']: 'claim:' + _hash({'packet': packet, 'id': c['id']})[:24] for c in data['result']['claims']}}
    result = review(payload, reject=True)
    key = semantic_task_identity_hash(setup[2], projection['semantic_input_hash'], 'review', payload, context)
    rejected = {'stage_hash': key, 'stage': 'review', 'input_hash': projection['semantic_input_hash'], 'status': 'needs_review', 'attempt': 1,
        'data': {'request_hash': key, 'request': payload, 'context': context, 'result': result, 'result_hash': _hash(result),
                 'failure_code': 'review_rejected', 'feedback': _review_feedback(result, data['result'], payload)}}
    current = [*rows.values(), extra, rejected]
    assert semantic_task_inventory(current, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'])['occupied_calls'] == 5
    with pytest.raises(ToolError):
        validate_current_semantic_projection(projection, snapshot=setup[2], expected_input_hash=projection['semantic_input_hash'], task_rows=current)


def private_multi_packet_case(case, *, local=True):
    """Three complete operations naturally exceed the fixed merge target."""
    if local:
        case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    for unit, row, title in zip(case[3], case[4], ('创建范围', '编辑范围', '维护范围')):
        unit.update(kind='finding', text='资料仅描述当前业务对象的观察范围。' * (400 if local else 100))
        row['title'] = title
    def provider(payload, count):
        if payload['stage'] == 'catalogue':
            value = {'operations': [{'title': item['title'], 'category': 'rules',
                                     'label_ids': [item['label_id']]} for item in payload['items']]}
        elif payload['stage'] == 'proposal':
            value = {'operations': [{'operation_id': op['operation_id'], 'purpose': '', 'summary': '',
                                     'support_ids': []} for op in payload['operations']], 'claims': [],
                     'decisions': [{'input_id': item['fact_id'], 'parts': [
                         {'outcome': 'source_scope', 'rationale': '保留资料观察范围。'}]} for item in payload['items']]}
            if not local:
                for row in value['decisions']:
                    row['parts'][0].update(claim_ids=[], support_ids=[row['input_id']], resolved_by_ids=[])
        else:
            return None
        return {'text': json.dumps(value, ensure_ascii=False), 'usage': {}}
    setup = build_case(case, provider=provider)
    # This component fixture constructs legacy multi-page receipt history.
    setup[0]._catalogue = setup[0]._catalogue_legacy
    projection = execute(setup)
    assert len(projection['verification']['packet_hashes']) == 3
    assert len(projection['semantic_coverage']['catalogue']) == 3
    assert projection['public_documents'] == []
    return setup, projection


def omit_accepted_packet(store, projection, index, *, public_claim=False):
    """Keep authentic remaining rows and the full catalogue; omit one admission."""
    from businessmap import business_semantics as s
    review_key = projection['verification']['accepted_review_hashes'].pop(index)
    projection['verification']['packet_hashes'].pop(index)
    with store._db() as db:
        reviewed = json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?', (review_key,)).fetchone()[0])
        db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash IN (?,?)',
                   (review_key, reviewed['context']['proposal_hash']))
    omitted = set(reviewed['context']['operation_ids'].values())
    operation = next(op for op in projection['semantic_coverage']['catalogue'] if op['id'] in omitted)
    if public_claim:
        fid = operation['fact_ids'][0]
        operation.update(purpose='说明当前业务范围。', summary='当前业务范围已有说明。', support_ids=[fid])
        claim = {'id': 'claim:unreviewed', 'operation_id': operation['id'], 'kind': 'rule',
                 'text': '当前业务范围已有说明。', 'support_ids': [fid], 'scope': '当前业务',
                 'conditions': [], 'section': 'conditions'}
        coverage = projection['semantic_coverage']
        coverage['claims'].append(claim)
        coverage['decisions'][fid] = {'parts': [{'outcome': 'claim', 'claim_ids': [claim['id']],
            'support_ids': [fid], 'resolved_by_ids': [], 'rationale': '未经评审的公开内容。'}]}
        key = 'topic:' + operation['id'].partition(':')[2]
        projection['public_documents'].append({'key': key, 'title': operation['title'], 'category': operation['category'],
            'body': s._render_operation(operation, [claim]), 'fact_ids': [fid]})
        projection['source_mapping'][key] = {field: coverage['inputs'][fid][field] for field in ('evidence_ids', 'source_paths')}
        projection['overview_body'], projection['summary_body'] = s._render_overviews(coverage['project_name'], coverage['catalogue'])
        projection['verification']['catalogue_hash'] = s._hash(coverage['catalogue'])
    projection['verification']['projection_hash'] = s._projection_hash(projection)


def check_complete_receipts(gate, setup, projection):
    from businessmap import business_semantics as s
    kwargs = {'snapshot': setup[2], 'expected_input_hash': projection['semantic_input_hash']}
    with setup[0].service.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, projection['semantic_input_hash'])
    if gate == 'support':
        return s.semantic_support_context(projection, **kwargs, read_checkpoint=rows.read_checkpoint)
    if gate == 'current':
        return s.validate_current_semantic_projection(projection, **kwargs, task_rows=rows)
    return s.validate_stored_semantic_projection(projection, **kwargs, store=setup[0].service.store)


@pytest.mark.parametrize('gate', ['support', 'current', 'stored'])
@pytest.mark.parametrize('index', [0, 1, 2], ids=['first', 'middle', 'last'])
@pytest.mark.parametrize('public_claim', [False, True], ids=['private-only', 'unreviewed-public'])
def test_complete_receipts_reject_missing_entire_packet(semantic_case, gate, index, public_claim):
    from businessmap import business_semantics as s
    setup, projection = private_multi_packet_case(semantic_case)
    check_complete_receipts(gate, setup, projection)
    omit_accepted_packet(semantic_case[0], projection, index, public_claim=public_claim)
    # Current-inventory equality is necessary but does not prove total coverage.
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, projection['semantic_input_hash'])
    inventory = s.semantic_final_task_inventory(projection, snapshot=setup[2],
        expected_input_hash=projection['semantic_input_hash'], task_rows=rows)
    assert set(inventory['task_hashes']) == {row['stage_hash'] for row in rows}
    assert [row['stage'] for row in rows].count('review') == 2
    with pytest.raises(s.SemanticValidationError) as error:
        check_complete_receipts(gate, setup, projection)
    assert error.value.reason_code == 'projection_invalid'


@pytest.mark.parametrize('empty', [False, True], ids=['complete-multi-packet', 'empty'])
@pytest.mark.parametrize('local', [False, True], ids=['http', 'codex'])
def test_complete_receipts_accept_exact_full_or_empty_sets(semantic_case, empty, local):
    if local:
        semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    if empty:
        semantic_case[3].clear(); semantic_case[4].clear()
        setup = build_case(semantic_case); projection = execute(setup)
    else:
        setup, projection = private_multi_packet_case(semantic_case, local=local)
    for gate in ('support', 'current', 'stored'):
        check_complete_receipts(gate, setup, projection)


def catalogue_evidence_case(units, *, local=True, respond=None):
    """Capture only synthetic transport, leaving real packing/normalization intact."""
    from businessmap.business_semantics import BusinessSemanticIntegration, _hash
    builder = object.__new__(BusinessSemanticIntegration)
    # Exercise the retained legacy catalogue transport in isolation.
    builder._catalogue = builder._catalogue_legacy
    builder._snapshot = {'project': {'name': '门店运营'}, 'provider': {
        'kind': 'codex-local' if local else 'openai-compatible',
        'base_url': 'codex://local' if local else 'http://127.0.0.1:1/v1'}}
    builder._units = {unit['_fact_id']: unit for unit in units}
    builder._forbidden, builder._matching, builder._partitions = [], [], []
    builder._catalogue_tasks, builder.plans = [], []
    calls, admissions = [], []
    builder._task_rows = lambda: {}
    builder._preflight = lambda stage, count: admissions.append((stage, count))
    def call(stage, system, payload, normalize, *, context):
        calls.append(deepcopy(payload))
        result = respond(payload) if respond else {'operations': [{
            'title': '创建门店方案', 'category': 'flows',
            'label_ids': [item['label_id'] for item in payload['items']]}]}
        return normalize(result), _hash({'request': payload, 'context': context})
    builder._call = call
    return builder, calls, admissions


@pytest.mark.parametrize('reverse', [False, True])
def test_leaf_catalogue_dispatch_retains_all_complete_members_and_scope_then_packets_together(reverse):
    units = [
        {'_fact_id': 'requirement', 'kind': 'finding', 'text': '平台角色为必填；操作人须有维护权限。',
         '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['backend'], 'evidence_ids': ['opaque-e'], 'match_receipt_ids': ['opaque-m']}},
        {'_fact_id': 'conflict', 'kind': 'unknown', 'text': '当前页面角色允许留空；正常保存及并发结果尚未验证。',
         '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['frontend']}},
        {'_fact_id': 'original', 'kind': 'context', 'text': '创建时等级起点待核实；不得自动补齐未声明的条件。',
         '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['backend']}},
        {'_fact_id': 'supplement', 'kind': 'finding', 'text': '创建等级从空档位起步，且必须同时具备维护权限和可管理门店范围。',
         '_observation': {'scope': 'matched_operation', 'origin': 'addendum', 'roles': ['frontend', 'backend'], 'match_receipt_ids': ['opaque-proof']}},
    ]
    if reverse:
        units.reverse()
    routes = [{'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'flows',
               'title': {'requirement': '角色资料', 'conflict': '字段检查', 'original': '创建说明', 'supplement': '创建说明'}[unit['_fact_id']]} for unit in units]
    builder, calls, _ = catalogue_evidence_case(units)
    operations = builder._catalogue(routes)
    wire = calls[0]
    members = [fact for item in wire['items'] for fact in item.get('member_facts', [])]
    assert {fact['text'] for fact in members} == {unit['text'] for unit in units}
    assert len(members) == 4 and len({fact['fact_id'] for fact in members}) == 4
    for unit in units:
        fact = next(fact for fact in members if fact['text'] == unit['text'])
        assert fact['kind'] == unit['kind']
        assert fact['observation'] == {key: unit['_observation'][key] for key in ('scope', 'origin', 'roles')}
    assert 'opaque-' not in json.dumps(wire) and wire['mode'] == 'leaf'
    # The synthetic provider supplies grouping, not evidence of model quality.
    packet, _, _ = builder._packet(operations)
    assert {fact['text'] for fact in packet['items']} == {unit['text'] for unit in units}
    assert len(packet['operations']) == 1
    assert packet['items'][0]['observation'] == units[0]['_observation']


def test_byte_paged_catalogue_preserves_every_original_alias_at_upper_merge():
    units = [{'_fact_id': f'fact:{i}', 'kind': 'finding', 'text': '完整条件' * 100,
              '_observation': {'scope': 'source_subset'}} for i in range(12)]
    titles = [f'门店业务别名{chr(0x7532 + i)}' for i in range(12)]
    routes = [{'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'flows', 'title': title}
              for unit, title in zip(units, titles)]
    builder, calls, admissions = catalogue_evidence_case(units)
    builder.WIRE_BYTES = 6000
    operations = builder._catalogue(routes)
    leaves = [wire for wire in calls if wire.get('mode') == 'leaf']
    merges = [wire for wire in calls if wire.get('mode') == 'merge']
    assert len(leaves) > 1 and merges
    assert all('aliases' not in item and item['alias_count'] > 0 and len(item['alias_digest']) == 64
               for wire in merges for item in wire['items'])
    assert all('member_facts' not in item for wire in merges for item in wire['items'])
    assert set(operations[0]['fact_ids']) == {unit['_fact_id'] for unit in units}
    assert set(operations[0]['aliases']) == set(titles)
    assert builder.plans[0]['maximum_calls'] >= len(calls)
    assert sum(count for _, count in admissions) == len(calls)


@pytest.mark.parametrize("member_count", [24, 1200])
def test_overflowing_internal_label_pages_complete_facts_without_claiming_independence(member_count):
    units = [{'_fact_id': f'internal:{i}', 'kind': 'context', 'text': f'内部观察{chr(0x7532+i)}：' + '完整内容' * 100,
              '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['backend']}} for i in range(member_count)]
    routes = [{'fact_id': unit['_fact_id'], 'disposition': 'internal'} for unit in units]
    builder, calls, _ = catalogue_evidence_case(units)
    builder.WIRE_BYTES = 6000
    operations = builder._catalogue(routes)
    texts = [fact['text'] for wire in calls if wire.get('mode') == 'leaf'
             for item in wire['items'] for fact in item['member_facts']]
    assert sorted(texts) == sorted(unit['text'] for unit in units)
    assert set(operations[0]['fact_ids']) == {unit['_fact_id'] for unit in units}
    assert operations[0]['aliases'] == ['内部资料']
    assert all(wire['stage'] == 'catalogue' for wire in calls)


def test_single_complete_fact_overflow_stops_before_any_catalogue_dispatch():
    units = [{'_fact_id': 'huge', 'kind': 'context', 'text': '同一句完整混合观察' * 12000}]
    builder, calls, admissions = catalogue_evidence_case(units)
    with pytest.raises(ToolError) as failure:
        builder._catalogue([{'fact_id': 'huge', 'disposition': 'internal'}])
    assert failure.value.reason_code == 'input_limit' and calls == [] and admissions == []


def test_codex_complete_operation_above_old_candidate_limit_stays_atomic_with_new_profile():
    units = [{'_fact_id': 'complete', 'kind': 'finding', 'text': '共同业务条件' * 1800}]
    builder, _, _ = catalogue_evidence_case(units)
    operation = {'id': 'whole', 'title': '创建门店方案', 'category': 'flows', 'fact_ids': ['complete'], 'aliases': ['创建']}
    # 32,400 original bytes +420 member +500 operation: above the old32k.
    assert len(units[0]['text'].encode()) + 920 == 33320
    assert builder._packet_fits([operation])
    assert builder._packet([operation])[0]['budget']['candidate_bytes'] == 40000


@pytest.mark.parametrize('member_count', [63, 64])
@pytest.mark.parametrize('scope_repetitions', [75, 89])
def test_many_input_complete_operation_keeps_review_reserve_headroom(member_count, scope_repetitions):
    from businessmap import business_semantics as s
    units = [{'_fact_id': f'synthetic:{index}', 'kind': 'finding', 'text': '共同业务条件' * 10,
              '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['frontend', 'backend'],
                               'evidence_ids': [f'evidence:{index}'], 'match_receipt_ids': []}}
             for index in range(member_count)]
    builder, _, _ = catalogue_evidence_case(units)
    builder._matching = [{'match_id': 'synthetic-match', 'operation': '活动报名', 'conclusion': 'static_request_chain',
        'evidence_ids': [f'source:{index}' for index in range(member_count)],
        'review_note': '仅确认资料声明的报名条件，不证明实际运行。' * scope_repetitions}]
    operation = {'id': 'synthetic-operation', 'title': '报名条件', 'category': 'rules',
                 'fact_ids': [unit['_fact_id'] for unit in units], 'aliases': []}
    payload, _, _ = builder._packet([operation])
    profile = builder._profile()
    estimate = sum(len(unit['text'].encode()) + 420 for unit in units) + 500
    assert estimate <= 40000
    assert builder._request_bytes('proposal', s.GRAPH_PROPOSAL_SYSTEM, payload) <= 64000
    review = builder._request_bytes('review', s.GRAPH_REVIEW_SYSTEM, {**payload, 'stage': 'review', 'candidate': {}})
    total = review + profile['candidate_bytes'] * 3
    assert builder._packet_fits([operation]), f'review with candidate reserve: {total}/160000'
    assert total <= 159600, f'complete packet needs 400 bytes review headroom: {total}/160000'


def test_catalogue_policy_changes_input_generator_task_and_direct_build_identities(semantic_case, monkeypatch):
    import businessmap.business_semantics as semantics
    setup = build_case(semantic_case)
    first = execute(setup)
    input_hash = semantics.semantic_input_hash(setup[2], setup[3], [], [])
    generator = semantics._semantic_generator_hash(setup[2])
    task = semantics.semantic_task_identity_hash(setup[2], '0' * 64, 'catalogue', {'stage': 'catalogue'}, {})
    monkeypatch.setattr(semantics, 'CATALOGUE_POLICY', 'changed-full-evidence-policy')
    assert semantics.semantic_input_hash(setup[2], setup[3], [], []) != input_hash
    assert semantics._semantic_generator_hash(setup[2]) != generator
    assert semantics.semantic_task_identity_hash(setup[2], '0' * 64, 'catalogue', {'stage': 'catalogue'}, {}) != task
    second = execute(setup)
    assert first['semantic_input_hash'] != second['semantic_input_hash']
    assert second['usage']['requests'] == 3 and len(setup[1]) == 6


@pytest.mark.parametrize('size', [32000, 32001, 40000, 40001])
def test_actual_codex_candidate_json_hard_boundary_and_no_extra_retry(semantic_case, size):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    sizes = []
    def mutate(result, payload):
        from businessmap.business_semantics import _json
        from businessmap.business_semantics import decode_support_graph
        result['decisions'][0]['parts'].append({'outcome': 'source_scope', 'rationale': '范围核对。'})
        refs = {i['fact_id']: {'_fact_id': i['fact_id'], 'kind': i['kind'], 'text': i['text']} for i in payload['items']}
        ops = {o['operation_id']: {'fact_ids': o['fact_ids']} for o in payload['operations']}
        initial = len(_json(decode_support_graph(result, refs, ops, candidate_bytes=40000)).encode())
        result['decisions'][0]['parts'][0]['rationale'] += 'x' * (size - initial)
        sizes.append(size)
    setup = build_case(semantic_case, mutate=mutate)
    if size <= 40000:
        result = execute(setup)
        assert result['complete'] and [wire['stage'] for wire in setup[1]] == ['catalogue', 'proposal', 'review']
        execute(setup)
        assert len(setup[1]) == 3 and sizes == [size]
    else:
        for _ in range(2):
            with pytest.raises(ToolError) as failure:
                execute(setup)
            assert failure.value.reason_code == 'candidate_limit'
        assert sizes == [40001, 40001]
        assert [wire['stage'] for wire in setup[1]] == ['catalogue', 'proposal', 'proposal']


def test_catalogue_remaining_calls_compare_exact_full_payload_and_mode():
    import businessmap.business_semantics as semantics
    units = [{'_fact_id': 'fact', 'kind': 'finding', 'text': '完整业务条件',
              '_observation': {'scope': 'source_subset', 'origin': 'chunk', 'roles': ['backend']}}]
    routes = [{'fact_id': 'fact', 'disposition': 'public', 'category': 'rules', 'title': '门店创建'}]
    builder, calls, admissions = catalogue_evidence_case(units)
    builder._catalogue(routes)
    saved = {'stage': 'catalogue', 'status': 'accepted', 'data': {'request': deepcopy(calls[0]), 'context': {
        'label_members': {'l0': ['fact']}, 'catalogue_mode': 'leaf',
        'budget_profile': builder._profile(), 'generator_hash': semantics._semantic_generator_hash(builder._snapshot)}}}
    builder._task_rows = lambda: {'receipt': saved}
    admissions.clear()
    builder._catalogue(routes)
    assert admissions == [('catalogue', 0)]
    saved['data']['request']['items'][0]['member_facts'][0]['observation']['scope'] = 'matched_operation'
    admissions.clear()
    builder._catalogue(routes)
    assert admissions == [('catalogue', 1)]
    saved['data']['request'] = deepcopy(calls[0])
    saved['data']['context']['catalogue_mode'] = 'merge'
    admissions.clear()
    builder._catalogue(routes)
    assert admissions == [('catalogue', 1)]


def test_upper_alias_budget_stall_preserves_aliases_and_stops_before_futile_level():
    import businessmap.business_semantics as semantics
    units = [{'_fact_id': f'fact:{i}', 'kind': 'finding', 'text': '业务条件完整。'} for i in range(100)]
    titles = ['原始业务名称' * 7 + chr(0x7532 + i) for i in range(100)]
    routes = [{'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'flows', 'title': title}
              for unit, title in zip(units, titles)]
    builder, calls, _ = catalogue_evidence_case(units)
    builder.WIRE_BYTES = 6000
    operations = builder._catalogue(routes)
    assert all(builder._request_bytes('catalogue', semantics.CATALOGUE_SYSTEM, wire) <= 6000 for wire in calls)
    assert len(operations) == 1
    assert operations[0]['aliases'] == titles
    assert set(operations[0]['fact_ids']) == {unit['_fact_id'] for unit in units}
    assert all('aliases' not in item for wire in calls if wire.get('mode') == 'merge' for item in wire['items'])


def test_single_merge_item_with_complete_aliases_over_cap_never_dispatches(semantic_case):
    setup = build_case(semantic_case)
    execute(setup)
    builder = setup[0]
    origin_id = next(iter(builder._catalogue_origins))
    label = {'id': origin_id, 'title': '完整标题' * 20000, 'category': 'flows',
             'fact_ids': [setup[4][0]['_fact_id']], 'aliases': ['私有别名']}
    with pytest.raises(ToolError) as failure:
        builder._catalogue_page([label], mode='merge')
    assert failure.value.reason_code == 'input_limit' and len(setup[1]) == 3


def test_multi_operation_merge_target_uses_actual_full_text_cost_at_34000():
    units = [{'_fact_id': str(i), 'kind': 'finding', 'text': '条' * 5360} for i in range(2)]
    builder, _, _ = catalogue_evidence_case(units)
    operations = [{'id': str(i), 'title': '维护门店方案', 'category': 'flows', 'fact_ids': [str(i)], 'aliases': []} for i in range(2)]
    assert sum(len(unit['text'].encode()) for unit in units) + 2 * 420 + 2 * 500 == 34000
    assert builder._packet_fits(operations)
    builder._units['0']['text'] += 'x'
    assert not builder._packet_fits(operations)
    assert all(builder._packet_fits([operation]) for operation in operations)


@pytest.mark.parametrize("change", ["none", "peer_commit", "source_scope", "packet_policy", "late_callback", "stop"])
def test_settled_first_draft_replay_requires_the_same_preflight(semantic_case, monkeypatch, change):
    from businessmap.business_semantics import SemanticValidationError
    setup = build_case(semantic_case)
    runtime, calls = setup[:2]
    original = execute(setup)
    sent = len(calls)
    slow = []
    integrate = runtime._integrate_packet

    def integrated(packet):
        slow.append(True)
        return integrate(packet)

    monkeypatch.setattr(runtime, "_integrate_packet", integrated)
    select = runtime._settled_first_drafts

    def select_then_change(rows, packets):
        found = select(rows, packets)
        assert len(found) == 1
        if change == "peer_commit":
            runtime.store.config_set("synthetic_peer_commit", True)
        if change == "packet_policy":
            runtime._packet_policy = (runtime._packet_policy[0], ())
        if change == "late_callback":
            def updated_control(plan):
                runtime._run["snapshot_id"] += "-changed"
            monkeypatch.setattr(runtime.service, "semantic_preflight", updated_control, raising=False)
        if change == "source_scope":
            runtime._run["snapshot_id"] += "-changed"
        if change == "stop":
            runtime._stopped.set()
        return found

    monkeypatch.setattr(runtime, "_settled_first_drafts", select_then_change)
    if change in {"source_scope", "packet_policy", "late_callback", "stop"}:
        with pytest.raises(SemanticValidationError) as failure:
            execute(setup)
        assert failure.value.reason_code == ("needs_review" if change == "stop" else "cache_invalid")
    else:
        replay = execute(setup)
        assert replay["public_documents"] == original["public_documents"]
        assert replay["usage"]["cache_hits"] == 3 and replay["usage"]["requests"] == 0
        assert bool(slow) == (change == "peer_commit")
    assert len(calls) == sent


def test_older_generator_review_does_not_skip_a_current_first_review(semantic_case):
    from businessmap import business_semantics as sem
    setup = build_case(semantic_case)
    runtime, calls, snapshot = setup[:3]
    original = execute(setup)
    with runtime.store._db() as db:
        old, raw = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='review'").fetchone()
        data = json.loads(raw)
        data["context"]["generator_hash"] = "a" * 64
        key = sem.semantic_task_identity_hash(snapshot, runtime._input_hash, "review", data["request"], data["context"])
        data["request_hash"] = key
        db.execute("UPDATE business_semantic_tasks SET stage_hash=?,data=? WHERE stage_hash=?", (key, sem._json(data), old))
    sent = len(calls)
    replay = execute(setup)
    assert [r["stage"] for r in calls[sent:]] == ["review"]
    assert original["public_documents"] == replay["public_documents"]
