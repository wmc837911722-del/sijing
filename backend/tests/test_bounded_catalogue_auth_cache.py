"""Bounded catalogue ancestry authentication against disposable SQLite."""
from collections import Counter
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest
from businessmap import business_semantics as s
from businessmap import semantic_catalogue_frontier as frontier
from test_business_semantics import semantic_case
from test_complete_catalogue_frontier import persist_catalogue_source


@pytest.fixture
def shared_parent_case(semantic_case):
    store, snapshot, run, _, _ = semantic_case
    snapshot = deepcopy(snapshot)
    snapshot['provider'] = {'id': 'fixture', 'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}
    run, units, routing = persist_catalogue_source(store, snapshot, run, 6)
    def complete(system, user, **kwargs):
        kwargs['before_send']()
        request = json.loads(user)
        result = {'operations': [{'title': item['title'], 'category': item['category'], 'label_ids': [item['label_id']]}
                                 for item in request['items']]}
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}}
    runtime = s.BusinessSemanticIntegration(SimpleNamespace(store=store, providers=SimpleNamespace(complete=complete)), guard=lambda: None)
    runtime._snapshot, runtime._run, runtime._input_hash = snapshot, run, run['semantic_input_hash']
    runtime._units = {unit['_fact_id']: unit for unit in units}
    runtime._forbidden, runtime._matching, runtime._partitions = [], [], []
    runtime._catalogue_tasks, runtime.plans, runtime._task_bindings = [], [], {}
    runtime._owned_slots, runtime._stopped = set(), threading.Event()
    runtime._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    labels = runtime._catalogue_initial_labels(routing)
    leaf = runtime._catalogue_page(labels)
    leaf_key = runtime._catalogue_tasks[-1]
    middle = runtime._catalogue_page(leaf, mode='merge')
    middle_key = runtime._catalogue_tasks[-1]
    top = runtime._catalogue_page(middle, mode='merge')
    top_key = runtime._catalogue_tasks[-1]
    assert len(leaf) == len(middle) == len(top) == 6
    return SimpleNamespace(store=store, snapshot=snapshot, run=run, units=units, leaf_key=leaf_key,
                           middle_key=middle_key, top_key=top_key)


def test_shared_parent_is_authenticated_once_per_validation_invocation(shared_parent_case, monkeypatch):
    c = shared_parent_case
    original = s._checkpoint_record
    visits = Counter()
    def counted(key, **kwargs):
        visits[key] += 1
        return original(key, **kwargs)
    monkeypatch.setattr(s, '_checkpoint_record', counted)
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        top = s._checkpoint_record(c.top_key, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=reader)
    assert top['status'] == 'accepted'
    assert visits == {c.top_key: 1, c.middle_key: 1, c.leaf_key: 1}


def _reseal_top(c, mutate):
    """Forge the top task consistently so validation reaches the changed binding."""
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        row = reader(c.top_key)
        data = deepcopy(row['data'])
        mutate(data)
        data['result_hash'] = s._hash(data['result'])
        new_key = s.semantic_task_identity_hash(c.snapshot, c.run['semantic_input_hash'], 'catalogue', data['request'], data['context'])
        data['request_hash'] = new_key
        db.execute('UPDATE business_semantic_tasks SET stage_hash=?,data=? WHERE stage_hash=?', (new_key, s._json(data), c.top_key))
    return new_key


def _check(c, db, key=None, *, ancestry=()):
    return s._checkpoint_record(key or c.top_key, snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'],
                                read_checkpoint=s.semantic_checkpoint_reader(db), _ancestry=ancestry)


def test_shared_history_is_source_authenticated_frontier(shared_parent_case):
    c = shared_parent_case
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    plan = frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'],
        read_checkpoint=rows.read_checkpoint, inputs=c.units)
    assert len(plan['operations']) == 6
    assert set(plan['page_hashes']) == {c.leaf_key, c.middle_key, c.top_key}
    assert set(plan['bindings']) == {operation['id'] for operation in plan['operations']}


@pytest.mark.parametrize('attack', ['result_hash', 'operation_id', 'duplicate_binding', 'facts', 'alias_digest', 'result_aliases'])
def test_cached_parent_does_not_skip_second_binding_or_result_checks(shared_parent_case, attack):
    c = shared_parent_case
    def tamper(data):
        context, request = data['context'], data['request']
        bindings = context['catalogue_parents']
        if attack == 'result_hash':
            bindings['l1']['result_hash'] = '0' * 64
        elif attack == 'operation_id':
            bindings['l1']['operation_id'] = 'operation:' + '0' * 20
        elif attack == 'duplicate_binding':
            bindings['l1']['operation_id'] = bindings['l0']['operation_id']
        elif attack == 'facts':
            context['label_members']['l1'] = ['forged-fact']
            request['membership_hash'] = s._hash(context['label_members'])
        elif attack == 'alias_digest':
            request['items'][1]['alias_digest'] = '0' * 64
        else:
            data['result'][1]['aliases'] = ['伪造别名']
    key = _reseal_top(c, tamper)
    with c.store._db() as db, pytest.raises(s.SemanticValidationError):
        _check(c, db, key)


@pytest.mark.parametrize('attack', ['status', 'input', 'generator', 'result', 'aliases'])
def test_parent_attacks_are_not_hidden_by_local_cache(shared_parent_case, attack):
    c = shared_parent_case
    with c.store._db() as db:
        row = s.semantic_checkpoint_reader(db)(c.middle_key)
        data = deepcopy(row['data'])
        if attack == 'status':
            db.execute("UPDATE business_semantic_tasks SET status='needs_review' WHERE stage_hash=?", (c.middle_key,))
        elif attack == 'input':
            db.execute('UPDATE business_semantic_tasks SET input_hash=? WHERE stage_hash=?', ('0'*64, c.middle_key))
        else:
            if attack == 'generator':
                data['context']['generator_hash'] = '0'*64
            elif attack == 'result':
                data['result'][0]['title'] = '伪造业务'
            else:
                data['result'][0]['aliases'] = ['伪造别名']
            # A rehashed result must still match the authenticated parent binding.
            data['result_hash'] = s._hash(data['result'])
            db.execute('UPDATE business_semantic_tasks SET data=? WHERE stage_hash=?', (s._json(data), c.middle_key))
        with pytest.raises(s.SemanticValidationError):
            _check(c, db)


def test_new_top_level_call_rereads_parent_mutation(shared_parent_case):
    c = shared_parent_case
    with c.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        args = dict(snapshot=c.snapshot, expected_input_hash=c.run['semantic_input_hash'], read_checkpoint=reader)
        assert s._checkpoint_record(c.top_key, **args)['status'] == 'accepted'
        db.execute("UPDATE business_semantic_tasks SET status='needs_review' WHERE stage_hash=?", (c.middle_key,))
        with pytest.raises(s.SemanticValidationError):
            s._checkpoint_record(c.top_key, **args)


def test_active_ancestry_still_rejects_parent_cycle(shared_parent_case):
    c = shared_parent_case
    with c.store._db() as db, pytest.raises(s.SemanticValidationError):
        _check(c, db, ancestry=(c.middle_key,))


def test_missing_source_cannot_be_sealed_despite_cached_parent_authentication(shared_parent_case):
    c = shared_parent_case
    with c.store._db() as db:
        assert _check(c, db)['status'] == 'accepted'
        db.execute('DELETE FROM full_analysis_chunks')
        rows = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])
    with pytest.raises(s.SemanticValidationError):
        frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=rows.read_checkpoint, inputs=c.units)
