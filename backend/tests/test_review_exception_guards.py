from copy import deepcopy
import json
import sqlite3
import pytest
from businessmap import business_semantics as s
from businessmap import semantic_review_exception as v
from businessmap.semantic_recovery import consume_recovery,held_project_lock
from review_exception_fixture import two_review_history
from test_review_exception_bundle import prepare_fixture,authorize_fixture,case_for

@pytest.mark.parametrize('cold_proposal', (True, False), ids=('cold-proposal', 'warm-proposal'))
def test_second_draft_saves_authenticate_the_parent_with_cold_and_warm_source(two_review_history, tmp_path, cold_proposal):
    import sys
    from test_business_semantics import independent_candidate, review

    c = two_review_history
    authorize_fixture(c, tmp_path)
    c.store.config_set('full_analysis_control:' + c.run['run_id'], {'paused': False, 'cancelled': False})
    calls = []

    def complete(system, text, **kwargs):
        kwargs['before_send']()
        payload = json.loads(text)
        calls.append(payload['stage'])
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload, reject=len(calls) == 1)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}

    c.service.providers.complete = complete
    save = c.runtime._save_task_once
    parent_reads = []

    def observe_save(key, stage, status, **kwargs):
        binding = c.runtime._task_bindings[key]
        if status not in ('proposed', 'accepted') or binding['context'].get('revision', {}).get('draft_index') != 2:
            return save(key, stage, status, **kwargs)
        if stage == 'proposal' and cold_proposal:
            c.runtime._source_material = None
        else:
            assert c.runtime._source_material is not None

        # Observe the actual parent authentication without replacing a compiler dependency.
        material = c.runtime._source_material
        old_profile = sys.getprofile()

        def observe(frame, event, arg):
            if event == 'call' and frame.f_code is s._checkpoint_record.__code__ and frame.f_back.f_code is save.__func__.__code__:
                reader = frame.f_locals['read_checkpoint']
                parent_reads.append((stage, reader._FrozenReader__material is material))
                sys.setprofile(old_profile)

        sys.setprofile(observe)
        try:
            return save(key, stage, status, **kwargs)
        finally:
            sys.setprofile(old_profile)

    c.runtime._save_task_once = observe_save
    with held_project_lock(c.snapshot['project']['id']):
        c.runtime._integrate_packet(c.packets[0])
    assert parent_reads == [('proposal', True), ('review', True)]
    assert calls == ['review', 'proposal', 'review']
    with c.store._db() as db:
        assert dict(db.execute('SELECT stage,status FROM business_semantic_tasks WHERE attempt=2')) == {
            'proposal': 'proposed', 'review': 'accepted'}



def test_raw_drift_missing_peer_and_nonfactory_views_fail_closed(two_review_history,tmp_path):
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 for mutation in ('whitespace','peer-receipt','peer-task','protected-task'):
  with c.store._db() as db:
   db.execute('BEGIN')
   if mutation=='whitespace':db.execute("UPDATE business_semantic_recoveries SET data=' '||data WHERE recovery_id=?",(c.old_receipts[0][0],))
   if mutation=='peer-receipt':db.execute('DELETE FROM business_semantic_recoveries WHERE recovery_id=?',(plan['members'][1]['recovery_id'],))
   if mutation=='peer-task':db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(plan['members'][1]['child_stage_hash'],))
   if mutation=='protected-task':db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.targets[0]['stage_hash'],))
   records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
   with pytest.raises(s.SemanticValidationError):v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint)
   with pytest.raises(s.SemanticValidationError):s.semantic_task_inventory(records,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
   db.rollback()
 with c.store._db() as db:records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 def fake(key):return records.read_checkpoint(key)
 fake.recovery_receipts=records.read_checkpoint.recovery_receipts
 with pytest.raises(s.SemanticValidationError):v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=fake)


def test_authorization_rolls_back_second_insert_and_emits_no_commit_event(two_review_history,tmp_path,caplog):
 c=two_review_history;plan,material,prepared,cap=prepare_fixture(c,tmp_path)
 with c.store._db() as db:
  before={name:tuple(db.execute('SELECT * FROM '+name+' ORDER BY rowid')) for name in ('business_semantic_tasks','business_semantic_recoveries')}
  child=plan['members'][1]['child_stage_hash']
  db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON business_semantic_tasks WHEN NEW.stage_hash='"+child+"' BEGIN SELECT RAISE(ABORT,'synthetic second insert'); END")
 with caplog.at_level('INFO'),pytest.raises(sqlite3.IntegrityError):
  v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
 with c.store._db() as db:
  assert all(tuple(db.execute('SELECT * FROM '+name+' ORDER BY rowid'))==rows for name,rows in before.items())
 assert not [record for record in caplog.records if getattr(record,'event',None)=='semantic_recovery_authorized' and getattr(record,'policy',None)==v.POLICY]


def test_revoked_cap_after_prepare_blocks_claim_and_keeps_all_rows(two_review_history,tmp_path):
 c=two_review_history;plan,_,cap=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 child=plan['members'][0]['child_stage_hash']
 with c.store._db() as db:row=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']).read_checkpoint(child)
 c.runtime._task_bindings[child]={key:deepcopy(row['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}
 with held_project_lock(c.snapshot['project']['id']):
  prepared=c.runtime._prepare_mutation(child,operation='consume_recovery')
  del c.service._review_exception_authorizations[plan['bundle_id']]
  with c.store._db() as db,pytest.raises(s.SemanticValidationError):
   db.execute('BEGIN IMMEDIATE');consume_recovery(db,c.snapshot,c.run,child,source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
 with c.store._db() as db:
  assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(child,)).fetchone()==('planned',)
  assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(child,)).fetchone()==(None,None)
  records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 assert v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint) is not None


def test_case_selector_or_bare_grant_path_cannot_authorize(two_review_history,tmp_path):
 c=two_review_history;plan,material,prepared,cap=prepare_fixture(c,tmp_path)
 for authority in (plan,case_for(c),cap._ReviewExceptionAuthorization__path):
  with pytest.raises(s.SemanticValidationError):v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=authority,source_material=material,prepared_mutation=prepared)
 bad=case_for(c);bad['members'][0]['logical_review_hash']='f'*64
 c.service._review_exception_authorizations.clear()
 with pytest.raises(s.SemanticValidationError):v._load_review_exception_authorization(c.service,cap._ReviewExceptionAuthorization__path,expected_case=bad)
 with c.store._db() as db:assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==len(c.rows)


def test_d2_callback_failure_is_terminal_and_never_replanned(two_review_history,tmp_path):
 from test_business_semantics import review
 c=two_review_history;plan,_,cap=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 calls=[]
 def complete(system,user,**kwargs):
  payload=json.loads(user);calls.append(payload['stage'])
  if payload['stage']=='proposal':del c.service._review_exception_authorizations[plan['bundle_id']]
  kwargs['before_send']()
  return {'text':json.dumps(review(payload,reject=True),ensure_ascii=False),'usage':{},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 assert calls==['review','proposal']
 with c.store._db() as db:
  rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
  d2=[row for row in rows if row['attempt']==2]
  assert len(d2)==1 and d2[0]['status']=='needs_review'
  assert d2[0]['data']['failure_code']=='admission_incomplete'
 c.service._review_exception_authorizations[plan['bundle_id']]=cap
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 assert calls==['review','proposal']


@pytest.mark.parametrize('two_review_history',['outside-progress'],indirect=True)
def test_nonselected_old_planned_task_progresses_with_protected_history_unchanged(two_review_history,tmp_path):
 from test_business_semantics import independent_candidate,review
 c=two_review_history
 with pytest.raises(s.SemanticValidationError):s.semantic_task_inventory(c.rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
 plan,_,cap=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 calls=[]
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
  result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload)
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']):result=c.runtime._integrate_packet(c.packets[2])
 assert calls==['proposal','review']
 with c.store._db() as db:
  rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
  assert rows.read_checkpoint(c.nonselected)['status']=='proposed'
  assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.outside[0],)).fetchone()==c.outside
  assert rows.read_checkpoint(result['review_hash'])['status']=='accepted'
  assert all(rows.read_checkpoint.raw_checkpoint_row(item['stage_hash'])==c.rows.read_checkpoint.raw_checkpoint_row(item['stage_hash']) for item in plan['protected_task_raw'])
 assert v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint) is not None
 assert s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])['occupied_calls']==9


def test_new_paid_return_is_saved_before_old_raw_drift_blocks_promotion(two_review_history,tmp_path,caplog):
 from test_business_semantics import review
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 baseline_usage=deepcopy(c.runtime._usage)
 calls=[]
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload)
  with c.store._db() as db:db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.targets[0]['stage_hash'],))
  return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{'input_tokens':11,'output_tokens':4},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with caplog.at_level('INFO'),held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 child=plan['members'][0]['child_stage_hash']
 with c.store._db() as db:
  row=db.execute('SELECT status,data FROM business_semantic_tasks WHERE stage_hash=?',(child,)).fetchone()
  assert row[0]=='received' and json.loads(row[1])['result'] is not None
 assert len(calls)==1 and c.runtime._usage['requests']==baseline_usage['requests']+1 and c.runtime._usage['input_tokens']==baseline_usage['input_tokens']+11
 events=[record for record in caplog.records if getattr(record,'event',None)=='semantic_checkpoint_committed' and getattr(record,'stage_hash',None)==child]
 assert [record.status for record in events]==['received']


def test_competing_authorizers_commit_one_complete_bundle(two_review_history,tmp_path):
 from concurrent.futures import ThreadPoolExecutor
 c=two_review_history;plan,material,first,cap=prepare_fixture(c,tmp_path)
 second=v.prepare_review_exception_bundle(c.service,c.snapshot,c.run,plan,source_material=material)
 def authorize(prepared):
  try:return v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
  except Exception as error:return error
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(authorize,(first,second)))
 assert sum(type(result) is list for result in results)==1
 with c.store._db() as db:
  assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==len(c.rows)+2
  assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==len(c.old_receipts)+2
  records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 assert v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint) is not None


def test_grant_closed_fields_types_expiry_pid_and_mode_are_live_guards(two_review_history,tmp_path,monkeypatch):
 import os,time
 from pathlib import Path
 c=two_review_history;plan,material,prepared,cap=prepare_fixture(c,tmp_path)
 path=Path(cap._ReviewExceptionAuthorization__path)
 grant=json.loads(cap._ReviewExceptionAuthorization__grant)
 original_time=time.time;original_kill=os.kill
 for change in ('unknown','bool-use','wrong-unit','bad-hash','duplicate-field'):
  value=deepcopy(grant)
  if change=='unknown':value['unreviewed']=True
  if change=='bool-use':value['max_uses']=True
  if change=='wrong-unit':value['use_unit']='one_per_member'
  if change=='bad-hash':value['reconciliation_hash']='F'*64
  raw=json.dumps(value)
  if change=='duplicate-field':raw=raw[:-1]+',"max_uses":1}'
  bad=tmp_path/('grant-'+change+'.json')
  fd=os.open(bad,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as stream:stream.write(raw)
  with pytest.raises(s.SemanticValidationError):v._load_review_exception_authorization(type('GrantLoader',(),{})(),bad,expected_case=case_for(c))
 with monkeypatch.context() as patch:
  patch.setattr(time,'time',lambda:grant['expires_at'])
  with pytest.raises(s.SemanticValidationError):v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
 with monkeypatch.context() as patch:
  patch.setattr(os,'kill',lambda pid,signal:None)
  with pytest.raises(s.SemanticValidationError):v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
 path.chmod(0o644)
 with pytest.raises(s.SemanticValidationError):v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
 path.chmod(0o600)
 with c.store._db() as db:assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==len(c.rows)


def test_source_trigger_rolls_back_the_entire_authorization(two_review_history,tmp_path):
 c=two_review_history;plan,material,prepared,cap=prepare_fixture(c,tmp_path)
 with c.store._db() as db:
  source=tuple(db.execute('SELECT * FROM full_analysis_chunks'))
  db.execute("CREATE TRIGGER change_source AFTER INSERT ON business_semantic_tasks BEGIN UPDATE full_analysis_chunks SET data=replace(data,'核对门店权限','改变门店权限'); END")
 with pytest.raises(s.SemanticValidationError):v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=cap,source_material=material,prepared_mutation=prepared)
 with c.store._db() as db:
  assert tuple(db.execute('SELECT * FROM full_analysis_chunks'))==source
  assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==len(c.rows)
  assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==len(c.old_receipts)


def test_competing_consumers_leave_one_claim_without_resend(two_review_history,tmp_path):
 from concurrent.futures import ThreadPoolExecutor
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 child=plan['members'][0]['child_stage_hash']
 with c.store._db() as db:row=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']).read_checkpoint(child)
 c.runtime._task_bindings[child]={key:deepcopy(row['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}
 preparations=[c.runtime._prepare_mutation(child,operation='consume_recovery') for _ in range(2)]
 def consume(prepared):
  try:
   with held_project_lock(c.snapshot['project']['id']),c.store._lock,c.store._db() as db:
    db.execute('BEGIN IMMEDIATE')
    return consume_recovery(db,c.snapshot,c.run,child,source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
  except Exception as error:return error
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(consume,preparations))
 assert sum(type(result) is dict for result in results)==1
 with c.store._db() as db:
  assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(child,)).fetchone()==('running',)
  stamp,dispatch=db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(child,)).fetchone()
  assert type(stamp) is int and len(dispatch)==64
 assert child not in c.runtime._owned_slots


@pytest.mark.parametrize('drift_stage',('proposal','review'))
@pytest.mark.parametrize('mutation',('oldraw','strip-parent-lineage','delete-peer','source'))
def test_d2_paid_drift_never_promotes(two_review_history,tmp_path,drift_stage,mutation):
 from test_business_semantics import independent_candidate,review
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 calls=[]
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
  result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=len(calls)==1)
  if len(calls)>1 and payload['stage']==drift_stage:
   with c.store._db() as db:
    if mutation=='oldraw':db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.targets[0]['stage_hash'],))
    elif mutation=='delete-peer':db.execute('DELETE FROM business_semantic_recoveries WHERE recovery_id=?',(plan['members'][1]['recovery_id'],))
    elif mutation=='source':db.execute("UPDATE full_analysis_chunks SET data=' '||data")
    else:
     key=plan['members'][0]['child_stage_hash']
     data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchone()[0])
     del data['recovery_id'];del data['logical_task_hash']
     db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(s._json(data),key))
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{'input_tokens':11,'output_tokens':4},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 with c.store._db() as db:
  row=db.execute('SELECT status,data FROM business_semantic_tasks WHERE attempt=2 AND stage=?',(drift_stage,)).fetchone()
 assert row[0]=='received' and json.loads(row[1])['result'] is not None
 assert len(calls)==(2 if drift_stage=='proposal' else 3)


@pytest.mark.parametrize('cached_stage',('proposal','review'))
def test_cached_d2_received_rechecks_bundle_before_promotion(two_review_history,tmp_path,cached_stage):
 from test_business_semantics import independent_candidate,review
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 stopped=[False];calls=[]
 def guard():
  if stopped[0]:raise s.SemanticValidationError('paused')
 c.runtime.guard=guard
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
  if len(calls)>1 and payload['stage']==cached_stage:stopped[0]=True
  result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=len(calls)==1)
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 with c.store._db() as db:row=next(row for row in s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']) if row['attempt']==2 and row['stage']==cached_stage)
 assert row['status']=='received'
 restarted=s.BusinessSemanticIntegration(c.service,guard=lambda:None)
 for name in ('_snapshot','_run','_input_hash','_units','_forbidden','_matching','_partitions','_catalogue_tasks','plans','_stopped','_usage'):
  setattr(restarted,name,getattr(c.runtime,name))
 restarted._task_bindings={};restarted._owned_slots=set()
 original=restarted._save_task_once
 def drift_at_promotion(key,stage,status,*args,**kwargs):
  if status in {'proposed','accepted'}:
   with c.store._db() as db:db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.targets[0]['stage_hash'],))
  return original(key,stage,status,*args,**kwargs)
 restarted._save_task_once=drift_at_promotion
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):
  restarted._call(cached_stage,'',row['data']['request'],lambda value:value,context=row['data']['context'])
 with c.store._db() as db:assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(row['stage_hash'],)).fetchone()==('received',)
 assert calls==(['review','proposal'] if cached_stage=='proposal' else ['review','proposal','review'])


@pytest.mark.parametrize('two_review_history',['groups'],indirect=True)
def test_cold_runtime_recovers_nonempty_source_groups_from_verified_bundle(two_review_history,tmp_path):
 from businessmap._source_material import SourceMaterial,bind_source_material
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 assert plan['atomic_budget_chains']
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 child=plan['members'][0]['child_stage_hash']
 with c.store._db() as db:row=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']).read_checkpoint(child)
 fresh=s.BusinessSemanticIntegration(c.service,guard=lambda:None)
 fresh._snapshot,fresh._run,fresh._input_hash=c.snapshot,c.run,c.run['semantic_input_hash']
 fresh._owned_slots=set();fresh._task_bindings={child:{key:deepcopy(row['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}}
 assert fresh._source_material is None
 prepared=fresh._prepare_mutation(child,operation='consume_recovery')
 assert type(prepared._PreparedMutation__material) is SourceMaterial
 assert fresh._source_material._SourceMaterial__groups[0]==tuple(tuple(item) for item in plan['atomic_budget_chains'])
 with c.store._db() as db:reader=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']).read_checkpoint
 assert bind_source_material(fresh._source_material,c.snapshot,c.run,read_checkpoint=reader) is not None
 # A cold D2 has ordinary lineage and no direct recovery receipt. Its Source
 # groups must still come from the fully verified bundle, before promotion.
 from test_business_semantics import independent_candidate,review
 paused=[False];calls=[]
 def guard():
  if paused[0]:raise s.SemanticValidationError('paused')
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload['stage'])
  result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=True)
  if payload['stage']=='proposal':paused[0]=True
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
 c.runtime.guard=guard;c.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 with c.store._db() as db:d2=next(row for row in s.semantic_checkpoint_rows(db,c.run['semantic_input_hash']) if row['attempt']==2)
 assert d2['status']=='received' and calls==['review','proposal']
 fresh._source_material=None
 fresh._task_bindings={d2['stage_hash']:{key:deepcopy(d2['data'][key]) for key in ('request','context')}}
 prepared=fresh._prepare_mutation(d2['stage_hash'],operation='save_proposed')
 assert type(prepared._PreparedMutation__material) is SourceMaterial
 assert fresh._source_material._SourceMaterial__groups[0]==tuple(tuple(item) for item in plan['atomic_budget_chains'])


@pytest.mark.parametrize('two_review_history',[True],indirect=True)
@pytest.mark.parametrize('mutation',('unknown-status','unrelated-failure','unowned-running','invalid-result','context-tamper'))
def test_prepare_rejects_invalid_nonselected_baseline_without_using_bundle(two_review_history,mutation):
 from businessmap._source_material import prepare_source_material
 c=two_review_history
 with c.store._db() as db:
  data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(c.nonselected,)).fetchone()[0])
  status='planned';error=None
  if mutation=='unknown-status':status='invalid'
  elif mutation=='unrelated-failure':status='needs_review';error='unrelated_failure';data['failure_code']=error
  elif mutation=='unowned-running':status='running'
  elif mutation=='invalid-result':status='proposed';data['result']={};data['result_hash']=s._hash({})
  else:data['context']['packet_hash']='f'*64
  db.execute('UPDATE business_semantic_tasks SET status=?,data=?,error_code=? WHERE stage_hash=?',(status,s._json(data),error,c.nonselected))
  records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
  before_tasks=tuple(db.execute('SELECT * FROM business_semantic_tasks ORDER BY rowid'))
  before_receipts=tuple(db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id'))
 reader=records.read_checkpoint
 plan=v.review_exception_bundle_plan(c.snapshot,c.run,read_checkpoint=reader,dynamic_capture=reader._raw_dynamic_capture(c.run['semantic_input_hash']),case_binding=case_for(c))
 material=prepare_source_material(c.snapshot,c.run,read_checkpoint=reader)
 with pytest.raises(s.SemanticValidationError):
  v.prepare_review_exception_bundle(c.service,c.snapshot,c.run,plan,source_material=material)
 with c.store._db() as db:
  assert tuple(db.execute('SELECT * FROM business_semantic_tasks ORDER BY rowid'))==before_tasks
  assert tuple(db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id'))==before_receipts
  assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()==(2,)
 assert not getattr(c.service,'_review_exception_authorizations',{})
 assert c.targets[0]['stage_hash'] not in c.runtime._owned_slots


def test_loader_bounds_read_before_rejecting_oversize_grant(tmp_path,monkeypatch):
 import os
 from types import SimpleNamespace
 path=tmp_path/'oversize-grant';path.write_bytes(b'x'*20000);path.chmod(0o600)
 case={'version':2,'run_id':'run','input_hash':'1'*64,'project_id':'project','snapshot_id':'snapshot','project_revision':1,
  'members':[{'role':role,**{key:digit*64 for key in ('logical_review_hash','parent_stage_hash','subject_proposal_hash','packet_hash')}} for role,digit in (('A','a'),('B','b'))]}
 fdopen=os.fdopen;reads=[]
 class Stream:
  def __init__(self,stream):self.stream=stream
  def __enter__(self):return self
  def __exit__(self,*args):self.stream.close()
  def fileno(self):return self.stream.fileno()
  def read(self,size=-1):
   assert size==16385,'loader requested an unbounded authority read'
   reads.append(size);return self.stream.read(size)
 monkeypatch.setattr(os,'fdopen',lambda *args,**kwargs:Stream(fdopen(*args,**kwargs)))
 service=SimpleNamespace()
 with pytest.raises(s.SemanticValidationError):v._load_review_exception_authorization(service,path,expected_case=case)
 assert reads==[16385] and not hasattr(service,'_review_exception_authorizations')


@pytest.mark.parametrize('two_review_history',['outside'],indirect=True)
@pytest.mark.parametrize('mutation',('whitespace','delete','rekey','move-input','scalar'))
def test_committed_outside_history_drift_rejects_full_proof_and_prepare(two_review_history,tmp_path,mutation):
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 with c.store._db() as db:
  if mutation=='whitespace':db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.outside[0],))
  elif mutation=='delete':db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(c.outside[0],))
  elif mutation=='rekey':db.execute('UPDATE business_semantic_tasks SET stage_hash=? WHERE stage_hash=?',('f'*64,c.outside[0]))
  elif mutation=='move-input':db.execute('UPDATE business_semantic_tasks SET input_hash=? WHERE stage_hash=?',(c.run['semantic_input_hash'],c.outside[0]))
  else:db.execute('UPDATE business_semantic_tasks SET attempt=38 WHERE stage_hash=?',(c.outside[0],))
  records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 with pytest.raises(s.SemanticValidationError):v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint)
 child=plan['members'][0]['child_stage_hash'];row=records.read_checkpoint(child)
 c.runtime._task_bindings[child]={key:deepcopy(row['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}
 with pytest.raises(s.SemanticValidationError):c.runtime._prepare_mutation(child,operation='consume_recovery')
 with c.store._db() as db:
  assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(child,)).fetchone()==('planned',)
  assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(child,)).fetchone()==(None,None)
 assert c.outside[0] not in c.runtime._owned_slots


@pytest.mark.parametrize('two_review_history',['outside'],indirect=True)
def test_outside_drift_keeps_new_paid_result_received_before_promotion(two_review_history,tmp_path):
 from test_business_semantics import review
 c=two_review_history;plan,_,_=authorize_fixture(c,tmp_path)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 calls=[];usage=deepcopy(c.runtime._usage)
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload)
  with c.store._db() as db:db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.outside[0],))
  return {'text':json.dumps(review(payload),ensure_ascii=False),'usage':{'input_tokens':11,'output_tokens':4},'output_limited':False}
 c.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']),pytest.raises(s.SemanticValidationError):c.runtime._integrate_packet(c.packets[0])
 with c.store._db() as db:
  row=db.execute('SELECT status,data FROM business_semantic_tasks WHERE stage_hash=?',(plan['members'][0]['child_stage_hash'],)).fetchone()
 assert row[0]=='received' and json.loads(row[1])['result'] is not None
 assert len(calls)==1 and c.runtime._usage['requests']==usage['requests']+1 and c.runtime._usage['input_tokens']==usage['input_tokens']+11
