"""Expired-unused review authority: only one event for the original child."""
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from businessmap import business_semantics as sem
from test_review_timeout_recovery import review_timeout, review_grant
from test_proposal_timeout_recovery import source_timeout



def test_expired_unused_preview_uses_exact_raw_row_and_same_transaction_capture(review_timeout, tmp_path, monkeypatch):
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    from businessmap._prepared_mutation import _capture_dynamic
    from businessmap import semantic_review_recovery as recovery
    c = review_timeout
    original, grant, _ = review_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(),
        _controls=lambda _: {'paused': True}, project_analysis=SimpleNamespace(guard=lambda _: None))
    receipt = authorize_review_timeout_recovery(service, c.snapshot, c.run, original, authorization=grant)
    monkeypatch.setattr(time, 'time', lambda: receipt['expires_at'] + 1)
    with c.store._db() as db:
        db.execute('BEGIN')
        reader = sem.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
        row = db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?', (original['child_stage_hash'],)).fetchone()
        capture = _capture_dynamic(db, c.run['semantic_input_hash'])
    plan = recovery.review_authority_reauthorization_plan(c.snapshot, c.run, original['child_stage_hash'],
        read_checkpoint=reader, original_row=row, dynamic_capture=capture)
    assert plan['original_plan_hash'] == original['plan_hash']
    assert plan['child_stage_hash'] == original['child_stage_hash']
    assert plan['event_limit'] == 1
    assert json.loads(row[4])['authorization_ref'] == receipt['authorization_ref']


def test_one_event_authorizes_same_child_then_consumes_and_replays(review_timeout, tmp_path, monkeypatch):
    import hashlib
    from businessmap import semantic_review_recovery as r
    from businessmap.semantic_recovery import held_project_lock
    from businessmap._prepared_mutation import _capture_dynamic
    from businessmap._source_material import prepare_source_material
    from test_business_semantics import review
    c=review_timeout; old,grant,old_evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=r.authorize_review_timeout_recovery(service,c.snapshot,c.run,old,authorization=grant)
    now=receipt['expires_at']+1;monkeypatch.setattr(time,'time',lambda:now)
    with c.store._db() as db:
        db.execute('BEGIN');records=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash']);reader=records.read_checkpoint
        row=db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(old['child_stage_hash'],)).fetchone()
        capture=_capture_dynamic(db,c.run['semantic_input_hash'])
    plan=r.review_authority_reauthorization_plan(c.snapshot,c.run,old['child_stage_hash'],read_checkpoint=reader,original_row=row,dynamic_capture=capture)
    material=prepare_source_material(c.snapshot,c.run,read_checkpoint=reader)
    prepared=r.prepare_review_authority_reauthorization(service,c.snapshot,c.run,plan,source_material=material)
    evidence={'checked_at':now,'local_owner_pids':[99999999],'local_processes_drained':True,'upstream_result_or_billing_reconciled':False,
        'zero_outbound':True,'nonambiguous':True,**{k:plan[k] for k in ('run_id','parent_stage_hash','child_stage_hash','original_row_digest','child_raw_digest','current_inventory_digest','plan_hash')}}
    ep=tmp_path/'renew-evidence.json';ep.write_text(json.dumps(evidence));ep.chmod(0o600)
    auth={**{k:plan[k] for k in ('policy','plan_hash','parent_stage_hash','run_id')},'operator_id':'root','authorization_source':'user-fix-and-continue',
        'authorized_at':now,'expires_at':now+3600,'max_uses':1,'reconciliation_ref':str(ep),'reconciliation_hash':hashlib.sha256(ep.read_bytes()).hexdigest()}
    gp=tmp_path/'renew-grant.json';gp.write_text(json.dumps(auth));gp.chmod(0o600)
    monkeypatch.setattr(sem.BusinessSemanticIntegration,'review_timeout_recovery_allowlist',{(old['parent_stage_hash'],old['child_stage_hash'])})
    renewed=r.reauthorize_expired_unused_review_recovery(service,c.snapshot,c.run,plan,authorization=gp,source_material=material,prepared_mutation=prepared)
    assert renewed['authority_event']['original_row']==list(row) and renewed['child_stage_hash']==old['child_stage_hash']
    with c.store._db() as db:
        reader=sem.semantic_checkpoint_reader(db)
        assert r.authenticate_review_timeout_recovery(reader(old['child_stage_hash']),snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)==renewed
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(old['parent_stage_hash'],old['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']): result=c.runtime._integrate_packet(c.packet)
    assert result['review_hash']==old['child_stage_hash'] and len(calls)==1
    grant.unlink();old_evidence.unlink();gp.unlink();ep.unlink()
    with held_project_lock(c.snapshot['project']['id']): result=c.runtime._integrate_packet(c.packet)
    assert result['review_hash']==old['child_stage_hash'] and len(calls)==1


@pytest.fixture
def renewal_case(review_timeout, tmp_path, monkeypatch):
    import hashlib
    from businessmap import semantic_review_recovery as r
    from businessmap._prepared_mutation import _capture_dynamic
    from businessmap._source_material import prepare_source_material
    c=review_timeout;old,grant,old_evidence=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    original=r.authorize_review_timeout_recovery(service,c.snapshot,c.run,old,authorization=grant)
    now=original['expires_at']+1;monkeypatch.setattr(time,'time',lambda:now)
    with c.store._db() as db:
        db.execute('BEGIN');records=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash']);reader=records.read_checkpoint
        row=db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(old['child_stage_hash'],)).fetchone()
        capture=_capture_dynamic(db,c.run['semantic_input_hash'])
    plan=r.review_authority_reauthorization_plan(c.snapshot,c.run,old['child_stage_hash'],read_checkpoint=reader,original_row=row,dynamic_capture=capture)
    material=prepare_source_material(c.snapshot,c.run,read_checkpoint=reader)
    prepared=r.prepare_review_authority_reauthorization(service,c.snapshot,c.run,plan,source_material=material)
    evidence={'checked_at':now,'local_owner_pids':[99999999],'local_processes_drained':True,'upstream_result_or_billing_reconciled':False,'zero_outbound':True,'nonambiguous':True,**{k:plan[k] for k in ('run_id','parent_stage_hash','child_stage_hash','original_row_digest','child_raw_digest','current_inventory_digest','plan_hash')}}
    ep=tmp_path/'new-evidence.json';ep.write_text(json.dumps(evidence));ep.chmod(0o600)
    auth={**{k:plan[k] for k in ('policy','plan_hash','parent_stage_hash','run_id')},'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':now,'expires_at':now+3600,'max_uses':1,'reconciliation_ref':str(ep),'reconciliation_hash':hashlib.sha256(ep.read_bytes()).hexdigest()}
    gp=tmp_path/'new-grant.json';gp.write_text(json.dumps(auth));gp.chmod(0o600)
    monkeypatch.setattr(sem.BusinessSemanticIntegration,'review_timeout_recovery_allowlist',{(old['parent_stage_hash'],old['child_stage_hash'])})
    return SimpleNamespace(c=c,old=old,original=original,row=row,plan=plan,material=material,prepared=prepared,service=service,grant=gp,evidence=ep,now=now)


def _append(case):
    from businessmap.semantic_review_recovery import reauthorize_expired_unused_review_recovery
    return reauthorize_expired_unused_review_recovery(case.service,case.c.snapshot,case.c.run,case.plan,
        authorization=case.grant,source_material=case.material,prepared_mutation=case.prepared)


@pytest.mark.parametrize('mutation',['no_outbound_proof','ambiguous','stale','allowlist','source','other_row','trigger'])
def test_invalid_new_authority_or_drift_rolls_back(renewal_case,monkeypatch,mutation):
    import hashlib
    x=renewal_case
    if mutation in ('no_outbound_proof','ambiguous'):
        evidence=json.loads(x.evidence.read_text());evidence['zero_outbound' if mutation=='no_outbound_proof' else 'nonambiguous']=False
        x.evidence.write_text(json.dumps(evidence));grant=json.loads(x.grant.read_text());grant['reconciliation_hash']=hashlib.sha256(x.evidence.read_bytes()).hexdigest();x.grant.write_text(json.dumps(grant))
    if mutation=='stale':monkeypatch.setattr(time,'time',lambda:x.now+61)
    if mutation=='allowlist':monkeypatch.setattr(sem.BusinessSemanticIntegration,'review_timeout_recovery_allowlist',set())
    with x.c.store._db() as db:
        if mutation=='source':db.execute("UPDATE full_analysis_chunks SET data=replace(data,'核对门店权限','改变门店权限')")
        if mutation=='other_row':db.execute('UPDATE business_semantic_tasks SET created_at=created_at||\'x\' WHERE stage_hash=?',(x.old['parent_stage_hash'],))
        if mutation=='trigger':db.execute("CREATE TRIGGER malicious AFTER UPDATE OF data ON business_semantic_recoveries BEGIN UPDATE business_semantic_tasks SET created_at=created_at||'x' WHERE stage_hash='%s'; END"%x.old['parent_stage_hash'])
    with pytest.raises(sem.SemanticValidationError):_append(x)
    with x.c.store._db() as db:
        assert db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==x.row
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==('planned',)


def test_second_event_and_tampered_event_are_rejected(renewal_case):
    from businessmap import semantic_review_recovery as r
    x=renewal_case;receipt=_append(x)
    with pytest.raises(sem.SemanticValidationError):_append(x)
    event=receipt['authority_event'];event['original_row'][4]+=' '
    with x.c.store._db() as db:
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(sem._json({k:v for k,v in receipt.items() if k not in ('consumed_at','dispatch_id')}),x.old['child_stage_hash']))
        reader=sem.semantic_checkpoint_reader(db)
        with pytest.raises(sem.SemanticValidationError):r.authenticate_review_timeout_recovery(reader(x.old['child_stage_hash']),snapshot=x.c.snapshot,expected_input_hash=x.c.run['semantic_input_hash'],read_checkpoint=reader)


def test_concurrent_authorizers_have_exactly_one_winner(renewal_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap.semantic_review_recovery import prepare_review_authority_reauthorization,reauthorize_expired_unused_review_recovery
    x=renewal_case;barrier=Barrier(2)
    second=prepare_review_authority_reauthorization(x.service,x.c.snapshot,x.c.run,x.plan,source_material=x.material)
    def attempt(prepared):
        barrier.wait()
        try:
            return reauthorize_expired_unused_review_recovery(x.service,x.c.snapshot,x.c.run,x.plan,authorization=x.grant,source_material=x.material,prepared_mutation=prepared)
        except sem.SemanticValidationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,[x.prepared,second]))
    assert sum(item is not None for item in results)==1
    with x.c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==1
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==('planned',)


@pytest.mark.parametrize('change',['event_hash','old_data','new_grant','new_evidence','max_uses_bool','outer_original','second_event'])
def test_event_tampering_rejected(renewal_case,change):
    from businessmap import semantic_review_recovery as r
    x=renewal_case;receipt=_append(x);data={k:v for k,v in receipt.items() if k not in ('consumed_at','dispatch_id')};event=data['authority_event']
    if change=='event_hash':event['event_hash']='0'*64
    if change=='old_data':event['original_row'][4]+=' '
    if change=='new_grant':event['grant_raw']+=' '
    if change=='new_evidence':event['evidence_raw']+=' '
    if change=='max_uses_bool':data['max_uses']=True
    if change=='outer_original':data['operator_id']='other'
    if change=='second_event':event['second_event']={}
    with x.c.store._db() as db:
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(sem._json(data),x.old['child_stage_hash']))
        reader=sem.semantic_checkpoint_reader(db)
        with pytest.raises(sem.SemanticValidationError):r.authenticate_review_timeout_recovery(reader(x.old['child_stage_hash']),snapshot=x.c.snapshot,expected_input_hash=x.c.run['semantic_input_hash'],read_checkpoint=reader)


def test_renewed_authority_expiry_blocks_same_child(renewal_case,monkeypatch):
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    from test_review_timeout_recovery import _prepared_review_child
    x=renewal_case;_append(x)
    x.c.store.config_set('full_analysis_control:'+x.c.run['run_id'],{'paused':False,'cancelled':False})
    prepared=_prepared_review_child(x.c,x.old)
    monkeypatch.setattr(time,'time',lambda:x.now+3600)
    with pytest.raises(sem.SemanticValidationError),held_project_lock(x.c.snapshot['project']['id']),x.c.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        consume_recovery(db,x.c.snapshot,x.c.run,x.old['child_stage_hash'],source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
    with x.c.store._db() as db:
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==(None,None)


def test_new_event_current_stored_support_and_free_history(renewal_case):
    from businessmap.semantic_recovery import held_project_lock,receipt_hash
    from test_business_semantics import review
    x=renewal_case;receipt=_append(x);c=x.c
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(x.old['parent_stage_hash'],x.old['child_stage_hash'])};calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']): projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert projection['complete'] and projection['verification']['recovery_receipt_hashes']==[receipt_hash(receipt)]
    with c.store._db() as db:rows=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    sem.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    sem.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    sem.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    x.grant.unlink();x.evidence.unlink()
    sem.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    with held_project_lock(c.snapshot['project']['id']): replay=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert sem._projection_hash(replay)==sem._projection_hash(projection) and calls==['review']


@pytest.mark.parametrize('tamper',['duplicate','oversize'])
def test_v2_raw_sql_rejects_duplicate_keys_and_oversize(renewal_case,tamper):
    x=renewal_case;_append(x)
    with x.c.store._db() as db:
        raw=db.execute('SELECT data FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()[0]
        if tamper=='duplicate':raw=raw.replace('"authority_event":','"authority_event":{},"authority_event":',1)
        else:raw += ' '*524288
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(raw,x.old['child_stage_hash']))
        reader=sem.semantic_checkpoint_reader(db)
        with pytest.raises(sem.SemanticValidationError):reader.recovery_receipts(x.c.run['semantic_input_hash'])


@pytest.mark.parametrize('field,value',[(field,value) for field,canonical in (
    ('logical_draft_maximum',2),('physical_maximum_calls',5),('replacement_allowance',1),('event_limit',1))
    for value in (True,float(canonical))])
def test_reauthorization_prepare_rejects_non_integer_allowances(renewal_case,field,value):
    from businessmap.semantic_review_recovery import prepare_review_authority_reauthorization
    x=renewal_case;plan={**x.plan,field:value}
    with pytest.raises(sem.SemanticValidationError):
        prepare_review_authority_reauthorization(x.service,x.c.snapshot,x.c.run,plan,source_material=x.material)
    with x.c.store._db() as db:
        assert db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==x.row


@pytest.mark.parametrize('field,value,rehash',[
    ('logical_draft_maximum',True,False),('physical_maximum_calls',5.0,False),('replacement_allowance',True,False),
    ('event_limit',True,False),('event_limit',2,False),('scope_digest','f'*64,False),
    ('scope_digest','f'*64,True),('plan_hash','0'*64,False)])
def test_prepared_reauthorization_rejects_changed_plan_without_write(renewal_case,field,value,rehash):
    from businessmap.semantic_review_recovery import reauthorize_expired_unused_review_recovery
    x=renewal_case;plan={**x.plan,field:value}
    if rehash:plan['plan_hash']=sem._hash({k:v for k,v in plan.items() if k!='plan_hash'})
    with pytest.raises(sem.SemanticValidationError):
        reauthorize_expired_unused_review_recovery(x.service,x.c.snapshot,x.c.run,plan,authorization=x.grant,source_material=x.material,prepared_mutation=x.prepared)
    with x.c.store._db() as db:
        assert db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()==x.row


@pytest.mark.parametrize('change',['whitespace','order'])
def test_v2_requires_canonical_sql_data_bytes(renewal_case,change):
    from businessmap import semantic_review_recovery as r
    x=renewal_case;_append(x)
    with x.c.store._db() as db:
        raw=db.execute('SELECT data FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()[0]
        if change=='whitespace':raw+=' '
        else:raw=json.dumps(dict(reversed(list(json.loads(raw).items()))),ensure_ascii=False,separators=(',',':'))
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(raw,x.old['child_stage_hash']))
        reader=sem.semantic_checkpoint_reader(db)
        with pytest.raises(sem.SemanticValidationError):
            r.authenticate_review_timeout_recovery(reader(x.old['child_stage_hash']),snapshot=x.c.snapshot,expected_input_hash=x.c.run['semantic_input_hash'],read_checkpoint=reader)


def test_prepared_reauthorization_rejects_changed_original_sql_bytes(renewal_case):
    x=renewal_case;changed=x.row[4]+' '
    with x.c.store._db() as db:
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(changed,x.old['child_stage_hash']))
    with pytest.raises(sem.SemanticValidationError):_append(x)
    with x.c.store._db() as db:
        row=db.execute('SELECT data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(x.old['child_stage_hash'],)).fetchone()
    assert row==(changed,None,None)


def test_original_v1_receipt_keeps_noncanonical_json_compatibility(renewal_case):
    from businessmap.semantic_review_recovery import authenticate_review_timeout_recovery
    x=renewal_case
    with x.c.store._db() as db:
        db.execute('UPDATE business_semantic_recoveries SET data=? WHERE child_stage_hash=?',(x.row[4]+' ',x.old['child_stage_hash']))
        reader=sem.semantic_checkpoint_reader(db)
        receipt=authenticate_review_timeout_recovery(reader(x.old['child_stage_hash']),snapshot=x.c.snapshot,expected_input_hash=x.c.run['semantic_input_hash'],read_checkpoint=reader)
    assert receipt['authorization_ref']==x.original['authorization_ref'] and 'authority_event' not in receipt
