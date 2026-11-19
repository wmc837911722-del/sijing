"""Whole-root admission through persisted SOURCE and the production controller."""
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
import pytest
from businessmap import business_semantics as s
from test_business_semantics import semantic_case


@pytest.fixture
def whole_semantic_case(tmp_path):
    isolated = tmp_path / 'whole-source'
    isolated.mkdir()
    fixture = semantic_case.__wrapped__(isolated)
    yield next(fixture)
    fixture.close()


@pytest.fixture
def whole_parent_case(whole_semantic_case, request):
    semantic_case = whole_semantic_case
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units
    store, snapshot, original_run, _, _ = semantic_case
    snapshot = deepcopy(snapshot)
    snapshot['provider'] = {'id': 'fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}
    snapshot['project']['name'] = '测试项目' + 'A'*getattr(request, 'param', 15000)
    snapshot.setdefault('repositories', [])
    run = {**original_run, 'project_id': snapshot['project']['id'], 'modules': [{'id':'module'}], 'source_repositories': []}
    chunks = [{'id':'source', 'module_id':'module', 'status':'completed', 'result': {'summary':'',
        'findings':[{'text':('办理业务必须取得门店授权。' if i==0 else '业务处理需核对门店、权限、范围和适用条件；'*3), 'evidence_ids':[]} for i in range(61)], 'unknowns':['普通办理是否需要门店授权及异常时如何恢复尚未核验。','实际运行时适用范围是否一致尚未核验。']}}]
    run['semantic_input_hash'] = s.semantic_input_hash(snapshot, run, chunks, [])
    units = source_fact_units(snapshot, run, chunks)
    synthesis = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    routing = []
    with store._db() as db:
        db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
        db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)', (run['run_id'],run['project_id'],run['snapshot_id'],s._json(run),'fixture'))
        db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)', (run['run_id'],0,s._json(chunks[0])))
        for batch in synthesis._batches(snapshot, units, 0):
            response = {'assignments': [{'fact_id':'f'+str(i),'disposition':'public','category':'flows','title':'办理业务'} for i in range(len(batch))]}
            normalized = synthesis._validate(response,batch)
            key = synthesis.cache_identity(snapshot,run,batch,0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)',(key,key,s._json(normalized),'fixture'))
            routing.extend(normalized['assignments'])
    calls = []
    def complete(system, user, **kwargs):
        from test_business_semantics import partition_review
        kwargs['before_send'](); payload=json.loads(user); calls.append(payload)
        if payload['stage']=='catalogue':
            result={'operations':[{'title':'办理业务','category':'flows','label_ids':[item['label_id'] for item in payload['items']]}]}
        elif payload['stage']=='subdivide':
            ids=[item['label_id'] for item in payload['items']]
            titles=['接收业务','核对业务','处理业务','完成业务','归档业务','查看业务']
            result={'operations':[{'title':titles[i//10], 'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        elif payload['stage']=='partition_review':
            result=partition_review(payload,reject=True)
        else:
            raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=complete))
    runtime=s.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
    runtime._units={unit['_fact_id']:unit for unit in units}
    runtime._forbidden,runtime._matching,runtime._partitions=[],[],[]
    runtime._catalogue_tasks,runtime.plans,runtime._task_bindings=[],[],{}
    runtime._owned_slots,runtime._stopped=set(),threading.Event()
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    root=runtime._catalogue_page(runtime._catalogue_initial_labels(routing))[0]
    assert len(root['fact_ids'])==63 and not runtime._packet_fits([root])
    return SimpleNamespace(store=store, snapshot=snapshot, run=run, units=units, runtime=runtime, parent=root, calls=calls)


def test_fresh_complete_root_reaches_real_strict_review(whole_parent_case):
    from test_business_semantics import independent_candidate, partition_review, review
    c = whole_parent_case
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        c.calls.append(payload)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '处理业务' + str(i // 10), 'category': 'flows', 'label_ids': ids[i:i + 10]} for i in range(0, len(ids), 10)]}
        elif payload['stage'] == 'partition_review':
            result = partition_review(payload, reject=True)
        elif payload['stage'] == 'proposal':
            result = independent_candidate(payload)
        elif payload['stage'] == 'review':
            result = review(payload)
        else:
            raise AssertionError(payload['stage'])
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    packets = c.runtime._packets([c.parent])
    assert packets == [[c.parent]]
    outcome = c.runtime._integrate_packet(packets[0])
    assert outcome['review_hash']
    assert [call['stage'] for call in c.calls[-2:]] == ['proposal', 'review']
    assert len(c.calls[-2]['items']) == 63
    assert c.calls[-2]['budget']['candidate_bytes'] == 40000


def test_whole_projection_is_authenticated_and_replays_without_new_calls(whole_parent_case):
    from test_business_semantics import independent_candidate, review
    c = whole_parent_case
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        c.calls.append(payload)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    # Use the production packet and projection path on the same persisted source.
    projection = c.runtime.build(c.snapshot, c.run, units=c.units,
        routing=[{'fact_id': unit['_fact_id'], 'disposition':'public', 'category':'flows', 'title':'办理业务'} for unit in c.units])
    assert len(projection['semantic_coverage']['whole_operations']) == 1
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    s.validate_current_semantic_projection(projection, snapshot=c.snapshot,
        expected_input_hash=c.run['semantic_input_hash'], task_rows=rows)
    before = len(c.calls)
    replay = c.runtime.build(c.snapshot, c.run, units=c.units,
        routing=[{'fact_id': unit['_fact_id'], 'disposition':'public', 'category':'flows', 'title':'办理业务'} for unit in c.units])
    assert s._projection_hash(replay) == s._projection_hash(projection)
    assert len(c.calls) == before


def test_quality_rejection_uses_prior_and_one_strict_second_review(whole_parent_case):
    from test_business_semantics import _canonical_independent_candidate, positive_graph, review
    c = whole_parent_case
    reviewed = 0
    def complete(system, user, **kwargs):
        nonlocal reviewed
        kwargs['before_send']()
        payload = json.loads(user)
        c.calls.append(payload)
        if payload['stage'] == 'proposal':
            candidate = _canonical_independent_candidate(payload)
            findings = [claim for claim in candidate['claims'] if claim['kind'] != 'unknown']
            combined = {**findings[0], 'text':'办理业务需要核对门店权限及适用条件。',
                'support_ids':[claim['support_ids'][0] for claim in findings]}
            candidate['claims'] = [combined] + [claim for claim in candidate['claims'] if claim['kind'] == 'unknown']
            for decision in candidate['decisions']:
                if decision['input_id'] in combined['support_ids']:
                    decision['parts'][0]['claim_ids'] = [combined['id']]
            result = positive_graph(candidate, payload)
        else:
            reviewed += 1
            result = review(payload, reject=(reviewed == 1))
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    packet = c.runtime._packets([c.parent])[0]
    result = c.runtime._integrate_packet(packet)
    assert result['review_hash']
    assert [p['stage'] for p in c.calls[-4:]] == ['proposal','review','proposal','review']
    assert {'prior_candidate','revision_feedback','preservation'} <= c.calls[-2].keys()


def test_injected_review_overflow_keeps_paid_d1_without_review_slot(whole_parent_case, monkeypatch):
    # Fault injection targets only the computed review measurement. This does
    # not claim the fixed-profile fixture actually exceeds 160,000 bytes.
    from test_business_semantics import independent_candidate, review
    c=whole_parent_case
    actual=[]
    original=s._semantic_request_bytes
    def measured(snapshot, profile, stage, system, payload):
        size=original(snapshot,profile,stage,system,payload)
        if stage=='review' and payload.get('candidate'):
            actual.append(size)
            return 160001
        return size
    monkeypatch.setattr(s,'_semantic_request_bytes',measured)
    def complete(system,user,**kwargs):
        kwargs['before_send']()
        payload=json.loads(user); c.calls.append(payload)
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    packet=c.runtime._packets([c.parent])[0]
    with pytest.raises(s.SemanticValidationError) as failure:
        c.runtime._integrate_packet(packet)
    assert failure.value.reason_code=='input_limit'
    assert actual and actual[0] <= 160000
    with c.store._db() as db:
        before=db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    assert len([r for r in rows if r['stage']=='proposal' and r['status']=='proposed'])==1
    assert not [r for r in rows if r['stage']=='review']
    sent=len(c.calls)
    with pytest.raises(s.SemanticValidationError) as replay:
        c.runtime._integrate_packet(packet)
    assert replay.value.reason_code=='input_limit' and len(c.calls)==sent
    with c.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()==before



from test_full_analysis import full_project
from test_semantic_integration import REAL_SYNTHESIS_BUILD, REAL_STORED_VALIDATOR


@pytest.fixture
def completed_whole_controller(full_project, monkeypatch):
    from businessmap.full_synthesis import FullBusinessSynthesis
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.analysis_addenda import AnalysisAddenda
    from test_business_semantics import independent_candidate, review
    full, service, project = full_project
    full.SOURCE_BYTES = 37000
    monkeypatch.setattr(FullBusinessSynthesis, 'build', REAL_SYNTHESIS_BUILD)
    monkeypatch.setattr(s, 'validate_stored_semantic_projection', REAL_STORED_VALIDATOR)
    provider = service.providers.save({'name':'whole fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'})
    ProjectAnalysis(service).bind_model(project['id'], provider['id'])
    calls=[]; source_counts=[]
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload=json.loads(user)
        stage=payload.get('stage','source' if 'files' in payload else 'routing')
        calls.append(payload)
        if 'review_question' in payload:
            result={'title':'业务补充','summary':'','findings':[{'text':'办理业务需具备门店权限。','evidence_ids':['s1']}],'unknowns':[]}
        elif stage=='source':
            count=source_counts.pop(0); eid=payload['files'][0]['evidence_id']
            result={'title':'办理业务','summary':'','findings':[{'text':f'办理业务条件 {i} 需核对门店授权和范围。','evidence_ids':[eid]} for i in range(count)],
                'unknowns':['授权失败后的恢复方式尚未核验。','门店适用范围尚未核验。'] if not source_counts else []}
        elif stage=='routing':
            result={'assignments':[{'fact_id':item['fact_id'],'disposition':'public','category':'flows','title':'办理业务'} for item in payload['items']]}
        elif stage=='catalogue':
            result={'operations':[{'title':'办理业务','category':'flows','label_ids':[item['label_id'] for item in payload['items']]}]}
        elif stage=='proposal':
            result=independent_candidate(payload)
            result['claims'][0]['support_ids'].append(payload['items'][1]['fact_id'])
            unknown=next(item['fact_id'] for item in payload['items'] if item['kind']=='unknown')
            next(row for row in result['decisions'] if row['input_id']==unknown)['parts'].append(
                {'outcome':'resolved', 'resolved_by_ids':[payload['items'][0]['fact_id']], 'rationale':'授权条件已有依据。'})
        elif stage=='review': result=review(payload)
        else: raise AssertionError(stage)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    monkeypatch.setattr(service.providers,'complete',complete)
    full.plan(project['id']); snapshot=full.project_analysis.snapshot(project['id']); run=full._load(project['id'])
    chunks=full._chunks(run)
    count=len(chunks); source_counts.extend([60//count + (i < 60%count) for i in range(count)])
    for chunk in chunks: full._process(snapshot,run,chunk)
    evidence=[chunk['id']+':'+chunk['segments'][0]['evidence_id'] for chunk in chunks[:2]]
    addenda=AnalysisAddenda(service,guard=lambda:None)
    receipt=addenda.collect(snapshot,run,{'gap_id':'whole-gap','question':'核对业务范围','evidence_ids':evidence,'snapshot_id':run['snapshot_id']})
    attestation={'operation':'办理业务','gap_id':'whole-gap','evidence_ids':evidence,
        'evidence_hashes':{item['evidence_id']:item['content_hash'] for item in receipt['evidence']},
        'conclusion':'static_request_chain','reviewer':'independent_static_review','review_note':'核对范围'*2500}
    addenda.attest_matching(snapshot,run,receipt['addendum_id'],attestation)
    result=full.analyze(project['id'])
    assert result['status']=='completed' and result['publication_complete'] is True, result
    persisted=full._load(project['id']); projection=persisted['synthesis']
    assert len(projection['semantic_coverage']['input_ids'])==63
    assert len(projection['semantic_coverage']['whole_operations'])==1
    assert len([v for v in projection['semantic_coverage']['inputs'].values() if v['kind']=='unknown'])==2
    with service.store._db() as db:
        tasks=s.semantic_checkpoint_rows(db,projection['semantic_input_hash'])
    s.validate_current_semantic_projection(projection,snapshot=snapshot,expected_input_hash=projection['semantic_input_hash'],task_rows=tasks)
    s.validate_stored_semantic_projection(projection,snapshot=snapshot,expected_input_hash=projection['semantic_input_hash'],store=service.store)
    s.semantic_support_context(projection,snapshot=snapshot,expected_input_hash=projection['semantic_input_hash'],read_checkpoint=tasks.read_checkpoint)
    return SimpleNamespace(full=full,service=service,store=service.store,project=project,snapshot=snapshot,
        run={**persisted,'semantic_input_hash':projection['semantic_input_hash']},projection=projection,tasks=tasks,calls=calls)


def test_full_controller_persists_whole_publication(completed_whole_controller):
    c=completed_whole_controller
    assert c.full._load(c.project['id'])['publication_complete'] is True
    assert [call.get('stage') for call in c.calls].count('review') == 1


from test_atomic_parent_budget import completed_atomic_controller


def test_historical_atomic_controller_restores_default_fresh_admission(completed_atomic_controller, monkeypatch, request):
    assert s.BusinessSemanticIntegration.WHOLE_FRESH_ENABLED is True
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'atomic_budget_allowlist', frozenset())
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'partition_repair_allowlist', frozenset())
    fresh = request.getfixturevalue('whole_parent_case')
    packets = fresh.runtime._packets([fresh.parent])
    assert packets == [[fresh.parent]]
    assert fresh.runtime._whole_plans()[0]['parent_operation'] == fresh.parent


def test_quality_d2_over_64k_never_reserves_new_proposal(whole_parent_case):
    from test_business_semantics import independent_candidate, review
    c=whole_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send']()
        payload=json.loads(user); c.calls.append(payload)
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=True)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    packet=c.runtime._packets([c.parent])[0]
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._integrate_packet(packet)
    assert error.value.reason_code=='input_limit'
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    assert len([r for r in rows if r['stage']=='proposal'])==1
    assert len([r for r in rows if r['stage']=='review'])==1


@pytest.mark.parametrize('mutation', ['lineage', 'strip', 'review'])
def test_whole_proof_tampering_is_rejected_even_with_rehashed_projection(completed_whole_controller, mutation):
    c=completed_whole_controller
    projection=deepcopy(c.projection)
    proof=projection['semantic_coverage']['whole_operations'][0]
    if mutation=='lineage': proof['lineage']['root_binding']['result_hash']='0'*64
    elif mutation=='strip': del projection['semantic_coverage']['whole_operations']
    else: proof['review_hash']='0'*64
    projection['verification']['projection_hash']=s._projection_hash(projection)
    with pytest.raises(s.SemanticValidationError):
        s.validate_current_semantic_projection(projection,snapshot=c.snapshot,
            expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)


def test_historical_paid_d1_replay_requires_selection_for_new_review(whole_parent_case, monkeypatch):
    from test_business_semantics import independent_candidate, partition_review, review
    c=whole_parent_case
    labels=[{'title':c.parent['title'],'category':c.parent['category'],'fact_ids':[fid],
        'aliases':c.parent['aliases']} for fid in c.parent['fact_ids']]
    def initial(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user)
        if payload['stage']=='subdivide':
            ids=[item['label_id'] for item in payload['items']]
            result={'operations':[{'title':'处理业务'+str(i//10),'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        else: result=partition_review(payload,reject=True)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=initial
    children=c.runtime._catalogue_page(labels,stage='subdivide'); sub=c.runtime._catalogue_tasks[-1]
    with pytest.raises(s.SemanticValidationError) as rejected:
        c.runtime._verify_partition(c.parent,children,sub)
    c.runtime.whole_operation_allowlist={(sub,rejected.value.stage_hash)}
    sent=[]
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); sent.append(payload['stage'])
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    packet=c.runtime._packets([c.parent])[0]
    original=c.runtime._request_bytes
    def interrupt(stage,system,payload):
        if stage=='review' and payload.get('candidate'): raise RuntimeError('simulated crash before review')
        return original(stage,system,payload)
    with monkeypatch.context() as patch:
        patch.setattr(c.runtime,'_request_bytes',interrupt)
        with pytest.raises(RuntimeError,match='simulated crash'):
            c.runtime._integrate_packet(packet)
    assert sent==['proposal']
    c.runtime.whole_operation_allowlist.clear()
    with pytest.raises(s.SemanticValidationError):
        c.runtime._integrate_packet(packet)
    assert sent==['proposal']
    c.runtime.whole_operation_allowlist={(sub,rejected.value.stage_hash)}
    accepted=c.runtime._integrate_packet(packet)
    assert accepted['review_hash'] and sent==['proposal','review']
    c.runtime.whole_operation_allowlist.clear()
    replay=c.runtime._integrate_packet(packet)
    assert replay==accepted and sent==['proposal','review']


def test_two_rejected_reviews_never_create_third_draft(whole_parent_case):
    from test_business_semantics import _canonical_independent_candidate, positive_graph, review
    c=whole_parent_case; sent=[]
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); sent.append(payload['stage'])
        if payload['stage']=='review':
            result=review(payload,reject=True)
        else:
            candidate=_canonical_independent_candidate(payload)
            findings=[claim for claim in candidate['claims'] if claim['kind']!='unknown']
            combined={**findings[0],'text':'办理业务需要核对门店权限及适用条件。',
                'support_ids':[claim['support_ids'][0] for claim in findings]}
            candidate['claims']=[combined]+[claim for claim in candidate['claims'] if claim['kind']=='unknown']
            for decision in candidate['decisions']:
                if decision['input_id'] in combined['support_ids']:
                    decision['parts'][0]['claim_ids']=[combined['id']]
            result=positive_graph(candidate,payload)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError) as failure:
        c.runtime._integrate_packet(c.runtime._packets([c.parent])[0])
    assert failure.value.reason_code=='review_rejected'
    assert sent==['proposal','review','proposal','review']
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    assert max(row['attempt'] for row in rows)==2


def _observe_prepared_packet_sends(runtime, monkeypatch):
    from unittest.mock import patch
    from test_business_semantics import independent_candidate, review

    sent = []
    plans_for_call = None
    original_call = runtime._call

    def observe_call(*args, **kwargs):
        nonlocal plans_for_call
        plans_for_call = {}
        try:
            return original_call(*args, **kwargs)
        finally:
            plans_for_call = None

    monkeypatch.setattr(runtime, '_call', observe_call)
    for kind, name in (('atomic_budget', '_atomic_plans'), ('whole_operation', '_whole_plans')):
        planner = getattr(runtime, name)

        def observe_plans(_kind=kind, _planner=planner):
            assert plans_for_call is not None, 'prepared packet selection rebuilt a plan'
            assert _kind not in plans_for_call, 'admission rebuilt the same plan twice'
            plans_for_call[_kind] = _planner()
            return plans_for_call[_kind]

        monkeypatch.setattr(runtime, name, observe_plans)

    def complete(system, user, **kwargs):
        payload = json.loads(user)
        before_send = kwargs['before_send']
        assert set(plans_for_call) == {'atomic_budget', 'whole_operation'}.intersection(payload)
        owned = before_send._semantic_owned_snapshot()
        for kind, plans in plans_for_call.items():
            matches = [plan for plan in plans if plan[kind] == payload[kind]]
            assert len(matches) == 1
            assert matches[0][payload['stage'] + '_hash'] == owned['stage_hash']
        with patch.object(runtime, '_read_tasks', wraps=runtime._read_tasks) as capture, \
             patch.object(runtime, '_current_inventory', wraps=runtime._current_inventory) as inventory:
            before_send()
        capture.assert_called_once_with()
        inventory.assert_called_once_with()
        sent.append(payload)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}

    monkeypatch.setattr(runtime.service.providers, 'complete', complete)
    return sent


def test_packet_policy_reuses_prepared_selection_but_reauthenticates_sends(whole_parent_case, monkeypatch):
    c=whole_parent_case; runtime=c.runtime
    packets=runtime._packets([c.parent])
    original_plan=runtime._whole_plans()[0]
    sent = _observe_prepared_packet_sends(runtime, monkeypatch)
    outcome=runtime._integrate_packet(packets[0])
    assert outcome['review_hash']
    assert [payload['stage'] for payload in sent] == ['proposal', 'review']
    assert sent[0]==original_plan['proposal_request']


@pytest.fixture
def mixed_source_case(whole_semantic_case):
    semantic_case=whole_semantic_case
    """Persisted synthetic SOURCE: 52 atomic, 63 whole and one ordinary fact."""
    from copy import deepcopy
    import threading
    from businessmap import business_semantics as sem
    from businessmap.full_synthesis import FullBusinessSynthesis,source_fact_units
    from test_business_semantics import partition_review
    store,snapshot,original_run,_,_=semantic_case
    snapshot=deepcopy(snapshot);snapshot['provider']={'id':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'}
    snapshot['project']['name']='并发测试'+'A'*15000;snapshot.setdefault('repositories',[])
    run={**original_run,'project_id':snapshot['project']['id'],'modules':[{'id':'module'}],'source_repositories':[]}
    chunks=[{'id':'source','module_id':'module','status':'completed','result':{'summary':'','findings':[
        {'text':'业务处理需核对门店、权限、范围和适用条件；'*3,'evidence_ids':[]} for _ in range(116)],'unknowns':[]}}]
    run['semantic_input_hash']=sem.semantic_input_hash(snapshot,run,chunks,[])
    units=source_fact_units(snapshot,run,chunks);positions={unit['_fact_id']:i for i,unit in enumerate(units)}
    synthesis=FullBusinessSynthesis.__new__(FullBusinessSynthesis);routing=[]
    with store._db() as db:
        db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
        db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',(run['run_id'],run['project_id'],run['snapshot_id'],sem._json(run),'fixture'))
        db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)',(run['run_id'],0,sem._json(chunks[0])))
        for batch in synthesis._batches(snapshot,units,0):
            response={'assignments':[{'fact_id':'f'+str(i),'disposition':'public','category':'flows',
                'title':('父业务A' if positions[unit['_fact_id']]<52 else '父业务B' if positions[unit['_fact_id']]<115 else '普通业务')} for i,unit in enumerate(batch)]}
            normalized=synthesis._validate(response,batch);key=synthesis.cache_identity(snapshot,run,batch,0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)',(key,key,sem._json(normalized),'fixture'));routing.extend(normalized['assignments'])
    calls=[]
    def historical(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        if payload['stage']=='catalogue':
            result={'operations':[{'title':item['title'],'category':'flows','label_ids':[item['label_id']]} for item in payload['items']]}
        elif payload['stage']=='subdivide':
            ids=[item['label_id'] for item in payload['items']]
            result={'operations':[{'title':['接收业务','核对业务','处理业务','完成业务','归档业务','查看业务'][i//10],'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        else:result=partition_review(payload,reject=payload['parent']['title']=='父业务A')
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=historical))
    runtime=sem.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
    runtime._units={unit['_fact_id']:unit for unit in units};runtime._forbidden,runtime._matching,runtime._partitions=[],[],[]
    runtime._catalogue_tasks,runtime.plans,runtime._task_bindings=[],[],{}
    runtime._owned_slots,runtime._stopped=set(),threading.Event();runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    roots=runtime._catalogue_page(runtime._catalogue_initial_labels(routing));selected=next(root for root in roots if root['title']=='父业务A');other=next(root for root in roots if root['title']=='父业务B')
    def labels(root):return [{'title':root['title'],'category':root['category'],'fact_ids':[fid],'aliases':root['aliases']} for fid in root['fact_ids']]
    children=runtime._catalogue_page(labels(selected),stage='subdivide');d1=runtime._catalogue_tasks[-1]
    with pytest.raises(sem.SemanticValidationError) as first:runtime._verify_partition(selected,children,d1)
    r1=first.value.stage_hash;runtime.partition_repair_allowlist={(d1,r1)}
    with pytest.raises(sem.SemanticValidationError) as second:runtime._packets([selected])
    r2=second.value.stage_hash
    with store._db() as db:
        tasks=sem.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        d2=next(row['stage_hash'] for row in tasks if row['stage']=='subdivide' and row['data']['context'].get('partition_revision'))
    four=(d1,r1,d2,r2);runtime.partition_repair_allowlist.clear();runtime.atomic_budget_allowlist={four}
    return SimpleNamespace(store=store,snapshot=snapshot,run=run,runtime=runtime,four_keys=four,roots=roots,calls=calls)


def test_mixed_persisted_packets_reuse_selection_and_keep_fresh_send_checks(mixed_source_case, monkeypatch):
    c=mixed_source_case; runtime=c.runtime
    packets=runtime._packets(c.roots)
    assert len(packets)==3
    atomic=runtime._atomic_plans()[0]; whole=runtime._whole_plans()[0]
    expected={'父业务A':atomic['proposal_request'],'父业务B':whole['proposal_request']}
    sent = _observe_prepared_packet_sends(runtime, monkeypatch)
    for packet in packets:
        runtime._integrate_packet(packet)
    assert [payload['stage'] for payload in sent] == ['proposal', 'review'] * len(packets)
    proposals=[p for p in sent if p['stage']=='proposal']
    assert len(proposals)==3
    for proposal in proposals:
        title=proposal['operations'][0]['title']
        if title in expected: assert proposal==expected[title]
        else: assert 'atomic_budget' not in proposal and 'whole_operation' not in proposal
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    proposed={row['stage_hash']:row for row in rows if row['stage']=='proposal'}
    assert proposed[atomic['proposal_hash']]['data']['request']==atomic['proposal_request']
    assert proposed[whole['proposal_hash']]['data']['request']==whole['proposal_request']


@pytest.mark.parametrize('mutation', ['snapshot','source','root'])
def test_prepared_packet_policy_rejects_mutated_binding(whole_parent_case, mutation):
    c=whole_parent_case; packet=c.runtime._packets([c.parent])[0]
    if mutation=='snapshot': c.runtime._snapshot['project']['name'] += '变化'
    elif mutation=='source': c.runtime._units[c.parent['fact_ids'][0]]['text'] += '变化'
    else: packet[0]['title'] += '变化'
    before=len(c.calls)
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packet)
    assert len(c.calls)==before


def test_prepared_selection_does_not_hide_persisted_source_changes(whole_parent_case):
    c=whole_parent_case; packet=c.runtime._packets([c.parent])[0]
    with c.store._db() as db:
        raw=db.execute('SELECT data FROM full_analysis_chunks WHERE run_id=?',(c.run['run_id'],)).fetchone()[0]
        source=json.loads(raw); source['result']['findings'][0]['text']+='篡改'
        db.execute('UPDATE full_analysis_chunks SET data=? WHERE run_id=?',(s._json(source),c.run['run_id']))
    before=len(c.calls)
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packet)
    assert len(c.calls)==before


def test_new_build_discards_previous_packet_policy_before_guard(whole_parent_case):
    c=whole_parent_case
    c.runtime._packets([c.parent])
    assert c.runtime._packet_policy is not None
    def stop(): raise RuntimeError('build stopped')
    c.runtime.guard=stop
    with pytest.raises(RuntimeError,match='build stopped'):
        c.runtime.build(c.snapshot,c.run,units=c.units,routing=[])
    assert c.runtime._packet_policy is None
