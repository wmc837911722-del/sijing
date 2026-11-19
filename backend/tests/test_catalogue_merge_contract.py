"""Catalogue transport regression; all providers and checkpoints are disposable."""
from collections import Counter
from copy import deepcopy
import pytest
from businessmap import business_semantics as sem


def ideal_catalogue(size):
    runtime = sem.BusinessSemanticIntegration.__new__(sem.BusinessSemanticIntegration)
    # This standalone fixture exercises the retained legacy merge transport.
    runtime._catalogue = runtime._catalogue_legacy
    runtime._snapshot = {'project': {'name': 'synthetic'}, 'provider': {'kind': 'codex-local', 'base_url': 'codex://local', 'model': 'fixture'}}
    runtime._profile = lambda: sem.budget_profile(runtime._snapshot)
    runtime._task_rows = lambda: {}
    runtime._preflight = lambda *args: None
    runtime._catalogue_tasks = []
    runtime.plans = []
    runtime._forbidden = []
    calls = []
    def faithful_call(stage, system, payload, normalize, **kwargs):
        system = sem.semantic_stage_system(runtime._snapshot, stage, payload)
        calls.append((deepcopy(payload), runtime._request_bytes(stage, system, payload)))
        result = normalize({'operations': [{'title': '同一业务操作', 'category': 'data', 'label_ids': [x['label_id'] for x in payload['items']]}]})
        return result, sem._hash([payload, kwargs])
    runtime._call = faithful_call
    routes = [{'title': f'{i:04d}' + '同一业务规则' * 8, 'category': 'data', 'disposition': 'public', 'fact_id': f'f{i}'} for i in range(size)]
    runtime._units = {r['fact_id']: {'kind': 'finding', 'text': '同一业务操作。'} for r in routes}
    return runtime, routes, calls


@pytest.mark.parametrize('size', [200, 600])
def test_ideal_merge_600_aliases_completes_without_truncation(size):
    runtime, routes, calls = ideal_catalogue(size)
    result = runtime._catalogue(routes)
    assert len(result) == 1
    assert Counter(f for op in result for f in op['fact_ids']) == Counter(r['fact_id'] for r in routes)
    assert result[0]['aliases'] == [r['title'] for r in routes]
    assert all(count <= 64000 for _, count in calls)
    merge = [p for p, _ in calls if p['mode'] == 'merge']
    assert merge and all('aliases' not in item for p in merge for item in p['items'])


def test_recorded_scale_lower_bound():
    runtime, _, _ = ideal_catalogue(1)
    aliases = [f'完整业务规则{i:04d}' + '适用条件' * 3 for i in range(8945)]
    label = {'id': 'operation:scale', 'title': '维护业务规则', 'category': 'rules', 'fact_ids': ['f0'], 'aliases': aliases}
    runtime._catalogue_origins = {'operation:scale': {'stage_hash': 'a' * 64, 'result_hash': 'b' * 64, 'operation_id': 'operation:scale'}}
    old, _, _ = runtime._catalogue_payload([label], mode='merge', legacy=True)
    new, _, _ = runtime._catalogue_payload([label], mode='merge')
    assert len(aliases) == len(set(aliases)) == 8945
    # Independent measured actual lower bound is 302476 bytes; this synthetic
    # high-cardinality fixture separately exercises complete unions and wire size.
    assert len(sem._json(old).encode()) >= len(sem._json(aliases).encode()) > 302476 > 64000
    assert runtime._request_bytes('catalogue', '', new) <= 64000
    assert new['items'][0]['alias_count'] == 8945
    assert new['items'][0]['alias_digest'] == sem._hash(aliases)
    assert label['aliases'] == aliases


def test_stage_system_legacy_default_is_unchanged():
    snapshot = {'provider': {'kind': 'codex-local', 'base_url': 'codex://local'}}
    assert sem.semantic_stage_system(snapshot, 'catalogue') == sem.CATALOGUE_SYSTEM
    assert sem.semantic_stage_system(snapshot, 'catalogue', {'mode': 'leaf'}) == sem.CATALOGUE_SYSTEM


@pytest.mark.parametrize('stage,mode,marker', [('catalogue', 'leaf', 'bounded-operation-navigation-v1'), ('subdivide', 'merge', 'bounded-operation-navigation-v1'), ('catalogue', 'merge', 'unknown')])
def test_reject_invalid_contract_marker(stage, mode, marker):
    with pytest.raises(sem.SemanticValidationError):
        sem.semantic_stage_system({}, stage, {'mode': mode, 'merge_contract': marker})


def test_bounded_navigation_binds_private_parent_and_order():
    runtime, routes, calls = ideal_catalogue(2)
    runtime._catalogue(routes)
    labels = [{'id': 'operation:a', 'title': '操作', 'category': 'data', 'fact_ids': ['f0'], 'aliases': ['甲', '乙']}]
    runtime._catalogue_origins = {'operation:a': {'stage_hash': 'a' * 64, 'result_hash': 'b' * 64, 'operation_id': 'operation:a'}}
    request, _, context = runtime._catalogue_payload(labels, mode='merge')
    assert request['items'] == [{'label_id': 'l0', 'title': '操作', 'category': 'data', 'fact_count': 1, 'alias_count': 2, 'alias_digest': sem._hash(['甲', '乙'])}]
    assert context['catalogue_parents']['l0'] == runtime._catalogue_origins['operation:a']
    labels[0]['aliases'].reverse()
    reordered, _, _ = runtime._catalogue_payload(labels, mode='merge')
    assert request['items'][0]['alias_digest'] != reordered['items'][0]['alias_digest']
    assert sem.semantic_stage_system(runtime._snapshot, 'catalogue', request) != sem.CATALOGUE_SYSTEM


def test_leaf_wire_context_and_generator_are_not_versioned_by_merge():
    runtime, routes, _ = ideal_catalogue(2)
    labels = [{'title': '操作', 'category': 'data', 'fact_ids': ['f0'], 'aliases': ['操作']}]
    payload, _, context = runtime._catalogue_payload(labels)
    assert set(payload) == {'stage', 'project', 'items', 'membership_hash', 'mode'}
    assert set(context) == {'label_members', 'catalogue_mode'}
    assert set(payload['items'][0]) == {'label_id', 'title', 'category', 'fact_count', 'member_facts'}
    assert sem.semantic_stage_system(runtime._snapshot, 'catalogue', payload) == sem.CATALOGUE_SYSTEM
