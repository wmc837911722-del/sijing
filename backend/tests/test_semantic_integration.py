import copy
import json

import pytest

from businessmap.full_synthesis import FullBusinessSynthesis, _hash, _json
from businessmap.providers import ToolError
from businessmap.business_semantics import validate_stored_semantic_projection as REAL_STORED_VALIDATOR
from test_full_synthesis import synthesis_project, fake_model
from test_full_analysis import full_project, model
from test_business_semantics import semantic_case


def test_l0_route_cache_survives_public_version_change(synthesis_project, monkeypatch):
    service, snapshot, run, chunks = synthesis_project
    synthesis = FullBusinessSynthesis(service)
    run['snapshot_id'] = 'fixed-snapshot'
    units = [{'kind': 'finding', 'text': '每店最多5套。', '_fact_id': 'private-fact', '_module_ids': ['private-module']}]
    key = synthesis.cache_identity(snapshot, run, units, 0, legacy=True)
    cached = {'assignments': [{'fact_id': 'private-fact', 'disposition': 'public', 'category': 'rules', 'title': '创建限制', 'text': '每店最多5套。'}]}
    with service.store._db() as db:
        db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, _json(cached), '2026-10-27'))
    calls = fake_model(monkeypatch, service)
    usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    assert synthesis.VERSION == 8 and synthesis.L0_ROUTING_VERSION == 8
    assert synthesis._request(snapshot, run, units, 0, usage) == {'assignments': [{k: v for k, v in row.items() if k != 'text'} for row in cached['assignments']]}
    assert not calls and usage['cache_hits'] == 1


def test_source_units_observation_never_infers_matching_from_roles(synthesis_project):
    from businessmap.full_synthesis import source_fact_units
    service, snapshot, run, chunks = synthesis_project
    for chunk in chunks:
        chunk['segments'] = [{'evidence_id': chunk['result']['findings'][0]['evidence_ids'][0], 'repository_id': 'front', 'path': 'src/rules.ts'}]
    units = source_fact_units(snapshot, run, chunks)
    assert all(unit['_observation']['scope'] == 'source_subset' for unit in units)
    assert all(unit['_observation']['match_receipt_ids'] == [] for unit in units)
    assert all(unit['_observation']['roles'] == ['frontend'] for unit in units)


def test_partial_matching_evidence_does_not_upgrade_whole_context(synthesis_project):
    from businessmap.full_synthesis import source_fact_units
    service, snapshot, run, chunks = synthesis_project
    chunk = copy.deepcopy(chunks[0])
    first = chunk['result']['findings'][0]['evidence_ids'][0]
    chunk['segments'] = [{'evidence_id': first, 'repository_id': 'front', 'path': 'src/create.ts'},
                         {'evidence_id': 'unmatched', 'repository_id': 'back', 'path': 'src/other.java'}]
    canonical = chunk['id'] + ':' + first
    addenda = [{'addendum_id': 'extra', 'evidence': [], 'result': {'summary': '', 'findings': [], 'unknowns': []},
        'matching_receipts': [{'match_id': 'independent-match', 'evidence_ids': [canonical]}]}]
    units = source_fact_units(snapshot, run, [chunk], addenda)
    assert units[0]['kind'] == 'context' and units[0]['_observation']['scope'] == 'source_subset'
    assert units[1]['kind'] == 'finding' and units[1]['_observation']['scope'] == 'matched_operation'


@pytest.mark.parametrize('change', ['old_version', 'addendum', 'override'])
def test_completed_projection_rebuilds_for_current_semantic_identity(full_project, monkeypatch, change):
    from businessmap.business_semantics import semantic_input_hash
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, project = full_project
    source_calls = model(monkeypatch, service)
    first = full.analyze(project['id'])
    assert first['publication_complete']
    run = full._load(project['id'])
    before = copy.deepcopy(full._chunks(run))
    paid = len(source_calls)
    if change == 'old_version':
        run['synthesis_version'] = 7
    elif change == 'override':
        run['catalogue_override_revision'] = 'operator-revision-two'
    else:
        monkeypatch.setattr(AnalysisAddenda, 'accepted', lambda *args: [{'addendum_id': 'new-receipt', 'input_hash': 'new',
            'result': {'title': '补充条件', 'summary': '补充有门店范围。', 'findings': [], 'unknowns': []}, 'evidence': [], 'destination': {}}])
    full._save(run)
    calls = []
    previous = FullBusinessSynthesis.build
    def build(self, snapshot, current, chunks):
        calls.append(current['run_id'])
        return previous(self, snapshot, current, chunks)
    monkeypatch.setattr(FullBusinessSynthesis, 'build', build)
    resumed = full.analyze(project['id'])
    assert resumed['publication_complete'] and len(calls) == 1
    assert len(source_calls) == paid and full._chunks(full._load(project['id'])) == before


# Capture before full_project deliberately replaces the external synthesis stage.
REAL_SYNTHESIS_BUILD = FullBusinessSynthesis.build


def semantic_pipeline(full_project, monkeypatch, *, reject=False, pause=False, shared=False):
    from test_business_semantics import review
    full, service, project = full_project
    full.SOURCE_BYTES = 37000
    calls = []
    paused = False
    monkeypatch.setattr(FullBusinessSynthesis, 'build', REAL_SYNTHESIS_BUILD)
    import businessmap.business_semantics as semantics
    monkeypatch.setattr(semantics, 'validate_stored_semantic_projection', REAL_STORED_VALIDATOR)
    assert semantics.validate_stored_semantic_projection is REAL_STORED_VALIDATOR
    def complete(system, user, **kwargs):
        nonlocal paused
        kwargs['before_send']()
        payload = json.loads(user)
        stage = payload.get('stage', 'source' if 'files' in payload else 'routing')
        calls.append(stage)
        if stage == 'source':
            answer = {'title': '创建规则', 'summary': '本批覆盖创建说明。',
                'findings': [{'text': '每家门店最多5套。', 'evidence_ids': [payload['files'][0]['evidence_id']]}],
                'unknowns': ['本组件未见额度检查；并发创建是否仍守住5套上限尚未验证。' + ('实际业务操作的成功结果尚未验证。' if shared else '')]}
        elif stage == 'routing':
            answer = {'assignments': [{'fact_id': item['fact_id'], 'disposition': 'public', 'category': 'rules', 'title': '核验实际效果' if shared and item['kind'] == 'unknown' else '创建方案'} for item in payload['items']]}
        elif stage in {'catalogue', 'subdivide'}:
            answer = {'operations': [{'title': '创建方案', 'category': 'flows', 'label_ids': [item['label_id'] for item in payload['items']]}]}
            if shared:
                answer = {'operations': [{'title': item['title'], 'category': 'flows', 'label_ids': [item['label_id']]} for item in payload['items']]}
        elif stage == 'proposal':
            op = payload['operations'][0]['operation_id']
            finding = next(item['fact_id'] for item in payload['items'] if item['kind'] == 'finding')
            answer = {'operations': [{'operation_id': op, 'purpose': '创建门店方案。', 'summary': '每店受数量条件限制，实际并发效果待核验。', 'support_ids': [finding]}], 'claims': [], 'decisions': []}
            for index, item in enumerate(payload['items']):
                fid, cid = item['fact_id'], 'c' + str(index)
                if item['kind'] == 'context':
                    parts = [{'outcome': 'source_scope', 'claim_ids': [], 'support_ids': [fid], 'resolved_by_ids': [], 'rationale': '仅为来源阅读范围。'}]
                else:
                    unknown = item['kind'] == 'unknown'
                    answer['claims'].append({'id': cid, 'operation_id': op, 'kind': 'unknown' if unknown else 'rule',
                        'text': '并发创建是否严格保持每店5套上限尚未验证。' if unknown else item['text'], 'support_ids': [fid],
                        'scope': '对应门店', 'conditions': [], 'section': 'unknowns' if unknown else 'conditions'})
                    parts = [{'outcome': 'retained' if unknown else 'claim', 'claim_ids': [cid], 'support_ids': [fid], 'resolved_by_ids': [], 'rationale': '完整保留业务条件。'}]
                    if unknown:
                        parts.insert(0, {'outcome': 'resolved', 'claim_ids': [], 'support_ids': [finding], 'resolved_by_ids': [finding], 'rationale': '普通额度已有明确实现说明，并不证明并发。'})
                answer['decisions'].append({'input_id': fid, 'parts': parts})
            if shared:
                other = payload['operations'][1]['operation_id']
                unknown_id = next(item['fact_id'] for item in payload['items'] if item['kind'] == 'unknown')
                answer['operations'].append({'operation_id': other, 'purpose': '核实实际业务效果。', 'summary': '实际使用结果仍待验证。', 'support_ids': [unknown_id]})
                for index, item in enumerate(payload['items']):
                    if item['kind'] != 'unknown':
                        continue
                    cid = 'c' + str(1000 + index)
                    answer['claims'].append({'id': cid, 'operation_id': other, 'kind': 'unknown', 'text': '实际业务操作的成功结果尚未验证。',
                        'support_ids': [item['fact_id']], 'scope': '实际业务操作', 'conditions': [], 'section': 'unknowns'})
                    decision = next(row for row in answer['decisions'] if row['input_id'] == item['fact_id'])
                    decision['parts'].append({'outcome': 'retained', 'claim_ids': [cid], 'support_ids': [item['fact_id']], 'resolved_by_ids': [], 'rationale': '保留实际使用未知。'})
            if pause and not paused:
                full.set_paused(project['id'], True)
                paused = True
        else:
            answer = review(payload, reject=reject)
        return {'text': json.dumps(answer, ensure_ascii=False), 'usage': {'input_tokens': 10, 'output_tokens': 10}}
    monkeypatch.setattr(service.providers, 'complete', complete)
    return full, service, project, calls


def test_schema8_scope_and_resolved_are_not_fake_internal(full_project, monkeypatch):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch)
    result = full.analyze(project['id'])
    assert result['status'] == 'completed' and result['publication_complete']
    projection = full._load(project['id'])['synthesis']
    coverage = projection['semantic_coverage']
    assert coverage['schema'] == 8 and 'fact_coverage' not in projection
    assert set(coverage['input_ids']) == set(coverage['decisions'])
    assert {'source_scope', 'claim', 'retained', 'resolved'} <= {part['outcome'] for row in coverage['decisions'].values() for part in row['parts']}
    body = projection['public_documents'][0]['body']
    assert '每家门店最多5套' in body and '并发创建是否严格保持每店5套上限尚未验证' in body
    assert '本组件未见' not in body and '本批覆盖' not in body
    assert calls.count('proposal') == calls.count('review') == 1


def test_rejected_review_survives_resume_without_new_calls(full_project, monkeypatch):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch, reject=True)
    result = full.analyze(project['id'])
    assert result['status'] == 'failed' and not result.get('publication_complete')
    assert result['synthesis_failure']['reason_code'] == 'review_rejected'
    previous = list(calls)
    original = copy.deepcopy(full._chunks(full._load(project['id'])))
    repeated = full.analyze(project['id'])
    assert repeated['status'] == 'failed' and calls == previous
    assert full._chunks(full._load(project['id'])) == original
    assert not list((service.store.vault / '01-Wiki').rglob('*.md'))


def test_semantic_pause_and_cas_conflict_preserve_private_checkpoint(full_project, monkeypatch):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch, pause=True)
    result = full.analyze(project['id'])
    assert result['status'] == 'paused' and 'review' not in calls
    paid_source = calls.count('source')
    full.set_paused(project['id'], False)
    resumed = full.analyze(project['id'])
    assert resumed['publication_complete'] and calls.count('source') == paid_source
    assert calls.count('proposal') == calls.count('review') == 1
    state = service.store.config_get('project_analysis:' + project['id'])
    entry = next(value for key, value in state['notes'].items() if key.startswith('topic:'))
    path = service.store.vault / entry['path']
    human = path.read_text() + '\n人工补充：特殊情况由门店复核。\n'
    path.write_text(human)
    conflict = full.analyze(project['id'])
    assert not conflict['publication_complete'] and path.read_text() == human
    assert calls.count('proposal') == calls.count('review') == 1


@pytest.mark.parametrize('pathway', ['resume', 'publish'])
def test_shared_projection_is_checked_on_publication_and_completed_resume(full_project, monkeypatch, pathway):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch, shared=True)
    result = full.analyze(project['id'])
    assert result['publication_complete'] is True
    run = full._load(project['id'])
    documents = run['synthesis']['public_documents']
    assert len(documents) == 2
    assert '并发创建是否严格保持每店5套上限尚未验证。' in documents[0]['body']
    assert '实际业务操作的成功结果尚未验证。' in documents[1]['body']
    common = set(documents[0]['fact_ids']) & set(documents[1]['fact_ids'])
    assert common and all(run['synthesis']['semantic_coverage']['inputs'][fid]['kind'] == 'unknown' for fid in common)
    before = {path: path.read_bytes() for path in service.store.vault.rglob('*.md')}
    count = len(calls)
    with service.store._db() as db:
        db.execute("DELETE FROM business_semantic_tasks WHERE stage='review'")
    if pathway == 'resume':
        rejected = full.analyze(project['id'])
        assert rejected['status'] == 'failed' and rejected['synthesis_failure']['reason_code'] == 'cache_invalid'
    else:
        snapshot = full.project_analysis.snapshot(project['id'])
        with pytest.raises(ToolError):
            full._publish(snapshot, run)
    assert len(calls) == count
    assert {path: path.read_bytes() for path in service.store.vault.rglob('*.md')} == before


def test_real_semantic_pipeline_restores_receipt_gate_after_source_stub(full_project, monkeypatch):
    import businessmap.business_semantics as semantics
    assert semantics.validate_stored_semantic_projection is not REAL_STORED_VALIDATOR
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch)
    first = full.analyze(project['id'])
    assert first['publication_complete'] is True
    paid = len(calls)
    with service.store._db() as db:
        db.execute("DELETE FROM business_semantic_tasks WHERE stage='catalogue'")
    rejected = full.analyze(project['id'])
    assert rejected['status'] == 'failed' and rejected['synthesis_failure']['reason_code'] == 'cache_invalid'
    assert len(calls) == paid


@pytest.mark.parametrize('index', [0, 1, 2], ids=['first', 'middle', 'last'])
@pytest.mark.parametrize('public_claim', [False, True], ids=['private-only', 'unreviewed-public'])
def test_publication_gate_rejects_complete_catalogue_with_unreviewed_packet(full_project, semantic_case, monkeypatch, index, public_claim):
    from businessmap import business_semantics as s
    from test_business_semantics import private_multi_packet_case, omit_accepted_packet
    # Keep the real Store, checkpoint reader, expected identity and publication
    # gate; only the external model is a synthetic provider.
    monkeypatch.setattr(s, 'validate_stored_semantic_projection', REAL_STORED_VALIDATOR)
    full, service, project = full_project
    full.plan(project['id'])
    snapshot = full.project_analysis.snapshot(project['id'])
    run = full._load(project['id'])
    context = {**run, 'semantic_input_hash': s.semantic_input_hash(snapshot, run, full._chunks(run), [])}
    case = (service.store, snapshot, context, semantic_case[3], semantic_case[4])
    setup, projection = private_multi_packet_case(case, local=False)
    s.validate_stored_semantic_projection(projection, snapshot=snapshot,
        expected_input_hash=context['semantic_input_hash'], store=service.store)
    omit_accepted_packet(service.store, projection, index, public_claim=public_claim)
    run['synthesis'] = projection
    paid = len(setup[1])
    before = {path: path.read_bytes() for path in service.store.vault.rglob('*.md')}
    with pytest.raises(s.SemanticValidationError) as error:
        full._publish(snapshot, run)
    assert error.value.reason_code == 'projection_invalid'
    assert len(setup[1]) == paid
    assert {path: path.read_bytes() for path in service.store.vault.rglob('*.md')} == before


def test_completed_http_old_service_footer_resumes_without_paid_calls(full_project, monkeypatch):
    from test_support_graph import legacy_service_projection
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch)
    first = full.analyze(project['id'])
    assert first['publication_complete']
    run = full._load(project['id'])
    legacy = legacy_service_projection(run['synthesis'])
    run['synthesis'] = legacy
    full._save(run)
    paid = len(calls)
    resumed = full.analyze(project['id'])
    assert resumed['publication_complete'] and resumed['status'] == 'completed'
    assert len(calls) == paid
    assert full._load(project['id'])['synthesis']['summary_body'] == legacy['summary_body']
