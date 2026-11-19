from __future__ import annotations

import json
import uuid

import pytest

from businessmap.core import CoreError, Store, revision
from businessmap.knowledge import KnowledgeNavigator
from businessmap.providers import ToolError
from businessmap.tools_api import ToolsService
from businessmap.exploration import Exploration


@pytest.fixture
def tools(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, 'get_keyring', lambda: (_ for _ in ()).throw(RuntimeError('locked')))
    store = Store(tmp_path / 'data')
    store.open_vault(str(tmp_path / 'vault'))
    yield ToolsService(store)
    store.close()


def write(tools, path, content):
    tools.store._write_note(path, content)
    tools.store.refresh()
    return dict(tools.store._cache[path])


def repo(tools, rid='repo-one', commit='a' * 40):
    data = {'id': rid, 'name': '赛事系统', 'vault_path': str(tools.store.vault), 'status': 'ready',
            'commit': commit, 'path': '/unused', 'url': 'fixture', 'auto_sync': False}
    tools.store._write_repo(data)
    return data


def owned(tools, path='01-Wiki/业务知识/赛事/退款规则.md', content='# 退款规则\n\n退款金额不能超过实际支付金额。\n'):
    repository = repo(tools)
    note = write(tools, path, content)
    tools.store.config_set(f"ai_analysis:{repository['id']}", {'vault_path': str(tools.store.vault), 'notes': {
        'refund': {'path': path, 'revision': note['revision'], 'commit': repository['commit'],
                   'source_paths': ['src/RefundServiceImpl.java'], 'stale': False, 'conflict': False}}})
    return repository, note


def test_sync_index_never_publishes_source_markdown(tools):
    repository = repo(tools)
    files = [{'path': 'src/RefundServiceImpl.java', 'blob': 'b' * 40, 'size': 120}]
    tools.store._publish_code_index(repository, repository['commit'], files, 1)
    assert not list(tools.store.vault.rglob('*.md'))
    index = tools.store.config_get('repository_file_index:' + repository['id'])
    assert index['files'] == files and index['commit'] == repository['commit']
    assert not tools.store.notes() and not tools.store.graph()['nodes']


def test_public_surface_never_reads_raw_or_moved_source_documents(tools):
    raw = write(tools, '00-Inbox/代码资料/repo-one/source.md', '# RefundServiceImpl.java\nsource://repo-one/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2')
    moved = write(tools, '业务/随意移动.md', '---\ntype: code\nsource_path: src/RefundServiceImpl.java\n---\n# 内部资料\nclass RefundServiceImpl {}')
    clean = write(tools, '业务/退款规则.md', '# 退款规则\n退款需审批。\n')
    assert [item['path'] for item in tools.store.notes()] == [clean['path']]
    assert {item['id'] for item in tools.store.graph()['nodes']} == {clean['path']}
    assert KnowledgeNavigator(tools.store).list()['total'] == 1
    for path in (raw['path'], moved['path']):
        with pytest.raises(CoreError):
            tools.store.read_note(path)
        with pytest.raises(CoreError):
            KnowledgeNavigator(tools.store).read(path)


def test_source_evidence_api_is_not_a_public_business_endpoint(tools):
    with pytest.raises(ToolError) as error:
        tools.source_evidence('source://repo-one/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2')
    assert error.value.status_code == 403
    assert 'RefundServiceImpl' not in str(error.value)


def test_private_ownership_survives_removed_technical_yaml(tools):
    repository, note = owned(tools)
    assert tools.note_repositories(note) == {repository['id']}
    assert tools._eligible(note)
    selected = tools.providers.save({'name': '授权模型', 'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
    assert not tools.note_allowed(note, selected)
    tools.bind_model(repository['id'], selected['id'])
    assert tools.note_allowed(note, selected)
    tools.store.update_repository(repository['id'], {'commit': 'c' * 40})
    assert not tools._eligible(note)
    assert not KnowledgeNavigator(tools.store, allowed=tools._eligible).list()['notes']


def test_removed_yaml_with_changed_revision_cannot_become_manual_evidence(tools):
    repository, note = owned(tools)
    tools.store._write_note(note['path'], '# 退款规则\n人工待确认。\n', note['revision'])
    tools.store.refresh()
    assert not tools._eligible(dict(tools.store._cache[note['path']]))


@pytest.mark.parametrize('marker', ['generated: true', '"generated": true', 'generated: yes', 'generated: !!bool true'])
def test_unowned_generated_yaml_cannot_enter_navigation_or_question_evidence(tools, marker):
    note = write(tools, '01-Wiki/业务知识/未归属.md', f'---\ntitle: 退款\n{marker}\n---\n# 退款\n需要审批。\n')
    assert tools.store.public_business_note(note)
    navigator = KnowledgeNavigator(tools.store)
    assert navigator.list()['notes'] == []
    assert navigator.search('退款')['notes'] == []
    with pytest.raises(CoreError) as error:
        navigator.read(note['path'])
    assert error.value.status_code == 404
    assert tools.context('退款') == ([], [])
    reply = tools.chat('退款')
    assert reply['sources'] == [] and '需要审批' not in reply['answer']
    assert not tools._eligible(note)


@pytest.mark.parametrize('frontmatter', ['', '---\ntitle: 退款\ntags: [审批]\n---\n'])
def test_manual_body_generated_words_remain_question_evidence(tools, frontmatter):
    note = write(tools, '业务/退款.md', frontmatter + '# 退款\n实际支付金额需要审批。\ngenerated: true\n')
    assert tools._eligible(note)
    assert KnowledgeNavigator(tools.store).search('退款')['notes'][0]['path'] == note['path']
    chunks, sources = tools.context('退款')
    assert chunks[0]['path'] == sources[0]['path'] == note['path']
    reply = tools.chat('退款')
    assert '实际支付金额需要审批' in reply['answer']
    assert reply['sources'][0]['path'] == note['path']


@pytest.mark.parametrize('marker', ['generated: true', '"generated": true', 'generated: yes', 'generated: !!bool true'])
def test_owned_generated_yaml_retains_private_version_eligibility(tools, marker):
    repository, note = owned(tools, content=f'---\ntitle: 退款\n{marker}\n---\n# 退款\n实际支付金额需要审批。\n')
    assert tools._eligible(note)
    navigator = KnowledgeNavigator(tools.store)
    assert navigator.search('退款')['notes'][0]['path'] == note['path']
    assert tools.context('退款')[0][0]['path'] == note['path']
    tools.store.update_repository(repository['id'], {'commit': 'd' * 40})
    assert not tools._eligible(note)
    assert navigator.list()['notes'] == []
    assert tools.context('退款') == ([], [])


@pytest.mark.parametrize('metadata', ['private_owner: 赛事', 'generated: false', 'generated: "true"', 'generated: ['])
def test_invalid_public_yaml_stays_out_of_question_evidence(tools, metadata):
    note = write(tools, '业务/退款.md', f'---\ntitle: 退款\n{metadata}\n---\n# 退款\n实际支付金额需要审批。\n')
    assert not tools.store.public_business_note(note)
    assert not tools._eligible(note)
    assert KnowledgeNavigator(tools.store).list()['notes'] == []
    assert tools.context('退款') == ([], [])


def test_no_model_fallback_uses_only_public_business_content(tools):
    write(tools, '00-Inbox/代码资料/repo-one/code.md', '# 退款\nRefundServiceImpl source://repo-one/' + 'a' * 40 + '/src/refund.java')
    write(tools, '业务/退款.md', '# 退款\n退款金额不能超过实际支付金额。\n')
    reply = tools.chat('退款')
    serialized = json.dumps(reply, ensure_ascii=False)
    assert '实际支付金额' in serialized
    assert not any(token in serialized for token in ['source://', 'RefundServiceImpl', '00-Inbox', 'repo-one'])


def test_business_exploration_has_no_source_tools_even_when_repo_selected(tools, monkeypatch):
    repository = repo(tools)
    selected = tools.providers.save({'name': '模型', 'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
    tools.bind_model(repository['id'], selected['id'])
    requests = []
    def turn(system, messages, schemas, **kwargs):
        requests.append(schemas)
        return {'tool_calls': [{'id': 'done', 'name': 'finish_answer', 'arguments': {'answer': '当前没有足够业务资料，请补充。', 'citations': [], 'kind': 'clarification'}}],
                'assistant_message': {'role': 'assistant', 'content': ''}, 'usage': {}}
    monkeypatch.setattr(tools.providers, 'tool_turn', turn)
    tools.chat('退款规则是什么', repository_id=repository['id'])
    names = {schema['function']['name'] for schema in requests[0]}
    assert not names.intersection({'repository_search', 'repository_read'})
    exploration = Exploration(tools, selected, 'knowledge', None, lambda _: True, lambda: None, repository_id=repository['id'])
    with pytest.raises(ToolError) as error:
        exploration.execute('repository_read', {'path': 'src/RefundServiceImpl.java'})
    assert error.value.status_code == 403


def test_technical_model_answers_are_not_returned_as_business_text(tools, monkeypatch):
    selected = tools.providers.save({'name': '模型', 'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
    exploration = Exploration(tools, selected, 'knowledge', None, lambda _: True, lambda: None)
    with pytest.raises(ToolError):
        exploration.response('代码位置 source://repo-one/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2')


def test_historical_technical_replies_activities_and_titles_do_not_replay(tools):
    session = tools.chats.create()
    internal = tools.store.config_get('chat:' + session['id'])
    internal['title'] = 'RefundServiceImpl.java'
    internal['messages'] = [{'id': uuid.uuid4().hex, 'role': 'assistant', 'content': '见 source://repo-one/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2',
                             'sources': [{'kind': 'code', 'path': 'source://private', 'source_path': 'src/RefundServiceImpl.java', 'commit': 'a' * 40}],
                             'activities': [{'id': 'step-1', 'tool': 'repository_read', 'label': '读取代码原文', 'status': 'complete', 'path': 'src/RefundServiceImpl.java'}]}]
    tools.chats._save(internal)
    exposed = json.dumps({'session': tools.chats.get(session['id']), 'index': tools.chats.list()}, ensure_ascii=False)
    assert not any(token in exposed for token in ['source://', 'RefundServiceImpl', 'repository_read', 'source_path', 'a' * 40])


def test_workspace_recent_activity_never_replays_technical_maintenance_details(tools):
    tools.store.activity('RefundServiceImpl.java', 'source://repo-one/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2')
    exposed = json.dumps(tools.store.workspace()['recent_activity'], ensure_ascii=False)
    assert 'source://' not in exposed and 'RefundServiceImpl' not in exposed and 'a' * 40 not in exposed


def test_single_repository_publication_keeps_evidence_only_in_private_state(tools, monkeypatch):
    repository = repo(tools)
    selected = tools.providers.save({'name': '模型', 'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
    tools.bind_model(repository['id'], selected['id'])
    monkeypatch.setattr(tools.store, 'repository_context', lambda _: {'repository': repository, 'commit': repository['commit'], 'total_files': 1,
        'files': [{'path': 'src/RefundServiceImpl.java', 'content': 'class RefundServiceImpl {\n  boolean allowed = true;\n}\n'}]})
    def complete(system, user, **kwargs):
        payload = json.loads(user)
        return {'text': json.dumps({'notes': [{'key': 'rules', 'title': '退款条件', 'content': '# 退款条件\n退款金额不能超过实际支付金额。',
            'sources': [{'evidence_id': payload['files'][0]['evidence'][0]['evidence_id']}]}]}), 'usage': {}, 'provider': {}}
    monkeypatch.setattr(tools.providers, 'complete', complete)
    tools.analyze(repository['id'])
    public = '\n'.join(path.read_text() for path in tools.store.vault.rglob('*.md'))
    assert '退款金额不能超过实际支付金额' in public
    assert not any(token in public for token in ['source://', 'RefundServiceImpl', repository['id'], repository['commit'], 'source_paths'])
    private = tools.store.config_get('ai_analysis:' + repository['id'])
    assert any(entry.get('source_paths') for entry in private['notes'].values())
    assert all(tools._eligible(dict(tools.store._cache[entry['path']])) for entry in private['notes'].values() if not entry.get('stale'))


def test_candidate_public_list_does_not_expose_private_receipts(tools):
    tools.store.config_set('analysis_candidates', [{'id': 'suggestion', 'repository_id': 'repo-one', 'key': 'rules',
        'path': '01-Wiki/赛事/规则/退款.md', 'content': '# 退款\n退款需审批。\n', 'commit': 'a' * 40,
        'source_paths': ['src/RefundServiceImpl.java'], 'policy_snapshot': {'provider_id': 'private'}, 'vault_path': str(tools.store.vault)}])
    exposed = json.dumps(tools.candidates(), ensure_ascii=False)
    assert '退款需审批' in exposed
    assert not any(token in exposed for token in ['source_paths', 'RefundServiceImpl', 'policy_snapshot', 'a' * 40, 'repository_id'])


@pytest.mark.parametrize("topic_count", [350, 1000])
def test_large_business_discovery_builds_private_owner_index_once_per_snapshot(tools, monkeypatch, topic_count):
    import time
    repository = repo(tools)
    notes = {}
    for number in range(topic_count):
        path = f'01-Wiki/赛事/规则/业务条件{number}.md'
        content = f'# 业务条件{number}\n\n退款金额不能超过实际支付金额。\n'
        rev = tools.store._write_note(path, content)
        notes[str(number)] = {'path': path, 'revision': rev, 'commit': repository['commit'], 'stale': False, 'conflict': False,
                              'source_paths': [f'src/LongBusinessRuleImplementation{item}.java' for item in range(80)]}
    tools.store.config_set('ai_analysis:' + repository['id'], {'vault_path': str(tools.store.vault), 'notes': notes})
    tools.store.refresh()
    original = tools.store._db
    connections = 0
    def counted():
        nonlocal connections
        connections += 1
        return original()
    monkeypatch.setattr(tools.store, '_db', counted)
    started = time.monotonic()
    result = KnowledgeNavigator(tools.store, allowed=tools._eligible).list(limit=50)
    elapsed = time.monotonic() - started
    assert result['total'] == topic_count
    print(f'private owner snapshot: topics={topic_count}, connections={connections}, elapsed={elapsed:.4f}s')
    assert connections < 20, f'{connections} DB connections for one snapshot in {elapsed:.3f}s'
    assert elapsed < 2.5, f'{topic_count} business topics took {elapsed:.3f}s'
    tools.store.update_repository(repository['id'], {'commit': 'd' * 40})
    assert KnowledgeNavigator(tools.store, allowed=tools._eligible).list()['total'] == 0


def test_technical_final_model_answer_is_terminal_without_paid_rewrite(tools, monkeypatch):
    tools.providers.save({'name': '模型', 'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
    calls = []
    def turn(*args, **kwargs):
        calls.append(True)
        return {'tool_calls': [{'id': 'done', 'name': 'finish_answer', 'arguments': {
            'answer': '见 source://private/' + 'a' * 40 + '/src/RefundServiceImpl.java#L1-L2',
            'citations': [], 'kind': 'clarification'}}], 'assistant_message': {'role': 'assistant', 'content': ''}, 'usage': {}}
    monkeypatch.setattr(tools.providers, 'tool_turn', turn)
    with pytest.raises(ToolError) as error:
        tools.chat('请解释退款')
    assert error.value.status_code == 422 and calls == [True]


@pytest.mark.parametrize('path', ['代码索引/repo/版本-abc.md', '00-Inbox/代码资料/repo/版本-abc.md'])
def test_public_save_rejects_source_folders_before_writing(tools, path):
    before = tools.store.version
    with pytest.raises(CoreError) as error:
        tools.store.save_note(path, '# 代码索引\n')
    assert not (tools.store.vault / path).exists()
    assert error.value.status_code == 422
    assert tools.store.version == before
    assert not list(tools.store.vault.rglob('*.md'))
