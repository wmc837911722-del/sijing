"""Bounded local revision contract; disposable SQLite and post-commit log sink."""
from contextlib import closing
from copy import deepcopy
import json
import logging
import sqlite3

import pytest

from businessmap import business_semantics as s
from businessmap.providers import ToolError
from test_business_semantics import semantic_case, build_case, execute, review, checkpoint_rows


def revision_graph():
    return {'operations': [{'operation_id': 'o0', 'purpose': '创建门店方案。',
                            'summary': '创建须有维护权限并遵守每店5套上限。', 'support_ids': ['f0']}],
            'claims': [
                {'operation_id': 'o0', 'kind': 'rule', 'text': '有维护权限且能管理目标门店时才可创建方案，每店最多5套。',
                 'support_ids': ['f0'], 'scope': '目标门店', 'conditions': ['有维护权限且可管理目标门店'], 'section': 'conditions'},
                {'operation_id': 'o0', 'kind': 'unknown', 'text': '并发创建时能否守住每店5套上限尚未验证。',
                 'support_ids': ['f1'], 'scope': '并发创建', 'conditions': [], 'section': 'unknowns'}],
            'decisions': [{'input_id': 'f0', 'parts': []},
                          {'input_id': 'f1', 'parts': [{'outcome': 'source_scope', 'rationale': '局部阅读没有涵盖常规额度检查。'}]},
                          {'input_id': 'f2', 'parts': [{'outcome': 'technical', 'rationale': '内部维护过程。'}]}]}


def revision_setup(case, *, mutate=None, target='f1', operation_only=False, candidate_suffix=''):
    case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    drafts = reviews = 0
    def provider(payload, count):
        nonlocal drafts, reviews
        if payload['stage'] == 'proposal':
            drafts += 1
            answer = revision_graph()
            if drafts == 2:
                if target == 'f1':
                    answer['claims'][1]['text'] = '多个创建请求同时发生时，能否守住每店5套上限尚未验证。'
                elif target == 'f2':
                    answer['decisions'][2]['parts'][0]['rationale'] = '仅涉及内部维护过程。'
                if operation_only:
                    answer = revision_graph()
                    answer['operations'][0]['purpose'] = '为目标门店创建方案。'
                if mutate:
                    mutate(answer)
            answer['claims'][1]['text'] += candidate_suffix
            return {'text': json.dumps(answer, ensure_ascii=False), 'usage': {}}
        if payload['stage'] == 'review':
            reviews += 1
            answer = review(payload)
            if reviews == 1:
                answer['decision'] = 'reject'
                answer['issues'] = [] if operation_only else [{'code': 'unknown_loss', 'input_ids': [target]}]
                if operation_only:
                    answer['operation_checks'][0]['checks']['business_language'] = False
                else:
                    next(r for r in answer['input_checks'] if r['input_id'] == target)['checks']['uncertainty'] = False
                    if target == 'f1':
                        next(r for r in answer['claim_checks'] if r['claim_id'] == payload['candidate']['claims'][1]['id'])['checks']['uncertainty'] = False
            return {'text': json.dumps(answer), 'usage': {}}
    return build_case(case, provider=provider)


def test_revision_prior_complete_negatives_only_and_independent_r2(semantic_case, caplog):
    """Dropping prior, changing original facts or leaking R1 into R2 fails this."""
    setup = revision_setup(semantic_case)
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    observed = []
    class CommittedRows(logging.Handler):
        def emit(self, record):
            if getattr(record, 'event', None) != 'semantic_checkpoint_committed':
                return
            with closing(sqlite3.connect(semantic_case[0].db_path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT stage,status,attempt,input_hash FROM business_semantic_tasks WHERE stage_hash=?', (record.stage_hash,)).fetchone()
            assert row == (record.stage, record.status, record.attempt, record.input_hash)
            observed.append((record.stage, record.status, record.attempt))
    handler = CommittedRows()
    logger = logging.getLogger('businessmap.business_semantics')
    logger.addHandler(handler)
    try:
        result = execute(setup)
    finally:
        logger.removeHandler(handler)
    assert result['complete'] and len(result['public_documents']) == 1
    assert '有维护权限且能管理目标门店时才可创建方案' in result['public_documents'][0]['body']
    assert '多个创建请求同时发生时' in result['public_documents'][0]['body']
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']
    d1, d2, r2 = setup[1][1], setup[1][3], setup[1][4]
    assert 'prior_candidate' in d2
    assert set(d2) == set(d1) | {'prior_candidate', 'revision_feedback', 'preservation'}
    assert {k: d2[k] for k in d1} == d1
    assert d2['preservation'] == {'claim_indices': [0], 'private_input_ids': ['f0', 'f2']}
    assert d2['revision_feedback']['input_checks'] == [{'input_id': 'f1', 'checks': {'uncertainty': False}}]
    assert d2['revision_feedback']['claim_checks'] == [{'prior_claim_index': 1, 'checks': {'uncertainty': False}}]
    assert d2['revision_feedback']['operation_checks'] == []
    assert not ({'prior_candidate', 'revision_feedback', 'preservation'} & set(r2))
    assert r2['items'] == d1['items'] and r2['operations'] == d1['operations']
    rows = checkpoint_rows(semantic_case[0])
    expected = [('catalogue', 'accepted', 1), ('proposal', 'proposed', 1), ('review', 'needs_review', 1),
                ('proposal', 'proposed', 2), ('review', 'accepted', 2)]
    assert [(r['stage'], r['status'], r['attempt']) for r in rows.values()] == expected
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_checkpoint_committed' and r.status != 'received']
    assert [(r.stage, r.status, r.attempt) for r in events] == expected
    assert len({r.stage_hash for r in events}) == 5 and len(observed) == 9
    r1 = next(r for r in rows.values() if r['stage'] == 'review' and r['attempt'] == 1)
    parent = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 1)
    child = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 2)
    assert len(r1['data']['result']['input_checks']) == 3
    assert len(r1['data']['result']['claim_checks']) == 2
    assert len(r1['data']['feedback']['input_checks']) == 3
    assert s.semantic_revision_payload(setup[2], parent, r1) == d2
    assert s.semantic_revision_base_packet(child, snapshot=setup[2], read_checkpoint=rows.get) == d1
    assert s.semantic_attempt_chain(child['stage_hash'], snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], read_checkpoint=rows.get) == child
    inventory = s.semantic_task_inventory(list(rows.values()), snapshot=setup[2], expected_input_hash=result['semantic_input_hash'])
    assert inventory['occupied_calls'] == 5
    s.validate_stored_semantic_projection(result, snapshot=setup[2], expected_input_hash=result['semantic_input_hash'], store=semantic_case[0])
    before_events = len(caplog.records)
    resumed = execute(setup)
    assert resumed['usage']['requests'] == 0 and resumed['public_documents'] == result['public_documents']
    assert len(setup[1]) == 5 and len(caplog.records) == before_events


def condition_loss(answer):
    answer['claims'][0]['conditions'] = []


def reword(answer):
    answer['claims'][0]['text'] = '创建方案须有维护权限，每店最多5套。'


def delete(answer):
    answer['claims'].pop(0)
    answer['decisions'][0]['parts'] = [{'outcome': 'source_scope', 'rationale': '局部观察。'}]


def duplicate(answer):
    answer['claims'].append(deepcopy(answer['claims'][0]))


def new_unrelated_support(answer):
    answer['claims'].append({'operation_id': 'o0', 'kind': 'result', 'text': '创建方案后获得门店方案。',
                             'support_ids': ['f0'], 'scope': '目标门店', 'conditions': [], 'section': 'results'})


def private_disposition(answer):
    answer['decisions'][2]['parts'] = [{'outcome': 'source_scope', 'rationale': '仅见局部过程。'}]


@pytest.mark.parametrize('mutate', [condition_loss, reword, delete, duplicate, new_unrelated_support, private_disposition])
def test_revision_unrelated_changes_fail_before_second_review(semantic_case, caplog, mutate):
    setup = revision_setup(semantic_case, mutate=mutate)
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    with pytest.raises(ToolError):
        execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal']
    rows = checkpoint_rows(semantic_case[0])
    child = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 2)
    assert child['status'] == 'needs_review' and child['data']['result'] is None
    assert not any(r['stage'] == 'review' and r['attempt'] == 2 for r in rows.values())
    assert not any(getattr(r, 'stage', None) == 'proposal' and getattr(r, 'attempt', None) == 2 and r.status in {'received', 'proposed'} for r in caplog.records)
    count = len(caplog.records)
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 4 and len(caplog.records) == count


def test_revision_unrelated_new_resolved_is_terminal(semantic_case):
    def change(answer):
        answer['decisions'][1]['parts'].append({'outcome': 'resolved', 'resolved_by_ids': ['f0'], 'rationale': '普通上限已有对应规则。'})
    setup = revision_setup(semantic_case, mutate=change, target='f2')
    with pytest.raises(ToolError):
        execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal']


def test_revision_operation_only_failure_does_not_unfreeze_claims(semantic_case):
    setup = revision_setup(semantic_case, mutate=condition_loss, operation_only=True)
    with pytest.raises(ToolError):
        execute(setup)
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal']


def table_case():
    refs = {'f0': {'_fact_id': 'synthetic:rule', 'kind': 'finding', 'text': '有权限时每店最多5套。'},
            'f1': {'_fact_id': 'synthetic:unknown', 'kind': 'unknown', 'text': '普通额度检查及并发顺序待确认，每店5套的并发上限未验证。'},
            'f2': {'_fact_id': 'synthetic:internal', 'kind': 'finding', 'text': '内部维护过程。'}}
    ops = {'o0': {'id': 'operation-main', 'fact_ids': ['synthetic:rule', 'synthetic:unknown']},
           'o1': {'id': 'operation-internal', 'fact_ids': ['synthetic:internal']}}
    wire = revision_graph()
    wire['operations'].append({'operation_id': 'o1', 'purpose': '', 'summary': '', 'support_ids': []})
    wire['decisions'][1]['parts'].append({'outcome': 'resolved', 'resolved_by_ids': ['f0'], 'rationale': '普通额度条件已有对应说明。'})
    canonical = s.decode_support_graph(wire, refs, ops, candidate_bytes=40000)
    return refs, ops, canonical


def test_prior_table_losslessly_roundtrips_mixed_private_empty_and_conditions():
    refs, ops, canonical = table_case()
    table = s._prior_candidate_table(canonical)
    assert table['format'] == 'support-graph-context-table-v1'
    assert table['operations']['columns'] == ['operation_id', 'purpose', 'summary', 'support_ids']
    assert table['claims']['columns'] == ['operation_id', 'kind', 'text', 'support_ids', 'scope', 'conditions', 'section']
    assert table['decisions']['columns'] == ['input_id', 'parts']
    assert table['decisions']['part_columns'] == ['outcome', 'rationale', 'resolved_by_ids']
    assert table['operations']['rows'][1] == ['o1', '', '', []]
    assert table['claims']['rows'][0][5] == ['有维护权限且可管理目标门店']
    assert table['decisions']['rows'][1] == ['f1', [
        ['source_scope', '局部阅读没有涵盖常规额度检查。', []],
        ['resolved', '普通额度条件已有对应说明。', ['f0']]]]
    restored = s._decode_prior_candidate(table, refs, ops, candidate_bytes=40000)
    assert restored == canonical
    table['claims']['rows'][0][5].clear()
    assert canonical['claims'][0]['conditions'] == ['有维护权限且可管理目标门店']


@pytest.mark.parametrize('mutation', ['format', 'columns', 'parts_columns', 'missing_value', 'extra_value', 'nonresolved_support', 'extra_section'])
def test_prior_table_rejects_spoofed_service_shape(mutation):
    refs, ops, canonical = table_case()
    table = s._prior_candidate_table(canonical)
    if mutation == 'format': table['format'] = 'support-graph-v1'
    elif mutation == 'columns': table['claims']['columns'].reverse()
    elif mutation == 'parts_columns': table['decisions']['part_columns'].reverse()
    elif mutation == 'missing_value': table['claims']['rows'][0].pop()
    elif mutation == 'extra_value': table['operations']['rows'][0].append('untrusted')
    elif mutation == 'nonresolved_support': table['decisions']['rows'][2][1][0][2] = ['f0']
    else: table['ignored'] = []
    with pytest.raises(ToolError) as failure:
        s._decode_prior_candidate(table, refs, ops, candidate_bytes=40000)
    assert failure.value.reason_code == 'cache_invalid'


@pytest.mark.parametrize('mutation', ['missing_prior', 'missing_all', 'parent_hash', 'parent_index', 'negative', 'mask', 'prior_text', 'prior_columns', 'prior_format', 'original_fact'])
def test_revision_base_reader_refuses_resigned_context_tampering(semantic_case, mutation):
    setup = revision_setup(semantic_case)
    result = execute(setup)
    rows = checkpoint_rows(semantic_case[0])
    child = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 2)
    child = deepcopy(child)
    payload, context = child['data']['request'], child['data']['context']
    if mutation == 'missing_prior': payload.pop('prior_candidate')
    elif mutation == 'missing_all':
        for key in ('prior_candidate', 'preservation'): payload.pop(key)
    elif mutation == 'parent_hash': context['revision']['parent_result_hash'] = 'f' * 64
    elif mutation == 'parent_index': payload['revision_feedback']['claim_checks'][0]['prior_claim_index'] = 0
    elif mutation == 'negative': payload['revision_feedback']['input_checks'][0]['checks']['conditions'] = False
    elif mutation == 'mask': payload['preservation']['claim_indices'] = []
    elif mutation == 'prior_text': payload['prior_candidate']['claims']['rows'][0][2] = '每店最多5套。'
    elif mutation == 'prior_columns': payload['prior_candidate']['claims']['columns'].reverse()
    elif mutation == 'prior_format': payload['prior_candidate']['format'] = 'support-graph-v1'
    elif mutation == 'original_fact': payload['items'][0]['text'] = '每店最多5套。'
    if mutation in {'parent_index', 'negative'}:
        context['revision']['feedback_hash'] = s._hash(payload['revision_feedback'])
    old_key = child['stage_hash']
    key = s.semantic_task_identity_hash(setup[2], result['semantic_input_hash'], 'proposal', payload, context)
    child['stage_hash'] = child['data']['request_hash'] = key
    rows.pop(old_key)
    rows[key] = child
    with pytest.raises(ToolError):
        s.semantic_revision_base_packet(child, snapshot=setup[2], read_checkpoint=rows.get)
    with pytest.raises(ToolError):
        s.semantic_task_inventory(list(rows.values()), snapshot=setup[2], expected_input_hash=result['semantic_input_hash'])
    assert len(setup[1]) == 5


def test_revision_same_support_claims_have_parent_row_indexes(semantic_case):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    semantic_case[3][1]['text'] += '创建请求同时到达时的处理顺序尚未验证。'
    rounds = 0
    def provider(payload, count):
        nonlocal rounds
        if payload['stage'] == 'proposal':
            wire = revision_graph()
            wire['claims'].append({**deepcopy(wire['claims'][1]), 'text': '创建请求同时到达时的处理顺序尚未验证。'})
            return {'text': json.dumps(wire), 'usage': {}}
        if payload['stage'] == 'review':
            rounds += 1
            answer = review(payload)
            if rounds == 1:
                answer['decision'] = 'reject'
                answer['claim_checks'][2]['checks']['uncertainty'] = False
            return {'text': json.dumps(answer), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    assert execute(setup)['complete']
    d2 = setup[1][3]
    assert d2['revision_feedback']['claim_checks'] == [{'prior_claim_index': 2, 'checks': {'uncertainty': False}}]
    assert d2['revision_feedback']['input_checks'] == [] and d2['revision_feedback']['issues'] == []
    assert d2['preservation']['claim_indices'] == [0]


def test_revision_operation_only_prose_and_support_may_change_without_unfreezing(semantic_case):
    def update(answer):
        answer['operations'][0]['summary'] = '按门店范围和数量条件创建方案，并发创建的上限仍待确认。'
        answer['operations'][0]['support_ids'] = ['f0', 'f1']
    setup = revision_setup(semantic_case, mutate=update, operation_only=True)
    assert execute(setup)['complete']
    assert setup[1][3]['preservation'] == {'claim_indices': [0, 1], 'private_input_ids': ['f0', 'f1', 'f2']}
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']


def test_revision_reorders_claims_without_treating_generated_ids_as_parent_identity(semantic_case):
    def reorder(answer):
        answer['claims'].reverse()
    setup = revision_setup(semantic_case, mutate=reorder)
    result = execute(setup)
    assert result['complete']
    rows = checkpoint_rows(semantic_case[0])
    original = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 1)['data']['result']
    changed = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 2)['data']['result']
    assert original['claims'][0]['id'] != changed['claims'][1]['id']
    assert {k:v for k,v in original['claims'][0].items() if k != 'id'} == {k:v for k,v in changed['claims'][1].items() if k != 'id'}


@pytest.mark.parametrize('size', [63999, 64000, 64001])
def test_revision_exact_serialized_boundary_and_oversize_has_zero_d2_sends(semantic_case, caplog, size):
    baseline = revision_setup(semantic_case)
    execute(baseline)
    proposed = deepcopy(baseline[1][3])
    suffix = '创建请求的处理顺序仍待确认。' * 500
    proposed['prior_candidate']['claims']['rows'][1][2] += suffix
    profile = s.budget_profile(baseline[2])
    from businessmap.codex_provider import _request_payload
    def actual_bytes(payload):
        prompt, schema, _ = _request_payload(
            [{'role': 'system', 'content': s.semantic_stage_system(baseline[2], 'proposal')},
             {'role': 'user', 'content': s._json(payload)}],
            16000, s.semantic_output_schema('proposal', payload))
        return len(prompt.encode()) + len(schema.encode())
    addition = size - actual_bytes(proposed)
    assert addition > 0
    proposed['items'][2]['text'] += 'x' * addition
    assert actual_bytes(proposed) == size
    assert s._semantic_request_bytes(baseline[2], profile, 'proposal', '', proposed) == size
    semantic_case[3][2]['text'] += 'x' * addition
    setup = revision_setup(semantic_case, candidate_suffix=suffix)
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    caplog.clear()
    if size <= 64000:
        result = execute(setup)
        assert result['complete']
        assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']
        assert s._semantic_request_bytes(setup[2], profile, 'proposal', '', setup[1][3]) == size
        assert setup[1][3]['items'] == setup[1][1]['items']
    else:
        with pytest.raises(ToolError) as failure:
            execute(setup)
        assert failure.value.reason_code == 'input_limit'
        assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review']
        rows = [r for r in checkpoint_rows(semantic_case[0]).values() if r['input_hash'] == setup[0]._input_hash]
        child = next(r for r in rows if r['stage'] == 'proposal' and r['attempt'] == 2)
        assert child['status'] == 'needs_review' and child['data']['failure_code'] == 'input_limit'
        assert child['data']['result'] is None and child['data']['request']['items'] == setup[1][1]['items']
        assert s._semantic_request_bytes(setup[2], profile, 'proposal', '', child['data']['request']) == 64001
        assert not any(r['stage'] == 'review' and r['attempt'] == 2 for r in rows)
        assert not any(getattr(r, 'status', None) in {'proposed', 'accepted'} and getattr(r, 'attempt', None) == 2 for r in caplog.records)
        with pytest.raises(ToolError) as repeat:
            execute(setup)
        assert repeat.value.reason_code == 'input_limit' and len(setup[1]) == 3


@pytest.mark.parametrize('failure_mode', ['privacy', 'incomplete', 'unlocated', 'provider', 'unknown_result', 'second_reject'])
def test_revision_unsafe_or_exhausted_paths_never_get_another_paid_attempt(semantic_case, failure_mode):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    rounds = 0
    def provider(payload, count):
        nonlocal rounds
        if payload['stage'] == 'proposal':
            return {'text': json.dumps(revision_graph()), 'usage': {}}
        if payload['stage'] == 'review':
            rounds += 1
            if failure_mode == 'provider':
                raise ToolError('synthetic provider failed', 502)
            if failure_mode == 'unknown_result':
                return {'text': '{}', 'usage': {}, 'output_limited': True}
            answer = review(payload, reject=True)
            if failure_mode == 'privacy':
                answer['issues'][0]['code'] = 'privacy'
            elif failure_mode == 'incomplete':
                answer['input_checks'].pop()
            elif failure_mode == 'unlocated':
                answer['issues'] = [{'code': 'condition_loss', 'input_ids': []}]
            return {'text': json.dumps(answer), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    with pytest.raises(ToolError):
        execute(setup)
    expected = ['catalogue', 'proposal', 'review']
    if failure_mode == 'second_reject':
        expected += ['proposal', 'review']
    assert [p['stage'] for p in setup[1]] == expected
    rows = checkpoint_rows(semantic_case[0])
    assert not any(r['attempt'] > 2 for r in rows.values())
    assert not any(r['stage'] == 'review' and r['status'] == 'accepted' for r in rows.values())
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == len(expected)


def test_revision_mechanical_invalid_first_draft_keeps_legacy_path_without_prior(semantic_case):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    drafts = 0
    def provider(payload, count):
        nonlocal drafts
        if payload['stage'] == 'proposal':
            drafts += 1
            wire = revision_graph()
            if drafts == 1:
                wire['claims'][0]['text'] = '每店最多1000套。'
            else:
                assert 'revision_feedback' in payload
                assert 'prior_candidate' not in payload and 'preservation' not in payload
            return {'text': json.dumps(wire), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    result = execute(setup)
    assert result['complete']
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'proposal', 'review']
    assert setup[1][2]['revision_feedback']['reason_code'] == 'number_unsupported'
    rows = checkpoint_rows(semantic_case[0])
    child = next(r for r in rows.values() if r['stage'] == 'proposal' and r['attempt'] == 2)
    assert s.semantic_revision_base_packet(child, snapshot=setup[2], read_checkpoint=rows.get) == setup[1][1]
    assert execute(setup)['usage']['requests'] == 0 and len(setup[1]) == 4


@pytest.mark.parametrize('change', ['unchanged', 'changed', 'duplicate'])
def test_revision_one_hop_mixed_support_keeps_existing_claim_capacity(semantic_case, monkeypatch, change):
    """A related existing claim may keep its other support; new unrelated edges may not."""
    original = revision_graph
    def mixed_graph():
        value = original()
        value['claims'][1]['support_ids'] = ['f1', 'f2']
        return value
    monkeypatch.setitem(globals(), 'revision_graph', mixed_graph)
    def mutate(value):
        if change == 'changed':
            value['claims'][1]['text'] = '多个请求同时创建方案时，能否守住每店5套上限尚未验证。'
        elif change == 'duplicate':
            value['claims'].append(deepcopy(value['claims'][1]))
    setup = revision_setup(semantic_case, target='f2', mutate=mutate)
    if change == 'duplicate':
        with pytest.raises(ToolError):
            execute(setup)
        assert len(setup[1]) == 4
    else:
        assert execute(setup)['complete']
        assert len(setup[1]) == 5


@pytest.mark.parametrize('locator', [[], ['missing'], ['f1', 'f1'], None, 'f1'])
def test_revision_scope_rejects_each_unreliable_issue_even_with_another_valid_negative(locator):
    refs, operations, canonical = table_case()
    rejected = review({'items': [{'fact_id': fid} for fid in refs], 'operations': operations,
                       'candidate': canonical})
    rejected['decision'] = 'reject'
    rejected['issues'] = [{'code': 'unknown_loss', 'input_ids': ['f1']},
                          {'code': 'condition_loss', 'input_ids': locator}]
    rejected['input_checks'][1]['checks']['uncertainty'] = False
    with pytest.raises(s.SemanticValidationError) as failure:
        s._revision_scope(canonical, rejected)
    assert failure.value.reason_code == 'cache_invalid'


@pytest.mark.parametrize('negative_kind', ['input', 'operation', 'claim'])
@pytest.mark.parametrize('empty_first', [False, True])
def test_revision_mixed_unlocated_issue_stops_before_paid_d2_and_retains_r1(semantic_case, caplog, negative_kind, empty_first):
    semantic_case[1]['provider'].update(kind='codex-local', base_url='codex://local')
    def provider(payload, count):
        if payload['stage'] == 'proposal':
            return {'text': json.dumps(revision_graph()), 'usage': {}}
        if payload['stage'] == 'review' and count == 3:
            answer = review(payload)
            answer['decision'] = 'reject'
            answer['issues'] = [{'code': 'unknown_loss', 'input_ids': ['f1']},
                                {'code': 'condition_loss', 'input_ids': []}]
            if empty_first:
                answer['issues'].reverse()
            if negative_kind == 'input':
                answer['input_checks'][1]['checks']['uncertainty'] = False
            elif negative_kind == 'operation':
                answer['operation_checks'][0]['checks']['business_language'] = False
            else:
                answer['claim_checks'][1]['checks']['uncertainty'] = False
            return {'text': json.dumps(answer), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    caplog.set_level(logging.INFO, logger='businessmap.business_semantics')
    with pytest.raises(ToolError) as failure:
        execute(setup)
    assert failure.value.reason_code == 'cache_invalid'
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review']
    rows = list(checkpoint_rows(semantic_case[0]).values())
    assert [(r['stage'], r['status'], r['attempt']) for r in rows] == [
        ('catalogue', 'accepted', 1), ('proposal', 'proposed', 1), ('review', 'needs_review', 1)]
    rejected = rows[2]['data']['result']
    assert rejected['decision'] == 'reject' and len(rejected['issues']) == 2
    assert rejected['issues'][0 if empty_first else 1] == {'code': 'condition_loss', 'input_ids': []}
    assert len(rejected['input_checks']) == 3 and len(rejected['claim_checks']) == 2
    assert len(rejected['operation_checks']) == 1
    assert rows[2]['data']['feedback']['issues'] == rejected['issues']
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'semantic_checkpoint_committed' and r.status != 'received']
    assert [(r.stage, r.status, r.attempt) for r in events] == [
        ('catalogue', 'accepted', 1), ('proposal', 'proposed', 1), ('review', 'needs_review', 1)]
    assert not any(getattr(r, 'attempt', 1) == 2 for r in caplog.records)
    event_count = len(caplog.records)
    with pytest.raises(ToolError):
        execute(setup)
    assert len(setup[1]) == 3 and len(caplog.records) == event_count
    assert list(checkpoint_rows(semantic_case[0]).values()) == rows


def test_http_mixed_unlocated_issue_keeps_existing_full_feedback_chain(semantic_case):
    def provider(payload, count):
        if payload['stage'] == 'review' and count == 3:
            answer = review(payload, reject=True)
            answer['issues'].append({'code': 'condition_loss', 'input_ids': []})
            answer['input_checks'][1]['checks']['uncertainty'] = False
            return {'text': json.dumps(answer), 'usage': {}}
    setup = build_case(semantic_case, provider=provider)
    result = execute(setup)
    assert result['complete']
    assert [p['stage'] for p in setup[1]] == ['catalogue', 'proposal', 'review', 'proposal', 'review']
    assert 'prior_candidate' not in setup[1][3] and 'preservation' not in setup[1][3]
    assert len(setup[1][3]['revision_feedback']['input_checks']) == 3
    assert setup[1][3]['revision_feedback']['issues'][1]['input_ids'] == []
    assert execute(setup)['usage']['requests'] == 0 and len(setup[1]) == 5
