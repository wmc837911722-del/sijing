"""One explicit graph authority; real SQLite checkpoints and captured logs."""
from copy import deepcopy
import json
import logging

import pytest
from jsonschema import Draft202012Validator

from businessmap import business_semantics as s
from test_business_semantics import semantic_case, build_case, execute, review


def graph(payload=None):
    return {'operations': [{'operation_id': 'o0', 'purpose': '创建方案。', 'summary': '每店最多5套。', 'support_ids': ['f0']}],
            'claims': [{'operation_id': 'o0', 'kind': 'rule', 'text': '每店最多5套。', 'support_ids': ['f0'], 'scope': '门店', 'conditions': [], 'section': 'conditions'},
                       {'operation_id': 'o0', 'kind': 'unknown', 'text': '并发创建能否保持5套上限尚未验证。', 'support_ids': ['f1'], 'scope': '并发创建', 'conditions': [], 'section': 'unknowns'}],
            'decisions': [{'input_id': 'f2', 'parts': [{'outcome': 'technical', 'rationale': '内部维护机制。'}]},
                          {'input_id': 'f1', 'parts': [{'outcome': 'resolved', 'resolved_by_ids': ['f0'], 'rationale': '普通额度检查已有说明。'}]},
                          {'input_id': 'f0', 'parts': []}]}


def refs_ops():
    return ({'f0': {'_fact_id': 'fact:limit', 'kind': 'finding', 'text': '每店最多5套。'},
             'f1': {'_fact_id': 'fact:unknown', 'kind': 'unknown', 'text': '并发创建能否保持5套上限尚未验证。'},
             'f2': {'_fact_id': 'fact:cache', 'kind': 'finding', 'text': '内部维护机制。'}},
            {'o0': {'id': 'operation', 'fact_ids': ['fact:limit', 'fact:unknown', 'fact:cache']}})


def decode(value, **kwargs):
    refs, ops = refs_ops()
    return s.decode_support_graph(value, refs, ops, candidate_bytes=40000, **kwargs)


def test_graph_pipeline_commits_exact_edges_and_resume_has_no_calls_or_events(semantic_case, caplog):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    def provider(payload, count):
        if payload['stage'] == 'proposal':
            assert payload['proposal_format'] == 'support-graph-v1'
            return {'text': json.dumps(graph()), 'usage': {}}
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    setup = build_case(semantic_case, provider=provider)
    result = execute(setup)
    assert result['complete'] and '每店最多5套' in result['public_documents'][0]['body']
    s.validate_stored_semantic_projection(result, snapshot=setup[2],
        expected_input_hash=result['semantic_input_hash'], store=semantic_case[0])
    with semantic_case[0]._db() as db:
        rows = db.execute('SELECT stage,status,data FROM business_semantic_tasks ORDER BY rowid').fetchall()
    assert [(r[0], r[1]) for r in rows] == [('catalogue', 'accepted'), ('proposal', 'proposed'), ('review', 'accepted')]
    canonical = json.loads(rows[1][2])['result']
    assert [(c['id'], c['support_ids']) for c in canonical['claims']] == [('c00', ['f0']), ('c31', ['f1'])]
    assert canonical['decisions'][1]['parts'][0]['claim_ids'] == ['c31']
    assert canonical['decisions'][1]['parts'][1]['resolved_by_ids'] == ['f0']
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_checkpoint_committed' and r.status != 'received']
    assert [(r.stage, r.status) for r in events] == [(r[0], r[1]) for r in rows]
    assert len({r.stage_hash for r in events}) == 3
    count = len(caplog.records)
    resumed = execute(setup)
    assert resumed['usage']['requests'] == 0 and len(setup[1]) == 3 and len(caplog.records) == count


def test_graph_serialized_systems_include_generic_observation_scope_rubric(semantic_case):
    from businessmap.codex_provider import _request_payload
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    setup = build_case(semantic_case)
    complete = setup[0].service.providers.complete
    prompts = {}
    def capture(system, user, **kwargs):
        payload = json.loads(user)
        if payload['stage'] in {'proposal', 'review'}:
            wire, _, _ = _request_payload([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                kwargs['max_output'], kwargs['output_schema'])
            prompts[payload['stage']] = json.loads(wire)['messages'][0]['content']
        return complete(system, user, **kwargs)
    setup[0].service.providers.complete = capture
    execute(setup)
    assert set(prompts) == {'proposal', 'review'}
    assert '严格schema' in prompts['review']
    assert '"input_checks":[{"input_id":"f0"' not in prompts['review']
    # These assert policy serialization, not a model's semantic judgment.
    for system in prompts.values():
        for policy in ('observation.roles/scope/origin仅表来源', '逐子问题',
                       '不证业务主体/对立/范围/跨端一致性',
                       '局部注释/声明不一', '正证不将观察resolved',
                       '正面条件仍分别完整公开', '真实规则互斥',
                       '具体请求缺必需输入', '用户可见限制/失败条件/结果',
                       '公开各字段用业务语言，禁来源审计标签',
                       '截止正证+注释冲突→条件公开/观察私有',
                       '同条件规则互斥或请求缺必需活动编号→冲突/不匹配及未知公开'):
            assert policy in system


def test_graph_rubric_changes_codex_generator_without_changing_http_identity():
    local = {'provider': {'kind': 'codex-local', 'base_url': 'codex://local'}}
    http = {'provider': {'kind': 'openai-compatible', 'base_url': 'http://127.0.0.1:1/v1'}}
    assert s._semantic_generator_hash(local) != '191a18ca9a19ee272edbeb1f8b74130e6dd50ec5f7c79f1461f2e0d14499e3d3'
    assert s._semantic_generator_hash(http) == 'ec531bc0a937a71a91a834ec214abfee7fbec56fc6092affe16a93ab2eb08387'


def test_graph_projection_only_uses_declared_edges_and_preserves_mixed_unknown():
    value = decode(graph())
    assert [(c['id'], c['support_ids']) for c in value['claims']] == [('c00', ['f0']), ('c31', ['f1'])]
    assert [p['outcome'] for p in value['decisions'][1]['parts']] == ['retained', 'resolved']
    assert value['decisions'][2]['parts'] == [{'outcome': 'claim', 'claim_ids': ['c00'], 'support_ids': ['f0'], 'resolved_by_ids': [], 'rationale': '由模型显式声明的资料支持关系生成；业务含义仍须独立核对。'}]


@pytest.mark.parametrize('case', ['missing_edge', 'legacy_id', 'legacy_backlink', 'extra_root', 'duplicate_support', 'unknown_positive', 'missing_input', 'finding_resolved', 'empty_resolution', 'self_resolution'])
def test_graph_rejects_undeclared_or_illegal_support_without_repair(case):
    value = graph()
    if case == 'missing_edge': value['claims'].pop(0)
    elif case == 'legacy_id': value['claims'][0]['id'] = 'c00'
    elif case == 'legacy_backlink': value['decisions'][2]['parts'] = [{'outcome': 'claim', 'claim_ids': ['c00'], 'support_ids': ['f0'], 'resolved_by_ids': [], 'rationale': '理由'}]
    elif case == 'extra_root': value['extra'] = []
    elif case == 'duplicate_support': value['claims'][0]['support_ids'] = ['f0', 'f0']
    elif case == 'unknown_positive': value['claims'][0]['support_ids'] = ['f1']
    elif case == 'missing_input': value['decisions'].pop()
    elif case == 'finding_resolved': value['decisions'][0]['parts'] = [{'outcome': 'resolved', 'resolved_by_ids': ['f0'], 'rationale': '理由'}]
    elif case == 'empty_resolution': value['decisions'][1]['parts'][0]['resolved_by_ids'] = []
    elif case == 'self_resolution': value['decisions'][1]['parts'][0]['resolved_by_ids'] = ['f1']
    with pytest.raises(s.SemanticValidationError): decode(value)


def test_graph_canonical_expansion_cannot_evade_candidate_limit():
    value = graph()
    raw = len(s._json(value).encode())
    with pytest.raises(s.SemanticValidationError) as error:
        refs, ops = refs_ops()
        s.decode_support_graph(value, refs, ops, candidate_bytes=raw)
    assert error.value.reason_code == 'candidate_limit'


@pytest.mark.parametrize('mutation', ['id', 'rationale', 'backlink'])
def test_graph_stored_roundtrip_rejects_resigned_canonical_forgery(mutation):
    value = decode(graph())
    if mutation == 'id':
        value['claims'][0]['id'] = 'c099'
        value['decisions'][2]['parts'][0]['claim_ids'] = ['c099']
    elif mutation == 'rationale': value['decisions'][2]['parts'][0]['rationale'] = '伪造生成说明。'
    else: value['decisions'][2]['parts'].append(deepcopy(value['decisions'][2]['parts'][0]))
    refs, ops = refs_ops()
    with pytest.raises(s.SemanticValidationError): s.validate_graph_canonical(value, refs, ops, candidate_bytes=40000)


def test_graph_schema_matches_wire_and_forbids_model_ids_and_public_parts():
    payload = {'proposal_format': 'support-graph-v1', 'items': [
        {'fact_id': 'f0', 'kind': 'finding'}, {'fact_id': 'f1', 'kind': 'unknown'}, {'fact_id': 'f2', 'kind': 'finding'}],
        'operations': [{'operation_id': 'o0'}]}
    validator = Draft202012Validator(s.semantic_output_schema('proposal', payload))
    validator.validate(graph())
    for mutation in ('id', 'public_part', 'unknown_support', 'finding_resolved'):
        value = graph()
        if mutation == 'id': value['claims'][0]['id'] = 'c00'
        elif mutation == 'public_part': value['decisions'][2]['parts'] = [{'outcome': 'claim', 'claim_ids': ['c00']}]
        elif mutation == 'unknown_support': value['claims'][0]['support_ids'] = ['f1']
        else: value['decisions'][0]['parts'] = [{'outcome': 'resolved', 'resolved_by_ids': ['f0'], 'rationale': '理由'}]
        assert not validator.is_valid(value)


def test_graph_same_packet_support_does_not_override_partition_restrictions():
    refs, ops = refs_ops()
    ops = {'o0': {'id': 'operation-a', 'fact_ids': ['fact:limit']},
           'o1': {'id': 'operation-b', 'fact_ids': ['fact:unknown', 'fact:cache']}}
    value = graph()
    value['claims'][1]['operation_id'] = 'o1'
    value['claims'].append({**value['claims'][0], 'operation_id': 'o1'})
    value['operations'].append({'operation_id': 'o1', 'purpose': '核对创建。', 'summary': '每店最多5套。', 'support_ids': ['f0']})
    canonical = s.decode_support_graph(value, refs, ops, candidate_bytes=40000)
    assert canonical['decisions'][2]['parts'][1]['claim_ids'] == ['c02']
    assert canonical['claims'][2]['support_ids'] == ['f0']
    with pytest.raises(s.SemanticValidationError):
        s.decode_support_graph(value, refs, ops, candidate_bytes=40000,
            allowed_support={'o0': {'fact:limit'}, 'o1': {'fact:unknown', 'fact:cache'}})


def test_graph_feedback_maps_private_parts_after_generated_public_parts():
    value = graph()
    refs, ops = refs_ops()
    ops = {'o0': {'id': 'operation-a', 'fact_ids': ['fact:limit', 'fact:cache']},
           'o1': {'id': 'operation-b', 'fact_ids': ['fact:unknown']}}
    value['claims'][1]['operation_id'] = 'o1'
    value['operations'].append({'operation_id': 'o1', 'purpose': '核对并发创建。', 'summary': '并发创建仍待确认。', 'support_ids': ['f1']})
    with pytest.raises(s.SemanticValidationError) as error:
        s.decode_support_graph(value, refs, ops, candidate_bytes=40000,
            allowed_support={'o0': {'fact:limit', 'fact:cache'}, 'o1': {'fact:unknown'}})
    assert error.value.validation['part_index'] == 0  # raw private, canonical index was1
    feedback = s._mechanical_feedback(error.value.reason_code, error.value.validation, value,
        {'items': [{'fact_id': fid} for fid in refs], 'operations': [{'operation_id': oid} for oid in ops]})
    assert feedback['returned_array'] == 'decisions' and feedback['input_ids'] == ['f1']
    assert feedback['diagnostic']['row_index'] == 1


@pytest.mark.parametrize('format_value', [None, 'semantic-json-schema-v2', 'support-graph-v0'])
@pytest.mark.parametrize('stage', ['proposal', 'review'])
def test_trusted_codex_snapshot_never_selects_legacy_from_missing_or_forged_marker(format_value, stage):
    snapshot = {'provider': {'kind': 'codex-local', 'base_url': 'codex://local'}}
    request = {'stage': stage}
    if format_value is not None: request['proposal_format'] = format_value
    with pytest.raises(s.SemanticValidationError) as error:
        s.semantic_task_identity_hash(snapshot, '0' * 64, stage, request, {})
    assert error.value.reason_code == 'cache_invalid'


@pytest.mark.parametrize('mutation', ['id', 'rationale', 'extra_public_part', 'marker'])
def test_current_graph_cache_rejects_forgery_before_new_sends(semantic_case, mutation):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    setup = build_case(semantic_case)
    projection = execute(setup)
    with semantic_case[0]._db() as db:
        key, encoded = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='proposal'").fetchone()
        data = json.loads(encoded)
        if mutation == 'id':
            data['result']['claims'][0]['id'] = 'c099'
            data['result']['decisions'][0]['parts'][0]['claim_ids'] = ['c099']
        elif mutation == 'rationale': data['result']['decisions'][0]['parts'][0]['rationale'] = '伪造生成说明。'
        elif mutation == 'extra_public_part': data['result']['decisions'][0]['parts'].append(deepcopy(data['result']['decisions'][0]['parts'][0]))
        else: data['request'].pop('proposal_format')
        data['result_hash'] = s._hash(data['result'])
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), key))
    with pytest.raises(s.SemanticValidationError) as proof_error:
        s.validate_stored_semantic_projection(projection, snapshot=setup[2],
            expected_input_hash=projection['semantic_input_hash'], store=semantic_case[0])
    assert proof_error.value.reason_code == 'cache_invalid'
    with pytest.raises(s.SemanticValidationError) as error: execute(setup)
    assert error.value.reason_code == 'cache_invalid' and len(setup[1]) == 3


def test_fixture_encoder_refuses_legacy_missing_backlink_instead_of_repairing_it():
    from support_graph_fixture import positive_graph
    from test_business_semantics import _canonical_candidate
    payload = {'proposal_format': 'support-graph-v1', 'items': [
        {'fact_id': 'f0', 'kind': 'finding', 'text': '每家门店最多5套。'},
        {'fact_id': 'f1', 'kind': 'unknown', 'text': '并发创建是否守住5套上限。'},
        {'fact_id': 'f2', 'kind': 'finding', 'text': '内部维护机制。'}],
        'operations': [{'operation_id': 'o0', 'fact_ids': ['f0', 'f1', 'f2']}]}
    value = _canonical_candidate(payload)
    value['decisions'][0]['parts'][0]['claim_ids'] = []
    with pytest.raises(s.SemanticValidationError): positive_graph(value, payload)


def legacy_service_projection(projection):
    """Exact prior service renderer, copied from the frozen before fixture.

    Keep this historical fixture independent of production renderers and of
    ignored audit files, so the regression also runs from a clean checkout.
    """
    value = deepcopy(projection)
    coverage = value['semantic_coverage']
    name, operations = coverage['project_name'], coverage['catalogue']
    public = [operation for operation in operations if operation['purpose']]
    value['overview_body'] = '# ' + name + ' · 业务地图\n\n' + '\n\n'.join(
        '- ' + operation['title'] + '：' + operation['purpose'] for operation in public) + '\n'
    value['summary_body'] = '# ' + name + ' · 业务梳理总结\n\n' + '\n\n'.join(
        '- ' + operation['title'] + '：' + operation['summary'] for operation in public)
    value['summary_body'] += '\n\n## 使用边界\n\n此处为静态资料整合与语义核对；实际运行、并发及未验证的业务条件仍以各操作的待确认事项为准。\n'
    value['verification']['projection_hash'] = s._projection_hash(value)
    return value


def test_http_exact_old_footer_keeps_real_stored_proof_and_zero_call_resume(semantic_case, caplog):
    setup = build_case(semantic_case)
    projection = execute(setup)
    legacy = legacy_service_projection(projection)
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    s.validate_stored_semantic_projection(legacy, snapshot=setup[2],
        expected_input_hash=legacy['semantic_input_hash'], store=semantic_case[0])
    resumed = execute(setup)
    assert resumed['complete'] and resumed['usage']['requests'] == 0
    assert len(setup[1]) == 3 and not caplog.records
    assert '本文根据现有业务资料整理。' in resumed['summary_body']


@pytest.mark.parametrize('mutation', ['arbitrary_footer', 'extra_text', 'overview', 'operation_summary', 'business_body', 'source_mapping', 'missing_review'])
def test_old_footer_compatibility_never_accepts_other_public_or_private_mutation(semantic_case, mutation):
    setup = build_case(semantic_case)
    legacy = legacy_service_projection(execute(setup))
    if mutation == 'arbitrary_footer': legacy['summary_body'] = legacy['summary_body'].rsplit('## 使用边界', 1)[0] + '## 使用边界\n\n均已验证完成。\n'
    elif mutation == 'extra_text': legacy['summary_body'] += '另一个未经支持的说明。\n'
    elif mutation == 'overview': legacy['overview_body'] += '未经支持的操作。\n'
    elif mutation == 'operation_summary':
        legacy['semantic_coverage']['catalogue'][0]['summary'] += '业务规则已改变。'
        legacy['summary_body'] = legacy['summary_body'].replace('创建受操作权限、门店范围及每店数量约束。', '创建受操作权限、门店范围及每店数量约束。业务规则已改变。')
        legacy['verification']['catalogue_hash'] = s._hash(legacy['semantic_coverage']['catalogue'])
    elif mutation == 'business_body': legacy['public_documents'][0]['body'] += '未经支持的业务条件。\n'
    elif mutation == 'source_mapping': next(iter(legacy['source_mapping'].values()))['evidence_ids'] = []
    else:
        with semantic_case[0]._db() as db: db.execute("DELETE FROM business_semantic_tasks WHERE stage='review'")
    legacy['verification']['projection_hash'] = s._projection_hash(legacy)
    with pytest.raises(s.SemanticValidationError):
        s.validate_stored_semantic_projection(legacy, snapshot=setup[2],
            expected_input_hash=legacy['semantic_input_hash'], store=semantic_case[0])
    assert len(setup[1]) == 3
