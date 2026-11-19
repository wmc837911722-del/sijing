"""Real disposable SQLite + captured logs are contractual integrity seams."""
import importlib.util
import json
from contextlib import nullcontext, contextmanager
from types import SimpleNamespace
from copy import deepcopy
import pytest
from businessmap import business_semantics as s
from businessmap.core import Store

@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path/'data')
    snapshot = {'project': {'id':'p','revision':'rev','name':'测试'}, 'provider': {'id':'codex','kind':'codex-local','base_url':'codex://local','model':'fixture'}, 'vault_path':str(tmp_path/'vault'), 'policies':{}}
    run = {'run_id':'run','snapshot_id':'snap','project_revision':'rev','semantic_input_hash':s.semantic_input_hash(snapshot,{'run_id':'run','snapshot_id':'snap','project_revision':'rev'},[],[])}
    request = {'stage':'catalogue','mode':'leaf','project':'测试','items':[],'membership_hash':s._hash({})}
    context = {'catalogue_mode':'leaf','label_members':{},'generator_hash':s._semantic_generator_hash(snapshot),'budget_profile':s.budget_profile(snapshot)}
    key = s.semantic_task_identity_hash(snapshot,run['semantic_input_hash'],'catalogue',request,context)
    data = {'request_hash':key,'request':request,'context':context,'result':None,'result_hash':s._hash(None),'failure_code':'model_call','diagnostic':{'codex_reason':'provider_error','status_code':502}}
    service = SimpleNamespace(store=store, _exclusive=lambda _:nullcontext(), _controls=lambda _:{'paused':True,'cancelled':False}, project_analysis=SimpleNamespace(guard=lambda _:None))
    s.BusinessSemanticIntegration(service,guard=lambda:None)
    with store._db() as db:
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',(key,'catalogue',run['semantic_input_hash'],'needs_review',1,s._json(data),'model_call','fixed'))
    yield service,snapshot,run,key
    store.close()

def test_plan_is_readonly_and_original_failure_still_terminal(case):
    from businessmap.semantic_recovery import catalogue_recovery_plan
    service,snapshot,run,key = case
    with service.store._db() as db:
        before = db.execute('SELECT * FROM business_semantic_tasks').fetchall()
        plan = catalogue_recovery_plan(snapshot,run,key,read_checkpoint=s.semantic_checkpoint_reader(db))
        assert plan['parent_stage_hash'] == key
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_recoveries'").fetchone()
        assert db.execute('SELECT * FROM business_semantic_tasks').fetchall() == before
        with pytest.raises(s.SemanticValidationError) as error:
            s.semantic_task_inventory(s.semantic_checkpoint_rows(db,run['semantic_input_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])
        assert error.value.reason_code == 'model_call'

@pytest.mark.parametrize('field,value',[('status_code',True),('status_code',503),('codex_reason','timeout')])
def test_wrong_diagnostic_rejected(case,field,value):
    from businessmap.semantic_recovery import catalogue_recovery_plan
    service,snapshot,run,key = case
    with service.store._db() as db:
        data = json.loads(db.execute('SELECT data FROM business_semantic_tasks').fetchone()[0]); data['diagnostic'][field]=value
        db.execute('UPDATE business_semantic_tasks SET data=?',(s._json(data),))
        with pytest.raises(s.SemanticValidationError): catalogue_recovery_plan(snapshot,run,key,read_checkpoint=s.semantic_checkpoint_reader(db))

def grant(case,tmp_path,*,persist=True):
    import time,hashlib
    from businessmap.semantic_recovery import catalogue_recovery_plan
    service,snapshot,run,key=case
    with service.store._db() as db: plan=catalogue_recovery_plan(snapshot,run,key,read_checkpoint=s.semantic_checkpoint_reader(db))
    if persist: persist_scope(service.store,snapshot,run)
    stamp=int(time.time())
    reconciliation=tmp_path/'reconcile.json'
    reconciliation.write_text(json.dumps({'checked_at':stamp,'local_owner_pids':[99999999],'local_processes_drained':True,'upstream_result_or_billing_reconciled':False}))
    auth={'policy':plan['policy'],'plan_hash':plan['plan_hash'],'parent_stage_hash':key,'run_id':run['run_id'],'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':stamp,'expires_at':stamp+3600,'max_uses':1,'reconciliation_ref':str(reconciliation),'reconciliation_hash':hashlib.sha256(reconciliation.read_bytes()).hexdigest()}
    path=tmp_path/'grant.json';path.write_text(json.dumps(auth));path.chmod(0o600)
    return plan,path

def test_authorize_atomic_idempotent_and_consumption_once(case,tmp_path,caplog):
    from businessmap.semantic_recovery import authorize_catalogue_recovery,consume_recovery
    service,snapshot,run,key=case
    plan,path=grant(case,tmp_path)
    with service.store._db() as db: original=db.execute('SELECT * FROM business_semantic_tasks').fetchall()
    with caplog.at_level('INFO'):
        receipt=authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
        assert authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)==receipt
        with service.store._db() as db:
            assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(key,)).fetchall()==original
            rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
            assert s.semantic_task_inventory(rows,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])['occupied_calls']==1
        with locked(service,snapshot), service.store._db() as db:
            consume_recovery(db,snapshot,run,plan['child_stage_hash'])
        with service.store._db() as db:
            with pytest.raises(s.SemanticValidationError):consume_recovery(db,snapshot,run,plan['child_stage_hash'])
            assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='running'
            assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries').fetchone()[0] is not None
    assert sum(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)==1

def test_runtime_resolves_child_and_never_resends_running(case,tmp_path):
    import threading
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case
    plan,path=grant(case,tmp_path); authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    calls=[]
    def complete(system,user,**kw):
        kw['before_send']();kw['before_send']();calls.append(user)
        return {'text':'{"operations":[]}', 'usage':{},'output_limited':False}
    service.providers=SimpleNamespace(complete=complete)
    builder=s.BusinessSemanticIntegration(service,guard=lambda:None)
    builder._snapshot=snapshot;builder._run=run;builder._input_hash=run['semantic_input_hash']
    builder._stopped=threading.Event();builder._owned_slots=set();builder._task_bindings={};builder._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with service.store._db() as db: data=s.semantic_checkpoint_reader(db)(key)['data']
    with locked(service,snapshot): result,child=builder._call('catalogue','unused',data['request'],lambda x:x,context=data['context'])
    assert child==plan['child_stage_hash'] and result=={'operations':[]}
    assert len(calls)==1
    assert builder._call('catalogue','unused',data['request'],lambda x:x,context=data['context'])[0]==result
    assert len(calls)==1

from test_business_semantics import semantic_case,build_case,execute

def test_full_projection_recovery_and_current_stored_tamper(semantic_case,tmp_path):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    from businessmap.providers import ToolError
    store,snapshot,run,units,routing=semantic_case
    snapshot['provider'].update(kind='codex-local',base_url='codex://local')
    run['semantic_input_hash']=s.semantic_input_hash(snapshot,run,[],[])
    def fail(payload,count):
        error=ToolError('failure',502);error.codex_reason='provider_error';raise error
    setup=build_case(semantic_case,provider=fail)
    with pytest.raises(s.SemanticValidationError): execute(setup)
    run['semantic_input_hash']=setup[0]._input_hash
    with store._db() as db:key=db.execute('SELECT stage_hash FROM business_semantic_tasks').fetchone()[0]
    service=SimpleNamespace(store=store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    plan,path=grant((service,snapshot,run,key),tmp_path)
    authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    resumed=build_case(semantic_case)
    with locked(service,snapshot):projection=execute(resumed)
    assert len([c for c in resumed[1] if c['stage']=='catalogue'])==1
    assert projection['verification']['catalogue_task_hashes']==[plan['child_stage_hash']]
    assert len(projection['verification']['recovery_receipt_hashes'])==1
    with store._db() as db:
        records=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
    s.validate_current_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],task_rows=records)
    with pytest.raises(s.SemanticValidationError):
        s.validate_current_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],task_rows=list(records))
    replay=build_case(semantic_case)
    # Replay this synthetic direct-input recovery through the legacy catalogue.
    replay[0]._catalogue=replay[0]._catalogue_legacy
    assert execute(replay)['verification']==projection['verification'];assert replay[1]==[]
    with store._db() as db:db.execute("UPDATE business_semantic_tasks SET created_at='tamper' WHERE stage_hash=?",(key,))
    with pytest.raises(s.SemanticValidationError):s.validate_stored_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],store=store)

def test_receipt_and_child_rollback_emits_no_success(case,tmp_path,caplog):
    import sqlite3
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    with service.store._db() as db:
        db.execute("CREATE TRIGGER refuse_child BEFORE INSERT ON business_semantic_tasks BEGIN SELECT RAISE(ABORT,'injected'); END")
    with caplog.at_level('INFO'),pytest.raises(sqlite3.IntegrityError):authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_recoveries'").fetchone()
    assert not any(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)

def test_real_sqlite_consumption_race_and_rollback(case,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap.semantic_recovery import authorize_catalogue_recovery,consume_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:
        db.execute("CREATE TRIGGER refuse_running BEFORE UPDATE ON business_semantic_tasks WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'injected'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with locked(service,snapshot), service.store._db() as db:consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    with service.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()==(None,)
        db.execute('DROP TRIGGER refuse_running')
    gate=Barrier(2)
    def race():
        gate.wait()
        try:
            with service.store._db() as db:
                db.execute('BEGIN IMMEDIATE');consume_recovery(db,snapshot,run,plan['child_stage_hash'])
            return True
        except s.SemanticValidationError:return False
    with locked(service,snapshot), ThreadPoolExecutor(2) as pool: assert sorted(pool.map(lambda _:race(),range(2)))==[False,True]
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries WHERE consumed_at IS NOT NULL').fetchone()==(1,)
        with pytest.raises(s.SemanticValidationError) as error:s.semantic_task_inventory(s.semantic_checkpoint_rows(db,run['semantic_input_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])
        assert error.value.reason_code=='interrupted'

def test_authorize_rejects_forged_context_even_with_valid_grant(case,tmp_path):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    forged={**run,'snapshot_id':'other'}
    with pytest.raises(s.SemanticValidationError):authorize_catalogue_recovery(service,snapshot,forged,plan,authorization=path)

def test_authorize_requires_current_persisted_run(case,tmp_path):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    # An authentic task row and a file grant are insufficient without the current run.
    with service.store._db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS full_analysis_runs (id TEXT PRIMARY KEY, project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('DELETE FROM full_analysis_runs')
    with pytest.raises(s.SemanticValidationError):authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)


def persist_scope(store,snapshot,run):
    store.config_set('full_analysis_control:'+run['run_id'],{'paused':True,'cancelled':False})
    with store._db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS full_analysis_runs (id TEXT PRIMARY KEY, project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS full_analysis_chunks (run_id TEXT,position INTEGER,data TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS analysis_addenda (id TEXT,run_id TEXT,data TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS analysis_matching_receipts (id TEXT,run_id TEXT,data TEXT)')
        db.execute('INSERT OR REPLACE INTO full_analysis_runs VALUES (?,?,?,?,?)',(run['run_id'],snapshot['project']['id'],run['snapshot_id'],s._json({**run, 'project_id': snapshot['project']['id']}),'fixed'))

def test_authorize_concurrent_same_plan_has_one_committed_event(case,tmp_path,caplog):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);gate=Barrier(2)
    def invoke():gate.wait();return authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with caplog.at_level('INFO'),ThreadPoolExecutor(2) as pool:
        a,b=list(pool.map(lambda _:invoke(),range(2)))
    assert a==b
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()==(2,)
    assert sum(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)==1

@pytest.mark.parametrize('mutation',['parent','receipt','child','orphan','schema','profile'])
def test_all_inventory_tamper_rejected(case,tmp_path,mutation):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:
        if mutation=='parent':db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(key,))
        elif mutation=='receipt':db.execute('DELETE FROM business_semantic_recoveries')
        elif mutation=='child':db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],))
        elif mutation=='orphan':db.execute("UPDATE business_semantic_recoveries SET child_stage_hash=?",('b'*64,))
        else:
            receipt=json.loads(db.execute('SELECT data FROM business_semantic_recoveries').fetchone()[0]);receipt[mutation+'_digest' if mutation=='schema' else 'budget_profile_digest']='b'*64
            db.execute('UPDATE business_semantic_recoveries SET data=?',(s._json(receipt),))
        with pytest.raises(s.SemanticValidationError):s.semantic_task_inventory(s.semantic_checkpoint_rows(db,run['semantic_input_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])

def test_consumption_requires_current_os_project_lock(case,tmp_path):
    from businessmap.semantic_recovery import authorize_catalogue_recovery,consume_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db,pytest.raises(s.SemanticValidationError):consume_recovery(db,snapshot,run,plan['child_stage_hash'])


@contextmanager
def locked(service,snapshot):
    from businessmap.full_analysis import FullProjectAnalysis
    controller=FullProjectAnalysis.__new__(FullProjectAnalysis)
    controller.store=service.store
    controller._recover_continuation=lambda _:None
    with service.store._db() as db:
        run_ids=[row[0] for row in db.execute('SELECT id FROM full_analysis_runs WHERE project_id=?',(snapshot['project']['id'],))]
    for run_id in run_ids:service.store.config_set('full_analysis_control:'+run_id,{'paused':False,'cancelled':False})
    with controller._exclusive(snapshot['project']['id']):yield

def test_support_reader_rejects_inline_recovery_proof(semantic_case):
    setup=build_case(semantic_case);projection=execute(setup)
    projection['verification']['recovery_receipt_hashes']=['b'*64]
    store,snapshot,run,_,_=semantic_case
    with store._db() as db,pytest.raises(s.SemanticValidationError):
        s.semantic_support_context(projection,snapshot=snapshot,expected_input_hash=projection['semantic_input_hash'],read_checkpoint=s.semantic_checkpoint_reader(db))

@pytest.mark.parametrize("scale", [False, True], ids=["small", "640-l0-13-pages"])
def test_real_full_controller_restart_recovers_and_publishes(tmp_path,monkeypatch,caplog,record_property,scale):
    import subprocess
    from businessmap.tools_api import ToolsService
    from businessmap.business_projects import BusinessProjects
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap.analysis_addenda import AnalysisAddenda
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    from businessmap.providers import ToolError
    from test_business_semantics import review
    from support_graph_fixture import positive_graph
    store=Store(tmp_path/'full-data');store.open_vault(str(tmp_path/'full-vault'))
    repo=tmp_path/'repo';repo.mkdir()
    for n in range(160 if scale else 1):
        path=repo/f'domain{n:03d}'/'orders.py';path.parent.mkdir()
        path.write_text('def create_order():\n    return "允许创建订单"\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    store._write_repo({'id':'repo','name':'repo','vault_path':str(store.vault),'status':'ready','commit':commit,'path':str(repo),'url':'fixture','auto_sync':False})
    service=ToolsService(store)
    provider=service.providers.save({'name':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'})
    import shutil
    front=tmp_path/'front';shutil.copytree(repo,front)
    store._write_repo({'id':'front','name':'front','vault_path':str(store.vault),'status':'ready','commit':commit,'path':str(front),'url':'fixture','auto_sync':False})
    project=BusinessProjects(store).create('订单业务','',['repo','front'],{'repo':'backend','front':'frontend'})
    ProjectAnalysis(service).bind_model(project['id'],provider['id'])
    calls=[];fail=True
    import threading,time
    began=time.monotonic();routing_by_fact={};catalogue_limit=[64000];setup_lock=threading.Lock()
    if scale:
        from businessmap.full_synthesis import FullBusinessSynthesis,source_fact_units
        monkeypatch.setattr(FullBusinessSynthesis,'MAX_ITEMS',1)
        original_profile=s.BusinessSemanticIntegration._profile
        def narrowed_profile(self):
            profile=original_profile(self);profile['stages']['catalogue']['input_bytes']=catalogue_limit[0];return profile
        monkeypatch.setattr(s.BusinessSemanticIntegration,'_profile',narrowed_profile)
    def configure_scale():
        with setup_lock:
            if routing_by_fact:return
            snap=ProjectAnalysis(service).snapshot(project['id']);current=full._load(project['id'])
            all_units=source_fact_units(snap,current,full._chunks(current),[])
            assert len(all_units)==640
            labels=[]
            for n in range(26):
                members=all_units[n::26];title='订单类别'+str(n)
                labels.append({'title':title,'category':'rules','fact_ids':[u['_fact_id'] for u in members],'aliases':[title]})
                for unit in members:routing_by_fact[(unit['chunk_id'],unit['kind'],unit['text'])]=title
            preview=s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
            preview._snapshot=snap;preview._units={u['_fact_id']:u for u in all_units}
            sizes=[]
            for n in range(0,26,2):
                payload,_,_=preview._catalogue_payload(labels[n:n+2]);sizes.append(preview._request_bytes('catalogue','',payload))
            catalogue_limit[0]=max(sizes)
            assert catalogue_limit[0]<64000

    def complete(system,user,**kw):
        nonlocal fail
        kw['before_send']();payload=json.loads(user);calls.append(payload)
        stage=payload.get('stage')
        if 'files' in payload:
            result={'title':'订单创建','summary':'有权限的员工可创建订单。','findings':[{'text':'有权限的员工可创建订单。','evidence_ids':[payload['files'][0]['evidence_id']]}],'unknowns':[]}
        elif stage=='catalogue':
            if fail and (not scale or sum(p.get('stage')=='catalogue' and p.get('mode')=='leaf' for p in calls)==13):
                error=ToolError('fixture',502);error.codex_reason='provider_error';raise error
            result={'operations':[{'title':i['title'],'category':'flows','label_ids':[i['label_id']]} for i in payload['items']]} if scale and payload['mode']=='merge' else {'operations':[{'title':payload['items'][0]['title'] if scale else '创建订单','category':'flows','label_ids':[i['label_id'] for i in payload['items']]}]}
        elif stage=='proposal':
            oid=payload['operations'][0]['operation_id'];ids=[i['fact_id'] for i in payload['items']]
            canonical={'operations':[{'operation_id':oid,'purpose':'创建可处理的订单。','summary':'员工按权限创建订单。','support_ids':[ids[0]]}],
                'claims':[{'id':'c0'+str(n),'operation_id':oid,'kind':'rule','text':item['text'],'support_ids':[item['fact_id']],'scope':'订单创建','conditions':['员工有权限'],'section':'conditions'} for n,item in enumerate(payload['items'])],
                'decisions':[{'input_id':fid,'parts':[{'outcome':'claim','claim_ids':['c0'+str(n)],'support_ids':[fid],'resolved_by_ids':[],'rationale':'保留业务条件。'}]} for n,fid in enumerate(ids)]}
            result=positive_graph(canonical,payload)
        elif stage=='review':result=review(payload)
        else:
            if scale:configure_scale()
            result={'assignments':[{'fact_id':i['fact_id'],'disposition':'public','category':'rules','title':routing_by_fact[(i['chunk_id'],i['kind'],i['text'])] if scale else '创建订单'} for i in payload['items']]}
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    monkeypatch.setattr(service.providers,'complete',complete)
    full=FullProjectAnalysis(service)
    first=full.analyze(project['id']);assert first['status']!='completed'
    snapshot=ProjectAnalysis(service).snapshot(project['id']);raw=full._load(project['id'])
    addenda=AnalysisAddenda(service,guard=lambda:None).accepted(snapshot,raw)
    run={**raw,'semantic_input_hash':s.semantic_input_hash(snapshot,raw,full._chunks(raw),addenda)}
    with store._db() as db:
        parent=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE status='needs_review'").fetchone()[0]
        original=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(parent,)).fetchone()
        old_l0=db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()
        old_pages=db.execute("SELECT * FROM business_semantic_tasks WHERE status='accepted' ORDER BY stage_hash").fetchall()
        old_source=db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()
    if scale:
        assert len(old_l0)==640 and len(old_pages)==12
        assert sum('level' in p for p in calls)==640
        assert sum('files' in p for p in calls)==320
    full.set_paused(project['id'],True)
    plan,path=grant((full,snapshot,run,parent),tmp_path,persist=False)
    with caplog.at_level('INFO'):authorize_catalogue_recovery(full,snapshot,run,plan,authorization=path)
    before=len(calls);fail=False
    del full
    restarted=FullProjectAnalysis(service);restarted.set_paused(project['id'],False)
    with caplog.at_level('INFO'):result=restarted.analyze(project['id'])
    assert result['status']=='completed',result
    assert result['publication_complete'] is True
    assert not any('files' in p or p.get('stage') not in {'catalogue','proposal','review'} for p in calls[before:])
    assert sum(p.get('stage')=='catalogue' and p.get('mode')=='leaf' for p in calls[before:])==1
    current=restarted._load(project['id']);projection=current['synthesis']
    s.validate_stored_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],store=store)
    with store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(parent,)).fetchone()==original
        records=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        assert db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()==old_l0
        assert db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()==old_source
        if old_pages:
            assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash IN (%s) ORDER BY stage_hash' % ','.join('?' for _ in old_pages),[row[0] for row in old_pages]).fetchall()==old_pages
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()==(1,)
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('accepted',)
    inventory=s.semantic_task_inventory(records,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])
    assert inventory['occupied_calls']==len(records)
    s.validate_current_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],task_rows=records)
    events=[getattr(r,'event',None) for r in caplog.records]
    for name in ('authorized','consumed','received','accepted'):assert events.count('semantic_recovery_'+name)==1
    assert list(store.vault.rglob('*.md'))
    record_property('integrity_evidence',json.dumps({'public_result':result['status'],'scale':scale,'elapsed_seconds':round(time.monotonic()-began,3),'reused_l0_count':len(old_l0),'reused_catalogue_pages':len(old_pages),'original_source_count':len(old_source),'post_restart_stage_counts':{stage:sum(p.get('stage')==stage for p in calls[before:]) for stage in ('catalogue','proposal','review')},'committed_statuses':{r['stage_hash']:r['status'] for r in records},'recovery_events':[{k:getattr(record,k,None) for k in ('event','status','run_id','input_hash','parent_stage_hash','child_stage_hash','dispatch_id')} for record in caplog.records if str(getattr(record,'event','')).startswith('semantic_recovery_')],'source_or_l0_resends':0,'additional_catalogue_calls':1,'current_and_stored_verified':True},sort_keys=True))
    store.close()

@pytest.mark.parametrize('field,value',[('max_uses',True),('authorized_at',True),('operator_id','model'),('authorization_source','model'),('extra','unknown'),('expires_at',0)])
def test_closed_authorization_rejects_untrusted_fields(case,tmp_path,field,value):
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    body=json.loads(path.read_text());body[field]=value;path.write_text(json.dumps(body))
    with pytest.raises(s.SemanticValidationError):authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()==(1,)

def test_expired_unconsumed_rejects_but_received_history_survives(case,tmp_path,monkeypatch):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);receipt=r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with locked(service,snapshot),service.store._db() as db:
        consumed=r.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    with service.store._db() as db:
        row=s.semantic_checkpoint_reader(db)(plan['child_stage_hash']);data=row['data'];data.update(result=[],result_hash=s._hash([]))
        db.execute("UPDATE business_semantic_tasks SET status='received',data=? WHERE stage_hash=?",(s._json(data),plan['child_stage_hash']))
    monkeypatch.setattr(r.time,'time',lambda:receipt['expires_at']+1)
    with service.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
    assert s.semantic_task_inventory(rows,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])['occupied_calls']==2
    assert r.authenticate_recovery(rows.read_checkpoint(plan['child_stage_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)['dispatch_id']==consumed['dispatch_id']

def test_expired_first_dispatch_consumes_nothing(case,tmp_path,monkeypatch):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);receipt=r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    monkeypatch.setattr(r.time,'time',lambda:receipt['expires_at'])
    with locked(service,snapshot),service.store._db() as db:
        with pytest.raises(s.SemanticValidationError):r.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()==(None,)

def test_consumption_rejects_paused_database_control(case,tmp_path):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with locked(service,snapshot):
        service.store.config_set('full_analysis_control:'+run['run_id'],{'paused':True,'cancelled':False})
        with service.store._db() as db,pytest.raises(s.SemanticValidationError):r.consume_recovery(db,snapshot,run,plan['child_stage_hash'])

def recovery_builder(case):
    import threading
    service,snapshot,run,key=case
    builder=s.BusinessSemanticIntegration(service,guard=lambda:None)
    builder._snapshot=snapshot;builder._run=run;builder._input_hash=run['semantic_input_hash'];builder._stopped=threading.Event()
    builder._owned_slots=set();builder._task_bindings={};builder._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with service.store._db() as db:data=s.semantic_checkpoint_reader(db)(key)['data']
    return builder,data

def test_cas_before_send_crash_is_not_replanned_or_resent(case,tmp_path):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    calls=[]
    def crash(*args,**kwargs):calls.append(1);raise RuntimeError('before callback')
    service.providers=SimpleNamespace(complete=crash)
    builder,data=recovery_builder(case)
    with locked(service,snapshot),pytest.raises(RuntimeError):builder._call('catalogue','',data['request'],lambda x:x,context=data['context'])
    with service.store._db() as db:
        row=db.execute('SELECT status,data,error_code FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
        assert row[0]=='needs_review' and row[2]=='admission_incomplete'
        data=json.loads(row[1]);assert data['result'] is None and 'diagnostic' not in data and 'feedback' not in data
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is not None
    restarted,data=recovery_builder(case)
    with locked(service,snapshot),pytest.raises(s.SemanticValidationError) as error:restarted._call('catalogue','',data['request'],lambda x:x,context=data['context'])
    assert error.value.reason_code=='admission_incomplete' and calls==[1]

def test_paid_received_drains_then_expired_resumption_is_free(case,tmp_path,monkeypatch,caplog):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path);receipt=r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    blocked=False;calls=[]
    def guard():
        if blocked:raise RuntimeError('paused')
    def complete(*args,**kwargs):
        nonlocal blocked
        kwargs['before_send']();calls.append(1);blocked=True
        return {'text':'[]','usage':{},'output_limited':False}
    service.providers=SimpleNamespace(complete=complete)
    builder,data=recovery_builder(case);builder.guard=guard
    with locked(service,snapshot),caplog.at_level('INFO'),pytest.raises(RuntimeError):builder._call('catalogue','',data['request'],lambda x:x,context=data['context'])
    with service.store._db() as db:assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('received',)
    blocked=False;monkeypatch.setattr(r.time,'time',lambda:receipt['expires_at']+1)
    resumed,data=recovery_builder(case)
    with caplog.at_level('INFO'):assert resumed._call('catalogue','',data['request'],lambda x:x,context=data['context'])[0]==[]
    assert calls==[1]
    assert sum(getattr(record,'event',None)=='semantic_recovery_received' for record in caplog.records)==1
    assert sum(getattr(record,'event',None)=='semantic_recovery_accepted' for record in caplog.records)==1

def test_twelve_accepted_catalogue_pages_reused_thirteenth_recovers_once(case,tmp_path):
    from businessmap import semantic_recovery as r
    from businessmap.providers import ToolError
    service,snapshot,run,_=case
    builder,_=recovery_builder(case)
    with service.store._db() as db:db.execute('DELETE FROM business_semantic_tasks')
    units={f'fact:{n}':{'_fact_id':f'fact:{n}','kind':'finding','text':'有权限的员工可以创建订单。'} for n in range(26)}
    routing=[{'fact_id':fid,'disposition':'public','category':'flows','title':'创建订单'+str(n)} for n,fid in enumerate(units)]
    def initialize(b):
        b.CATALOGUE_ITEMS=2;b._units=units;b._forbidden=[];b._catalogue_tasks=[];b.plans=[]
    initialize(builder);calls=[];fail=True
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        if fail and len(calls)==13:
            error=ToolError('fixture',502);error.codex_reason='provider_error';raise error
        return {'text':json.dumps({'operations':[{'title':'创建订单','category':'flows','label_ids':[item['label_id'] for item in payload['items']]}]}),'usage':{},'output_limited':False}
    service.providers=SimpleNamespace(complete=complete)
    with pytest.raises(s.SemanticValidationError):builder._catalogue(routing)
    with service.store._db() as db:
        accepted=db.execute("SELECT * FROM business_semantic_tasks WHERE status='accepted' ORDER BY stage_hash").fetchall()
        parent=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE status='needs_review'").fetchone()[0]
    assert len(accepted)==12 and len(calls)==13
    plan,path=grant((service,snapshot,run,parent),tmp_path);r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    # Reconstruct fresh builder state, retaining only the durable SQL evidence.
    resumed,_=recovery_builder((service,snapshot,run,parent));initialize(resumed);fail=False
    # This test reconstructs the old direct-input, multi-page transport history.
    resumed._catalogue=resumed._catalogue_legacy
    with locked(service,snapshot):roots=resumed._catalogue(routing)
    assert len(roots)==1 and set(roots[0]['fact_ids'])==set(units)
    assert sum(p['mode']=='leaf' for p in calls[13:])==1
    with service.store._db() as db:
        assert db.execute("SELECT * FROM business_semantic_tasks WHERE stage_hash IN (%s) ORDER BY stage_hash" % ','.join('?' for _ in accepted),[row[0] for row in accepted]).fetchall()==accepted
        rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        assert s.semantic_task_inventory(rows,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'])['occupied_calls']==len(rows)

def test_superseded_run_cannot_be_authorized(case,tmp_path):
    from businessmap import semantic_recovery as r
    service,snapshot,run,key=case;plan,path=grant(case,tmp_path)
    with service.store._db() as db:
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',('newer',snapshot['project']['id'],run['snapshot_id'],s._json({**run,'run_id':'newer'}),'zzzz'))
    with pytest.raises(s.SemanticValidationError):r.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)

def test_frozen_reader_is_independent_of_mutable_materialized_rows(case):
    service,snapshot,run,key=case
    with service.store._db() as db:rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
    original=rows.read_checkpoint(key)
    rows[0]['status']='accepted'
    rows[0]['data']['context']['catalogue_mode']='merge'
    rows[0]['data']['request']['items'].append({'injected':True})
    assert rows.read_checkpoint(key)==original


def test_authorize_rechecks_real_scope_after_grant_before_write_lock(semantic_case,tmp_path,monkeypatch,caplog):
    from businessmap.tools_api import ToolsService
    from businessmap.business_projects import BusinessProjects
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap import semantic_recovery as r
    from businessmap.providers import ToolError
    store,_,run,units,routing=semantic_case;store.open_vault(str(tmp_path/'race-vault'))
    for rid in ('front','back'):
        path=tmp_path/rid;path.mkdir()
        store._write_repo({'id':rid,'name':rid,'vault_path':str(store.vault),'status':'ready','commit':'a'*40,'path':str(path),'url':'fixture','auto_sync':False})
    service=ToolsService(store);provider=service.providers.save({'name':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'})
    project=BusinessProjects(store).create('门店运营','',['front','back'],{'front':'frontend','back':'backend'})
    ProjectAnalysis(service).bind_model(project['id'],provider['id']);snapshot=ProjectAnalysis(service).snapshot(project['id'])
    run.update(project_id=project['id'],project_revision=project['revision']);run['semantic_input_hash']=s.semantic_input_hash(snapshot,run,[],[])
    def fail(*args):
        error=ToolError('fixture',502);error.codex_reason='provider_error';raise error
    with pytest.raises(s.SemanticValidationError):execute(build_case((store,snapshot,run,units,routing),provider=fail))
    controller=FullProjectAnalysis(service)
    with store._db() as db:parent=db.execute('SELECT stage_hash FROM business_semantic_tasks').fetchone()[0]
    plan,path=grant((controller,snapshot,run,parent),tmp_path)
    original=r._check_grant
    def race(*args):
        result=original(*args)
        projects=store.config_get('business_projects');projects[0]['revision']='changed-after-check'
        store.config_set('business_projects',projects)
        return result
    monkeypatch.setattr(r,'_check_grant',race)
    with caplog.at_level('INFO'),pytest.raises(ToolError):r.authorize_catalogue_recovery(controller,snapshot,run,plan,authorization=path)
    with store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks').fetchone()==(1,)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_recoveries'").fetchone()
    assert not any(getattr(record,'event',None)=='semantic_recovery_authorized' for record in caplog.records)
