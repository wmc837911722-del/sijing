"""SQLite/lock regressions for source preparation and paid checkpoints."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from threading import Event

from businessmap import business_semantics as sem
from test_proposal_timeout_recovery import source_timeout


def test_three_paid_returns_persist_during_source_prepare(source_timeout, monkeypatch, caplog):
    """A slow source snapshot must not hold the Store lock needed by paid returns."""
    c=source_timeout; runtime=c.runtime
    entered=Event(); release=Event(); original=sem.semantic_checkpoint_rows
    def held_capture(db,identity):
        entered.set()
        if not release.wait(10): raise AssertionError('source preparation was not released')
        monkeypatch.setattr(sem,'semantic_checkpoint_rows',original)
        return original(db,identity)
    monkeypatch.setattr(sem,'semantic_checkpoint_rows',held_capture)
    keys=[format(index,'064x') for index in (101,102,103)]
    with c.store._db() as db:
        for key in keys:
            binding={'request':deepcopy(c.parent['data']['request']),'context':deepcopy(c.parent['data']['context'])}
            runtime._task_bindings[key]=binding
            data={'request_hash':key,'result':None,'result_hash':sem._hash(None),**binding}
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                (key,'proposal',c.run['semantic_input_hash'],'running',1,sem._json(data),None,'fixture'))
            runtime._owned_slots.add(key)
    def paid_return(key):
        c.store.config_update('provider_usage',lambda value:(value or 0)+1,0)
        runtime._save_task(key,'proposal','received',{'claims':[]})
        return key
    with caplog.at_level('INFO'), ThreadPoolExecutor(max_workers=4) as pool:
        preparing=pool.submit(runtime._read_tasks)
        try:
            assert entered.wait(3)
            futures=[pool.submit(paid_return,key) for key in keys]
            completed=[future.result(timeout=2) for future in futures]
        finally:
            release.set()
            preparing.result(timeout=5)
    assert set(completed)==set(keys)
    with c.store._db() as db:
        rows=db.execute("SELECT stage_hash,status FROM business_semantic_tasks WHERE stage_hash IN (?,?,?)",keys).fetchall()
    assert set(rows)=={(key,'received') for key in keys}
    assert c.store.config_get('provider_usage')==3
    events=[row for row in caplog.records if getattr(row,'event',None)=='semantic_checkpoint_committed' and row.stage_hash in keys]
    assert len(events)==3 and {row.stage_hash for row in events}==set(keys)


def test_source_change_preserves_received_but_blocks_use(source_timeout,tmp_path):
    import hashlib
    import json
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan,authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user)
        assert payload['stage']=='proposal'
        result=independent_candidate(payload)
        with c.store._db() as db:
            db.execute("UPDATE full_analysis_chunks SET data=replace(data,'核对门店权限','改变门店权限')")
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        status,data=db.execute('SELECT status,data FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        assert status=='received' and json.loads(data)['result'] is not None
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage='review'").fetchone()[0]==0


def test_pause_after_paid_return_saves_received_without_acceptance(source_timeout,tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    def guard():
        if c.store.config_get('full_analysis_control:'+c.run['run_id'])['paused']:
            raise sem.SemanticValidationError('cache_invalid')
    c.runtime.guard=guard
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);result=independent_candidate(payload)
        c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        status,data=db.execute('SELECT status,data FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        assert status=='received' and json.loads(data)['result'] is not None
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage='review'").fetchone()[0]==0


def test_paid_result_rejects_changed_claim_receipt(source_timeout,tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);result=independent_candidate(payload)
        with c.store._db() as db:
            db.execute("UPDATE business_semantic_recoveries SET dispatch_id=?",('f'*64,))
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='running'


def test_dynamic_raw_capture_includes_current_tasks_receipts_and_membership(source_timeout):
    import pytest
    from businessmap._prepared_mutation import _capture_dynamic,_same_dynamic,_DynamicCapture
    c=source_timeout
    with c.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        before=_capture_dynamic(db,c.run['semantic_input_hash'])
        assert _same_dynamic(before,_capture_dynamic(db,c.run['semantic_input_hash']))
        db.execute("INSERT INTO business_semantic_tasks SELECT ?,stage,input_hash,status,attempt,data,error_code,created_at FROM business_semantic_tasks LIMIT 1",('e'*64,))
        assert not _same_dynamic(before,_capture_dynamic(db,c.run['semantic_input_hash']))
        db.rollback()
    with pytest.raises(sem.SemanticValidationError):
        _DynamicCapture((),c.run['semantic_input_hash'])


def test_authorization_uses_sealed_pregrant_preparation(source_timeout,tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap._source_material import prepare_source_material
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan,authorize_proposal_timeout_recovery,_prepare_authorization
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    with c.store._db() as db:
        rows=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    material=prepare_source_material(c.snapshot,c.run,read_checkpoint=rows.read_checkpoint)
    plan=proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=rows.read_checkpoint)
    prepared=_prepare_authorization(service,c.snapshot,c.run,plan,source_material=material)
    same_plan,path=proposal_grant(c,tmp_path)
    assert same_plan==plan
    receipt=authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path,source_material=material,prepared_mutation=prepared)
    assert receipt['parent_stage_hash']==plan['parent_stage_hash'] and receipt['consumed_at'] is None
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==1
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'


def test_proposal_consume_without_prepared_material_fails_closed(source_timeout,tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    with held_project_lock(c.snapshot['project']['id']),c.store._db() as db,pytest.raises(sem.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');consume_recovery(db,c.snapshot,c.run,plan['child_stage_hash'])
    with c.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None


def test_prepared_consume_rejects_forgery_and_dynamic_drift(source_timeout,tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    c.runtime._task_bindings[plan['child_stage_hash']]={'request':deepcopy(c.parent['data']['request']),
        'context':deepcopy(c.parent['data']['context']),'recovery_id':receipt['recovery_id'],'logical_task_hash':receipt['logical_task_hash']}
    prepared=c.runtime._prepare_mutation(plan['child_stage_hash'],operation='consume_recovery')
    with c.store._db() as db:
        db.execute("INSERT INTO business_semantic_tasks SELECT ?,stage,input_hash,status,attempt,data,error_code,created_at FROM business_semantic_tasks WHERE stage_hash=?",('e'*64,plan['parent_stage_hash']))
    for candidate in (object(),prepared,prepared):
        with pytest.raises(sem.SemanticValidationError):
            with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
                db.execute('BEGIN IMMEDIATE')
                consume_recovery(db,c.snapshot,c.run,plan['child_stage_hash'],source_material=c.runtime._source_material,prepared_mutation=candidate)
    with c.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'


def test_known_peer_transition_reprepares_before_any_send(source_timeout,tmp_path,monkeypatch):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate,review
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout; parent=c.parent; request=deepcopy(parent['data']['request']);context=deepcopy(parent['data']['context'])
    request['membership_hash']='f'*64;context['packet_hash']=sem._hash(request)
    peer=sem.semantic_task_identity_hash(c.snapshot,c.run['semantic_input_hash'],'proposal',request,context)
    peer_data={'request_hash':peer,'request':request,'context':context,'result':None,'result_hash':sem._hash(None)}
    with c.store._db() as db:
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',(peer,'proposal',c.run['semantic_input_hash'],'planned',1,sem._json(peer_data),None,'fixture'))
    plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    original=c.runtime._prepare_mutation;attempts=[];calls=[]
    def prepare(stage_hash,*,operation):
        value=original(stage_hash,operation=operation);attempts.append(operation)
        if attempts==['consume_recovery']:
            with c.store._lock,c.store._db() as db:
                db.execute("UPDATE business_semantic_tasks SET status='running' WHERE stage_hash=?",(peer,))
                c.runtime._owned_slots.add(peer)
        return value
    monkeypatch.setattr(c.runtime,'_prepare_mutation',prepare)
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']):
        result=c.runtime._integrate_packet(c.packet)
    assert result['review_hash'] and attempts.count('consume_recovery')==2 and calls==['proposal','review']


def test_claim_rollback_never_records_owned_receipt_or_sends(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    import sqlite3
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    sent=[];c.runtime.service.providers.complete=lambda *args,**kwargs:sent.append(args)
    with c.store._db() as db:
        db.execute("CREATE TRIGGER reject_claim BEFORE UPDATE OF status ON business_semantic_tasks WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'fixture claim failure'); END")
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sqlite3.IntegrityError):
        c.runtime._integrate_packet(c.packet)
    assert plan['child_stage_hash'] not in c.runtime._owned_slots
    assert plan['child_stage_hash'] not in c.runtime._claimed_recovery_receipts and not sent
    with c.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None


def test_authorize_rolls_back_source_mutation_trigger(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    with c.store._db() as db:
        original=db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]
        db.execute("CREATE TRIGGER tamper_source AFTER INSERT ON business_semantic_tasks WHEN NEW.stage='proposal' BEGIN UPDATE full_analysis_chunks SET data=data || ' '; END")
    with pytest.raises(sem.SemanticValidationError):
        authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    with c.store._db() as db:
        assert db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]==original
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_semantic_recoveries'").fetchone()
        assert not exists or db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==0


def test_consume_rolls_back_unrelated_trigger_mutation(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_proposal_timeout_recovery import proposal_grant, _prepare_proposal_consume, _consume_prepared_fixture
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    prepared=_prepare_proposal_consume(c,plan)
    with c.store._db() as db:
        original=db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]
        db.execute("CREATE TRIGGER consume_tamper AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.status='running' BEGIN UPDATE full_analysis_chunks SET data=data || ' '; END")
    with pytest.raises(sem.SemanticValidationError):
        with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
            _consume_prepared_fixture(db,c,plan,prepared)
    with c.store._db() as db:
        assert db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]==original
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None


def test_prepared_concurrent_authorizers_share_only_identical_child(source_timeout,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap._source_material import prepare_source_material
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery, _prepare_authorization
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    with c.store._db() as db:
        rows=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    material=prepare_source_material(c.snapshot,c.run,read_checkpoint=rows.read_checkpoint)
    prepared=[_prepare_authorization(service,c.snapshot,c.run,plan,source_material=material) for _ in range(2)]
    barrier=Barrier(2)
    def authorize(index):
        barrier.wait()
        return authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path,
            source_material=material,prepared_mutation=prepared[index])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=list(pool.map(authorize,range(2)))
    assert first==second and first['consumed_at'] is None
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==1


def test_peer_drift_stops_after_three_preparations_without_send(source_timeout,tmp_path,monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    original=c.runtime._prepare_mutation;attempts=[];sent=[]
    def prepare(stage_hash,*,operation):
        prepared=original(stage_hash,operation=operation)
        if operation=='consume_recovery':
            attempts.append(stage_hash)
            request=deepcopy(c.parent['data']['request']);context=deepcopy(c.parent['data']['context'])
            request['membership_hash']=str(len(attempts))*64;context['packet_hash']=sem._hash(request)
            peer=sem.semantic_task_identity_hash(c.snapshot,c.run['semantic_input_hash'],'proposal',request,context)
            data={'request_hash':peer,'request':request,'context':context,'result':None,'result_hash':sem._hash(None)}
            with c.store._lock,c.store._db() as db:
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',(peer,'proposal',c.run['semantic_input_hash'],'running',1,sem._json(data),None,'fixture'))
                c.runtime._owned_slots.add(peer)
        return prepared
    monkeypatch.setattr(c.runtime,'_prepare_mutation',prepare)
    c.runtime.service.providers.complete=lambda *args,**kwargs:sent.append(args)
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert len(attempts)==3 and not sent
    with c.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'


def test_paid_result_rejects_forged_claim_evidence(source_timeout,tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user)
        c.runtime._claimed_recovery_receipts[plan['child_stage_hash']]=dict(receipt)
        return {'text':json.dumps(independent_candidate(payload),ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='running'


@pytest.mark.parametrize('mutation', ['parent', 'source', 'extra'])
def test_promotion_rejects_trigger_drift_preserves_paid_received(source_timeout,tmp_path,mutation):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate,review
    from test_proposal_timeout_recovery import proposal_grant
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    with c.store._db() as db:
        parent=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(plan['parent_stage_hash'],)).fetchone()
        source=db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]
        if mutation=='parent': action="UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash='"+plan['parent_stage_hash']+"'"
        elif mutation=='source': action="UPDATE full_analysis_chunks SET data=data || ' '"
        else: action="INSERT INTO business_semantic_tasks VALUES ('extra','review','foreign','planned',1,'{}',NULL,'fixture')"
        db.execute("CREATE TRIGGER drift_after_promotion AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.stage_hash='"+plan['child_stage_hash']+"' AND NEW.status='proposed' BEGIN "+action+"; END")
    calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
        value=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
        return {'text':json.dumps(value,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(plan['parent_stage_hash'],)).fetchone()==parent
        assert db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]==source
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('received',)
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage_hash='extra'").fetchone()[0]==0
    assert calls==['proposal']


from test_semantic_supersession import legacy_case
from test_interrupted_catalogue_recovery import interrupted_recovery_case, bounded_recovery_case


@pytest.mark.parametrize('phase,mutation', [('consume','parent'),('consume','source'),('consume','extra'),('accepted','parent'),('accepted','source'),('accepted','extra')])
def test_supersession_postwrite_rejects_trigger_drift(legacy_case,tmp_path,phase,mutation):
    import json
    from types import SimpleNamespace
    from businessmap import semantic_supersession as sup
    from businessmap.semantic_recovery import held_project_lock
    from test_semantic_supersession import actual_grant
    service,snapshot,run,parent=legacy_case
    plan,path=actual_grant(legacy_case,tmp_path)
    sup.authorize_catalogue_supersession(service,snapshot,run,plan,authorization=path)
    service.store.config_set('full_analysis_control:run',{'paused':False,'cancelled':False})
    sent=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();sent.append(True)
        return {'text':json.dumps({'operations':[]}), 'usage':{},'output_limited':False}
    service.providers=SimpleNamespace(complete=complete)
    runtime=sem.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
    from threading import Event
    runtime._stopped=Event()
    runtime._owned_slots=set()
    runtime._catalogue_tasks,runtime._task_bindings,runtime.plans,runtime._forbidden=[],{},[],[]
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with service.store._db() as db:
        old_parent=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(parent,)).fetchone()
        old_source=db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]
        if mutation=='parent': action="UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash='"+parent+"'"
        elif mutation=='source': action="UPDATE full_analysis_chunks SET data=data || ' '"
        else: action="INSERT INTO business_semantic_tasks VALUES ('extra','review','foreign','planned',1,'{}',NULL,'fixture')"
        status='running' if phase=='consume' else 'accepted'
        db.execute("CREATE TRIGGER drift_legacy AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.stage_hash='"+plan['child_stage_hash']+"' AND NEW.status='"+status+"' BEGIN "+action+"; END")
    with held_project_lock(snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):
        runtime._call('catalogue',sem.BOUNDED_MERGE_SYSTEM,plan['new_request'],lambda value:value,context=plan['new_context'])
    with service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(parent,)).fetchone()==old_parent
        assert db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0]==old_source
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage_hash='extra'").fetchone()[0]==0
        row=db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        receipt=db.execute('SELECT consumed_at FROM business_semantic_supersessions WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    assert row==(('planned',) if phase=='consume' else ('received',))
    assert (receipt[0] is None)==(phase=='consume')
    assert len(sent)==(0 if phase=='consume' else 1)
    if phase=='consume': assert plan['child_stage_hash'] not in runtime._owned_slots


@pytest.mark.parametrize('mutation',['parent','source','extra'])
def test_legacy_recovery_consume_rejects_trigger_drift(interrupted_recovery_case,mutation):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    c=interrupted_recovery_case
    authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    with c.service.store._db() as db:
        parent=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()
        source=db.execute('SELECT data FROM full_analysis_chunks ORDER BY rowid').fetchall()
        if mutation=='parent': action="UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash='"+c.parent_stage_hash+"'"
        elif mutation=='source': action="UPDATE full_analysis_chunks SET data=data || ' '"
        else: action="INSERT INTO business_semantic_tasks VALUES ('extra','review','foreign','planned',1,'{}',NULL,'fixture')"
        db.execute("CREATE TRIGGER drift_recovery AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.stage_hash='"+c.plan['child_stage_hash']+"' AND NEW.status='running' BEGIN "+action+"; END")
    calls=len(c.calls);result=c.service.analyze(c.project_id)
    assert result['status']!='completed'
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()==parent
        assert db.execute('SELECT data FROM full_analysis_chunks ORDER BY rowid').fetchall()==source
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE stage_hash='extra'").fetchone()[0]==0
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==('planned',)
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries WHERE child_stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==(None,)
    assert len(c.calls)==calls


def test_legacy_recovery_accepted_rollback_retains_paid_received(interrupted_recovery_case):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    c=interrupted_recovery_case
    authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    with c.service.store._db() as db:
        parent=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()
        db.execute("CREATE TRIGGER drift_accepted AFTER UPDATE OF status ON business_semantic_tasks WHEN NEW.stage_hash='"+c.plan['child_stage_hash']+"' AND NEW.status='accepted' BEGIN UPDATE business_semantic_tasks SET created_at='tampered' WHERE stage_hash='"+c.parent_stage_hash+"'; END")
    calls=len(c.calls);result=c.service.analyze(c.project_id)
    assert result['status']!='completed' and len(c.calls)==calls+1
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()==parent
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==('received',)
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries WHERE child_stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()[0] is not None


def test_legacy_compiler_stamp_tracks_transitive_url_normalizer(monkeypatch):
    import urllib.parse
    from businessmap._prepared_mutation import _legacy_compiler_stamp
    from businessmap.business_content import _canonical
    before=_legacy_compiler_stamp()
    assert before is not None and _canonical('%41')=='A'
    monkeypatch.setattr(urllib.parse,'_unquote_impl',lambda value:b'changed')
    after=_legacy_compiler_stamp()
    assert _canonical('%41')=='changed'
    assert after is None or after!=before
