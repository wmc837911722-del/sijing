"""Bounded partition repair: paid history remains immutable and admission is explicit."""
import importlib

import pytest

from businessmap import business_semantics as s
from test_business_semantics import semantic_case, oversized_case, execute
from businessmap.providers import ToolError


def test_rejected_partition_has_no_implicit_repair_admission(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    rejected = [row for row in rows if row['stage'] == 'partition_review']
    assert len(rejected) == 1 and rejected[0]['status'] == 'needs_review'
    assert rejected[0]['data']['failure_code'] == 'review_rejected'
    runtime = setup[0]
    assert getattr(runtime, 'partition_repair_allowlist', None) == set()
    with pytest.raises(ToolError):
        execute(setup)
    assert [call['stage'] for call in setup[1]] == ['catalogue', 'subdivide', 'partition_review']


def test_repair_plan_rejects_missing_source_and_untrusted_parents(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    subdivision = next(row for row in rows if row['stage'] == 'subdivide')
    review = next(row for row in rows if row['stage'] == 'partition_review')
    module = importlib.import_module('businessmap.semantic_partition_repair')
    with pytest.raises(s.SemanticValidationError) as error:
        module.partition_repair_plan(semantic_case[1], setup[0]._input_hash,
            read_checkpoint=rows.read_checkpoint, parent_subdivision_hash=subdivision['stage_hash'],
            parent_review_hash=review['stage_hash'])
    assert error.value.reason_code == 'cache_invalid'


def test_parent_lineage_authentication_rejects_changed_feedback(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    sub = next(row for row in rows if row['stage'] == 'subdivide')
    review = next(row for row in rows if row['stage'] == 'partition_review')
    module = importlib.import_module('businessmap.semantic_partition_repair')
    lineage = module.authenticate_parent(semantic_case[1], setup[0]._input_hash,
        read_checkpoint=rows.read_checkpoint, parent_subdivision_hash=sub['stage_hash'],
        parent_review_hash=review['stage_hash'])
    assert lineage['feedback_hash'] == s._hash({'issues': review['data']['issues']})
    assert lineage['draft_index'] == 2


def test_inventory_exempts_only_authenticated_selected_reject(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    sub = next(row for row in rows if row['stage'] == 'subdivide')
    review = next(row for row in rows if row['stage'] == 'partition_review')
    pair = (sub['stage_hash'], review['stage_hash'])
    inventory = s.semantic_task_inventory(rows, snapshot=semantic_case[1],
        expected_input_hash=setup[0]._input_hash, partition_repair_pairs=(pair,))
    assert inventory['occupied_calls'] == 3
    with pytest.raises(s.SemanticValidationError):
        s.semantic_task_inventory(rows, snapshot=semantic_case[1],
            expected_input_hash=setup[0]._input_hash, partition_repair_pairs=((pair[1], pair[0]),))


def test_repair_system_is_conditional_and_legacy_unchanged(semantic_case):
    module = importlib.import_module('businessmap.semantic_partition_repair')
    snapshot = semantic_case[1]
    assert s.semantic_stage_system(snapshot, 'subdivide', {'stage': 'subdivide'}) == s.CATALOGUE_SYSTEM
    rev = {'contract': module.CONTRACT, 'draft_index': 2, 'parent_subdivision_hash': 'a'*64,
        'parent_review_hash': 'b'*64, 'parent_partition_hash': 'c'*64, 'feedback_hash': 'd'*64}
    request = {'stage': 'subdivide', 'partition_revision': rev,
        'prior_children': [], 'partition_feedback': {'issues': []}}
    assert s.semantic_stage_system(snapshot, 'subdivide', request) == s.CATALOGUE_SYSTEM + '\n' + module.REPAIR_SYSTEM_SUFFIX


def test_draft_reconstruction_preserves_owned_running_request(semantic_case):
    setup = oversized_case(semantic_case, independent=False, reject=True)
    with pytest.raises(ToolError):
        execute(setup)
    with semantic_case[0]._db() as db:
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    sub = next(row for row in rows if row['stage'] == 'subdivide')
    review = next(row for row in rows if row['stage'] == 'partition_review')
    m = importlib.import_module('businessmap.semantic_partition_repair')
    args = (semantic_case[1], setup[0]._input_hash)
    kw = dict(read_checkpoint=rows.read_checkpoint, parent_subdivision_hash=sub['stage_hash'],
              parent_review_hash=review['stage_hash'])
    draft = m._draft(*args, **kw)
    assert draft['status'] == 'planned' and draft['review_hash'] is None
    with semantic_case[0]._db() as db:
        data = {'request_hash': draft['subdivision_hash'], 'result': None, 'result_hash': s._hash(None),
                'request': draft['subdivision_request'], 'context': draft['subdivision_context']}
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
            (draft['subdivision_hash'], 'subdivide', setup[0]._input_hash, 'running', 1, s._json(data), None, 'fixture'))
        rows = s.semantic_checkpoint_rows(db, setup[0]._input_hash)
    kw['read_checkpoint'] = rows.read_checkpoint
    running = m._draft(*args, **kw)
    assert running['status'] == 'blocked'
    assert running['subdivision_request'] == draft['subdivision_request']
    assert running['subdivision_hash'] == draft['subdivision_hash']
    assert running['review_hash'] is None


def test_runtime_copies_scoped_class_allowlist(semantic_case, monkeypatch):
    store = semantic_case[0]
    from types import SimpleNamespace
    pair = ('a'*64, 'b'*64)
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'partition_repair_allowlist', {pair})
    runtime = s.BusinessSemanticIntegration(SimpleNamespace(store=store), guard=lambda: None)
    assert runtime.partition_repair_allowlist == {pair}
    runtime.partition_repair_allowlist.clear()
    assert s.BusinessSemanticIntegration.partition_repair_allowlist == {pair}


from copy import deepcopy
import json
from types import SimpleNamespace
from test_complete_catalogue_frontier import frontier_history, configured_runtime
from test_business_semantics import partition_review
from businessmap import semantic_catalogue_frontier as frontier
from businessmap import semantic_supersession as sup


@pytest.fixture
def partition_repair_case(tmp_path, monkeypatch):
    original = s.BusinessSemanticIntegration._profile
    def profile(self):
        result = original(self)
        result['candidate_bytes'] = 2000
        return result
    monkeypatch.setattr(s.BusinessSemanticIntegration, '_profile', profile)
    generator = frontier_history.__wrapped__(tmp_path, monkeypatch)
    c = next(generator)
    runtime, units, routing = configured_runtime(c)
    _, _, addenda = sup._source_inputs(c.snapshot, c.run, c.tasks.read_checkpoint)
    runtime._matching = [m for a in addenda for m in a.get('matching_receipts', [])]
    operations = frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'],
        read_checkpoint=c.tasks.read_checkpoint)['operations']
    parent = max((item for item in operations if len(item['fact_ids']) <= runtime._profile()['catalogue_items']), key=lambda item: len(item['fact_ids']))
    assert not runtime._packet_fits([parent])
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        calls.append(payload)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            response = {'operations': [{'title': '业务操作' + str(i), 'category': 'rules', 'label_ids': ids[i:i+2]}
                                       for i in range(0, len(ids), 2)]}
        elif payload['stage'] == 'partition_review':
            response = partition_review(payload, reject=True)
        else:
            raise AssertionError('unexpected stage')
        return {'text': json.dumps(response, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    runtime.service = SimpleNamespace(store=runtime.store, providers=SimpleNamespace(complete=complete))
    labels = [{'title': parent['title'], 'category': parent['category'], 'fact_ids': [fid], 'aliases': parent['aliases']}
              for fid in parent['fact_ids']]
    children = runtime._catalogue_page(labels, stage='subdivide')
    subhash = runtime._catalogue_tasks[-1]
    with pytest.raises(s.SemanticValidationError) as error:
        runtime._verify_partition(parent, children, subhash)
    assert error.value.reason_code == 'review_rejected'
    with runtime.store._db() as db:
        tasks = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    review = next(row for row in tasks if row['stage'] == 'partition_review' and row['status'] == 'needs_review')
    with runtime.store._db() as db:
        old_rows = db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
    yield SimpleNamespace(store=runtime.store, service=runtime.service, snapshot=c.snapshot, run=c.run,
        old_rows=old_rows, units=units, routing=routing, calls=calls, runtime=runtime, parent=parent,
        parent_subdivision_hash=subhash, parent_review_hash=review['stage_hash'], tasks=tasks)
    next(generator, None)


def test_real_source_plan_preserves_d1_inputs_and_two_call_limit(partition_repair_case):
    c = partition_repair_case
    m = importlib.import_module('businessmap.semantic_partition_repair')
    plan = m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=c.tasks.read_checkpoint,
        parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash)
    assert plan['status'] == 'planned' and plan['remaining_maximum_calls'] == 2
    assert plan['review_hash'] is None and plan['parent_operation'] == c.parent
    original = c.tasks.read_checkpoint(c.parent_subdivision_hash)['data']['request']
    assert all(plan['subdivision_request'][key] == value for key, value in original.items())
    assert plan['subdivision_request']['partition_feedback']['issues'] == c.tasks.read_checkpoint(c.parent_review_hash)['data']['issues']


def test_selected_runtime_regroups_once_and_strictly_reviews(partition_repair_case):
    c = partition_repair_case
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        c.calls.append(payload)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            response = {'operations': [{'title': '修正业务操作' + str(i), 'category': 'rules', 'label_ids': ids[i:i+2]}
                                       for i in range(0, len(ids), 2)]}
        elif payload['stage'] == 'partition_review':
            response = partition_review(payload)
        else:
            raise AssertionError('unexpected stage')
        return {'text': json.dumps(response, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    c.runtime.service.providers.complete = complete
    c.runtime.partition_repair_allowlist = {(c.parent_subdivision_hash, c.parent_review_hash)}
    packets = c.runtime._packets([c.parent])
    assert packets and len(c.runtime._partitions) == 1
    assert [item['stage'] for item in c.calls] == ['subdivide', 'partition_review', 'subdivide', 'partition_review']
    proof = c.runtime._partitions[0]['repair']
    assert proof['parent_review_hash'] == c.parent_review_hash
    assert proof['draft_index'] == 2
    with c.store._db() as db:
        tasks = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
        for original in c.old_rows:
            assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?', (original[0],)).fetchone() == original
    assert tasks.read_checkpoint(proof['subdivision_hash'])['attempt'] == 1
    assert tasks.read_checkpoint(proof['review_hash'])['attempt'] == 1


@pytest.fixture
def completed_partition_repair(semantic_case):
    from test_complete_catalogue_frontier import persist_catalogue_source
    from test_business_semantics import independent_candidate, review as semantic_review
    store, snapshot, raw_run, _, _ = semantic_case
    snapshot = deepcopy(snapshot)
    snapshot['provider'] = {'id': 'fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}
    run, units, routing = persist_catalogue_source(store, snapshot, raw_run, 8)
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user); calls.append(payload)
        stage = payload['stage']
        if stage == 'catalogue':
            result = {'operations': [{'title': '办理业务', 'category': 'flows',
                                      'label_ids': [item['label_id'] for item in payload['items']]}]}
        elif stage == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': ['接收业务', '确认业务', '办理业务', '完成业务'][i//2], 'category': 'flows', 'label_ids': ids[i:i+2]}
                                     for i in range(0, len(ids), 2)]}
        elif stage == 'partition_review':
            result = partition_review(payload, reject='partition_revision' not in payload)
        elif stage == 'proposal':
            result = independent_candidate(payload)
        elif stage == 'review':
            result = semantic_review(payload)
        else:
            raise AssertionError(stage)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}
    service = SimpleNamespace(store=store, providers=SimpleNamespace(complete=complete))
    runtime = s.BusinessSemanticIntegration(service, guard=lambda: None)
    runtime.CANDIDATE_BYTES = 2000
    with pytest.raises(s.SemanticValidationError) as failure:
        runtime.build(snapshot, run, units=units, routing=routing)
    assert failure.value.reason_code == 'review_rejected'
    with store._db() as db:
        rows = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    sub = next(row for row in rows if row['stage'] == 'subdivide')
    old_review = next(row for row in rows if row['stage'] == 'partition_review')
    with store._db() as db:
        old_rows = db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
    paid = len(calls)
    runtime.partition_repair_allowlist = {(sub['stage_hash'], old_review['stage_hash'])}
    projection = runtime.build(snapshot, run, units=units, routing=routing)
    assert projection['complete'] and projection['semantic_coverage']['partitions'][0]['repair']
    with store._db() as db:
        rows = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    s.validate_current_semantic_projection(projection, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], task_rows=rows)
    return SimpleNamespace(store=store, service=service, snapshot=snapshot, run=run, units=units,
        routing=routing, calls=calls, runtime=runtime, parent_subdivision_hash=sub['stage_hash'],
        parent_review_hash=old_review['stage_hash'], old_rows=old_rows, tasks=rows, projection=projection,
        new_calls=deepcopy(calls[paid:]))


def test_source_repair_build_publishes_and_replays_without_paid_calls(completed_partition_repair):
    c = completed_partition_repair
    before = len(c.calls)
    c.runtime.partition_repair_allowlist.clear()
    replay = c.runtime.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert s._projection_hash(replay) == s._projection_hash(c.projection)
    assert len(c.calls) == before


def test_support_consumer_rejects_forged_repair_proof(completed_partition_repair):
    c = completed_partition_repair
    forged = deepcopy(c.projection)
    forged['semantic_coverage']['partitions'][0]['repair']['feedback_hash'] = 'a' * 64
    forged['verification']['projection_hash'] = s._projection_hash(forged)
    with pytest.raises(s.SemanticValidationError):
        s.semantic_support_context(forged, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'],
                                   read_checkpoint=c.tasks.read_checkpoint)


@pytest.mark.parametrize('attack', ['delete_source', 'delete_parent_review', 'feedback', 'aliases', 'proof'])
def test_current_and_support_reject_repair_tampering(completed_partition_repair, attack):
    c = completed_partition_repair
    projection = deepcopy(c.projection)
    repair = projection['semantic_coverage']['partitions'][0]['repair']
    with c.store._db() as db:
        if attack == 'delete_source':
            db.execute('DELETE FROM full_analysis_chunks')
        elif attack == 'delete_parent_review':
            db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?', (c.parent_review_hash,))
        elif attack in {'feedback', 'aliases'}:
            key = c.parent_review_hash if attack == 'feedback' else repair['subdivision_hash']
            data = json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?', (key,)).fetchone()[0])
            if attack == 'feedback':
                data['issues'][0]['code'] = 'missing_input'
            else:
                data['result'][0]['aliases'] = ['伪造别名']
                data['result_hash'] = s._hash(data['result'])
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), key))
        elif attack == 'proof':
            repair['parent_partition_hash'] = '0' * 64
            projection['verification']['projection_hash'] = s._projection_hash(projection)
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    with pytest.raises(s.SemanticValidationError):
        s.validate_current_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], task_rows=rows)
    with pytest.raises(s.SemanticValidationError):
        s.semantic_support_context(projection, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint)


def test_received_review_is_free_accepted_without_selection(partition_repair_case, monkeypatch):
    c = partition_repair_case
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user); c.calls.append(payload)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '修正操作' + str(i), 'category': 'rules', 'label_ids': ids[i:i+2]} for i in range(0, len(ids), 2)]}
        else:
            result = partition_review(payload)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    c.runtime.service.providers.complete = complete
    c.runtime.partition_repair_allowlist = {(c.parent_subdivision_hash, c.parent_review_hash)}
    original = s.BusinessSemanticIntegration._save_task
    with monkeypatch.context() as patch:
        def interrupt(self, key, stage, status, *args, **kwargs):
            original(self, key, stage, status, *args, **kwargs)
            if stage == 'partition_review' and status == 'received':
                raise RuntimeError('stop after received commit')
        patch.setattr(s.BusinessSemanticIntegration, '_save_task', interrupt)
        with pytest.raises(RuntimeError, match='received commit'):
            c.runtime._packets([c.parent])
    paid = len(c.calls)
    c.runtime.partition_repair_allowlist.clear()
    c.runtime._packets([c.parent])
    assert len(c.calls) == paid
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    review_hash = c.runtime._partitions[-1]['review_hash']
    assert rows.read_checkpoint(review_hash)['status'] == 'accepted'


def test_second_review_rejection_blocks_any_third_draft(partition_repair_case):
    c = partition_repair_case
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user); c.calls.append(payload)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '修正操作' + str(i), 'category': 'rules', 'label_ids': ids[i:i+2]} for i in range(0, len(ids), 2)]}
        else:
            result = partition_review(payload, reject=True)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    c.runtime.service.providers.complete = complete
    c.runtime.partition_repair_allowlist = {(c.parent_subdivision_hash, c.parent_review_hash)}
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._packets([c.parent])
    assert error.value.reason_code == 'review_rejected'
    before = len(c.calls)
    with pytest.raises(s.SemanticValidationError):
        c.runtime._packets([c.parent])
    assert len(c.calls) == before == 4


def test_concurrent_resume_has_one_paid_slot_and_matching_commit_log(partition_repair_case, caplog):
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor
    c = partition_repair_case
    entered, release = threading.Event(), threading.Event()
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user); c.calls.append(payload)
        if payload['stage'] == 'subdivide':
            entered.set()
            assert release.wait(10)
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations': [{'title': '修正操作' + str(i), 'category': 'rules', 'label_ids': ids[i:i+2]} for i in range(0, len(ids), 2)]}
        else:
            result = partition_review(payload)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    c.runtime.service.providers.complete = complete
    pair = (c.parent_subdivision_hash, c.parent_review_hash)
    c.runtime.partition_repair_allowlist = {pair}
    peer = copy.copy(c.runtime)
    peer._owned_slots, peer._task_bindings, peer._partitions = set(), {}, []
    peer._counter_lock, peer._stopped = threading.Lock(), threading.Event()
    caplog.set_level('INFO', logger='businessmap.business_semantics')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(c.runtime._packets, [c.parent])
        try:
            assert entered.wait(10)
            second = pool.submit(peer._packets, [c.parent])
            with pytest.raises(s.SemanticValidationError):
                second.result(timeout=10)
        finally:
            release.set()
        assert first.result(timeout=15)
    assert [item['stage'] for item in c.calls].count('subdivide') == 2
    assert [item['stage'] for item in c.calls].count('partition_review') == 2
    repair = c.runtime._partitions[-1]['repair']
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    for stage, key in [('subdivide', repair['subdivision_hash']), ('partition_review', repair['review_hash'])]:
        assert rows.read_checkpoint(key)['status'] == 'accepted'
        accepted = [event for event in caplog.records if getattr(event, 'event', None) == 'semantic_checkpoint_committed'
                    and getattr(event, 'stage_hash', None) == key and getattr(event, 'status', None) == 'accepted']
        assert len(accepted) == 1 and accepted[0].stage == stage and accepted[0].input_hash == c.run['semantic_input_hash']


def test_original_sql_error_code_must_match_saved_rejection(partition_repair_case):
    c = partition_repair_case
    with c.store._db() as db:
        db.execute('UPDATE business_semantic_tasks SET error_code=? WHERE stage_hash=?', ('model_call', c.parent_review_hash))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    m = importlib.import_module('businessmap.semantic_partition_repair')
    with pytest.raises(s.SemanticValidationError):
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash)


def test_unknown_running_catalogue_is_never_excused_by_plan(completed_partition_repair):
    c = completed_partition_repair
    original = next(row for row in c.tasks if row['stage'] == 'catalogue')
    request = deepcopy(original['data']['request']); context = deepcopy(original['data']['context'])
    request['items'][0]['title'] += '其他'
    key = s.semantic_task_identity_hash(c.snapshot, c.run['semantic_input_hash'], 'catalogue', request, context)
    data = {'request_hash': key, 'request': request, 'context': context, 'result': None, 'result_hash': s._hash(None)}
    with c.store._db() as db:
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
            (key, 'catalogue', c.run['semantic_input_hash'], 'running', 1, s._json(data), None, 'fixture'))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    m = importlib.import_module('businessmap.semantic_partition_repair')
    with pytest.raises(s.SemanticValidationError):
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash)
    with pytest.raises(s.SemanticValidationError):
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash, allow_running=(key,))


def test_privacy_rejection_never_qualifies_for_repair(partition_repair_case):
    c = partition_repair_case
    with c.store._db() as db:
        data = json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?', (c.parent_review_hash,)).fetchone()[0])
        data['issues'][0]['code'] = 'privacy'
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), c.parent_review_hash))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    m = importlib.import_module('businessmap.semantic_partition_repair')
    with pytest.raises(s.SemanticValidationError):
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash)


from test_full_analysis import full_project
from test_semantic_integration import REAL_SYNTHESIS_BUILD, REAL_STORED_VALIDATOR


def test_controller_persists_complete_repaired_publication(full_project, monkeypatch):
    from test_business_semantics import independent_candidate, review as semantic_review
    from businessmap.full_synthesis import FullBusinessSynthesis
    full, service, project = full_project
    full.SOURCE_BYTES = 37000
    monkeypatch.setattr(FullBusinessSynthesis, 'build', REAL_SYNTHESIS_BUILD)
    monkeypatch.setattr(s, 'validate_stored_semantic_projection', REAL_STORED_VALIDATOR)
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'CANDIDATE_BYTES', 2000)
    calls = []
    def complete(system, user, **kwargs):
        kwargs['before_send'](); payload = json.loads(user)
        stage = payload.get('stage', 'source' if 'files' in payload else 'routing'); calls.append(stage)
        if stage == 'source':
            evidence = payload['files'][0]['evidence_id']
            result = {'title': '办理业务', 'summary': '', 'findings': [
                {'text': text, 'evidence_ids': [evidence]} for text in
                ['业务办理需要员工权限。', '业务办理需要门店授权。', '业务办理需要完整资料。',
                 '业务办理需要确认条件。', '业务办理需要核对对象。', '业务办理需要确认结果。']], 'unknowns': []}
        elif stage == 'routing':
            result = {'assignments': [{'fact_id': item['fact_id'], 'disposition': 'public', 'category': 'flows', 'title': '办理业务'} for item in payload['items']]}
        elif stage == 'catalogue':
            result = {'operations': [{'title': '办理业务', 'category': 'flows', 'label_ids': [item['label_id'] for item in payload['items']]}]}
        elif stage == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            titles = ['接收业务', '确认业务', '办理业务', '完成业务', '处理资料', '核对资料', '提交资料', '审批资料', '归档资料', '查看资料', '核实条件', '确认对象', '确认门店', '查看结果', '核对结果', '接收结果']
            result = {'operations': [{'title': titles[i//2], 'category': 'flows', 'label_ids': ids[i:i+2]} for i in range(0, len(ids), 2)]}
        elif stage == 'partition_review':
            result = partition_review(payload, reject='partition_revision' not in payload)
        elif stage == 'proposal':
            result = independent_candidate(payload)
        elif stage == 'review':
            result = semantic_review(payload)
        else:
            raise AssertionError(stage)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    monkeypatch.setattr(service.providers, 'complete', complete)
    first = full.analyze(project['id'])
    assert first['status'] == 'failed' and first['synthesis_failure']['reason_code'] == 'review_rejected'
    with service.store._db() as db:
        sub = db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='subdivide'").fetchone()[0]
        rejected = db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE stage='partition_review'").fetchone()[0]
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'partition_repair_allowlist', {(sub, rejected)})
    second = full.analyze(project['id'])
    assert second['status'] == 'completed' and second['publication_complete'] is True
    stored = full._load(project['id'])
    assert stored['status'] == 'completed' and stored['publication_complete'] is True
    assert stored['synthesis']['semantic_coverage']['partitions'][0]['repair']['parent_review_hash'] == rejected
    prior = list(calls)
    monkeypatch.setattr(s.BusinessSemanticIntegration, 'partition_repair_allowlist', frozenset())
    assert full.analyze(project['id'])['publication_complete'] is True
    assert calls == prior


def test_reject_with_provider_failure_diagnostic_is_ineligible(partition_repair_case):
    c = partition_repair_case
    with c.store._db() as db:
        data = json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?', (c.parent_review_hash,)).fetchone()[0])
        data['diagnostic'] = {'status_code': 502, 'codex_reason': 'provider_error'}
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), c.parent_review_hash))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    m = importlib.import_module('businessmap.semantic_partition_repair')
    with pytest.raises(s.SemanticValidationError):
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=c.parent_subdivision_hash, parent_review_hash=c.parent_review_hash)


def test_provider_failure_is_blocked_without_repayment(partition_repair_case):
    c = partition_repair_case
    def complete(system, user, **kwargs):
        kwargs['before_send'](); c.calls.append(json.loads(user))
        raise ToolError('controlled provider failure', 502)
    c.runtime.service.providers.complete = complete
    c.runtime.partition_repair_allowlist = {(c.parent_subdivision_hash, c.parent_review_hash)}
    with pytest.raises(s.SemanticValidationError) as first:
        c.runtime._packets([c.parent])
    assert first.value.reason_code == 'model_call'
    before = len(c.calls)
    with pytest.raises(s.SemanticValidationError):
        c.runtime._packets([c.parent])
    assert len(c.calls) == before == 3
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    repair_rows = [row for row in rows if row['data']['context'].get('partition_revision')]
    assert len(repair_rows) == 1 and repair_rows[0]['status'] == 'needs_review'
    assert repair_rows[0]['data']['failure_code'] == 'model_call'


def test_malformed_saved_revision_fails_with_controlled_error(completed_partition_repair):
    c = completed_partition_repair
    key = c.projection['semantic_coverage']['partitions'][0]['subdivision_hash']
    with c.store._db() as db:
        data = json.loads(db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?', (key,)).fetchone()[0])
        data['context']['partition_revision'] = 'not-an-object'
        db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), key))
    c.runtime.partition_repair_allowlist.clear()
    with pytest.raises(s.SemanticValidationError) as error:
        c.runtime._repair_plans()
    assert error.value.reason_code == 'cache_invalid'


def test_public_plan_does_not_implicitly_exempt_running_proposal(completed_partition_repair):
    c = completed_partition_repair
    proposed = next(row for row in c.tasks if row['stage'] == 'proposal')
    key = proposed['stage_hash']
    with c.store._db() as db:
        for row in c.tasks:
            if row['stage'] == 'review' and row['data']['context']['proposal_hash'] == key:
                db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?', (row['stage_hash'],))
        data = deepcopy(proposed['data']); data['result'] = None; data['result_hash'] = s._hash(None)
        db.execute("UPDATE business_semantic_tasks SET status='running', data=? WHERE stage_hash=?", (s._json(data), key))
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    m = importlib.import_module('businessmap.semantic_partition_repair')
    args = dict(read_checkpoint=rows.read_checkpoint, parent_subdivision_hash=c.parent_subdivision_hash,
                parent_review_hash=c.parent_review_hash)
    with pytest.raises(s.SemanticValidationError) as error:
        m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], **args)
    assert error.value.reason_code == 'interrupted'
    c.runtime._owned_slots.clear()
    with pytest.raises(s.SemanticValidationError) as runtime_error:
        c.runtime._repair_plans()
    assert runtime_error.value.reason_code == 'interrupted'
    plan = m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], **args, allow_running=(key,))
    assert plan['status'] == 'accepted'


@pytest.mark.parametrize('state,expected', [('fresh', 4), ('subdivision_accepted', 3), ('repair_accepted', 2), ('legacy', 4)])
def test_subdivision_preflight_counts_each_oversized_root(partition_repair_case, state, expected):
    c = partition_repair_case
    plans = frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'],
        read_checkpoint=c.tasks.read_checkpoint,
        partition_repair_pairs=((c.parent_subdivision_hash, c.parent_review_hash),))
    other = next(op for op in plans['operations'] if op != c.parent and not c.runtime._packet_fits([op])
                 and len(op['fact_ids']) <= c.runtime._profile()['catalogue_items'])
    pair = (c.parent_subdivision_hash, c.parent_review_hash)
    c.runtime.partition_repair_allowlist = {pair}
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            response = {'operations': [{'title': '修正操作'+str(i), 'category': 'rules', 'label_ids': ids[i:i+2]} for i in range(0, len(ids), 2)]}
        else:
            response = partition_review(payload)
        return {'text': json.dumps(response, ensure_ascii=False), 'usage': {}}
    c.runtime.service.providers.complete = complete
    if state == 'subdivision_accepted':
        with c.store._db() as db:
            rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
        m = importlib.import_module('businessmap.semantic_partition_repair')
        plan = m.partition_repair_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint,
            parent_subdivision_hash=pair[0], parent_review_hash=pair[1])
        labels = [{'title': c.parent['title'], 'category': c.parent['category'], 'fact_ids': [fid], 'aliases': c.parent['aliases']}
                  for fid in c.parent['fact_ids']]
        c.runtime._catalogue_page(labels, stage='subdivide', repair=plan)
    elif state == 'repair_accepted':
        c.runtime._packets([c.parent])
    elif state == 'legacy':
        with c.store._db() as db:
            db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash IN (?,?)', pair)
        c.runtime.partition_repair_allowlist.clear()
    class StopPreflight(Exception):
        pass
    observed = []
    def capture(plan):
        observed.append(plan)
        raise StopPreflight()
    c.runtime.service.semantic_preflight = capture
    with pytest.raises(StopPreflight):
        c.runtime._packets([c.parent, other])
    assert len(observed) == 1 and observed[0]['stage'] == 'subdivision'
    assert observed[0]['remaining_maximum_calls'] == expected
    assert observed[0]['cumulative_maximum_calls'] == observed[0]['occupied_calls'] + expected
