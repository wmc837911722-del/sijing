"""Disposable SQLite checks for a committed claim with incomplete admission."""
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from businessmap import business_semantics as sem
from businessmap.semantic_recovery import held_project_lock
from test_review_timeout_recovery import review_timeout, review_grant
from test_proposal_timeout_recovery import source_timeout


def test_claimed_review_callback_incomplete_records_local_failure(review_timeout,tmp_path):
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])};outbound=[]
    def incomplete(*args,**kwargs):
        outbound.append('possible outbound')
        raise RuntimeError('fixture provider failed before callback')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError,match='fixture provider failed'):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        row=db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    data=json.loads(row[1]);assert row[0]=='needs_review' and row[2]=='admission_incomplete'
    assert data['failure_code']=='admission_incomplete' and data['result'] is None
    assert 'diagnostic' not in data and 'feedback' not in data and receipt[0] is not None and receipt[1]
    assert outbound==['possible outbound']


@pytest.fixture
def authorized_review(review_timeout,tmp_path):
    from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
    c=review_timeout;plan,grant,_=review_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_review_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.review_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    return c,plan


@pytest.mark.parametrize('mutation',['owner','receipt','received','request','rows_cap','bytes_cap','tables_cap','source_trigger','config_trigger','repositories_trigger','dynamic_trigger'])
def test_unsafe_finalization_retains_current_evidence(authorized_review,mutation):
    c,plan=authorized_review;key=plan['child_stage_hash']
    with c.store._db() as db:
        protected_source=db.execute('SELECT * FROM full_analysis_chunks').fetchall()
    def incomplete(*args,**kwargs):
        if mutation=='owner':c.runtime._owned_slots.discard(key)
        with c.store._db() as db:
            if mutation=='receipt':db.execute('UPDATE business_semantic_recoveries SET dispatch_id=? WHERE child_stage_hash=?',('f'*64,key))
            if mutation=='received':
                row=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone();data=json.loads(row[0])
                data['result']={'paid':'retained'};data['result_hash']=sem._hash(data['result'])
                db.execute("UPDATE business_semantic_tasks SET status='received',data=? WHERE stage_hash=?",(sem._json(data),key))
            if mutation=='request':
                row=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone();data=json.loads(row[0]);data['request']['bad']=True
                db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(sem._json(data),key))
            if mutation=='rows_cap':
                db.executemany('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',((f'extra-{n}','proposal','other','planned',1,'{}',None,'now') for n in range(8193)))
            if mutation=='bytes_cap':db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',('huge','proposal','other','planned',1,'x'*(32*1024*1024+1),None,'now'))
            if mutation=='tables_cap':
                for n in range(129):db.execute(f'CREATE TABLE extra_{n}(id TEXT)')
            actions={'source_trigger':"UPDATE full_analysis_chunks SET data='forged'",'config_trigger':"UPDATE config SET value='{}'",'repositories_trigger':"DELETE FROM repositories",'dynamic_trigger':"UPDATE business_semantic_tasks SET created_at='forged'"}
            if mutation in actions:db.execute("CREATE TRIGGER bad_terminal AFTER UPDATE ON business_semantic_tasks WHEN NEW.status='needs_review' BEGIN "+actions[mutation]+"; END")
        raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError,match='fixture failure'):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        row=db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()
        assert db.execute('SELECT * FROM full_analysis_chunks').fetchall()==protected_source
    assert row[0]==('received' if mutation=='received' else 'running') and row[2] is None
    if mutation=='received':assert json.loads(row[1])['result']=={'paid':'retained'}


def test_claimed_proposal_incomplete_never_creates_second_send(source_timeout,tmp_path):
    from test_proposal_timeout_recovery import proposal_grant
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c=source_timeout;plan,grant=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime._owned_slots.clear();c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'],plan['child_stage_hash']))
    calls=[]
    def incomplete(*args,**kwargs):calls.append(1);raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        row=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    assert row==('needs_review','admission_incomplete')
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    assert calls==[1]


from test_semantic_supersession import legacy_case, actual_grant


def test_claimed_supersession_incomplete_is_terminal(legacy_case,tmp_path):
    import threading
    from businessmap import semantic_supersession as sup
    service,snapshot,run,parent=legacy_case;plan,path=actual_grant(legacy_case,tmp_path)
    sup.authorize_catalogue_supersession(service,snapshot,run,plan,authorization=path)
    service.store.config_update('full_analysis_control:run',lambda _:{'paused':False,'cancelled':False})
    def incomplete(*args,**kwargs):raise RuntimeError('fixture failure')
    service.providers=SimpleNamespace(complete=incomplete)
    runtime=sem.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
    runtime._catalogue_tasks,runtime._task_bindings,runtime.plans,runtime._forbidden=[],{ },[],[]
    runtime._owned_slots=set();runtime._stopped=threading.Event();runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with held_project_lock('project'),pytest.raises(RuntimeError):
        runtime._call('catalogue',sem.BOUNDED_MERGE_SYSTEM,plan['new_request'],lambda x:x,context=plan['new_context'])
    with service.store._db() as db:
        row=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_supersessions WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    assert row==('needs_review','admission_incomplete') and receipt[0] is not None and receipt[1]


@pytest.mark.parametrize('feature',['foreign_key','extra_index','schema_bytes','time_limit','missing_claim'])
def test_unknown_schema_or_expired_budget_remains_unresolved(authorized_review,monkeypatch,feature):
    c,plan=authorized_review;key=plan['child_stage_hash']
    def incomplete(*args,**kwargs):
        with c.store._db() as db:
            if feature=='foreign_key':db.execute('CREATE TABLE attached_reference(id TEXT REFERENCES business_semantic_tasks(stage_hash))')
            if feature=='extra_index':db.execute('CREATE INDEX extra_status ON business_semantic_tasks(status)')
            if feature=='schema_bytes':db.execute("CREATE TABLE too_big (id TEXT DEFAULT '"+('x'*65536)+"')")
        if feature=='missing_claim':c.runtime._claimed_recovery_receipts.pop(key,None)
        if feature=='time_limit':
            calls=iter([100.0,102.0,102.0,102.0,102.0]);monkeypatch.setattr(sem.time,'monotonic',lambda:next(calls,102.0))
        raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError,match='fixture failure'):
        c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        row=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(key,)).fetchone()
    assert row==('running',None) and receipt[0] is not None and receipt[1]


def test_cannot_finalize_twice_or_change_receipt(authorized_review):
    c,plan=authorized_review;key=plan['child_stage_hash'];observed=[]
    def incomplete(*args,**kwargs):
        c.runtime._record_admission_incomplete(key,'review')
        with pytest.raises(sem.SemanticValidationError):c.runtime._record_admission_incomplete(key,'review')
        observed.append(True);raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        row=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(key,)).fetchone()
    assert observed==[True] and row==('needs_review','admission_incomplete') and receipt[0] is not None and receipt[1]


def test_admitted_provider_failure_retains_original_model_failure_path(authorized_review):
    c,plan=authorized_review
    def fail(*args,**kwargs):kwargs['before_send']();raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=fail
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:row=db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    assert row[0]=='needs_review' and row[2]=='model_call' and json.loads(row[1])['failure_code']=='model_call'


def test_attached_database_is_refused_in_schema_gate(tmp_path):
    import sqlite3,time
    db=sqlite3.connect(tmp_path/'main.sqlite');db.execute("ATTACH DATABASE ? AS other",(str(tmp_path/'other.sqlite'),));db.execute('BEGIN IMMEDIATE')
    with pytest.raises(sem.SemanticValidationError):sem._admission_failure_schema(db,time.monotonic()+1)
    db.rollback();db.close()


def test_paid_peer_received_persists_while_selected_invocation_waits(authorized_review):
    import threading
    c,plan=authorized_review;entered=threading.Event();release=threading.Event();errors=[];peer='a'*64
    def incomplete(*args,**kwargs):
        binding={'request':{'stage':'proposal'},'context':{}}
        c.runtime._task_bindings[peer]=binding;c.runtime._owned_slots.add(peer)
        initial={'request_hash':peer,'result':None,'result_hash':sem._hash(None),**binding}
        with c.store._db() as db:db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',(peer,'proposal','other','running',1,sem._json(initial),None,'fixture'))
        entered.set();assert release.wait(5)
        raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    def selected():
        try:
            with held_project_lock(c.snapshot['project']['id']):c.runtime._integrate_packet(c.packet)
        except RuntimeError as error:errors.append(str(error))
    thread=threading.Thread(target=selected);thread.start()
    assert entered.wait(5)
    c.runtime._save_task(peer,'proposal','received',{'paid_result':'retained'})
    with c.store._db() as db:row=db.execute('SELECT status,data FROM business_semantic_tasks WHERE stage_hash=?',(peer,)).fetchone()
    assert row[0]=='received' and json.loads(row[1])['result']=={'paid_result':'retained'}
    release.set();thread.join(10)
    assert not thread.is_alive() and errors==['fixture failure']
    with c.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('needs_review',)
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(peer,)).fetchone()==('received',)


def test_admission_incomplete_remains_failure_on_replay(authorized_review):
    c,plan=authorized_review;calls=[]
    def incomplete(*args,**kwargs):calls.append(1);raise RuntimeError('fixture failure')
    c.runtime.service.providers.complete=incomplete
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError):c.runtime._integrate_packet(c.packet)
    with c.store._db() as db:
        records=sem.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    with pytest.raises(sem.SemanticValidationError) as failure:
        sem.semantic_task_inventory(records,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    assert failure.value.reason_code=='admission_incomplete'
    with held_project_lock(c.snapshot['project']['id']),pytest.raises(sem.SemanticValidationError):c.runtime._integrate_packet(c.packet)
    assert calls==[1]


@pytest.mark.parametrize('case',['competing_lineage','commit_ack_lost'])
def test_conflicting_lineage_or_uncertain_commit_never_implies_safe_retry(authorized_review,case,caplog):
    from contextlib import contextmanager
    c,plan=authorized_review;key=plan['child_stage_hash'];original_db=c.store._db
    @contextmanager
    def lost_ack():
        with original_db() as db:
            yield db
        raise RuntimeError('simulated acknowledgement lost after commit')
    def incomplete(*args,**kwargs):
        if case=='competing_lineage':
            with original_db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS business_semantic_supersessions (supersession_id TEXT PRIMARY KEY,parent_stage_hash TEXT,child_stage_hash TEXT,input_hash TEXT,data TEXT,consumed_at INTEGER,dispatch_id TEXT)')
                db.execute('INSERT INTO business_semantic_supersessions VALUES (?,?,?,?,?,?,?)',('b'*64,'c'*64,key,c.run['semantic_input_hash'],'{}',123,'d'*64))
        else:c.store._db=lost_ack
        raise RuntimeError('original provider failure')
    c.runtime.service.providers.complete=incomplete
    try:
        with held_project_lock(c.snapshot['project']['id']),pytest.raises(RuntimeError,match='original provider failure'):
            c.runtime._integrate_packet(c.packet)
    finally:c.store._db=original_db
    with original_db() as db:
        state=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(key,)).fetchone()
    assert receipt[0] is not None and receipt[1]
    if case=='competing_lineage':assert state==('running',None)
    else:
        assert state==('needs_review','admission_incomplete')
        assert any(getattr(record,'commit_status',None)=='commit_unknown' and getattr(record,'reconciliation',None)=='read_only_required' for record in caplog.records)


def test_supersession_rejects_competing_recovery_receipt(legacy_case,tmp_path):
    import threading
    from businessmap import semantic_supersession as sup
    service,snapshot,run,parent=legacy_case;plan,path=actual_grant(legacy_case,tmp_path)
    sup.authorize_catalogue_supersession(service,snapshot,run,plan,authorization=path)
    service.store.config_update('full_analysis_control:run',lambda _:{'paused':False,'cancelled':False})
    def incomplete(*args,**kwargs):
        with service.store._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS business_semantic_recoveries (recovery_id TEXT PRIMARY KEY,parent_stage_hash TEXT,child_stage_hash TEXT,input_hash TEXT,data TEXT,consumed_at INTEGER,dispatch_id TEXT)')
            db.execute('INSERT INTO business_semantic_recoveries VALUES (?,?,?,?,?,?,?)',('b'*64,'c'*64,plan['child_stage_hash'],run['semantic_input_hash'],'{}',123,'d'*64))
        raise RuntimeError('fixture failure')
    service.providers=SimpleNamespace(complete=incomplete)
    runtime=sem.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
    runtime._catalogue_tasks,runtime._task_bindings,runtime.plans,runtime._forbidden=[],{},[],[]
    runtime._owned_slots=set();runtime._stopped=threading.Event();runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with held_project_lock('project'),pytest.raises(RuntimeError,match='fixture failure'):
        runtime._call('catalogue',sem.BOUNDED_MERGE_SYSTEM,plan['new_request'],lambda x:x,context=plan['new_context'])
    with service.store._db() as db:
        row=db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        receipt=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_supersessions WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
    assert row==('running',None) and receipt[0] is not None and receipt[1]
