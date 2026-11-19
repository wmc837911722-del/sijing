from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from businessmap.business_projects import BusinessProjects
from businessmap.core import Store
from businessmap.project_analysis import ProjectAnalysis
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService


@pytest.fixture
def synthesis_project(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    for rid in ("front", "back"):
        path = tmp_path / rid
        path.mkdir()
        store._write_repo({"id": rid, "name": rid, "vault_path": str(store.vault), "status": "ready", "commit": "a" * 40,
                           "path": str(path), "url": "fixture", "auto_sync": False})
    service = ToolsService(store)
    provider = service.providers.save({"name": "Fixture", "kind": "openai-compatible", "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    project = BusinessProjects(store).create("完整赛事", "", ["front", "back"], {"front": "frontend", "back": "backend"})
    ProjectAnalysis(service).bind_model(project["id"], provider["id"])
    snapshot = ProjectAnalysis(service).snapshot(project["id"])
    run = {"run_id": "fixture-run", "modules": [{"id": f"module-{i}", "name": f"src/internal/folder-{i}", "directory": f"src/internal/folder-{i}", "repository_ids": ["front" if i % 2 else "back"]} for i in range(11)]}
    chunks = [{"id": f"chunk-{i}", "module_id": f"module-{i % 11}", "status": "completed", "result": {
        "title": "订单支付", "summary": f"已核对业务批次 {i}", "findings": [{"text": f"订单在条件 {i} 下允许支付", "evidence_ids": [f"evidence-{i}"]}], "unknowns": ["跨端调用链尚未核对"]}} for i in range(31)]
    # These regressions exercise the real L0/cache boundary. Independent
    # semantic proposal/review behavior has its own integration tests.
    from businessmap.business_semantics import BusinessSemanticIntegration
    from business_projection_fixture import projection_from_units
    monkeypatch.setattr(BusinessSemanticIntegration, "build", lambda self, snapshot, run, **kwargs: projection_from_units(snapshot, run, **kwargs))
    yield service, snapshot, run, chunks
    store.close()


def fake_model(monkeypatch, service, callback=None, omit=False):
    sent = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        assert kwargs["json_mode"] is True
        payload = json.loads(user)
        sent.append(payload)
        if callback:
            callback(payload, len(sent))
        items = payload["items"]
        if omit:
            items = items[:1]
        # A second real database connection can acquire a write transaction:
        # provider calls must not be inside the checkpoint transaction.
        with sqlite3.connect(service.store.db_path, timeout=0.1) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
            "title": "订单与支付"} for item in items]}, ensure_ascii=False),
            "usage": {"input_tokens": 100, "output_tokens": 80}}
    monkeypatch.setattr(service.providers, "complete", complete)
    return sent


def test_all_chunks_semantically_synthesized_with_bounded_wire_and_durable_reuse(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    sent = fake_model(monkeypatch, service)
    result = synthesis.build(snapshot, run, chunks)
    assert result["complete"]
    assert {item["chunk_id"] for request in sent for item in request["items"] if "chunk_id" in item} == {chunk["id"] for chunk in chunks}
    assert all(len(json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()) + len(synthesis.SYSTEM.encode()) <= synthesis.WIRE_BYTES for request in sent)
    assert len(result["semantic_coverage"]["input_ids"]) == 93
    assert set(result["semantic_coverage"]["input_ids"]) == set(result["semantic_coverage"]["decisions"])
    assert len(result["public_documents"]) == 1
    text = result["public_documents"][0]["body"]
    assert "跨端调用链尚未核对" in text
    for chunk in chunks:
        assert chunk["result"]["findings"][0]["text"] in text
    assert "module:" not in result["overview_body"] and "src/internal/folder" not in result["overview_body"]
    previous = len(sent)
    resumed_service = FullBusinessSynthesis(service)
    resumed_service.MAX_ITEMS = synthesis.MAX_ITEMS
    resumed = resumed_service.build(snapshot, run, chunks)
    assert resumed["overview_body"] == result["overview_body"] and len(sent) == previous
    assert resumed["usage"]["requests"] == 0 and resumed["usage"]["cache_hits"] > 0


def test_failed_synthesis_has_no_automatic_retry_and_only_failed_inputs_are_resent(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    synthesis.CONCURRENCY = 1
    def fail_second(payload, count):
        if count == 2:
            raise ToolError("provider quota raw-secret should not escape", 429)
    failed_calls = fake_model(monkeypatch, service, fail_second)
    with pytest.raises(ToolError) as error:
        synthesis.build(snapshot, run, chunks)
    assert "raw-secret" not in str(error.value) and len(failed_calls) == 2
    first_request = failed_calls[0]
    new_calls = fake_model(monkeypatch, service)
    assert synthesis.build(snapshot, run, chunks)["complete"]
    assert first_request not in new_calls


def test_unknown_or_missing_fact_assignments_are_rejected_without_semantic_fallback(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    def forged(system, user, **kwargs):
        return {"text": json.dumps({"assignments": [{"fact_id": "foreign", "disposition": "internal", "classification": "technical", "reason": "伪造"}]})}
    monkeypatch.setattr(service.providers, "complete", forged)
    with pytest.raises(ToolError):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)
    fake_model(monkeypatch, service, omit=True)
    with pytest.raises(ToolError, match="事实"):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)


def test_snapshot_changes_and_pause_preserve_cache_without_publishing(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    synthesis.CONCURRENCY = 1
    def pause(payload, count):
        service.store.config_set(f"full_analysis_control:{run['run_id']}", {"paused": True, "cancelled": False})
    calls = fake_model(monkeypatch, service, pause)
    paused = synthesis.build(snapshot, run, chunks)
    assert paused["complete"] is False and paused["paused"] is True and len(calls) == 1
    service.store.config_set(f"full_analysis_control:{run['run_id']}", {"paused": False, "cancelled": False})
    sent = fake_model(monkeypatch, service)
    assert synthesis.build(snapshot, run, chunks)["complete"] and calls[0] not in sent
    service.store.update_repository("front", {"commit": "b" * 40})
    with pytest.raises(ToolError) as error:
        synthesis.build(snapshot, run, chunks)
    assert error.value.status_code == 409


def test_unbounded_single_summary_and_incomplete_receipts_are_not_silently_truncated(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    sent = fake_model(monkeypatch, service)
    chunks[0]["result"]["summary"] = "大" * 40000
    with pytest.raises(ToolError):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert not sent
    chunks[0]["result"]["summary"] = "有效摘要"
    chunks[-1]["status"] = "failed"
    with pytest.raises(ToolError):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert not sent


def test_empty_only_modules_are_explicit_without_paid_requests(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    run["files"] = [{"module_id": module["id"], "line_count": 0} for module in run["modules"]]
    calls = fake_model(monkeypatch, service)
    result = FullBusinessSynthesis(service).build(snapshot, run, [])
    assert result["complete"] and not calls
    assert result["semantic_coverage"]["input_ids"] == []
    assert result["public_documents"] == []
    assert "空文件" not in result["overview_body"]


def test_runtime_epoch_does_not_invalidate_persisted_semantic_cache(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    calls = fake_model(monkeypatch, service)
    first = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    previous = len(calls)
    service.store._vault_epoch += 1
    snapshot["vault_epoch"] = service.store._vault_epoch
    resumed = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert first["overview_body"] == resumed["overview_body"] and len(calls) == previous


def test_version_four_internal_cache_is_not_reused_after_classification_fix(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis, _hash, _json
    service, snapshot, run, _ = synthesis_project
    run["snapshot_id"] = "fixture-snapshot"
    synthesis = FullBusinessSynthesis(service)
    text = "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额；该规则尚无测试覆盖。"
    units = [{"kind": "finding", "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"],
              "_source_paths": ["back/service/RefundService.java"]}]
    old_key = _hash({"version": 4, "system": synthesis.SYSTEM, "wire": synthesis._wire(snapshot, units, 0),
                     "members": [unit["_module_ids"] for unit in units], "snapshot_id": run["snapshot_id"]})
    old_result = {"assignments": [{"fact_id": "private-fact", "disposition": "internal", "classification": "test",
                                  "reason": "测试说明", "classification_basis": {"supported_categories": ["test"], "evidence_ids": []}}]}
    with service.store._lock, service.store._db() as db:
        db.execute("INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)", (old_key, old_key, _json(old_result), "2026-10-27"))
    calls = fake_model(monkeypatch, service)
    usage = {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    result = synthesis._request(snapshot, run, units, 0, usage)
    assert len(calls) == 1 and usage["requests"] == 1 and usage["cache_hits"] == 0
    assert result["assignments"][0]["disposition"] == "public"
    assert "text" not in result["assignments"][0]
    next_usage = {key: 0 for key in usage}
    assert synthesis._request(snapshot, run, units, 0, next_usage) == result
    assert len(calls) == 1 and next_usage["cache_hits"] == 1 and next_usage["requests"] == 0
    with service.store._db() as db:
        assert json.loads(db.execute("SELECT data FROM full_analysis_synthesis WHERE cache_key=?", (old_key,)).fetchone()[0]) == old_result
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == 2


def test_version_five_forced_public_style_cache_is_not_reused(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis, _hash, _json
    service, snapshot, run, _ = synthesis_project
    run["snapshot_id"] = "fixture-snapshot"
    synthesis = FullBusinessSynthesis(service)
    text = "选中选项以橙色突出。"
    units = [{"kind": "finding", "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"],
              "_source_paths": ["front/styles/theme.css"], "_evidence_ids": ["private-evidence"]}]
    old_key = _hash({"version": 5, "system": synthesis.SYSTEM, "wire": synthesis._wire(snapshot, units, 0),
                     "members": [["private-module"]], "snapshot_id": run["snapshot_id"]})
    old_result = {"assignments": [{"fact_id": "private-fact", "disposition": "public", "category": "rules",
                                  "title": "界面配色", "text": text}]}
    with service.store._db() as db:
        db.execute("INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)", (old_key, old_key, _json(old_result), "2026-10-27"))
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        return {"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "internal",
            "classification": "technical", "reason": "纯呈现样式"}]})}
    monkeypatch.setattr(service.providers, "complete", complete)
    usage = {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    result = synthesis._request(snapshot, run, units, 0, usage)
    assert result["assignments"][0]["disposition"] == "internal"
    assert usage["requests"] == 1 and usage["cache_hits"] == 0
    assert synthesis._request(snapshot, run, units, 0, usage) == result
    assert usage["requests"] == 1 and usage["cache_hits"] == 1
    with service.store._db() as db:
        assert json.loads(db.execute("SELECT data FROM full_analysis_synthesis WHERE cache_key=?", (old_key,)).fetchone()[0]) == old_result


def test_version_six_cache_is_not_reused_for_source_owned_text(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis, _hash, _json
    service, snapshot, run, _ = synthesis_project
    run["snapshot_id"] = "fixture-snapshot"
    synthesis = FullBusinessSynthesis(service)
    text = "退款条件尚未确认。"
    units = [{"kind": "unknown", "text": text, "_fact_id": "private-fact", "_module_ids": ["private-module"]}]
    old_key = _hash({"version": 6, "system": synthesis.SYSTEM, "wire": synthesis._wire(snapshot, units, 0),
                     "members": [["private-module"]], "snapshot_id": run["snapshot_id"]})
    old_result = {"assignments": [{"fact_id": "private-fact", "disposition": "public", "category": "rules", "title": "退款条件", "text": text}]}
    with service.store._db() as db:
        db.execute("INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)", (old_key, old_key, _json(old_result), "2026-10-27"))
    fake_model(monkeypatch, service)
    usage = {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    result = synthesis._request(snapshot, run, units, 0, usage)
    assert "text" not in result["assignments"][0]
    assert usage["requests"] == 1 and usage["cache_hits"] == 0
    assert synthesis._request(snapshot, run, units, 0, usage) == result
    assert usage["requests"] == 1 and usage["cache_hits"] == 1
    with service.store._db() as db:
        assert json.loads(db.execute("SELECT data FROM full_analysis_synthesis WHERE cache_key=?", (old_key,)).fetchone()[0]) == old_result


def test_source_owned_unknown_is_preserved_in_output_cache_and_original_coverage(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, _ = synthesis_project
    run["modules"] = [run["modules"][0]]
    text = "尚未看到业务入口及调用方，无法确认段位写操作和超管专属操作是否均接入对应守卫，也无法确认门店可写校验的具体规则。"
    chunks = [{"id": "unknown-chunk", "module_id": run["modules"][0]["id"], "status": "completed",
        "input_hash": "original-source-receipt", "segments": [],
        "result": {"title": "段位业务", "summary": "", "findings": [], "unknowns": [text]}}]
    original = json.loads(json.dumps(chunks))
    calls = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        item = json.loads(user)["items"][0]
        assert "text_policy" not in item and item["internal_categories"] == []
        assert item["kind"] == "unknown" and item["text"] == text
        calls.append(True)
        return {"text": json.dumps({"assignments": [{"fact_id": "f0", "disposition": "public", "category": "rules",
            "title": "段位权限"}]})}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    result = synthesis.build(snapshot, run, chunks)
    assert result["complete"] and chunks == original and len(calls) == 1
    assert "## 待确认\n\n- " + text in result["public_documents"][0]["body"]
    assert "## 业务说明" not in result["public_documents"][0]["body"]
    assert next(iter(result["semantic_coverage"]["inputs"].values()))["text"] == text
    assert synthesis.build(snapshot, run, chunks)["public_documents"] == result["public_documents"]
    assert len(calls) == 1


def test_stylesheet_projection_retains_originals_and_receipts_in_private_coverage(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, _ = synthesis_project
    run["modules"] = [run["modules"][0]]
    style = "内部界面规范以橙色作为主色，统一配置文字、背景、间距与字号，并提供六种标签配色；未见配色与具体业务状态的对应关系。"
    unknown = "退款条件尚未确认。"
    chunks = [{"id": "style-chunk", "module_id": run["modules"][0]["id"], "status": "completed",
        "input_hash": "original-source-receipt", "origin_provider": {"id": "original-source-provider"},
        "segments": [{"evidence_id": "style-evidence", "repository_id": "front", "path": "src/styles/base.css"}],
        "result": {"title": "界面样式", "summary": "", "findings": [{"text": style, "evidence_ids": ["style-evidence"]}],
                   "unknowns": [unknown]}}]
    original = json.loads(json.dumps(chunks))
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        items = json.loads(user)["items"]
        assert [item["internal_categories"] for item in items] == [["configuration", "technical"], []]
        assert "src/styles" not in user and "style-evidence" not in user
        return {"text": json.dumps({"assignments": [
            {"fact_id": "f0", "disposition": "internal", "classification": "configuration", "reason": "纯呈现配置"},
            {"fact_id": "f1", "disposition": "public", "category": "rules", "title": "退款条件"}]})}
    monkeypatch.setattr(service.providers, "complete", complete)
    result = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert result["complete"] and chunks == original
    coverage = result["semantic_coverage"]
    assert len(coverage["input_ids"]) == 2 and len(coverage["claims"]) == 1
    internal_id = next(fid for fid, decision in coverage["decisions"].items() if decision["parts"][0]["outcome"] == "technical")
    assert {key: value for key, value in coverage["inputs"][internal_id].items() if key != "observation"} == {"chunk_id": "style-chunk", "kind": "finding", "text": style,
        "evidence_ids": ["style-evidence"], "source_paths": ["front/src/styles/base.css"]}
    assert style not in json.dumps(result["public_documents"], ensure_ascii=False)
    assert unknown in result["public_documents"][0]["body"]


def test_all_leaf_business_domains_survive_parent_reduction_and_keep_specific_titles(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, _ = synthesis_project
    topics = ["账号", "租户", "权限", "报名", "赛事", "赛季", "比赛", "记分", "积分", "段位", "排行", "奖励", "宝箱", "成就", "支付"]
    run["modules"] = [{"id": f"module-{index}", "name": f"src/technical-{index}", "repository_ids": ["back"]} for index in range(len(topics))]
    chunks = [{"id": f"chunk-{index}", "module_id": f"module-{index // 2}", "status": "completed", "result": {
        "title": topics[index // 2], "summary": f"{topics[index // 2]}的实际业务职责与状态规则", "findings": [{"text": f"{topics[index // 2]}在特定状态下允许操作", "evidence_ids": [f"evidence-{index}"]}], "unknowns": []}} for index in range(len(topics) * 2)]
    levels = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        levels.append(payload["level"])
        return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
            "title": item["title"]} for item in payload["items"]]}, ensure_ascii=False), "usage": {}}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    result = synthesis.build(snapshot, run, chunks)
    assert {document["title"] for document in result["public_documents"]} == set(topics)
    assert len(result["public_documents"]) == 15
    text = "\n".join(document["body"] for document in result["public_documents"])
    for chunk in chunks:
        assert chunk["result"]["findings"][0]["text"] in text
    assert len(result["semantic_coverage"]["claims"]) == len(chunks) * 2
    assert "src/technical" not in text and "module:" not in result["overview_body"]


def test_full_permitted_schema_has_sufficient_bounded_output_budget(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    for chunk in chunks:
        chunk["result"]["summary"] += "业务说明" * 30
    budgets = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        budgets.append(kwargs["max_output"])
        assert kwargs["max_output"] == 6000
        payload = json.loads(user)
        result = {"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
                  "title": "业务主题" + str(item["position"])} for item in payload["items"]]}
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {"output_tokens": 3100}, "output_limited": False}
    monkeypatch.setattr(service.providers, "complete", complete)
    result = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert result["complete"] and len(result["public_documents"]) == 93
    assert result["usage"]["output_tokens"] == len(budgets) * 3100
    text = "\n".join(document["body"] for document in result["public_documents"])
    assert "业务说明" * 30 in text


def test_output_limited_synthesis_stops_without_cache_or_automatic_retry(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    calls = []
    def limited(system, user, **kwargs):
        calls.append(True)
        return {"text": "{}", "output_limited": True, "usage": {"output_tokens": 6000}}
    monkeypatch.setattr(service.providers, "complete", limited)
    synthesis = FullBusinessSynthesis(service)
    synthesis.CONCURRENCY = 1
    with pytest.raises(ToolError) as error:
        synthesis.build(snapshot, run, chunks)
    assert error.value.reason_code == "output_limit"
    assert len(calls) == 1
    with service.store._db() as db:
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == 0


def _semantic_answer(payload):
    return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
        "title": "业务" + item["chunk_id"].split("-")[-1].zfill(2)} for item in payload["items"]]}, ensure_ascii=False),
        "usage": {"input_tokens": 7, "output_tokens": 3}}


def test_synthesis_runs_bounded_parallel_requests_and_aggregates_usage(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    barrier, lock = threading.Barrier(4), threading.Lock()
    active, maximum, calls = 0, 0, []
    def complete(system, user, **kwargs):
        nonlocal active, maximum
        kwargs["before_send"]()
        payload = json.loads(user)
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(payload)
        try:
            if payload["level"] == 0:
                barrier.wait(timeout=3)
            time.sleep(0.01)
            return _semantic_answer(payload)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    synthesis.CONCURRENCY = 4
    result = synthesis.build(snapshot, run, chunks)
    assert result["complete"] and maximum == 4
    assert result["usage"]["requests"] == len(calls)
    assert result["usage"]["input_tokens"] == 7 * len(calls)
    assert result["usage"]["output_tokens"] == 3 * len(calls)


def test_reverse_completion_preserves_serial_business_order_and_cache_fingerprints(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    parallel = False
    batch_count = (sum(1 + len(chunk["result"]["findings"]) + len(chunk["result"]["unknowns"]) for chunk in chunks) + 3) // 4
    barriers = [threading.Barrier(min(4, batch_count - start)) for start in range(0, batch_count, 4)]
    completed, events, lock = [], [threading.Event() for _ in range(batch_count)], threading.Lock()
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        if parallel and payload["level"] == 0:
            index = payload["items"][0]["position"] // 4
            barriers[index // 4].wait(timeout=3)
            if index % 4 != min(4, batch_count - index // 4 * 4) - 1:
                assert events[index + 1].wait(timeout=3)
            with lock:
                completed.append(index)
            events[index].set()
        return _semantic_answer(payload)
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    synthesis.CONCURRENCY = 1
    serial = synthesis.build(snapshot, run, chunks)
    with service.store._db() as db:
        serial_keys = {row[0] for row in db.execute("SELECT cache_key FROM full_analysis_synthesis")}
        db.execute("DELETE FROM full_analysis_synthesis")
    parallel = True
    synthesis.CONCURRENCY = 4
    result = synthesis.build(snapshot, run, chunks)
    assert completed[:4] == [3, 2, 1, 0]
    assert result["overview_body"] == serial["overview_body"]
    assert result["public_documents"] == serial["public_documents"] and result["semantic_coverage"] == serial["semantic_coverage"]
    with service.store._db() as db:
        assert {row[0] for row in db.execute("SELECT cache_key FROM full_analysis_synthesis")} == serial_keys


@pytest.mark.parametrize("pause", [False, True])
def test_parallel_failure_or_pause_stops_new_dispatch_drains_and_reuses_cache(synthesis_project, monkeypatch, pause):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    barrier, released = threading.Barrier(3), threading.Event()
    sent, sent_lock = [], threading.Lock()
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        index = payload["items"][0]["position"] // 4
        with sent_lock:
            sent.append(index)
        barrier.wait(timeout=3)
        if index == 1:
            if pause:
                service.store.config_set(f"full_analysis_control:{run['run_id']}", {"paused": True, "cancelled": False})
                released.set()
                return _semantic_answer(payload)
            threading.Timer(0.05, released.set).start()
            raise ToolError("fixture quota failure", 429)
        assert released.wait(timeout=3)
        return _semantic_answer(payload)
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    synthesis.CONCURRENCY = 3
    if pause:
        result = synthesis.build(snapshot, run, chunks)
        assert result["complete"] is False and result["paused"]
        assert result["usage"]["requests"] == 3
    else:
        with pytest.raises(ToolError):
            synthesis.build(snapshot, run, chunks)
    assert set(sent) == {0, 1, 2} and len(sent) == 3
    with service.store._db() as db:
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == (3 if pause else 2)
    service.store.config_set(f"full_analysis_control:{run['run_id']}", {"paused": False, "cancelled": False})
    resumed = []
    def recover(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        if payload["level"] == 0:
            resumed.append(payload["items"][0]["position"] // 4)
        return _semantic_answer(payload)
    monkeypatch.setattr(service.providers, "complete", recover)
    assert synthesis.build(snapshot, run, chunks)["complete"]
    assert 0 not in resumed and 2 not in resumed
    assert (1 not in resumed) is pause


def test_identical_inflight_inputs_share_one_paid_dispatch(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, _ = synthesis_project
    calls, lock = [], threading.Lock()
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        with lock:
            calls.append(user)
        time.sleep(0.1)
        payload = json.loads(user)
        return {"text": json.dumps({"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "rules",
            "title": "订单"} for item in payload["items"]]}), "usage": {"input_tokens": 7, "output_tokens": 3}}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    batch = [{"kind": "finding", "text": "相同业务输入。", "_fact_id": "stable-fact", "_module_ids": ["module-0"]}]
    usage = {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    results = synthesis._level_results(snapshot, run, [batch] * 8, 1, usage)
    assert len(calls) == 1 and len(results) == 8
    assert all(result == results[0] for result in results)
    assert usage == {"requests": 1, "cache_hits": 7, "input_tokens": 7, "output_tokens": 3}
