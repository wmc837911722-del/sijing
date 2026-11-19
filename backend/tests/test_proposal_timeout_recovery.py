"""The one-shot policy accepts only an ordinary failed D1 timeout."""
from copy import deepcopy

import pytest
from businessmap import business_semantics as s


def timeout_parent():
    return {'stage': 'proposal', 'attempt': 1, 'status': 'needs_review', 'data': {
        'request': {'stage': 'proposal'}, 'context': {'revision': s._revision_context()},
        'failure_code': 'model_call', 'result': None, 'result_hash': s._hash(None),
        'diagnostic': {'status_code': 408, 'codex_reason': 'timeout'}}}


def test_only_ordinary_first_draft_timeout_is_eligible():
    from businessmap.semantic_proposal_recovery import _eligible
    snapshot = {'provider': {'kind': 'codex-local', 'base_url': 'codex://local'}}
    # Structured-output is a provider contract, independent of the task metadata.
    assert s._structured_output(snapshot)
    _eligible(timeout_parent(), snapshot)
    for change in ('status', 'attempt', 'http', 'reason', 'result', 'recovery', 'whole', 'revision'):
        row = deepcopy(timeout_parent())
        if change == 'status': row['status'] = 'running'
        elif change == 'attempt': row['attempt'] = 2
        elif change == 'http': row['data']['diagnostic']['status_code'] = 502
        elif change == 'reason': row['data']['diagnostic']['codex_reason'] = 'provider_error'
        elif change == 'result': row['data']['result'] = {}
        elif change == 'recovery': row['data']['recovery_id'] = 'x'
        elif change == 'whole': row['data']['context']['whole_operation'] = {}
        elif change == 'revision': row['data']['context']['revision'] = s._revision_context(2)
        with pytest.raises(s.SemanticValidationError):
            _eligible(row, snapshot)


@pytest.fixture
def source_timeout(tmp_path, request):
    import json
    import threading
    from types import SimpleNamespace
    from businessmap.full_synthesis import FullBusinessSynthesis, source_fact_units
    from businessmap.providers import ToolError
    from test_business_semantics import semantic_case

    fixture = semantic_case.__wrapped__(tmp_path)
    store, snapshot, original_run, _, _ = next(fixture)
    snapshot = deepcopy(snapshot)
    snapshot['provider'] = {'id': 'fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}
    snapshot['repositories'] = []
    run = {**original_run, 'project_id': snapshot['project']['id'], 'modules': [{'id': 'module'}]}
    chunks = [{'id': 'source', 'module_id': 'module', 'status': 'completed', 'result': {'summary': '',
        'findings': [{'text': '办理业务需要核对门店权限。', 'evidence_ids': []}, {'text': '办理业务需要核对适用范围。', 'evidence_ids': []}], 'unknowns': []}}]
    multiple_packets = getattr(request, 'param', None) == 'two-packets'
    if multiple_packets:
        findings = chunks[0]['result']['findings']
        findings.extend({'text': item['text'].replace('办理业务', '提交申请'), 'evidence_ids': []} for item in findings[:])
        for item in findings:
            item['text'] *= 220
    run['semantic_input_hash'] = s.semantic_input_hash(snapshot, run, chunks, [])
    units = source_fact_units(snapshot, run, chunks)
    synthesis = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    routing = []
    with store._db() as db:
        db.execute('CREATE TABLE full_analysis_runs(id TEXT,project_id TEXT,snapshot_id TEXT,data TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE full_analysis_chunks(run_id TEXT,position INTEGER,data TEXT)')
        db.execute('CREATE TABLE full_analysis_synthesis(cache_key TEXT,input_hash TEXT,data TEXT,created_at TEXT)')
        db.execute('CREATE TABLE analysis_addenda(id TEXT,run_id TEXT,data TEXT)')
        db.execute('CREATE TABLE analysis_matching_receipts(id TEXT,run_id TEXT,data TEXT)')
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)', (run['run_id'], run['project_id'], run['snapshot_id'], s._json(run), 'fixture'))
        db.execute('INSERT INTO full_analysis_chunks VALUES (?,?,?)', (run['run_id'], 0, s._json(chunks[0])))
        for batch in synthesis._batches(snapshot, units, 0):
            response = {'assignments': [{'fact_id': 'f'+str(i), 'disposition': 'public', 'category': 'flows',
                'title': '提交申请' if item['text'].startswith('提交申请') else '办理业务'} for i, item in enumerate(batch)]}
            normalized = synthesis._validate(response, batch)
            key = synthesis.cache_identity(snapshot, run, batch, 0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, s._json(normalized), 'fixture'))
            routing.extend(normalized['assignments'])
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        if payload['stage'] == 'proposal':
            error = ToolError('timeout', 408)
            error.codex_reason = 'timeout'
            raise error
        result = {'operations': [{'title': item['title'], 'category': 'flows', 'label_ids': [item['label_id']]} for item in payload['items']]}
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    service = SimpleNamespace(store=store, providers=SimpleNamespace(complete=complete))
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
    runtime._units = {unit['_fact_id']: unit for unit in units}
    runtime._forbidden, runtime._matching, runtime._partitions = [], [], []
    runtime._catalogue_tasks, runtime.plans, runtime._task_bindings = [], [], {}
    runtime._owned_slots, runtime._stopped = set(), threading.Event()
    runtime._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    operations = runtime._catalogue_page(runtime._catalogue_initial_labels(routing))
    packets = runtime._packets(operations)
    assert len(packets) == (2 if multiple_packets else 1)
    with pytest.raises(s.SemanticValidationError):
        runtime._integrate_packet(packets[-1])
    with store._db() as db:
        rows = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    parent = next(row for row in rows if row['stage'] == 'proposal')
    yield SimpleNamespace(store=store, snapshot=snapshot, run=run, parent=parent, runtime=runtime, packet=packets[-1], packets=packets, units=units, routing=routing)
    fixture.close()


def test_plan_rebuilds_packet_from_genuine_source_and_rejects_source_tampering(source_timeout):
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    c = source_timeout
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        plan = proposal_timeout_recovery_plan(c.snapshot, c.run, c.parent['stage_hash'], read_checkpoint=reader)
    assert plan['physical_maximum_calls'] == 5
    assert plan['replacement_allowance'] == 1
    assert plan['logical_draft_maximum'] == 2
    assert plan['parent_stage_hash'] == c.parent['stage_hash']
    with c.store._db() as db:
        db.execute("UPDATE full_analysis_chunks SET data = replace(data, '核对门店权限', '不核对门店权限')")
    with c.store._db() as db:
        with pytest.raises(s.SemanticValidationError):
            proposal_timeout_recovery_plan(c.snapshot, c.run, c.parent['stage_hash'], read_checkpoint=s.semantic_checkpoint_reader(db))


def proposal_grant(c, tmp_path):
    import hashlib
    import json
    import time
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    with c.store._db() as db:
        plan = proposal_timeout_recovery_plan(c.snapshot, c.run, c.parent['stage_hash'], read_checkpoint=s.semantic_checkpoint_reader(db))
    now = int(time.time())
    evidence = {'checked_at': now, 'local_owner_pids': [99999999], 'local_processes_drained': True,
        'upstream_result_or_billing_reconciled': False, 'run_id': c.run['run_id'],
        'parent_stage_hash': c.parent['stage_hash'], 'parent_row_digest': plan['parent_failure_digest'],
        'inventory_digest': plan['inventory_digest']}
    evidence_path = tmp_path / 'evidence.json'
    evidence_path.write_text(json.dumps(evidence)); evidence_path.chmod(0o600)
    auth = {'policy': plan['policy'], 'plan_hash': plan['plan_hash'], 'parent_stage_hash': c.parent['stage_hash'],
        'run_id': c.run['run_id'], 'operator_id': 'root', 'authorization_source': 'user-fix-and-continue',
        'authorized_at': now, 'expires_at': now + 3600, 'max_uses': 1, 'reconciliation_ref': str(evidence_path),
        'reconciliation_hash': hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
    path = tmp_path / 'grant.json'; path.write_text(json.dumps(auth)); path.chmod(0o600)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': True, 'cancelled': False})
    return plan, path


def test_authorize_is_idempotent_and_never_mutates_parent(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True, 'cancelled': False},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    with c.store._db() as db:
        original = db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?', (c.parent['stage_hash'],)).fetchone()
    receipt = authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    assert authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path) == receipt
    with c.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?', (c.parent['stage_hash'],)).fetchone() == original
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0] == 1
        child = s.semantic_checkpoint_reader(db)(plan['child_stage_hash'])
        assert child['status'] == 'planned' and child['data']['request'] == c.parent['data']['request']


def _prepare_proposal_consume(c, plan):
    """Prepare using the same trusted runtime binding as the controller."""
    key = plan['child_stage_hash']
    with c.store._db() as db:
        row = s.semantic_checkpoint_reader(db)(key)
    c.runtime._task_bindings[key] = {name: deepcopy(row['data'][name])
        for name in ('request', 'context', 'recovery_id', 'logical_task_hash')}
    c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'],key))
    return c.runtime._prepare_mutation(key, operation='consume_recovery')


def _consume_prepared_fixture(db, c, plan, prepared):
    from businessmap.semantic_recovery import consume_recovery
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    return consume_recovery(db, c.snapshot, c.run, plan['child_stage_hash'],
        source_material=prepared._PreparedMutation__material, prepared_mutation=prepared)


def test_consumed_child_is_one_shot_and_parent_remains_occupied(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery, held_project_lock
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True, 'cancelled': False},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': False, 'cancelled': False})
    prepared = _prepare_proposal_consume(c, plan)
    with held_project_lock(c.snapshot['project']['id']), c.store._db() as db:
        _consume_prepared_fixture(db, c, plan, prepared)
    with held_project_lock(c.snapshot['project']['id']), c.store._db() as db:
        with pytest.raises(s.SemanticValidationError):
            consume_recovery(db, c.snapshot, c.run, plan['child_stage_hash'])
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    result = s.semantic_task_inventory(rows, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'],
        allow_running={plan['child_stage_hash']})
    # Includes the accepted catalogue, failed parent and consumed physical child.
    assert result['occupied_calls'] == 3


def test_runtime_requires_exact_allowlist_then_reviews_actual_child(source_timeout, tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate, review
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True, 'cancelled': False},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': False, 'cancelled': False})
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user); calls.append(payload)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload)
        return {'text':json.dumps(result, ensure_ascii=False), 'usage':{}, 'output_limited':False}
    c.runtime.service.providers.complete = complete
    with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert calls == []
    c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'], plan['child_stage_hash']))
    with held_project_lock(c.snapshot['project']['id']):
        result = c.runtime._integrate_packet(c.packet)
    assert [call['stage'] for call in calls] == ['proposal', 'review']
    assert calls[1]['proposal_hash'] == plan['child_stage_hash']
    assert result['review_hash']


from test_whole_operation_exact_fit import mixed_source_case, whole_semantic_case


def test_source_plan_preserves_other_atomic_history(mixed_source_case):
    import json
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    c = mixed_source_case
    packets = c.runtime._packets(c.roots)
    packet = next(p for p in packets if any(op['title'] == '普通业务' for op in p))
    def fail(system, user, **kwargs):
        kwargs['before_send']()
        error = ToolError('timeout', 408); error.codex_reason = 'timeout'; raise error
    c.runtime.service.providers.complete = fail
    with pytest.raises(s.SemanticValidationError):
        c.runtime._integrate_packet(packet)
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        parent = next(r for r in reader.task_rows(c.run['semantic_input_hash']) if r['stage'] == 'proposal')
        plan = proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=reader, atomic_budget_chains=(c.four_keys,))
    assert plan['physical_maximum_calls'] == 5


def test_quality_rejection_still_allows_only_one_real_second_draft(source_timeout, tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate, review
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': False, 'cancelled': False})
    c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'], plan['child_stage_hash']))
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user); calls.append(payload)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload, reject=sum(x['stage']=='review' for x in calls) == 1)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    with held_project_lock(c.snapshot['project']['id']):
        c.runtime._integrate_packet(c.packet)
    assert [x['stage'] for x in calls] == ['proposal', 'review', 'proposal', 'review']
    assert calls[1]['proposal_hash'] == plan['child_stage_hash']
    assert {'prior_candidate', 'revision_feedback', 'preservation'} <= calls[2].keys()
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    related = [r for r in rows if r['stage'] in {'proposal', 'review'}]
    assert len(related) == 5 and all(r['status'] != 'planned' for r in related)
    assert s.semantic_task_inventory(rows, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'])['occupied_calls'] == 6
    before = len(calls)
    with held_project_lock(c.snapshot['project']['id']):
        projection = c.runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert len(calls) == before
    s.validate_stored_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], store=c.store)


def test_projection_closes_parent_and_recovery_and_replays_without_send(source_timeout, tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from test_business_semantics import independent_candidate, review
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': False, 'cancelled': False})
    c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'], plan['child_stage_hash']))
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user); calls.append(payload)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    with held_project_lock(c.snapshot['project']['id']):
        projection = c.runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    s.validate_current_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], task_rows=rows)
    s.validate_stored_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], store=c.store)
    before = len(calls)
    with held_project_lock(c.snapshot['project']['id']):
        replay = c.runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert len(calls) == before
    assert s._projection_hash(projection) == s._projection_hash(replay)
    assert len(projection['verification']['recovery_receipt_hashes']) == 1


from test_whole_operation_exact_fit import whole_parent_case


def test_source_plan_reconstructs_accepted_partition(whole_parent_case):
    import json
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    from test_business_semantics import partition_review
    c = whole_parent_case
    c.runtime.WHOLE_FRESH_ENABLED = False
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '处理业务'+str(i//10), 'category': 'flows', 'label_ids': ids[i:i+10]} for i in range(0,len(ids),10)]}
        elif payload['stage'] == 'partition_review':
            result = partition_review(payload)
        else:
            error = ToolError('timeout', 408); error.codex_reason = 'timeout'; raise error
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    packets = c.runtime._packets([c.parent])
    with pytest.raises(s.SemanticValidationError):
        c.runtime._integrate_packet(packets[0])
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        parent = next(r for r in reader.task_rows(c.run['semantic_input_hash']) if r['stage'] == 'proposal')
        plan = proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=reader)
    assert plan['packet_hash'] == parent['data']['context']['packet_hash']


def test_unknown_other_failure_blocks_consumption(source_timeout, tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery, held_project_lock
    c = source_timeout
    plan, path = proposal_grant(c, tmp_path)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'], {'paused': False, 'cancelled': False})
    prepared = _prepare_proposal_consume(c, plan)
    with c.store._db() as db:
        catalogue = db.execute("SELECT stage_hash,data FROM business_semantic_tasks WHERE stage='catalogue'").fetchone()
        data = json.loads(catalogue[1]); data['failure_code'] = 'model_call'; data['result'] = None; data['result_hash'] = s._hash(None)
        db.execute("UPDATE business_semantic_tasks SET status='needs_review',data=?,error_code='model_call' WHERE stage_hash=?", (s._json(data), catalogue[0]))
    with held_project_lock(c.snapshot['project']['id']), c.store._db() as db, pytest.raises(s.SemanticValidationError):
        _consume_prepared_fixture(db, c, plan, prepared)
    with c.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone()[0] == 'planned'


def test_concurrent_authorizers_commit_one_child(source_timeout, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Barrier
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c = source_timeout; plan, path = proposal_grant(c, tmp_path); barrier = Barrier(2)
    service = SimpleNamespace(store=c.store, _exclusive=lambda _: nullcontext(), _controls=lambda _: {'paused': True},
        project_analysis=SimpleNamespace(guard=lambda _: None))
    def authorize():
        barrier.wait()
        return authorize_proposal_timeout_recovery(service, c.snapshot, c.run, plan, authorization=path)
    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(lambda _: authorize(), range(2)))
    assert a == b
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?', (plan['child_stage_hash'],)).fetchone()[0] == 1


def test_source_plan_accepts_exact_atomic_and_whole_histories(mixed_source_case):
    import json
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    from test_business_semantics import partition_review
    c = mixed_source_case
    whole = next(root for root in c.roots if root['title'] == '父业务B')
    def prepare(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '处理业务'+str(i//10), 'category':'flows', 'label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        else:
            result = partition_review(payload, reject=True)
        return {'text':json.dumps(result, ensure_ascii=False), 'usage':{}, 'output_limited':False}
    c.runtime.service.providers.complete = prepare
    labels = [{'title':whole['title'], 'category':whole['category'], 'fact_ids':[fid], 'aliases':whole['aliases']} for fid in whole['fact_ids']]
    children = c.runtime._catalogue_page(labels, stage='subdivide'); sub = c.runtime._catalogue_tasks[-1]
    with pytest.raises(s.SemanticValidationError) as failure:
        c.runtime._verify_partition(whole, children, sub)
    pair = (sub, failure.value.stage_hash)
    c.runtime.whole_operation_allowlist = {pair}
    packets = c.runtime._packets(c.roots)
    packet = next(p for p in packets if any(op['title'] == '普通业务' for op in p))
    def timeout(system, user, **kwargs):
        kwargs['before_send'](); error = ToolError('timeout', 408); error.codex_reason='timeout'; raise error
    c.runtime.service.providers.complete = timeout
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packet)
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        parent = next(r for r in reader.task_rows(c.run['semantic_input_hash']) if r['stage'] == 'proposal')
        with pytest.raises(s.SemanticValidationError):
            proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=reader, atomic_budget_chains=(c.four_keys,))
        plan = proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=reader,
            atomic_budget_chains=(c.four_keys,), whole_histories=(pair,))
    assert plan['atomic_budget_chains'] == [list(c.four_keys)] and plan['whole_histories'] == [list(pair)]


def test_source_plan_reconstructs_selected_accepted_partition_repair(whole_parent_case):
    import json
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    from test_business_semantics import partition_review
    c = whole_parent_case; c.runtime.WHOLE_FRESH_ENABLED = False
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload=json.loads(user)
        if payload['stage'] == 'subdivide':
            ids=[item['label_id'] for item in payload['items']]
            result={'operations':[{'title':'处理业务'+str(i//10),'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        elif payload['stage'] == 'partition_review':
            result=partition_review(payload, reject='partition_revision' not in payload)
        else:
            error=ToolError('timeout',408); error.codex_reason='timeout'; raise error
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError) as rejected: c.runtime._packets([c.parent])
    with c.store._db() as db:
        reader=s.semantic_checkpoint_reader(db); review=reader(rejected.value.stage_hash)
    pair=(review['data']['context']['subdivision_hash'], review['stage_hash'])
    c.runtime.partition_repair_allowlist={pair}
    packets=c.runtime._packets([c.parent])
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packets[0])
    with c.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        parent=next(r for r in reader.task_rows(c.run['semantic_input_hash']) if r['stage']=='proposal')
        with pytest.raises(s.SemanticValidationError):
            proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader)
        plan=proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader,partition_repair_pairs=(pair,))
        assert proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader,partition_repair_pairs=[list(pair)])==plan
        for invalid in ([[pair[0]]], [list(pair),list(pair)], [list(reversed(pair))], [['a'*64,'b'*64]]):
            with pytest.raises(s.SemanticValidationError):
                proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader,partition_repair_pairs=invalid)
    assert plan['partition_repair_pairs']==[list(pair)]


def test_crash_after_consumption_never_resends_or_creates_second_child(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery, held_project_lock
    c=source_timeout; plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    prepared = _prepare_proposal_consume(c, plan)
    with held_project_lock(c.snapshot['project']['id']), c.store._db() as db:
        _consume_prepared_fixture(db, c, plan, prepared)
    calls=[]
    c.runtime.service.providers.complete=lambda *args,**kwargs:calls.append(args)
    c.runtime.proposal_timeout_recovery_allowlist.add((plan['parent_stage_hash'],plan['child_stage_hash']))
    with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError):
        c.runtime._integrate_packet(c.packet)
    assert calls==[]
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==1
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='running'


@pytest.mark.parametrize('mutation', ['parent_raw', 'child_request', 'receipt_budget', 'orphan'])
def test_inventory_rejects_tampered_ancestry(source_timeout, tmp_path, mutation):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c=source_timeout; plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    with c.store._db() as db:
        if mutation=='parent_raw':
            db.execute("UPDATE business_semantic_tasks SET created_at='changed' WHERE stage_hash=?",(plan['parent_stage_hash'],))
        elif mutation=='child_request':
            data=json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0])
            data['request']['project']='changed'
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?',(s._json(data),plan['child_stage_hash']))
        elif mutation=='receipt_budget':
            data=json.loads(db.execute('SELECT data FROM business_semantic_recoveries').fetchone()[0]);data['physical_maximum_calls']=6
            db.execute('UPDATE business_semantic_recoveries SET data=?',(s._json(data),))
        else:
            db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],))
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    with pytest.raises(s.SemanticValidationError):
        s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])


def test_consumed_receipt_retains_evidence_after_external_files_disappear(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery, held_project_lock
    c=source_timeout; plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    assert receipt['reconciliation_evidence']
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    prepared = _prepare_proposal_consume(c, plan)
    with held_project_lock(c.snapshot['project']['id']), c.store._db() as db:
        _consume_prepared_fixture(db, c, plan, prepared)
    path.unlink(); (tmp_path/'evidence.json').unlink()
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
    s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],allow_running={plan['child_stage_hash']})


from test_full_analysis import full_project


def test_real_full_controller_recovers_and_publishes_once(full_project, monkeypatch, tmp_path):
    import hashlib
    import json
    import time
    from businessmap.full_synthesis import FullBusinessSynthesis
    from businessmap.project_analysis import ProjectAnalysis
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan, authorize_proposal_timeout_recovery
    from test_business_semantics import independent_candidate, review
    from test_semantic_integration import REAL_SYNTHESIS_BUILD, REAL_STORED_VALIDATOR
    full, service, project = full_project
    full.SOURCE_BYTES = 37000
    monkeypatch.setattr(FullBusinessSynthesis, 'build', REAL_SYNTHESIS_BUILD)
    monkeypatch.setattr(s, 'validate_stored_semantic_projection', REAL_STORED_VALIDATOR)
    provider = service.providers.save({'name': 'controller fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'})
    ProjectAnalysis(service).bind_model(project['id'], provider['id'])
    calls=[]; timeout_sent=False
    def complete(system, user, **kwargs):
        nonlocal timeout_sent
        kwargs['before_send'](); payload=json.loads(user)
        stage=payload.get('stage','source' if 'files' in payload else 'routing')
        calls.append((stage,payload))
        if stage=='source':
            result={'title':'业务办理','summary':'','findings':[{'text':'办理业务需要核对门店授权。',
                'evidence_ids':[payload['files'][0]['evidence_id']]}], 'unknowns':[]}
        elif stage=='routing':
            result={'assignments':[{'fact_id':x['fact_id'],'disposition':'public','category':'flows','title':'办理业务'} for x in payload['items']]}
        elif stage=='catalogue':
            result={'operations':[{'title':'办理业务','category':'flows','label_ids':[x['label_id'] for x in payload['items']]}]}
        elif stage=='proposal':
            if not timeout_sent:
                timeout_sent=True; error=ToolError('timeout',408); error.codex_reason='timeout'; raise error
            result=independent_candidate(payload)
        elif stage=='review': result=review(payload)
        else: raise AssertionError(stage)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    monkeypatch.setattr(service.providers,'complete',complete)
    failed=full.analyze(project['id'])
    assert failed['status']=='failed' and timeout_sent, failed
    snapshot=full.project_analysis.snapshot(project['id']); persisted=full._load(project['id'])
    with service.store._db() as db:
        reader=s.semantic_checkpoint_reader(db)
        parents=[row for row in reader.task_rows(db.execute("SELECT input_hash FROM business_semantic_tasks WHERE stage='proposal'").fetchone()[0]) if row['stage']=='proposal']
        assert len(parents)==1
        parent=parents[0]; original=reader.raw_checkpoint_row(parent['stage_hash'])
        run={**persisted,'semantic_input_hash':parent['input_hash']}
    full.set_paused(project['id'],True)
    with service.store._db() as db:
        temporary_reader=s.semantic_checkpoint_reader(db)
        source_before=temporary_reader.catalogue_source(run['run_id'])
        plan=proposal_timeout_recovery_plan(snapshot,run,parent['stage_hash'],read_checkpoint=temporary_reader)
    stamp=int(time.time())
    evidence={'checked_at':stamp,'local_owner_pids':[99999999],'local_processes_drained':True,
        'upstream_result_or_billing_reconciled':False,'run_id':run['run_id'],'parent_stage_hash':parent['stage_hash'],
        'parent_row_digest':plan['parent_failure_digest'],'inventory_digest':plan['inventory_digest']}
    evidence_path=tmp_path/'full-recovery-evidence.json';evidence_path.write_text(json.dumps(evidence));evidence_path.chmod(0o600)
    auth={'policy':plan['policy'],'plan_hash':plan['plan_hash'],'parent_stage_hash':parent['stage_hash'],'run_id':run['run_id'],
        'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':stamp,'expires_at':stamp+3600,
        'max_uses':1,'reconciliation_ref':str(evidence_path),'reconciliation_hash':hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
    path=tmp_path/'full-recovery-grant.json';path.write_text(json.dumps(auth));path.chmod(0o600)
    receipt=authorize_proposal_timeout_recovery(full,snapshot,run,plan,authorization=path)
    assert receipt['consumed_at'] is None
    monkeypatch.setattr(s.BusinessSemanticIntegration,'proposal_timeout_recovery_allowlist', {(plan['parent_stage_hash'],plan['child_stage_hash'])})
    full.set_paused(project['id'],False)
    completed=full.analyze(project['id'])
    assert completed['status']=='completed' and completed['publication_complete'] is True, (completed.get('synthesis_failure'), [stage for stage,_ in calls])
    final=full._load(project['id']); projection=final['synthesis']
    assert final['status']=='completed' and final['publication_complete'] is True
    with service.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        assert rows.read_checkpoint.raw_checkpoint_row(parent['stage_hash'])==original
        assert rows.read_checkpoint(plan['child_stage_hash'])['status']=='proposed'
        receipts=rows.read_checkpoint.recovery_receipts(run['semantic_input_hash'])
        assert len(receipts)==1 and receipts[0]['consumed_at'] is not None
    s.validate_current_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],task_rows=rows)
    s.validate_stored_semantic_projection(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],store=service.store)
    s.semantic_support_context(projection,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    stages=[stage for stage,_ in calls]
    assert stages.count('proposal')==2 and stages.count('review')==1
    assert next(payload for stage,payload in calls if stage=='review')['proposal_hash']==plan['child_stage_hash']


def test_legal_controller_progress_does_not_change_recovery_identity(source_timeout):
    import json
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    c=source_timeout
    with c.store._db() as db:
        before=proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=s.semantic_checkpoint_reader(db))
    with c.store._db() as db:
        run=json.loads(db.execute('SELECT data FROM full_analysis_runs').fetchone()[0])
        run.update(status='running',error='temporary state',synthesis_failure={'reason':'temporary'},updated_at='later')
        db.execute('UPDATE full_analysis_runs SET data=?',(s._json(run),))
    with c.store._db() as db:
        after=proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=s.semantic_checkpoint_reader(db))
    assert after==before


@pytest.mark.parametrize('field', ['run_id', 'project_id', 'snapshot_id', 'project_revision', 'source_repositories', 'catalogue_override_revision', 'module_ids'])
def test_stable_source_run_dependency_cannot_change(source_timeout, field):
    import json
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan
    from businessmap.providers import ToolError
    c=source_timeout
    with c.store._db() as db:
        run=json.loads(db.execute('SELECT data FROM full_analysis_runs').fetchone()[0])
        if field=='source_repositories': run[field]=[{'repository_id':'other','commit':'a'*40}]
        elif field=='module_ids': run['modules']=[{'id':'other'}]
        else: run[field]='other'
        db.execute('UPDATE full_analysis_runs SET data=?',(s._json(run),))
    with c.store._db() as db, pytest.raises((s.SemanticValidationError, ToolError)):
        proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=s.semantic_checkpoint_reader(db))


def test_partition_repair_json_plan_authorizes_consumes_and_replays(whole_parent_case, tmp_path):
    import hashlib
    import json
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.providers import ToolError
    from businessmap.semantic_proposal_recovery import proposal_timeout_recovery_plan, authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import held_project_lock
    from businessmap import semantic_supersession as sup
    from test_business_semantics import partition_review, independent_candidate, review
    c=whole_parent_case; c.runtime.WHOLE_FRESH_ENABLED=False
    timeout=True; calls=[]
    def complete(system,user,**kwargs):
        nonlocal timeout
        kwargs['before_send'](); payload=json.loads(user); stage=payload['stage']; calls.append(payload)
        if stage=='subdivide':
            ids=[item['label_id'] for item in payload['items']]
            titles=['接收业务','核对业务','处理业务','完成业务','归档业务','查看业务','确认业务']
            result={'operations':[{'title':titles[i//10],'category':'flows','label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        elif stage=='partition_review': result=partition_review(payload,reject='partition_revision' not in payload)
        elif stage=='proposal' and timeout:
            timeout=False; error=ToolError('timeout',408); error.codex_reason='timeout'; raise error
        elif stage=='proposal': result=independent_candidate(payload)
        elif stage=='review': result=review(payload)
        else: raise AssertionError(stage)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    c.runtime.service.providers.complete=complete
    with pytest.raises(s.SemanticValidationError) as rejected: c.runtime._packets([c.parent])
    with c.store._db() as db:
        reader=s.semantic_checkpoint_reader(db); previous=reader(rejected.value.stage_hash)
    pair=(previous['data']['context']['subdivision_hash'],previous['stage_hash'])
    c.runtime.partition_repair_allowlist={pair}
    packets=c.runtime._packets([c.parent])
    with pytest.raises(s.SemanticValidationError): c.runtime._integrate_packet(packets[0])
    with c.store._db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS analysis_addenda(id TEXT,run_id TEXT,data TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS analysis_matching_receipts(id TEXT,run_id TEXT,data TEXT)')
        reader=s.semantic_checkpoint_reader(db)
        parent=next(row for row in reader.task_rows(c.run['semantic_input_hash']) if row['stage']=='proposal')
        original=reader.raw_checkpoint_row(parent['stage_hash'])
        plan=proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader,partition_repair_pairs=(pair,))
    assert plan['partition_repair_pairs']==[list(pair)]
    # Round trip exactly as the operator driver and SQLite receipt do.
    plan=json.loads(json.dumps(plan))
    now=int(time.time()); evidence={'checked_at':now,'local_owner_pids':[99999999],'local_processes_drained':True,
        'upstream_result_or_billing_reconciled':False,'run_id':c.run['run_id'],'parent_stage_hash':parent['stage_hash'],
        'parent_row_digest':plan['parent_failure_digest'],'inventory_digest':plan['inventory_digest']}
    evidence_path=tmp_path/'repair-evidence.json';evidence_path.write_text(json.dumps(evidence));evidence_path.chmod(0o600)
    auth={'policy':plan['policy'],'plan_hash':plan['plan_hash'],'parent_stage_hash':parent['stage_hash'],'run_id':c.run['run_id'],
        'operator_id':'root','authorization_source':'user-fix-and-continue','authorized_at':now,'expires_at':now+3600,'max_uses':1,
        'reconciliation_ref':str(evidence_path),'reconciliation_hash':hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
    path=tmp_path/'repair-grant.json';path.write_text(json.dumps(auth));path.chmod(0o600)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':True,'cancelled':False})
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True,'cancelled':False},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    receipt=authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    assert receipt['partition_repair_pairs']==[list(pair)] and receipt['consumed_at'] is None
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    c.runtime.proposal_timeout_recovery_allowlist={(plan['parent_stage_hash'],plan['child_stage_hash'])}
    with c.store._db() as db:
        reader=s.semantic_checkpoint_reader(db); units,routing=sup._source_routing(c.snapshot,c.run,reader)
    with held_project_lock(c.snapshot['project']['id']):
        projection=c.runtime.build(c.snapshot,c.run,units=units,routing=routing)
    assert projection['complete'] is True
    with c.store._db() as db:
        rows=s.semantic_checkpoint_rows(db,c.run['semantic_input_hash'])
        assert rows.read_checkpoint.raw_checkpoint_row(parent['stage_hash'])==original
        assert rows.read_checkpoint(plan['child_stage_hash'])['status']=='proposed'
        receipts=rows.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])
        assert len(receipts)==1 and receipts[0]['consumed_at'] is not None and receipts[0]['partition_repair_pairs']==[list(pair)]
    s.validate_current_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],task_rows=rows)
    s.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    path.unlink(); evidence_path.unlink()
    s.validate_stored_semantic_projection(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],store=c.store)
    s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],read_checkpoint=rows.read_checkpoint)
    count=len(calls)
    with held_project_lock(c.snapshot['project']['id']):
        replay=c.runtime.build(c.snapshot,c.run,units=units,routing=routing)
    assert len(calls)==count and s._projection_hash(replay)==s._projection_hash(projection)


def test_transaction_snapshots_share_routing_only_within_each_capture(source_timeout, tmp_path):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap import semantic_supersession as sup
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery, held_project_lock
    c=source_timeout; plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    counts={}; phase=['authorization']
    def profile(frame,event,arg):
        if event=='call' and frame.f_code is sup._derive_source_routing.__code__:
            counts[phase[0]]=counts.get(phase[0],0)+1
    try:
        sys.setprofile(profile)
        authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
        sys.setprofile(None)
        c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
        prepared = _prepare_proposal_consume(c, plan)
        phase[0]='consume';sys.setprofile(profile)
        with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
            _consume_prepared_fixture(db,c,plan,prepared)
    finally: sys.setprofile(None)
    assert counts=={'authorization':1}, counts



@pytest.mark.parametrize('drift', ['grant','evidence','clock','pid','guard'])
def test_authorization_rechecks_evidence_and_scope_after_inventory(source_timeout,tmp_path,monkeypatch,drift):
    import os
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    state={'changed':False,'guards':0};before=int(time.time());orig_kill=os.kill
    def guard(_):
        state['guards'] += 1
        if state['guards'] != 3:
            return
        state['changed']=True
        if drift=='grant': path.write_text(path.read_text()+' ')
        if drift=='evidence': (tmp_path/'evidence.json').write_text('{}')
        if drift=='clock': monkeypatch.setattr(time,'time',lambda:before+61)
        if drift=='pid': monkeypatch.setattr(os,'kill',lambda pid,sig: None if pid==99999999 else orig_kill(pid,sig))
        if drift=='guard': raise s.SemanticValidationError('cache_invalid')
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=guard))
    with pytest.raises(s.SemanticValidationError):
        authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    assert state['changed']
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='business_semantic_recoveries'").fetchone()
        assert not exists or db.execute('SELECT count(*) FROM business_semantic_recoveries').fetchone()[0]==0


def test_authorization_snapshots_do_not_hide_inserted_child(source_timeout,tmp_path):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    from businessmap import _prepared_mutation as prepared_module
    captures=[]
    def profile(frame,event,arg):
        if event=='return' and frame.f_code is prepared_module._capture_dynamic.__code__:
            captures.append(arg)
    try:
        sys.setprofile(profile);authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    finally: sys.setprofile(None)
    tables=[dict(item._DynamicCapture__tables) for item in captures]
    before=tables[0];after=tables[-1]
    assert all(row[0]!=plan['child_stage_hash'] for row in before['business_semantic_tasks'][1])
    assert any(row[0]==plan['child_stage_hash'] and row[3]=='planned' for row in after['business_semantic_tasks'][1])
    assert before['business_semantic_recoveries'] is None
    assert len(after['business_semantic_recoveries'][1])==1


@pytest.mark.parametrize('drift',['evidence','clock','pid','control'])
def test_consumption_rechecks_scope_immediately_before_cas(source_timeout,tmp_path,monkeypatch,drift):
    import os
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    before=int(time.time());original_kill=os.kill;changed=[]
    prepared=_prepare_proposal_consume(c,plan)
    with pytest.raises(s.SemanticValidationError):
        with held_project_lock(c.snapshot['project']['id']),c.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed.append(True)
            if drift=='evidence': (tmp_path/'evidence.json').write_text('{}')
            if drift=='clock': monkeypatch.setattr(time,'time',lambda:before+3601)
            if drift=='pid': monkeypatch.setattr(os,'kill',lambda pid,sig:None if pid==99999999 else original_kill(pid,sig))
            if drift=='control': db.execute('UPDATE config SET value=? WHERE key=?',(s._json({'paused':True,'cancelled':False}),'full_analysis_control:'+c.run['run_id']))
            _consume_prepared_fixture(db,c,plan,prepared)
    assert changed
    with c.store._db() as db:
        assert db.execute('SELECT consumed_at FROM business_semantic_recoveries').fetchone()[0] is None
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='planned'


def test_authorization_rolls_back_if_database_controls_change_before_commit(source_timeout,tmp_path):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    with c.store._db() as db:
        db.execute("CREATE TRIGGER drift_control AFTER INSERT ON business_semantic_tasks WHEN NEW.stage='proposal' BEGIN UPDATE config SET value='{}' WHERE key='full_analysis_control:' || 'run'; END")
    with pytest.raises(s.SemanticValidationError):
        authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]==0
        assert json.loads(db.execute('SELECT value FROM config WHERE key=?',('full_analysis_control:'+c.run['run_id'],)).fetchone()[0])['paused'] is True


def test_simultaneous_proposal_consumers_burn_one_child(source_timeout,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Barrier
    from types import SimpleNamespace
    from businessmap.semantic_proposal_recovery import authorize_proposal_timeout_recovery
    from businessmap.semantic_recovery import consume_recovery,held_project_lock
    c=source_timeout;plan,path=proposal_grant(c,tmp_path)
    service=SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True},project_analysis=SimpleNamespace(guard=lambda _:None))
    authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=path)
    c.store.config_set('full_analysis_control:'+c.run['run_id'],{'paused':False,'cancelled':False})
    prepared = [_prepare_proposal_consume(c, plan) for _ in range(2)]
    barrier=Barrier(2)
    def consume(index):
        barrier.wait()
        try:
            with c.store._db() as db:
                _consume_prepared_fixture(db,c,plan,prepared[index])
            return True
        except s.SemanticValidationError: return False
    with held_project_lock(c.snapshot['project']['id']),ThreadPoolExecutor(2) as pool:
        result=list(pool.map(consume,range(2)))
    assert sorted(result)==[False,True]
    with c.store._db() as db:
        assert db.execute('SELECT count(*) FROM business_semantic_recoveries WHERE consumed_at IS NOT NULL').fetchone()[0]==1
        assert db.execute('SELECT status FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()[0]=='running'
