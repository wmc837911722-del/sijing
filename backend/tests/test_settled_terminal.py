"""Repeat Source validation on every new frozen capture and every custom input."""
from copy import deepcopy
import sys

import pytest

from businessmap import semantic_catalogue_frontier as frontier
from businessmap import semantic_proposal_recovery as recovery
from test_source_material import bound_rows, compile_case, source_timeout


@pytest.mark.parametrize('custom_snapshot', (False, True))
def test_repeated_packet_binding_preserves_capture_and_result_isolation(source_timeout, custom_snapshot):
    case = source_timeout
    material = compile_case(case)
    first = bound_rows(case, material).read_checkpoint
    fresh = bound_rows(case, material).read_checkpoint
    groups = dict(atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=())
    snapshot = dict(case.snapshot)
    if custom_snapshot:
        class CustomPolicy(dict):
            pass
        snapshot['policies'] = CustomPolicy(snapshot['policies'])

    calls = []
    code = frontier._packet_frontier.__code__
    monitor = sys.monitoring
    monitor.use_tool_id(3, 'settled-terminal-test')
    previous = monitor.register_callback(3, monitor.events.PY_START, lambda code, offset: calls.append(None))
    try:
        monitor.set_local_events(3, code, monitor.events.PY_START)
        before = recovery._packet_binding(snapshot, case.run, case.parent, first, **groups)
        reference = deepcopy(before)
        before[1]['terminal_candidate'].clear()
        repeat = recovery._packet_binding(snapshot, case.run, case.parent, first, **groups)
        after = recovery._packet_binding(snapshot, case.run, case.parent, fresh, **groups)
        assert reference == repeat == after
        assert len(calls) == (3 if custom_snapshot else 2)
    finally:
        monitor.set_local_events(3, code, 0)
        monitor.register_callback(3, monitor.events.PY_START, previous)
        monitor.free_tool_id(3)


@pytest.mark.parametrize('unsettled', ('recovery', 'supersession', 'proposal', 'd2', 'custom_capture', 'received'))
def test_nonterminal_or_custom_view_revalidates_each_time(monkeypatch, unsettled):
    from businessmap import _frozen_routing, _source_material

    class CapturedView:
        _FrozenReader__material = object()
        _FrozenReader__hidden = frozenset({'old'})
        _FrozenReader__packet_terminal = None
        _FrozenReader__plain_capture = unsettled != 'custom_capture'

        def recovery_receipts(self, identity):
            return [{'consumed_at': None}] if unsettled == 'recovery' else []

        def supersession_receipts(self, identity):
            return [{'consumed_at': None}] if unsettled == 'supersession' else []

        def task_rows(self, identity):
            return [{'stage': 'proposal' if unsettled == 'proposal' else 'catalogue'}]

        def d2_retry_rows(self, identity):
            return [{'stage': 'proposal'}] if unsettled == 'd2' else []

    calls = []
    def validate(*args, **kwargs):
        calls.append(None)
        return {'received_to_accept': ['waiting'] if unsettled == 'received' else [],
                'terminal_candidate': {'verification': len(calls)}}

    monkeypatch.setattr(_frozen_routing, 'FrozenReader', CapturedView)
    monkeypatch.setattr(_source_material, '_template_material', lambda *args, **kwargs: object())
    monkeypatch.setattr(frontier, '_packet_frontier', validate)
    view = CapturedView()
    for attempt in (1, 2):
        result = recovery._settled_source_frontier({}, {'semantic_input_hash': 'input'}, view, ((), (), ()))
        assert result['terminal_candidate']['verification'] == attempt
