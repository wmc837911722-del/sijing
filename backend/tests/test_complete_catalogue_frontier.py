"""Catalogue completion and compatibility with historical paid checkpoints."""
from contextlib import closing
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from businessmap import business_semantics as s
from businessmap import semantic_catalogue_frontier as frontier
from test_business_semantics import semantic_case


def test_distinct_pass_through_pages_can_finish_complete_fact_frontier(semantic_case):
    store,snapshot,run,base,_=semantic_case
    snapshot=deepcopy(snapshot)
    snapshot['provider']={'id':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'}
    titles=['创建订单','登记发票','维护门店','办理退货','确认收款']
    units=[{**deepcopy(base[0]),'_fact_id':f'fact:{index}','text':title+'仅允许有权限员工处理。'} for index,title in enumerate(titles)]
    routing=[{'fact_id':unit['_fact_id'],'title':title,'category':'flows','disposition':'public'} for unit,title in zip(units,titles)]
    calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']()
        payload=json.loads(user)
        assert payload['stage']=='catalogue', 'diagnostic stops before semantic stages'
        calls.append({'mode':payload['mode'],'items':len(payload['items'])})
        result={'operations':[{'title':item['title'],'category':item['category'],'label_ids':[item['label_id']]} for item in payload['items']]}
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=complete))
    runtime=s.BusinessSemanticIntegration(service,guard=lambda:None)
    runtime.CATALOGUE_ITEMS=2
    runtime._snapshot,runtime._run=snapshot,run
    runtime._units={unit['_fact_id']:unit for unit in units}
    runtime._input_hash=s._hash({'diagnostic_facts':units})
    runtime._forbidden=[];runtime._catalogue_tasks=[];runtime.plans=[]
    runtime._task_bindings={};runtime._owned_slots=set();runtime._stopped=threading.Event()
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    try:
        roots=runtime._catalogue(routing)
    except s.SemanticValidationError as error:
        with store._db() as db:
            rows=db.execute("SELECT status,json_extract(data,'$.request.mode'),json_array_length(data,'$.request.items'),json_array_length(data,'$.result') FROM business_semantic_tasks ORDER BY rowid").fetchall()
        print(json.dumps({'actual_error':error.reason_code,'calls':calls,'committed_catalogue_rows':rows},ensure_ascii=False))
        raise
    assert {fid for operation in roots for fid in operation['fact_ids']}=={unit['_fact_id'] for unit in units}
    assert len(roots)==5


# Captured from the pre-terminal catalogue implementation with synthetic Git
# repositories and an in-process fake provider. Only the eight checkpoint/source
# tables are included. Receipt paths are synthetic identities, never opened.
# Producer business_semantics.py SHA-256:
# 86db0b035f53b13ab9eca4ec2c61caaf64c8e82b718e4f471b01a82cdfef80e2
LEGACY_FIXTURE_SHA256 = "f14d74986f03cea6ab6aaf6c91023dcd69dc9d25990b14cc3eea6d3e58f80daa"
LEGACY_PROJECTION_HASH = "e41cf59a391a851455de67864cb60650b4fc62d72fd120da9097552f0c103f81"
LEGACY_TABLES = (
    "full_analysis_runs", "full_analysis_chunks", "full_analysis_synthesis",
    "analysis_addenda", "analysis_matching_receipts", "business_semantic_tasks",
    "business_semantic_recoveries", "business_semantic_supersessions",
)


@pytest.fixture(scope="module")
def legacy_catalogue_history(tmp_path_factory):
    fixture = Path(__file__).with_name("fixtures") / "legacy_catalogue_four_plus_one.json.gz"
    raw = gzip.decompress(fixture.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == LEGACY_FIXTURE_SHA256
    history = json.loads(raw)
    assert set(history["tables"]) == set(LEGACY_TABLES)
    assert history["projection"]["verification"]["projection_hash"] == LEGACY_PROJECTION_HASH
    database = tmp_path_factory.mktemp("legacy-catalogue") / "checkpoint.sqlite"
    with closing(sqlite3.connect(database)) as target, target:
        for name in LEGACY_TABLES:
            table = history["tables"][name]
            target.execute(table["schema"])
            if table["rows"]:
                placeholders = ",".join("?" for _ in table["rows"][0])
                target.executemany(f"INSERT INTO {name} VALUES ({placeholders})", table["rows"])
    return SimpleNamespace(database=database, snapshot=history["snapshot"],
                           run=history["run"], projection=history["projection"])


@pytest.fixture
def historical_completed(monkeypatch, legacy_catalogue_history):
    from businessmap.full_synthesis import FullBusinessSynthesis
    original=s.BusinessSemanticIntegration._profile
    def profile(self):
        value=original(self);value['catalogue_items']=8;return value
    monkeypatch.setattr(s.BusinessSemanticIntegration,'_profile',profile)
    monkeypatch.setattr(FullBusinessSynthesis,'MAX_ITEMS',1)
    history = legacy_catalogue_history
    database_uri = history.database.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(database_uri, uri=True)) as db:
        tasks = s.semantic_checkpoint_rows(db, history.run['semantic_input_hash'])
    return SimpleNamespace(database_uri=database_uri, tasks=tasks, run=deepcopy(history.run),
                           snapshot=deepcopy(history.snapshot), projection=deepcopy(history.projection))


def test_old_completed_four_plus_one_projection_and_hash_remain_exact(historical_completed):
    c=historical_completed;before=deepcopy(c.projection)
    s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)
    assert 'catalogue_terminal' not in c.projection['verification']
    assert s._projection_hash(c.projection)==before['verification']['projection_hash']
    assert c.projection==before
    plan=frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    assert len(plan['recovery_receipt_hashes'])==4 and len(plan['supersession_receipt_hashes'])==1
    assert not plan['received_to_accept']
    assert set(fid for operation in plan['operations'] for fid in operation['fact_ids'])==set(c.projection['semantic_coverage']['input_ids'])


@pytest.fixture
def frontier_history(tmp_path,monkeypatch):
    """Root-approved truthful old history; restore current flow before exercise."""
    from test_bounded_merge_recovery import bounded_recovery_case as bounded_history
    from test_interrupted_catalogue_recovery import interrupted_recovery_case as interrupted_history
    from businessmap import semantic_recovery as recovery
    bounded_gen=interrupted_gen=None
    try:
        with monkeypatch.context() as historical:
            historical.setattr(s.BusinessSemanticIntegration,'_catalogue',s.BusinessSemanticIntegration._catalogue_legacy)
            bounded_gen=bounded_history.__wrapped__(tmp_path,monkeypatch)
            bounded=next(bounded_gen)
            interrupted_gen=interrupted_history.__wrapped__(bounded,tmp_path)
            c=next(interrupted_gen)
            c.new_receipt=recovery.authorize_catalogue_recovery(c.service,c.snapshot,c.run,c.plan,authorization=c.grant)
            original=s.BusinessSemanticIntegration._save_task
            def observe_accepted(self,key,stage,status,*args,**kwargs):
                result=original(self,key,stage,status,*args,**kwargs)
                if key==c.plan['child_stage_hash'] and status=='accepted':raise RuntimeError('observer stops after fourth accepted commit')
                return result
            historical.setattr(s.BusinessSemanticIntegration,'_save_task',observe_accepted)
            c.service.set_paused(c.project_id,False)
            stopped=c.service.analyze(c.project_id)
            assert stopped['status']!='completed'
            c.service.set_paused(c.project_id,True)
        c.received_key=None
        with c.service.store._db() as db:
            rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
            assert rows.read_checkpoint(c.plan['child_stage_hash'])['status']=='accepted'
            assert len(rows.read_checkpoint.recovery_receipts(c.run['semantic_input_hash']))==4
        capture_baseline(c)
        c.tasks=rows
        yield c
    finally:
        if interrupted_gen is not None:next(interrupted_gen,None)
        if bounded_gen is not None:next(bounded_gen,None)


def capture_baseline(c):
    with c.service.store._db() as db:
        c.old_rows=db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
        c.old_recoveries=db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()
        c.old_supersessions=db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()
        c.old_source=db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()
        c.old_l0=db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()


def configured_runtime(c):
    from businessmap import semantic_supersession as sup
    with c.service.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    units,routing=sup._source_routing(c.snapshot,c.run,rows.read_checkpoint)
    runtime=s.BusinessSemanticIntegration(c.tools,guard=lambda:None)
    runtime._snapshot,runtime._run,runtime._input_hash=c.snapshot,c.run,c.run['semantic_input_hash']
    runtime._units={unit['_fact_id']:unit for unit in units}
    runtime._forbidden=[];runtime._catalogue_tasks=[];runtime.plans=[];runtime._partitions=[]
    runtime._task_bindings={};runtime._owned_slots=set();runtime._stopped=threading.Event()
    runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
    return runtime,units,routing


@pytest.fixture
def partial_frontier_case(frontier_history,monkeypatch):
    c=frontier_history
    runtime,_,_=configured_runtime(c)
    preview=frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    assert len(preview['operations'])>2
    runtime._catalogue_origins=deepcopy(preview['bindings'])
    original=s.BusinessSemanticIntegration._save_task
    with monkeypatch.context() as interrupt:
        def observe_received(self,key,stage,status,*args,**kwargs):
            result=original(self,key,stage,status,*args,**kwargs)
            if status=='received':
                c.received_key=key
                raise RuntimeError('observer stops after genuine ordinary received commit')
            return result
        interrupt.setattr(s.BusinessSemanticIntegration,'_save_task',observe_received)
        with pytest.raises(RuntimeError):runtime._catalogue_page(preview['operations'][:2],mode='merge')
    with c.service.store._db() as db:
        c.tasks=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        received=c.tasks.read_checkpoint(c.received_key)
        assert received['status']=='received' and len(received['data']['request']['items'])==2
        assert len(received['data']['result'])==1
    c.preview=frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    assert c.preview['received_to_accept']==[c.received_key] and len(c.preview['operations'])>1
    capture_baseline(c)
    yield c


@pytest.fixture
def completed_frontier_history(partial_frontier_case,caplog):
    c=partial_frontier_case
    paid=len(c.calls)
    with caplog.at_level('INFO'):
        c.service.set_paused(c.project_id,False)
        result=c.service.analyze(c.project_id)
    assert result['status']=='completed' and result['publication_complete'],result.get('synthesis_failure')
    c.projection=c.service._load(c.project_id)['synthesis']
    c.new_calls=c.calls[paid:]
    assert c.new_calls and all(call.get('stage') in {'proposal','review','subdivide','partition_review'} for call in c.new_calls)
    with c.service.store._db() as db:
        c.tasks=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        for old in c.old_rows:
            row=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(old[0],)).fetchone()
            expected=(old[:3]+('accepted',)+old[4:]) if old[0]==c.received_key else old
            assert row==expected
        assert db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()==c.old_recoveries
        assert db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()==c.old_supersessions
        assert db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall()==c.old_source
        assert db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall()==c.old_l0
    s.validate_current_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=c.tasks)
    s.validate_stored_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.service.store)
    s.semantic_support_context(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=c.tasks.read_checkpoint)
    yield c


def test_partial_paid_forest_free_accepts_received_and_finishes_all_semantics(completed_frontier_history,caplog):
    c=completed_frontier_history
    terminal=c.projection['verification']['catalogue_terminal']
    assert set(terminal)==frontier.TERMINAL_FIELDS
    assert terminal==c.preview['terminal_candidate']
    assert len(c.projection['verification']['recovery_receipt_hashes'])==4
    assert len(c.projection['verification']['supersession_receipt_hashes'])==1
    assert len(c.projection['semantic_coverage']['input_ids'])==64
    assert c.tasks.read_checkpoint(c.parent_stage_hash)['status']=='running'
    events=[record for record in caplog.get_records('setup') if getattr(record,'event',None)=='semantic_checkpoint_committed' and getattr(record,'stage_hash',None)==c.received_key]
    assert len(events)==1 and events[0].status=='accepted'


def test_over128_terminal_operations_and_minimal_contraction_need_no_further_calls(semantic_case):
    store,snapshot,run,base,_=semantic_case
    snapshot=deepcopy(snapshot);snapshot['provider']={'id':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'}
    run,units,routing=persist_catalogue_source(store,snapshot,run,257)
    calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        items=payload['items'];groups=[[item] for item in items]
        if payload['mode']=='merge':groups=[items[:2]]+[[item] for item in items[2:]]
        return {'text':json.dumps({'operations':[{'title':group[0]['title'],'category':'flows','label_ids':[item['label_id'] for item in group]} for group in groups]},ensure_ascii=False),'usage':{},'output_limited':False}
    service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=complete))
    def runtime():
        value=s.BusinessSemanticIntegration(service,guard=lambda:None)
        value._snapshot,value._run=snapshot,run
        value._units={unit['_fact_id']:unit for unit in units};value._input_hash=run['semantic_input_hash']
        value._forbidden=[];value._catalogue_tasks=[];value.plans=[];value._task_bindings={};value._owned_slots=set();value._stopped=threading.Event()
        value._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
        return value
    first=runtime();operations=first._catalogue(routing)
    assert len(operations)==257 and [len(call['items']) for call in calls]==[128,128,1]
    assert all(call['mode']=='leaf' for call in calls)
    # A genuine already-paid refinement contracts only one item in one page;
    # the remaining transport layer is intentionally not completed.
    first._catalogue_page(operations[:128],mode='merge')
    with store._db() as db:
        rows=s.semantic_checkpoint_rows(db,first._input_hash)
        assert db.execute("SELECT json_array_length(data,'$.result') FROM business_semantic_tasks WHERE json_extract(data,'$.request.mode')='merge'").fetchone()==(127,)
    paid=len(calls)
    plan=frontier.catalogue_frontier_plan(snapshot,first._input_hash,read_checkpoint=rows.read_checkpoint,inputs=units)
    assert len(plan['operations'])==256>128
    resumed=runtime();result=resumed._catalogue(routing)
    assert len(calls)==paid and result==plan['operations']
    assert len({fid for operation in result for fid in operation['fact_ids']})==257
    assert sum(len(operation['fact_ids']) for operation in result)==257
    assert sorted(alias for operation in result for alias in operation['aliases'])==sorted(route['title'] for route in routing)
    assert len({binding['stage_hash'] for binding in plan['terminal_candidate']['frontier']})>1
    original_reader=rows.read_checkpoint
    def permuted(key):return original_reader(key)
    for name in ('raw_digest','raw_error_code','catalogue_runs','catalogue_source','recovery_receipts','supersession_receipts'):
        setattr(permuted,name,getattr(original_reader,name))
    permuted.task_rows=lambda h:list(reversed(original_reader.task_rows(h)))
    reordered=frontier.catalogue_frontier_plan(snapshot,first._input_hash,read_checkpoint=permuted,inputs=units)
    assert reordered==plan


def reseal_terminal(projection):
    proof=projection['verification'];terminal=proof.get('catalogue_terminal')
    if isinstance(terminal,dict) and set(terminal)==frontier.TERMINAL_FIELDS:
        terminal['terminal_hash']=s._hash({**{key:value for key,value in terminal.items() if key!='terminal_hash'},
            'recovery_receipt_hashes':proof.get('recovery_receipt_hashes',[]),
            'supersession_receipt_hashes':proof.get('supersession_receipt_hashes',[])})
    proof['projection_hash']=s._projection_hash(projection)


def assert_all_consumers_reject(c,projection,tasks=None):
    tasks=tasks or c.tasks
    with pytest.raises(s.SemanticValidationError):s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=tasks)
    with pytest.raises(s.SemanticValidationError):s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=tasks.read_checkpoint)
    if tasks is c.tasks:
        with pytest.raises(s.SemanticValidationError):s.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.service.store)


def frozen_rows(c,rows,receipts=None):
    source=c.tasks.read_checkpoint
    lookup={row['stage_hash']:deepcopy(row) for row in rows}
    def read(key):return deepcopy(lookup.get(key))
    for name in ('raw_digest','raw_error_code','catalogue_runs','catalogue_source','supersession_receipts'):
        setattr(read,name,getattr(source,name))
    read.task_rows=lambda h:deepcopy(list(lookup.values()))
    read.recovery_receipts=lambda h:deepcopy(receipts if receipts is not None else source.recovery_receipts(h))
    result=s.SemanticCheckpointRows(deepcopy(rows));result.read_checkpoint=read
    return result


def test_rehashed_terminal_attacks_fail_current_stored_and_support(completed_frontier_history):
    c=completed_frontier_history
    for field in sorted(frontier.TERMINAL_FIELDS):
        for mutation in ('missing','value'):
            projection=deepcopy(c.projection);terminal=projection['verification']['catalogue_terminal']
            if mutation=='missing':terminal.pop(field)
            elif field in {'page_hashes','frontier'}:terminal[field]=[]
            else:terminal[field]='tampered'
            # Keep the terminal_hash mutation itself deliberately invalid.
            if field=='terminal_hash' and mutation=='value':projection['verification']['projection_hash']=s._projection_hash(projection)
            else:reseal_terminal(projection)
            assert_all_consumers_reject(c,projection)
    for mutation in ('extra_terminal','extra_binding','missing_binding_field','duplicate_binding','reordered_binding','missing_root','duplicate_root','missing_page','logical_parent','absent_contract'):
        projection=deepcopy(c.projection);proof=projection['verification'];terminal=proof['catalogue_terminal']
        if mutation=='extra_terminal':terminal['unknown']=True
        elif mutation=='extra_binding':terminal['frontier'][0]['unknown']=True
        elif mutation=='missing_binding_field':terminal['frontier'][0].pop('result_hash')
        elif mutation=='duplicate_binding':terminal['frontier'].append(deepcopy(terminal['frontier'][0]))
        elif mutation=='reordered_binding':terminal['frontier'].reverse()
        elif mutation=='missing_root':projection['semantic_coverage']['catalogue_roots'].pop()
        elif mutation=='duplicate_root':projection['semantic_coverage']['catalogue_roots'].append(deepcopy(projection['semantic_coverage']['catalogue_roots'][0]))
        elif mutation=='missing_page':proof['catalogue_task_hashes'].remove(c.received_key)
        elif mutation=='logical_parent':terminal['page_hashes'][terminal['page_hashes'].index(c.plan['child_stage_hash'])]=c.parent_stage_hash
        else:proof.pop('catalogue_terminal')
        reseal_terminal(projection)
        assert_all_consumers_reject(c,projection)


def test_physical_page_orphan_double_consumption_and_missing_history_reject(completed_frontier_history):
    from test_interrupted_catalogue_recovery import permuted_ordinary
    c=completed_frontier_history
    for missing in (c.received_key,c.parent_stage_hash,c.plan['child_stage_hash']):
        rows=frozen_rows(c,[row for row in c.tasks if row['stage_hash']!=missing])
        assert_all_consumers_reject(c,c.projection,rows)
    receipts=c.tasks.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])
    assert_all_consumers_reject(c,c.projection,frozen_rows(c,list(c.tasks),receipts[:-1]))
    orphan=permuted_ordinary(c,c.tasks.read_checkpoint(c.received_key))
    rows=frozen_rows(c,[*c.tasks,orphan])
    assert s._checkpoint_record(orphan['stage_hash'],snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)==orphan
    projection=deepcopy(c.projection);proof=projection['verification'];proof['catalogue_task_hashes'].append(orphan['stage_hash'])
    proof['catalogue_terminal']['page_hashes'].append(orphan['stage_hash']);proof['catalogue_terminal']['page_hashes'].sort();reseal_terminal(projection)
    assert_all_consumers_reject(c,projection,rows)
    # A partial whole-operation result cannot be blessed by rehashing it.
    corrupt=deepcopy(c.tasks.read_checkpoint(c.received_key));corrupt['data']['result'][0]['fact_ids'].pop()
    corrupt['data']['result_hash']=s._hash(corrupt['data']['result'])
    rows=frozen_rows(c,[corrupt if row['stage_hash']==corrupt['stage_hash'] else row for row in c.tasks])
    assert_all_consumers_reject(c,c.projection,rows)
    # Unknown sibling remains blocked even though the original unknown parent
    # is authenticated through its already-accepted fourth recovery child.
    orphan['status']='running';orphan['data'].update(result=None,result_hash=s._hash(None))
    rows=frozen_rows(c,[*c.tasks,orphan])
    with pytest.raises(s.SemanticValidationError) as error:
        frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    assert error.value.reason_code=='interrupted'


def test_received_free_accept_guard_and_sql_rollback_emit_no_success(partial_frontier_case,monkeypatch,caplog):
    c=partial_frontier_case
    runtime,_,routing=configured_runtime(c)
    paid=len(c.calls);checks=[]
    def guard():
        checks.append(True)
        if len(checks)==2:raise RuntimeError('scope changed before received acceptance')
    runtime.guard=guard
    with caplog.at_level('INFO'),pytest.raises(RuntimeError):runtime._catalogue(routing)
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()==c.old_rows
        db.execute("CREATE TRIGGER reject_received BEFORE UPDATE OF status ON business_semantic_tasks WHEN OLD.stage_hash='"+c.received_key+"' AND NEW.status='accepted' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    runtime,_,routing=configured_runtime(c)
    with caplog.at_level('INFO'),pytest.raises(sqlite3.IntegrityError):runtime._catalogue(routing)
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()==c.old_rows
        assert db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall()==c.old_recoveries
        assert db.execute('SELECT * FROM business_semantic_supersessions ORDER BY supersession_id').fetchall()==c.old_supersessions
        db.execute('DROP TRIGGER reject_received')
    assert len(c.calls)==paid
    assert not any(getattr(record,'event',None)=='semantic_checkpoint_committed' and getattr(record,'stage_hash',None)==c.received_key and getattr(record,'status',None)=='accepted' for record in caplog.records)


def test_single_catalogue_row_still_freezes_trusted_source_bundle(historical_completed):
    c=historical_completed
    with closing(sqlite3.connect(c.database_uri,uri=True)) as source,sqlite3.connect(':memory:') as db:
        source.backup(db)
        key=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE input_hash=? AND status='accepted' AND json_extract(data,'$.request.mode')='leaf' AND json_extract(data,'$.recovery_id') IS NULL LIMIT 1",(c.run['semantic_input_hash'],)).fetchone()[0]
        db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash!=?',(key,))
        db.execute('DELETE FROM business_semantic_recoveries');db.execute('DELETE FROM business_semantic_supersessions')
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    assert len(rows)==1
    runs=rows.read_checkpoint.catalogue_runs(c.snapshot['project']['id'])
    assert any(run['run_id']==c.run['run_id'] for run in runs)
    assert len(rows.read_checkpoint.catalogue_source(c.run['run_id'])['chunks'])==32


def test_retained_legacy_runtime_replays_paid_completed_catalogue_without_calls(historical_completed,tmp_path):
    from businessmap.core import Store
    from businessmap import semantic_supersession as sup
    c=historical_completed;store=Store(tmp_path/'replay')
    try:
        with closing(sqlite3.connect(c.database_uri,uri=True)) as source,store._db() as target:source.backup(target)
        def forbidden(*args,**kwargs):raise AssertionError('completed legacy replay must never send')
        service=SimpleNamespace(store=store,providers=SimpleNamespace(complete=forbidden))
        units,routing=sup._source_routing(c.snapshot,c.run,c.tasks.read_checkpoint)
        runtime=s.BusinessSemanticIntegration(service,guard=lambda:None)
        runtime._snapshot,runtime._run,runtime._input_hash=c.snapshot,c.run,c.run['semantic_input_hash']
        runtime._units={unit['_fact_id']:unit for unit in units};runtime._forbidden=[]
        runtime._catalogue_tasks=[];runtime._catalogue_terminal=None;runtime.plans=[];runtime._task_bindings={};runtime._owned_slots=set();runtime._stopped=threading.Event()
        runtime._usage={'requests':0,'cache_hits':0,'input_tokens':0,'output_tokens':0}
        result=runtime._catalogue(routing)
        assert result==c.projection['semantic_coverage']['catalogue_roots']
        assert runtime._catalogue_terminal is None
        assert runtime._catalogue_tasks==[key for key in c.projection['verification']['catalogue_task_hashes'] if c.tasks.read_checkpoint(key)['stage']=='catalogue']
        assert runtime._usage['requests']==0
    finally:store.close()


def inline_leaf_terminal(snapshot,input_hash,rows,inputs,receipts):
    """Attack fixture independently seals caller-supplied order, never a product plan."""
    from businessmap.semantic_recovery import receipt_hash
    units=inputs if isinstance(inputs,dict) else {unit['_fact_id']:unit for unit in inputs}
    order={fid:index for index,fid in enumerate(units)}
    paid=[row for row in rows if row['status']=='accepted']
    pairs=[(row,operation) for row in paid for operation in row['data']['result']]
    pairs.sort(key=lambda pair:(min(order[fid] for fid in pair[1]['fact_ids']),pair[1]['id'],pair[0]['stage_hash']))
    operations=[deepcopy(pair[1]) for pair in pairs]
    bindings=[{'stage_hash':row['stage_hash'],'result_hash':row['data']['result_hash'],'operation_id':op['id']} for row,op in pairs]
    terminal={'contract':frontier.CONTRACT,'input_hash':input_hash,'generator_hash':s._semantic_generator_hash(snapshot),
        'page_hashes':sorted(row['stage_hash'] for row in paid),'frontier':bindings,'operations_hash':s._hash(operations)}
    terminal['terminal_hash']=s._hash({**terminal,'recovery_receipt_hashes':sorted(receipt_hash(receipt) for receipt in receipts),'supersession_receipt_hashes':[]})
    return {'verification':{'catalogue_terminal':terminal,'catalogue_task_hashes':terminal['page_hashes']},
        'semantic_coverage':{'inputs':inputs,'catalogue_roots':operations}}


@pytest.mark.parametrize('enumeration',['missing','empty'])
@pytest.mark.parametrize('consumer',['plan','final'])
def test_source_bearing_callback_never_uses_inline_order_when_run_enumeration_is_lost(historical_completed,enumeration,consumer):
    from businessmap import semantic_supersession as sup
    c=historical_completed;original=c.tasks.read_checkpoint;ih=c.run['semantic_input_hash']
    units,_=sup._source_routing(c.snapshot,c.run,original)
    rows=[deepcopy(row) for row in c.tasks if row['stage']=='catalogue' and row['data']['request'].get('mode')=='leaf']
    lookup={row['stage_hash']:row for row in rows}
    def read(key):return deepcopy(lookup.get(key))
    for name in ('raw_digest','raw_error_code','catalogue_source'):setattr(read,name,getattr(original,name))
    read.task_rows=lambda value:deepcopy(rows)
    receipts=[deepcopy(receipt) for receipt in original.recovery_receipts(ih) if receipt['child_stage_hash'] in lookup]
    read.recovery_receipts=lambda value:deepcopy(receipts)
    read.supersession_receipts=lambda value:[]
    assert read.catalogue_source(c.run['run_id'])['run']['run_id']==c.run['run_id']
    if enumeration=='empty':read.catalogue_runs=lambda project:[]
    provided={unit['_fact_id']:{'kind':unit['kind'],'text':unit['text'],'observation':unit.get('_observation',{})} for unit in reversed(units)}
    proof=inline_leaf_terminal(c.snapshot,ih,rows,provided,receipts)
    with pytest.raises(s.SemanticValidationError):
        if consumer=='plan':frontier.catalogue_frontier_plan(c.snapshot,ih,read_checkpoint=read,inputs=provided)
        else:frontier.validate_catalogue_terminal(proof,snapshot=c.snapshot,expected_input_hash=ih,read_checkpoint=read)


@pytest.mark.parametrize('run_loss',['delete_rows','drop_table'])
@pytest.mark.parametrize('consumer',['plan','final'])
def test_real_sql_source_deletion_cannot_reduce_full64_facts_to_inline8(historical_completed,run_loss,consumer):
    from businessmap import semantic_supersession as sup
    c=historical_completed;ih=c.run['semantic_input_hash']
    units,_=sup._source_routing(c.snapshot,c.run,c.tasks.read_checkpoint)
    with closing(sqlite3.connect(c.database_uri,uri=True)) as source,sqlite3.connect(':memory:') as db:
        source.backup(db)
        key=db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE input_hash=? AND status='accepted' AND json_extract(data,'$.request.mode')='leaf' AND json_extract(data,'$.recovery_id') IS NULL LIMIT 1",(ih,)).fetchone()[0]
        db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash!=?',(key,))
        db.execute('DELETE FROM business_semantic_recoveries');db.execute('DELETE FROM business_semantic_supersessions')
        db.execute('DELETE FROM full_analysis_runs' if run_loss=='delete_rows' else 'DROP TABLE full_analysis_runs')
        rows=s.semantic_checkpoint_rows(db,ih)
    members={fid for operation in rows[0]['data']['result'] for fid in operation['fact_ids']}
    provided={unit['_fact_id']:{'kind':unit['kind'],'text':unit['text'],'observation':unit.get('_observation',{})} for unit in units if unit['_fact_id'] in members}
    assert len(provided)==8 and len(units)==64
    proof=inline_leaf_terminal(c.snapshot,ih,rows,provided,[])
    with pytest.raises(s.SemanticValidationError):
        if consumer=='plan':frontier.catalogue_frontier_plan(c.snapshot,ih,read_checkpoint=rows.read_checkpoint,inputs=provided)
        else:frontier.validate_catalogue_terminal(proof,snapshot=c.snapshot,expected_input_hash=ih,read_checkpoint=rows.read_checkpoint)


def persist_catalogue_source(store,snapshot,original_run,count):
    """Real SOURCE and validated L0 cache, independent of catalogue/model results."""
    from businessmap.full_synthesis import FullBusinessSynthesis,source_fact_units
    snapshot.setdefault('repositories',[])
    run={**original_run,'project_id':snapshot['project']['id'],'modules':[{'id':'module'}],'source_repositories':[]}
    chunks=[{'id':'source','module_id':'module','status':'completed','result':{'summary':'',
        'findings':[{'text':'此项业务仅允许有权限员工处理。','evidence_ids':[]} for _ in range(count)],'unknowns':[]}}]
    run['semantic_input_hash']=s.semantic_input_hash(snapshot,run,chunks,[])
    units=source_fact_units(snapshot,run,chunks)
    titles={unit['_fact_id']:f'办理业务{index}' for index,unit in enumerate(units)}
    synthesis=FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    routing=[]
    with store._db() as db:
        db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
        db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',(run['run_id'],run['project_id'],run['snapshot_id'],s._json(run),'fixture'))
        db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)',(run['run_id'],0,s._json(chunks[0])))
        for batch in synthesis._batches(snapshot,units,0):
            response={'assignments':[{'fact_id':f'f{index}','disposition':'public','category':'flows','title':titles[unit['_fact_id']]} for index,unit in enumerate(batch)]}
            normalized=synthesis._validate(response,batch)
            key=synthesis.cache_identity(snapshot,run,batch,0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)',(key,key,s._json(normalized),'fixture'))
            routing.extend(normalized['assignments'])
    return run,units,routing


@pytest.mark.parametrize('loss',['missing_runs','empty_runs','mismatched_runs','missing_source','empty_source','mismatched_source'])
def test_source_plan_never_downgrades_an_incomplete_source_contract(historical_completed,loss):
    c=historical_completed;original=c.tasks.read_checkpoint
    def read(key):return original(key)
    for name in ('raw_digest','raw_error_code','task_rows','recovery_receipts','supersession_receipts','catalogue_source','catalogue_runs'):
        setattr(read,name,getattr(original,name))
    if loss=='missing_runs':del read.catalogue_runs
    elif loss=='empty_runs':read.catalogue_runs=lambda project:[]
    elif loss=='mismatched_runs':read.catalogue_runs=lambda project:[{**c.run,'project_revision':'foreign'}]
    elif loss=='missing_source':del read.catalogue_source
    elif loss=='empty_source':read.catalogue_source=lambda run_id:None
    else:
        def source(run_id):
            value=original.catalogue_source(run_id);value['chunks'][0]['result']['findings'][0]['text']='来源已改变。';return value
        read.catalogue_source=source
    with pytest.raises(s.SemanticValidationError):
        frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=read,inputs=c.projection['semantic_coverage']['inputs'])


def test_explicit_checkpoint_only_preview_cannot_seal_a_new_terminal(semantic_case):
    store,snapshot,run,base,_=semantic_case
    snapshot=deepcopy(snapshot);snapshot['provider']={'id':'fixture','kind':'codex-local','base_url':'codex://local','model':'fixture'}
    units=[{**deepcopy(base[0]),'_fact_id':f'preview:{index}','text':'允许有权限员工办理。'} for index in range(5)]
    routing=[{'fact_id':unit['_fact_id'],'title':f'办理业务{index}','category':'flows','disposition':'public'} for index,unit in enumerate(units)]
    calls=[]
    def complete(system,user,**kwargs):
        kwargs['before_send']();payload=json.loads(user);calls.append(payload)
        assert payload['stage']=='catalogue', 'preview must fail before downstream semantic sends'
        return {'text':json.dumps({'operations':[{'title':item['title'],'category':'flows','label_ids':[item['label_id']]} for item in payload['items']]},ensure_ascii=False),'usage':{},'output_limited':False}
    runtime=s.BusinessSemanticIntegration(SimpleNamespace(store=store,providers=SimpleNamespace(complete=complete)),guard=lambda:None)
    runtime.CATALOGUE_ITEMS=2
    with pytest.raises(s.SemanticValidationError):runtime.build(snapshot,run,units=units,routing=routing)
    assert [len(call['items']) for call in calls]==[2,2,1]
    with store._db() as db:rows=s.semantic_checkpoint_rows(db,runtime._input_hash)
    preview=frontier.checkpoint_catalogue_preview(snapshot,runtime._input_hash,read_checkpoint=rows.read_checkpoint,inputs=units)
    assert len(preview['operations'])==5 and preview['terminal_candidate'] is None
    with pytest.raises(s.SemanticValidationError):
        frontier.catalogue_frontier_plan(snapshot,runtime._input_hash,read_checkpoint=rows.read_checkpoint,inputs=units)
    provided={unit['_fact_id']:{'kind':unit['kind'],'text':unit['text'],'observation':unit.get('_observation',{})} for unit in units}
    forged=inline_leaf_terminal(snapshot,runtime._input_hash,rows,provided,[])
    with pytest.raises(s.SemanticValidationError):
        frontier.validate_catalogue_terminal(forged,snapshot=snapshot,expected_input_hash=runtime._input_hash,read_checkpoint=rows.read_checkpoint)


def test_persisted_source_canonical_order_ignores_reversed_inline_input_order(historical_completed):
    from businessmap import semantic_supersession as sup
    c=historical_completed;reader=c.tasks.read_checkpoint
    units,_=sup._source_routing(c.snapshot,c.run,reader)
    supplied={unit['_fact_id']:{'kind':unit['kind'],'text':unit['text'],'observation':unit.get('_observation',{})} for unit in reversed(units)}
    baseline=frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=reader)
    plan=frontier.catalogue_frontier_plan(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=reader,inputs=supplied)
    assert plan==baseline
    proof={'verification':{'catalogue_terminal':plan['terminal_candidate'],'catalogue_task_hashes':plan['page_hashes']},
        'semantic_coverage':{'inputs':supplied,'catalogue_roots':plan['operations']}}
    assert frontier.validate_catalogue_terminal(proof,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=reader)==plan


def test_lost_persisted_source_rejects_current_stored_and_support(completed_frontier_history):
    c=completed_frontier_history;original=c.tasks.read_checkpoint
    def read(key):return original(key)
    for name in ('raw_digest','raw_error_code','task_rows','recovery_receipts','supersession_receipts','catalogue_source'):
        setattr(read,name,getattr(original,name))
    read.catalogue_runs=lambda project:[]
    tasks=s.SemanticCheckpointRows(deepcopy(list(c.tasks)));tasks.read_checkpoint=read
    assert_all_consumers_reject(c,c.projection,tasks)
    with c.service.store._db() as db:
        saved=db.execute('SELECT * FROM full_analysis_runs').fetchall()
        db.execute('DELETE FROM full_analysis_runs')
    try:
        with pytest.raises(s.SemanticValidationError):
            s.validate_stored_semantic_projection(c.projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.service.store)
    finally:
        with c.service.store._db() as db:db.executemany('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',saved)
