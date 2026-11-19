"""Genuine temporary Source and one ordinary D1 review transport timeout."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from businessmap import business_semantics as sem
from businessmap.providers import ToolError
from test_business_semantics import independent_candidate
from test_proposal_timeout_recovery import source_timeout


@pytest.fixture
def review_timeout(source_timeout):
    c=source_timeout
    # Isolated fixture setup: build a fresh proposal+review pair from the same
    # genuine Source, before any recovery authorization exists.
    with c.store._db() as db:
        db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(c.parent['stage_hash'],))
    c.runtime._owned_slots.clear()
    calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        if payload['stage']=='review':
            error=ToolError('timeout',408);error.codex_reason='timeout';raise error
        return {'text':json.dumps(independent_candidate(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert calls==['proposal','review']
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        tasks=reader.task_rows(c.run['semantic_input_hash'])
        proposal=next(row for row in tasks if row['stage']=='proposal')
        parent=next(row for row in tasks if row['stage']=='review')
        raw={key:reader.raw_checkpoint_row(key) for key in (proposal['stage_hash'],parent['stage_hash'])}
    assert proposal['status']=='proposed' and parent['status']=='needs_review'
    assert parent['data']['diagnostic']=={'status_code':408,'codex_reason':'timeout'}
    assert parent['data']['result'] is None and parent['data']['context']['proposal_hash']==proposal['stage_hash']
    return SimpleNamespace(**{**vars(c),'proposal':proposal,'parent':parent,'protected':raw})


def test_review_plan_reconstructs_real_source_subject_and_preserves_raw_rows(review_timeout):
    from businessmap.semantic_review_recovery import review_timeout_recovery_plan, PLAN_FIELDS, POLICY
    c=review_timeout
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        plan=review_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=reader)
        assert set(plan)==PLAN_FIELDS and plan['policy']==POLICY
        assert plan['parent_stage_hash']==plan['logical_task_hash']==c.parent['stage_hash']
        assert plan['subject_proposal_hash']==c.proposal['stage_hash']
        assert plan['parent_failure_digest']==reader.raw_digest(c.parent['stage_hash'])
        assert plan['subject_proposal_digest']==reader.raw_digest(c.proposal['stage_hash'])
        assert plan['subject_result_hash']==c.proposal['data']['result_hash']
        assert (plan['logical_draft_maximum'],plan['physical_maximum_calls'],plan['replacement_allowance'])==(2,5,1)
        assert plan['child_stage_hash'] != plan['parent_stage_hash']
        assert {key:reader.raw_checkpoint_row(key) for key in c.protected}==c.protected


@pytest.mark.parametrize("tamper", ["source", "subject"])
def test_review_plan_rejects_changed_source_and_subject(review_timeout,tamper):
    from businessmap.semantic_review_recovery import review_timeout_recovery_plan
    c=review_timeout
    with c.store._db() as db:
        if tamper=='source':
            db.execute("UPDATE full_analysis_chunks SET data=replace(data,'核对门店权限','改变门店权限')")
        else:
            raw=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(c.proposal['stage_hash'],)).fetchone()[0]
            data=json.loads(raw);data['result']['claims'][0]['text']='forged candidate claim'
            data['result_hash']=sem._hash(data['result'])
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(sem._json(data),c.proposal['stage_hash']))
    with c.store._db() as db,pytest.raises(sem.SemanticValidationError):
        review_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=sem.semantic_checkpoint_reader(db))


def review_grant(c,tmp_path):
    import hashlib
    import time
    from businessmap.semantic_review_recovery import review_timeout_recovery_plan
    with c.store._db() as db:
        plan=review_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=sem.semantic_checkpoint_reader(db))
    stamp=int(time.time())
    evidence={'checked_at':stamp,'local_owner_pids':[99999999],'local_processes_drained':True,
        'upstream_result_or_billing_reconciled':False,'run_id':c.run['run_id'],'parent_stage_hash':c.parent['stage_hash'],
        'parent_row_digest':plan['parent_failure_digest'],'inventory_digest':plan['inventory_digest']}
    path=tmp_path/'review-evidence.json';path.write_text(json.dumps(evidence));path.chmod(0o600)
    auth={'policy':plan['policy'],'plan_hash':plan['plan_hash'],'parent_stage_hash':plan['parent_stage_hash'],'run_id':plan['run_id'],
        'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':stamp,'expires_at':stamp+3600,
        'max_uses':1,'reconciliation_ref':str(path),'reconciliation_hash':hashlib.sha256(path.read_bytes()).hexdigest()}
    grant=tmp_path/'review-grant.json';grant.write_text(json.dumps(auth));grant.chmod(0o600)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
    return plan,grant,path


def test_real_authorization_creates_one_exact_child_without_mutating_parent(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery,authenticate_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    assert receipt['policy']==plan['policy'] and receipt['consumed_at'] is None
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db);child=reader(plan['child_stage_hash'])
        assert child['stage']=='review' and child['status']=='planned' and child['attempt']==1
        assert child['data']['request']==c.parent['data']['request'] and child['data']['context']==c.parent['data']['context']
        assert {key:reader.raw_checkpoint_row(key) for key in c.protected}==c.protected
        assert authenticate_review_timeout_recovery(child,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)==receipt


def test_review_child_completes_once_with_strict_review_and_free_replay(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        assert payload['stage']=='review' and payload==c.parent['data']['request']
        return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    with held_project_lock(c.snapshot['project']['id']):
        result=c.runtime._integrate_packet(c.packet)
    assert result['review_hash']==plan['child_stage_hash'] and len(calls)==1
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        assert reader(plan['child_stage_hash'])['status']=='accepted'
        assert {key:reader.raw_checkpoint_row(key) for key in c.protected}==c.protected
    grant.unlink();evidence.unlink()
    with held_project_lock(c.snapshot['project']['id']):
        again=c.runtime._integrate_packet(c.packet)
    assert again['review_hash']==plan['child_stage_hash'] and len(calls)==1


def test_real_negative_child_only_enables_two_drafts_and_five_physical_calls(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=sum(v['stage']=='review' for v in calls)==1)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']):
        result=c.runtime._integrate_packet(c.packet)
    assert [v['stage'] for v in calls]==['review','proposal','review']
    assert {'prior_candidate','revision_feedback','preservation'} <= calls[1].keys()
    with c.store._db() as db:
        rows=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        packet=[row for row in rows if row['stage'] in {'proposal','review'} and row['data']['context']['packet_hash']==plan['packet_hash']]
        assert len(packet)==5 and all(row['status']!='planned' for row in packet)
        rejected=rows.read_checkpoint(plan['child_stage_hash'])
        assert rejected['status']=='needs_review' and rejected['data']['failure_code']=='review_rejected'
        from businessmap.semantic_review_recovery import _plan
        assert _plan(c.snapshot,c.run,plan['parent_stage_hash'],read_checkpoint=rows.read_checkpoint,baseline=plan['inventory_digest'])==plan
        assert result['review_hash'] != plan['child_stage_hash']
        assert {key:rows.read_checkpoint.raw_checkpoint_row(key) for key in c.protected}==c.protected
    sem.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    with held_project_lock(c.snapshot['project']['id']):
        projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert projection['complete'] and projection['verification']['accepted_review_hashes']==[result['review_hash']]
    sem.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    assert len(calls)==3


def test_review_child_projection_current_stored_support_and_historical_replay(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']):
        projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert projection['complete'] and projection['verification']['accepted_review_hashes']==[plan['child_stage_hash']]
    assert projection['verification']['recovery_receipt_hashes']==[__import__('businessmap.semantic_recovery',fromlist=['receipt_hash']).receipt_hash(receipt)]
    with c.store._db() as db: rows=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    sem.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    sem.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    sem.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    grant.unlink();evidence.unlink()
    sem.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    with held_project_lock(c.snapshot['project']['id']):
        replay=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert sem._projection_hash(replay)==sem._projection_hash(projection) and calls==['review']


@pytest.mark.parametrize('tamper',['sql_error','timeout_code','timeout_reason','result','draft','candidate','unselected_failure','extra_packet_row'])
def test_review_plan_refuses_unsupported_parent_or_inventory(review_timeout,tamper):
    from businessmap.semantic_review_recovery import review_timeout_recovery_plan
    c=review_timeout
    with c.store._db() as db:
        if tamper=='sql_error':
            db.execute('UPDATE business_semantic_tasks SET error_code=? WHERE stage_hash=?',('provider_error',c.parent['stage_hash']))
        elif tamper=='unselected_failure':
            db.execute("UPDATE business_semantic_tasks SET status='needs_review' WHERE stage='catalogue'")
        elif tamper=='extra_packet_row':
            data=deepcopy(c.parent['data']);data['request_hash']='f'*64
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',('f'*64,'review',c.run['semantic_input_hash'],'planned',1,sem._json(data),None,'fixture'))
        else:
            data=deepcopy(c.parent['data'])
            if tamper=='timeout_code':data['diagnostic']['status_code']=502
            if tamper=='timeout_reason':data['diagnostic']['codex_reason']='provider_error'
            if tamper=='result':data['result']={};data['result_hash']=sem._hash({})
            if tamper=='draft':data['context']['revision']['draft_index']=2
            if tamper=='candidate':data['request']['candidate']['claims'][0]['text']='forged'
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(sem._json(data),c.parent['stage_hash']))
    with c.store._db() as db,pytest.raises(sem.SemanticValidationError):
        review_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=sem.semantic_checkpoint_reader(db))


@pytest.mark.parametrize('bad',['privacy','invalid_id','incomplete','unknown_code'])
def test_unsafe_review_child_never_creates_d2(review_timeout,tmp_path,bad):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage']);value=review(payload,reject=True)
        if bad=='privacy':value['issues'][0]['code']='privacy'
        if bad=='invalid_id':value['issues'][0]['input_ids']=['secret/path.java']
        if bad=='incomplete':value['operation_checks']=[]
        if bad=='unknown_code':value['issues'][0]['code']='raw-reason'
        return {'text':json.dumps(value),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert calls==['review']
    with c.store._db() as db:
        rows=sem.semantic_checkpoint_reader(db).task_rows(c.run['semantic_input_hash'])
    assert not any(row['stage'] in {'proposal','review'} and row['attempt']==2 for row in rows)


def test_consumed_review_child_crash_never_resends(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[];observed_receipts=[]
    def crash(*args,**kwargs):
        calls.append(True)
        with c.store._db() as db:
            observed_receipts.append(db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone())
        raise RuntimeError('pre-dispatch crash')
    c.runtime.service.providers.complete=crash
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError):
        c.runtime._integrate_packet(c.packet)
    c.runtime._owned_slots.clear()
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert len(calls)==1 and len(observed_receipts)==1
    with c.store._db() as db:
        row=db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        receipt=db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        assert receipt==observed_receipts[0] and type(receipt[5]) is int and isinstance(receipt[6],str) and sem.HEX.fullmatch(receipt[6])
        data=json.loads(row[1])
        assert row[0]=='needs_review' and row[2]==data['failure_code']=='admission_incomplete'
        assert data['result'] is None and data['result_hash']==sem._hash(None)
        assert 'diagnostic' not in data and 'feedback' not in data
        assert not db.execute("SELECT 1 FROM business_semantic_tasks WHERE input_hash=? AND stage IN ('proposal','review') AND attempt=2",(c.run['semantic_input_hash'],)).fetchall()
        assert {key:sem.semantic_checkpoint_reader(db).raw_checkpoint_row(key) for key in c.protected}==c.protected


def test_second_review_timeout_never_retries_or_fabricates_d2(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    def timeout(system,user,**kwargs):
        kwargs['before_send']();calls.append(json.loads(user)['stage']);error=ToolError('timeout',408);error.codex_reason='timeout';raise error
    c.runtime.service.providers.complete=timeout
    for _ in range(2):
        with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
            c.runtime._integrate_packet(c.packet)
    assert calls==['review']
    with c.store._db() as db:
        rows=sem.semantic_checkpoint_reader(db).task_rows(c.run['semantic_input_hash'])
    assert next(r for r in rows if r['stage_hash']==plan['child_stage_hash'])['status']=='needs_review'
    assert not any(r['stage'] in {'proposal','review'} and r['attempt']==2 for r in rows)


@pytest.mark.parametrize('tamper',['grant_mode','grant_bytes','evidence_mode','evidence_bytes','expired','live_pid'])
def test_review_authorization_rejects_changed_private_proof(review_timeout,tmp_path,monkeypatch,tamper):
    import os
    import time
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    c=review_timeout;plan,grant,evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    if tamper=='grant_mode':grant.chmod(0o644)
    if tamper=='grant_bytes':
        seen=[]
        def guard(_):
            seen.append(True)
            if len(seen)==3:grant.write_text(grant.read_text()+' ')
        service.project_analysis.guard=guard
    if tamper=='evidence_mode':evidence.chmod(0o644)
    if tamper=='evidence_bytes':evidence.write_text('{}')
    if tamper=='expired':
        now=time.time();monkeypatch.setattr(time,'time',lambda:now+3601)
    if tamper=='live_pid':
        original=os.kill;monkeypatch.setattr(os,'kill',lambda pid,sig:None if pid==99999999 else original(pid,sig))
    with pytest.raises(sem.SemanticValidationError):
        authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0
        reader=sem.semantic_checkpoint_reader(db)
        assert {key:reader.raw_checkpoint_row(key) for key in c.protected}==c.protected


def test_review_timeout_after_proposal_recovery_cannot_stack_same_packet(source_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_review_recovery import review_timeout_recovery_plan
    from businessmap.semantic_recovery import held_project_lock
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,grant=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user)
        if payload['stage']=='review':
            error=ToolError('timeout',408);error.codex_reason='timeout';raise error
        return {'text':json.dumps(independent_candidate(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        parent=next(row for row in reader.task_rows(c.run['semantic_input_hash']) if row['stage']=='review')
        with pytest.raises(sem.SemanticValidationError):
            review_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader)
        assert len(reader.recovery_receipts(c.run['semantic_input_hash']))==1


def test_two_review_authorizers_and_two_consumers_create_one_child(review_timeout,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Barrier
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    barrier=Barrier(2)
    def authorize(_):barrier.wait();return authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with ThreadPoolExecutor(max_workers=2) as pool: receipts=list(pool.map(authorize,range(2)))
    assert receipts[0]==receipts[1]
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    c.runtime._task_bindings[plan['child_stage_hash']]={'request':deepcopy(c.parent['data']['request']),'context':deepcopy(c.parent['data']['context']),
        'recovery_id':plan['recovery_id'],'logical_task_hash':plan['logical_task_hash']}
    prepared=[c.runtime._prepare_mutation(plan['child_stage_hash'],operation='consume_recovery') for _ in range(2)]
    barrier=Barrier(2)
    def consume(index):
        barrier.wait()
        try:
            with c.store._lock,c.store._db() as db:
                db.execute('BEGIN IMMEDIATE')
                consume_recovery(db,c.snapshot,c.run,plan['child_stage_hash'],source_material=prepared[index]._PreparedMutation__material,prepared_mutation=prepared[index])
            return True
        except sem.SemanticValidationError:return False
    with held_project_lock(c.snapshot['project']['id']),ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(consume,range(2)))
    assert sorted(outcomes)==[False,True]
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==1


@pytest.mark.parametrize('tamper',['parent','subject','receipt_evidence','child_request','extra_receipt_field'])
def test_review_authentication_rejects_tampered_lineage(review_timeout,tmp_path,tamper):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery,authenticate_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with c.store._db() as db:
        if tamper in {'parent','subject'}:
            key=c.parent['stage_hash'] if tamper=='parent' else c.proposal['stage_hash']
            db.execute("UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash=?",(key,))
        elif tamper=='child_request':
            raw=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]
            data=json.loads(raw);data['request']['candidate']['claims'][0]['text']='forged'
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(sem._json(data),plan['child_stage_hash']))
        else:
            raw=db.execute('SELECT data FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]
            data=json.loads(raw)
            if tamper=='receipt_evidence':data['reconciliation_evidence']+=' '
            else:data['unrecognized']=True
            db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(sem._json(data),plan['child_stage_hash']))
    with c.store._db() as db,pytest.raises(sem.SemanticValidationError):
        reader=sem.semantic_checkpoint_reader(db)
        authenticate_review_timeout_recovery(reader(plan['child_stage_hash']),snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)


def test_paid_review_result_is_retained_when_source_changes(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage']);result=review(payload)
        with c.store._db() as db:db.execute("UPDATE full_analysis_chunks SET data=replace(data,'核对门店权限','改变门店权限')")
        return {'text':json.dumps(result),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        child=sem.semantic_checkpoint_reader(db)(plan['child_stage_hash'])
        assert child['status']=='received' and child['data']['result'] is not None
    assert calls==['review']


def _prepared_review_child(c,plan):
    key=plan['child_stage_hash']
    c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],key)}
    c.runtime._owned_slots.clear()
    c.runtime._task_bindings[key]={'request':deepcopy(c.parent['data']['request']),'context':deepcopy(c.parent['data']['context']),
        'recovery_id':plan['recovery_id'],'logical_task_hash':plan['logical_task_hash']}
    return c.runtime._prepare_mutation(key,operation='consume_recovery')


def test_review_authorization_evidence_older_than_60_rejected(review_timeout,tmp_path,monkeypatch):
    import time
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path);now=time.time()
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    monkeypatch.setattr(time,'time',lambda:now+61)
    with pytest.raises(sem.SemanticValidationError):authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with c.store._db() as db:assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0


@pytest.mark.parametrize('condition',['after_120','expired','evidence','pid','control'])
def test_review_consume_rechecks_current_authority_without_resetting_evidence(review_timeout,tmp_path,monkeypatch,condition):
    import os,time
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    c=review_timeout;plan,grant,evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    prepared=_prepared_review_child(c,plan); original=evidence.read_bytes()
    if condition=='after_120':monkeypatch.setattr(time,'time',lambda:receipt['authorized_at']+120)
    if condition=='expired':monkeypatch.setattr(time,'time',lambda:receipt['expires_at'])
    if condition=='evidence':evidence.write_text('{}')
    if condition=='pid':
        kill=os.kill;monkeypatch.setattr(os,'kill',lambda pid,sig:None if pid==99999999 else kill(pid,sig))
    if condition=='control':c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
    def consume():
        with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            return consume_recovery(db,c.snapshot,c.run,plan['child_stage_hash'],source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
    if condition=='after_120':
        consumed=consume();assert consumed['consumed_at']==receipt['authorized_at']+120 and evidence.read_bytes()==original
    else:
        with pytest.raises(sem.SemanticValidationError):consume()
        with c.store._db() as db:
            assert db.execute('SELECT consumed_at FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()==(None,)
            assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('planned',)


@pytest.mark.parametrize('phase,mutation',[('running','parent'),('running','source'),('running','extra'),('accepted','parent'),('accepted','source'),('accepted','extra')])
def test_review_claim_and_promotion_rollback_trigger_drift(review_timeout,tmp_path,phase,mutation):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};calls=[]
    with c.store._db() as db:
        source=db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]
        if mutation=='parent':action="UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash='"+plan['parent_stage_hash']+"'"
        elif mutation=='source':action="UPDATE full_analysis_chunks SET data=data || ' '"
        else:action="INSERT INTO business_semantic_tasks VALUES ('extra','review','foreign','planned',1,'{}',NULL,'fixture')"
        db.execute("CREATE TRIGGER drift_review AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.stage_hash='"+plan['child_stage_hash']+"' AND NEW.status='"+phase+"' BEGIN "+action+"; END")
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        return {'text':json.dumps(review(payload)),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        assert {key:reader.raw_checkpoint_row(key) for key in c.protected}==c.protected
        assert db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]==source
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage_hash='extra'").fetchone()[0]==0
        assert reader(plan['child_stage_hash'])['status']==('planned' if phase=='running' else 'received')
    assert calls==([] if phase=='running' else ['review'])
    if phase=='running': assert plan['child_stage_hash'] not in c.runtime._owned_slots


def test_effective_review_requires_authenticated_receipt_and_never_grants_send(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery,effective_review
    from businessmap.semantic_recovery import held_project_lock
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    with c.store._db() as db:
        assert effective_review(plan['parent_stage_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=sem.semantic_checkpoint_reader(db))==(plan['parent_stage_hash'],None)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with c.store._db() as db:
        key,receipt=effective_review(plan['parent_stage_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=sem.semantic_checkpoint_reader(db))
    assert key==plan['child_stage_hash'] and receipt['consumed_at'] is None
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False});c.runtime._owned_slots.clear();calls=[]
    c.runtime.service.providers.complete=lambda *args,**kwargs:calls.append(True)
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    assert not calls
    with c.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()==('planned',)


def test_review_replacement_preserves_exact_system_wire_and_schema(review_timeout,tmp_path):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import review
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    request=c.parent['data']['request'];system=sem.semantic_stage_system(c.snapshot,'review',request)
    schema=sem.semantic_output_schema('review',request)
    assert plan['system_digest']==sem._hash(system)
    assert plan['wire_digest']==sem._hash(sem._json(request))
    assert plan['schema_digest']==sem._hash(schema)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};observed=[]
    def complete(actual_system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user)
        observed.append((actual_system,user,kwargs['output_schema']))
        return {'text':json.dumps(review(payload)),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']):c.runtime._integrate_packet(c.packet)
    assert observed==[(system,sem._json(request),schema)]


@pytest.mark.parametrize('field,noncanonical', [('logical_draft_maximum',2.0),('physical_maximum_calls',5.0),('replacement_allowance',1.0),('replacement_allowance',True)])
@pytest.mark.parametrize('historical',[False,True])
def test_review_receipt_rejects_noninteger_allowances_even_when_equal(review_timeout,tmp_path,field,noncanonical,historical):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery,authenticate_review_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    c=review_timeout;plan,grant,evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    if historical:
        c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
        prepared=_prepared_review_child(c,plan)
        with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            consume_recovery(db,c.snapshot,c.run,plan['child_stage_hash'],source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
        grant.unlink();evidence.unlink()
    with c.store._db() as db:
        raw=db.execute('SELECT data FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]
        data=json.loads(raw);data[field]=noncanonical
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(sem._json(data),plan['child_stage_hash']))
    with pytest.raises(sem.SemanticValidationError):
        with c.store._db() as db:
            reader=sem.semantic_checkpoint_reader(db)
            authenticate_review_timeout_recovery(reader(plan['child_stage_hash']),snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)


@pytest.mark.parametrize('field,noncanonical', [('logical_draft_maximum',2.0),('physical_maximum_calls',5.0),('replacement_allowance',1.0),('replacement_allowance',True)])
def test_review_authorization_rejects_noninteger_plan_allowances(review_timeout,tmp_path,field,noncanonical):
    from contextlib import nullcontext
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path);plan[field]=noncanonical
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    with pytest.raises(sem.SemanticValidationError):authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0
