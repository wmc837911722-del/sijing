import pytest
from review_exception_fixture import two_review_history


def test_fixture_has_genuine_two_consumed_legacy_histories(two_review_history):
 c=two_review_history
 assert c.targets[0]['data']['result'] is None
 assert c.targets[1]['data']['failure_code']=='model_call'
 assert c.a['packet_hash']!=c.b['packet_hash']
 assert {row['stage'] for row in c.rows}=={'catalogue','proposal','review'}


def case_for(c):
 return {'version':2,'run_id':c.run['run_id'],'input_hash':c.run['semantic_input_hash'],
  'project_id':c.snapshot['project']['id'],'snapshot_id':c.run['snapshot_id'],'project_revision':c.run['project_revision'],
  'members':[{'role':role,'logical_review_hash':logical,'parent_stage_hash':row['stage_hash'],
   'subject_proposal_hash':row['data']['context']['proposal_hash'],'packet_hash':row['data']['context']['packet_hash']}
   for role,logical,row in [('A',c.a['logical_task_hash'],c.targets[0]),('B',c.targets[1]['stage_hash'],c.targets[1])]]}


def test_pure_plan_selects_two_members_but_grants_no_authority(two_review_history):
 import importlib.util,json,hashlib
 c=two_review_history
 assert importlib.util.find_spec('businessmap.semantic_review_exception') is not None, 'closed two-member policy plan is missing'
 from businessmap.semantic_review_exception import review_exception_bundle_plan
 reader=c.rows.read_checkpoint;capture=reader._raw_dynamic_capture(c.run['semantic_input_hash'])
 plan=review_exception_bundle_plan(c.snapshot,c.run,read_checkpoint=reader,dynamic_capture=capture,case_binding=case_for(c))
 assert [member['role'] for member in plan['members']]==['A','B']
 assert len({member['child_stage_hash'] for member in plan['members']})==2
 assert [member['parent_stage_hash'] for member in plan['members']]==[row['stage_hash'] for row in c.targets]
 assert len(plan['old_task_bindings'])==len(c.rows)
 assert len(plan['protected_receipt_raw'])==2
 assert len(json.dumps(plan,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())<=131072
 expected=hashlib.sha256(json.dumps(case_for(c),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
 assert plan['selection_binding_digest']==expected
 with c.store._db() as db:
  assert tuple(db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id'))==c.old_receipts
  assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==len(c.rows)


def prepare_fixture(c,tmp_path):
 import businessmap.semantic_review_exception as v
 from businessmap._source_material import prepare_source_material
 from businessmap.semantic_recovery import receipt_hash
 import os,time,json,hashlib
 reader=c.rows.read_checkpoint
 plan=v.review_exception_bundle_plan(c.snapshot,c.run,read_checkpoint=reader,
  dynamic_capture=reader._raw_dynamic_capture(c.run['semantic_input_hash']),case_binding=case_for(c))
 material=prepare_source_material(c.snapshot,c.run,read_checkpoint=reader,
  atomic_budget_chains=tuple(tuple(item) for item in plan['atomic_budget_chains']),
  whole_histories=tuple(tuple(item) for item in plan['whole_histories']),
  partition_repair_pairs=tuple(tuple(item) for item in plan['partition_repair_pairs']))
 prepared=v.prepare_review_exception_bundle(c.service,c.snapshot,c.run,plan,source_material=material)
 stamp=int(time.time())
 evidence={'checked_at':stamp,'local_owner_pids':[99999999],'local_processes_drained':True,
  'upstream_outcome_unknown':True,'duplicate_analysis_fee_risk_accepted':True,'analysis_only':True,
  **{key:plan[key] for key in ('run_id','input_hash','bundle_id','plan_hash','base_dynamic_digest','selection_binding_digest')}}
 evidence_raw=json.dumps(evidence,sort_keys=True,separators=(',',':'))
 evidence_path=tmp_path/'v2-evidence.json'
 fd=os.open(evidence_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:f.write(evidence_raw)
 grant={'policy':v.POLICY,**{key:plan[key] for key in ('plan_hash','bundle_id','run_id','input_hash','selection_binding_digest')},
  'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':stamp,'expires_at':stamp+3600,
  'max_uses':1,'use_unit':'atomic_two_target_authorization','reconciliation_ref':str(evidence_path),
  'reconciliation_hash':hashlib.sha256(evidence_raw.encode()).hexdigest()}
 grant_path=tmp_path/'v2-grant.json';grant_raw=json.dumps(grant,sort_keys=True,separators=(',',':'))
 fd=os.open(grant_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:f.write(grant_raw)
 authorization=v._load_review_exception_authorization(c.service,grant_path,expected_case=case_for(c))
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
 return plan,material,prepared,authorization


def authorize_fixture(c,tmp_path):
 import businessmap.semantic_review_exception as v
 plan,material,prepared,authorization=prepare_fixture(c,tmp_path)
 receipts=v.authorize_review_exception_bundle(c.service,c.snapshot,c.run,plan,authorization=authorization,source_material=material,prepared_mutation=prepared)
 return plan,receipts,authorization


def test_atomic_authorization_appends_only_two_tasks_and_two_receipts(two_review_history,tmp_path,caplog):
 import businessmap.semantic_review_exception as v
 from businessmap.semantic_recovery import receipt_hash
 import hashlib,json
 c=two_review_history
 with c.store._db() as db:before=tuple(db.execute('SELECT * FROM business_semantic_tasks ORDER BY rowid'))
 with caplog.at_level('INFO'):
  plan,receipts,authorization=authorize_fixture(c,tmp_path)
 events=[record for record in caplog.records if getattr(record,'event',None)=='semantic_recovery_authorized' and getattr(record,'policy',None)==v.POLICY]
 assert len(events)==2 and {event.recovery_id for event in events}=={member['recovery_id'] for member in plan['members']}
 assert len(receipts)==2 and [r['role'] for r in receipts]==['A','B']
 with c.store._db() as db:
  after=tuple(db.execute('SELECT * FROM business_semantic_tasks ORDER BY rowid'))
  assert after[:len(before)]==before and len(after)==len(before)+2
  assert all(row[3]=='planned' for row in after[len(before):])
  old=tuple(db.execute('SELECT * FROM business_semantic_recoveries WHERE recovery_id IN (?,?) ORDER BY recovery_id',tuple(row[0] for row in c.old_receipts)))
  assert old==c.old_receipts
  records=v.s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 proof=v.authenticate_review_exception_bundle(snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint)
 assert proof is not None
 for receipt in receipts:
  expected=hashlib.sha256(json.dumps({k:receipt[k] for k in v.RECEIPT_FIELDS},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
  assert receipt_hash(receipt)==expected



def test_full_inventory_keeps_both_old_charges_and_each_child_consumes_once(two_review_history,tmp_path):
 import pytest
 from businessmap import business_semantics as s
 from businessmap.semantic_recovery import consume_recovery,held_project_lock
 from copy import deepcopy
 c=two_review_history;plan,receipts,cap=authorize_fixture(c,tmp_path)
 with c.store._db() as db:records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 inventory=s.semantic_task_inventory(records,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
 assert inventory['occupied_calls']==7
 assert len(inventory['task_hashes'])==9
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 for member in plan['members']:
  child=records.read_checkpoint(member['child_stage_hash'])
  c.runtime._task_bindings[child['stage_hash']]={key:deepcopy(child['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}
  with held_project_lock(c.snapshot['project']['id']):
   prepared=c.runtime._prepare_mutation(child['stage_hash'],operation='consume_recovery')
   with c.store._db() as db:
    db.execute('BEGIN IMMEDIATE')
    receipt=consume_recovery(db,c.snapshot,c.run,child['stage_hash'],source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
   assert type(receipt['consumed_at']) is int and len(receipt['dispatch_id'])==64
  c.runtime._owned_slots.add(child['stage_hash'])
  with pytest.raises(s.SemanticValidationError):c.runtime._prepare_mutation(child['stage_hash'],operation='consume_recovery')
 with c.store._db() as db:
  records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
  assert len([r for r in records if r['status']=='running'])==3
  assert tuple(db.execute('SELECT * FROM business_semantic_recoveries WHERE recovery_id IN (?,?) ORDER BY recovery_id',tuple(row[0] for row in c.old_receipts)))==c.old_receipts
 inventory=s.semantic_task_inventory(records,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],allow_running=c.runtime._owned_slots)
 assert inventory['occupied_calls']==9
 assert c.targets[0]['stage_hash'] not in c.runtime._owned_slots


@pytest.mark.parametrize('two_review_history',['outside'],indirect=True)
def test_real_negative_enables_only_bound_d2_and_all_projection_paths_agree(two_review_history,tmp_path):
 import json
 from businessmap import business_semantics as s
 from businessmap.semantic_recovery import held_project_lock
 from test_business_semantics import independent_candidate,review
 c=two_review_history
 with c.store._db() as db:baseline_keys={row[0] for row in db.execute('SELECT stage_hash FROM business_semantic_tasks')}
 plan,receipts,cap=authorize_fixture(c,tmp_path)
 import hashlib
 expected=hashlib.sha256(json.dumps(list(c.outside),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
 assert plan['outside_input_task_raw']==[{'stage_hash':c.outside[0],'raw8_digest':expected}]
 current={item['stage_hash'] for item in plan['old_task_bindings']};outside={item['stage_hash'] for item in plan['outside_input_task_raw']}
 assert not current & outside and current | outside == baseline_keys
 assert all(receipt['plan']['outside_input_task_raw']==plan['outside_input_task_raw'] for receipt in receipts)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
 calls=[]
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user);calls.append(payload)
  result=independent_candidate(payload) if payload['stage']=='proposal' else review(payload,reject=len(calls) in {1,4})
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{'input_tokens':7,'output_tokens':3},'output_limited':False}
 c.runtime.service.providers.complete=complete
 with held_project_lock(c.snapshot['project']['id']):
  a=c.runtime._integrate_packet(c.packets[0]);b=c.runtime._integrate_packet(c.packets[1])
 assert [p['stage'] for p in calls]==['review','proposal','review','review','proposal','review']
 assert a['review_hash']!=plan['members'][0]['child_stage_hash'] and b['review_hash']!=plan['members'][1]['child_stage_hash']
 assert {'prior_candidate','revision_feedback','preservation'}<=calls[1].keys()
 with c.store._db() as db:
  rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
  assert len(rows)==13
  assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(c.outside[0],)).fetchone()==c.outside
  assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()==(14,)
  old=tuple(db.execute('SELECT * FROM business_semantic_recoveries WHERE recovery_id IN (?,?) ORDER BY recovery_id',tuple(row[0] for row in c.old_receipts)))
  assert old==c.old_receipts
 inventory=s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
 assert inventory['occupied_calls']==13
 c.service._review_exception_authorizations.clear()
 from pathlib import Path
 Path(cap._ReviewExceptionAuthorization__path).unlink()
 Path(json.loads(cap._ReviewExceptionAuthorization__grant)['reconciliation_ref']).unlink()
 # New unrelated history is permitted; the original committed outside set stays fixed.
 with c.store._db() as db:
  db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',('f'*64,*c.outside[1:2],'9'*64,*c.outside[3:]))
  rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 with held_project_lock(c.snapshot['project']['id']):projection=c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
 assert projection['complete'] and len(calls)==6
 s.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
 s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
 assert set(projection['verification']['accepted_review_hashes'])=={a['review_hash'],b['review_hash']}

 with c.store._db() as db:
  db.execute("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?",(c.outside[0],))
  drifted=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
 with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=drifted)
 with pytest.raises(s.SemanticValidationError):s.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
 with pytest.raises(s.SemanticValidationError):s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=drifted.read_checkpoint)
 assert len(calls)==6 and c.outside[0] not in c.runtime._owned_slots
