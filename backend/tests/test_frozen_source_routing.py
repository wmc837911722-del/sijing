"""Frozen SOURCE routing uses only disposable test databases."""
from businessmap import business_semantics as s
from businessmap import semantic_supersession as sup
from businessmap.full_synthesis import FullBusinessSynthesis
from test_semantic_supersession import legacy_case


def test_three_entries_reuse_one_batching_pass(legacy_case, monkeypatch):
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    import sys
    calls = []
    original = FullBusinessSynthesis._batches.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is original:
            calls.append(1)
    sys.setprofile(profile)
    try:
        results = [sup._source_routing(snapshot, run, reader) for _ in range(3)]
    finally:
        sys.setprofile(None)
    assert results[0] == results[1] == results[2]
    assert len(calls) == 1


def test_return_mutation_and_new_reader_isolation(legacy_case):
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        first = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        second = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    expected = sup._source_routing(snapshot, run, second)
    result = sup._source_routing(snapshot, run, first)
    result[0].clear()
    result[1].clear()
    assert sup._source_routing(snapshot, run, first) == expected
    assert first is not second
    import pytest
    with pytest.raises(AttributeError):
        first.catalogue_source = lambda _: {}
    with pytest.raises(AttributeError):
        first.__dict__


def test_policy_and_callable_changes_recompute(legacy_case, monkeypatch):
    import sys
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    expected = sup._source_routing(snapshot, run, reader)
    calls = []
    code = FullBusinessSynthesis._batches.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    monkeypatch.setattr(FullBusinessSynthesis, 'MAX_OUTPUT', FullBusinessSynthesis.MAX_OUTPUT + 1)
    sys.setprofile(profile)
    try:
        assert sup._source_routing(snapshot, run, reader) == expected
    finally:
        sys.setprofile(None)
    assert len(calls) == 1
    original = FullBusinessSynthesis.navigation
    def alternate(result):
        return original(result)
    monkeypatch.setattr(FullBusinessSynthesis, 'navigation', staticmethod(alternate))
    sys.setprofile(profile)
    try:
        assert sup._source_routing(snapshot, run, reader) == expected
    finally:
        sys.setprofile(None)
    assert len(calls) == 2


def test_input_checked_even_after_cache_hit(legacy_case):
    import pytest
    from copy import deepcopy
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    sup._source_routing(snapshot, run, reader)
    changed = deepcopy(run)
    changed['semantic_input_hash'] = '0' * 64
    with pytest.raises(s.SemanticValidationError):
        sup._source_routing(snapshot, changed, reader)
    changed = deepcopy(snapshot)
    changed['project']['revision'] = 'other'
    with pytest.raises(s.SemanticValidationError):
        sup._source_routing(changed, run, reader)


def test_failed_derivation_is_not_cached(legacy_case, monkeypatch):
    import pytest
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    calls = []
    def fail(self, *args):
        calls.append(1)
        raise ValueError('fixture failure')
    monkeypatch.setattr(FullBusinessSynthesis, '_batches', fail)
    for _ in range(2):
        with pytest.raises(ValueError, match='fixture failure'):
            sup._source_routing(snapshot, run, reader)
    assert len(calls) == 2


def test_mixed_current_and_legacy_l0_are_reused(legacy_case):
    import json
    import sys
    from businessmap.l0_legacy import LegacyL0
    from businessmap.full_synthesis import source_fact_units
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        live = s.semantic_checkpoint_reader(db)
        source = live.catalogue_source(run['run_id'])
        units = source_fact_units(snapshot, run, source['chunks'])
        synth = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
        batches = synth._batches(snapshot, units, 0)
        assert len(batches) > 1
        for batch in batches[::2]:
            current = synth.cache_identity(snapshot, run, batch, 0)
            old = synth.cache_identity(snapshot, run, batch, 0, legacy=True)
            answer = {'assignments': [{'fact_id': f'f{i}', 'disposition': 'public', 'category': 'rules',
                       'title': '门店业务' + chr(0x4e00 + unit['position'])} for i, unit in enumerate(batch)]}
            normalized = LegacyL0._validate(answer, batch)
            db.execute('DELETE FROM full_analysis_synthesis WHERE cache_key=?', (current,))
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (old, old, json.dumps(normalized), 'fixed'))
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        expected = sup._source_routing(snapshot, run, live)
    calls = []
    code = FullBusinessSynthesis._batches.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    sys.setprofile(profile)
    try:
        actual = [sup._source_routing(snapshot, run, reader) for _ in range(3)]
    finally:
        sys.setprofile(None)
    assert actual == [expected] * 3
    assert len(calls) == 1


def test_live_reader_revalidates_changed_database(legacy_case):
    import json
    import pytest
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        sup._source_routing(snapshot, run, reader)
        chunk = json.loads(db.execute('SELECT data FROM full_analysis_chunks').fetchone()[0])
        chunk['result']['findings'][0]['text'] = '内容被修改。'
        db.execute('UPDATE full_analysis_chunks SET data=?', (json.dumps(chunk),))
        with pytest.raises(s.SemanticValidationError):
            sup._source_routing(snapshot, run, reader)


def test_arbitrary_callback_cannot_construct_frozen_reader():
    import pytest
    from businessmap._frozen_routing import FrozenReader
    def arbitrary(_):
        return None
    with pytest.raises(TypeError):
        FrozenReader(arbitrary)


def test_new_frozen_reader_does_not_reuse_tampered_l0(legacy_case):
    import pytest
    from businessmap.full_synthesis import SynthesisValidationError
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        first = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        sup._source_routing(snapshot, run, first)
        db.execute("UPDATE full_analysis_synthesis SET data='{}' WHERE rowid=(SELECT min(rowid) FROM full_analysis_synthesis)")
        second = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        with pytest.raises(SynthesisValidationError):
            sup._source_routing(snapshot, run, second)


# The source and receipts are generated by the existing real planning and
# addendum/attestation fixtures, with all provider responses locally stubbed.
from test_analysis_addenda import prepared_source, addendum_model
from test_full_analysis import full_project
import pytest


@pytest.fixture
def authenticated_source_with_match(prepared_source, monkeypatch):
    import json
    from businessmap.analysis_addenda import AnalysisAddenda
    from businessmap.full_synthesis import source_fact_units, _internal_basis
    full, service, snapshot, run, chunks, request = prepared_source
    addendum_model(monkeypatch, service)
    addenda = AnalysisAddenda(service, guard=lambda: None)
    receipt = addenda.collect(snapshot, run, request)
    attestation = {'operation': '普通创建', 'gap_id': request['gap_id'], 'evidence_ids': request['evidence_ids'],
        'evidence_hashes': {e['evidence_id']: e['content_hash'] for e in receipt['evidence']},
        'conclusion': 'static_request_chain', 'reviewer': 'independent_static_review', 'review_note': '已独立核对请求与接收条件。'}
    match = addenda.attest_matching(snapshot, run, receipt['addendum_id'], attestation)
    accepted = addenda.accepted(snapshot, run)
    assert accepted[0]['matching_receipts'] == [match]
    run = {**run, 'semantic_input_hash': s.semantic_input_hash(snapshot, run, chunks, accepted)}
    units = source_fact_units(snapshot, run, chunks, accepted)
    synth = FullBusinessSynthesis(service)
    s.BusinessSemanticIntegration(service, guard=lambda: None)
    with service.store._db() as db:
        db.execute('UPDATE full_analysis_runs SET data=? WHERE id=?', (json.dumps(run), run['run_id']))
        for batch in synth._batches(snapshot, units, 0):
            answers = []
            for index, unit in enumerate(batch):
                basis = _internal_basis(unit)
                if basis:
                    answers.append({'fact_id': f'f{index}', 'disposition': 'internal',
                                    'classification': sorted(basis)[0], 'reason': '内部技术说明'})
                else:
                    answers.append({'fact_id': f'f{index}', 'disposition': 'public',
                                    'category': 'rules', 'title': '订单业务'})
            result = synth._validate({'assignments': answers}, batch)
            key = synth.cache_identity(snapshot, run, batch, 0)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, json.dumps(result), 'fixed'))
        # Inventory presence makes the production frozen-reader factory capture
        # the real SOURCE bundle; routing itself never reads this placeholder.
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                   ('source-fixture-task', 'catalogue', run['semantic_input_hash'], 'planned', 1, '{}', None, 'fixed'))
    return service, snapshot, run


@pytest.mark.parametrize('target', ['chunk', 'addendum', 'matching'])
def test_authenticated_source_tamper_rejects_new_reader_and_preserves_old(authenticated_source_with_match, target):
    import json
    from businessmap.providers import ToolError
    service, snapshot, run = authenticated_source_with_match
    with service.store._db() as db:
        from businessmap.semantic_proposal_recovery import _without_packet
        old_reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        old = _without_packet(old_reader, run['semantic_input_hash'], 'packet', source_only=True)
        live = s.semantic_checkpoint_reader(db)
        original = sup._source_routing(snapshot, run, live)
        assert sup._source_routing(snapshot, run, old) == original
        table = {'chunk': 'full_analysis_chunks', 'addendum': 'analysis_addenda',
                 'matching': 'analysis_matching_receipts'}[target]
        rowid, raw = db.execute('SELECT rowid,data FROM ' + table + ' LIMIT 1').fetchone()
        data = json.loads(raw)
        if target == 'chunk':
            data['result']['findings'][0]['text'] += '被篡改'
        elif target == 'addendum':
            data['result']['findings'][0]['text'] += '被篡改'
        else:
            data['source_repositories'] = []
        db.execute('UPDATE ' + table + ' SET data=? WHERE rowid=?', (json.dumps(data), rowid))
        new_reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        new = _without_packet(new_reader, run['semantic_input_hash'], 'packet', source_only=True)
        assert sup._source_routing(snapshot, run, old) == original
        with pytest.raises((s.SemanticValidationError, ToolError)):
            sup._source_routing(snapshot, run, new)


from test_semantic_supersession import bounded_joint_case


def test_complete_controller_current_stored_support_and_source_identity(bounded_joint_case):
    case = bounded_joint_case
    service, snapshot, run = case['service'], case['snapshot'], case['run']
    projection, reader = case['projection'], case['records'].read_checkpoint
    assert projection['complete'] is True
    s.validate_current_semantic_projection(projection, snapshot=snapshot,
        expected_input_hash=run['semantic_input_hash'], task_rows=case['records'])
    s.validate_stored_semantic_projection(projection, snapshot=snapshot,
        expected_input_hash=run['semantic_input_hash'], store=service.store)
    assert s.semantic_support_context(projection, snapshot=snapshot,
        expected_input_hash=run['semantic_input_hash'], read_checkpoint=reader) is not None
    with service.store._db() as db:
        live = s.semantic_checkpoint_reader(db)
        assert sup._source_routing(snapshot, run, reader) == sup._source_routing(snapshot, run, live)
        persisted_run, source, addenda = sup._source_inputs(snapshot, run, reader)
        assert s.semantic_input_hash(snapshot, persisted_run, source['chunks'], addenda) == run['semantic_input_hash']


def test_source_routing_is_reused_across_nested_packet_views(legacy_case):
    import sys
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    calls = []
    code = FullBusinessSynthesis._batches.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    sys.setprofile(profile)
    try:
        first = sup._source_routing(snapshot, run, reader)
        view = _without_packet(reader, run['semantic_input_hash'], 'packet', source_only=True)
        second = sup._source_routing(snapshot, run, view)
        nested = _without_packet(view, run['semantic_input_hash'], 'other', source_only=True)
        third = sup._source_routing(snapshot, run, nested)
    finally:
        sys.setprofile(None)
    assert first == second == third
    assert len(calls) == 1


def test_filtered_view_keeps_task_selection_and_source_cache_isolation(legacy_case):
    import json
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    identity = run['semantic_input_hash']
    with service.store._db() as db:
        for key, stage, packet in [('p1', 'proposal', 'one'), ('r1', 'review', 'one'), ('p2', 'proposal', 'two')]:
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                (key, stage, identity, 'planned', 1, json.dumps({'context': {'packet_hash': packet}}), None, 'fixed'))
        reader = s.semantic_checkpoint_rows(db, identity).read_checkpoint
    first = _without_packet(reader, identity, 'one')
    nested = _without_packet(first, identity, 'two')
    assert {'p1', 'r1', 'p2'} <= {row['stage_hash'] for row in reader.task_rows(identity)}
    assert 'p2' in {row['stage_hash'] for row in first.task_rows(identity)}
    assert not {'p1', 'r1'} & {row['stage_hash'] for row in first.task_rows(identity)}
    assert not {'p1', 'r1', 'p2'} & {row['stage_hash'] for row in nested.task_rows(identity)}
    assert first('p1') == reader('p1')
    assert reader._catalogue_frontiers is not first._catalogue_frontiers
    assert first._catalogue_frontiers is not nested._catalogue_frontiers
    expected = sup._source_routing(snapshot, run, reader)
    result = sup._source_routing(snapshot, run, nested)
    result[0].clear()
    result[1].clear()
    assert sup._source_routing(snapshot, run, first) == expected
    assert sup._source_routing(snapshot, run, nested) == expected
    with pytest.raises(AttributeError):
        nested.task_rows = lambda _: []
    with pytest.raises(AttributeError):
        nested.__dict__


def test_nonfactory_packet_views_never_share_routing(legacy_case):
    import sys
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        live = s.semantic_checkpoint_reader(db)
        first = _without_packet(live, run['semantic_input_hash'], 'packet', source_only=True)
        nested = _without_packet(first, run['semantic_input_hash'], 'packet', source_only=True)
        calls = []
        code = FullBusinessSynthesis._batches.__code__
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code is code:
                calls.append(1)
        sys.setprofile(profile)
        try:
            one = sup._source_routing(snapshot, run, first)
            two = sup._source_routing(snapshot, run, nested)
        finally:
            sys.setprofile(None)
        assert one == two
        assert len(calls) == 2


def test_packet_view_revalidates_input_policy_callable_and_reader_identity(legacy_case, monkeypatch):
    import sys
    from copy import deepcopy
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        first = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        second = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    view = _without_packet(first, run['semantic_input_hash'], 'packet', source_only=True)
    other = _without_packet(second, run['semantic_input_hash'], 'packet', source_only=True)
    calls = []
    code = FullBusinessSynthesis._batches.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    sys.setprofile(profile)
    try:
        expected = sup._source_routing(snapshot, run, view)
        assert sup._source_routing(snapshot, run, view) == expected
        assert sup._source_routing(snapshot, run, other) == expected
        assert len(calls) == 2
        changed = deepcopy(run)
        changed['semantic_input_hash'] = '0' * 64
        with pytest.raises(s.SemanticValidationError):
            sup._source_routing(snapshot, changed, view)
        monkeypatch.setattr(FullBusinessSynthesis, 'MAX_OUTPUT', FullBusinessSynthesis.MAX_OUTPUT + 1)
        assert sup._source_routing(snapshot, run, view) == expected
        assert len(calls) == 3
        original = FullBusinessSynthesis.navigation
        def replacement(result):
            return original(result)
        monkeypatch.setattr(FullBusinessSynthesis, 'navigation', staticmethod(replacement))
        assert sup._source_routing(snapshot, run, view) == expected
        assert sup._source_routing(snapshot, run, view) == expected
        assert len(calls) == 5
    finally:
        sys.setprofile(None)


def test_private_routing_copy_preserves_alias_isolation_and_reduces_work():
    import copy
    import sys
    from businessmap import _frozen_routing as memo
    assert hasattr(memo, '_routing_copy_metadata')
    assert hasattr(memo, '_copy_routing')
    tokens = ['token_' + str(i) for i in range(400)]
    units = [{'_forbidden': list(tokens), 'nested': {'v': [i]}} for i in range(80)]
    units[1]['_forbidden'] = units[0]['_forbidden']
    units[1]['cross_reference'] = units[0]['_forbidden']
    value = (units, [{'value': 'route'}])
    counts = []
    def count(frame, event, arg):
        if event == 'call' and frame.f_code is copy.deepcopy.__code__:
            counts.append(1)
    sys.setprofile(count)
    try:
        baseline = copy.deepcopy(value)
    finally:
        sys.setprofile(None)
    baseline_count = len(counts)
    counts.clear()
    metadata = memo._routing_copy_metadata(value)
    sys.setprofile(count)
    try:
        first = memo._copy_routing(value, metadata)
        second = memo._copy_routing(value, metadata)
    finally:
        sys.setprofile(None)
    assert first == baseline == second
    assert len(counts) < baseline_count // 5
    assert first[0][0]['_forbidden'] is first[0][1]['_forbidden'] is first[0][1]['cross_reference']
    assert first[0][0]['_forbidden'] is not units[0]['_forbidden']
    assert first[0][0]['_forbidden'] is not second[0][0]['_forbidden']
    first[0][0]['_forbidden'].append('mutated')
    first[0][0]['nested']['v'].append('mutated')
    assert units[0]['_forbidden'] == tokens
    assert second[0][0]['_forbidden'] == tokens
    assert units[0]['nested']['v'] == [0]


def test_private_routing_copy_falls_back_for_nonexact_types_and_mutables():
    import copy
    from businessmap import _frozen_routing as memo
    assert hasattr(memo, '_routing_copy_metadata')
    class CustomList(list):
        def __deepcopy__(self, memo_arg):
            return CustomList(['custom-copy'])
    class CustomStr(str):
        pass
    class CustomTuple(tuple):
        pass
    safe_tuple = ('a', 'b')
    unsafe = [['mutable']]
    values = ([( {'_forbidden': x}) for x in (safe_tuple, unsafe, CustomList(['x']), [CustomStr('x')], CustomTuple(('x',)))], [])
    expected = copy.deepcopy(values)
    assert memo._routing_copy_metadata(values) == ()
    actual = memo._copy_routing(values, memo._routing_copy_metadata(values))
    assert actual == expected
    assert type(actual[0][0]['_forbidden']) is tuple
    assert type(actual[0][2]['_forbidden']) is CustomList
    assert type(actual[0][4]['_forbidden']) is CustomTuple
    actual[0][1]['_forbidden'][0].append('changed')
    assert unsafe == [['mutable']]


def test_real_source_forbidden_lists_isolated_on_first_and_cached_returns(legacy_case):
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
        live = s.semantic_checkpoint_reader(db)
        expected = sup._source_routing(snapshot, run, live)
    view = _without_packet(reader, run['semantic_input_hash'], 'packet', source_only=True)
    first = sup._source_routing(snapshot, run, view)
    assert first == expected and type(first[0][0]['_forbidden']) is list
    first[0][0]['_forbidden'].append('mutation of first return')
    cached = sup._source_routing(snapshot, run, view)
    assert cached == expected
    assert cached[0][0]['_forbidden'] is not first[0][0]['_forbidden']
    cached[0][0]['_forbidden'].append('mutation of cache hit')
    again = sup._source_routing(snapshot, run, reader)
    assert again == expected
    assert again[0][0]['_forbidden'] is not cached[0][0]['_forbidden']


def test_cache_miss_first_return_and_hit_keep_original_deepcopy_semantics(legacy_case, monkeypatch):
    from businessmap import _frozen_routing as memo
    class Generation:
        def __init__(self, n):
            self.n = n
        def __deepcopy__(self, copy_memo):
            return Generation(self.n + 1)
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    monkeypatch.setattr(sup, '_derive_source_routing', lambda *args: ([{'_forbidden': ['secret'], 'generation': Generation(0)}], []))
    # This synthetic seam ensures the factory's actual cache-miss branch is
    # exercised despite the deliberately replaced derivation callable.
    monkeypatch.setattr(memo, 'dependency_stamp', lambda: 'controlled-test-stamp')
    first = sup._source_routing(snapshot, run, reader)
    second = sup._source_routing(snapshot, run, reader)
    assert first[0][0]['generation'].n == 1
    assert second[0][0]['generation'].n == 2
    assert first[0][0]['generation'] is not second[0][0]['generation']


def test_uncacheable_derivation_still_uses_one_standard_deepcopy(legacy_case, monkeypatch):
    from businessmap import _frozen_routing as memo
    class Generation:
        def __init__(self, n):
            self.n = n
        def __deepcopy__(self, copy_memo):
            return Generation(self.n + 1)
    service, snapshot, run, _ = legacy_case
    with service.store._db() as db:
        reader = s.semantic_checkpoint_rows(db, run['semantic_input_hash']).read_checkpoint
    monkeypatch.setattr(sup, '_derive_source_routing', lambda *args: ([{'generation': Generation(0)}], []))
    monkeypatch.setattr(memo, 'dependency_stamp', lambda: None)
    assert sup._source_routing(snapshot, run, reader)[0][0]['generation'].n == 1
    assert sup._source_routing(snapshot, run, reader)[0][0]['generation'].n == 1


def test_whole_builtin_routing_copy_avoids_recursive_deepcopy():
    import copy
    import sys
    from businessmap import _frozen_routing as memo
    value = ([{'nested': {'items': [{'name': '业务', 'ids': [n, n + 1]} for n in range(30)]}}
              for _ in range(30)], [{'fact_id': 'fact', 'disposition': 'public'}])
    calls = []
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is copy.deepcopy.__code__:
            calls.append(1)
    sys.setprofile(profile)
    try:
        expected = copy.deepcopy(value)
        baseline = len(calls)
        calls.clear()
        metadata = memo._routing_copy_metadata(value)
        actual = memo._copy_routing(value, metadata)
    finally:
        sys.setprofile(None)
    assert actual == expected
    assert len(calls) < baseline // 10


def test_equivalent_filtered_views_reuse_frontier_but_keep_distinct_scopes(legacy_case):
    import json
    import sys
    from businessmap.semantic_proposal_recovery import _without_packet
    service, snapshot, run, _ = legacy_case
    identity = run['semantic_input_hash']
    with service.store._db() as db:
        for key, packet in [('one', 'one'), ('two', 'two')]:
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                (key, 'proposal', identity, 'planned', 1, json.dumps({'context': {'packet_hash': packet}}), None, 'fixed'))
        reader = s.semantic_checkpoint_rows(db, identity).read_checkpoint
    first = _without_packet(reader, identity, 'one')
    calls = []
    code = s.BusinessSemanticIntegration._catalogue_payload.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    sys.setprofile(profile)
    try:
        expected = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=first)
        initial = len(calls)
        assert initial > 0
        equivalent = _without_packet(reader, identity, 'one')
        assert sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=equivalent) == expected
        assert len(calls) == initial
    finally:
        sys.setprofile(None)
    nested = _without_packet(first, identity, 'two')
    all_hidden = _without_packet(reader, identity, 'ignored', source_only=True)
    assert nested is all_hidden
    assert first._catalogue_frontiers is not nested._catalogue_frontiers
    assert _without_packet(reader, 'other-input', 'one') is not first


def test_frozen_factory_getters_avoid_repeated_recursive_copy(legacy_case):
    import copy
    import sys
    service, snapshot, run, _ = legacy_case
    identity = run['semantic_input_hash']
    with service.store._db() as db:
        records = s.semantic_checkpoint_rows(db, identity)
    reader = records.read_checkpoint
    key = records[0]['stage_hash']
    accessors = [lambda: reader(key), lambda: reader.task_rows(identity),
                 lambda: reader.catalogue_source(run['run_id'])]
    for read in accessors:
        expected = read()
        calls = []
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code is copy.deepcopy.__code__:
                calls.append(1)
        sys.setprofile(profile)
        try:
            copy.deepcopy(expected)
            baseline = len(calls)
            calls.clear()
            actual = read()
        finally:
            sys.setprofile(None)
        assert actual == expected
        assert len(calls) < baseline // 10
        actual.clear()
        assert read() == expected
    records.clear()
    assert reader.task_rows(identity)


def test_prepared_builtin_copy_preserves_types_order_aliases_and_cycles():
    from businessmap._frozen_routing import _prepared_copy
    shared = [None, True, False, 2 ** 130, -2 ** 130, b'\x00\xff', '业务\ud800']
    cycle = []
    through_tuple = (cycle,)
    cycle.append(through_tuple)
    value = {'second': shared, 'first': (shared,), ('tuple', 1): cycle}
    value['self'] = value
    clone = _prepared_copy(value)
    first, second = clone(), clone()
    for result in (first, second):
        assert list(result) == ['second', 'first', ('tuple', 1), 'self']
        assert result['self'] is result
        assert type(result['first']) is tuple
        assert result['second'] is result['first'][0]
        assert result[('tuple', 1)][0][0] is result[('tuple', 1)]
        assert result['second'] == shared
        assert [type(x) for x in result['second']] == [type(x) for x in shared]
    first['second'].append('changed')
    assert second['second'] == shared and second['second'] is not shared
    assert first[('tuple', 1)] is not second[('tuple', 1)]


@pytest.mark.parametrize('unsupported', [0.0, -0.0, float('nan'), float('inf'),
    complex(1, 2), set(), frozenset(), bytearray(b'x'), (lambda: None).__code__])
def test_prepared_copy_rejects_entire_unsupported_graph(unsupported, monkeypatch):
    import copy
    from businessmap import _frozen_routing as memo
    value = {'safe': [{'nested': ['business']}], 'unsupported': unsupported}
    calls = []
    def forbidden_encode(*args):
        raise AssertionError('unsupported graph must not reach serialization')
    original = copy.deepcopy
    def ordinary(item, *args):
        calls.append(item)
        return original(item, *args)
    monkeypatch.setattr(memo.marshal, 'dumps', forbidden_encode)
    monkeypatch.setattr(memo, 'deepcopy', ordinary)
    clone = memo._prepared_copy(value)
    assert not calls
    first, second = clone(), clone()
    assert len(calls) == 2 and all(item is value for item in calls)
    assert type(first['unsupported']) is type(unsupported)
    assert first['safe'] is not second['safe'] and first['safe'] is not value['safe']
    if type(unsupported) is float:
        assert first['unsupported'] is unsupported


def test_prepared_copy_preserves_custom_side_effects_without_eager_copy():
    from businessmap._frozen_routing import _prepared_copy
    calls = []
    class CustomDict(dict):
        def __deepcopy__(self, memo):
            calls.append(1)
            return {'generation': len(calls)}
    value = {'safe': ['unchanged'], 'custom': CustomDict()}
    clone = _prepared_copy(value)
    assert calls == []
    assert clone()['custom'] == {'generation': 1}
    assert clone()['custom'] == {'generation': 2}
    assert value['custom'] == {}


@pytest.mark.parametrize('error', [TypeError, ValueError, RecursionError])
def test_prepared_copy_encoding_failure_uses_standard_deepcopy(error, monkeypatch):
    from businessmap import _frozen_routing as memo
    value = {'nested': [[1], (2,)]}
    def fail(*args):
        raise error('fixture encoding limit')
    monkeypatch.setattr(memo.marshal, 'dumps', fail)
    clone = memo._prepared_copy(value)
    first, second = clone(), clone()
    assert first == second == value
    first['nested'][0].append(3)
    assert second == value


def test_prepared_copy_real_depth_limit_falls_back():
    import copy
    from businessmap._frozen_routing import _routing_copy_metadata, _prepared_copy
    value = []
    for _ in range(2100):
        value = [value]
    assert _routing_copy_metadata(value) == ()
    with pytest.raises(RecursionError):
        copy.deepcopy(value)
    with pytest.raises(RecursionError):
        _prepared_copy(value)()


from test_semantic_supersession import completed_legacy_catalogue


def test_legacy_receipt_timeout_plan_and_postchild_inventory_reuse(completed_legacy_catalogue, tmp_path):
    import copy
    import sys
    from types import SimpleNamespace
    from businessmap import semantic_proposal_recovery as recovery
    from test_proposal_timeout_recovery import proposal_grant
    c = completed_legacy_catalogue
    identity = c.run['semantic_input_hash']
    with c.service.store._db() as db:
        before = s.semantic_checkpoint_rows(db, identity)
        parent = next(row for row in before if row['stage'] == 'proposal')
        data = copy.deepcopy(parent['data'])
        data.update(result=None, result_hash=s._hash(None), failure_code='model_call',
                    diagnostic={'status_code': 408, 'codex_reason': 'timeout'})
        db.execute("UPDATE business_semantic_tasks SET status='needs_review',error_code='model_call',data=? WHERE stage_hash=?",
                   (s._json(data), parent['stage_hash']))
        for row in before:
            if row['stage'] == 'review' and row['data']['context']['proposal_hash'] == parent['stage_hash']:
                db.execute('DELETE FROM business_semantic_tasks WHERE stage_hash=?', (row['stage_hash'],))
        frozen = s.semantic_checkpoint_rows(db, identity).read_checkpoint
        live = s.semantic_checkpoint_reader(db)
        expected = recovery.proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=live)
        actual = recovery.proposal_timeout_recovery_plan(c.snapshot, c.run, parent['stage_hash'], read_checkpoint=frozen)
        assert actual == expected and s._hash(actual) == s._hash(expected)
        protected = db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()
    cfg = SimpleNamespace(store=c.service.store, snapshot=c.snapshot, run=c.run, parent=parent)
    plan, grant = proposal_grant(cfg, tmp_path)
    assert plan == expected
    recovery.authorize_proposal_timeout_recovery(c.service, c.snapshot, c.run, plan, authorization=grant)
    with c.service.store._db() as db:
        assert db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash != ? ORDER BY stage_hash',
                          (plan['child_stage_hash'],)).fetchall() == protected
        records = s.semantic_checkpoint_rows(db, identity)
        other = s.semantic_checkpoint_rows(db, identity)
    calls = []
    code = s.BusinessSemanticIntegration._catalogue_payload.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(1)
    sys.setprofile(profile)
    try:
        first = s.semantic_task_inventory(records, snapshot=c.snapshot, expected_input_hash=identity)
        initial = len(calls)
        calls.clear()
        second = s.semantic_task_inventory(records, snapshot=c.snapshot, expected_input_hash=identity)
        repeated = len(calls)
        assert first == second and s._hash(first) == s._hash(second)
        assert repeated < initial // 2
        calls.clear()
        assert s.semantic_task_inventory(other, snapshot=c.snapshot, expected_input_hash=identity) == first
        assert len(calls) > repeated * 2
    finally:
        sys.setprofile(None)
    reader = records.read_checkpoint
    for name in ('task_rows', 'recovery_receipts', 'supersession_receipts'):
        accessor = getattr(reader, name)
        expected_rows = accessor(identity)
        assert expected_rows
        changed = accessor(identity)
        changed[0].clear()
        assert accessor(identity) == expected_rows
        assert accessor('other-input') == []
    old_source = reader.catalogue_source(c.run['run_id'])
    with c.service.store._db() as db:
        import json
        chunk = json.loads(db.execute('SELECT data FROM full_analysis_chunks LIMIT 1').fetchone()[0])
        chunk['result']['findings'][0]['text'] = '门店允许无维护权限的人操作。'
        db.execute('UPDATE full_analysis_chunks SET data=?', (s._json(chunk),))
        fresh = s.semantic_checkpoint_rows(db, identity).read_checkpoint
    assert reader.catalogue_source(c.run['run_id']) == old_source
    assert fresh.catalogue_source(c.run['run_id']) != old_source
    with pytest.raises(s.SemanticValidationError):
        sup._source_routing(c.snapshot, c.run, fresh)


def test_factory_custom_source_fallback_and_cross_getter_isolation(legacy_case, monkeypatch):
    service, snapshot, run, _ = legacy_case
    original_factory = s.semantic_checkpoint_reader
    calls = []
    class Generation:
        def __init__(self, n):
            self.n = n
        def __deepcopy__(self, memo):
            calls.append(self.n)
            return Generation(self.n + 1)
    def reader_with_custom_source(db):
        reader = original_factory(db)
        original_source = reader.catalogue_source
        def source(run_id):
            value = original_source(run_id)
            value['custom_fixture'] = Generation(0)
            return value
        reader.catalogue_source = source
        return reader
    monkeypatch.setattr(s, 'semantic_checkpoint_reader', reader_with_custom_source)
    with service.store._db() as db:
        records = s.semantic_checkpoint_rows(db, run['semantic_input_hash'])
    assert calls == []
    reader = records.read_checkpoint
    assert reader.catalogue_source(run['run_id'])['custom_fixture'].n == 1
    assert reader.catalogue_source(run['run_id'])['custom_fixture'].n == 1
    assert calls == [0, 0]
    all_rows = reader.task_rows(run['semantic_input_hash'])
    first = reader(all_rows[0]['stage_hash'])
    first['data']['request'].clear()
    assert all_rows[0]['data']['request']
    assert reader(all_rows[0]['stage_hash'])['data']['request'] == all_rows[0]['data']['request']
    all_rows[0]['data']['request'].clear()
    assert records[0]['data']['request']


from test_proposal_timeout_recovery import source_timeout
from businessmap.providers import ToolError


@pytest.fixture
def percent_source_reader(source_timeout):
    import json
    c = source_timeout
    with c.store._db() as db:
        key, raw = db.execute('SELECT cache_key,data FROM full_analysis_synthesis').fetchone()
        value = json.loads(raw)
        for assignment in value['assignments']:
            assignment['title'] = '办理业务%41'
        db.execute('UPDATE full_analysis_synthesis SET data=? WHERE cache_key=?', (s._json(value), key))
        reader = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    return c, reader


@pytest.mark.parametrize('change', ('implementation', 'generator', 'lookup', 'custom_lookup'))
def test_unbound_warm_route_rejects_changed_url_decoding(percent_source_reader, monkeypatch, change):
    import urllib.parse as url
    from businessmap._frozen_routing import dependency_stamp
    c, warm = percent_source_reader
    assert sup._source_routing(c.snapshot, c.run, warm)[1][0]['title'] == '办理业务%41'
    before = dependency_stamp()
    if change == 'implementation': monkeypatch.setattr(url, '_unquote_impl', lambda value: b'`')
    elif change == 'generator': monkeypatch.setattr(url, '_generate_unquoted_parts', lambda *args: iter(('`',)))
    elif change == 'lookup': monkeypatch.setattr(url, '_hextobyte', {**url._hextobyte, b'41': b'`'})
    else:
        class Lookup(dict):
            def __getitem__(self, key): return b'`'
        monkeypatch.setattr(url, '_hextobyte', Lookup(url._hextobyte))
    with c.store._db() as db:
        cold = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    with pytest.raises(ToolError, match="导航缓存"): sup._source_routing(c.snapshot, c.run, cold)
    with pytest.raises(ToolError, match="导航缓存"): sup._source_routing(c.snapshot, c.run, warm)
    assert dependency_stamp() != before


@pytest.mark.parametrize('change', ('missing', 'extra', 'hex', 'ascii'))
def test_unbound_malformed_url_policy_recomputes_like_fresh_reader(percent_source_reader, monkeypatch, change):
    import urllib.parse as url
    import re
    import sys
    from businessmap._frozen_routing import dependency_stamp
    c, warm = percent_source_reader
    sup._source_routing(c.snapshot, c.run, warm)
    before = dependency_stamp()
    table = dict(url._hextobyte)
    if change == 'missing':
        table.pop(b'41')
        monkeypatch.setattr(url, '_hextobyte', table)
    elif change == 'extra':
        monkeypatch.setattr(url, '_hextobyte', {**table, b'GG': b'X'})
    elif change == 'hex': monkeypatch.setattr(url, '_hexdig', '0')
    else: monkeypatch.setattr(url, '_asciire', re.compile('(never-matches)'))
    with c.store._db() as db:
        cold = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    if change in ('missing', 'ascii'):
        with pytest.raises(ToolError) as failure:
            sup._source_routing(c.snapshot, c.run, cold)
        assert failure.value.reason_code == 'cache_invalid'
    else:
        expected = sup._source_routing(c.snapshot, c.run, cold)
    calls = []
    code = sup._derive_source_routing.__code__
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code is code: calls.append(1)
    sys.setprofile(profile)
    try:
        if change in ('missing', 'ascii'):
            with pytest.raises(ToolError) as failure:
                sup._source_routing(c.snapshot, c.run, warm)
            assert failure.value.reason_code == 'cache_invalid'
        else:
            assert sup._source_routing(c.snapshot, c.run, warm) == expected
    finally: sys.setprofile(None)
    assert calls == [1]
    assert dependency_stamp() != before


def test_unbound_url_cold_hot_and_concurrent_readers_keep_memo(percent_source_reader, monkeypatch):
    import urllib.parse as url
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap._frozen_routing import dependency_stamp
    c, first = percent_source_reader
    with c.store._db() as db:
        second = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    monkeypatch.setattr(url, '_hextobyte', None)
    before = dependency_stamp()
    assert before is not None
    barrier = Barrier(2)
    code = sup._derive_source_routing.__code__
    def read(reader):
        calls = []
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code is code: calls.append(1)
        barrier.wait()
        sys.setprofile(profile)
        try:
            results = [sup._source_routing(c.snapshot, c.run, reader) for _ in range(3)]
        finally: sys.setprofile(None)
        assert results[0] == results[1] == results[2]
        assert results[0][1][0]['title'] == '办理业务%41'
        assert calls == [1]
        return results[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(read, (first, second)))
    assert left == right
    assert dependency_stamp() == before
