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
        return {"complete": True, "overview_body": "# 完整赛事业务地图\n\n订单与退款业务的实现规则。\n" + "\n".join(
            "- [[module:" + module["id"] + "|业务知识]]" for module in run["modules"]),
            "module_titles": {module["id"]: "业务知识 · " + str(index) for index, module in enumerate(run["modules"], 1)},
            "domains": [], "usage": {}}
    monkeypatch.setattr(FullBusinessSynthesis, "build", synthesis)
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
        return {"text": json.dumps({"title": "订单知识", "summary": "代码声明信息", "findings": [
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


def test_module_documents_and_map_preserve_human_edits(full_project, monkeypatch):
    full, service, project = full_project
    model(monkeypatch, service)
    result = full.analyze(project["id"])
    module_notes = list((service.store.vault / "01-Wiki" / "业务知识" / "项目" / project["id"] / "模块").glob("*.md"))
    assert len(module_notes) == result["coverage"]["total_modules"]
    note = service.store.read_note(str(module_notes[0].relative_to(service.store.vault)))
    human = service.store.save_note(note["path"], note["content"] + "\n人工确认的补充。\n", note["revision"])
    result = full.analyze(project["id"])
    assert service.store.read_note(note["path"])["content"] == human["content"]
    assert any(item["path"] == note["path"] for item in result["conflicts"])
    from businessmap.content_layout import project_business_paths
    overview = service.store.read_note(project_business_paths(project["id"])["overview"])["content"]
    assert "完整源码" in overview and "5 / 5" in overview
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


def test_publication_pages_preserve_all_batches_and_homepage_navigation(full_project, monkeypatch):
    full, service, project = full_project
    full.PAGE_BYTES = 1400
    sent = model(monkeypatch, service)
    original = service.providers.complete
    def verbose(system, user, **kwargs):
        response = original(system, user, **kwargs)
        data = json.loads(response["text"])
        data["findings"][0]["text"] = "唯一批次:" + json.loads(user)["chunk_id"] + "业务规则" * 80
        response["text"] = json.dumps(data)
        return response
    monkeypatch.setattr(service.providers, "complete", verbose)
    result = full.analyze(project["id"])
    assert result["status"] == "completed"
    state = service.store.config_get("project_analysis:" + project["id"])
    parts = [value for key, value in state["notes"].items() if ":part:" in key]
    assert parts
    text = "\n".join(service.store.read_note(value["path"])["content"] for key, value in state["notes"].items() if key.startswith("module:"))
    for payload in sent:
        assert text.count("唯一批次:" + payload["chunk_id"]) == 1
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
    service.store.MAX_NOTE_BYTES = 1200
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
    run["synthesis"]["module_titles"] = {key: title + "新版本" for key, title in run["synthesis"]["module_titles"].items()}
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
            "唯一业务事实" + str(domain) + "-" + str(fact) + "：" + "规则条件" * 50 + f" [[module:{module_id}|关联业务]]"
            for fact in range(15)))
    run["synthesis"]["overview_body"] = "# 完整业务地图\n\n## 业务域与职责\n\n" + "\n\n".join(domains)
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
        assert all_summary_parts.count("排除材料" + str(index) + ".bin") == 1


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
