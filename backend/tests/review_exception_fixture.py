"""Disposable genuine Source plus consumed v1 review and C5 proposal histories."""
from copy import deepcopy
from contextlib import nullcontext
import json
import threading
from types import SimpleNamespace
import pytest
from businessmap import business_semantics as s
from businessmap.providers import ToolError
from businessmap.semantic_recovery import held_project_lock,consume_recovery
from test_business_semantics import semantic_case,independent_candidate
from test_review_timeout_recovery import review_grant
from test_proposal_timeout_recovery import proposal_grant

@pytest.fixture
def two_review_history(tmp_path, request):
 from businessmap.full_synthesis import FullBusinessSynthesis,source_fact_units
 from businessmap.semantic_review_recovery import authorize_review_timeout_recovery
 from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
 fixture=semantic_case.__wrapped__(tmp_path)
 store,snapshot,original_run,_,_=next(fixture)
 snapshot=deepcopy(snapshot)
 snapshot['provider']={'id':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'}
 snapshot['repositories']=[]
 run={**original_run,'project_id':snapshot['project']['id'],'modules':[{'id':'module'}]}
 third_packet=getattr(request,'param',False) is True or getattr(request,'param',None)=='outside-progress'
 grouped=getattr(request,'param',False)=='groups'
 if grouped:snapshot['project']['name']='测试项目'+'A'*15000
 chunks=[{'id':'source','module_id':'module','status':'completed','result':{'summary':'','findings':[
  {'text':text*220,'evidence_ids':[]} for text in ['办理业务需要核对门店权限。','办理业务需要核对适用范围。','提交申请需要核对申请权限。','提交申请需要核对申请范围。']+(['核销记录需要核对记录权限。','核销记录需要核对记录范围。'] if third_packet else [])], 'unknowns':[]}}]
 if grouped:
  chunks[0]['result']['findings'].extend({'text':'核验业务处理需核对门店、权限、范围和适用条件；'*3,'evidence_ids':[]} for _ in range(52))
 run['semantic_input_hash']=s.semantic_input_hash(snapshot,run,chunks,[])
 units=source_fact_units(snapshot,run,chunks);synthesis=FullBusinessSynthesis.__new__(FullBusinessSynthesis);routing=[]
 with store._db() as db:
  db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
  db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
  db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
  db.execute('CREATE TABLE analysis_addenda(id TEXT,run_id TEXT,data TEXT)')
  db.execute('CREATE TABLE analysis_matching_receipts(id TEXT,run_id TEXT,data TEXT)')
  db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',(run['run_id'],run['project_id'],run['snapshot_id'],s._json(run),'fixture'))
  db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)',(run['run_id'],0,s._json(chunks[0])))
  for batch in synthesis._batches(snapshot,units,0):
   response={'assignments':[{'fact_id':'f'+str(i),'disposition':'public','category':'flows','title':next(title for title in ('办理业务','提交申请','核销记录','核验业务') if batch[i]['text'].startswith(title))} for i in range(len(batch))]}
   normalized=synthesis._validate(response,batch);key=synthesis.cache_identity(snapshot,run,batch,0)
   db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)',(key,key,s._json(normalized),'fixture'));routing.extend(normalized['assignments'])
 mode=['catalogue']
 def complete(system,user,**kwargs):
  kwargs['before_send']();payload=json.loads(user)
  if payload['stage']=='catalogue':
   result={'operations':[{'title':item['title'],'category':'flows','label_ids':[item['label_id']]} for item in payload['items']]}
  elif payload['stage']=='subdivide':
   ids=[item['label_id'] for item in payload['items']]
   result={'operations':[{'title':'处理业务'+str(i//10),'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
  elif payload['stage']=='partition_review':
   from test_business_semantics import partition_review
   result=partition_review(payload,reject=True)
  elif payload['stage']==mode[0]:
   error=ToolError('timeout',408);error.codex_reason='timeout';raise error
  else:result=independent_candidate(payload)
  return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
 service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=complete),_exclusive=lambda _:nullcontext(),_controls=lambda _: {'paused':True,'cancelled':False},project_analysis=SimpleNamespace(guard=lambda _:None))
 runtime=s.BusinessSemanticIntegration(service,guard=lambda:None)
 runtime._snapshot,runtime._run,runtime._input_hash=snapshot,run,run['semantic_input_hash']
 runtime._units={unit['_fact_id']:unit for unit in units};runtime._forbidden,runtime._matching,runtime._partitions=[],[],[]
 runtime._catalogue_tasks,runtime.plans,runtime._task_bindings=[],[],{}
 runtime._owned_slots,runtime._stopped=set(),threading.Event();runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
 operations=runtime._catalogue(routing) if grouped else runtime._catalogue_page(runtime._catalogue_initial_labels(routing))
 if grouped:
  selected=next(op for op in operations if op['title']=='核验业务')
  labels=[{'title':selected['title'],'category':selected['category'],'fact_ids':[fid],'aliases':selected['aliases']} for fid in selected['fact_ids']]
  children=runtime._catalogue_page(labels,stage='subdivide');d1=runtime._catalogue_tasks[-1]
  with pytest.raises(s.SemanticValidationError) as first:runtime._verify_partition(selected,children,d1)
  r1=first.value.stage_hash;runtime.partition_repair_allowlist={(d1,r1)}
  runtime.WHOLE_FRESH_ENABLED=False
  with pytest.raises(s.SemanticValidationError) as second:runtime._packets([selected])
  assert hasattr(second.value,'stage_hash'), (second.value.reason_code, runtime._packet_fits([selected]), len(selected['fact_ids']))
  r2=second.value.stage_hash
  with store._db() as db:
   rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
   d2=next(row['stage_hash'] for row in rows if row['stage']=='subdivide' and row['data']['context'].get('partition_revision'))
  runtime.partition_repair_allowlist.clear();runtime.atomic_budget_allowlist={(d1,r1,d2,r2)}
  packets=[packet for packet in runtime._packets(operations) if packet != [selected]]
 else:packets=runtime._packets(operations)
 assert len(packets)==(3 if third_packet else 2)
 c=SimpleNamespace(store=store,snapshot=snapshot,run=run,runtime=runtime,service=service,packets=packets,units=units,routing=routing)
 mode[0]='review'
 with pytest.raises(s.SemanticValidationError):runtime._integrate_packet(packets[0])
 with store._db() as db:
  rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
 c.parent=next(row for row in rows if row['stage']=='review')
 c.proposal=next(row for row in rows if row['stage']=='proposal')
 adir=tmp_path/'legacy-a';adir.mkdir();a,grant,_=(_group_grant(c,adir,'review') if grouped else review_grant(c,adir))
 authorize_review_timeout_recovery(service,snapshot,run,a,authorization=grant)
 store.config_set('full_analysis_control:'+run['run_id'],{'paused':False,'cancelled':False})
 mode[0]='proposal'
 with pytest.raises(s.SemanticValidationError):runtime._integrate_packet(packets[1])
 with store._db() as db:
  rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
 c.parent=next(row for row in rows if row['stage']=='proposal' and row['status']=='needs_review')
 bdir=tmp_path/'legacy-b';bdir.mkdir();b,grant=(_group_grant(c,bdir,'proposal')[:2] if grouped else proposal_grant(c,bdir))
 authorize_proposal_timeout_recovery(service,snapshot,run,b,authorization=grant)
 store.config_set('full_analysis_control:'+run['run_id'],{'paused':False,'cancelled':False})
 with store._db() as db:child=s.semantic_checkpoint_rows(db,run['semantic_input_hash']).read_checkpoint(a['child_stage_hash'])
 runtime._task_bindings[a['child_stage_hash']]={key:deepcopy(child['data'][key]) for key in ('request','context','recovery_id','logical_task_hash')}
 runtime.review_timeout_recovery_allowlist={(a['parent_stage_hash'],a['child_stage_hash'])}
 with held_project_lock(snapshot['project']['id']):
  prepared=runtime._prepare_mutation(a['child_stage_hash'],operation='consume_recovery')
  with store._db() as db:
   db.execute('BEGIN IMMEDIATE');consume_recovery(db,snapshot,run,a['child_stage_hash'],source_material=prepared._PreparedMutation__material,prepared_mutation=prepared)
 runtime._owned_slots.add(a['child_stage_hash'])
 runtime.proposal_timeout_recovery_allowlist={(b['parent_stage_hash'],b['child_stage_hash'])}
 mode[0]='review'
 with held_project_lock(snapshot['project']['id']),pytest.raises(s.SemanticValidationError):runtime._integrate_packet(packets[1])
 runtime._owned_slots.clear()
 with store._db() as db:
  rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash']);raw=tuple(db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id'))
 if third_packet:
  packet,refs,ops=runtime._packet(packets[2])
  context={'packet_hash':s._hash(packet),'input_ids':{key:unit['_fact_id'] for key,unit in refs.items()},
   'operation_ids':{key:operation['id'] for key,operation in ops.items()},'partition_review_hashes':packet['partition_review_hashes'],
   'budget_profile':runtime._profile(),'generator_hash':s._semantic_generator_hash(snapshot),'revision':s._revision_context()}
  key=s.semantic_task_identity_hash(snapshot,run['semantic_input_hash'],'proposal',packet,context)
  data={'request_hash':key,'request':packet,'context':context,'result':None,'result_hash':s._hash(None)}
  with store._db() as db:
   db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',(key,'proposal',run['semantic_input_hash'],'planned',1,s._json(data),None,'fixture'))
   rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
  c.nonselected=key
 if getattr(request,'param',None) in {'outside','outside-progress'}:
  c.outside=('e'*64,'legacy-stage','d'*64,'old-unknown',37,' {"legacy":"unresolved"} ', 'unknown-billing','old-time')
  with store._db() as db:
   db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',c.outside)
   rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
 c.a,c.b=a,b;c.rows=rows;c.old_receipts=raw
 c.targets=[rows.read_checkpoint(a['child_stage_hash']),next(row for row in rows if row['stage']=='review' and row['data']['context']['packet_hash']==b['packet_hash'])]
 assert c.targets[0]['status']=='running' and c.targets[1]['status']=='needs_review'
 assert len(raw)==2 and all(type(row[5]) is int and len(row[6])==64 for row in raw)
 yield c
 fixture.close()


def _group_grant(c,path,stage):
 import hashlib,time
 from businessmap.semantic_review_recovery import review_timeout_recovery_plan
 from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
 planner=review_timeout_recovery_plan if stage=='review' else proposal_timeout_recovery_plan
 with c.store._db() as db:
  plan=planner(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=s.semantic_checkpoint_reader(db),atomic_budget_chains=tuple(c.runtime.atomic_budget_allowlist))
 stamp=int(time.time())
 evidence={'checked_at':stamp,'local_owner_pids':[99999999],'local_processes_drained':True,'upstream_result_or_billing_reconciled':False,'run_id':c.run['run_id'],'parent_stage_hash':c.parent['stage_hash'],'parent_row_digest':plan['parent_failure_digest'],'inventory_digest':plan['inventory_digest']}
 evidence_path=path/'evidence.json';evidence_path.write_text(json.dumps(evidence));evidence_path.chmod(0o600)
 auth={'policy':plan['policy'],'plan_hash':plan['plan_hash'],'parent_stage_hash':c.parent['stage_hash'],'run_id':plan['run_id'],'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':stamp,'expires_at':stamp+3600,'max_uses':1,'reconciliation_ref':str(evidence_path),'reconciliation_hash':hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
 grant=path/'grant.json';grant.write_text(json.dumps(auth));grant.chmod(0o600)
 c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
 return plan,grant,evidence_path
