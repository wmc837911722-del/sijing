"""A locally rejected public draft may use the ordinary second draft only."""
import json

import pytest

from businessmap.business_semantics import SemanticValidationError
from test_business_semantics import build_case, candidate, execute, semantic_case
from test_full_analysis import full_project
from test_proposal_timeout_recovery import source_timeout
from test_semantic_integration import REAL_STORED_VALIDATOR, REAL_SYNTHESIS_BUILD


def test_local_public_rejection_resumes_from_original_packet(semantic_case):
    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    rejected = "src/private/RejectedDraft.java"
    paused = False

    def guard():
        if paused:
            raise InterruptedError("paused after paid response")

    def first_response(payload, count):
        nonlocal paused
        if payload["stage"] == "proposal":
            value = candidate(payload)
            value["operations"][0]["summary"] = "内部位置为 " + rejected
            paused = True
            return {"text": json.dumps(value), "usage": {}}

    first = build_case(semantic_case, guard=guard, provider=first_response)
    with pytest.raises(InterruptedError):
        execute(first)
    assert [p["stage"] for p in first[1]] == ["catalogue", "proposal"]

    def failed_receipt():
        with semantic_case[0]._db() as db:
            return db.execute("SELECT * FROM business_semantic_tasks WHERE stage='proposal' AND attempt=1").fetchone()

    before = failed_receipt()
    data = json.loads(before[5])
    assert before[3] == "needs_review" and before[6] == data["failure_code"] == "public_content"
    assert data["result"] is None and data["diagnostic"] == {}
    assert "feedback" not in data and rejected not in before[5]

    blocked = build_case(semantic_case)
    with pytest.raises(SemanticValidationError, match="已阻止发布"):
        execute(blocked)
    assert not blocked[1] and failed_receipt() == before

    resumed = build_case(semantic_case)
    resumed[0].public_content_revision = (resumed[3]["run_id"], before[0])
    projection = execute(resumed)
    assert projection["complete"] and [p["stage"] for p in resumed[1]] == ["proposal", "review"]
    draft, review = resumed[1]
    assert {key: value for key, value in draft.items() if key != "revision_feedback"} == first[1][1]
    assert draft["revision_feedback"]["reason_code"] == "public_content"
    assert "revision_feedback" not in review and review["candidate"]["operations"][0]["summary"]
    assert rejected not in json.dumps([draft, review, projection]) and failed_receipt() == before
    again = build_case(semantic_case)
    assert execute(again)["public_documents"] == projection["public_documents"] and not again[1]


def _reject_first_draft(semantic_case, *, reason="public_content"):
    if reason == "public_content":
        def provider(payload, count):
            if payload["stage"] == "proposal":
                value = candidate(payload)
                value["operations"][0]["purpose"] = "参见 src/private/Hidden.java"
                return {"text": json.dumps(value), "usage": {}}
    else:
        from businessmap.providers import ToolError

        def provider(payload, count):
            if payload["stage"] == "proposal":
                raise ToolError("provider returned 422", 422)

    semantic_case[1]["provider"].update(kind="codex-local", base_url="codex://local")
    first = build_case(semantic_case, provider=provider)
    with pytest.raises(SemanticValidationError) as failure:
        execute(first)
    assert failure.value.reason_code == reason
    with semantic_case[0]._db() as db:
        return db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='proposal'").fetchone()[0]


@pytest.mark.parametrize("corruption", ["raw_error", "diagnostic", "feedback", "run", "parent", "model_call"])
def test_explicit_selection_requires_the_matching_intact_local_rejection(semantic_case, corruption):
    parent = _reject_first_draft(semantic_case, reason="model_call" if corruption == "model_call" else "public_content")
    store = semantic_case[0]
    if corruption in {"raw_error", "diagnostic", "feedback"}:
        with store._db() as db:
            if corruption == "raw_error":
                db.execute("UPDATE business_semantic_tasks SET error_code='model_call' WHERE stage_hash=?", (parent,))
            else:
                row = json.loads(db.execute("SELECT data FROM business_semantic_tasks WHERE stage_hash=?", (parent,)).fetchone()[0])
                row[corruption] = {"unexpected": "untrusted"}
                from businessmap.business_semantics import _json
                db.execute("UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?", (_json(row), parent))
    selected = build_case(semantic_case)
    selected[0].public_content_revision = ("other" if corruption == "run" else selected[3]["run_id"],
                                            "0" * 64 if corruption == "parent" else parent)
    with pytest.raises(SemanticValidationError):
        execute(selected)
    assert not selected[1]
    with store._db() as db:
        assert not db.execute("SELECT 1 FROM business_semantic_tasks WHERE attempt=2").fetchall()


@pytest.mark.parametrize("failure", ["public_content", "model_call", "review_rejected"])
def test_failed_second_draft_or_review_never_spends_another_slot(semantic_case, failure):
    parent = _reject_first_draft(semantic_case)
    from businessmap.providers import ToolError

    def second(payload, count):
        if payload["stage"] == "proposal":
            if failure == "model_call":
                raise ToolError("unknown paid outcome", 408)
            if failure == "public_content":
                value = candidate(payload)
                value["operations"][0]["summary"] = "源文件为 src/private/HiddenAgain.java"
                return {"text": json.dumps(value), "usage": {}}
    selected = build_case(semantic_case, reject=failure == "review_rejected", provider=second)
    selected[0].public_content_revision = (selected[3]["run_id"], parent)
    with pytest.raises(SemanticValidationError) as error:
        execute(selected)
    assert error.value.reason_code == failure
    assert [p["stage"] for p in selected[1]] == (["proposal", "review"] if failure == "review_rejected" else ["proposal"])
    for selection in [None, selected[0].public_content_revision]:
        restarted = build_case(semantic_case)
        restarted[0].public_content_revision = selection
        with pytest.raises(SemanticValidationError):
            execute(restarted)
        assert not restarted[1]
    with semantic_case[0]._db() as db:
        assert not db.execute("SELECT 1 FROM business_semantic_tasks WHERE attempt>2").fetchall()


def test_original_private_source_can_be_read_but_never_used_in_public_output(semantic_case):
    # The source-only word is part of a technical input; the accepted fixture
    # keeps it out of every published claim and operation.
    semantic_case[3][2]["_forbidden"] = ["缓存有效期"]
    parent = _reject_first_draft(semantic_case)
    selected = build_case(semantic_case)
    selected[0].public_content_revision = (selected[3]["run_id"], parent)
    projection = execute(selected)
    assert "缓存有效期" in json.dumps(selected[1][0]["items"], ensure_ascii=False)
    assert "缓存有效期" not in json.dumps(projection["public_documents"], ensure_ascii=False)


@pytest.mark.parametrize("existing", [False, True])
def test_controller_wrong_selection_never_creates_a_run_or_an_initial_source_call(full_project, monkeypatch, existing):
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap.providers import ToolError
    full, service, project = full_project
    run_id = full.plan(project["id"])["run_id"] if existing else "other"
    selected = FullProjectAnalysis(service, public_content_revision=(run_id, "0" * 64))
    attempted = []
    def provider(*args, **kwargs):
        attempted.append(1)
        raise ToolError("unexpected call", 502)
    monkeypatch.setattr(service.providers, "complete", provider)
    with pytest.raises(ToolError):
        selected.analyze(project["id"])
    assert not attempted
    with service.store._db() as db:
        assert db.execute("SELECT COUNT(*) FROM full_analysis_runs").fetchone()[0] == int(existing)


def test_controller_uses_only_second_draft_and_review_then_publishes(full_project, monkeypatch):
    from businessmap import business_semantics as semantics
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap.full_synthesis import FullBusinessSynthesis
    from businessmap.project_analysis import ProjectAnalysis
    from test_business_semantics import independent_candidate, review

    full, service, project = full_project
    full.SOURCE_BYTES = 37000
    monkeypatch.setattr(FullBusinessSynthesis, "build", REAL_SYNTHESIS_BUILD)
    monkeypatch.setattr(semantics, "validate_stored_semantic_projection", REAL_STORED_VALIDATOR)
    dest = service.providers.save({"name": "fixture", "kind": "codex-local", "base_url": "codex://local", "model": "fixture"})
    ProjectAnalysis(service).bind_model(project["id"], dest["id"])

    calls = []
    def response(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        stage = payload.get("stage", "source" if "files" in payload else "routing")
        calls.append(stage)
        if stage == "source":
            value = {"title": "业务办理", "summary": "", "findings": [{"text": "办理业务需要核对门店授权。",
                "evidence_ids": [payload["files"][0]["evidence_id"]]}], "unknowns": []}
        elif stage == "routing":
            value = {"assignments": [{"fact_id": item["fact_id"], "disposition": "public", "category": "flows",
                "title": "办理业务"} for item in payload["items"]]}
        elif stage == "catalogue":
            value = {"operations": [{"title": "办理业务", "category": "flows",
                "label_ids": [item["label_id"] for item in payload["items"]]}]}
        elif stage == "proposal":
            value = independent_candidate(payload)
            if "revision_feedback" not in payload:
                value["operations"][0]["purpose"] = "见 src/private/UnsafeFixture.java"
        else:
            assert stage == "review"
            value = review(payload)
        return {"text": json.dumps(value), "usage": {}}
    monkeypatch.setattr(service.providers, "complete", response)
    failed = full.analyze(project["id"])
    assert failed["status"] == "failed" and failed["synthesis_failure"]["reason_code"] == "public_content"
    with service.store._db() as db:
        original = db.execute("SELECT * FROM business_semantic_tasks WHERE stage='proposal'").fetchone()
    paid = list(calls)
    selected = FullProjectAnalysis(service, public_content_revision=(failed["run_id"], original[0]))
    result = selected.analyze(project["id"])
    assert result["run_id"] == failed["run_id"] and result["publication_complete"], result.get("error")
    assert calls == paid + ["proposal", "review"]
    assert full.analyze(project["id"])["publication_complete"] and calls == paid + ["proposal", "review"]
    with service.store._db() as db:
        assert db.execute("SELECT * FROM business_semantic_tasks WHERE stage_hash=?", (original[0],)).fetchone() == original
    notes = list(service.store.vault.rglob("*.md"))
    assert notes and all("UnsafeFixture" not in note.read_text() for note in notes)


def test_concurrent_selection_cannot_charge_the_same_draft_twice(semantic_case):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    parent = _reject_first_draft(semantic_case)
    started = threading.Event()
    release = threading.Event()
    def pause_inside_provider(payload, count):
        if payload["stage"] == "proposal":
            started.set()
            assert release.wait(5)

    first = build_case(semantic_case, provider=pause_inside_provider)
    second = build_case(semantic_case)
    first[0].public_content_revision = second[0].public_content_revision = (first[3]["run_id"], parent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(execute, first)
        try:
            assert started.wait(5)
            with pytest.raises(SemanticValidationError) as busy:
                execute(second)
            assert busy.value.reason_code == "interrupted" and not second[1]
        finally:
            release.set()
        assert owner.result()["complete"]
    assert [p["stage"] for p in first[1]] == ["proposal", "review"]


@pytest.mark.parametrize("paused_stage", ["planned", "received_proposal", "received_review"])
def test_unspent_slots_require_selection_and_settled_review_replays_freely(semantic_case, monkeypatch, paused_stage):
    parent = _reject_first_draft(semantic_case)
    paused = False
    def guard():
        if paused:
            raise InterruptedError()
    def after_response(payload, count):
        nonlocal paused
        paused = paused_stage == "received_" + payload["stage"]
    selected = build_case(semantic_case, guard=guard, provider=after_response)
    selected[0].public_content_revision = (selected[3]["run_id"], parent)
    if paused_stage == "planned":
        original = selected[0]._plan_public_rewrite
        def after_plan(selection):
            nonlocal paused
            original(selection)
            paused = True
        monkeypatch.setattr(selected[0], "_plan_public_rewrite", after_plan)
    with pytest.raises(InterruptedError):
        execute(selected)

    ordinary = build_case(semantic_case)
    if paused_stage == "received_review":
        assert execute(ordinary)["complete"] and not ordinary[1]
        return
    with pytest.raises(SemanticValidationError) as blocked:
        execute(ordinary)
    assert blocked.value.reason_code == "public_content" and not ordinary[1]
    resumed = build_case(semantic_case)
    resumed[0].public_content_revision = (selected[3]["run_id"], parent)
    assert execute(resumed)["complete"]
    assert [p["stage"] for p in resumed[1]] == (["proposal", "review"] if paused_stage == "planned" else ["review"])

@pytest.mark.parametrize("source_timeout", ["two-packets-public-rejection"], indirect=True)
@pytest.mark.parametrize("draft_received", [False, True])
def test_selected_public_rewrite_goes_first_and_keeps_catalogue_order(source_timeout, draft_received):
    from businessmap.business_semantics import _hash
    from test_business_semantics import independent_candidate, review

    c = source_timeout
    assert c.parent["data"]["failure_code"] == "public_content"
    runtime = c.runtime
    runtime.CONCURRENCY = 1
    runtime.public_content_revision = (c.run["run_id"], c.parent["stage_hash"])
    target = c.parent["data"]["context"]["packet_hash"]
    calls = []
    paused = False
    def guard():
        if paused:
            raise InterruptedError("paused after receiving the selected draft")
    def complete(system, user, **kwargs):
        nonlocal paused
        kwargs["before_send"]()
        payload = json.loads(user)
        calls.append(payload)
        paused = draft_received and len(calls) == 1
        value = independent_candidate(payload) if payload["stage"] == "proposal" else review(payload)
        return {"text": json.dumps(value), "usage": {}}
    runtime.guard = guard
    runtime.service.providers.complete = complete
    if draft_received:
        runtime._plan_public_rewrite(runtime.public_content_revision)
        with pytest.raises(InterruptedError):
            runtime._integrate_packet(c.packets[-1])
        assert calls[0]["revision_feedback"]["reason_code"] == "public_content"
        paused = False
    initial = len(calls)
    projection = runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    expected = ["review", "proposal", "review"] if draft_received else ["proposal", "review", "proposal", "review"]
    assert [p["stage"] for p in calls[initial:]] == expected
    assert calls[0]["revision_feedback"]["reason_code"] == "public_content"
    assert calls[1]["packet_hash"] == target
    assert projection["verification"]["packet_hashes"] == [_hash(runtime._packet(p)[0]) for p in c.packets]
    assert projection["verification"]["packet_hashes"][-1] == target
    seen = list(calls)
    schedule = runtime._run_packets
    runtime._run_packets = lambda packets: schedule(packets)
    replay = runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert replay["verification"] == projection["verification"] and calls == seen
