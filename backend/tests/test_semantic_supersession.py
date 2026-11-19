"""Operator grant tests use only disposable files and exited mock PIDs."""
import hashlib
import json
import time
from copy import deepcopy
import pytest
from businessmap import semantic_supersession as sup
from businessmap.business_semantics import SemanticValidationError


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def grant(tmp_path, monkeypatch):
    stamp = int(time.time())
    plan = {key: key for key in sup.PLAN_FIELDS}
    plan['policy'] = sup.POLICY
    plan['parent_stage_hash'] = 'a' * 64
    evidence = tmp_path / 'evidence.txt'
    evidence.write_text('stage=' + plan['parent_stage_hash'] + ' error_type=OperationalError http_status=null codex_reason=null')
    evidence.chmod(0o600)
    check = {'checked_at': stamp, 'run_id': plan['run_id'], 'parent_stage_hash': plan['parent_stage_hash'],
        'local_owner_pids': [1234567], 'local_processes_drained': True, 'upstream_result_or_billing_reconciled': False,
        'observed_error_type': 'OperationalError', 'observed_http_status': None, 'observed_codex_reason': None,
        'actual_trace_available': False, 'evidence_ref': str(evidence), 'evidence_hash': hashlib.sha256(evidence.read_bytes()).hexdigest()}
    reconciliation = tmp_path / 'reconciliation.json'
    digest = write_private(reconciliation, check)
    auth = {key: plan[key] for key in ('policy', 'plan_hash', 'supersession_id', 'parent_stage_hash', 'child_stage_hash', 'run_id')}
    auth.update(operator_id='root', authorization_source='user-fix-and-continue', authorized_at=stamp, expires_at=stamp+3600,
        max_uses=1, reconciliation_ref=str(reconciliation), reconciliation_hash=digest)
    path = tmp_path / 'authorization.json'
    write_private(path, auth)
    def exited(pid, signal):
        raise ProcessLookupError
    monkeypatch.setattr(sup.os, 'kill', exited)
    return plan, path, auth, reconciliation, check


def test_private_grant_preflight_returns_raw_bytes_reference(grant):
    plan, path, auth, _, _ = grant
    assert sup._check_grant(path, plan) == {**auth, 'authorization_ref': hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize('field', sorted(sup.AUTH_FIELDS))
def test_every_authorization_field_required(grant, field):
    plan, path, auth, _, _ = grant
    auth.pop(field)
    write_private(path, auth)
    with pytest.raises(SemanticValidationError):
        sup._check_grant(path, plan)


@pytest.mark.parametrize('field', sorted(sup.RECONCILIATION_FIELDS))
def test_every_reconciliation_field_required(grant, field):
    plan, path, auth, reconciliation, check = grant
    check.pop(field)
    auth['reconciliation_hash'] = write_private(reconciliation, check)
    write_private(path, auth)
    with pytest.raises(SemanticValidationError):
        sup._check_grant(path, plan)


@pytest.mark.parametrize('field,value', [('max_uses', True), ('authorized_at', True), ('expires_at', True), ('operator_id', 'other'), ('authorization_source', 'model'), ('extra', 1)])
def test_authorization_strict_values(grant, field, value):
    plan, path, auth, _, _ = grant
    auth[field] = value
    write_private(path, auth)
    with pytest.raises(SemanticValidationError):
        sup._check_grant(path, plan)


def test_permissions_symlink_and_evidence_hash_fail_closed(grant, tmp_path):
    plan, path, auth, reconciliation, check = grant
    path.chmod(0o644)
    with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)
    path.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(SemanticValidationError): sup._check_grant(link, plan)
    check['evidence_hash'] = '0' * 64
    auth['reconciliation_hash'] = write_private(reconciliation, check)
    write_private(path, auth)
    with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)


@pytest.fixture
def legacy_case(tmp_path, request):
    partial_legacy = getattr(request, "param", None) == "partial"
    from types import SimpleNamespace
    from contextlib import nullcontext
    from businessmap import business_semantics as s
    from businessmap.core import Store
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units
    store = Store(tmp_path / 'state')
    snapshot = {'project': {'id': 'project', 'revision': 'rev', 'name': '门店业务'},
        'provider': {'id': 'provider', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'},
        'repositories': [], 'policies': {}, 'vault_path': str(tmp_path / 'vault')}
    run = {'run_id': 'run', 'project_id': 'project', 'snapshot_id': 'snap', 'project_revision': 'rev', 'modules': [{'id': 'module'}], 'source_repositories': []}
    chunks = [{'id': 'chunk', 'module_id': 'module', 'status': 'completed', 'result': {'summary': '',
        'findings': [{'text': '门店只能由有维护权限的人操作。', 'evidence_ids': []} for _ in range(260)], 'unknowns': []}}]
    run['semantic_input_hash'] = s.semantic_input_hash(snapshot, run, chunks, [])
    units = source_fact_units(snapshot, run, chunks)
    synthesis = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    routing = []
    with store._db() as db:
        db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)', ('run', 'project', 'snap', json.dumps(run), 'fixed'))
        db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
        db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)', ('run', 0, json.dumps(chunks[0])))
        db.execute('CREATE TABLE analysis_addenda(id TEXT,run_id TEXT,data TEXT)')
        db.execute('CREATE TABLE analysis_matching_receipts(id TEXT,run_id TEXT,data TEXT)')
        db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
        for batch in synthesis._batches(snapshot, units, 0):
            answer = {'assignments': [{'fact_id': f'f{i}', 'disposition': 'public', 'category': 'rules',
                'title': '门店业务' + chr(0x4e00 + unit['position'])} for i, unit in enumerate(batch)]}
            normalized = synthesis._validate(answer, batch)
            key = synthesis.cache_identity(snapshot, run, batch, 0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, json.dumps(normalized), 'fixed'))
            routing.extend(normalized['assignments'])
    service = SimpleNamespace(store=store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True}, project_analysis=SimpleNamespace(guard=lambda _: None))
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime._snapshot = snapshot
    runtime._units = {unit['_fact_id']: unit for unit in units}
    runtime._forbidden = []
    runtime._catalogue_tasks = []
    runtime.plans = []
    runtime._task_rows = lambda: {}
    runtime._preflight = lambda *args: None
    original_builder = runtime._catalogue_payload
    runtime._catalogue_payload = lambda *args, **kwargs: original_builder(*args, **kwargs, legacy=True)
    failed, accepted_merges = [], []
    def save(stage, system, request, normalize, *, context):
        context = {**context, 'budget_profile': runtime._profile(), 'generator_hash': s._semantic_generator_hash(snapshot)}
        key = s.semantic_task_identity_hash(snapshot, run['semantic_input_hash'], stage, request, context)
        fail = request['mode'] == 'merge' and (not partial_legacy or bool(accepted_merges))
        groups = [[item['label_id']] for item in request['items']] if partial_legacy and request['mode'] == 'leaf' else [[item['label_id'] for item in request['items']]]
        result = None if fail else normalize({'operations': [{'title': '维护门店', 'category': 'rules', 'label_ids': group} for group in groups]})
        if not fail and request['mode'] == 'merge':
            accepted_merges.append(key)
        data = {'request': request, 'context': context, 'request_hash': key, 'result': result, 'result_hash': s._hash(result)}
        if fail:
            data.update(failure_code='model_call', diagnostic={'status_code': 502})
        with store._db() as db:
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', (key, stage, run['semantic_input_hash'], 'needs_review' if fail else 'accepted', 1, json.dumps(data), 'model_call' if fail else None, 'fixed'))
        if fail:
            failed.append(key)
            raise s.SemanticValidationError('model_call')
        return result, key
    runtime._call = save
    with pytest.raises(s.SemanticValidationError, match='业务'):
        # Construct authentic legacy failed-merge history for supersession.
        runtime._catalogue_legacy(routing)
    assert len(failed) == 1
    store.config_update('full_analysis_control:run', lambda _: {'paused': True, 'cancelled': False})
    yield service, snapshot, run, failed[0]
    store.close()


def test_real_legacy_frontier_plan_preserves_order_and_frozen_reads(legacy_case):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        before = db.execute('SELECT * FROM business_semantic_tasks').fetchall()
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    frontier = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=records.read_checkpoint)
    assert len(frontier['catalogue_task_hashes']) == 3
    assert len(frontier['failed_pages']) == 1
    plan = sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=records.read_checkpoint)
    assert plan['new_request']['merge_contract'] == 'bounded-operation-navigation-v1'
    assert plan['new_context']['label_members'] == records.read_checkpoint(parent)['data']['context']['label_members']
    records.read_checkpoint.catalogue_source('run')['chunks'].clear()
    assert records.read_checkpoint.catalogue_source('run')['chunks']
    assert sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=records.read_checkpoint) == plan
    with service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks').fetchall() == before
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_supersessions'").fetchone()


def actual_grant(case, tmp_path):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = case
    with service.store._db() as db:
        plan = sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=s.semantic_checkpoint_reader(db))
    stamp = int(time.time())
    evidence = tmp_path / 'observation.txt'
    evidence.write_text(parent + ' OperationalError http_status=null codex_reason=null')
    evidence.chmod(0o600)
    reconcile = tmp_path / 'reconcile.json'
    check = {'checked_at': stamp, 'run_id': run['run_id'], 'parent_stage_hash': parent,
        'local_owner_pids': [99999999], 'local_processes_drained': True, 'upstream_result_or_billing_reconciled': False,
        'observed_error_type': 'OperationalError', 'observed_http_status': None, 'observed_codex_reason': None,
        'actual_trace_available': False, 'evidence_ref': str(evidence), 'evidence_hash': hashlib.sha256(evidence.read_bytes()).hexdigest()}
    digest = write_private(reconcile, check)
    auth = {key: plan[key] for key in ('policy', 'plan_hash', 'supersession_id', 'parent_stage_hash', 'child_stage_hash', 'run_id')}
    auth.update(operator_id='root', authorization_source='user-fix-and-continue', authorized_at=stamp, expires_at=stamp+3600,
        max_uses=1, reconciliation_ref=str(reconcile), reconciliation_hash=digest)
    path = tmp_path / 'grant.json'
    write_private(path, auth)
    return plan, path


def test_real_authorization_cas_inventory_and_immutable_parent(legacy_case, tmp_path, caplog):
    from businessmap import business_semantics as s
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    with service.store._db() as db:
        original = db.execute('SELECT * FROM business_semantic_tasks').fetchall()
    with caplog.at_level('INFO'):
        receipt = sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
        assert sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path) == receipt
    assert sum(getattr(r, 'event', None) == 'semantic_supersession_authorized' for r in caplog.records) == 1
    with service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash != ?', (plan['child_stage_hash'],)).fetchall() == original
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    assert s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])['occupied_calls'] == 4
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    with held_project_lock('project'), service.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        consumed = sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
        assert consumed['consumed_at'] is not None
    with held_project_lock('project'), service.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(SemanticValidationError):
            sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
    with service.store._db() as db:
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone() == ('running',)
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    with pytest.raises(SemanticValidationError) as failure:
        s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])
    assert failure.value.reason_code == 'interrupted'


def test_authorization_rollback_keeps_old_rows_and_no_success(legacy_case, tmp_path, caplog):
    import sqlite3
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    with service.store._db() as db:
        original = db.execute('SELECT * FROM business_semantic_tasks').fetchall()
        db.execute("CREATE TRIGGER refuse_new BEFORE INSERT ON business_semantic_tasks BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with caplog.at_level('INFO'), pytest.raises(sqlite3.IntegrityError):
        sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    with service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks').fetchall() == original
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_supersessions'").fetchone()
    assert not any(getattr(r, 'event', None) == 'semantic_supersession_authorized' for r in caplog.records)


def test_runtime_adopts_paid_leaves_dispatches_only_child_and_replays_free(legacy_case, tmp_path):
    import threading
    from types import SimpleNamespace
    from businessmap import business_semantics as s
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    sent = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        sent.append(payload)
        assert payload['merge_contract'] == s.BOUNDED_MERGE_CONTRACT
        return {'text': json.dumps({'operations': [{'title': '维护门店', 'category': 'rules', 'label_ids': [item['label_id'] for item in payload['items']]}]}), 'usage': {}, 'output_limited': False}
    service.providers = SimpleNamespace(complete=complete)
    def builder():
        runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
        runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
        with service.store._db() as db:
            units, routing = sup._source_routing(snapshot, run, s.semantic_checkpoint_reader(db))
        runtime._units = {u['_fact_id']: u for u in units}
        runtime._catalogue_tasks, runtime._task_bindings, runtime.plans, runtime._forbidden = [], {}, [], []
        runtime._owned_slots = set()
        runtime._stopped = threading.Event()
        runtime._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
        return runtime, routing
    runtime, routing = builder()
    with held_project_lock('project'):
        result = runtime._catalogue(routing)
    assert len(sent) == 1 and len(result) == 1 and len(result[0]['fact_ids']) == 260
    assert len(runtime._catalogue_tasks) == 4
    replay, routing = builder()
    assert replay._catalogue(routing) == result
    assert len(sent) == 1
    with service.store._db() as db:
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    assert s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])['occupied_calls'] == 5


@pytest.mark.parametrize('field', sorted(sup.PLAN_FIELDS))
def test_every_plan_field_tamper_is_rejected_before_authorization(legacy_case, tmp_path, field):
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    changed = deepcopy(plan)
    changed[field] = {'tampered': True}
    with pytest.raises(SemanticValidationError):
        sup.authorize_catalogue_supersession(service, snapshot, run, changed, authorization=path)
    with service.store._db() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_supersessions'").fetchone()


@pytest.mark.parametrize('field', sorted(sup.RECEIPT_FIELDS))
@pytest.mark.parametrize('mutation', ['missing', 'value'])
def test_every_receipt_field_tamper_rejected(legacy_case, tmp_path, field, mutation):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    with service.store._db() as db:
        row = json.loads(db.execute('SELECT data FROM business_semantic_supersessions').fetchone()[0])
        if mutation == 'missing':
            row.pop(field)
        else:
            row[field] = 'tampered'
        db.execute('UPDATE business_semantic_supersessions SET data=?', (json.dumps(row),))
    with service.store._db() as db, pytest.raises(SemanticValidationError):
        s.semantic_task_inventory(s.semantic_checkpoint_rows(db, run['semantic_input_hash']), snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])


def test_two_real_connections_can_consume_only_once(legacy_case, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    def consume():
        with service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
                return True
            except SemanticValidationError:
                return False
    with held_project_lock('project'), ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]


def test_scope_changes_during_guard_leave_no_receipt(legacy_case, tmp_path):
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    called = []
    def guard(_):
        called.append(True)
        if len(called) == 1:
            with service.store._db() as db:
                current = json.loads(db.execute('SELECT data FROM full_analysis_runs').fetchone()[0])
                current['catalogue_override_revision'] = 'changed'
                db.execute('UPDATE full_analysis_runs SET data=?', (json.dumps(current),))
    service.project_analysis.guard = guard
    with pytest.raises(SemanticValidationError):
        sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    with service.store._db() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_supersessions'").fetchone()


def test_expired_unconsumed_rejected_but_consumed_history_authenticates(legacy_case, tmp_path, monkeypatch):
    from businessmap import business_semantics as s
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    receipt = sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    real_time = sup.time.time
    monkeypatch.setattr(sup.time, 'time', lambda: receipt['expires_at'])
    with held_project_lock('project'), service.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(SemanticValidationError):
            sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
        assert db.execute('SELECT consumed_at FROM business_semantic_supersessions').fetchone() == (None,)
    monkeypatch.setattr(sup.time, 'time', real_time)
    with held_project_lock('project'), service.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
        reader = s.semantic_checkpoint_reader(db)
        row = reader(plan['child_stage_hash'])
        labels = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=reader)['failed_pages'][0]['labels']
        facts = [fid for label in labels for fid in label['fact_ids']]
        result = [{'id': 'operation:' + s._hash({'title': '维护门店', 'category': 'rules', 'members': sorted(facts)})[:20],
            'title': '维护门店', 'category': 'rules', 'fact_ids': facts, 'aliases': s._unique(a for label in labels for a in label['aliases'])}]
        data = {**row['data'], 'result': result, 'result_hash': s._hash(result)}
        db.execute("UPDATE business_semantic_tasks SET status='received',data=? WHERE stage_hash=?", (json.dumps(data), plan['child_stage_hash']))
    monkeypatch.setattr(sup.time, 'time', lambda: receipt['expires_at'] + 1)
    with service.store._db() as db:
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    authenticated = s._checkpoint_record(plan['child_stage_hash'], snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=records.read_checkpoint)
    assert authenticated['status'] == 'received'


@pytest.fixture
def bounded_joint_case(tmp_path, monkeypatch, record_property):
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
    from support_graph_fixture import positive_graph
    store=Store(tmp_path/'full-data');store.open_vault(str(tmp_path/'full-vault'))
    repo=tmp_path/'repo';repo.mkdir()
    for n in range(160):
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
    failed_leaf_ordinals = {13, 85}
    merge_calls = [0]
    setup_lock = threading.Lock()
    began = time.monotonic()
    original_profile = s.BusinessSemanticIntegration._profile
    def narrowed_profile(self):
        profile = original_profile(self)
        profile['stages']['catalogue']['input_bytes'] = catalogue_limit[0]
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
            assert len(units) == 640
            routes = []
            for n, unit in enumerate(units):
                title = '有权限的员工维护订单业务' + '条件' * ((n % 612) % 17) + chr(0x4e00 + n % 612)
                routing_by_fact[(unit['chunk_id'], unit['kind'], unit['text'])] = title
                routes.append({'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'rules', 'title': title})
            preview = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
            preview._snapshot = snap
            preview._units = {u['_fact_id']: u for u in units}
            def page_count(limit):
                catalogue_limit[0] = limit
                labels = preview._catalogue_initial_labels(routes)
                count, page = 0, []
                for label in labels:
                    wire, _, _ = preview._catalogue_payload(page + [label])
                    if preview._request_bytes('catalogue', '', wire) > limit:
                        count += 1
                        page = [label]
                    else:
                        page.append(label)
                return count + bool(page)
            low, high = 4000, 20000
            while low < high:
                mid = (low + high) // 2
                if page_count(mid) <= 102:
                    high = mid
                else:
                    low = mid + 1
            boundary_observation.append((low, page_count(low)))
            assert page_count(low) == 102, (low, page_count(low))
            catalogue_limit[0] = low

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
                if 'merge_contract' not in payload:
                    merge_calls[0] += 1
                    if merge_calls[0] == 10:
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
        # Keep the legacy algorithm scoped to each history-building call.
        with pytest.MonkeyPatch.context() as history:
            history.setattr(s.BusinessSemanticIntegration, '_catalogue', s.BusinessSemanticIntegration._catalogue_legacy)
            return full.analyze(project['id'])
    for ordinal in (13, 85):
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
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='leaf'").fetchone() == (102,)
        assert db.execute("SELECT count(*) FROM business_semantic_tasks WHERE status='accepted' AND json_extract(data,'$.context.catalogue_mode')='merge'").fetchone() == (9,)
        assert len(old_l0) == 640 and len(old_recoveries) == 2 and len(old_source) == 320
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
    result = full.analyze(project['id'])
    assert result['status'] == 'completed' and result['publication_complete'], (result.get('status'), result.get('synthesis_failure'), catalogue_limit[0])
    current = full._load(project['id'])
    projection = current['synthesis']
    s.validate_stored_semantic_projection(projection, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], store=store)
    with store._db() as db:
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
        assert db.execute('SELECT * FROM business_semantic_recoveries ORDER BY recovery_id').fetchall() == old_recoveries
        assert db.execute('SELECT * FROM full_analysis_synthesis ORDER BY cache_key').fetchall() == old_l0
        assert db.execute('SELECT * FROM full_analysis_chunks ORDER BY run_id,id').fetchall() == old_source
        keys = [row[0] for row in old_rows]
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash IN (%s) ORDER BY stage_hash' % ','.join('?' for _ in keys), keys).fetchall() == old_rows
    s.validate_current_semantic_projection(projection, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], task_rows=records)
    assert all(p.get('stage') in {'catalogue', 'proposal', 'review'} and p.get('mode') != 'leaf' for p in calls[paid:])
    record_property('bounded_topology', json.dumps({'facts': 640, 'source': 320, 'l0': 640, 'leaves': 102, 'v1_receipts': 2, 'old_merges': 9,
        'catalogue_input_bytes': catalogue_limit[0], 'elapsed_seconds': time.monotonic() - began, 'new_calls': len(calls) - paid,
        'source_l0_leaf_resends': 0, 'source_reconstructions': source_reconstructions[0], 'publication_complete': True}))
    import os
    export_path = os.environ.get('SIJING_JOINT_EXPORT')
    if export_path:
        import sqlite3
        from pathlib import Path
        destination = Path(export_path)
        destination.mkdir(parents=True, exist_ok=True)
        with store._db() as source_db, sqlite3.connect(destination / 'workspace.sqlite') as target_db:
            source_db.backup(target_db)
        envelope = {'snapshot': snapshot, 'run': {**current, 'semantic_input_hash': run['semantic_input_hash']},
            'projection': projection, 'source_bundle': records.read_checkpoint.catalogue_source(run['run_id']),
            'fixture_parameters': {'l0_max_items': 1, 'catalogue_items': 128, 'catalogue_input_bytes': catalogue_limit[0]},
            'vault_documents': {str(path): path.read_text() for path in store.vault.rglob('*.md')}}
        (destination / 'proof.json').write_text(json.dumps(envelope, ensure_ascii=False, indent=2))
        manifest = {'original_temporary_root': str(tmp_path), 'run_id': run['run_id'], 'status': result['status'],
            'publication_complete': result['publication_complete'],
            'files': {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in ('workspace.sqlite', 'proof.json')}}
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        for path in destination.iterdir():
            path.chmod(0o600)
    yield {'service': service, 'full': full, 'snapshot': snapshot, 'run': run, 'projection': projection, 'records': records,
        'supersession_plan': plan, 'old_rows': old_rows, 'old_recoveries': old_recoveries}
    store.close()


def test_joint_real_full_analysis_topology(bounded_joint_case):
    assert bounded_joint_case['projection']['complete'] is True


@pytest.mark.parametrize('mutation', ['alias', 'title', 'fact_order', 'routing_order', 'orphan_planned'])
def test_legacy_frontier_rejects_equal_membership_with_wrong_provenance(legacy_case, mutation):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        if mutation == 'routing_order':
            key, raw = db.execute('SELECT cache_key,data FROM full_analysis_synthesis ORDER BY rowid LIMIT 1').fetchone()
            data = json.loads(raw)
            data['assignments'].reverse()
            db.execute('UPDATE full_analysis_synthesis SET data=? WHERE cache_key=?', (json.dumps(data), key))
        else:
            key, raw = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE status='accepted' ORDER BY rowid LIMIT 1").fetchone()
            data = json.loads(raw)
            if mutation == 'orphan_planned':
                context = deepcopy(data['context'])
                request = deepcopy(data['request'])
                request['project'] = '另一个名称'
                extra = s.semantic_task_identity_hash(snapshot, run['semantic_input_hash'], 'catalogue', request, context)
                record = {'request': request, 'context': context, 'request_hash': extra, 'result': None, 'result_hash': s._hash(None)}
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', (extra, 'catalogue', run['semantic_input_hash'], 'planned', 1, json.dumps(record), None, 'fixed'))
            else:
                operation = data['result'][0]
                if mutation == 'alias': operation['aliases'].reverse()
                if mutation == 'title': operation['title'] = '不同门店动作'
                if mutation == 'fact_order': operation['fact_ids'].reverse()
                operation['id'] = 'operation:' + s._hash({'title': operation['title'], 'category': operation['category'], 'members': sorted(operation['fact_ids'])})[:20]
                data['result_hash'] = s._hash(data['result'])
                db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (json.dumps(data), key))
    with service.store._db() as db, pytest.raises(SemanticValidationError):
        sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=s.semantic_checkpoint_reader(db))


def test_completed_legacy_frontier_replays_without_any_provider(legacy_case):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        frontier = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=reader)
        labels = frontier['failed_pages'][0]['labels']
        facts = [fid for label in labels for fid in label['fact_ids']]
        result = [{'id': 'operation:' + s._hash({'title': '维护门店', 'category': 'rules', 'members': sorted(facts)})[:20],
            'title': '维护门店', 'category': 'rules', 'fact_ids': facts, 'aliases': s._unique(a for label in labels for a in label['aliases'])}]
        data = reader(parent)['data']
        data.pop('failure_code')
        data.pop('diagnostic')
        data.update(result=result, result_hash=s._hash(result))
        db.execute("UPDATE business_semantic_tasks SET status='accepted',data=?,error_code=NULL WHERE stage_hash=?", (json.dumps(data), parent))
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
    with service.store._db() as db:
        units, routing = sup._source_routing(snapshot, run, s.semantic_checkpoint_reader(db))
    runtime._units = {u['_fact_id']: u for u in units}
    runtime._catalogue_tasks, runtime.plans = [], []
    assert runtime._catalogue(routing) == result
    assert runtime._catalogue_tasks[-1] == parent
    assert len(runtime._catalogue_tasks) == 4


def test_readonly_sql_bundle_does_not_create_supersession_schema(legacy_case):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        db.execute('PRAGMA query_only=ON')
        reader = s.semantic_checkpoint_reader(db)
        statements = []
        db.set_trace_callback(statements.append)
        sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=reader)
        assert not any(query.lstrip().upper().startswith(('CREATE', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for query in statements)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_supersessions'").fetchone()


@pytest.mark.parametrize('field', sorted(sup.AUTH_FIELDS))
def test_every_authorization_value_rejected(grant, field):
    plan, path, auth, _, _ = grant
    auth[field] = 'tampered'
    write_private(path, auth)
    with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)


@pytest.mark.parametrize('field', sorted(sup.RECONCILIATION_FIELDS))
def test_every_reconciliation_value_rejected(grant, field):
    plan, path, auth, reconciliation, check = grant
    check[field] = 'tampered'
    auth['reconciliation_hash'] = write_private(reconciliation, check)
    write_private(path, auth)
    with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)


@pytest.mark.parametrize('schema', ['plan', 'authorization', 'reconciliation', 'receipt'])
def test_unknown_fields_rejected(grant, legacy_case, tmp_path, schema):
    from businessmap import business_semantics as s
    plan, path, auth, reconciliation, check = grant
    if schema == 'plan': plan['extra'] = 1
    if schema == 'authorization':
        auth['extra'] = 1
        write_private(path, auth)
    if schema == 'reconciliation':
        check['extra'] = 1
        auth['reconciliation_hash'] = write_private(reconciliation, check)
        write_private(path, auth)
    if schema != 'receipt':
        with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)
        return
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    with service.store._db() as db:
        receipt = json.loads(db.execute('SELECT data FROM business_semantic_supersessions').fetchone()[0])
        receipt['extra'] = 1
        db.execute('UPDATE business_semantic_supersessions SET data=?', (json.dumps(receipt),))
    with service.store._db() as db, pytest.raises(SemanticValidationError):
        s.semantic_task_inventory(s.semantic_checkpoint_rows(db, run['semantic_input_hash']), snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])


@pytest.mark.parametrize('field', sorted(sup.PLAN_FIELDS))
def test_every_plan_field_required_by_file_preflight(grant, field):
    plan, path, _, _, _ = grant
    plan.pop(field)
    with pytest.raises(SemanticValidationError): sup._check_grant(path, plan)


def test_normal_bounded_checkpoint_rejects_changed_project_even_with_new_hash(legacy_case):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        plan = sup.catalogue_supersession_plan(snapshot, run, parent, read_checkpoint=reader)
        request = deepcopy(plan['new_request'])
        request['project'] = '另一项目'
        key = s.semantic_task_identity_hash(snapshot, run['semantic_input_hash'], 'catalogue', request, plan['new_context'])
        row = {'stage_hash': key, 'stage': 'catalogue', 'input_hash': run['semantic_input_hash'], 'status': 'planned', 'attempt': 1,
            'data': {'request_hash': key, 'request': request, 'context': plan['new_context'], 'result': None, 'result_hash': s._hash(None)}}
        def changed(stage_hash): return row if stage_hash == key else reader(stage_hash)
        with pytest.raises(SemanticValidationError):
            s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=changed)


def test_concurrent_identical_authorizers_commit_one_receipt(legacy_case, tmp_path, caplog):
    from concurrent.futures import ThreadPoolExecutor
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    with caplog.at_level('INFO'), ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(lambda _: sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path), range(2)))
    assert receipts[0] == receipts[1]
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_supersessions').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone() == (1,)
    assert sum(getattr(r, 'event', None) == 'semantic_supersession_authorized' for r in caplog.records) == 1


def test_consumption_rolls_back_receipt_when_task_update_aborts(legacy_case, tmp_path):
    import sqlite3
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    with service.store._db() as db:
        db.execute("CREATE TRIGGER refuse_running BEFORE UPDATE OF status ON business_semantic_tasks WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with held_project_lock('project'), pytest.raises(sqlite3.IntegrityError):
        with service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            sup.consume_supersession(db, snapshot, run, plan['child_stage_hash'])
    with service.store._db() as db:
        assert db.execute('SELECT consumed_at,dispatch_id FROM business_semantic_supersessions').fetchone() == (None, None)
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone() == ('planned',)


def test_claimed_supersession_admission_incomplete_cannot_resend(legacy_case, tmp_path):
    import threading
    from types import SimpleNamespace
    from businessmap import business_semantics as s
    from businessmap.semantic_recovery import held_project_lock
    service, snapshot, run, parent = legacy_case
    plan, path = actual_grant(legacy_case, tmp_path)
    sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_update('full_analysis_control:run', lambda _: {'paused': False, 'cancelled': False})
    calls = []
    def complete(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('provider setup failed before admission')
    service.providers = SimpleNamespace(complete=complete)
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
    runtime._catalogue_tasks, runtime._task_bindings, runtime.plans, runtime._forbidden = [], {}, [], []
    runtime._owned_slots = set()
    runtime._stopped = threading.Event()
    runtime._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    with held_project_lock('project'), pytest.raises(RuntimeError, match='provider setup'):
        runtime._call('catalogue', s.BOUNDED_MERGE_SYSTEM, plan['new_request'], lambda x: x, context=plan['new_context'])
    with service.store._db() as db:
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
        reader = records.read_checkpoint
        row = reader(plan['child_stage_hash'])
        receipt = s.semantic_checkpoint_reader(db).supersession_receipts(run['semantic_input_hash'])[0]
    assert receipt['consumed_at'] is not None
    assert receipt['dispatch_id'] is not None
    assert row['status'] == 'needs_review' and row['data']['failure_code'] == 'admission_incomplete'
    assert row['data']['result'] is None and 'diagnostic' not in row['data'] and 'feedback' not in row['data']
    assert sup.authenticate_supersession(row, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=reader) == receipt
    with pytest.raises(s.SemanticValidationError) as failure:
        s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'])
    assert failure.value.reason_code == 'admission_incomplete'
    with held_project_lock('project'), pytest.raises(s.SemanticValidationError) as failure:
        runtime._call('catalogue', s.BOUNDED_MERGE_SYSTEM, plan['new_request'], lambda x: x, context=plan['new_context'])
    assert failure.value.reason_code == 'admission_incomplete'
    assert len(calls) == 1
    with service.store._db() as db:
        assert s.semantic_checkpoint_reader(db)(plan['child_stage_hash']) == row
        assert s.semantic_checkpoint_reader(db).supersession_receipts(run['semantic_input_hash'])[0] == receipt


@pytest.fixture
def completed_legacy_catalogue(legacy_case, tmp_path, request):
    """Three paid legacy leaves, failed merge, normal bounded child, full D1."""
    from types import SimpleNamespace
    from businessmap import business_semantics as s
    from businessmap.semantic_recovery import held_project_lock
    def records(case):
        with case[0].store._db() as db:
            return s.semantic_checkpoint_rows(db, case[2]["semantic_input_hash"])
    service, snapshot, run, parent = legacy_case
    if getattr(request, 'param', None) == 'no_receipt':
        # Paid prefix of a layer, stopped before a later page was dispatched.
        with service.store._db() as db:
            db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?', (parent,))
        plan = path = receipt = None
        before = records(legacy_case)
    else:
        plan, path = actual_grant(legacy_case, tmp_path)
        before = records(legacy_case)
        receipt = sup.authorize_catalogue_supersession(service, snapshot, run, plan, authorization=path)
    service.store.config_set('full_analysis_control:run', {'paused': False, 'cancelled': False})
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        wire = json.loads(user)
        calls.append(wire)
        if wire['stage'] == 'catalogue':
            groups, group, count = [], [], 0
            for item in wire['items']:
                if group and (count + item['fact_count'] > 70 or getattr(request, 'param', None) != 'no_receipt'):
                    groups.append(group)
                    group, count = [], 0
                group.append(item['label_id'])
                count += item['fact_count']
            if group:
                groups.append(group)
            result = {'operations': [{'title': '维护门店', 'category': 'rules', 'label_ids': group} for group in groups]}
        elif wire['stage'] == 'subdivide':
            result = {'operations': [{'title': '维护门店', 'category': 'rules', 'label_ids': [v['label_id'] for v in wire['items'][i:i+50]]} for i in range(0, len(wire['items']), 50)]}
        elif wire['stage'] == 'partition_review':
            result = {'decision': 'accept', 'issues': [],
                'parent_checks': {k: True for k in ('complete_partition', 'no_shared_conditions_lost', 'no_cross_child_dependencies', 'unknowns_preserved')},
                'input_checks': [{'input_id': v['fact_id'], 'checks': {k: True for k in ('membership', 'conditions', 'scope', 'uncertainty', 'relationships')}} for v in wire['items']],
                'operation_checks': [{'operation_id': v['operation_id'], 'checks': {k: True for k in ('independent_operation', 'complete_conditions', 'complete_unknowns', 'no_cross_child_dependencies')}} for v in wire['children']]}
        elif wire['stage'] == 'proposal':
            result = {'operations': [{'operation_id': v['operation_id'], 'purpose': '', 'summary': '', 'support_ids': []} for v in wire['operations']],
                'claims': [], 'decisions': [{'input_id': v['fact_id'], 'parts': [{'outcome': 'source_scope', 'rationale': '保留局部资料观察。'}]} for v in wire['items']]}
        else:
            result = {'decision': 'accept', 'issues': [],
                'input_checks': [{'input_id': v['fact_id'], 'checks': {k: True for k in ('coverage', 'conditions', 'scope', 'uncertainty', 'technical_boundary')}} for v in wire['items']],
                'claim_checks': [], 'operation_checks': [{'operation_id': v['operation_id'], 'checks': {k: True for k in ('business_language', 'complete_flow', 'relationships', 'unknowns')}} for v in wire['candidate']['operations']]}
        return {'text': json.dumps(result), 'usage': {}, 'output_limited': False}
    service.providers = SimpleNamespace(complete=complete)
    with service.store._db() as db:
        units, routing = sup._source_routing(snapshot, run, s.semantic_checkpoint_reader(db))
    builder = s.BusinessSemanticIntegration(service, guard=lambda: None)
    builder.CONCURRENCY = 1
    with held_project_lock(snapshot['project']['id']):
        projection = builder.build(snapshot, run, units=units, routing=routing)
    tasks = records(legacy_case)
    # Product gates establish this is a valid fixture before the independent gate.
    s.validate_current_semantic_projection(projection, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], task_rows=tasks)
    s.validate_stored_semantic_projection(projection, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], store=service.store)
    return SimpleNamespace(case=legacy_case, service=service, snapshot=snapshot, run=run, parent=parent,
        plan=plan, grant=path, receipt=receipt, projection=projection, tasks=tasks, before=before, calls=calls,
        originals=projection['semantic_coverage']['inputs'])


@pytest.mark.parametrize('gate', ['current', 'stored', 'support'])
def test_rehashed_orphan_bounded_page_rejected_by_product(completed_legacy_catalogue, gate):
    from businessmap import business_semantics as s
    c = completed_legacy_catalogue
    orphan = deepcopy(c.tasks.read_checkpoint(c.plan['child_stage_hash']))
    orphan['data'].pop('supersession_id')
    data = orphan['data']
    request, context = data['request'], data['context']
    olditems, oldmembers, oldparents = deepcopy(request['items']), deepcopy(context['label_members']), deepcopy(context['catalogue_parents'])
    request['items'] = []
    for i, item in enumerate(reversed(olditems)):
        oldkey, key = item['label_id'], f'l{i}'
        request['items'].append({**item, 'label_id': key})
        context['label_members'][key] = oldmembers[oldkey]
        context['catalogue_parents'][key] = oldparents[oldkey]
    request['membership_hash'] = s._hash(context['label_members'])
    key = s.semantic_task_identity_hash(c.snapshot, c.run['semantic_input_hash'], 'catalogue', request, context)
    assert key != c.plan['child_stage_hash']
    orphan['stage_hash'] = data['request_hash'] = key
    with c.service.store._db() as db:
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
            (key, 'catalogue', c.run['semantic_input_hash'], 'accepted', 1, s._json(data), None, s.now()))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    # Each page is individually valid: only the global ancestry is invalid.
    assert s._checkpoint_record(key, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint) == orphan
    projection = deepcopy(c.projection)
    projection['verification']['catalogue_task_hashes'].append(key)
    projection['verification']['projection_hash'] = s._projection_hash(projection)
    with pytest.raises(s.SemanticValidationError):
        if gate == 'current':
            s.validate_current_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], task_rows=rows)
        elif gate == 'stored':
            s.validate_stored_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], store=c.service.store)
        else:
            s.semantic_support_context(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint)


@pytest.mark.parametrize('legacy_case', ['partial'], indirect=True)
@pytest.mark.parametrize('completed_legacy_catalogue', ['no_receipt'], indirect=True)
def test_partial_paid_legacy_frontier_without_receipt_closes(completed_legacy_catalogue):
    from businessmap import business_semantics as s
    c = completed_legacy_catalogue
    legacy = [r for r in c.before if r['data']['request'].get('mode') == 'merge']
    assert legacy and all(r['status'] == 'accepted' for r in legacy)
    assert 'supersession_receipt_hashes' not in c.projection['verification']
    assert all(call['mode'] == 'merge' for call in c.calls if call['stage'] == 'catalogue')
    assert all(c.tasks.read_checkpoint(old['stage_hash']) == old for old in c.before)
    s.semantic_support_context(c.projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=c.tasks.read_checkpoint)


@pytest.mark.parametrize('mutation', ['duplicate_run', 'sql_identity', 'missing_source_seam', 'missing_page', 'missing_declared_page'])
def test_catalogue_closure_requires_unique_source_and_complete_pages(completed_legacy_catalogue, mutation):
    from businessmap import business_semantics as s
    c = completed_legacy_catalogue
    projection = deepcopy(c.projection)
    with c.service.store._db() as db:
        if mutation == 'duplicate_run':
            duplicate = {**c.run, 'run_id': 'duplicate'}
            db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)', ('duplicate', 'project', 'snap', json.dumps(duplicate), 'fixed'))
            db.execute("INSERT INTO full_analysis_chunks SELECT 'duplicate',position,data FROM full_analysis_chunks WHERE run_id='run'")
        elif mutation == 'sql_identity':
            db.execute("UPDATE full_analysis_runs SET snapshot_id='tampered'")
        elif mutation == 'missing_page':
            db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?', (c.before[0]['stage_hash'],))
        elif mutation == 'missing_declared_page':
            projection['verification']['catalogue_task_hashes'].remove(c.before[0]['stage_hash'])
            projection['verification']['projection_hash'] = s._projection_hash(projection)
    with pytest.raises(s.SemanticValidationError):
        with c.service.store._db() as db:
            rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
        if mutation == 'missing_source_seam':
            del rows.read_checkpoint.catalogue_runs
        s.semantic_support_context(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint)


def test_catalogue_runs_are_scoped_and_frozen_in_sql_snapshot(legacy_case):
    from businessmap import business_semantics as s
    service, snapshot, run, parent = legacy_case
    with service.store._db() as db:
        db.execute('PRAGMA query_only=ON')
        rows = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    reader = rows.read_checkpoint
    assert reader.catalogue_runs('other-project') == []
    first = reader.catalogue_runs('project')
    assert first == [run]
    first[0]['modules'].clear()
    assert reader.catalogue_runs('project') == [run]
    with service.store._db() as db:
        db.execute("UPDATE full_analysis_runs SET data='{}'")
    assert reader.catalogue_runs('project') == [run]
