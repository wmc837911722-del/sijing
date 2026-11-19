"""Real disposable SQL, process interruption and committed structured-log evidence."""
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import pytest
from businessmap import business_semantics as s, semantic_recovery as recovery
from test_bounded_merge_recovery import bounded_parent, bounded_recovery_case
from test_semantic_supersession import legacy_case
from test_semantic_recovery import case, grant as old_grant

NEW_POLICY = 'operator-bounded-catalogue-interrupted-once-v1'


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def interrupted_grant(c, tmp_path):
    with c.service.store._db() as db:
        plan = recovery.catalogue_recovery_plan(c.snapshot, c.run, c.parent_stage_hash,
            read_checkpoint=s.semantic_checkpoint_reader(db))
    events = [json.loads(line) for line in c.events_path.read_text().splitlines()]
    dispatch = dict(run_id=c.run['run_id'], input_hash=c.run['semantic_input_hash'],
        parent_stage_hash=c.parent_stage_hash, parent_row_digest=plan['parent_failure_digest'],
        events_ref=str(c.events_path), events_hash=hashlib.sha256(c.events_path.read_bytes()).hexdigest(),
        reserved_event_index=len(events)-2, started_event_index=len(events)-1)
    evidence = tmp_path/'dispatch.json'
    digest = private_json(evidence, dispatch)
    check = dict(checked_at=int(time.time()), local_owner_pids=[c.owner_pid], local_processes_drained=True,
        upstream_result_or_billing_reconciled=False, run_id=c.run['run_id'],
        parent_stage_hash=c.parent_stage_hash, parent_row_digest=plan['parent_failure_digest'],
        dispatch_evidence_ref=str(evidence), dispatch_evidence_hash=digest,
        result_recovered=False, duplicate_call_risk_accepted=True)
    reconciliation = tmp_path/'interrupted-reconcile.json'
    digest = private_json(reconciliation, check)
    stamp = int(time.time())
    auth = {key:plan[key] for key in ('policy','plan_hash','parent_stage_hash','run_id')}
    auth.update(operator_id='root', authorization_source='user-fix-and-continue', authorized_at=stamp,
        expires_at=stamp+3600, max_uses=1, reconciliation_ref=str(reconciliation), reconciliation_hash=digest)
    grant = tmp_path/'interrupted-grant.json'
    private_json(grant, auth)
    return plan, grant


@pytest.fixture
def interrupted_recovery_case(bounded_recovery_case, tmp_path):
    """64 real facts, third old recovery, then real process death in ordinary send."""
    c = bounded_recovery_case
    old_plan, path = old_grant((c.service,c.snapshot,c.run,c.parent_stage_hash),tmp_path,persist=False)
    recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,old_plan,authorization=path)
    c.events_path = tmp_path/'interrupted-events.jsonl'
    c.events_path.touch(mode=0o600)
    calls_path = tmp_path/'child-calls.jsonl'
    original_complete = c.tools.providers.complete
    owner = os.fork()
    if owner == 0:
        # Build the historical interrupted merge only inside this child process.
        s.BusinessSemanticIntegration._catalogue = s.BusinessSemanticIntegration._catalogue_legacy
        class Capture(logging.Handler):
            def emit(self, record):
                if getattr(record,'event',None) == 'semantic_slot_reserved':
                    value = {k:getattr(record,k) for k in ('event','stage','status','attempt','stage_hash','input_hash')}
                    value['at'] = record.created
                    with c.events_path.open('a') as stream: stream.write(json.dumps(value)+'\n')
        log = logging.getLogger('businessmap.business_semantics')
        log.setLevel(logging.INFO)
        log.addHandler(Capture())
        def complete(system, user, **kwargs):
            payload = json.loads(user)
            with calls_path.open('a') as stream: stream.write(json.dumps(payload)+'\n')
            if payload.get('stage') == 'catalogue' and payload.get('merge_contract'):
                with c.service.store._db() as db:
                    rows = s.semantic_checkpoint_reader(db).task_rows(c.run['semantic_input_hash'])
                normal = [r for r in rows if r['status']=='running' and r['data']['request']==payload
                    and not {'recovery_id','supersession_id'}.intersection(r['data'])]
                if normal:
                    kwargs['before_send']()
                    with c.events_path.open('a') as stream:
                        stream.write(json.dumps(dict(stage='catalogue',state='started',at=time.time()))+'\n')
                    os._exit(73)
            return original_complete(system,user,**kwargs)
        c.tools.providers.complete = complete
        c.service.set_paused(c.project_id,False)
        c.service.analyze(c.project_id)
        os._exit(74)
    waited, status = os.waitpid(owner,0)
    assert waited == owner and os.waitstatus_to_exitcode(status) == 73
    c.owner_pid = owner
    c.calls.extend(json.loads(line) for line in calls_path.read_text().splitlines())
    c.service.set_paused(c.project_id,True)
    with c.service.store._db() as db:
        rows = s.semantic_checkpoint_reader(db).task_rows(c.run['semantic_input_hash'])
        unknown = [r for r in rows if r['status']=='running']
        assert len(unknown)==1 and unknown[0]['data']['result'] is None
        c.parent_stage_hash=unknown[0]['stage_hash']
        c.old_rows=db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
        c.old_recoveries=db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()
        assert len(c.old_recoveries)==3
        assert db.execute('SELECT error_code FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()==(None,)
    c.plan,c.grant=interrupted_grant(c,tmp_path)
    yield c


def test_raw_error_seam_missing_is_not_null_and_frozen_is_same_snapshot(case):
    service,snapshot,run,key=case
    with service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        assert callable(getattr(reader,'raw_error_code',None)), 'raw SQL error seam is missing'
        assert reader.raw_error_code(key)=='model_call'
        with pytest.raises(s.SemanticValidationError):reader.raw_error_code('f'*64)
        frozen=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
    with service.store._db() as db:db.execute('UPDATE business_semantic_tasks SET error_code=NULL')
    assert frozen.read_checkpoint.raw_error_code(key)=='model_call'
    with pytest.raises(s.SemanticValidationError):frozen.read_checkpoint.raw_error_code('f'*64)


def test_running_bounded_policy_is_additive_and_old_plans_are_identical(bounded_parent,case):
    import importlib.util
    baseline=Path(__file__).parent/'fixtures/semantic_recovery_before_interrupted_catalogue.py'
    spec=importlib.util.spec_from_file_location('businessmap._before_interrupted_recovery',baseline)
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    for fixture in (case,bounded_parent):
        service,snapshot,run,key=fixture
        with service.store._db() as db:
            reader=s.semantic_checkpoint_reader(db)
            assert recovery.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=reader)==old.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=reader)
    service,snapshot,run,key=bounded_parent
    with service.store._db() as db:row=s.semantic_checkpoint_reader(db)(key)
    row['status']='running';row['data'].pop('diagnostic');row['data'].pop('failure_code')
    assert recovery._recovery_policy(row,snapshot)==NEW_POLICY


@pytest.fixture
def completed_interrupted_recovery(interrupted_recovery_case,caplog):
    c=interrupted_recovery_case
    with caplog.at_level('INFO'):
        c.new_receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
        paid=len(c.calls)
        c.service.set_paused(c.project_id,False)
        result=c.service.analyze(c.project_id)
    assert result['status']=='completed' and result['publication_complete'],result.get('synthesis_failure')
    c.projection=c.service._load(c.project_id)['synthesis']
    c.new_calls=c.calls[paid:]
    with c.service.store._db() as db:
        c.tasks=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        keys=[row[0] for row in c.old_rows]
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash IN (%s) ORDER BY stage_hash'%','.join('?' for _ in keys),keys).fetchall()==c.old_rows
        ids=[row[0] for row in c.old_recoveries]
        assert db.execute('SELECT * FROM business_semantic_recoveries WHERE recovery_id IN (?,?,?) ORDER BY recovery_id',ids).fetchall()==c.old_recoveries
        assert db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()==c.old_supersessions
        assert db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()==c.old_l0
        assert db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()==c.old_source
    receipts=c.tasks.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])
    assert len(receipts)==4
    import importlib.util
    baseline=Path(__file__).parent/'fixtures/semantic_recovery_before_interrupted_catalogue.py'
    spec=importlib.util.spec_from_file_location('businessmap._before_interrupted_full',baseline)
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    for receipt in receipts:
        if receipt['policy']==NEW_POLICY:continue
        assert recovery.catalogue_recovery_plan(c.snapshot,c.run,receipt['parent_stage_hash'],read_checkpoint=c.tasks.read_checkpoint)==old.catalogue_recovery_plan(c.snapshot,c.run,receipt['parent_stage_hash'],read_checkpoint=c.tasks.read_checkpoint)
        assert recovery.receipt_hash(receipt)==old.receipt_hash(receipt)
    old_requests=[json.loads(raw[5])['request'] for raw in c.old_rows if raw[3]=='accepted']
    assert all(payload not in old_requests for payload in c.new_calls)
    assert all(p.get('stage') in {'catalogue','proposal','review'} and p.get('mode')!='leaf' for p in c.new_calls)
    s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)
    s.validate_stored_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.service.store)
    s.semantic_support_context(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    yield c


def test_four_recoveries_one_supersession_complete_without_repay(completed_interrupted_recovery,caplog):
    c=completed_interrupted_recovery
    assert c.plan['policy']==NEW_POLICY
    assert c.tasks.read_checkpoint(c.parent_stage_hash)['status']=='running'
    assert c.tasks.read_checkpoint(c.plan['child_stage_hash'])['status']=='accepted'
    assert s.semantic_task_inventory(c.tasks,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])['occupied_calls']==len(c.tasks)
    for status in ('authorized','consumed','received','accepted'):
        events=[r for r in caplog.get_records('setup')+caplog.records if getattr(r,'event',None)=='semantic_recovery_'+status and getattr(r,'recovery_id',None)==c.plan['recovery_id']]
        assert len(events)==1 and events[0].policy==NEW_POLICY

@pytest.fixture
def evidence_case(tmp_path):
    """Pure fresh evidence seam, independent literal event/plan bindings."""
    from types import SimpleNamespace
    stamp=int(time.time())
    plan=dict(policy=NEW_POLICY,plan_hash='b'*64,parent_stage_hash='a'*64,
        parent_failure_digest='d'*64,run_id='run',input_hash='c'*64)
    events=[dict(event='semantic_slot_reserved',stage='catalogue',status='running',attempt=1,
        stage_hash='a'*64,input_hash='c'*64,at=stamp-1),
        dict(stage='catalogue',state='started',at=stamp)]
    event_path=tmp_path/'events.jsonl'
    event_path.write_text(''.join(json.dumps(e)+'\n' for e in events));event_path.chmod(0o600)
    dispatch=dict(run_id='run',input_hash='c'*64,parent_stage_hash='a'*64,parent_row_digest='d'*64,
        events_ref=str(event_path),events_hash=hashlib.sha256(event_path.read_bytes()).hexdigest(),
        reserved_event_index=0,started_event_index=1)
    dispatch_path=tmp_path/'dispatch.json'
    check=dict(checked_at=stamp,local_owner_pids=[99999999],local_processes_drained=True,
        upstream_result_or_billing_reconciled=False,run_id='run',parent_stage_hash='a'*64,parent_row_digest='d'*64,
        dispatch_evidence_ref=str(dispatch_path),dispatch_evidence_hash=private_json(dispatch_path,dispatch),
        result_recovered=False,duplicate_call_risk_accepted=True)
    check_path=tmp_path/'check.json'
    auth={key:plan[key] for key in ('policy','plan_hash','parent_stage_hash','run_id')}
    auth.update(operator_id='root',authorization_source='user-fix-and-continue',authorized_at=stamp,
        expires_at=stamp+3600,max_uses=1,reconciliation_ref=str(check_path),reconciliation_hash=private_json(check_path,check))
    grant_path=tmp_path/'grant.json';private_json(grant_path,auth)
    c=SimpleNamespace(plan=plan,auth=auth,check=check,dispatch=dispatch,events=events,
        grant_path=grant_path,check_path=check_path,dispatch_path=dispatch_path,event_path=event_path)
    return c


def rebind_evidence(c):
    c.event_path.write_text(''.join(json.dumps(e)+'\n' for e in c.events))
    c.dispatch['events_hash']=hashlib.sha256(c.event_path.read_bytes()).hexdigest()
    c.check['dispatch_evidence_hash']=private_json(c.dispatch_path,c.dispatch)
    c.auth['reconciliation_hash']=private_json(c.check_path,c.check)
    private_json(c.grant_path,c.auth)


def test_new_evidence_validates_before_database_construction(evidence_case):
    c=evidence_case
    result=recovery._check_grant(c.grant_path,c.plan)
    assert result=={**c.auth,'authorization_ref':hashlib.sha256(c.grant_path.read_bytes()).hexdigest()}


CHECK_FIELDS=('checked_at local_owner_pids local_processes_drained upstream_result_or_billing_reconciled '
    'run_id parent_stage_hash parent_row_digest dispatch_evidence_ref dispatch_evidence_hash '
    'result_recovered duplicate_call_risk_accepted').split()
DISPATCH_FIELDS=('run_id input_hash parent_stage_hash parent_row_digest events_ref events_hash '
    'reserved_event_index started_event_index').split()

@pytest.mark.parametrize('kind,field', [('check',f) for f in CHECK_FIELDS]+[('dispatch',f) for f in DISPATCH_FIELDS]+[('auth',f) for f in sorted(recovery.AUTH_FIELDS)])
def test_all_new_evidence_fields_required(evidence_case,kind,field):
    c=evidence_case
    getattr(c,kind).pop(field)
    # Rebind only the containing layers; missing hash/ref fields stay missing.
    if kind=='dispatch':
        c.check['dispatch_evidence_hash']=private_json(c.dispatch_path,c.dispatch)
    if kind!='auth':c.auth['reconciliation_hash']=private_json(c.check_path,c.check)
    private_json(c.grant_path,c.auth)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('kind,field,value', [
    ('check','extra',1),('dispatch','extra',1),('auth','extra',1),
    ('check','checked_at',True),('check','checked_at',0),('check','checked_at',2**63),
    ('check','local_owner_pids',[]),('check','local_owner_pids',[True]),('check','local_owner_pids',[0]),
    ('check','local_owner_pids',[-1]),('check','local_owner_pids',[99999999,99999999]),
    ('check','local_owner_pids',[os.getpid()]),('check','local_processes_drained',1),
    ('check','upstream_result_or_billing_reconciled',True),('check','result_recovered',True),
    ('check','duplicate_call_risk_accepted',False),('check','run_id','foreign'),
    ('check','parent_stage_hash','e'*64),('check','parent_row_digest','e'*64),
    ('dispatch','run_id','foreign'),('dispatch','input_hash','e'*64),
    ('dispatch','parent_stage_hash','e'*64),('dispatch','parent_row_digest','e'*64),
    ('dispatch','reserved_event_index',True),('dispatch','reserved_event_index',-1),
    ('dispatch','started_event_index',True),('dispatch','started_event_index',0),('dispatch','started_event_index',2),
    ('auth','policy','operator-bounded-catalogue-provider502-once-v1'),('auth','max_uses',True),
    ('auth','authorized_at',True),('auth','expires_at',0),('auth','operator_id','model')])
def test_new_evidence_values_are_closed(evidence_case,kind,field,value):
    c=evidence_case;getattr(c,kind)[field]=value
    rebind_evidence(c)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('index,field,value', [
    (0,'event','other'),(0,'stage','proposal'),(0,'status','received'),(0,'attempt',True),
    (0,'attempt',2),(0,'stage_hash','e'*64),(0,'input_hash','e'*64),(0,'at',True),
    (0,'at',float('nan')),(0,'at',float('inf')),(0,'at',-1),
    (1,'stage','review'),(1,'state','returned'),(1,'at',0),(1,'at',True),
    (1,'at',float('inf')),(1,'at',2**63)])
def test_dispatch_final_pair_binds_real_request_and_finite_time(evidence_case,index,field,value):
    c=evidence_case;c.events[index][field]=value;rebind_evidence(c)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('mutation',['missing_reserved','missing_start','trailing_return','trailing_start','trailing_unrelated','late_append','recovered'])
def test_missing_or_later_event_requires_new_reconciliation(evidence_case,mutation):
    c=evidence_case
    if mutation=='missing_reserved':c.events.pop(0)
    elif mutation=='missing_start':c.events.pop()
    elif mutation=='recovered':c.check['result_recovered']=True
    elif mutation=='late_append':
        with c.event_path.open('a') as stream:stream.write('{}\n')
        with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)
        return
    else:c.events.append(dict(stage='catalogue',state='returned' if mutation=='trailing_return' else 'started',at=time.time()))
    rebind_evidence(c)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('name',['grant_path','check_path','dispatch_path','event_path'])
@pytest.mark.parametrize('kind',['mode','symlink','directory'])
def test_every_new_file_is_regular_private_and_nofollow(evidence_case,name,kind,tmp_path):
    c=evidence_case;path=getattr(c,name)
    if kind=='mode':path.chmod(0o644)
    elif kind=='symlink':
        saved=tmp_path/(name+'-original');path.rename(saved);path.symlink_to(saved)
    else:path.unlink();path.mkdir(mode=0o600)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('path,value', [
    ('stage','proposal'),('attempt',True),('attempt',2),('status','planned'),('status','received'),
    ('status','accepted'),('status','needs_review'),('data.request.stage','review'),
    ('data.request.mode','leaf'),('data.context.catalogue_mode','leaf'),
    ('data.request.merge_contract','foreign'),('data.context.merge_contract','foreign'),
    ('data.failure_code',None),('data.diagnostic',None),('data.recovery_id',None),
    ('data.logical_task_hash',None),('data.supersession_id',None),('data.result',[]),('data.result_hash','e'*64)])
def test_interrupted_policy_cannot_claim_failures_children_or_other_stages(bounded_parent,path,value):
    service,snapshot,run,key=bounded_parent
    with service.store._db() as db:row=s.semantic_checkpoint_reader(db)(key)
    row['status']='running';row['data'].pop('diagnostic');row['data'].pop('failure_code')
    target=row;parts=path.split('.')
    for part in parts[:-1]:target=target[part]
    target[parts[-1]]=value
    with pytest.raises(s.SemanticValidationError):recovery._recovery_policy(row,snapshot)


def test_real_authorization_atomic_concurrent_and_evidence_rechecked(interrupted_recovery_case,monkeypatch,caplog):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    c=interrupted_recovery_case
    with c.service.store._db() as db:
        db.execute("CREATE TRIGGER abort_child BEFORE INSERT ON business_semantic_tasks WHEN NEW.status='planned' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with caplog.at_level('INFO'),pytest.raises(sqlite3.IntegrityError):
        recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()==c.old_rows
        assert db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()==c.old_recoveries
        db.execute('DROP TRIGGER abort_child')
    assert not any(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)
    original=recovery._check_grant
    raw=c.events_path.read_bytes();checked=[]
    def race(*args):
        result=original(*args)
        if not checked:
            checked.append(True)
            with c.events_path.open('a') as stream:stream.write('{}\n')
        return result
    monkeypatch.setattr(recovery,'_check_grant',race)
    with pytest.raises(s.SemanticValidationError):
        recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.events_path.write_bytes(raw)
    monkeypatch.setattr(recovery,'_check_grant',original)
    from businessmap.providers import ToolError
    def authorize(_):
        try:return recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
        except ToolError as error:
            assert error.status_code==409
            return None
    with caplog.at_level('INFO'),ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(authorize,range(2)))
    assert any(result)
    receipt=next(r for r in result if r)
    assert recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)==receipt
    events=[r for r in caplog.records if getattr(r,'event',None)=='semantic_recovery_authorized']
    assert len(events)==1 and events[0].child_stage_hash==c.plan['child_stage_hash'] and events[0].policy==NEW_POLICY
    with c.service.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==('planned',)
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE recovery_id=?',(c.plan['recovery_id'],)).fetchone()==(None,None)
        reader=s.semantic_checkpoint_reader(db)
        recovery.authenticate_recovery(reader(c.plan['child_stage_hash']),snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)
    # Planned preflight must reject evidence tampering before constructing Store.
    with c.events_path.open('a') as stream:stream.write('{}\n')
    with c.service.store._db() as db,pytest.raises(s.SemanticValidationError):
        reader=s.semantic_checkpoint_reader(db)
        recovery.authenticate_recovery(reader(c.plan['child_stage_hash']),snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)


def test_real_consumption_rollback_current_scope_live_pid_and_once(interrupted_recovery_case,tmp_path,monkeypatch,caplog):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    c=interrupted_recovery_case
    recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    with c.service.store._db() as db:
        db.execute("CREATE TRIGGER abort_consume BEFORE UPDATE OF status ON business_semantic_tasks WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with recovery.held_project_lock(c.project_id),pytest.raises(sqlite3.IntegrityError),c.service.store._db() as db:
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
    with c.service.store._db() as db:
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE recovery_id=?',(c.plan['recovery_id'],)).fetchone()==(None,None)
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==('planned',)
        db.execute('DROP TRIGGER abort_consume')
    # Simulate a reused owner PID at the OS observation seam, no process control.
    original_kill=recovery.os.kill
    def reused(pid,signal):
        if pid==c.owner_pid:return None
        return original_kill(pid,signal)
    monkeypatch.setattr(recovery.os,'kill',reused)
    with recovery.held_project_lock(c.project_id),c.service.store._db() as db,pytest.raises(s.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
    monkeypatch.setattr(recovery.os,'kill',original_kill)
    with c.service.store._db() as db:
        raw=db.execute('SELECT data FROM full_analysis_runs WHERE id=?',(c.run['run_id'],)).fetchone()[0]
        altered=json.loads(raw);altered['project_revision']='foreign'
        db.execute('UPDATE full_analysis_runs SET data=? WHERE id=?',(s._json(altered),c.run['run_id']))
    with recovery.held_project_lock(c.project_id),c.service.store._db() as db,pytest.raises(s.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
    with c.service.store._db() as db:db.execute('UPDATE full_analysis_runs SET data=? WHERE id=?',(raw,c.run['run_id']))
    def consume(_):
        try:
            with c.service.store._db() as db:
                db.execute('BEGIN IMMEDIATE')
                result=recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
            recovery.log_event('consumed',result)
            return result
        except s.SemanticValidationError:return None
    with caplog.at_level('INFO'),recovery.held_project_lock(c.project_id),ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(consume,range(2)))
    assert sum(r is not None for r in results)==1
    result=next(r for r in results if r)
    with c.service.store._db() as db:
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE recovery_id=?',(c.plan['recovery_id'],)).fetchone()==(result['consumed_at'],result['dispatch_id'])
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()==('running',)
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    events=[r for r in caplog.records if getattr(r,'event',None)=='semantic_recovery_consumed']
    assert len(events)==1 and events[0].dispatch_id==result['dispatch_id'] and events[0].policy==NEW_POLICY
    with pytest.raises(s.SemanticValidationError) as error:
        s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    assert error.value.reason_code=='interrupted'
    with pytest.raises(s.SemanticValidationError):
        recovery.catalogue_recovery_plan(c.snapshot,c.run,c.plan['child_stage_hash'],read_checkpoint=rows.read_checkpoint)


def test_raw_parent_null_is_required_and_missing_reader_rejects(interrupted_recovery_case):
    c=interrupted_recovery_case
    with c.service.store._db() as db:
        original=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()
        db.execute("UPDATE business_semantic_tasks SET error_code='model_call' WHERE stage_hash=?",(c.parent_stage_hash,))
    with c.service.store._db() as db,pytest.raises(s.SemanticValidationError):
        recovery.catalogue_recovery_plan(c.snapshot,c.run,c.parent_stage_hash,read_checkpoint=s.semantic_checkpoint_reader(db))
    with c.service.store._db() as db:db.execute('UPDATE business_semantic_tasks SET error_code=NULL WHERE stage_hash=?',(c.parent_stage_hash,))
    with c.service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        def incomplete(key):return reader(key)
        for name in ('raw_digest','recovery_receipts','supersession_receipts','catalogue_source','catalogue_runs','task_rows'):
            setattr(incomplete,name,getattr(reader,name))
        with pytest.raises(s.SemanticValidationError):
            recovery.catalogue_recovery_plan(c.snapshot,c.run,c.parent_stage_hash,read_checkpoint=incomplete)
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()==original
    with c.service.store._db() as db:
        db.execute("UPDATE business_semantic_tasks SET created_at='changed-after-plan' WHERE stage_hash=?",(c.parent_stage_hash,))
    with pytest.raises(s.SemanticValidationError):
        recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)


def test_expired_planned_rejects_and_consumed_received_history_is_free(interrupted_recovery_case,monkeypatch):
    c=interrupted_recovery_case
    receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    real_clock=time.time
    monkeypatch.setattr(time,'time',lambda:receipt['expires_at']+1)
    with recovery.held_project_lock(c.project_id),c.service.store._db() as db,pytest.raises(s.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
    monkeypatch.setattr(time,'time',real_clock)
    original=s.BusinessSemanticIntegration._save_task
    def stop_after_received(self,key,stage,status,*args,**kwargs):
        if key==c.plan['child_stage_hash'] and status=='accepted':raise RuntimeError('stop after durable received')
        return original(self,key,stage,status,*args,**kwargs)
    monkeypatch.setattr(s.BusinessSemanticIntegration,'_save_task',stop_after_received)
    result=c.service.analyze(c.project_id)
    assert result['status']!='completed'
    with c.service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        row=reader(c.plan['child_stage_hash']);assert row['status']=='received'
    paid=len(c.calls)
    # Consumed ancestry authenticates without historical files or current expiry.
    c.events_path.unlink();c.grant.unlink()
    Path(receipt['reconciliation_ref']).unlink()
    monkeypatch.setattr(s.BusinessSemanticIntegration,'_save_task',original)
    monkeypatch.setattr(time,'time',lambda:receipt['expires_at']+1)
    result=c.service.analyze(c.project_id)
    assert result['status']=='completed' and result['publication_complete'],result.get('synthesis_failure')
    assert all(p!=row['data']['request'] for p in c.calls[paid:])


def test_presend_exception_leaves_unknown_child_and_cannot_resend(interrupted_recovery_case):
    import threading
    c=interrupted_recovery_case
    recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    calls=[]
    def crash(*args,**kwargs):calls.append(1);raise RuntimeError('provider setup')
    c.tools.providers.complete=crash
    runtime=s.BusinessSemanticIntegration(c.tools,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=c.snapshot,c.run,c.run['semantic_input_hash']
    runtime._task_bindings={};runtime._owned_slots=set();runtime._stopped=threading.Event()
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with c.service.store._db() as db:parent=s.semantic_checkpoint_reader(db)(c.parent_stage_hash)
    def call():return runtime._call('catalogue','',parent['data']['request'],lambda x:x,context=parent['data']['context'])
    with recovery.held_project_lock(c.project_id),pytest.raises(RuntimeError):call()
    with c.service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        child=reader(c.plan['child_stage_hash'])
        assert child['status']=='needs_review' and child['data']['failure_code']=='admission_incomplete'
        assert child['data']['result'] is None and 'diagnostic' not in child['data'] and 'feedback' not in child['data']
        assert recovery.authenticate_recovery(child,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)['consumed_at'] is not None
    with recovery.held_project_lock(c.project_id),pytest.raises(s.SemanticValidationError) as error:call()
    assert error.value.reason_code=='admission_incomplete' and calls==[1]


def permuted_ordinary(c,row):
    orphan=deepcopy(row);data=orphan['data']
    data.pop('recovery_id',None);data.pop('logical_task_hash',None)
    request,context=data['request'],data['context']
    items=deepcopy(request['items']);members=deepcopy(context['label_members']);parents=deepcopy(context['catalogue_parents'])
    request['items']=[]
    for n,item in enumerate(reversed(items)):
        old=item['label_id'];label=f'l{n}';request['items'].append({**item,'label_id':label})
        context['label_members'][label]=members[old];context['catalogue_parents'][label]=parents[old]
    request['membership_hash']=s._hash(context['label_members'])
    key=s.semantic_task_identity_hash(c.snapshot,c.run['semantic_input_hash'],'catalogue',request,context)
    orphan['stage_hash']=data['request_hash']=key
    return orphan


def test_full_closure_rejects_unknown_sibling_orphan_omissions_and_double_parent(completed_interrupted_recovery):
    c=completed_interrupted_recovery;base=c.tasks
    receipts=base.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])
    def frozen(rows,proofs):
        lookup={r['stage_hash']:deepcopy(r) for r in rows}
        def read(key):return deepcopy(lookup.get(key))
        read.task_rows=lambda h:deepcopy(list(lookup.values()))
        read.recovery_receipts=lambda h:deepcopy(proofs)
        for name in ('raw_digest','raw_error_code','supersession_receipts','catalogue_source','catalogue_runs'):
            setattr(read,name,getattr(base.read_checkpoint,name))
        result=s.SemanticCheckpointRows(deepcopy(rows));result.read_checkpoint=read
        return result
    for remove in (c.parent_stage_hash,c.plan['child_stage_hash']):
        rows=frozen([r for r in base if r['stage_hash']!=remove],receipts)
        with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    rows=frozen(list(base),[r for r in receipts if r['recovery_id']!=c.plan['recovery_id']])
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    orphan=permuted_ordinary(c,base.read_checkpoint(c.plan['child_stage_hash']))
    rows=frozen([*base,orphan],receipts)
    assert s._checkpoint_record(orphan['stage_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)==orphan
    projection=deepcopy(c.projection);projection['verification']['catalogue_task_hashes'].append(orphan['stage_hash'])
    projection['verification']['projection_hash']=s._projection_hash(projection)
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    orphan['status']='running';orphan['data'].update(result=None,result_hash=s._hash(None))
    rows=frozen([*base,orphan],receipts)
    assert s._checkpoint_record(orphan['stage_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)==orphan
    with pytest.raises(s.SemanticValidationError) as error:s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    assert error.value.reason_code=='interrupted'
    duplicate=deepcopy(receipts[-1]);duplicate['parent_stage_hash']=c.parent_stage_hash
    rows=frozen(list(base),[*receipts,duplicate])
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)


def test_unrepresentable_numeric_time_is_invalid_evidence(evidence_case):
    c=evidence_case;c.events[0]['at']=10**1000;rebind_evidence(c)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


def test_old_closed_shapes_receipt_hashes_and_structured_events_are_exact(case,bounded_parent,caplog):
    import importlib.util
    baseline=Path(__file__).parent/'fixtures/semantic_recovery_before_interrupted_catalogue.py'
    spec=importlib.util.spec_from_file_location('businessmap._before_interrupted_event',baseline)
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    assert (recovery.PLAN_FIELDS,recovery.AUTH_FIELDS,recovery.RECEIPT_FIELDS)==(old.PLAN_FIELDS,old.AUTH_FIELDS,old.RECEIPT_FIELDS)
    standard=set(logging.makeLogRecord({}).__dict__)|{'message','asctime'}
    for fixture in (case,bounded_parent):
        service,snapshot,run,key=fixture
        with service.store._db() as db:
            plan=recovery.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=s.semantic_checkpoint_reader(db))
        receipt={key:'fixed' for key in recovery.RECEIPT_FIELDS};receipt.update(plan,dispatch_id='e'*64)
        assert recovery.receipt_hash(receipt)==old.receipt_hash(receipt)
        with caplog.at_level('INFO'):
            old.log_event('accepted',receipt);recovery.log_event('accepted',receipt)
        before,after=caplog.records[-2:]
        assert {k:v for k,v in before.__dict__.items() if k not in standard}=={k:v for k,v in after.__dict__.items() if k not in standard}


@pytest.mark.parametrize('operation',['planned_authenticate','consume'])
def test_delayed_startup_uses_original_grant_freshness_and_current_guards(interrupted_recovery_case,monkeypatch,operation):
    c=interrupted_recovery_case
    receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
    c.service.set_paused(c.project_id,False)
    monkeypatch.setattr(time,'time',lambda:receipt['authorized_at']+205)
    with recovery.held_project_lock(c.project_id),c.service.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        reader=s.semantic_checkpoint_reader(db)
        if operation=='planned_authenticate':
            result=recovery.authenticate_recovery(reader(c.plan['child_stage_hash']),snapshot=c.snapshot,
                expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)
            assert result['consumed_at'] is None
        else:
            result=recovery.consume_recovery(db,c.snapshot,c.run,c.plan['child_stage_hash'])
            assert result['consumed_at']==receipt['authorized_at']+205
    with c.service.store._db() as db:
        state=db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(c.plan['child_stage_hash'],)).fetchone()[0]
        assert state==('planned' if operation=='planned_authenticate' else 'running')
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.parent_stage_hash,)).fetchone()==next(r for r in c.old_rows if r[0]==c.parent_stage_hash)


def test_reconciliation_cannot_postdate_original_authorization(evidence_case):
    c=evidence_case
    c.auth['authorized_at']-=1;c.auth['expires_at']-=1
    private_json(c.grant_path,c.auth)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('offset',[-1,3600,3601])
def test_later_planned_evidence_cannot_bypass_original_grant_window(evidence_case,monkeypatch,offset):
    c=evidence_case
    monkeypatch.setattr(time,'time',lambda:c.auth['authorized_at']+offset)
    with pytest.raises(s.SemanticValidationError):
        recovery._check_interrupted_reconciliation(c.auth,c.plan,fresh=False)


@pytest.mark.parametrize('age',[-1,61])
def test_later_evidence_must_have_been_fresh_before_original_authorization(evidence_case,monkeypatch,age):
    c=evidence_case
    c.check['checked_at']=c.auth['authorized_at']-age
    c.events[0]['at']=c.check['checked_at']-1;c.events[1]['at']=c.check['checked_at']
    rebind_evidence(c)
    monkeypatch.setattr(time,'time',lambda:c.auth['authorized_at']+205)
    with pytest.raises(s.SemanticValidationError):
        recovery._check_interrupted_reconciliation(c.auth,c.plan,fresh=False)


def test_new_authorization_cannot_reuse_a_205_second_old_check(evidence_case,monkeypatch):
    c=evidence_case
    stamp=c.auth['authorized_at']+205
    c.auth.update(authorized_at=stamp,expires_at=stamp+3600)
    private_json(c.grant_path,c.auth)
    monkeypatch.setattr(time,'time',lambda:stamp)
    with pytest.raises(s.SemanticValidationError):recovery._check_grant(c.grant_path,c.plan)


@pytest.mark.parametrize('mutation',['live_pid','event_append','dispatch_hash','reconciliation_hash'])
def test_delayed_evidence_rechecks_original_owner_and_all_immutable_bytes(evidence_case,monkeypatch,mutation):
    c=evidence_case
    monkeypatch.setattr(time,'time',lambda:c.auth['authorized_at']+205)
    if mutation=='live_pid':monkeypatch.setattr(recovery.os,'kill',lambda pid,signal:None)
    elif mutation=='event_append':
        with c.event_path.open('a') as stream:stream.write('{}\n')
    elif mutation=='dispatch_hash':
        c.dispatch['events_hash']='e'*64
        private_json(c.dispatch_path,c.dispatch)
    else:
        c.check['duplicate_call_risk_accepted']=False
        private_json(c.check_path,c.check)
    with pytest.raises(s.SemanticValidationError):
        recovery._check_interrupted_reconciliation(c.auth,c.plan,fresh=False)
