import copy
import hashlib
import json

import pytest

from businessmap.providers import ToolError
from test_full_analysis import full_project, model


@pytest.fixture
def prepared_source(full_project, monkeypatch):
    full, service, project = full_project
    snapshot = full.project_analysis.snapshot(project['id'])
    full.plan(project['id'])
    run = full._load(project['id'])
    model(monkeypatch, service)
    for chunk in full._chunks(run):
        full._process(snapshot, run, chunk)
    chunks = full._chunks(run)
    evidence = [chunk['id'] + ':' + chunk['segments'][0]['evidence_id'] for chunk in chunks[:2]]
    request = {'gap_id': 'creation-initial-state', 'question': '确认创建条件、初始状态与尚未核验事项。',
               'evidence_ids': evidence, 'snapshot_id': run['snapshot_id']}
    return full, service, snapshot, run, chunks, request


def addendum_model(monkeypatch, service, *, malformed=False, callback=None):
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        calls.append((system, payload, kwargs['max_output']))
        if callback:
            callback()
        result = {'title': '创建条件', 'summary': '创建需符合门店条件。',
                  'findings': [{'text': '初始没有明细项。', 'evidence_ids': ['s1']}],
                  'unknowns': ['并发创建是否仍受限制尚待运行核验。'],
                  'matching': {'verified': True, 'scope': 'matched_operation'}}
        return {'text': 'RAW-PRIVATE' if malformed else json.dumps(result), 'usage': {'input_tokens': 11, 'output_tokens': 13}}
    monkeypatch.setattr(service.providers, 'complete', complete)
    return calls


def test_addendum_uses_exact_evidence_and_unchanged_system(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    from businessmap.full_analysis import FullProjectAnalysis
    full, service, snapshot, run, chunks, request = prepared_source
    calls = addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: full._send_guard(snapshot, run))
    receipt = addenda.collect(snapshot, run, request)
    assert calls[0][0] == FullProjectAnalysis.SYSTEM and calls[0][2] == 2800
    assert calls[0][1]['review_question'] == request['question']
    assert len(calls[0][1]['files']) == len(request['evidence_ids'])
    assert receipt['status'] == 'accepted' and [e['evidence_id'] for e in receipt['evidence']] == request['evidence_ids']
    assert receipt['result']['findings'][0]['evidence_ids'] == [request['evidence_ids'][0]]
    assert receipt['result']['unknowns'] == ['并发创建是否仍受限制尚待运行核验。']
    assert not receipt.get('matching_receipts')
    assert addenda.collect(snapshot, run, request) == receipt and len(calls) == 1
    assert addenda.accepted(snapshot, run) == [receipt]


def test_addendum_does_not_modify_original_chunks(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    with service.store._db() as db:
        before = list(db.execute('SELECT id,data FROM full_analysis_chunks ORDER BY id'))
    calls = addendum_model(monkeypatch, service)
    AnalysisAddenda(service, guard=lambda: None).collect(snapshot, run, request)
    with service.store._db() as db:
        assert list(db.execute('SELECT id,data FROM full_analysis_chunks ORDER BY id')) == before
    assert full._chunks(run) == chunks and len(calls) == 1
    assert full.VERSION == 1


def test_addendum_unknown_evidence_or_destination_change_is_rejected(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    calls = addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    with pytest.raises(ToolError):
        addenda.collect(snapshot, run, {**request, 'evidence_ids': ['invented']})
    assert not calls
    receipt = addenda.collect(snapshot, run, request)
    changed = copy.deepcopy(snapshot)
    changed['provider']['model'] = 'another-model'
    with pytest.raises(ToolError):
        addenda.accepted(changed, run)
    assert addenda.accepted(snapshot, run) == [receipt]


def test_addendum_rejected_response_is_not_retried_or_logged(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    calls = addendum_model(monkeypatch, service, malformed=True)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    for _ in range(2):
        with pytest.raises(ToolError):
            addenda.collect(snapshot, run, request)
    assert len(calls) == 1 and addenda.accepted(snapshot, run) == []
    with service.store._db() as db:
        stored = db.execute('SELECT data FROM analysis_addenda').fetchone()[0]
    assert 'RAW-PRIVATE' not in stored and 'needs_review' in stored


def test_accepted_addendum_reuse_does_not_reread_source(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    calls = addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    receipt = addenda.collect(snapshot, run, request)
    monkeypatch.setattr(addenda.full, '_payload', lambda *args: pytest.fail('accepted source was reread'))
    assert addenda.collect(snapshot, run, request) == receipt and len(calls) == 1


def test_addendum_admission_is_durable_before_provider_dispatch(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    def check_admission():
        with service.store._db() as db:
            row = db.execute('SELECT data FROM analysis_addenda').fetchone()
        assert row is not None and json.loads(row[0])['status'] == 'planned'
    calls = addendum_model(monkeypatch, service, callback=check_admission)
    receipt = AnalysisAddenda(service, guard=lambda: None).collect(snapshot, run, request)
    assert receipt['status'] == 'accepted' and len(calls) == 1


def test_addendum_bare_evidence_and_wire_overflow_dispatch_nothing(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    calls = addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    for invalid in ({**request, 'evidence_ids': [chunks[0]['segments'][0]['evidence_id']]},
                    {**request, 'question': '补充问题' * 20000}):
        with pytest.raises(ToolError):
            addenda.collect(snapshot, run, invalid)
    assert not calls


def test_matching_requires_explicit_independent_attestation(prepared_source, monkeypatch):
    from businessmap.analysis_addenda import AnalysisAddenda
    full, service, snapshot, run, chunks, request = prepared_source
    addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    receipt = addenda.collect(snapshot, run, request)
    attestation = {'operation': '普通创建', 'gap_id': request['gap_id'], 'evidence_ids': request['evidence_ids'],
                   'evidence_hashes': {e['evidence_id']: e['content_hash'] for e in receipt['evidence']},
                   'conclusion': 'static_request_chain', 'reviewer': 'independent_static_review', 'review_note': '已独立核对请求与接收条件。'}
    with pytest.raises(ToolError):
        addenda.attest_matching(snapshot, run, receipt['addendum_id'], {**attestation, 'evidence_hashes': {}})
    match = addenda.attest_matching(snapshot, run, receipt['addendum_id'], attestation)
    assert match['snapshot_id'] == run['snapshot_id'] and match['input_hash'] == receipt['input_hash']
    assert addenda.attest_matching(snapshot, run, receipt['addendum_id'], attestation) == match
    assert addenda.accepted(snapshot, run)[0]['matching_receipts'] == [match]
    with service.store._db() as db:
        raw = json.loads(db.execute('SELECT data FROM analysis_matching_receipts').fetchone()[0])
        raw.pop('source_repositories')
        db.execute('UPDATE analysis_matching_receipts SET data=?', (json.dumps(raw),))
    with pytest.raises(ToolError):
        addenda.accepted(snapshot, run)


def test_source_normalization_is_pure_and_preserves_existing_result_bytes():
    from businessmap.full_analysis import _normalize_source_response
    response = {'text': json.dumps({'title': '规则', 'summary': '业务条件。', 'findings': [
        {'text': '每店最多5套。', 'evidence_ids': ['s2', 's2', 'e-one']}], 'unknowns': ['并发未知。'], 'extra': {'kept': True}})}
    before = copy.deepcopy(response)
    result = _normalize_source_response(response, [{'evidence_id': 'e-one'}, {'evidence_id': 'e-two'}])
    assert result == {'title': '规则', 'summary': '业务条件。', 'findings': [
        {'text': '每店最多5套。', 'evidence_ids': ['e-two', 'e-one']}], 'unknowns': ['并发未知。'], 'extra': {'kept': True}}
    assert response == before
