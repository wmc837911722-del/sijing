from __future__ import annotations

import json
import subprocess

import pytest

from businessmap.core import CoreError, Store
from businessmap.exploration import Exploration
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService


@pytest.fixture
def project_sources(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store = Store(tmp_path / "data")
    service = ToolsService(store)
    selected = service.providers.save({"name": "Project fixture", "kind": "openai-compatible",
        "base_url": "https://example.invalid/v1", "model": "fixture"})
    repos = []
    for repo_id, name, content in (("web", "管理前端", "SAVE_URL = '/api/entries'\n"),
                                   ("api", "赛事后端", "POST_ROUTE = '/api/entries'\n"),
                                   ("outside", "其他项目", "OUTSIDE_SECRET = 1\n")):
        source = tmp_path / repo_id
        source.mkdir()
        (source / "entry.py").write_text(content)
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        repo = {"id": repo_id, "name": name, "path": str(source), "url": str(source),
                "remote": False, "status": "ready", "branch": "main", "commit": commit,
                "vault_path": str(store.vault), "connection": {"auth_type": "none"}}
        store._write_repo(repo)
        repos.append(repo)
    project = {"id": "arena", "name": "天梯赛事", "repository_ids": ["web", "api"], "revision": "initial",
               "repository_roles": {"web": "frontend", "api": "backend"}}
    store.config_set("project-chat-fixture", project)
    # Isolate the chat runtime contract from project CRUD, owned by another module.
    monkeypatch.setattr(store, "get_project", lambda _id: store.config_get("project-chat-fixture"), raising=False)
    yield service, selected, repos, project
    store.close()


def runtime(project_sources, **kwargs):
    service, selected, _, project = project_sources
    return Exploration(service, selected, "knowledge", None, lambda _: True, lambda: None,
                       project_id=project["id"], **kwargs)


def tool_reply(*calls):
    normalized = [{"id": f"call-{index}", "name": name, "arguments": args}
                  for index, (name, args) in enumerate(calls)]
    return {"tool_calls": normalized, "usage": {"input_tokens": 100, "output_tokens": 10},
        "assistant_message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": item["id"], "type": "function", "function": {
                "name": item["name"], "arguments": json.dumps(item["arguments"])}} for item in normalized]}}


def test_project_question_reads_public_business_topics_without_repository_receipts(project_sources, monkeypatch):
    service, _, repos, _ = project_sources
    paths = ["01-Wiki/天梯赛事/流程/提交申请.md", "01-Wiki/天梯赛事/规则/受理条件.md"]
    for path, body in zip(paths, ["# 提交申请\n申请提交后等待受理。\n", "# 受理条件\n未通过条件核验的申请不予受理。\n"]):
        service.store.save_note(path, body)
    turns = []
    def turn(system, messages, tools, **kwargs):
        turns.append((system, messages, tools))
        receipts = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        if not receipts:
            return tool_reply(*[("knowledge_read", {"path": path}) for path in paths])
        ids = [item["citation_id"] for item in receipts]
        return tool_reply(("finish_answer", {"answer": f"提交后等待受理，未通过核验不受理。[申请]({ids[0]}) [条件]({ids[1]})",
            "citations": ids, "kind": "answer"}))
    monkeypatch.setattr(service.providers, "tool_turn", turn)
    result = runtime(project_sources).run("提交申请后如何受理？")
    assert result["completion"] == "complete"
    assert {source["path"] for source in result["sources"]} == set(paths)
    assert all(set(source) == {"path", "title"} for source in result['sources'])
    assert not {"repository_search", "repository_read"} & {tool['function']['name'] for tool in turns[0][2]}
    public = json.dumps(result, ensure_ascii=False)
    assert not any(value in public for value in ['source://', 'entry.py', 'source_path', *(repo['commit'] for repo in repos)])

@pytest.mark.parametrize("name,args", [
    ("repository_search", {"repository_id": "outside", "query": "OUTSIDE_SECRET"}),
    ("repository_read", {"repository_id": "outside", "path": "entry.py"}),
    ("repository_read", {"path": "entry.py"}),
])
def test_project_source_tools_reject_non_member_or_missing_repository(project_sources, name, args):
    with pytest.raises(ToolError):
        runtime(project_sources).execute(name, args)


def test_unread_member_commit_change_invalidates_project_answer(project_sources, monkeypatch):
    service, _, _, _ = project_sources

    def turn(*args, **kwargs):
        service.store.update_repository("api", {"commit": "f" * 40})
        return tool_reply(("finish_answer", {"answer": "需要补充。", "citations": [], "kind": "clarification"}))

    monkeypatch.setattr(service.providers, "tool_turn", turn)
    with pytest.raises((ToolError, CoreError), match="版本已变化"):
        runtime(project_sources).run("查看项目")


def test_project_membership_change_invalidates_answer(project_sources, monkeypatch):
    service, _, _, project = project_sources

    def turn(*args, **kwargs):
        service.store.config_set("project-chat-fixture", {**project, "repository_ids": ["web"], "revision": "changed"})
        return tool_reply(("finish_answer", {"answer": "需要补充。", "citations": [], "kind": "clarification"}))

    monkeypatch.setattr(service.providers, "tool_turn", turn)
    with pytest.raises((ToolError, CoreError), match="项目.*变化"):
        runtime(project_sources).run("查看项目")


def test_repository_and_project_scope_cannot_be_combined(project_sources):
    with pytest.raises(ToolError):
        runtime(project_sources, repository_id="web")


def test_closing_context_keeps_business_evidence_without_source_identity(project_sources):
    exploration = runtime(project_sources)
    for path, body in [("业务/提交.md", "# 提交\n提交后等待受理。\n"), ("业务/受理.md", "# 受理\n条件不满足时拒绝。\n")]:
        exploration.store.save_note(path, body)
        exploration.execute("knowledge_read", {"path": path})
    _, messages, _ = exploration.closing_context("申请受理", 15000)
    evidence = json.loads(messages[0]["content"])["read_evidence"]
    assert len(evidence) == 2
    assert all(set(item) == {"reference", "kind", "content"} for item in evidence)
    assert '提交后等待受理' in json.dumps(evidence, ensure_ascii=False)
    assert '条件不满足时拒绝' in json.dumps(evidence, ensure_ascii=False)

def test_legacy_chat_defaults_project_selection_to_none(project_sources):
    service, _, _, _ = project_sources
    session = service.chats.create()
    persisted = service.store.config_get(f"chat:{session['id']}")
    persisted.pop("project_id", None)
    service.store.config_set(f"chat:{session['id']}", persisted)
    index = service.store.config_get("chat_index")
    index[0].pop("project_id", None)
    service.store.config_set("chat_index", index)
    assert service.chats.get(session["id"])["project_id"] is None
    assert service.chats.list()[0]["project_id"] is None


@pytest.fixture
def persisted_project(project_sources, monkeypatch):
    service, selected, _, _ = project_sources
    monkeypatch.delattr(service.store, "get_project")
    project = service.store.create_project("天梯赛事", "前后端联动", ["web", "api"],
                                          {"web": "frontend", "api": "backend"})
    for repo_id in project["repository_ids"]:
        service.bind_model(repo_id, selected["id"])
    return service, project


def test_project_chat_persists_scope_and_resets_history_after_project_change(persisted_project, monkeypatch):
    service, project = persisted_project
    histories = []

    def turn(system, messages, tools, **kwargs):
        histories.append(json.loads(json.dumps(messages[:-1])))
        return tool_reply(("finish_answer", {"answer": "需要核对实现。", "citations": [], "kind": "clarification"}))

    monkeypatch.setattr(service.providers, "tool_turn", turn)
    session = service.chats.create()
    first = service.chats.send(session["id"], "前后端入口", project_id=project["id"])
    assert first["project_id"] == project["id"] and first["repository_id"] is None
    assert not first["messages"][-1].get("error")
    service.chats.send(session["id"], "继续检查", project_id=project["id"])
    assert histories[0] == [] and len(histories[1]) == 2
    assert service.chats.list()[0]["project_id"] == project["id"]
    reopened = Store(service.store.data_dir)
    try:
        assert ToolsService(reopened).chats.get(session["id"])["project_id"] == project["id"]
    finally:
        reopened.close()
    service.store.update_project(project["id"], project["revision"], description="已补充业务范围")
    service.chats.send(session["id"], "项目已更新", project_id=project["id"])
    assert histories[-1] == []
    cleared = service.chats.send(session["id"], "当前知识库")
    assert cleared["project_id"] is None and histories[-1] == []


def test_invalid_project_selection_does_not_append_chat_message(persisted_project):
    service, project = persisted_project
    session = service.chats.create()
    with pytest.raises(CoreError):
        service.chats.send(session["id"], "不存在的项目", project_id="missing")
    with pytest.raises(ToolError):
        service.chats.send(session["id"], "重复范围", project_id=project["id"], repository_id="web")
    assert service.chats.get(session["id"])["messages"] == []


def test_service_project_chat_uses_privately_owned_public_business_topics(persisted_project, monkeypatch):
    from businessmap.business_content import render_public_note
    service, project = persisted_project
    sources = [{"repository_id": rid, "commit": service.store.get_repository(rid)['commit']} for rid in project['repository_ids']]
    notes = {}
    for key, title, body in [('flow', '提交申请', '提交后等待受理。'), ('rule', '受理条件', '条件不满足时拒绝。')]:
        path = f"01-Wiki/天梯赛事/{title}.md"
        note = service.store.save_note(path, render_public_note(title, '# ' + title + '\n' + body))
        notes[key] = {'path': path, 'revision': note['revision'], 'source_repositories': sources, 'stale': False, 'conflict': False}
    service.store.config_set(f"project_analysis:{project['id']}", {'vault_path': str(service.store.vault), 'project_revision': project['revision'], 'notes': notes})
    def turn(system, messages, tools, **kwargs):
        receipts = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        assert not {'repository_read', 'repository_search'} & {tool['function']['name'] for tool in tools}
        if not receipts:
            return tool_reply(*[("knowledge_read", {"path": note['path']}) for note in notes.values()])
        return tool_reply(("finish_answer", {"answer": "提交后等待受理；条件不满足时拒绝。",
            "citations": [item["citation_id"] for item in receipts], "kind": "answer"}))
    monkeypatch.setattr(service.providers, "tool_turn", turn)
    response = service.chats.send(service.chats.create()["id"], "完整申请流程", project_id=project["id"])
    answer = response["messages"][-1]
    assert not answer.get("error")
    assert {item["path"] for item in answer["sources"]} == {note['path'] for note in notes.values()}
    assert all(set(item) == {'path', 'title'} for item in answer['sources'])
