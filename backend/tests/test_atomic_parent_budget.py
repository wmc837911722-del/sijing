"""Atomic parent budget over authenticated source and retired partition history."""
import json
import pytest
from businessmap import business_semantics as s
from businessmap.providers import ToolError
from test_business_semantics import semantic_case, oversized_case, execute


def test_raw_checkpoint_reader_and_frozen_snapshot_preserve_exact_sql_row(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
        key = next(row['stage_hash'] for row in rows if row['stage'] == 'partition_review')
        expected = db.execute('SELECT stage_hash,stage,input_hash,status,attempt,data,error_code,created_at FROM business_semantic_tasks WHERE stage_hash=?', (key,)).fetchone()
        live = s.semantic_checkpoint_reader(db)
        assert live.raw_checkpoint_row(key) == expected
        assert rows.read_checkpoint.raw_checkpoint_row(key) == expected
        assert json.loads(expected[5]) == rows.read_checkpoint(key)['data']
        assert expected[6] == 'review_rejected'
    with semantic_case[0]._db() as db:
        db.execute('UPDATE business_semantic_tasks SET error_code=? WHERE stage_hash=?', ('model_call', key))
        assert s.semantic_checkpoint_reader(db).raw_checkpoint_row(key)[6] == 'model_call'
    assert rows.read_checkpoint.raw_checkpoint_row(key) == expected
    with pytest.raises(s.SemanticValidationError):
        rows.read_checkpoint.raw_checkpoint_row('0'*64)


from copy import deepcopy
import importlib
import threading
from types import SimpleNamespace


@pytest.fixture
def atomic_parent_case(semantic_case):
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units
    store, snapshot, original_run, _, _ = semantic_case
    snapshot = deepcopy(snapshot)
    snapshot['provider'] = {'id': 'fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}
    snapshot['project']['name'] = '测试项目' + 'A'*15000
    snapshot.setdefault('repositories', [])
    run = {**original_run, 'project_id': snapshot['project']['id'], 'modules': [{'id':'module'}], 'source_repositories': []}
    chunks = [{'id':'source', 'module_id':'module', 'status':'completed', 'result': {'summary':'',
        'findings':[{'text':('办理业务必须取得门店授权。' if i==0 else '业务处理需核对门店、权限、范围和适用条件；'*3), 'evidence_ids':[]} for i in range(50)], 'unknowns':['普通办理是否需要门店授权及异常时如何恢复尚未核验。','实际运行时适用范围是否一致尚未核验。']}}]
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
    assert len(root['fact_ids'])==52 and not runtime._packet_fits([root])
    labels=[{'title':root['title'],'category':root['category'],'fact_ids':[fid],'aliases':root['aliases']} for fid in root['fact_ids']]
    children=runtime._catalogue_page(labels,stage='subdivide'); d1=runtime._catalogue_tasks[-1]
    with pytest.raises(s.SemanticValidationError) as first:
        runtime._verify_partition(root,children,d1)
    assert first.value.reason_code=='review_rejected'; r1=first.value.stage_hash
    runtime.partition_repair_allowlist={(d1,r1)}
    with pytest.raises(s.SemanticValidationError) as second:
        runtime._packets([root])
    assert second.value.reason_code=='review_rejected'; r2=second.value.stage_hash
    with store._db() as db:
        tasks=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        d2=next(row['stage_hash'] for row in tasks if row['stage']=='subdivide' and row['data']['context'].get('partition_revision'))
        old_rows=db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
    return SimpleNamespace(store=store,service=service,snapshot=snapshot,run=run,units=units,routing=routing,
        calls=calls,runtime=runtime,parent=root,tasks=tasks,old_rows=old_rows,four_keys=(d1,r1,d2,r2))


def test_atomic_plan_authenticates_source_four_history_and_selects_full_parent(atomic_parent_case):
    c=atomic_parent_case
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    assert plan['status']=='planned' and plan['parent_operation']==c.parent
    assert plan['candidate_estimate']<=plan['candidate_bytes']<=40000
    assert plan['review_base_bytes']+3*plan['candidate_bytes']+1024<=160000
    larger=deepcopy(plan['proposal_request']); larger['budget']['candidate_bytes']+=1
    larger['atomic_budget']['candidate_bytes']+=1
    profile=c.tasks.read_checkpoint(c.four_keys[0])['data']['context']['budget_profile']
    preview={**larger,'stage':'review','candidate':{},'proposal_hash':'0'*64,'packet_hash':'0'*64}
    larger_proposal=s._semantic_request_bytes(c.snapshot,profile,'proposal','',larger)
    larger_review=s._semantic_request_bytes(c.snapshot,profile,'review','',preview)
    assert (larger['budget']['candidate_bytes']>profile['candidate_bytes'] or larger_proposal>profile['stages']['proposal']['input_bytes']
            or larger_review+3*larger['budget']['candidate_bytes']+1024>profile['stages']['review']['input_bytes'])
    assert plan['review_request'] is None and plan['review_hash'] is None
    assert set(c.four_keys)<=set(plan['required_task_hashes'])
    assert plan['remaining_maximum_calls']==4


def test_atomic_runtime_allowlist_is_empty_and_copied_from_scoped_class(semantic_case, monkeypatch):
    pair=('a'*64,'b'*64,'c'*64,'d'*64)
    monkeypatch.setattr(s.BusinessSemanticIntegration,'atomic_budget_allowlist',{pair},raising=False)
    runtime=s.BusinessSemanticIntegration(SimpleNamespace(store=semantic_case[0]),guard=lambda:None)
    assert runtime.atomic_budget_allowlist=={pair}
    runtime.atomic_budget_allowlist.clear()
    assert s.BusinessSemanticIntegration.atomic_budget_allowlist=={pair}


def test_selected_atomic_parent_uses_strict_review_and_retains_four_rows(atomic_parent_case):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal': result=independent_candidate(payload)
        elif payload['stage']=='review': result=review(payload)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear()
    c.runtime.atomic_budget_allowlist={c.four_keys}
    before=len(c.calls)
    packets=c.runtime._packets([c.parent])
    assert len(packets)==1 and packets[0]==[c.parent]
    result=c.runtime._integrate_packet(packets[0])
    assert result['review_hash']
    assert [payload['stage'] for payload in c.calls[before:]]==['proposal','review']
    proposal=c.calls[before]
    assert len(proposal['items'])==52 and len(proposal['operations'])==1
    assert proposal['budget']['candidate_bytes']==proposal['atomic_budget']['candidate_bytes']<40000
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        for row in c.old_rows:
            assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(row[0],)).fetchone()==row
    p=next(row for row in rows if row['stage']=='proposal' and row['status']=='proposed')
    assert p['data']['atomic_raw_graph_hash']==s._hash(p['data']['atomic_raw_graph'])
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    assert plan['status']=='accepted' and plan['review_hash']==result['review_hash']


@pytest.fixture
def completed_atomic_parent(atomic_parent_case):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal':
            result=independent_candidate(payload)
            result['claims'][0]['support_ids'].append(payload['items'][1]['fact_id'])
            mixed=next(item['fact_id'] for item in payload['items'] if item['kind']=='unknown')
            next(claim for claim in result['claims'] if mixed in claim['support_ids'])['text']='异常时门店权限如何恢复尚未核验。'
            next(decision for decision in result['decisions'] if decision['input_id']==mixed)['parts'].append(
                {'outcome':'resolved','resolved_by_ids':[payload['items'][0]['fact_id']],'rationale':'普通办理授权已有明确依据。'})
        elif payload['stage']=='review': result=review(payload)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear()
    c.runtime.atomic_budget_allowlist={c.four_keys}
    paid=len(c.calls)
    c.projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    c.new_calls=deepcopy(c.calls[paid:])
    with c.store._db() as db:
        c.tasks=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    return c


def test_atomic_projection_current_stored_support_and_free_replay(completed_atomic_parent):
    c=completed_atomic_parent
    assert c.projection['complete'] and len(c.projection['semantic_coverage']['atomic_budgets'])==1
    assert c.projection['semantic_coverage']['partitions']==[]
    coverage=c.projection['semantic_coverage']
    first_two=set(coverage['input_ids'][:2])
    assert any(first_two <= set(claim['support_ids']) for claim in coverage['claims'])
    unknowns={fid for fid,value in coverage['inputs'].items() if value['kind']=='unknown'}
    assert len(unknowns)==2 and all(any(fid in claim['support_ids'] and claim['kind']=='unknown' for claim in coverage['claims']) for fid in unknowns)
    assert any({'resolved','retained'} <= {part['outcome'] for part in coverage['decisions'][fid]['parts']} for fid in unknowns)
    s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)
    s.validate_stored_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    s.semantic_support_context(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    before=len(c.calls)
    c.runtime.atomic_budget_allowlist.clear()
    replay=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert s._projection_hash(replay)==s._projection_hash(c.projection)
    assert len(c.calls)==before


def test_second_semantic_draft_accepted_when_actual_wires_fit(atomic_parent_case):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    reviews=[]
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal': result=independent_candidate(payload)
        elif payload['stage']=='review':
            reviews.append(payload)
            result=review(payload,reject=len(reviews)==1)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packets=c.runtime._packets([c.parent]); before=len(c.calls)
    result=c.runtime._integrate_packet(packets[0])
    assert [payload['stage'] for payload in c.calls[before:]]==['proposal','review','proposal','review']
    assert 'revision_feedback' in c.calls[before+2] and 'revision_feedback' not in c.calls[before+3]
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    atomic=[row for row in rows if row['data']['context'].get('atomic_budget')]
    assert sorted((row['stage'],row['attempt'],row['status']) for row in atomic)==[('proposal',1,'proposed'),('proposal',2,'proposed'),('review',1,'needs_review'),('review',2,'accepted')]
    assert result['review_hash']==next(row['stage_hash'] for row in atomic if row['stage']=='review' and row['attempt']==2)


@pytest.mark.parametrize('attack', ['source','review_error','created_at','raw_json','raw_graph','atomic_error','proof_cap','proof_review'])
def test_atomic_current_and_support_reject_history_raw_and_proof_tampering(completed_atomic_parent, attack):
    c=completed_atomic_parent
    projection=deepcopy(c.projection)
    proof=projection['semantic_coverage']['atomic_budgets'][0]
    with c.store._db() as db:
        if attack=='source':
            db.execute('DELETE FROM full_analysis_chunks')
        elif attack=='review_error':
            db.execute('UPDATE business_semantic_tasks SET error_code=? WHERE stage_hash=?',('model_call',c.four_keys[3]))
        elif attack=='created_at':
            db.execute('UPDATE business_semantic_tasks SET created_at=? WHERE stage_hash=?',('tampered',c.four_keys[3]))
        elif attack=='raw_json':
            old=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(c.four_keys[3],)).fetchone()[0]
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(json.dumps(json.loads(old),ensure_ascii=False,indent=1),c.four_keys[3]))
        elif attack=='atomic_error':
            db.execute('UPDATE business_semantic_tasks SET error_code=? WHERE stage_hash=?',('model_call',proof['review_hash']))
        elif attack=='raw_graph':
            key=next(row['stage_hash'] for row in c.tasks if row['stage']=='proposal' and row['data']['context'].get('atomic_budget'))
            data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()[0])
            data['atomic_raw_graph']['claims'][0]['text']='伪造业务'
            data['atomic_raw_graph_hash']=s._hash(data['atomic_raw_graph'])
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(s._json(data),key))
        elif attack=='proof_cap':
            proof['candidate_bytes']-=1
            projection['verification']['projection_hash']=s._projection_hash(projection)
        else:
            proof['review_hash']='0'*64
            projection['verification']['projection_hash']=s._projection_hash(projection)
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    with pytest.raises(s.SemanticValidationError):
        s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    with pytest.raises(s.SemanticValidationError):
        s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)


def test_second_draft_projection_replays_without_new_calls(atomic_parent_case):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    reviews=[]
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal': result=independent_candidate(payload)
        elif payload['stage']=='review':
            reviews.append(payload); result=review(payload,reject=len(reviews)==1)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert projection['semantic_coverage']['atomic_budgets'][0]['review_hash']
    before=len(c.calls); c.runtime.atomic_budget_allowlist.clear()
    replay=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert s._projection_hash(replay)==s._projection_hash(projection) and len(c.calls)==before


@pytest.mark.parametrize('failure', ['raw_limit','privacy','shape'])
def test_invalid_atomic_graph_is_not_retained_in_private_row(atomic_parent_case, failure):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case; c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    _,refs,ops=c.runtime._packet([c.parent]); cap=plan['candidate_bytes']
    if failure=='raw_limit':
        graph={'operations':[],'claims':[],'decisions':[],'padding':''}
        graph['padding']='a'*(cap+1-len(s._json(graph).encode()))
        assert len(s._json(graph).encode())==cap+1
    elif failure=='privacy':
        graph=independent_candidate(plan['proposal_request']); graph['claims'][0]['text']='/Users/private/secret.py'
    else:
        graph={'operations':[],'claims':[],'decisions':[],'unexpected':'bad'}
    def complete(system,user,**kwargs):
        kwargs['before_send']()
        return {'text':json.dumps(graph,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError):
        c.runtime._call('proposal',s.GRAPH_PROPOSAL_SYSTEM,plan['proposal_request'],
            lambda value:s.decode_support_graph(value,refs,ops,candidate_bytes=cap),context=plan['proposal_context'])
    with c.store._db() as db:
        row=s.semantic_checkpoint_reader(db)(plan['proposal_hash'])
    assert row['status']=='needs_review'
    assert 'atomic_raw_graph' not in row['data'] and 'atomic_raw_graph_hash' not in row['data']
    assert '/Users/private/secret.py' not in s._json(row['data'])


def test_atomic_candidate_wire_and_canonical_fit_derived_cap(atomic_parent_case):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case; module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    graph=independent_candidate(plan['proposal_request']); _,refs,ops=c.runtime._packet([c.parent])
    canonical=s.decode_support_graph(graph,refs,ops,candidate_bytes=plan['candidate_bytes'])
    print('ATOMIC_SIZE',plan['candidate_bytes'],plan['proposal_bytes'],len(s._json(graph).encode()),len(s._json(canonical).encode()))
    assert len(s._json(graph).encode())<=plan['candidate_bytes']
    assert len(s._json(canonical).encode())<=plan['candidate_bytes']


def test_second_proposal_that_exceeds_input_limit_never_claims_slot(atomic_parent_case):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal':
            result=independent_candidate(payload)
            result['claims'][0]['text'] += '条件'*1200
        elif payload['stage']=='review': result=review(payload,reject=True)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packets=c.runtime._packets([c.parent]); before=len(c.calls)
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._integrate_packet(packets[0])
    assert error.value.reason_code=='input_limit'
    assert [payload['stage'] for payload in c.calls[before:]]==['proposal','review']
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    atomic=[row for row in rows if row['data']['context'].get('atomic_budget')]
    assert sorted((row['stage'],row['attempt'],row['status']) for row in atomic)==[('proposal',1,'proposed'),('review',1,'needs_review')]


def test_canonical_expansion_over_cap_fails_without_retaining_raw_graph(atomic_parent_case):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case; c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    graph=independent_candidate(plan['proposal_request']); entry=deepcopy(graph['claims'][0]); cap=plan['candidate_bytes']
    while True:
        graph['claims'].append(deepcopy(entry))
        if len(s._json(graph).encode())>cap:
            graph['claims'].pop(); break
    assert len(s._json(graph).encode())<=cap
    _,refs,ops=c.runtime._packet([c.parent])
    def complete(system,user,**kwargs):
        kwargs['before_send'](); return {'text':json.dumps(graph,ensure_ascii=False),'usage':{}}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._call('proposal',s.GRAPH_PROPOSAL_SYSTEM,plan['proposal_request'],
            lambda value:s.decode_support_graph(value,refs,ops,candidate_bytes=cap),context=plan['proposal_context'])
    assert error.value.reason_code=='candidate_limit'
    with c.store._db() as db:
        row=s.semantic_checkpoint_reader(db)(plan['proposal_hash'])
    assert row['status']=='needs_review' and 'atomic_raw_graph' not in row['data']


def test_mutating_decoder_cannot_persist_a_mismatched_raw_graph(atomic_parent_case):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case; c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    graph=independent_candidate(plan['proposal_request']); _,refs,ops=c.runtime._packet([c.parent]); cap=plan['candidate_bytes']
    def complete(system,user,**kwargs):
        kwargs['before_send'](); return {'text':json.dumps(graph,ensure_ascii=False),'usage':{}}
    c.runtime.service.providers.complete=complete
    def mutating_decoder(value):
        value['claims'][0]['text']='已变更业务条件。'
        return s.decode_support_graph(value,refs,ops,candidate_bytes=cap)
    with pytest.raises(s.SemanticValidationError):
        c.runtime._call('proposal',s.GRAPH_PROPOSAL_SYSTEM,plan['proposal_request'],mutating_decoder,context=plan['proposal_context'])
    with c.store._db() as db:
        row=s.semantic_checkpoint_reader(db)(plan['proposal_hash'])
    assert row['status']=='needs_review' and 'atomic_raw_graph' not in row['data']


def test_review_threefold_bound_covers_escaping_unicode_controls_and_many_ids(atomic_parent_case):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case; module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    graph=independent_candidate(plan['proposal_request'])
    for claim in graph['claims']:
        claim['text']='业务条件“甲”\\说明\n权限\t范围'
    for _ in range(40): graph['claims'].append(deepcopy(graph['claims'][0]))
    _,refs,ops=c.runtime._packet([c.parent]); cap=plan['candidate_bytes']
    canonical=s.decode_support_graph(graph,refs,ops,candidate_bytes=cap)
    assert len(canonical['claims'])==92
    candidate_bytes=len(s._json(canonical).encode())
    review={**plan['proposal_request'],'stage':'review','candidate':canonical,
            'proposal_hash':plan['proposal_hash'],'packet_hash':plan['packet_hash']}
    profile=c.tasks.read_checkpoint(c.four_keys[0])['data']['context']['budget_profile']
    actual=s._semantic_request_bytes(c.snapshot,profile,'review','',review)
    assert actual<=plan['review_base_bytes']+3*candidate_bytes+1024
    assert actual<=plan['review_base_bytes']+3*cap+1024<=profile['stages']['review']['input_bytes']


def test_preplanned_review_requires_selected_atomic_allowlist(completed_atomic_parent):
    c=completed_atomic_parent
    review_hash=c.projection['semantic_coverage']['atomic_budgets'][0]['review_hash']
    with c.store._db() as db:
        data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(review_hash,)).fetchone()[0])
        data['result']=None; data['result_hash']=s._hash(None)
        db.execute("UPDATE business_semantic_tasks SET status='planned',data=? WHERE stage_hash=?",(s._json(data),review_hash))
    before=len(c.calls); c.runtime.atomic_budget_allowlist.clear()
    with pytest.raises(s.SemanticValidationError):
        c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    with c.store._db() as db:
        row=db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(review_hash,)).fetchone()
    assert row==('planned',) and len(c.calls)==before


def test_failed_atomic_proposal_with_injected_raw_graph_is_rejected(atomic_parent_case):
    c=atomic_parent_case; c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    plan=module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint,
        original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
        repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    def complete(system,user,**kwargs):
        kwargs['before_send'](); return {'text':json.dumps({'bad':'shape'}),'usage':{}}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError):
        c.runtime._call('proposal',s.GRAPH_PROPOSAL_SYSTEM,plan['proposal_request'],lambda value:s.decode_support_graph(value,{}, {},candidate_bytes=plan['candidate_bytes']),context=plan['proposal_context'])
    with c.store._db() as db:
        data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(plan['proposal_hash'],)).fetchone()[0])
        data['atomic_raw_graph']={'bad':'private'}; data['atomic_raw_graph_hash']=s._hash(data['atomic_raw_graph'])
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(s._json(data),plan['proposal_hash']))
        reader=s.semantic_checkpoint_reader(db)
        with pytest.raises(s.SemanticValidationError):
            s._checkpoint_record(plan['proposal_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)


from test_full_analysis import full_project
from test_semantic_integration import REAL_SYNTHESIS_BUILD, REAL_STORED_VALIDATOR


@pytest.fixture
def completed_atomic_controller(full_project, monkeypatch, request):
    from businessmap.full_synthesis import FullBusinessSynthesis
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.analysis_addenda import AnalysisAddenda
    from test_business_semantics import partition_review, independent_candidate, review
    full,service,project=full_project
    full.SOURCE_BYTES=37000
    monkeypatch.setattr(FullBusinessSynthesis,'build',REAL_SYNTHESIS_BUILD)
    monkeypatch.setattr(s,'validate_stored_semantic_projection',REAL_STORED_VALIDATOR)
    provider=service.providers.save({'name':'local fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'})
    ProjectAnalysis(service).bind_model(project['id'],provider['id'])
    calls=[]; semantic_reviews=[]; reject_first=bool(getattr(request,'param',False))
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user)
        stage=payload.get('stage','source' if 'files' in payload else 'routing'); calls.append(stage)
        if 'review_question' in payload:
            result={'title':'业务补充','summary':'','findings':[{'text':'办理业务需具备门店权限。','evidence_ids':['s1']}],'unknowns':[]}
        elif stage=='source':
            eid=payload['files'][0]['evidence_id']
            result={'title':'办理业务','summary':'','findings':[{'text':text,'evidence_ids':[eid]} for text in
                ['业务办理需要员工权限。','业务办理需要门店授权。','业务办理需要完整资料。','业务办理需要确认条件。',
                 '业务办理需要核对对象。','业务办理需要确认结果。']], 'unknowns':[]}
        elif stage=='routing':
            result={'assignments':[{'fact_id':item['fact_id'],'disposition':'public','category':'flows','title':'办理业务'} for item in payload['items']]}
        elif stage=='catalogue':
            result={'operations':[{'title':'办理业务','category':'flows','label_ids':[item['label_id'] for item in payload['items']]}]}
        elif stage=='subdivide':
            ids=[item['label_id'] for item in payload['items']]
            titles=['接收业务','确认业务','办理业务','完成业务','处理资料','核对资料','提交资料','审批资料','归档资料','查看资料','核实条件','确认对象','确认门店','查看结果','核对结果','接收结果']
            result={'operations':[{'title':titles[i//2],'category':'flows','label_ids':ids[i:i+2]} for i in range(0,len(ids),2)]}
        elif stage=='partition_review': result=partition_review(payload,reject=True)
        elif stage=='proposal': result=independent_candidate(payload)
        elif stage=='review':
            semantic_reviews.append(payload)
            result=review(payload,reject=reject_first and len(semantic_reviews)==1)
        else: raise AssertionError(stage)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    monkeypatch.setattr(service.providers,'complete',complete)
    full.plan(project['id']); snapshot=full.project_analysis.snapshot(project['id']); run=full._load(project['id'])
    for chunk in full._chunks(run): full._process(snapshot,run,chunk)
    chunks=full._chunks(run)
    evidence=[chunk['id']+':'+chunk['segments'][0]['evidence_id'] for chunk in chunks[:2]]
    addenda=AnalysisAddenda(service,guard=lambda:None)
    receipt=addenda.collect(snapshot,run,{'gap_id':'fixture-gap','question':'核对业务范围','evidence_ids':evidence,'snapshot_id':run['snapshot_id']})
    attestation={'operation':'办理业务','gap_id':'fixture-gap','evidence_ids':evidence,
        'evidence_hashes':{item['evidence_id']:item['content_hash'] for item in receipt['evidence']},
        'conclusion':'static_request_chain','reviewer':'independent_static_review','review_note':'核对范围'*2500}
    addenda.attest_matching(snapshot,run,receipt['addendum_id'],attestation)
    with pytest.MonkeyPatch.context() as historical:
        historical.setattr(s.BusinessSemanticIntegration, 'WHOLE_FRESH_ENABLED', False)
        first=full.analyze(project['id'])
    assert first['status']=='failed' and first['synthesis_failure']['reason_code']=='review_rejected', first
    with service.store._db() as db:
        d1=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='subdivide'").fetchone()[0]
        r1=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='partition_review'").fetchone()[0]
    monkeypatch.setattr(s.BusinessSemanticIntegration,'partition_repair_allowlist',{(d1,r1)})
    second=full.analyze(project['id'])
    assert second['status']=='failed' and second['synthesis_failure']['reason_code']=='review_rejected', second
    with service.store._db() as db:
        d2=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='subdivide' AND json_extract(data,'$.context.partition_revision') IS NOT NULL").fetchone()[0]
        r2=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='partition_review' AND json_extract(data,'$.context.partition_revision') IS NOT NULL").fetchone()[0]
        before=db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
    monkeypatch.setattr(s.BusinessSemanticIntegration,'partition_repair_allowlist',frozenset())
    monkeypatch.setattr(s.BusinessSemanticIntegration,'atomic_budget_allowlist',{(d1,r1,d2,r2)})
    paid=len(calls)
    third=full.analyze(project['id'])
    assert third['status']=='completed' and third['publication_complete'] is True, third
    persisted=full._load(project['id'])
    assert persisted['status']=='completed' and persisted['publication_complete'] is True
    assert len(persisted['synthesis']['semantic_coverage']['atomic_budgets'])==1
    with service.store._db() as db:
        for row in before:
            assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(row[0],)).fetchone()==row
    with service.store._db() as db:
        tasks=s.semantic_checkpoint_rows(db,persisted['synthesis']['semantic_input_hash'])
    return SimpleNamespace(full=full,service=service,store=service.store,project=project,snapshot=snapshot,
        run={**persisted,'semantic_input_hash':persisted['synthesis']['semantic_input_hash']},projection=persisted['synthesis'],
        tasks=tasks,four_keys=(d1,r1,d2,r2),old_rows=before,calls=calls,new_calls=deepcopy(calls[paid:]))


def test_atomic_full_controller_persists_completed_publication(completed_atomic_controller, monkeypatch):
    c=completed_atomic_controller
    prior=list(c.calls); monkeypatch.setattr(s.BusinessSemanticIntegration,'atomic_budget_allowlist',frozenset())
    assert c.full.analyze(c.project['id'])['publication_complete'] is True and c.calls==prior


def test_actual_review_overflow_stops_before_review_slot(atomic_parent_case, monkeypatch):
    from test_business_semantics import independent_candidate
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        assert payload['stage']=='proposal'
        return {'text':json.dumps(independent_candidate(payload),ensure_ascii=False),'usage':{}}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packets=c.runtime._packets([c.parent]); original=s._semantic_request_bytes
    def measured(snapshot,profile,stage,system,payload):
        actual=original(snapshot,profile,stage,system,payload)
        return profile['stages']['review']['input_bytes']+1 if stage=='review' and payload.get('candidate') else actual
    monkeypatch.setattr(s,'_semantic_request_bytes',measured)
    before=len(c.calls)
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._integrate_packet(packets[0])
    assert error.value.reason_code=='input_limit'
    assert [payload['stage'] for payload in c.calls[before:]]==['proposal']
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    atomic=[row for row in rows if row['data']['context'].get('atomic_budget')]
    assert [(row['stage'],row['status']) for row in atomic]==[('proposal','proposed')]


def test_atomic_concurrency_claims_one_slot_and_logs_committed_rows(atomic_parent_case, caplog):
    import copy
    from concurrent.futures import ThreadPoolExecutor
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case; entered=threading.Event(); release=threading.Event()
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        if payload['stage']=='proposal':
            entered.set(); assert release.wait(10); result=independent_candidate(payload)
        elif payload['stage']=='review': result=review(payload)
        else: raise AssertionError(payload['stage'])
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packets=c.runtime._packets([c.parent]); peer=copy.copy(c.runtime)
    peer._owned_slots,peer._task_bindings,peer._partitions=set(),{},[]
    peer._counter_lock,peer._stopped=threading.Lock(),threading.Event()
    before=len(c.calls); caplog.set_level('INFO',logger='businessmap.business_semantics')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(c.runtime._integrate_packet,packets[0])
        try:
            assert entered.wait(10)
            second=pool.submit(peer._integrate_packet,packets[0])
            with pytest.raises(s.SemanticValidationError): second.result(timeout=10)
        finally: release.set()
        result=first.result(timeout=20)
    assert [payload['stage'] for payload in c.calls[before:]]==['proposal','review']
    with c.store._db() as db: rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    atomic=[row for row in rows if row['data']['context'].get('atomic_budget')]
    assert sorted((row['stage'],row['status']) for row in atomic)==[('proposal','proposed'),('review','accepted')]
    for row in atomic:
        events=[event for event in caplog.records if getattr(event,'event',None)=='semantic_checkpoint_committed'
            and getattr(event,'stage_hash',None)==row['stage_hash'] and getattr(event,'status',None)==row['status']]
        assert len(events)==1 and events[0].input_hash==c.run['semantic_input_hash']
    assert result['review_hash']==next(row['stage_hash'] for row in atomic if row['stage']=='review')


@pytest.mark.parametrize('interrupted_stage', ['proposal','review'])
def test_atomic_received_checkpoint_is_free_accepted_without_new_selection(atomic_parent_case, monkeypatch, interrupted_stage):
    from test_business_semantics import independent_candidate, review
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); payload=json.loads(user); c.calls.append(payload)
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packet=c.runtime._packets([c.parent])[0]; original=s.BusinessSemanticIntegration._save_task
    with monkeypatch.context() as patch:
        def stop(self,key,stage,status,*args,**kwargs):
            original(self,key,stage,status,*args,**kwargs)
            if stage==interrupted_stage and status=='received': raise RuntimeError('after received')
        patch.setattr(s.BusinessSemanticIntegration,'_save_task',stop)
        with pytest.raises(RuntimeError,match='after received'):
            c.runtime._integrate_packet(packet)
    before=len(c.calls); c.runtime.atomic_budget_allowlist.clear()
    if interrupted_stage=='proposal':
        with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packet)
        with c.store._db() as db:
            rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        proposal=next(row for row in rows if row['stage']=='proposal' and row['data']['context'].get('atomic_budget'))
        assert proposal['status']=='proposed' and 'atomic_raw_graph' in proposal['data'] and len(c.calls)==before
        c.runtime.atomic_budget_allowlist={c.four_keys}
        c.runtime._integrate_packet(packet)
        assert len(c.calls)==before+1 and c.calls[-1]['stage']=='review'
    else:
        c.runtime._integrate_packet(packet)
        assert len(c.calls)==before
        with c.store._db() as db:
            rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        reviewrow=next(row for row in rows if row['stage']=='review' and row['data']['context'].get('atomic_budget'))
        assert reviewrow['status']=='accepted'


@pytest.mark.parametrize('receipt_kind',['recovery','supersession'])
def test_atomic_plan_does_not_ignore_inconsistent_receipt_callback(atomic_parent_case, receipt_kind):
    c=atomic_parent_case; original=c.tasks.read_checkpoint
    def reader(key): return original(key)
    for name in ('raw_checkpoint_row','raw_digest','raw_error_code','task_rows','catalogue_runs','catalogue_source','recovery_receipts','supersession_receipts'):
        setattr(reader,name,getattr(original,name))
    setattr(reader,receipt_kind+'_receipts',lambda _: [{'input_hash':c.run['semantic_input_hash']}])
    module=importlib.import_module('businessmap.semantic_atomic_budget')
    before=len(c.calls)
    with pytest.raises(s.SemanticValidationError):
        module.atomic_budget_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=reader,
            original_subdivision_hash=c.four_keys[0],original_review_hash=c.four_keys[1],
            repaired_subdivision_hash=c.four_keys[2],repaired_review_hash=c.four_keys[3])
    assert len(c.calls)==before


def test_raw_checkpoint_row_missing_table_is_controlled_failure():
    import sqlite3
    with sqlite3.connect(':memory:') as db:
        reader=s.semantic_checkpoint_reader(db)
        with pytest.raises(s.SemanticValidationError) as error:
            reader.raw_checkpoint_row('0'*64)
    assert error.value.reason_code=='cache_invalid'


def test_atomic_rejected_history_does_not_authorize_without_selection(atomic_parent_case):
    c=atomic_parent_case; c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist.clear()
    before=len(c.calls)
    with pytest.raises(s.SemanticValidationError): c.runtime._packets([c.parent])
    assert len(c.calls)==before
    with c.store._db() as db:
        assert not db.execute("SELECT 1 FROM business_semantic_tasks WHERE json_extract(data,'$.context.atomic_budget') IS NOT NULL").fetchone()


def test_atomic_provider_failure_is_not_retried_and_matches_sql_log(atomic_parent_case, caplog):
    c=atomic_parent_case
    def complete(system,user,**kwargs):
        kwargs['before_send'](); c.calls.append(json.loads(user)); raise ToolError('fixture provider failure',502)
    c.runtime.service.providers.complete=complete
    c.runtime.partition_repair_allowlist.clear(); c.runtime.atomic_budget_allowlist={c.four_keys}
    packet=c.runtime._packets([c.parent])[0]; before=len(c.calls)
    caplog.set_level('INFO',logger='businessmap.business_semantics')
    with pytest.raises(s.SemanticValidationError) as first: c.runtime._integrate_packet(packet)
    assert first.value.reason_code=='model_call'
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packet)
    assert len(c.calls)==before+1 and c.calls[-1]['stage']=='proposal'
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        row=next(row for row in rows if row['stage']=='proposal' and row['data']['context'].get('atomic_budget'))
        raw=rows.read_checkpoint.raw_checkpoint_row(row['stage_hash'])
    assert row['status']=='needs_review' and raw[6]=='model_call' and 'atomic_raw_graph' not in row['data']
    events=[event for event in caplog.records if getattr(event,'event',None)=='semantic_checkpoint_committed'
            and getattr(event,'stage_hash',None)==row['stage_hash'] and getattr(event,'status',None)=='needs_review']
    assert len(events)==1 and events[0].input_hash==c.run['semantic_input_hash']


@pytest.mark.parametrize('completed_atomic_controller',[True],indirect=True)
def test_atomic_controller_second_semantic_draft_persists_and_replays(completed_atomic_controller, monkeypatch):
    c=completed_atomic_controller
    assert c.new_calls==['proposal','review','proposal','review']
    assert c.projection['semantic_coverage']['atomic_budgets'][0]['review_hash']
    old=list(c.calls); monkeypatch.setattr(s.BusinessSemanticIntegration,'atomic_budget_allowlist',frozenset())
    assert c.full.analyze(c.project['id'])['publication_complete'] is True and c.calls==old


def test_owned_snapshot_reports_real_concurrent_provider_slots(semantic_case):
    setup = oversized_case(semantic_case, independent=True)
    runtime = setup[0]
    runtime.CONCURRENCY = 2
    original = runtime.service.providers.complete
    barrier = threading.Barrier(2)
    seen = []
    count_lock = threading.Lock()

    def observed(system, user, **kwargs):
        if json.loads(user)['stage'] == 'proposal':
            with count_lock:
                selected = len(seen) < 2
                if selected:
                    seen.append(None)
                    index = len(seen) - 1
            if selected:
                callback = kwargs['before_send']
                getter = callback._semantic_owned_snapshot
                callback()  # A real provider admits before it waits on a response.
                barrier.wait(timeout=30)
                snapshot = getter()
                with runtime.store._db() as db:
                    running = {row[0] for row in db.execute(
                        "SELECT stage_hash FROM business_semantic_tasks WHERE input_hash=? AND status='running'",
                        (runtime._input_hash,))}
                seen[index] = (snapshot, running)
                barrier.wait(timeout=30)
        return original(system, user, **kwargs)

    runtime.service.providers.complete = observed
    result = execute(setup)
    assert len(result['public_documents']) == 4
    assert len(seen) == 2
    keys = {snapshot['stage_hash'] for snapshot, _ in seen}
    assert len(keys) == 2
    for snapshot, running in seen:
        assert set(snapshot) == {'run_id', 'input_hash', 'stage_hash', 'owned_task_hashes'}
        assert snapshot['run_id'] == setup[3]['run_id']
        assert snapshot['input_hash'] == runtime._input_hash
        assert isinstance(snapshot['owned_task_hashes'], frozenset)
        assert keys <= snapshot['owned_task_hashes']
        assert keys <= running


def test_atomic_plans_are_computed_once_for_multiple_preflight_packets(semantic_case, monkeypatch):
    import sys
    setup = oversized_case(semantic_case, independent=True)
    runtime = setup[0]
    original = runtime._atomic_plans
    preflight_calls = []

    def observed():
        if sys._getframe(1).f_code.co_name == 'build':
            preflight_calls.append(1)
        return original()

    monkeypatch.setattr(runtime, '_atomic_plans', observed)
    result = execute(setup)
    assert len(result['public_documents']) == 4
    # One call for the pure packet loop, plus a fresh final projection call.
    assert len(preflight_calls) == 2


def test_owned_snapshot_does_not_admit_or_replace_fresh_before_send(semantic_case):
    from test_business_semantics import build_case
    guards = []
    setup = build_case(semantic_case, guard=lambda: guards.append(1))
    runtime = setup[0]

    def stopped_provider(system, user, **kwargs):
        before_send = kwargs['before_send']
        count = len(guards)
        snapshot = before_send._semantic_owned_snapshot()
        assert len(guards) == count
        assert snapshot['stage_hash'] in snapshot['owned_task_hashes']
        runtime._stopped.set()
        before_send()
        raise AssertionError('stopped guard must prevent sending')

    runtime.service.providers.complete = stopped_provider
    with pytest.raises(s.SemanticValidationError):
        execute(setup)
    assert setup[1] == []
    with runtime.store._db() as db:
        rows = db.execute("SELECT status FROM business_semantic_tasks WHERE input_hash=?", (runtime._input_hash,)).fetchall()
    assert rows == [('planned',)]


from test_semantic_recovery import case as recovery_case
from test_semantic_supersession import legacy_case as supersession_case


@pytest.mark.parametrize('kind', ['recovery', 'supersession'])
def test_receipt_ownership_is_registered_under_store_lock_before_visible(kind, request, tmp_path):
    from businessmap.semantic_recovery import authorize_catalogue_recovery, held_project_lock
    from businessmap.semantic_supersession import authorize_catalogue_supersession
    from test_semantic_recovery import grant
    from test_semantic_supersession import actual_grant

    case = request.getfixturevalue(kind + '_case')
    service, snapshot, run, parent = case
    if kind == 'recovery':
        plan, path = grant(case, tmp_path)
        authorize_catalogue_recovery(service, snapshot, run, plan, authorization=path)
        with service.store._db() as db:
            data = s.semantic_checkpoint_reader(db)(parent)['data']
        payload, context, system = data['request'], data['context'], 'unused'
    else:
        plan, path = actual_grant(case, tmp_path)
        authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
        payload, context, system = plan['new_request'], plan['new_context'], s.BOUNDED_MERGE_SYSTEM
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    receipt_table = 'business_semantic_recoveries' if kind == 'recovery' else 'business_semantic_supersessions'
    with service.store._db() as db:
        prior_receipt = db.execute('SELECT * FROM '+receipt_table+' WHERE child_stage_hash=?', (plan['child_stage_hash'],)).fetchone()
    assert prior_receipt is not None and prior_receipt[5:] == (None, None)

    class LockedOwnership(set):
        def add(self, key):
            assert service.store._lock._is_owned(), 'ownership registered outside store lock'
            super().add(key)

    snapshots, observed_receipts = [], []
    def complete(*args, **kwargs):
        snapshots.append(kwargs['before_send']._semantic_owned_snapshot())
        with service.store._db() as db:
            observed_receipts.append(db.execute('SELECT * FROM '+receipt_table+' WHERE child_stage_hash=?', (plan['child_stage_hash'],)).fetchone())
        raise RuntimeError('stop before provider admission')
    service.providers = SimpleNamespace(complete=complete)
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
    runtime._catalogue_tasks, runtime._task_bindings, runtime.plans, runtime._forbidden = [], {}, [], []
    runtime._owned_slots, runtime._stopped = LockedOwnership(), threading.Event()
    runtime._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    with held_project_lock(snapshot['project']['id']), pytest.raises(RuntimeError, match='stop before provider admission'):
        runtime._call('catalogue', system, payload, lambda x: x, context=context)
    assert len(snapshots) == len(observed_receipts) == 1
    assert snapshots[0]['stage_hash'] == plan['child_stage_hash']
    assert plan['child_stage_hash'] in snapshots[0]['owned_task_hashes']
    with service.store._db() as db:
        status, raw, error = db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone()
        receipt = db.execute('SELECT * FROM '+receipt_table+' WHERE child_stage_hash=?', (plan['child_stage_hash'],)).fetchone()
    data = json.loads(raw)
    assert status == 'needs_review' and error == data['failure_code'] == 'admission_incomplete'
    assert data['result'] is None and data['result_hash'] == s._hash(None)
    assert 'diagnostic' not in data and 'feedback' not in data
    assert receipt == observed_receipts[0] and receipt[:5] == prior_receipt[:5]
    assert type(receipt[5]) is int and isinstance(receipt[6], str) and s.HEX.fullmatch(receipt[6])
