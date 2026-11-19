"""Bounded deadline policy, with disposable SQLite and executable fake CLI only."""
from copy import deepcopy
import json
import time

import pytest

from businessmap import business_semantics as semantics, codex_provider
from businessmap.providers import ToolError
from test_business_semantics import semantic_case, build_case, execute, checkpoint_rows
from test_codex_provider import service, cli, save


SCHEMA = {"type": "object", "properties": {"decision": {"type": "string"},
          "checks": {"type": "object"}, "issues": {"type": "array"}},
          "required": ["decision", "checks", "issues"], "additionalProperties": False}


@pytest.mark.parametrize("structured,timeout,succeeds", [
    (True, 1, True), (True, None, False), (True, .1, False),
    (False, 1, False), (False, None, False),
])
def test_delayed_complete_result_requires_explicit_structured_extension(service, cli, monkeypatch, structured, timeout, succeeds):
    monkeypatch.setattr(codex_provider, "TIMEOUT_SECONDS", .15)
    monkeypatch.setattr(codex_provider, "STRUCTURED_TIMEOUT_SECONDS", .75, raising=False)
    (cli / "mode").write_text("delayed-structured" if structured else "delayed-legacy")
    selected = save(service)
    options = {"output_schema": SCHEMA} if structured else {}
    if timeout is not None:
        options["_deadline"] = time.monotonic() + timeout
    if succeeds:
        result = service.complete("规则", "资料", provider_id=selected["id"], **options)
        assert json.loads(result["text"])["decision"] == "reject"
    else:
        with pytest.raises(ToolError) as failure:
            service.complete("规则", "资料", provider_id=selected["id"], **options)
        assert failure.value.status_code == 408 and failure.value.codex_reason == "timeout"
    call = json.loads((cli / "invocation.json").read_text())
    from pathlib import Path
    assert not Path(call["cwd"]).exists()


def test_structured_extension_cannot_exceed_fixed_ceiling(service, cli, monkeypatch):
    monkeypatch.setattr(codex_provider, "TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(codex_provider, "STRUCTURED_TIMEOUT_SECONDS", .2, raising=False)
    (cli / "mode").write_text("delayed-structured")
    selected = save(service)
    started = time.monotonic()
    with pytest.raises(ToolError) as failure:
        service.complete("规则", "资料", provider_id=selected["id"], output_schema=SCHEMA, _deadline=started + 100)
    assert failure.value.status_code == 408 and time.monotonic() - started < 1


@pytest.mark.parametrize("structured,timeout,ceiling", [(False, None, 180), (True, None, 180),
    (False, 10000, 180), (True, 10000, 360)])
def test_actual_adapter_ceiling_with_virtual_clock_keeps_default_and_legacy_180(cli, monkeypatch, structured, timeout, ceiling):
    from types import SimpleNamespace
    (cli / "mode").write_text("timeout")
    elapsed = [0.]
    ticks = iter([180., 180.001, 360., 360.001])
    def sleep(_):
        elapsed[0] = next(ticks)
        time.sleep(.02)
    monkeypatch.setattr(codex_provider, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    options = {"output_schema": SCHEMA} if structured else {}
    with pytest.raises(ToolError) as failure:
        codex_provider.complete([], "default", max_output=6000, before_send=lambda: None, timeout=timeout, **options)
    assert failure.value.status_code == 408 and failure.value.codex_reason == "timeout"
    # Completion must be observed strictly before the bound.
    assert elapsed[0] == ceiling


@pytest.mark.parametrize("interruption", ["pause", "destination"])
def test_extended_structured_call_still_runs_streaming_guards_and_cleanup(service, cli, monkeypatch, interruption):
    from pathlib import Path
    monkeypatch.setattr(codex_provider, "TIMEOUT_SECONDS", .15)
    (cli / "mode").write_text("timeout")
    selected = save(service)
    def guard():
        if not (cli / "invocation.json").exists():
            return
        if interruption == "pause":
            raise ToolError("用户暂停", 409)
        current = service.store.config_get("providers")
        current[0]["model"] = "changed-fixture"
        service.store.config_set("providers", current)
    started = time.monotonic()
    with pytest.raises(ToolError) as failure:
        service.complete("规则", "资料", provider_id=selected["id"], output_schema=SCHEMA,
            _deadline=started + 2, before_send=guard)
    assert failure.value.status_code == 409 and time.monotonic() - started < 1.8
    assert ("用户暂停" if interruption == "pause" else "模型目标已变化") in str(failure.value)
    assert not Path(json.loads((cli / "invocation.json").read_text())["cwd"]).exists()
    assert service.usage()["requests"] == 0


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"), 0, -1, True, "360"])
@pytest.mark.parametrize("structured", [False, True])
def test_invalid_timeout_is_controlled_before_any_process(service, cli, timeout, structured):
    options = {"output_schema": SCHEMA} if structured else {}
    with pytest.raises(ToolError) as failure:
        codex_provider.complete([], "default", max_output=6000, before_send=lambda: None, timeout=timeout, **options)
    assert failure.value.status_code == 408 and failure.value.codex_reason == "timeout"
    assert not (cli / "invocation.json").exists()


@pytest.mark.parametrize("local", [False, True])
def test_fixed_stage_duration_is_ephemeral_argument_and_resume_has_zero_sends(semantic_case, monkeypatch, local):
    if local:
        semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    original = setup[0].service.providers.complete
    observed = []
    clock = [1000.]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def complete(system, user, **kwargs):
        observed.append((json.loads(user)["stage"], deepcopy(kwargs)))
        return original(system, user, **kwargs)
    setup[0].service.providers.complete = complete
    projection = execute(setup)
    profile = semantics.budget_profile(semantic_case[1])
    if local:
        assert profile["id"] == "semantic-codex-evidence-v4"
        assert {stage: limits["timeout_seconds"] for stage, limits in profile["stages"].items()} == {
            "catalogue": 180, "subdivide": 180, "proposal": 360, "review": 360, "partition_review": 360}
    for stage, options in observed:
        if local:
            assert options["_deadline"] == 1000 + (180 if stage in {"catalogue", "subdivide"} else 360)
        else:
            assert "_deadline" not in options
            assert "timeout_seconds" not in profile["stages"][stage]
    before = checkpoint_rows(semantic_case[0])
    assert "_deadline" not in json.dumps(before)
    clock[0] = 5000.
    replay = execute(setup)
    assert replay["semantic_input_hash"] == projection["semantic_input_hash"]
    assert len(observed) == 3 and checkpoint_rows(semantic_case[0]) == before


def test_fixed_duration_changes_input_generator_and_stage_identity(semantic_case, monkeypatch):
    snapshot, run = semantic_case[1:3]
    snapshot["provider"].update(kind="codex-local", base_url="codex://local")
    original = semantics.budget_profile
    old_input = semantics.semantic_input_hash(snapshot, run, [], [])
    old_generator = semantics._semantic_generator_hash(snapshot)
    old_task = semantics.semantic_task_identity_hash(snapshot, old_input, "catalogue", {"items": []}, {})
    changed = original(snapshot)
    changed["stages"]["proposal"]["timeout_seconds"] = 359
    monkeypatch.setattr(semantics, "budget_profile", lambda _: deepcopy(changed))
    assert semantics.semantic_input_hash(snapshot, run, [], []) != old_input
    assert semantics._semantic_generator_hash(snapshot) != old_generator
    assert semantics.semantic_task_identity_hash(snapshot, old_input, "catalogue", {"items": []}, {}) != old_task


@pytest.mark.parametrize("value,valid", [(360, True), (359, True), (361, False), (0, False), (-1, False), (True, False), ("360", False), (float("nan"), False)])
def test_shared_checkpoint_authenticates_fixed_timeout_boundary(semantic_case, value, valid):
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    setup = build_case(semantic_case)
    projection = execute(setup)
    row = deepcopy(next(row for row in checkpoint_rows(semantic_case[0]).values() if row["stage"] == "proposal"))
    row["data"]["context"]["budget_profile"]["stages"]["proposal"]["timeout_seconds"] = value
    key = semantics.semantic_task_identity_hash(setup[2], projection["semantic_input_hash"], "proposal", row["data"]["request"], row["data"]["context"])
    row["stage_hash"] = row["data"]["request_hash"] = key
    def read():
        return semantics._checkpoint_record(key, snapshot=setup[2], expected_input_hash=projection["semantic_input_hash"], read_checkpoint=lambda _: row)
    if valid:
        assert read() == row
    else:
        with pytest.raises(ToolError) as failure:
            read()
        assert failure.value.reason_code == "cache_invalid"


def test_current_timeout_recovery_never_resends_or_adds_draft(semantic_case):
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    def timeout(payload, _):
        if payload["stage"] == "proposal":
            error = ToolError("fixture timeout", 408)
            error.codex_reason = "timeout"
            raise error
    setup = build_case(semantic_case, provider=timeout)
    for _ in range(2):
        with pytest.raises(ToolError) as failure:
            execute(setup)
        assert failure.value.status_code == 408 and failure.value.codex_reason == "timeout"
    assert [call["stage"] for call in setup[1]] == ["catalogue", "proposal"]
    assert all(row["attempt"] == 1 for row in checkpoint_rows(semantic_case[0]).values())


@pytest.mark.parametrize("failed", [False, True])
def test_old_profile_completed_and_failed_rows_are_preserved_without_current_reuse(semantic_case, monkeypatch, failed):
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    current_profile = semantics.budget_profile
    legacy = current_profile(semantic_case[1])
    legacy["id"] = "semantic-codex-evidence-v3"
    for limits in legacy["stages"].values():
        limits.pop("timeout_seconds", None)
    setup = build_case(semantic_case)
    execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    # Seed a completed or terminal synthetic legacy receipt with its old fixed
    # profile and genuine identity functions; no legacy call is dispatched.
    with monkeypatch.context() as old:
        old.setattr(semantics, "budget_profile", lambda _: deepcopy(legacy))
        old_input = semantics.semantic_input_hash(setup[2], setup[3], [], [])
        row = deepcopy(next(row for row in rows.values() if row["stage"] == "proposal"))
        row["input_hash"] = old_input
        row["data"]["context"]["budget_profile"] = legacy
        row["data"]["context"]["generator_hash"] = semantics._semantic_generator_hash(setup[2])
        if failed:
            row["status"] = "needs_review"
            row["error_code"] = row["data"]["failure_code"] = "model_call"
            row["data"]["result"] = None
            row["data"]["result_hash"] = semantics._hash(None)
            row["data"]["diagnostic"] = {"status_code": 408, "codex_reason": "timeout"}
        key = semantics.semantic_task_identity_hash(setup[2], old_input, "proposal", row["data"]["request"], row["data"]["context"])
        row["stage_hash"] = row["data"]["request_hash"] = key
    with semantic_case[0]._db() as db:
        db.execute("DELETE FROM business_semantic_tasks")
        db.execute("INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)", (key, "proposal", old_input,
            row["status"], 1, semantics._json(row["data"]), row.get("error_code"), "2026-10-28T00:00:00Z"))
        before_bytes = db.execute("SELECT * FROM business_semantic_tasks WHERE stage_hash=?", (key,)).fetchone()
    before = checkpoint_rows(semantic_case[0])
    with pytest.raises(ToolError) as failure:
        semantics._checkpoint_record(key, snapshot=setup[2], expected_input_hash=old_input, read_checkpoint=before.get)
    assert failure.value.reason_code == "cache_invalid"
    semantic_case[2]["semantic_input_hash"] = semantics.semantic_input_hash(setup[2], setup[3], [], [])
    current = build_case(semantic_case)
    projection = execute(current)
    assert projection["semantic_input_hash"] != old_input
    assert [call["stage"] for call in current[1]] == ["catalogue", "proposal", "review"]
    after = checkpoint_rows(semantic_case[0])
    assert all(after[key] == row for key, row in before.items())
    with semantic_case[0]._db() as db:
        assert db.execute("SELECT * FROM business_semantic_tasks WHERE stage_hash=?", (key,)).fetchone() == before_bytes


def test_generator_and_reviewer_receive_same_generic_business_unknown_rubric():
    # Policy transmission coverage only; this does not assess model interpretation.
    for system in (semantics.PROPOSAL_SYSTEM, semantics.REVIEW_SYSTEM):
        assert "局部缺少实现证据不能支持" in system
        assert "正确性或正常运行疑问" in system
        assert "不额外评价功能是否正确工作" in system
        assert "普通阅读覆盖留私有source_scope" in system
        assert "内部状态数字没有受支持的业务名称" in system
        assert "不发明名称或宣称全系统没有" in system
        assert "公开purpose/summary不写前端/后端实现标签" in system
    assert "缓存或守卫机制不能作为业务标题" in semantics.CATALOGUE_SYSTEM
