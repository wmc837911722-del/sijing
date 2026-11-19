"""Pure-work regressions at real fixture-owned validation/send boundaries."""
from collections import Counter
from copy import deepcopy
import json
import sys

import pytest
from businessmap import business_semantics as s
from businessmap import _source_material as material
from businessmap import semantic_catalogue_frontier as frontier
from businessmap import semantic_proposal_recovery as recovery
from businessmap import semantic_supersession as sup
from test_source_material import capture, bound_rows, compile_case
from test_proposal_timeout_recovery import source_timeout
from test_semantic_supersession import completed_legacy_catalogue, legacy_case
from test_whole_operation_exact_fit import mixed_source_case, whole_semantic_case


class BoundaryReached(BaseException):
    pass


def observe(call):
    """Observe actual calls/returned shape without replacing stamped functions."""
    counts = Counter()
    def profile(frame, event, result):
        if event == 'call':
            if frame.f_code is s.semantic_task_inventory.__code__: counts['inventory'] += 1
            if frame.f_code is material._compiler_stamp.__code__:
                counts['stamp'] += 1
                if frame.f_back.f_code is material.bind_source_material.__code__:
                    counts['stamp_on_bind'] += 1
                if frame.f_back.f_code is material._template_material.__code__:
                    counts['stamp_on_template'] += 1
            if frame.f_code is s.BusinessSemanticIntegration._packet_fits.__code__:
                if frame.f_back.f_code.co_name == 'whole_operation_plans': counts['whole_root_fits'] += 1
        if event == 'return' and frame.f_code is material._template.__code__:
            name = frame.f_locals.get('name')
            if name == 'catalogue' and isinstance(result, tuple) and type(result[1]) is not bool:
                counts['combined_catalogue_clone'] += 1
            if name == 'packets' and result is not None: counts['all_packets_clone'] += 1
            if name == 'routing' and result is not None:
                caller = frame.f_back
                while caller is not None:
                    if caller.f_code is sup.reconstruct_catalogue_frontier.__code__:
                        counts['legacy_routing_clone'] += 1
                        break
                    caller = caller.f_back
        if event == 'return' and frame.f_code is frontier._catalogue_plan.__code__ and isinstance(result, dict):
            caller = frame.f_back
            while caller is not None:
                if caller.f_code is recovery._packet_binding.__code__:
                    if 'operations' in result: counts['packet_full_forest'] += 1
                    break
                caller = caller.f_back
    previous = sys.getprofile()
    sys.setprofile(profile)
    try: value = call()
    finally: sys.setprofile(previous)
    return value, counts


@pytest.fixture
def prepared_mixed(mixed_source_case):
    from test_business_semantics import partition_review
    c = mixed_source_case
    runtime = c.runtime
    whole = next(root for root in c.roots if root['title'] == '父业务B')
    def historical(system, user, **kwargs):
        kwargs['before_send']()
        payload = json.loads(user)
        if payload['stage'] == 'subdivide':
            ids = [item['label_id'] for item in payload['items']]
            result = {'operations':[{'title':'处理业务'+str(i//10), 'category':'flows',
                                     'label_ids':ids[i:i+10]} for i in range(0,len(ids),10)]}
        else: result = partition_review(payload, reject=True)
        return {'text':json.dumps(result,ensure_ascii=False),'usage':{},'output_limited':False}
    runtime.service.providers.complete = historical
    labels = [{'title':whole['title'],'category':whole['category'],'fact_ids':[fid],
               'aliases':whole['aliases']} for fid in whole['fact_ids']]
    children = runtime._catalogue_page(labels,stage='subdivide')
    subdivision = runtime._catalogue_tasks[-1]
    with pytest.raises(s.SemanticValidationError) as rejected:
        runtime._verify_partition(whole,children,subdivision)
    pair = (subdivision,rejected.value.stage_hash)
    runtime.whole_operation_allowlist = {pair}
    groups = {'atomic_budget_chains':(c.four_keys,), 'whole_histories':(pair,)}
    c.groups = groups
    c.material = material.prepare_source_material(c.snapshot,c.run,read_checkpoint=capture(c).read_checkpoint,**groups)
    runtime._source_material = c.material
    return c


def test_first_before_send_avoids_rebuilding_whole_root_fits_and_unused_inputs(prepared_mixed):
    c = prepared_mixed
    runtime = c.runtime
    packets = runtime._packets(c.roots)
    ordinary = next(packet for packet in packets if any(op['title']=='普通业务' for op in packet))
    before = capture(c)
    before_raw = {row['stage_hash']:before.read_checkpoint.raw_checkpoint_row(row['stage_hash']) for row in before}
    calls = []
    def boundary(system, user, **kwargs):
        result, counts = observe(kwargs['before_send'])
        calls.append((json.loads(user), result, counts))
        raise BoundaryReached()
    runtime.service.providers.complete = boundary
    with pytest.raises(BoundaryReached): runtime._integrate_packet(ordinary)
    assert len(calls) == 1
    request, _, counts = calls[0]
    assert request['stage'] == 'proposal' and request['operations'][0]['title'] == '普通业务'
    assert request['budget']['candidate_bytes'] == 40000
    assert counts['inventory'] >= 3
    assert counts['stamp_on_bind'] and counts['stamp_on_template'], counts
    after = capture(c)
    assert material._same_raw(before.read_checkpoint._source_capture, after.read_checkpoint._source_capture)
    assert all(after.read_checkpoint.raw_checkpoint_row(key)==raw for key,raw in before_raw.items())
    assert counts['whole_root_fits'] == 0, counts
    assert counts['combined_catalogue_clone'] == 0, counts


def test_prepared_legacy_does_not_clone_unused_routing(completed_legacy_catalogue):
    c = completed_legacy_catalogue
    expected = sup.reconstruct_catalogue_frontier(c.snapshot,c.run,read_checkpoint=capture(c).read_checkpoint)
    reader = bound_rows(c,compile_case(c)).read_checkpoint
    actual, counts = observe(lambda: sup.reconstruct_catalogue_frontier(c.snapshot,c.run,read_checkpoint=reader))
    assert actual == expected and s._hash(actual) == s._hash(expected)
    assert counts['stamp'] >= 2
    assert counts['legacy_routing_clone'] == 0, counts


def test_packet_binding_clones_selected_packet_and_terminal_only(source_timeout):
    c = source_timeout
    raw = capture(c)
    parent = next(row for row in raw if row['stage']=='proposal')
    expected = recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=raw.read_checkpoint)
    reader = bound_rows(c,compile_case(c)).read_checkpoint
    actual, counts = observe(lambda: recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=reader))
    assert actual == expected and s._hash(actual) == s._hash(expected)
    assert counts['inventory'] >= 1 and counts['stamp'] >= 1
    assert counts['all_packets_clone'] == 0, counts
    assert counts['packet_full_forest'] == 0, counts


def test_whole_templates_preserve_complete_plans_and_independent_returns(prepared_mixed):
    from businessmap.semantic_whole_operation import whole_operation_plans
    c = prepared_mixed
    kwargs = {'selected_history_pairs':c.groups['whole_histories'],
              'atomic_budget_chains':c.groups['atomic_budget_chains']}
    expected = whole_operation_plans(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=capture(c).read_checkpoint,**kwargs)
    reader = bound_rows(c,c.material).read_checkpoint
    def call(): return whole_operation_plans(c.snapshot,c.run['semantic_input_hash'],read_checkpoint=reader,**kwargs)
    actual, counts = observe(call)
    assert len(actual) == 1 and actual == expected and s._hash(actual) == s._hash(expected)
    assert actual[0]['candidate_bytes'] == 40000
    assert counts['whole_root_fits'] == 0 and counts['inventory'] >= 1
    actual[0]['proposal_request']['items'][0]['text'] = 'caller mutation'
    assert call() == expected


def test_selected_packet_isolation_and_wrong_request_rejection(source_timeout):
    c = source_timeout
    reader = bound_rows(c,compile_case(c)).read_checkpoint
    parent = next(row for row in reader.task_rows(c.run['semantic_input_hash']) if row['stage']=='proposal')
    args = (reader,c.snapshot,c.run,parent['data']['request'],parent['data']['context'])
    packet, deps = material._packet_template(*args,groups=((),(),()))
    expected = deepcopy((packet,deps))
    packet['items'][0]['text'] = 'caller mutation'
    deps.append('caller mutation')
    assert material._packet_template(*args,groups=((),(),())) == expected
    altered = deepcopy(parent['data']['request'])
    altered['items'][0]['text'] += 'changed'
    with pytest.raises(s.SemanticValidationError):
        material._packet_template(reader,c.snapshot,c.run,altered,parent['data']['context'],groups=((),(),()))


def test_split_catalogue_projection_retains_custom_fallback_and_alias_contract():
    calls = []
    class Custom:
        def __deepcopy__(self, memo):
            calls.append(1)
            return {'copy':len(calls)}
    forest = {'operations':[], 'page_hashes':[], 'terminal_candidate':None}
    parts = material._catalogue_template(forest,[{'_fact_id':'one','custom':Custom()}],separate=True)
    assert calls == []
    assert parts['catalogue']() == (forest, False)
    assert parts['catalogue_inputs']() is None
    assert calls == []
    shared = []; shared.append(shared)
    units = [{'_fact_id':'one','left':shared,'right':shared}, {'_fact_id':'two','left':shared}]
    parts = material._catalogue_template(forest,units,separate=True)
    plan, available = parts['catalogue']()
    first = parts['catalogue_inputs']()
    assert available is True and plan == forest
    assert first['one']['left'] is first['one']['right']
    assert first['one']['left'][0] is first['one']['left']
    assert first['one']['left'] is not first['two']['left']
    first['one']['left'].append('changed')
    assert len(parts['catalogue_inputs']()['one']['left']) == 1


def test_terminal_projection_keeps_unsupported_full_plan_copy_hooks():
    calls = []
    class Custom:
        def __deepcopy__(self, memo):
            calls.append('operation')
            return {'copied':len(calls)}
    forest = {'operations':[Custom()], 'page_hashes':[], 'terminal_candidate':None}
    parts = material._catalogue_template(forest,[],separate=True)
    assert calls == []
    terminal, available = parts['catalogue_terminal']()
    assert calls == ['operation']
    assert terminal['operations'] == [{'copied':1}] and available is True


def test_root_fit_template_declines_custom_context(source_timeout):
    from businessmap.semantic_whole_operation import _root_fit_data
    class CustomSnapshot(dict): pass
    c = source_timeout
    assert _root_fit_data(CustomSnapshot(c.snapshot),{},[],[],s.budget_profile(c.snapshot)) is None


def test_bound_packet_enumeration_retains_old_consumer_without_rebuilding(source_timeout):
    c = source_timeout
    rows = capture(c)
    _, _, expected, dependencies = recovery._packet_templates(c.snapshot,c.run,rows.read_checkpoint,
        budget_profile=s.budget_profile(c.snapshot),atomic_budget_chains=(),whole_histories=(),partition_repair_pairs=())
    reader = bound_rows(c,compile_case(c)).read_checkpoint
    rebuilt = []
    def profile(frame,event,arg):
        if event == 'call' and frame.f_code is recovery._packet_templates.__code__: rebuilt.append(True)
    previous = sys.getprofile();sys.setprofile(profile)
    try:
        # Same API and fallback used by the existing private driver.
        value = material._template(reader,'packets',c.snapshot,c.run,groups=((),(),()))
        if value is None:
            _, _, packets, deps = recovery._packet_templates(c.snapshot,c.run,reader,
                budget_profile=s.budget_profile(c.snapshot),atomic_budget_chains=(),whole_histories=(),partition_repair_pairs=())
            value = packets,deps
    finally: sys.setprofile(previous)
    assert value == (expected,dependencies) and s._hash(value) == s._hash((expected,dependencies))
    assert not rebuilt
    packet, deps = material._packet_template(reader,c.snapshot,c.run,expected[0]['payload'],expected[0]['binding'],groups=((),(),()))
    assert (packet,deps) == (expected[0]['payload'],dependencies)
    value[0][0]['payload']['items'][0]['text'] = 'caller mutation'
    assert material._template(reader,'packets',c.snapshot,c.run,groups=((),(),())) == (expected,dependencies)


@pytest.mark.parametrize('custom', (False, True))
def test_aggregate_packet_copy_preserves_cross_entry_aliases_and_custom_hooks(source_timeout, monkeypatch, custom):
    # Inject only at the pure layout/copy boundary, retaining the real factory,
    # Source compilation and bind. This is not an admissible packet fixture.
    original = recovery._packet_templates
    def layout(*args, **kwargs):
        source, forest, packets, deps = original(*args, **kwargs)
        packets.append(packets[0])
        if custom:
            class CopyProbe:
                def __init__(self): self.count = 0
                def __deepcopy__(self, memo):
                    self.count += 1
                    return {'copy':self.count}
            deps.append(CopyProbe())
        return source,forest,packets,deps
    monkeypatch.setattr(recovery,'_packet_templates',layout)
    c = source_timeout
    reader = bound_rows(c,compile_case(c)).read_checkpoint
    first = material._template(reader,'packets',c.snapshot,c.run,groups=((),(),()))
    assert first is not None
    packets,deps = first
    assert packets[0] is packets[-1]
    packets[0]['payload']['items'][0]['text'] = 'caller mutation'
    second = material._template(reader,'packets',c.snapshot,c.run,groups=((),(),()))
    assert second[0][0] is second[0][-1] and second[0][0] is not packets[0]
    assert second[0][0]['payload']['items'][0]['text'] != 'caller mutation'
    if custom:
        assert deps[-1] == {'copy':1} and second[1][-1] == {'copy':2}


def test_compiler_stamp_represents_repeated_immutable_code_once_per_invocation():
    from businessmap import full_analysis
    from types import CodeType
    target = full_analysis._hash.__code__
    expected = sum(type(item) is not CodeType for item in target.co_consts)
    def measured():
        visits = []
        def profile(frame, event, result):
            if event != 'call' or frame.f_code is not material._policy_value.__code__:
                return
            parent = frame.f_back
            if parent.f_code.co_name != '<genexpr>':
                return
            caller = parent.f_back
            if caller and caller.f_code is material._code_value.__code__ and caller.f_locals['code'] is target:
                visits.append(1)
        previous = sys.getprofile()
        sys.setprofile(profile)
        try: stamp = material._compiler_stamp()
        finally: sys.setprofile(previous)
        return stamp, len(visits)
    first, work = measured()
    second, next_work = measured()
    assert first is not None and second == first
    assert work == next_work == expected


@pytest.mark.parametrize('constant', [None, True, 3, 2.5, b'bytes', 'text', Ellipsis,
                                     (1, ('nested',)), frozenset({'a', 'b'})])
def test_stamp_code_memo_preserves_canonical_immutable_representation(constant):
    from types import FunctionType
    template = (lambda: None).__code__
    code = template.replace(co_consts=(constant,))
    function = FunctionType(code, {})
    memo = {}
    expected = material._policy_value(function, set())
    assert material._policy_value(function, set(), memo) == expected
    assert material._policy_value(function, set(), memo) == expected
    assert memo[id(code)][0] is code


@pytest.mark.parametrize('container', [[], {}, set(), ([],)])
def test_stamp_code_memo_never_reuses_mutable_constants(container):
    code = (lambda: None).__code__.replace(co_consts=(container,))
    memo = {}
    before = material._code_value(code, memo)
    assert id(code) not in memo
    mutable = container[0] if type(container) is tuple else container
    if type(mutable) is list: mutable.append('changed')
    elif type(mutable) is dict: mutable['changed'] = True
    else: mutable.add('changed')
    after = material._code_value(code, memo)
    assert before != after == material._code_value(code)
    assert id(code) not in memo


def test_stamp_code_memo_keeps_live_defaults_closures_and_code():
    state = []
    def function(value=[], *, option={}):
        return state, value, option
    memo = {}
    before = material._policy_value(function, set(), memo)
    for change in (lambda: function.__defaults__[0].append(1),
                   lambda: function.__kwdefaults__['option'].update(changed=1),
                   lambda: state.append(1)):
        change()
        after = material._policy_value(function, set(), memo)
        assert before != after == material._policy_value(function, set())
        before = after
    function.__code__ = function.__code__.replace(co_consts=(*function.__code__.co_consts, 'new-code'))
    after = material._policy_value(function, set(), memo)
    assert before != after == material._policy_value(function, set())


def test_stamp_code_memo_custom_constant_preserves_unsupported_behavior():
    class Custom:
        def __repr__(self):
            raise AssertionError('must not execute custom representation')
    code = (lambda: None).__code__.replace(co_consts=(Custom(),))
    for memo in (None, {}):
        with pytest.raises(TypeError, match='unsupported compiler dependency'):
            material._code_value(code, memo)
