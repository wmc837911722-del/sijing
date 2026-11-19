"""Small real-controller cross-policy recovery; all files/providers are disposable."""
from copy import deepcopy
import hashlib
import json
import pytest
from businessmap import business_semantics as s, semantic_recovery as recovery, semantic_supersession as sup
from test_semantic_supersession import actual_grant, legacy_case
from test_semantic_recovery import case, grant as recovery_grant

def test_nested_history_fixture_restores_catalogue_after_outer_then_main_undo(tmp_path):
    cls = s.BusinessSemanticIntegration
    original = cls._catalogue
    main = pytest.MonkeyPatch()
    history = None
    try:
        with main.context() as outer:
            outer.setattr(cls, '_catalogue', cls._catalogue_legacy)
            history = bounded_recovery_case.__wrapped__(tmp_path, main)
            next(history)
        next(history, None)
        history = None
        main.undo()
        assert cls._catalogue is original
    finally:
        if history is not None:
            history.close()
        main.undo()
        cls._catalogue = original

@pytest.fixture
def bounded_parent(legacy_case):
    service, snapshot, run, old_parent = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        labels = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=reader)
        runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
        runtime._snapshot = snapshot
        runtime._catalogue_origins = labels['bindings']
        request, _, context = runtime._catalogue_payload(labels['frontier'], mode='merge')
        context.update(generator_hash=s._semantic_generator_hash(snapshot), budget_profile=runtime._profile())
        key = s.semantic_task_identity_hash(snapshot, run['semantic_input_hash'], 'catalogue', request, context)
        data = dict(request=request, context=context, request_hash=key, result=None, result_hash=s._hash(None),
            failure_code='model_call', diagnostic=dict(status_code=502,codex_reason='provider_error'))
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
            (key,'catalogue',run['semantic_input_hash'],'needs_review',1,s._json(data),'model_call','fixed'))
    return service, snapshot, run, key


def test_bounded_policy_plan_is_same_wire_distinct_physical_child(bounded_parent):
    service,snapshot,run,key=bounded_parent
    with service.store._db() as db:
        before=db.execute('SELECT * FROM business_semantic_tasks').fetchall()
        reader=s.semantic_checkpoint_reader(db)
        plan=recovery.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=reader)
        parent=reader(key)
        assert plan['policy']=='operator-bounded-catalogue-provider502-once-v1'
        assert plan['logical_task_hash']==key and plan['child_stage_hash']!=key
        assert plan['system_digest']==s._hash(s.semantic_stage_system(snapshot,'catalogue',parent['data']['request']))
        assert db.execute('SELECT * FROM business_semantic_tasks').fetchall()==before

@pytest.fixture
def bounded_recovery_case(tmp_path, monkeypatch):
    """Real controller topology, not equivalence to the actual 15357-fact project."""
    from businessmap import business_semantics as s
    from businessmap.core import Store
    import subprocess
    from businessmap.tools_api import ToolsService
    from businessmap.business_projects import BusinessProjects
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.full_analysis import FullProjectAnalysis
    from businessmap.analysis_addenda import AnalysisAddenda
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    from businessmap.providers import ToolError
    from test_business_semantics import review
    store=Store(tmp_path/'full-data');store.open_vault(str(tmp_path/'full-vault'))
    repo=tmp_path/'repo';repo.mkdir()
    for n in range(16):
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
    import threading, time
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units
    from businessmap.semantic_recovery import authorize_catalogue_recovery
    from test_semantic_recovery import grant as recovery_grant
    monkeypatch.setattr(FullBusinessSynthesis, 'MAX_ITEMS', 1)
    calls, routing_by_fact = [], {}
    source_reconstructions = [0]
    original_source_units = source_fact_units
    def measured_source_units(*args, **kwargs):
        source_reconstructions[0] += 1
        return original_source_units(*args, **kwargs)
    monkeypatch.setattr('businessmap.full_synthesis.source_fact_units', measured_source_units)
    catalogue_limit = [64000]
    boundary_observation = []
    legacy = [True]
    failed_leaf_ordinals = {2, 6}
    merge_calls = [0]
    bounded_calls = [0]
    setup_lock = threading.Lock()
    began = time.monotonic()
    original_profile = s.BusinessSemanticIntegration._profile
    def narrowed_profile(self):
        profile = original_profile(self)
        profile['stages']['catalogue']['input_bytes'] = catalogue_limit[0]
        profile['catalogue_items'] = 8
        return profile
    monkeypatch.setattr(s.BusinessSemanticIntegration, '_profile', narrowed_profile)
    original_payload = s.BusinessSemanticIntegration._catalogue_payload
    def selected_payload(self, *args, **kwargs):
        kwargs.setdefault('legacy', legacy[0])
        return original_payload(self, *args, **kwargs)
    monkeypatch.setattr(s.BusinessSemanticIntegration, '_catalogue_payload', selected_payload)
    def configure():
        with setup_lock:
            if routing_by_fact:
                return
            snap = ProjectAnalysis(service).snapshot(project['id'])
            current = full._load(project['id'])
            units = source_fact_units(snap, current, full._chunks(current), [])
            assert len(units) == 64
            routes = []
            for n, unit in enumerate(units):
                title = '有权限的员工维护订单业务' + '条件' * ((n % 64) % 17) + chr(0x4e00 + n % 64)
                routing_by_fact[(unit['chunk_id'], unit['kind'], unit['text'])] = title
                routes.append({'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'rules', 'title': title})
            preview = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
            preview._snapshot = snap
            preview._units = {u['_fact_id']: u for u in units}

    def complete(system, user, **kw):
        kw['before_send']()
        payload = json.loads(user)
        calls.append(payload)
        stage = payload.get('stage')
        if 'files' in payload:
            result = {'title': '订单创建', 'summary': '有权限的员工可创建订单。',
                'findings': [{'text': '有权限的员工可创建订单。', 'evidence_ids': [payload['files'][0]['evidence_id']]}], 'unknowns': []}
        elif stage == 'catalogue':
            if payload['mode'] == 'leaf':
                with store._db() as db:
                    accepted = db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='leaf'").fetchone()[0]
                if accepted + 1 in failed_leaf_ordinals:
                    failed_leaf_ordinals.remove(accepted + 1)
                    error = ToolError('fixture', 502)
                    error.codex_reason = 'provider_error'
                    raise error
                groups = [[item] for item in payload['items']]
            else:
                if 'merge_contract' in payload:
                    bounded_calls[0] += 1
                    if bounded_calls[0] == 3:
                        error = ToolError('fixture', 502)
                        error.codex_reason = 'provider_error'
                        raise error
                if 'merge_contract' not in payload:
                    merge_calls[0] += 1
                    if merge_calls[0] == 2:
                        import sqlite3
                        raise sqlite3.OperationalError('disposable fixture bookkeeping failure')
                groups, group, facts = [], [], 0
                for item in payload['items']:
                    if group and facts + item['fact_count'] > 70:
                        groups.append(group)
                        group, facts = [], 0
                    group.append(item)
                    facts += item['fact_count']
                if group:
                    groups.append(group)
            result = {'operations': [{'title': group[0]['title'], 'category': 'flows', 'label_ids': [i['label_id'] for i in group]} for group in groups]}
        elif stage == 'proposal':
            operations, claims, decisions = [], [], []
            for n, op in enumerate(payload['operations']):
                ids = op['fact_ids']
                operations.append({'operation_id': op['operation_id'], 'purpose': '创建可处理的订单。', 'summary': '员工按权限创建订单。', 'support_ids': [ids[0]]})
                for fid in ids:
                    item = next(i for i in payload['items'] if i['fact_id'] == fid)
                    claims.append({'operation_id': op['operation_id'], 'kind': 'rule', 'text': item['text'], 'support_ids': [fid], 'scope': '订单创建', 'conditions': ['员工有权限'], 'section': 'conditions'})
                    decisions.append({'input_id': fid, 'parts': []})
            result = {'operations': operations, 'claims': claims, 'decisions': decisions}
        elif stage == 'review':
            result = review(payload)
        else:
            configure()
            result = {'assignments': [{'fact_id': i['fact_id'], 'disposition': 'public', 'category': 'rules', 'title': routing_by_fact[(i['chunk_id'], i['kind'], i['text'])]} for i in payload['items']]}
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}

    monkeypatch.setattr(service.providers, 'complete', complete)
    full = FullProjectAnalysis(service)
    def run_legacy_analysis():
        # Restore the entering method immediately, even under nested fixtures.
        with pytest.MonkeyPatch.context() as history:
            history.setattr(s.BusinessSemanticIntegration, '_catalogue', s.BusinessSemanticIntegration._catalogue_legacy)
            return full.analyze(project['id'])
    for ordinal in (2, 6):
        stopped = run_legacy_analysis()
        assert stopped['status'] != 'completed'
        snapshot = ProjectAnalysis(service).snapshot(project['id'])
        raw = full._load(project['id'])
        run = {**raw, 'semantic_input_hash': s.semantic_input_hash(snapshot, raw, full._chunks(raw), [])}
        with store._db() as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_tasks'").fetchone(), (stopped.get("synthesis_failure"), catalogue_limit[0], len(calls), boundary_observation)
            parents = db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE status='needs_review' AND json_extract(data,'$.context.catalogue_mode')='leaf' ORDER BY rowid").fetchall()
            parent = parents[-1][0]
            assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='leaf'").fetchone()[0] == ordinal - 1
        full.set_paused(project['id'], True)
        plan, path = recovery_grant((full, snapshot, run, parent), tmp_path, persist=False)
        authorize_catalogue_recovery(full, snapshot, run, plan, authorization=path)
        full.set_paused(project['id'], False)
    stopped = run_legacy_analysis()
    assert stopped['status'] != 'completed'
    raw = full._load(project['id'])
    run = {**raw, 'semantic_input_hash': s.semantic_input_hash(snapshot, raw, full._chunks(raw), [])}
    with store._db() as db:
        parent = db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE status='needs_review' AND json_extract(data,'$.context.catalogue_mode')='merge'").fetchone()[0]
        old_rows = db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
        old_recoveries = db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()
        old_l0 = db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()
        old_source = db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='leaf'").fetchone() == (8,)
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='merge'").fetchone() == (1,)
        assert len(old_l0) == 64 and len(old_recoveries) == 2 and len(old_source) == 32
    # Historical paid v1 proofs remain valid after their authorization windows.
    real_clock = time.time
    monkeypatch.setattr(time, 'time', lambda: real_clock() + 7200)
    assert all(json.loads(row[4])['expires_at'] < time.time() for row in old_recoveries)
    legacy[0] = False
    full.set_paused(project['id'], True)
    plan, path = actual_grant((full, snapshot, run, parent), tmp_path)
    sup.authorize_catalogue_supersession(full, snapshot, run, plan, authorization=path)
    paid = len(calls)
    full.set_paused(project['id'], False)
    stopped = run_legacy_analysis()
    assert stopped['status'] != 'completed', stopped
    full.set_paused(project['id'], True)
    with store._db() as db:
        target = db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE status='needs_review' AND json_extract(data,'$.request.merge_contract') IS NOT NULL").fetchall()
        assert len(target) == 1
        old_rows = db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
        old_recoveries = db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()
        old_supersessions = db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()
        assert len(old_recoveries) == 2 and len(old_supersessions) == 1
    from types import SimpleNamespace
    yield SimpleNamespace(service=full, tools=service, snapshot=snapshot, run=run,
        parent_stage_hash=target[0][0], project_id=project['id'], old_rows=old_rows,
        old_recoveries=old_recoveries, old_supersessions=old_supersessions,
        old_source=old_source, old_l0=old_l0, calls=calls, run_legacy_analysis=run_legacy_analysis)
    store.close()

@pytest.fixture
def completed_bounded_recovery(bounded_recovery_case, tmp_path, request):
    c=bounded_recovery_case
    c.catalogue_mode = getattr(request, 'param', 'current')
    plan,path=recovery_grant((c.service,c.snapshot,c.run,c.parent_stage_hash),tmp_path,persist=False)
    c.plan,c.grant=plan,path
    c.new_receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,plan,authorization=path)
    paid=len(c.calls)
    c.service.set_paused(c.project_id,False)
    result = c.run_legacy_analysis() if c.catalogue_mode == 'legacy' else c.service.analyze(c.project_id)
    assert result['status']=='completed' and result['publication_complete'], result.get('synthesis_failure')
    c.projection=c.service._load(c.project_id)['synthesis']
    with c.service.store._db() as db:
        c.tasks=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        keys=[row[0] for row in c.old_rows]
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash IN (%s) ORDER BY stage_hash' % ','.join('?' for _ in keys),keys).fetchall()==c.old_rows
        assert db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()==c.old_supersessions
        ids=[row[0] for row in c.old_recoveries]
        assert db.execute('SELECT * FROM business_semantic_recoveries WHERE recovery_id IN (?,?) ORDER BY recovery_id',ids).fetchall()==c.old_recoveries
        assert db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()==c.old_l0
        assert db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()==c.old_source
    c.new_calls=c.calls[paid:]
    assert all(p.get('stage') in {'catalogue','proposal','review'} and p.get('mode')!='leaf' for p in c.new_calls)
    assert len(c.tasks.read_checkpoint.recovery_receipts(c.run['semantic_input_hash']))==3
    assert len(c.tasks.read_checkpoint.supersession_receipts(c.run['semantic_input_hash']))==1
    s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)
    s.validate_stored_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.service.store)
    s.semantic_support_context(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    yield c


@pytest.mark.parametrize('completed_bounded_recovery', ['current', 'legacy'], indirect=True)
def test_three_recoveries_one_supersession_complete_without_repay(completed_bounded_recovery):
    c=completed_bounded_recovery
    assert c.plan['policy']==recovery.BOUNDED_MERGE_POLICY
    assert c.tasks.read_checkpoint(c.plan['child_stage_hash'])['status']=='accepted'
    assert c.tasks.read_checkpoint(c.parent_stage_hash)['status']=='needs_review'
    downstream=[r for r in c.tasks if any(b['stage_hash']==c.plan['child_stage_hash']
        for b in r['data']['context'].get('catalogue_parents',{}).values())]
    if c.catalogue_mode == 'legacy':
        assert downstream
    else:
        from businessmap.semantic_catalogue_frontier import validate_catalogue_terminal
        plan = validate_catalogue_terminal(c.projection, snapshot=c.snapshot,
            expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=c.tasks.read_checkpoint)
        child = c.plan['child_stage_hash']
        assert child in plan['page_hashes']
        assert child in c.projection['verification']['catalogue_task_hashes']
        assert len(c.projection['semantic_coverage']['input_ids']) == 64
        assert not downstream
        assert len([p for p in c.new_calls if p.get('stage') == 'catalogue']) == 1

@pytest.mark.parametrize('path,value', [
    ('stage','proposal'),('attempt',True),('attempt',2),('status','running'),
    ('data.request.mode','leaf'),('data.request.mode','unknown'),('data.context.catalogue_mode','leaf'),
    ('data.request.merge_contract','unknown'),('data.context.merge_contract','unknown'),
    ('data.request.stage','review'),('data.failure_code','input_limit'),('data.result',[]),
    ('data.diagnostic.codex_reason','timeout'),('data.diagnostic.status_code',True),
    ('data.diagnostic.status_code','502'),('data.diagnostic.status_code',503),
    ('data.recovery_id',None),('data.logical_task_hash',None),('data.supersession_id',None)])
def test_bounded_eligibility_is_narrow(bounded_parent,path,value):
    service,snapshot,run,key=bounded_parent
    with service.store._db() as db: row=s.semantic_checkpoint_reader(db)(key)
    target=row
    parts=path.split('.')
    for part in parts[:-1]: target=target[part]
    target[parts[-1]]=value
    with pytest.raises(s.SemanticValidationError): recovery._recovery_policy(row,snapshot)


@pytest.mark.parametrize('field',sorted(recovery.RECEIPT_FIELDS))
def test_bounded_receipt_every_field_is_authenticated(bounded_parent,tmp_path,field):
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:
        raw=json.loads(db.execute('SELECT data FROM business_semantic_recoveries').fetchone()[0])
        raw[field]='tampered'
        db.execute('UPDATE business_semantic_recoveries SET data=?',(json.dumps(raw),))
    with service.store._db() as db, pytest.raises((s.SemanticValidationError,TypeError,ValueError)):
        reader=s.semantic_checkpoint_reader(db)
        recovery.authenticate_recovery(reader(plan['child_stage_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=reader)


@pytest.mark.parametrize('field',sorted(recovery.AUTH_FIELDS))
def test_bounded_grant_every_field_preflight(bounded_parent,tmp_path,field):
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    raw=json.loads(path.read_text());raw[field]='tampered';path.write_text(json.dumps(raw))
    with pytest.raises((s.SemanticValidationError,TypeError,ValueError)):
        recovery._check_grant(path,plan)


def test_v1_plan_receipt_and_system_bytes_unchanged(case):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).parent/'fixtures/semantic_recovery_before_bounded_merge_transport.py'
    spec=importlib.util.spec_from_file_location('businessmap._old_recovery_contract',path)
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    service,snapshot,run,key=case
    with service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        before=old.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=reader)
        after=recovery.catalogue_recovery_plan(snapshot,run,key,read_checkpoint=reader)
    assert after==before
    assert recovery.POLICY==old.POLICY and recovery.PLAN_FIELDS==old.PLAN_FIELDS and recovery.AUTH_FIELDS==old.AUTH_FIELDS and recovery.RECEIPT_FIELDS==old.RECEIPT_FIELDS
    receipt={k:'fixed' for k in recovery.RECEIPT_FIELDS};receipt.update(after)
    assert recovery.receipt_hash(receipt)==old.receipt_hash(receipt)


def test_bounded_authorization_concurrency_and_consume_rollback(bounded_parent,tmp_path,caplog):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    with caplog.at_level('INFO'), ThreadPoolExecutor(max_workers=2) as pool:
        receipts=list(pool.map(lambda _:recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path),range(2)))
    assert receipts[0]==receipts[1]
    assert sum(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)==1
    event=next(r for r in caplog.records if getattr(r,'event',None)=='semantic_recovery_authorized')
    assert event.policy==recovery.BOUNDED_MERGE_POLICY
    service.store.config_set('full_analysis_control:run',{'paused':False,'cancelled':False})
    with service.store._db() as db:
        db.execute("CREATE TRIGGER abort_consume BEFORE UPDATE OF status ON business_semantic_tasks WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with recovery.held_project_lock('project'),pytest.raises(sqlite3.IntegrityError),service.store._db() as db:
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    with service.store._db() as db:
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_recoveries').fetchone()==(None,None)
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()==('planned',)
        db.execute('DROP TRIGGER abort_consume')
    with recovery.held_project_lock('project'),service.store._db() as db:
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    with recovery.held_project_lock('project'),service.store._db() as db,pytest.raises(s.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])


def test_bounded_expired_plan_and_presend_unknown(bounded_parent,tmp_path,monkeypatch):
    import time
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    receipt=recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    service.store.config_set('full_analysis_control:run',{'paused':False,'cancelled':False})
    now=time.time
    monkeypatch.setattr(time,'time',lambda:receipt['expires_at']+1)
    with recovery.held_project_lock('project'),service.store._db() as db,pytest.raises(s.SemanticValidationError):
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    monkeypatch.setattr(time,'time',now)
    with recovery.held_project_lock('project'),service.store._db() as db:
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    monkeypatch.setattr(time,'time',lambda:receipt['expires_at']+1)
    with service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db);row=reader(plan['child_stage_hash'])
        assert recovery.authenticate_recovery(row,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=reader)['consumed_at'] is not None
        assert row['status']=='running'
        with pytest.raises(s.SemanticValidationError): recovery.catalogue_recovery_plan(snapshot,run,row['stage_hash'],read_checkpoint=reader)


def test_bounded_real_connections_consume_once(bounded_parent,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    service.store.config_set('full_analysis_control:run',{'paused':False,'cancelled':False})
    def consume(_):
        try:
            with service.store._db() as db:
                db.execute('BEGIN IMMEDIATE');return recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
        except s.SemanticValidationError:
            return None
    with recovery.held_project_lock('project'),ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(consume,range(2)))
    assert sum(r is not None for r in results)==1
    with service.store._db() as db:
        assert db.execute('SELECT dispatch_id FROM business_semantic_recoveries').fetchone()[0]==next(r for r in results if r)['dispatch_id']


@pytest.mark.parametrize('failure',['insert','scope'])
def test_bounded_authorization_rollback_and_scope(bounded_parent,tmp_path,caplog,failure):
    import sqlite3
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    with service.store._db() as db:
        before=db.execute('SELECT * FROM business_semantic_tasks').fetchall()
        if failure=='insert':
            db.execute("CREATE TRIGGER abort_child BEFORE INSERT ON business_semantic_tasks WHEN NEW.status='planned' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    if failure=='scope':
        changed_once=[]
        def change(_):
            if changed_once:return
            changed_once.append(True)
            with service.store._db() as db:
                changed={**run,'project_revision':'changed'}
                db.execute('UPDATE full_analysis_runs SET data=?',(json.dumps(changed),))
        service.project_analysis.guard=change
    with caplog.at_level('INFO'),pytest.raises((s.SemanticValidationError,sqlite3.IntegrityError)):
        recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    with service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks').fetchall()==before
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_recoveries'").fetchone()
    assert not any(getattr(r,'event',None)=='semantic_recovery_authorized' for r in caplog.records)


def test_bounded_presend_exception_cannot_reopen_dispatch(bounded_recovery_case,tmp_path):
    import threading
    c=bounded_recovery_case
    plan,path=recovery_grant((c.service,c.snapshot,c.run,c.parent_stage_hash),tmp_path,persist=False)
    recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,plan,authorization=path)
    c.service.set_paused(c.project_id,False)
    invoked=[]
    def fail(*args,**kwargs):
        invoked.append(True);raise RuntimeError('setup before admission')
    c.tools.providers.complete=fail
    runtime=s.BusinessSemanticIntegration(c.tools,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=c.snapshot,c.run,c.run['semantic_input_hash']
    runtime._task_bindings={};runtime._owned_slots=set();runtime._stopped=threading.Event()
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    with c.service.store._db() as db:
        parent=s.semantic_checkpoint_reader(db)(c.parent_stage_hash)
    def call():
        return runtime._call('catalogue',s.BOUNDED_MERGE_SYSTEM,parent['data']['request'],lambda value:value,context=parent['data']['context'])
    with recovery.held_project_lock(c.project_id),pytest.raises(RuntimeError):call()
    with c.service.store._db() as db:
        records=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    child=records.read_checkpoint(plan['child_stage_hash'])
    assert child['status']=='needs_review' and child['data']['failure_code']=='admission_incomplete'
    assert child['data']['result'] is None and 'diagnostic' not in child['data'] and 'feedback' not in child['data']
    receipt=recovery.authenticate_recovery(child,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=records.read_checkpoint)
    assert receipt['consumed_at'] is not None
    with recovery.held_project_lock(c.project_id),pytest.raises(s.SemanticValidationError) as error:call()
    assert error.value.reason_code=='admission_incomplete' and len(invoked)==1


def test_mixed_ancestry_tampering_and_expired_received_replay(completed_bounded_recovery,monkeypatch):
    from types import SimpleNamespace
    import time
    c=completed_bounded_recovery
    base=c.tasks
    def frozen(rows,receipts):
        lookup={r['stage_hash']:deepcopy(r) for r in rows}
        def read(key):return deepcopy(lookup.get(key))
        read.task_rows=lambda h:deepcopy(list(lookup.values()))
        read.recovery_receipts=lambda h:deepcopy(receipts)
        for name in ('raw_digest','supersession_receipts','catalogue_source','catalogue_runs'):
            setattr(read,name,getattr(base.read_checkpoint,name))
        result=s.SemanticCheckpointRows(deepcopy(rows));result.read_checkpoint=read
        return result
    receipts=base.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])
    for remove in (c.parent_stage_hash,c.plan['child_stage_hash']):
        rows=frozen([r for r in base if r['stage_hash']!=remove],receipts)
        with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    rows=frozen(list(base),[r for r in receipts if r['child_stage_hash']!=c.plan['child_stage_hash']])
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    # An individually valid normal page with the same upstream operations cannot
    # be declared beside the accepted transport child, even with a fresh hash.
    orphan=deepcopy(base.read_checkpoint(c.plan['child_stage_hash']))
    data=orphan['data'];data.pop('recovery_id');data.pop('logical_task_hash')
    request,context=data['request'],data['context']
    items=deepcopy(request['items']);members=deepcopy(context['label_members']);parents=deepcopy(context['catalogue_parents'])
    request['items']=[]
    for i,item in enumerate(reversed(items)):
        old=item['label_id'];label=f'l{i}';request['items'].append({**item,'label_id':label})
        context['label_members'][label]=members[old];context['catalogue_parents'][label]=parents[old]
    request['membership_hash']=s._hash(context['label_members'])
    key=s.semantic_task_identity_hash(c.snapshot,c.run['semantic_input_hash'],'catalogue',request,context)
    orphan['stage_hash']=data['request_hash']=key
    rows=frozen([*base,orphan],receipts)
    assert s._checkpoint_record(key,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)==orphan
    projection=deepcopy(c.projection);projection['verification']['catalogue_task_hashes'].append(key)
    projection['verification']['projection_hash']=s._projection_hash(projection)
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)


def test_expired_received_child_replays_without_second_dispatch(bounded_recovery_case,tmp_path,monkeypatch):
    import time
    c=bounded_recovery_case
    plan,path=recovery_grant((c.service,c.snapshot,c.run,c.parent_stage_hash),tmp_path,persist=False)
    receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,plan,authorization=path)
    original=s.BusinessSemanticIntegration._save_task
    def interrupt(self,key,stage,status,*args,**kwargs):
        if key==plan['child_stage_hash'] and status=='accepted':
            raise RuntimeError('interrupt after received')
        return original(self,key,stage,status,*args,**kwargs)
    monkeypatch.setattr(s.BusinessSemanticIntegration,'_save_task',interrupt)
    c.service.set_paused(c.project_id,False)
    stopped=c.service.analyze(c.project_id)
    assert stopped['status']!='completed'
    with c.service.store._db() as db:
        assert s.semantic_checkpoint_reader(db)(plan['child_stage_hash'])['status']=='received'
    paid=len(c.calls)
    with c.service.store._db() as db:
        request=s.semantic_checkpoint_reader(db)(plan['child_stage_hash'])['data']['request']
    prior=sum(call==request for call in c.calls)
    monkeypatch.setattr(s.BusinessSemanticIntegration,'_save_task',original)
    monkeypatch.setattr(time,'time',lambda:receipt['expires_at']+1)
    result=c.service.analyze(c.project_id)
    assert result['status']=='completed' and result['publication_complete'],result.get('synthesis_failure')
    assert sum(call==request for call in c.calls)==prior
    assert all(p.get('stage') in {'catalogue','proposal','review'} and p.get('mode')!='leaf' for p in c.calls[paid:])


@pytest.mark.parametrize('status',['accepted','received','proposed','unknown'])
def test_new_policy_rejects_invalid_child_state_or_missing_result(bounded_parent,tmp_path,status):
    service,snapshot,run,key=bounded_parent
    plan,path=recovery_grant(bounded_parent,tmp_path,persist=False)
    recovery.authorize_catalogue_recovery(service,snapshot,run,plan,authorization=path)
    service.store.config_set('full_analysis_control:run',{'paused':False,'cancelled':False})
    with recovery.held_project_lock('project'),service.store._db() as db:
        db.execute('BEGIN IMMEDIATE');recovery.consume_recovery(db,snapshot,run,plan['child_stage_hash'])
    with service.store._db() as db:
        db.execute('UPDATE business_semantic_tasks SET status=? WHERE stage_hash=?',(status,plan['child_stage_hash']))
        reader=s.semantic_checkpoint_reader(db)
        with pytest.raises(s.SemanticValidationError):
            recovery.authenticate_recovery(reader(plan['child_stage_hash']),snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=reader)
