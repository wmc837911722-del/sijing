from __future__ import annotations

import json

import pytest
import yaml

from businessmap.business_projects import BusinessProjects
from businessmap.core import CoreError, Store
from businessmap.project_analysis import ProjectAnalysis
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService


@pytest.fixture
def project_tools(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store = Store(tmp_path / "data")
    store.open_vault(str(tmp_path / "vault"))
    for name, commit in (("front", "a" * 40), ("back", "b" * 40)):
        store._write_repo({"id": name, "name": name, "vault_path": str(store.vault), "status": "ready",
                           "commit": commit, "path": "unused", "url": "fixture", "auto_sync": False})
    tools = ToolsService(store)
    provider = tools.providers.save({"name": "Fixture", "kind": "openai-compatible",
                                    "base_url": "http://127.0.0.1:1/v1", "model": "fixture"})
    project = BusinessProjects(store).create("天梯赛事", "前后端联合业务", ["front", "back"], {"front": "frontend", "back": "backend"})
    analysis = ProjectAnalysis(tools)
    analysis.bind_model(project["id"], provider["id"])
    def context(repo_id):
        repo = store.get_repository(repo_id)
        return {"repository": repo, "commit": repo["commit"], "total_files": 3, "truncated": True,
                "files": [{"path": "src/rules.py", "content": f"{repo_id}_payment = True\n"}]}
    monkeypatch.setattr(store, "repository_context", context)
    yield analysis, tools, project, provider
    store.close()


def fake_model(monkeypatch, tools, callback=None):
    requests = []
    def complete(system, user, **kwargs):
        kwargs["before_send"]()
        payload = json.loads(user)
        requests.append((system, payload))
        if callback:
            callback(payload)
        sources = [{"evidence_id": item["evidence"][0]["evidence_id"]} for item in payload["files"]]
        return {"text": json.dumps({"notes": [{"key": "rules", "title": "支付规则", "content": "# 支付规则\n两端代码各自声明支付启用；调用关系待确认。", "sources": sources},
                                             {"key": "overview", "title": "赛事地图", "content": "# 赛事地图\n支付业务跨端关系待确认。", "sources": sources}]}),
                "usage": {}, "provider": {"id": "fixture"}}
    monkeypatch.setattr(tools.providers, "complete", complete)
    return requests


def test_analysis_preserves_repository_identity_for_same_paths(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    requests = fake_model(monkeypatch, tools)
    result = analysis.analyze(project["id"])
    assert result["notes_created"] == 3
    assert {item["path"] for item in requests[0][1]["files"]} == {"front/src/rules.py", "back/src/rules.py"}
    from businessmap.content_layout import project_business_paths, project_summary_path
    path = project_business_paths(project["id"])["rules"]
    content = tools.store.read_note(path)["content"]
    assert "source://front/" + "a" * 40 + "/src/rules.py#L1-L1" in content
    assert "source://back/" + "b" * 40 + "/src/rules.py#L1-L1" in content
    meta = yaml.safe_load(content.split("---", 2)[1])
    assert meta["project_id"] == project["id"] and "repository_id" not in meta
    assert meta["source_repositories"] == [{"repository_id": "front", "commit": "a" * 40}, {"repository_id": "back", "commit": "b" * 40}]
    summary = tools.store.read_note(project_summary_path(project["id"]))["content"]
    assert "1 / 3" in summary and "front" in summary and "back" in summary
    for repo_id in project["repository_ids"]:
        assert tools.store.config_get(f"ai_analysis:{repo_id}", {}).get("auto_enabled") is False
    assert analysis.status(project["id"])["status"] == "completed"


def test_round_robin_budget_includes_each_member_before_large_repo_consumes_it(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    def context(repo_id):
        repo = tools.store.get_repository(repo_id)
        return {"repository": repo, "commit": repo["commit"], "total_files": 100, "truncated": True,
                "files": [{"path": f"src/rule-{i}.py", "content": (f"{repo_id}_rule = True\n" * 400)} for i in range(30)]}
    monkeypatch.setattr(tools.store, "repository_context", context)
    requests = fake_model(monkeypatch, tools)
    analysis.analyze(project["id"])
    system, payload = requests[0]
    assert len(json.dumps(payload, ensure_ascii=False)) + len(system) <= 44000
    assert [item["path"].split("/", 1)[0] for item in payload["files"][:2]] == ["front", "back"]
    counts = {rid: sum(len(item["content"]) for item in payload["files"] if item["path"].startswith(rid + "/")) for rid in ("front", "back")}
    assert min(counts.values()) / max(counts.values()) > 0.5


@pytest.mark.parametrize("change", ["project", "commit", "policy", "vault", "provider"])
def test_changed_snapshot_never_publishes_model_result(project_tools, monkeypatch, tmp_path, change):
    analysis, tools, project, provider = project_tools
    def mutate(_payload):
        if change == "project":
            BusinessProjects(tools.store).update(project["id"], project["revision"], name="改名")
        elif change == "commit":
            tools.store.update_repository("front", {"commit": "c" * 40})
        elif change == "policy":
            tools.bind_model("front", provider["id"])
        elif change == "vault":
            tools.store.open_vault(str(tmp_path / "other-vault"))
        else:
            tools.providers.save({**{key: provider[key] for key in ("id", "name", "kind", "base_url", "model")}, "model": "changed"})
    fake_model(monkeypatch, tools, mutate)
    with pytest.raises((ToolError, CoreError)) as error:
        analysis.analyze(project["id"])
    assert error.value.status_code == 409
    assert not list((tmp_path / "vault" / "02-Areas").rglob("*.md"))
    assert not list((tmp_path / "vault" / "01-Wiki").rglob("*.md"))


def test_member_model_binding_must_share_exact_destination(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    other = tools.providers.save({"name": "Other", "kind": "openai-compatible", "base_url": "http://127.0.0.1:2/v1", "model": "other"})
    tools.bind_model("back", other["id"])
    assert analysis.model_policy(project["id"])["status"] == "changed"
    requests = fake_model(monkeypatch, tools)
    with pytest.raises(ToolError):
        analysis.analyze(project["id"])
    assert not requests


def test_human_edit_and_concurrent_creation_preserved_with_separate_candidate(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    from businessmap.content_layout import project_business_paths
    path = project_business_paths(project["id"])["rules"]
    fake_model(monkeypatch, tools)
    analysis.analyze(project["id"])
    note = tools.store.read_note(path)
    human = note["content"] + "\n人工确认：退款另行审批。\n"
    tools.store.save_note(path, human, note["revision"])
    analysis.analyze(project["id"])
    assert tools.store.read_note(path)["content"] == human
    candidates = tools.store.config_get("project_analysis_candidates", [])
    assert any(item["path"] == path and item["project_id"] == project["id"] for item in candidates)
    overview = tools.store.read_note(project_business_paths(project["id"])["overview"])["content"]
    assert "[[" + path[:-3] not in overview


def test_replaced_candidate_is_cas_rejected_before_write(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    from businessmap.content_layout import project_business_paths
    path = project_business_paths(project["id"])["rules"]
    fake_model(monkeypatch, tools)
    analysis.analyze(project["id"])
    saved = tools.store.read_note(path)
    human = tools.store.save_note(path, saved["content"] + "\n人工保持。\n", saved["revision"])
    analysis.analyze(project["id"])
    candidate = next(item for item in analysis.candidates() if item["key"] == "rules")
    snapshot = analysis.snapshot
    def replace_while_reading_candidate(project_id):
        current = tools.store.config_get("project_analysis_candidates", [])
        tools.store.config_set("project_analysis_candidates", [
            {**item, "content": item["content"] + "\n更新的建议\n"} if item["id"] == candidate["id"] else item
            for item in current])
        return snapshot(project_id)
    monkeypatch.setattr(analysis, "snapshot", replace_while_reading_candidate)
    with pytest.raises(ToolError) as error:
        analysis.apply_candidate(candidate["id"], human["revision"])
    assert error.value.status_code == 409
    assert tools.store.read_note(path)["content"] == human["content"]
    assert "更新的建议" in next(item for item in analysis.candidates() if item["id"] == candidate["id"])["content"]


def test_map_and_summary_link_sources_while_cards_keep_exact_code(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    from businessmap.content_layout import project_business_paths, project_summary_path
    fake_model(monkeypatch, tools)
    analysis.analyze(project["id"])
    paths = project_business_paths(project["id"])
    card = tools.store.read_note(paths["rules"])["content"]
    assert "```text\nfront_payment = True\n```" in card
    assert "```text\nback_payment = True\n```" in card
    for path in (paths["overview"], project_summary_path(project["id"])):
        content = tools.store.read_note(path)["content"]
        assert "source://front/" in content and "source://back/" in content
        assert "front_payment = True" not in content and "back_payment = True" not in content


def test_candidate_application_uses_current_note_revision_and_preserves_other_vault_candidates(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    from businessmap.content_layout import project_business_paths
    path = project_business_paths(project["id"])["rules"]
    fake_model(monkeypatch, tools)
    analysis.analyze(project["id"])
    original = tools.store.read_note(path)
    edited = tools.store.save_note(path, original["content"] + "\n人工修改。\n", original["revision"])
    analysis.analyze(project["id"])
    candidate = next(item for item in analysis.candidates() if item["key"] == "rules")
    other = {"id": "other-vault-candidate", "vault_path": "/other/vault"}
    tools.store.config_update("project_analysis_candidates", lambda old: [*old, other], [])
    with pytest.raises(CoreError):
        analysis.apply_candidate(candidate["id"], original["revision"])
    assert tools.store.read_note(path)["revision"] == edited["revision"]
    saved = analysis.apply_candidate(candidate["id"], edited["revision"])
    assert saved["content"] == candidate["content"]
    assert other in tools.store.config_get("project_analysis_candidates", [])
    assert not any(item["id"] == candidate["id"] for item in analysis.candidates())


def test_queued_analysis_rejects_membership_revision_change_before_model_send(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    callbacks = []
    monkeypatch.setattr(tools.store, "run_task", lambda task_id, callback: callbacks.append((task_id, callback)))
    requests = fake_model(monkeypatch, tools)
    queued = analysis.queue(project["id"])
    assert analysis.queue(project["id"])["task_id"] == queued["task_id"]
    assert len(callbacks) == 1
    BusinessProjects(tools.store).update(project["id"], project["revision"], repository_ids=["front"])
    with pytest.raises(ToolError) as error:
        callbacks[0][1](callbacks[0][0])
    assert error.value.status_code == 409 and not requests


def test_repository_not_ready_rejected_before_model(project_tools, monkeypatch):
    analysis, tools, project, provider = project_tools
    tools.store.update_repository("back", {"status": "syncing"})
    requests = fake_model(monkeypatch, tools)
    with pytest.raises(ToolError):
        analysis.analyze(project["id"])
    assert not requests


@pytest.mark.parametrize("members,roles", [(["front"], {"front": "frontend"}), (["back"], {"back": "backend"}), (["front", "back"], {})])
def test_partial_or_untyped_project_cannot_claim_joint_analysis(project_tools, monkeypatch, members, roles):
    analysis, tools, project, provider = project_tools
    BusinessProjects(tools.store).update(project["id"], project["revision"], repository_ids=members, repository_roles=roles)
    requests = fake_model(monkeypatch, tools)
    with pytest.raises(ToolError, match="请补齐前端和后端仓库后再联合分析") as error:
        analysis.queue(project["id"])
    assert error.value.status_code == 409 and not requests
    assert not [task for task in tools.store.tasks() if task["kind"] == "project-analysis"]
