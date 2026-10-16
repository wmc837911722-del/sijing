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
        ids = [item["module_id"] for item in payload["items"]]
        if omit:
            ids = ids[:1]
        # A second real database connection can acquire a write transaction:
        # provider calls must not be inside the checkpoint transaction.
        with sqlite3.connect(service.store.db_path, timeout=0.1) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        return {"text": json.dumps({"domains": [{"name": "订单与支付", "summary": "订单受状态与金额条件约束后进入支付处理。", "module_ids": ids}],
                                   "flows": [{"text": "订单状态校验后处理支付结果。", "module_ids": ids}], "unknowns": ["前后端接口对应关系待核验"]}, ensure_ascii=False),
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
    assert {mid for domain in result["domains"] for mid in domain["module_ids"]} == {module["id"] for module in run["modules"]}
    assert set(result["module_titles"]) == {module["id"] for module in run["modules"]}
    assert "订单与支付" in result["overview_body"]
    assert "src/internal/folder" not in result["overview_body"]
    assert "前后端接口对应关系待核验" in result["overview_body"]
    assert all(f"[[module:{module['id']}|" in result["overview_body"] for module in run["modules"])
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


def test_unknown_ids_are_rejected_but_missing_modules_get_explicit_semantic_fallback(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    def forged(system, user, **kwargs):
        return {"text": json.dumps({"domains": [{"name": "伪造", "summary": "无来源", "module_ids": ["foreign"]}], "flows": [], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", forged)
    with pytest.raises(ToolError):
        FullBusinessSynthesis(service).build(snapshot, run, chunks)
    fake_model(monkeypatch, service, omit=True)
    result = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert result["complete"]
    assert set(result["module_titles"]) == {module["id"] for module in run["modules"]}
    assert any(domain["name"] == "待归类/技术支撑" for domain in result["domains"])


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
    assert len(result["module_titles"]) == len(run["modules"])
    assert "空文件/技术占位" in result["overview_body"]


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
        grouped = {}
        for item in payload["items"]:
            name = item["title"] if payload["level"] == 0 else "赛事经营"
            grouped.setdefault(name, []).append(item["module_id"])
        return {"text": json.dumps({"domains": [{"name": name, "summary": f"{name}的业务职责", "module_ids": ids} for name, ids in grouped.items()], "flows": [], "unknowns": []}, ensure_ascii=False), "usage": {}}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    synthesis.MAX_ITEMS = 4
    result = synthesis.build(snapshot, run, chunks)
    assert max(levels) > 0  # A broad parent layer must not erase its children.
    assert {domain["name"] for domain in result["domains"]} == set(topics)
    assert len(result["domains"]) == 15
    for index, name in enumerate(topics):
        assert f"### {name}\n" in result["overview_body"]
        assert result["module_titles"][f"module-{index}"] == name
        assert f"[[module:module-{index}|{name}]]" in result["overview_body"]
    assert "src/technical" not in result["overview_body"]


def test_full_permitted_schema_has_sufficient_bounded_output_budget(synthesis_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    service, snapshot, run, chunks = synthesis_project
    budgets = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        budgets.append(kwargs["max_output"])
        assert kwargs["max_output"] == 6000
        payload = json.loads(user)
        ids = [item["module_id"] for item in payload["items"]]
        result = {"domains": [{"name": f"领域{index}" + "名" * 37, "summary": "业" * 140, "module_ids": ids} for index in range(8)],
                  "flows": [{"text": str(index) + "流" * 139, "module_ids": ids} for index in range(6)],
                  "unknowns": [str(index) + "疑" * 139 for index in range(6)]}
        return {"text": json.dumps(result, ensure_ascii=False), "usage": {"output_tokens": 3100}, "output_limited": False}
    monkeypatch.setattr(service.providers, "complete", complete)
    result = FullBusinessSynthesis(service).build(snapshot, run, chunks)
    assert result["complete"] and len(result["domains"]) == 8
    assert result["usage"]["output_tokens"] == len(budgets) * 3100
    assert all(str(index) + "疑" * 139 in result["overview_body"] for index in range(6))


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
    with pytest.raises(ToolError, match="6000"):
        synthesis.build(snapshot, run, chunks)
    assert len(calls) == 1
    with service.store._db() as db:
        assert db.execute("SELECT count(*) FROM full_analysis_synthesis").fetchone()[0] == 0


def _semantic_answer(payload):
    first = payload["items"][0]
    name = "业务" + first["chunk_id"].split("-")[-1].zfill(2) if payload["level"] == 0 else "项目总览"
    return {"text": json.dumps({"domains": [{"name": name, "summary": "保留业务条件。", "module_ids": [item["module_id"] for item in payload["items"]]}],
                               "flows": [], "unknowns": []}, ensure_ascii=False),
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
    barriers = [threading.Barrier(4), threading.Barrier(4)]
    completed, events, lock = [], [threading.Event() for _ in range(8)], threading.Lock()
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        if parallel and payload["level"] == 0:
            index = int(payload["items"][0]["chunk_id"].split("-")[-1]) // 4
            barriers[index // 4].wait(timeout=3)
            if index % 4 != 3:
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
    assert result["domains"] == serial["domains"] and result["module_titles"] == serial["module_titles"]
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
        index = int(payload["items"][0]["chunk_id"].split("-")[-1]) // 4
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
            resumed.append(int(payload["items"][0]["chunk_id"].split("-")[-1]) // 4)
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
        return {"text": json.dumps({"domains": [{"name": "订单", "summary": "相同输入归纳。", "module_ids": [item["module_id"] for item in payload["items"]]}], "flows": [], "unknowns": []}), "usage": {"input_tokens": 7, "output_tokens": 3}}
    monkeypatch.setattr(service.providers, "complete", complete)
    synthesis = FullBusinessSynthesis(service)
    batch = [{"title": "订单", "summary": "相同业务输入。", "findings_texts": [], "unknowns": [], "_module_ids": ["module-0"]}]
    usage = {"requests": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    results = synthesis._level_results(snapshot, run, [batch] * 8, 1, usage)
    assert len(calls) == 1 and len(results) == 8
    assert all(result == results[0] for result in results)
    assert usage == {"requests": 1, "cache_hits": 7, "input_tokens": 7, "output_tokens": 3}
