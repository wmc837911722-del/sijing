from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from businessmap.business_projects import BusinessProjects
from businessmap.core import Store
from businessmap.project_analysis import ProjectAnalysis
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService


@pytest.fixture
def full_project(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    for rid, files in {
        "front": {"src/views/orders/index.ts": "export const order = '前端订单';\n" * 420,
                  "src/views/refunds/index.ts": "export const refund = true;\n"},
        "back": {"src/main/java/com/example/arena/orders/Order.java": "class Order {}\n" * 450,
                 "src/main/java/com/example/arena/refunds/Refund.java": "class Refund {}\n",
                 "src/empty.py": "", ".env": "TOKEN='never-send'\n"},
    }.items():
        path = tmp_path / rid
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        for relative, content in files.items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        store._write_repo({"id": rid, "name": rid, "vault_path": str(store.vault), "status": "ready", "commit": commit,
                           "path": str(path), "url": "fixture", "auto_sync": False})
    service = ToolsService(store)
    provider = service.providers.save({"name": "Fixture", "kind": "openai-compatible", "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    project = BusinessProjects(store).create("完整赛事", "", ["front", "back"], {"front": "frontend", "back": "backend"})
    ProjectAnalysis(service).bind_model(project["id"], provider["id"])
    from businessmap.full_analysis import FullProjectAnalysis
    full = FullProjectAnalysis(service)
    full.SOURCE_BYTES = 5000
    # Exercise source planning/publication; synthesis has its own complete reduction tests.
    from businessmap.full_synthesis import FullBusinessSynthesis
    def synthesis(_self, snapshot, run, chunks):
        from business_projection_fixture import public_result
        from businessmap.analysis_addenda import AnalysisAddenda
        addenda = AnalysisAddenda(service, guard=lambda: None).accepted(snapshot, run)
        return public_result(snapshot, run, chunks, addenda)
    monkeypatch.setattr(FullBusinessSynthesis, "build", synthesis)
    from business_projection_fixture import isolate_stubbed_semantic_receipts
    isolate_stubbed_semantic_receipts(monkeypatch)
    yield full, service, project
    store.close()


def model(monkeypatch, service, callback=None):
    sent = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        sent.append(payload)
        if callback:
            callback(payload, len(sent))
        first = payload["files"][0]
        return {"text": json.dumps({"title": "订单知识", "summary": "订单业务说明", "findings": [
            {"text": "此片段声明订单或业务规则。", "evidence_ids": [first["evidence_id"]]}], "unknowns": ["运行行为待核验"]}),
            "usage": {"input_tokens": 100, "output_tokens": 30}, "provider": {"id": "fixture"}}
    monkeypatch.setattr(service.providers, "complete", complete)
    return sent


def test_plan_covers_every_line_including_file_tails_without_paid_calls(full_project, monkeypatch):
    full, service, project = full_project
    calls = model(monkeypatch, service)
    plan = full.plan(project["id"])
    assert not calls
    assert plan["coverage"]["eligible_files"] == 5
    assert plan["coverage"]["excluded_files"] == 1
    assert plan["coverage"]["total_chunks"] > 4
    assert len(plan["modules"]) >= 4
    result = full.analyze(project["id"])
    assert result["status"] == "completed"
    assert result["coverage"]["analyzed_files"] == 5
    lines = {}
    for payload in calls:
        identities = set()
        for item in payload["files"]:
            key = (item["repository_id"], item["path"])
            lines.setdefault(key, []).extend(range(item["line_start"], item["line_end"] + 1))
            assert item["evidence_id"] not in identities
            identities.add(item["evidence_id"])
        assert len(json.dumps(payload, ensure_ascii=False).encode()) + len(full.SYSTEM.encode()) <= 44000
    assert sorted(lines[("front", "src/views/orders/index.ts")]) == list(range(1, 421))
    assert sorted(lines[("back", "src/main/java/com/example/arena/orders/Order.java")]) == list(range(1, 451))
    assert "never-send" not in json.dumps(calls)


def test_completed_chunks_are_resumed_without_repaying_and_persist_across_instances(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def fail_second(payload, count):
        if count == 2:
            raise ToolError("fixture failure", 502)
    first = model(monkeypatch, service, fail_second)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and failed["coverage"]["completed_chunks"] == 1
    successful_id = first[0]["chunk_id"]
    from businessmap.full_analysis import FullProjectAnalysis
    resumed = FullProjectAnalysis(service)
    resumed.SOURCE_BYTES = 5000
    second = model(monkeypatch, service)
    result = resumed.analyze(project["id"])
    assert result["status"] == "completed"
    assert successful_id not in {payload["chunk_id"] for payload in second}
    assert len(first) + len(second) == result["coverage"]["total_chunks"] + 1


def test_unknown_evidence_and_snapshot_change_cannot_publish(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def forged(system, user, **kwargs):
        kwargs["before_send"]()
        return {"text": json.dumps({"title": "伪造", "summary": "", "findings": [{"text": "凭空结论", "evidence_ids": ["other-repo"]}], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", forged)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and result["coverage"]["completed_chunks"] == 0
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))
    def changed(payload, count):
        if count == 1:
            service.store.update_repository("front", {"commit": "c" * 40})
    model(monkeypatch, service, changed)
    with pytest.raises(ToolError) as error:
        full.analyze(project["id"])
    assert error.value.status_code == 409
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))


def test_pause_prevents_new_paid_dispatch_and_resume_keeps_completed_receipts(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def pause(payload, count):
        full.set_paused(project["id"], True)
    first = model(monkeypatch, service, pause)
    result = full.analyze(project["id"])
    assert result["status"] == "paused" and len(first) == 1
    assert result["coverage"]["completed_chunks"] == 1
    full.set_paused(project["id"], False)
    second = model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    assert first[0]["chunk_id"] not in {payload["chunk_id"] for payload in second}


def test_semantic_topics_and_map_preserve_human_edits(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    result = full.analyze(project["id"])
    module_notes = list((service.store.vault / "01-Wiki" / "完整赛事" / "规则").glob("*.md"))
    assert len(module_notes) == 1 and result["coverage"]["total_modules"] > 1
    note = service.store.read_note(str(module_notes[0].relative_to(service.store.vault)))
    human = service.store.save_note(note["path"], note["content"] + "\n人工确认的补充。\n", note["revision"])
    result = full.analyze(project["id"])
    assert service.store.read_note(note["path"])["content"] == human["content"]
    assert any(item["path"] == note["path"] for item in result["conflicts"])
    from businessmap.content_layout import public_project_paths
    overview = service.store.read_note(public_project_paths(project["name"])["overview"])["content"]
    assert "完整源码" not in overview and "source://" not in overview
    assert "[[" + note["path"][:-3] not in overview
    assert "前端调用后端" not in overview


def test_oversized_whole_line_is_explicit_failure_never_complete(full_project, monkeypatch):
    full, service, project = full_project
    repo = service.store.get_repository("front")
    target = Path(repo["path"]) / "src/long.ts"
    target.write_text("x" * 45000)
    subprocess.run(["git", "-C", repo["path"], "add", "."], check=True)
    subprocess.run(["git", "-C", repo["path"], "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "long"], check=True)
    service.store.update_repository("front", {"commit": subprocess.check_output(["git", "-C", repo["path"], "rev-parse", "HEAD"], text=True).strip()})
    calls = model(monkeypatch, service)
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    assert any(item["reason"] == "oversized_line" for item in result["exclusions"]), result
    assert not calls


def test_other_process_vault_change_rejects_old_results(full_project, monkeypatch, tmp_path):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def change_vault(payload, count):
        # Simulates the other desktop process changing the shared DB setting.
        service.store.config_set("vault_path", str(tmp_path / "other-vault"))
    model(monkeypatch, service, change_vault)
    with pytest.raises(ToolError) as error:
        full.analyze(project["id"])
    assert error.value.status_code == 409
    assert not list((tmp_path / "vault" / "01-Wiki").rglob("*.md"))


def test_kernel_lock_blocks_second_analysis_before_any_paid_send(full_project, monkeypatch):
    full, service, project = full_project
    calls = model(monkeypatch, service)
    from businessmap.full_analysis import FullProjectAnalysis
    other = FullProjectAnalysis(service)
    with full._exclusive(project["id"]):
        with pytest.raises(ToolError) as error:
            other.analyze(project["id"])
    assert error.value.status_code == 409 and not calls


def test_restart_with_new_runtime_epoch_reuses_successful_source_batches(full_project, monkeypatch):
    full, service, project = full_project
    first = model(monkeypatch, service)
    initial = full.analyze(project["id"])
    assert initial["status"] == "completed" and first
    # A process-local vault epoch is deliberately different after a restart.
    service.store._vault_epoch += 9
    second = model(monkeypatch, service)
    resumed = full.analyze(project["id"])
    assert resumed["status"] == "completed"
    assert resumed["run_id"] == initial["run_id"] and not second


def test_status_marks_old_commits_stale_instead_of_current_complete(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    service.store.update_repository("front", {"commit": "d" * 40})
    status = full.status(project["id"])
    assert status["status"] == "failed" and status["stale"] is True


def test_synthesis_failure_resumes_after_full_source_without_repaying(full_project, monkeypatch):
    full, service, project = full_project
    from businessmap.full_synthesis import FullBusinessSynthesis
    original = FullBusinessSynthesis.build
    monkeypatch.setattr(FullBusinessSynthesis, "build", lambda *args: (_ for _ in ()).throw(ToolError("model failure", 502)))
    sent = model(monkeypatch, service)
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    assert result["coverage"]["completed_chunks"] == result["coverage"]["total_chunks"] and sent
    monkeypatch.setattr(FullBusinessSynthesis, "build", original)
    second = model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    assert not second


def test_version_four_complete_projection_is_rebuilt_without_repaying_source(full_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    from business_projection_fixture import public_result
    full, service, project = full_project
    first = model(monkeypatch, service)
    original = service.providers.complete
    text = "订单仅在已支付且未退款时允许退款；退款金额不得超过已支付金额；该规则尚无测试覆盖。"
    def source_with_mixed_business_fact(system, user, **kwargs):
        response = original(system, user, **kwargs)
        data = json.loads(response["text"])
        data["summary"] = text
        response["text"] = json.dumps(data, ensure_ascii=False)
        return response
    monkeypatch.setattr(service.providers, "complete", source_with_mixed_business_fact)
    initial = full.analyze(project["id"])
    assert initial["status"] == "completed" and first
    run = full._load(project["id"])
    receipts = full._chunks(run)
    assert all(chunk["status"] == "completed" and chunk["input_hash"] for chunk in receipts)
    # A prior permissive v4 projection hid the entire statement as a test note.
    old_projection = run["synthesis"]
    old_projection["public_documents"] = []
    old_projection["source_mapping"] = {}
    old_projection["fact_coverage"] = {"input_ids": old_projection["semantic_coverage"]["input_ids"], "public": {}, "internal": {
        fid: {"classification": "test", "reason": "测试说明", "classification_basis": {"supported_categories": ["test"], "evidence_ids": []}}
        for fid in old_projection["semantic_coverage"]["input_ids"]}}
    old_projection.pop("semantic_coverage")
    old_projection.pop("verification")
    run["synthesis_version"] = 4
    full._save(run)
    synthesis_calls = []
    def rebuild(_self, snapshot, current_run, chunks):
        synthesis_calls.append(current_run["run_id"])
        return public_result(snapshot, current_run, chunks)
    monkeypatch.setattr(FullBusinessSynthesis, "build", rebuild)
    source_calls = model(monkeypatch, service)
    resumed = full.analyze(project["id"])
    assert resumed["status"] == "completed" and resumed["run_id"] == initial["run_id"]
    assert synthesis_calls == [initial["run_id"]]
    assert not source_calls and full._chunks(full._load(project["id"])) == receipts
    current = full._load(project["id"])
    assert current["synthesis_version"] == FullBusinessSynthesis.VERSION
    assert all(row["parts"][0]["outcome"] in {"claim", "retained"} for row in current["synthesis"]["semantic_coverage"]["decisions"].values())
    assert text in "\n".join(document["body"] for document in current["synthesis"]["public_documents"])
    assert full.VERSION == 1


def test_publication_pages_preserve_all_batches_and_homepage_navigation(full_project, monkeypatch):
    full, service, project = full_project
    full.PAGE_BYTES = 1400
    sent = model(monkeypatch, service)
    original = service.providers.complete
    import itertools
    sequence = itertools.count(1)
    def verbose(system, user, **kwargs):
        response = original(system, user, **kwargs)
        data = json.loads(response["text"])
        data["findings"][0]["text"] = "唯一业务说明第" + str(next(sequence)) + "项：" + "业务规则" * 80
        response["text"] = json.dumps(data)
        return response
    monkeypatch.setattr(service.providers, "complete", verbose)
    result = full.analyze(project["id"])
    assert result["status"] == "completed"
    state = service.store.config_get("project_analysis:" + project["id"])
    parts = [value for key, value in state["notes"].items() if ":part:" in key]
    assert parts
    text = "\n".join(service.store.read_note(value["path"])["content"] for key, value in state["notes"].items() if key.startswith("topic:"))
    for number in range(1, len(sent) + 1):
        assert text.count("唯一业务说明第" + str(number) + "项：") == 1
    for part in parts:
        assert "[[" + part["path"][:-3] in text
        assert len(service.store.read_note(part["path"])["content"].encode()) <= service.store.MAX_NOTE_BYTES


def test_publication_io_failure_restores_only_our_writes_and_can_resume(full_project, monkeypatch):
    full, service, project = full_project
    sent = model(monkeypatch, service)
    write = service.store._write_note
    writes = []
    def fail_second(path, content, revision=None, **kwargs):
        if len(writes) == 1:
            raise OSError("fixture disk failure")
        result = write(path, content, revision, **kwargs)
        writes.append(path)
        return result
    monkeypatch.setattr(service.store, "_write_note", fail_second)
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))
    assert not service.store.config_get("project_analysis:" + project["id"])
    monkeypatch.setattr(service.store, "_write_note", write)
    second = model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    assert sent and not second


def test_publication_failure_does_not_rollback_concurrent_human_change(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    write = service.store._write_note
    written = []
    def fail_after_human_edit(path, content, revision=None, **kwargs):
        if written:
            target = service.store.vault / written[0]
            target.write_text("# 人工并发修改\n不要覆盖。\n")
            raise OSError("fixture disk failure")
        result = write(path, content, revision, **kwargs)
        written.append(path)
        return result
    monkeypatch.setattr(service.store, "_write_note", fail_after_human_edit)
    assert full.analyze(project["id"])["status"] == "failed"
    assert (service.store.vault / written[0]).read_text() == "# 人工并发修改\n不要覆盖。\n"


def test_publication_size_preflight_performs_zero_writes(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    service.store.MAX_NOTE_BYTES = 300
    original = service.store._write_note
    writes = []
    def tracked(path, *args, **kwargs):
        writes.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(service.store, "_write_note", tracked)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not writes
    assert not list((service.store.vault / "01-Wiki").rglob("*.md"))


def test_crash_manifest_recovers_uncommitted_notes_before_resume(full_project, monkeypatch):
    full, service, project = full_project
    sent = model(monkeypatch, service)
    write = service.store._write_note
    writes = []
    def crash_second(path, *args, **kwargs):
        if writes:
            raise SystemExit("fixture process crash")
        result = write(path, *args, **kwargs)
        writes.append(path)
        return result
    monkeypatch.setattr(service.store, "_write_note", crash_second)
    with pytest.raises(SystemExit):
        full.analyze(project["id"])
    assert (service.store.vault / writes[0]).exists()
    assert not service.store.config_get("project_analysis:" + project["id"])
    monkeypatch.setattr(service.store, "_write_note", write)
    second = model(monkeypatch, service)
    result = full.analyze(project["id"])
    assert result["status"] == "completed" and not result["conflicts"]
    assert sent and not second
    manifests = list((service.store.data_dir / "full-analysis-publications").rglob("manifest.json"))
    assert {json.loads(path.read_text())["status"] for path in manifests} == {"committed", "rolled_back"}


def test_failed_publication_restores_previous_managed_versions(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    state_before = service.store.config_get("project_analysis:" + project["id"])
    contents = {value["path"]: service.store.read_note(value["path"])["content"] for value in state_before["notes"].values()}
    run = full._load(project["id"])
    run["synthesis"]["public_documents"][0]["body"] += "\n新的业务归纳。\n"
    from business_projection_fixture import seal
    seal(run["synthesis"])
    full._save(run)
    chunks = full._chunks(run)
    chunks[0]["result"]["findings"][0]["text"] += "新的机器归纳。"
    full._chunk_save(run, chunks[0])
    write = service.store._write_note
    count = 0
    def fail_once(path, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("fixture write failure")
        return write(path, *args, **kwargs)
    monkeypatch.setattr(service.store, "_write_note", fail_once)
    assert full.analyze(project["id"])["status"] == "failed"
    assert service.store.config_get("project_analysis:" + project["id"]) == state_before
    assert {path: service.store.read_note(path)["content"] for path in contents} == contents


def test_request_short_ids_restore_repository_and_line_segment_identity(full_project, monkeypatch):
    full, service, project = full_project
    full.plan(project["id"])
    run = full._load(project["id"])
    chunks = full._chunks(run)
    front = [segment for chunk in chunks for segment in chunk["segments"] if segment["path"] == "src/views/orders/index.ts"]
    back = next(segment for chunk in chunks for segment in chunk["segments"] if segment["repository_id"] == "back" and "Order.java" in segment["path"])
    selected = [front[0], front[-1], back]
    chunk = {"id": "short-id-fixture", "position": 9000, "module_id": chunks[0]["module_id"], "status": "pending", "segments": selected}
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        assert [item["evidence_id"] for item in payload["files"]] == ["s1", "s2", "s3"]
        assert payload["files"][1]["line_start"] > 1
        return {"text": json.dumps({"title": "订单", "summary": "", "findings": [
            {"text": "跨片段规则", "evidence_ids": ["s1", "s2", "s3"]}], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", complete)
    result = full._process(full.project_analysis.snapshot(project["id"]), run, chunk)
    assert result["result"]["findings"][0]["evidence_ids"] == [segment["evidence_id"] for segment in selected]
    assert len(set(result["result"]["findings"][0]["evidence_ids"])) == 3
    assert result["result"]["findings"][0]["text"] == "跨片段规则"


def test_legacy_stable_ids_work_only_inside_current_request_and_unknown_ids_fail(full_project, monkeypatch):
    from businessmap.analysis_output import AnalysisOutputError
    full, service, project = full_project
    full.plan(project["id"])
    run = full._load(project["id"])
    chunks = full._chunks(run)
    chunk = chunks[0]
    allowed = chunk["segments"][0]["evidence_id"]
    foreign = next(segment["evidence_id"] for other in chunks[1:] for segment in other["segments"] if segment["evidence_id"] != allowed)
    def answer(ids):
        return {"text": json.dumps({"title": "规则", "summary": "", "findings": [{"text": "事实", "evidence_ids": ids}], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", lambda *args, **kwargs: answer([allowed]))
    assert full._process(full.project_analysis.snapshot(project["id"]), run, chunk)["result"]["findings"][0]["evidence_ids"] == [allowed]
    monkeypatch.setattr(service.providers, "complete", lambda *args, **kwargs: answer(["s999", foreign]))
    with pytest.raises(AnalysisOutputError) as error:
        full._process(full.project_analysis.snapshot(project["id"]), run, chunk)
    assert error.value.details == {"reasons": {"unknown_evidence_ids": 2}}


def test_incomplete_findings_record_only_controlled_failure_counts(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def invalid(*args, **kwargs):
        kwargs["before_send"]()
        return {"text": json.dumps({"title": "规则", "summary": "", "findings": [{"text": "不应保存的模型原文"}, {}], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", invalid)
    assert full.analyze(project["id"])["status"] == "failed"
    chunks = full._chunks(full._load(project["id"]))
    failed = next(chunk for chunk in chunks if chunk["status"] == "failed")
    assert failed["details"] == {"reasons": {"finding_text_missing": 1, "missing_evidence_ids": 1}}
    assert "不应保存的模型原文" not in json.dumps(failed, ensure_ascii=False)


def test_new_snapshot_recovers_old_uncommitted_publication_before_writing(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    write = service.store._write_note
    writes = []
    def crash_second(path, *args, **kwargs):
        if writes:
            raise SystemExit("fixture process crash")
        result = write(path, *args, **kwargs)
        writes.append(path)
        return result
    monkeypatch.setattr(service.store, "_write_note", crash_second)
    with pytest.raises(SystemExit):
        full.analyze(project["id"])
    old_run = full._load(project["id"])["run_id"]
    repo = service.store.get_repository("front")
    path = Path(repo["path"]) / "src/views/orders/index.ts"
    path.write_text(path.read_text() + "export const changed = true;\n")
    subprocess.run(["git", "-C", repo["path"], "add", "."], check=True)
    subprocess.run(["git", "-C", repo["path"], "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "new snapshot"], check=True)
    service.store.update_repository("front", {"commit": subprocess.check_output(["git", "-C", repo["path"], "rev-parse", "HEAD"], text=True).strip()})
    monkeypatch.setattr(service.store, "_write_note", write)
    result = full.analyze(project["id"])
    assert result["status"] == "completed" and result["publication_complete"] and not result["conflicts"]
    assert result["run_id"] != old_run
    old = list((service.store.data_dir / "full-analysis-publications" / old_run).rglob("manifest.json"))
    assert json.loads(old[0].read_text())["status"] == "rolled_back"


def test_short_ids_for_identical_paths_in_different_repositories_stay_distinct(full_project, monkeypatch):
    full, service, project = full_project
    shared = "src/contracts/shared.ts"
    for rid in ("front", "back"):
        repository = service.store.get_repository(rid)
        path = Path(repository["path"]) / shared
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const side = '" + rid + "';\n")
        subprocess.run(["git", "-C", repository["path"], "add", "."], check=True)
        subprocess.run(["git", "-C", repository["path"], "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "shared path"], check=True)
        service.store.update_repository(rid, {"commit": subprocess.check_output(["git", "-C", repository["path"], "rev-parse", "HEAD"], text=True).strip()})
    full.plan(project["id"])
    run = full._load(project["id"])
    chunks = full._chunks(run)
    selected = [segment for chunk in chunks for segment in chunk["segments"] if segment["path"] == shared]
    assert len(selected) == 2
    chunk = {"id": "same-path-short-id-fixture", "position": 9000, "module_id": chunks[0]["module_id"], "status": "pending", "segments": selected}
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        assert {item["repository_id"] for item in payload["files"]} == {"front", "back"}
        assert [item["evidence_id"] for item in payload["files"]] == ["s1", "s2"]
        return {"text": json.dumps({"title": "契约", "summary": "", "findings": [{"text": "两个端分别定义常量", "evidence_ids": ["s1", "s2"]}], "unknowns": []})}
    monkeypatch.setattr(service.providers, "complete", complete)
    result = full._process(full.project_analysis.snapshot(project["id"]), run, chunk)
    identities = result["result"]["findings"][0]["evidence_ids"]
    assert identities == [segment["evidence_id"] for segment in selected] and len(set(identities)) == 2


def test_output_limit_failure_keeps_controlled_usage_without_raw_response(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def truncated(system, user, **kwargs):
        kwargs["before_send"]()
        assert kwargs["max_output"] >= 2800
        return {"text": '{"title":"PRIVATE-UNSAVED", "findings":[', "output_limited": True,
                "usage": {"input_tokens": 7000, "output_tokens": 2800}}
    monkeypatch.setattr(service.providers, "complete", truncated)
    assert full.analyze(project["id"])["status"] == "failed"
    failed = next(chunk for chunk in full._chunks(full._load(project["id"])) if chunk["status"] == "failed")
    assert failed["details"] == {"reasons": {"output_limited": 1}, "usage": {"input_tokens": 7000, "output_tokens": 2800}}
    assert "PRIVATE-UNSAVED" not in json.dumps(failed)


def test_expanded_safe_policy_reuses_verified_receipts_despite_changed_packing(full_project, monkeypatch):
    from businessmap.full_source import FullSourceInventory
    full, service, project = full_project
    full.CONCURRENCY = 1
    repository = service.store.get_repository("front")
    extra_path = "src/views/orders/PasswordPanel.vue"
    path = Path(repository["path"]) / extra_path
    path.write_text("<template>修改密码业务页面</template>\n")
    subprocess.run(["git", "-C", repository["path"], "add", "."], check=True)
    subprocess.run(["git", "-C", repository["path"], "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "business source"], check=True)
    service.store.update_repository("front", {"commit": subprocess.check_output(["git", "-C", repository["path"], "rev-parse", "HEAD"], text=True).strip()})
    original_exclusion = FullSourceInventory._excluded_reason
    monkeypatch.setattr(FullSourceInventory, "POLICY_VERSION", "full-source-v3-text-literals-linear-redaction")
    monkeypatch.setattr(FullSourceInventory, "_excluded_reason", classmethod(lambda cls, path, size:
        "unsafe_path" if path == extra_path and cls.POLICY_VERSION.endswith("linear-redaction") else original_exclusion(path, size)))
    def fail_third(payload, count):
        if count == 3:
            raise ToolError("fixture stop", 502)
    model(monkeypatch, service, fail_third)
    old = full.analyze(project["id"])
    assert old["coverage"]["completed_chunks"] == 2
    old_run = full._load(project["id"])
    # Real v3 paid runs predate the persisted authorization boundary.
    old_run.pop("receipt_boundary", None)
    full._save(old_run)
    completed = [chunk for chunk in full._chunks(old_run) if chunk["status"] == "completed"]
    previous_intervals = {(segment["repository_id"], segment["path"], segment["line_start"], segment["line_end"]) for chunk in completed for segment in chunk["segments"]}
    monkeypatch.setattr(FullSourceInventory, "POLICY_VERSION", "full-source-v4-password-business-sources")
    plan = full.plan(project["id"])
    assert plan["run_id"] != old["run_id"]
    assert plan["coverage"]["eligible_files"] == 6
    assert plan["coverage"]["completed_chunks"] == 2
    sent = model(monkeypatch, service)
    result = full.analyze(project["id"])
    assert result["status"] == "completed"
    resent = {(item["repository_id"], item["path"], item["line_start"], item["line_end"]) for payload in sent for item in payload["files"]}
    assert not previous_intervals & resent
    assert any(item["path"] == extra_path for payload in sent for item in payload["files"])


def test_policy_expansion_does_not_reuse_receipts_after_authorization_revision_changes(full_project, monkeypatch):
    from businessmap.full_source import FullSourceInventory
    full, service, project = full_project
    monkeypatch.setattr(FullSourceInventory, "POLICY_VERSION", "full-source-v3-text-literals-linear-redaction")
    model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    provider = full.project_analysis.model_policy(project["id"])["provider_id"]
    full.project_analysis.bind_model(project["id"], provider)
    monkeypatch.setattr(FullSourceInventory, "POLICY_VERSION", "full-source-v4-password-business-sources")
    plan = full.plan(project["id"])
    assert plan["coverage"]["completed_chunks"] == 0


def test_explicit_provider_continuation_preserves_verified_original_receipts(full_project, monkeypatch):
    from businessmap.providers import provider_destination
    full, service, project = full_project
    full.CONCURRENCY = 1
    def stop_third(payload, count):
        if count == 3:
            raise ToolError("stop", 502)
    calls = model(monkeypatch, service, stop_third)
    old_status = full.analyze(project["id"])
    old = full._load(project["id"])
    old_data = json.dumps(old, sort_keys=True)
    old_chunks = full._chunks(old)
    original_provider = provider_destination(full.project_analysis.snapshot(project["id"])["provider"])
    provider = service.providers.save({"name": "Second model", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    result = full.continue_with_provider(project["id"], provider["id"], old_status["run_id"])
    assert len(calls) == 3  # Moving checkpoints must not invoke any model.
    assert result["run_id"] != old_status["run_id"]
    assert result["status"] == "paused"
    assert result["coverage"]["completed_chunks"] == result["reused_chunks"] == 2
    assert result["receipt_migration"]["source_provider"] == original_provider
    assert result["receipt_migration"]["target_provider"]["id"] == provider["id"]
    with service.store._db() as db:
        saved = json.loads(db.execute("SELECT data FROM full_analysis_runs WHERE id=?", (old["run_id"],)).fetchone()[0])
    assert json.dumps(saved, sort_keys=True) == old_data
    assert full._chunks(old) == old_chunks
    run = full._load(project["id"])
    reused = [chunk for chunk in full._chunks(run) if chunk["status"] == "completed"]
    for chunk in reused:
        origin = next(value for value in old_chunks if value["id"] == chunk["reused_from"]["chunk_id"])
        assert chunk["result"] == origin["result"]
        assert chunk["input_hash"] == origin["input_hash"]
        assert chunk["completed_at"] == origin["completed_at"]
        assert chunk["origin_provider"] == original_provider
    full.set_paused(project["id"], False)
    continued = model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    assert not {value["chunk_id"] for value in continued} & {value["id"] for value in reused}
    from businessmap.content_layout import public_project_paths
    summary = service.store.read_note(public_project_paths(project["name"])["summary"])["content"]
    assert "混合模型" not in summary and "Second model" not in summary and "Fixture" not in summary
    private = full._load(project["id"])["receipt_migration"]
    assert private["source_provider"] == original_provider and private["target_provider"]["id"] == provider["id"]


def test_provider_continuation_rejects_tampered_input_before_changing_binding(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    full.analyze(project["id"])
    old = full._load(project["id"])
    chunk = full._chunks(old)[0]
    chunk["input_hash"] = "0" * 64
    full._chunk_save(old, chunk)
    provider = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    policy = full.project_analysis.model_policy(project["id"])
    with pytest.raises(ToolError) as error:
        full.continue_with_provider(project["id"], provider["id"], old["run_id"])
    assert error.value.status_code == 409
    assert full.project_analysis.model_policy(project["id"]) == policy


def test_provider_continuation_requires_current_source_snapshot(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    full.analyze(project["id"])
    old = full._load(project["id"])
    provider = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    # Revoking/rebinding even the same old target must not be silently bypassed.
    full.project_analysis.bind_model(project["id"], full.project_analysis.model_policy(project["id"])["provider_id"])
    with pytest.raises(ToolError) as error:
        full.continue_with_provider(project["id"], provider["id"], old["run_id"])
    assert error.value.status_code == 409


def test_provider_continuation_verifies_legacy_evidence_wire_without_rewriting_hash(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    original_payload = full._payload
    def historic_payload(snapshot, run, chunk):
        payload = json.loads(original_payload(snapshot, run, chunk))
        for item, segment in zip(payload["files"], chunk["segments"]):
            item["evidence_id"] = segment["evidence_id"]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    monkeypatch.setattr(full, "_payload", historic_payload)
    def pause(payload, count):
        full.set_paused(project["id"], True)
    model(monkeypatch, service, pause)
    full.analyze(project["id"])
    old = full._load(project["id"])
    chunk = next(value for value in full._chunks(old) if value["status"] == "completed")
    # Make an authentic earlier source receipt, as v3 -> v4 reuse did.
    earlier = {**old, "run_id": "historic-source-run"}
    full._save(earlier)
    full._chunk_save(earlier, chunk)
    chunk["reused_from"] = {"run_id": earlier["run_id"], "chunk_id": chunk["id"], "input_hash": chunk["input_hash"]}
    full._chunk_save(old, chunk)
    full._save(old)
    monkeypatch.setattr(full, "_payload", original_payload)
    provider = service.providers.save({"name": "Second", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "second"})
    result = full.continue_with_provider(project["id"], provider["id"], old["run_id"])
    assert result["reused_chunks"] == 1
    reused = next(value for value in full._chunks(full._load(project["id"])) if value["status"] == "completed")
    assert reused["input_hash"] == chunk["input_hash"]


def test_invalid_output_does_not_retry_or_block_other_independent_batches(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    sent = model(monkeypatch, service)
    original = service.providers.complete
    def invalid_first(system, user, **kwargs):
        response = original(system, user, **kwargs)
        if len(sent) == 1:
            response["text"] = '{"unfinished":'
        return response
    monkeypatch.setattr(service.providers, "complete", invalid_first)
    result = full.analyze(project["id"])
    assert result["status"] == "failed"
    assert result["coverage"]["completed_chunks"] == result["coverage"]["total_chunks"] - 1
    assert len(sent) == result["coverage"]["total_chunks"]
    assert len({item["chunk_id"] for item in sent}) == len(sent)
    assert not list((service.store.vault / "02-Areas").rglob("*.md"))


def test_wire_budget_input_failure_stops_new_paid_dispatch(full_project, monkeypatch):
    from businessmap.analysis_output import AnalysisOutputError
    full, service, project = full_project
    full.CONCURRENCY = 1
    sent = model(monkeypatch, service)
    payload = full._payload
    def fail_input(snapshot, run, chunk):
        raise AnalysisOutputError("input too large", "format", {"reasons": {"wire_budget_exceeded": 1}})
    monkeypatch.setattr(full, "_payload", fail_input)
    result = full.analyze(project["id"])
    assert result["status"] == "failed" and not sent
    chunks = full._chunks(full._load(project["id"]))
    assert sum(chunk["status"] == "failed" for chunk in chunks) == 1
    assert any(chunk["status"] == "pending" for chunk in chunks)


def test_large_map_and_summary_publish_lossless_pages_with_matching_roles(full_project, monkeypatch):
    import yaml
    full, service, project = full_project
    model(monkeypatch, service)
    assert full.analyze(project["id"])["status"] == "completed"
    run = full._load(project["id"])
    full.PAGE_BYTES = 8000
    service.store.MAX_NOTE_BYTES = 20000
    module_id = run["modules"][0]["id"]
    domains = []
    for domain in range(4):
        domains.append("### 领域" + str(domain) + "\n\n" + "\n\n".join(
            "唯一业务事实" + str(domain) + "-" + str(fact) + "：" + "规则条件" * 50 + " [[01-Wiki/完整赛事/规则/订单知识|订单知识]]"
            for fact in range(15)))
    run["synthesis"]["overview_body"] = "# 完整业务地图\n\n## 业务域与职责\n\n" + "\n\n".join(domains)
    run["synthesis"]["summary_body"] = "# 业务梳理总结\n\n## 口径待确认\n\n" + "\n\n".join("待确认口径第" + str(index) + "项：" + "业务范围尚待确认。" * 5 for index in range(450))
    from business_projection_fixture import seal
    seal(run["synthesis"])
    run["exclusions"] = [{"repository_id": "front", "path": "排除材料" + str(index) + ".bin", "reason": "outside_source_scope"} for index in range(450)]
    full._save(run)
    assert full.analyze(project["id"])["status"] == "completed"
    state = service.store.config_get("project_analysis:" + project["id"])
    for key, role in (("overview", "business_map"), ("summary", "analysis_summary")):
        homepage = service.store.read_note(state["notes"][key]["path"])["content"]
        parts = [value for part_key, value in state["notes"].items() if part_key.startswith(key + ":part:")]
        assert parts
        for part in parts:
            assert "[[" + part["path"][:-3] in homepage
            content = service.store.read_note(part["path"])["content"]
            assert yaml.safe_load(content.split("---", 2)[1])["content_role"] == role
            assert len(content.encode()) <= service.store.MAX_NOTE_BYTES
    all_map_parts = "\n".join(service.store.read_note(value["path"])["content"] for key, value in state["notes"].items() if key.startswith("overview:part:"))
    for domain in range(4):
        assert "领域" + str(domain) in service.store.read_note(state["notes"]["overview"]["path"])["content"]
        for fact in range(15):
            assert all_map_parts.count("唯一业务事实" + str(domain) + "-" + str(fact) + "：") == 1
    all_summary_parts = "\n".join(service.store.read_note(value["path"])["content"] for key, value in state["notes"].items() if key.startswith("summary:part:"))
    for index in range(450):
        assert all_summary_parts.count("待确认口径第" + str(index) + "项：") == 1
    assert ".bin" not in all_summary_parts and "source://" not in all_summary_parts


def test_orphan_running_state_offers_resume_without_starting_model(full_project, monkeypatch):
    full, service, project = full_project
    full.plan(project["id"])
    run = full._load(project["id"])
    run["status"] = "running"
    full._save(run)
    sent = model(monkeypatch, service)
    status = full.status(project["id"])
    assert status["status"] == "paused" and status["interrupted"] is True
    assert not sent
    with full._exclusive(project["id"]):
        active = full.status(project["id"])
        assert active["status"] == "running" and not active.get("interrupted")
    # A status probe does not persist a pause which would stop a new owner.
    assert not full._controls(run).get("paused")


def test_progress_keeps_status_precedence_and_empty_file_coverage(full_project):
    full, service, project = full_project
    run = {"run_id": "progress-literal-fixture", "files": [
        {"module_id": "done", "line_count": 3, "chunk_ids": ["a"]},
        {"module_id": "empty", "line_count": 0, "chunk_ids": []},
        {"module_id": "failed", "line_count": 5, "chunk_ids": ["b"]},
        {"module_id": "running", "line_count": 2, "chunk_ids": ["c"]},
        {"module_id": "pending", "line_count": 1, "chunk_ids": ["d"]},
        {"module_id": "blocked", "line_count": 10, "chunk_ids": ["e"], "blocking": True}],
        "modules": [{"id": key} for key in ("done", "empty", "failed", "running", "pending", "blocked")], "exclusions": [{}]}
    for position, (cid, mid, status, end) in enumerate((
        ("a", "done", "completed", 3), ("b", "failed", "failed", 5),
        ("c", "running", "running", 2), ("d", "pending", "pending", 1), ("e", "blocked", "completed", 2))):
        full._chunk_save(run, {"id": cid, "position": position, "module_id": mid, "status": status,
                               "segments": [{"line_start": 1, "line_end": end}]})
    progress = full._progress(run)
    assert progress["coverage"] == {"eligible_files": 6, "excluded_files": 1, "total_chunks": 5,
        "completed_chunks": 2, "analyzed_files": 2, "total_modules": 6, "completed_modules": 2,
        "total_lines": 21, "analyzed_lines": 5}
    assert {module["id"]: module["status"] for module in progress["modules"]} == {
        "done": "completed", "empty": "completed", "failed": "failed", "running": "running", "pending": "pending", "blocked": "pending"}


def test_progress_does_not_rescan_all_files_for_every_completed_module(full_project):
    full, service, project = full_project
    class CountedFiles(list):
        visits = 0
        def __iter__(self):
            for value in super().__iter__():
                self.visits += 1
                yield value
    files = CountedFiles([{"module_id": str(index // 10), "line_count": 1, "chunk_ids": [str(index // 10)]} for index in range(1000)])
    run = {"run_id": "linear-progress-fixture", "files": files, "modules": [{"id": str(index)} for index in range(100)], "exclusions": []}
    for index in range(100):
        full._chunk_save(run, {"id": str(index), "position": index, "module_id": str(index), "status": "completed",
            "segments": [{"line_start": 1, "line_end": 1} for _ in range(10)]})
    result = full._progress(run)
    assert result["coverage"]["analyzed_files"] == 1000 and result["coverage"]["completed_modules"] == 100
    assert result["coverage"]["analyzed_lines"] == 1000
    assert files.visits < 10000


def test_source_analysis_explicitly_requests_json_output_mode(full_project, monkeypatch):
    full, service, project = full_project
    original = model(monkeypatch, service)
    complete = service.providers.complete
    def guarded(system, user, **kwargs):
        assert kwargs.get("json_mode") is True
        return complete(system, user, **kwargs)
    monkeypatch.setattr(service.providers, "complete", guarded)
    assert full.analyze(project["id"])["status"] == "completed"
    assert original


@pytest.mark.parametrize("message,want", [
    ("模型服务返回 HTTP 429。请检查模型、地址、凭据或额度。", {"exception_type": "ToolError", "status_code": 502, "http_status": 429}),
    ("PRIVATE-KEY-DO-NOT-PERSIST HTTP 429 authorization bearer", {"exception_type": "ToolError", "status_code": 502}),
    ("模型调用失败（ReadTimeout），未获得有效结果。", {"exception_type": "ToolError", "status_code": 502, "provider_error_type": "ReadTimeout"}),
])
def test_non_output_failures_keep_only_controlled_diagnostics(full_project, monkeypatch, message, want):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def failure(*args, **kwargs):
        kwargs["before_send"]()
        raise ToolError(message, 502)
    monkeypatch.setattr(service.providers, "complete", failure)
    assert full.analyze(project["id"])["status"] == "failed"
    failed = next(chunk for chunk in full._chunks(full._load(project["id"])) if chunk["status"] == "failed")
    assert failed["details"] == want
    assert "PRIVATE-KEY-DO-NOT-PERSIST" not in json.dumps(failed)


@pytest.mark.parametrize("code,action", [(402, "余额不足"), (429, "限流"), (401, "凭据"), (403, "访问权限")])
def test_failed_and_paused_status_show_controlled_http_action(full_project, monkeypatch, code, action):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def rejected(*args, **kwargs):
        kwargs["before_send"]()
        raise ToolError(f"模型服务返回 HTTP {code}。请检查模型、地址、凭据或额度。", 502)
    monkeypatch.setattr(service.providers, "complete", rejected)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and failed["coverage"]["completed_chunks"] < failed["coverage"]["total_chunks"]
    assert action in failed["error"] and str(code) in failed["error"]
    full.set_paused(project["id"], True)
    monkeypatch.setattr(service.providers, "complete", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("status must not call model")))
    status = full.status(project["id"])
    assert status["status"] == "paused" and action in status["error"]
    assert full._controls(full._load(project["id"]))["paused"] is True


def test_failure_action_does_not_expose_untrusted_error_or_unknown_http_code(full_project, monkeypatch):
    full, service, project = full_project
    full.CONCURRENCY = 1
    def rejected(*args, **kwargs):
        kwargs["before_send"]()
        raise ToolError("PRIVATE-SECRET HTTP 402 must not be inferred", 502)
    monkeypatch.setattr(service.providers, "complete", rejected)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and "PRIVATE-SECRET" not in json.dumps(failed)
    run = full._load(project["id"])
    chunk = next(chunk for chunk in full._chunks(run) if chunk["status"] == "failed")
    chunk["details"] = {"http_status": 499}
    chunk["error"] = "PRIVATE-SECRET untrusted stored legacy error"
    full._chunk_save(run, chunk)
    status = full.status(project["id"])
    assert status["status"] == "failed"
    assert "PRIVATE-SECRET" not in json.dumps(status)
    assert "余额不足" not in status["error"] and "499" not in status["error"]

@pytest.mark.parametrize("legacy", (False, True))
def test_linked_purposes_and_summaries_are_grouped_once(full_project, monkeypatch, legacy):
    from business_projection_fixture import projection_from_units, seal
    from businessmap.business_semantics import _render_operation, _render_overviews, semantic_input_hash
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units

    full, service, project = full_project
    calls = model(monkeypatch, service)
    # Long, reviewed descriptions exercise both index families and page links.
    purposes = {"flows": "提交报名审核。", "rules": "核对报名资格。"}
    summaries = {"flows": "申请人可在门店提交报名。", "rules": "报名资格由门店核对。"}
    if legacy:
        full.PAGE_BYTES = 1200
        purposes = {key: value * 23 for key, value in purposes.items()}
        summaries = {key: value * 23 for key, value in summaries.items()}

    def synthesize(_self, snapshot, run, chunks):
        units = source_fact_units(snapshot, run, chunks)
        routed = [{"fact_id": unit["_fact_id"], "disposition": "public",
                   "category": "flows" if i % 2 else "rules", "title": "报名审核"}
                  for i, unit in enumerate(units)]
        identity = semantic_input_hash(snapshot, run, chunks, [])
        projection = projection_from_units(snapshot, {**run, "semantic_input_hash": identity}, units, routed)
        catalogue = projection["semantic_coverage"]["catalogue"]
        for op in catalogue:
            op.update(purpose=purposes[op["category"]], summary=summaries[op["category"]])
            doc = next(d for d in projection["public_documents"] if d["category"] == op["category"])
            claims = [c for c in projection["semantic_coverage"]["claims"] if c["operation_id"] == op["id"]]
            doc["body"] = _render_operation(op, claims)
        projection["overview_body"], projection["summary_body"] = _render_overviews(project["name"], catalogue, _legacy_boundary=legacy)
        return seal(projection)

    monkeypatch.setattr(FullBusinessSynthesis, "build", synthesize)
    assert full.analyze(project["id"])["publication_complete"]
    initial_calls = len(calls)
    digest = full._load(project["id"])["synthesis"]["verification"]["projection_hash"]
    def index_body(key):
        notes = service.store.config_get("project_analysis:" + project["id"])["notes"]
        def read(k):
            return service.store.read_note(notes[k]["path"])["content"].split("---", 2)[-1].strip()
        parts = [k for k, note in notes.items() if k.startswith(key + ":part:") and not note["stale"]]
        if parts:
            for part in parts:
                assert "[[" + notes[part]["path"][:-3] + "|" in read(key)
            return "\n\n".join(read(part).split("\n\n", 1)[1] for part in parts)
        return read(key)

    rules_path = "01-Wiki/完整赛事/规则/报名审核"
    flow_path = "01-Wiki/完整赛事/流程/报名审核"
    for key, descriptions in (("overview", purposes), ("summary", summaries)):
        text = index_body(key)
        assert text.count(f"[[{flow_path}|报名审核]]：" + descriptions["flows"]) == 1
        assert text.count(f"[[{rules_path}|报名审核]]：" + descriptions["rules"]) == 1
        assert "- 报名审核：" not in text
        assert text.index("\n## 流程\n") < text.index(flow_path) < text.index("\n## 规则\n") < text.index(rules_path)
    assert "业务用途" in index_body("overview")
    boundary = "此处为静态资料整合" if legacy else "本文根据现有业务资料整理"
    assert boundary in index_body("summary")
    if legacy:
        assert any(":part:" in k for k in service.store.config_get("project_analysis:" + project["id"])["notes"] if k.startswith("summary:"))
    manual = service.store.vault / (rules_path + ".md")
    manual.write_text(manual.read_text() + "\n人工补充\n")
    assert not full.analyze(project["id"])["publication_complete"]
    assert "人工补充" in manual.read_text()
    for key, descriptions in (("overview", purposes), ("summary", summaries)):
        text = index_body(key)
        assert f"[[{rules_path}|" not in text
        assert f"[[{flow_path}|报名审核]]：" + descriptions["flows"] in text
        assert "- 报名审核：" + descriptions["rules"] in text
    flow_note = service.store.vault / (flow_path + ".md")
    flow_note.write_text(flow_note.read_text() + "\n另一项人工补充\n")
    assert not full.analyze(project["id"])["publication_complete"]
    assert "本次没有成功保存的业务主题" in index_body("overview")
    assert "打开主题" not in index_body("overview")
    for key in ("overview", "summary"):
        assert "[[01-Wiki/" not in index_body(key)
    assert len(calls) == initial_calls and full._load(project["id"])["synthesis"]["verification"]["projection_hash"] == digest
