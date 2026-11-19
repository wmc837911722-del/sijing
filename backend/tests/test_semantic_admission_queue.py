"""Local admission scheduling through real calls and disposable SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from businessmap import business_semantics as sem
from businessmap.providers import ToolError
from test_business_semantics import semantic_case
from test_claimed_admission_incomplete import authorized_review
from test_proposal_timeout_recovery import source_timeout
from test_review_timeout_recovery import review_timeout


def runtime(case, service=None, *, identity='a', guard=lambda: None):
    store, snapshot, run, units, _ = case
    snapshot = deepcopy(snapshot)
    snapshot['provider'].update(kind='codex-local', base_url='codex://local')
    service = service or SimpleNamespace(store=store, providers=SimpleNamespace())
    result = sem.BusinessSemanticIntegration(service, guard=guard)
    result._snapshot, result._run = snapshot, deepcopy(run)
    result._input_hash = sem._hash(identity)
    result._units = {unit['_fact_id']: deepcopy(unit) for unit in units}
    result._forbidden, result._matching = [], []
    result._stopped = threading.Event()
    result._usage = {'requests': 0, 'cache_hits': 0, 'input_tokens': 0, 'output_tokens': 0}
    result._task_bindings, result._owned_slots, result.plans = {}, set(), []
    return result


def request():
    return {'stage': 'catalogue', 'project': '门店运营', 'mode': 'leaf',
            'items': [], 'membership_hash': sem._hash({})}


def invoke(current):
    return current._call('catalogue', sem.CATALOGUE_SYSTEM, request(), lambda value: value,
                         context={'catalogue_mode': 'leaf', 'label_members': {}})


def response():
    return {'text': '[]', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'output_limited': False}


def test_shared_service_serializes_admission_but_four_models_overlap(semantic_case):
    """Without the permit, four first guards overlap; holding it through return deadlocks models."""
    store = semantic_case[0]
    service = SimpleNamespace(store=store, providers=SimpleNamespace())
    lock, start, models = threading.Lock(), threading.Barrier(5), threading.Barrier(4)
    release, first, overlap = threading.Event(), threading.Event(), threading.Event()
    counts = {'admission': 0, 'peak': 0, 'models': 0, 'model_peak': 0}
    runtimes = [runtime(semantic_case, service, identity=str(index)) for index in range(4)]
    for current in runtimes:
        original = current._raise_cached_failure
        def check(original=original, calls=[0]):
            calls[0] += 1
            if calls[0] == 1:
                with lock:
                    counts['admission'] += 1
                    counts['peak'] = max(counts['peak'], counts['admission'])
                    if counts['admission'] > 1:
                        overlap.set()
                first.set()
                assert release.wait(3), 'test did not release local preparation'
            original()
            if calls[0] == 2:
                with lock:
                    counts['admission'] -= 1
        current._raise_cached_failure = check
    def complete(*args, **kwargs):
        kwargs['before_send']()
        with lock:
            counts['models'] += 1
            counts['model_peak'] = max(counts['model_peak'], counts['models'])
        try:
            models.wait(3)
            return response()
        finally:
            with lock:
                counts['models'] -= 1
    service.providers.complete = complete
    def run(current):
        start.wait(3)
        return invoke(current)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, current) for current in runtimes]
        start.wait(3)
        assert first.wait(3)
        overlap.wait(.15)
        release.set()
        values = [future.result(5) for future in futures]
    assert [value[0] for value in values] == [[], [], [], []]
    assert counts['peak'] == 1
    assert counts['model_peak'] == 4
    with store._db() as db:
        rows = db.execute('SELECT status FROM business_semantic_tasks').fetchall()
    assert rows == [('accepted',)] * 4
    assert [current._usage['input_tokens'] for current in runtimes] == [7] * 4


def raw_tasks(store):
    with store._db() as db:
        return db.execute('SELECT * FROM business_semantic_tasks ORDER BY stage_hash').fetchall()


@pytest.mark.parametrize('stage', ['admission', 'preflight'])
def test_inventory_check_uses_one_sql_snapshot_and_sees_next_commit(semantic_case, monkeypatch, stage):
    current = runtime(semantic_case)
    capture = current._read_tasks
    counts = []

    def capture_during_peer_write():
        rows = capture()
        if not counts:
            # A peer commits just after the inventory snapshot has been frozen.
            context = {'generator_hash': sem._semantic_generator_hash(current._snapshot),
                       'budget_profile': current._profile()}
            with current.store._db() as peer:
                peer.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                             ('f' * 64, 'proposal', current._input_hash, 'accepted', 1,
                              json.dumps({'context': context}), None, 'fixture'))
        counts.append(len(rows))
        return rows

    monkeypatch.setattr(current, '_read_tasks', capture_during_peer_write)
    check = current._raise_cached_failure if stage == 'admission' else lambda: current._preflight('proposal', 1)
    check()
    assert counts == [0]
    with pytest.raises(sem.SemanticValidationError) as error:
        check()
    assert error.value.reason_code == 'cache_invalid'
    assert counts == [0, 1]


def test_first_send_recaptures_reserved_row_before_reading_its_owner(semantic_case, monkeypatch):
    current = runtime(semantic_case)
    capture = current._read_tasks
    send_snapshots = []

    def complete(*args, before_send, **kwargs):
        key = before_send._semantic_owned_snapshot()['stage_hash']
        current._owned_slots.remove(key)

        def capture_then_publish_owner():
            rows = capture()
            send_snapshots.append([(row['stage_hash'], row['status']) for row in rows])
            current._owned_slots.add(key)
            return rows

        monkeypatch.setattr(current, '_read_tasks', capture_then_publish_owner)
        before_send()
        assert send_snapshots == [[(key, 'running')]]
        return response()

    current.service.providers.complete = complete
    assert invoke(current)[0] == []


def held_call(case):
    entered, release = threading.Event(), threading.Event()
    current = runtime(case, identity='holder')
    def complete(*args, **kwargs):
        entered.set()
        assert release.wait(3), 'holder was not released'
        kwargs['before_send']()
        return response()
    current.service.providers.complete = complete
    return current, entered, release


@pytest.mark.parametrize('change', ['stop', 'pause', 'cancel', 'permission', 'destination'])
def test_waiter_observes_controls_without_claim_or_receipt_change(semantic_case, change, caplog):
    holder, entered, release = held_call(semantic_case)
    waiting, changed = threading.Event(), threading.Event()
    def guard():
        waiting.set()
        if changed.is_set():
            raise ToolError('fixture ' + change, 409)
    current = runtime(semantic_case, holder.service, identity='waiter', guard=guard)
    with caplog.at_level('INFO'), ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(invoke, holder)
        assert entered.wait(3)
        before = raw_tasks(current.store)
        queued = pool.submit(invoke, current)
        assert waiting.wait(3)
        if change == 'stop':
            current._stopped.set()
        else:
            changed.set()
        try:
            with pytest.raises(ToolError):
                queued.result(2)
            assert raw_tasks(current.store) == before
            assert not any(getattr(record, 'event', None) == 'semantic_slot_reserved'
                           and getattr(record, 'input_hash', None) == current._input_hash for record in caplog.records)
        finally:
            release.set()
        assert running.result(3)[0] == []


def test_queue_wait_preserves_complete_stage_execution_budget(semantic_case, monkeypatch):
    holder, entered, release = held_call(semantic_case)
    waiting = threading.Event()
    current = runtime(semantic_case, holder.service, identity='waiter', guard=waiting.set)
    clock, windows = [1000.], []
    monkeypatch.setattr(sem, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original = holder.service.providers.complete
    def complete(*args, **kwargs):
        windows.append((clock[0], kwargs['_codex_execution_timeout']))
        return original(*args, **kwargs)
    holder.service.providers.complete = complete
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, holder)
        assert entered.wait(3)
        second = pool.submit(invoke, current)
        assert waiting.wait(3)
        assert len(raw_tasks(current.store)) == 1
        clock[0] = 9000.
        release.set()
        assert first.result(3)[0] == second.result(3)[0] == []
    # Catalogue retains 180, while the structured review/proposal policy remains 360.
    assert windows == [(1000., 180), (9000., 180)]
    assert sem.budget_profile(current._snapshot)['stages']['proposal']['timeout_seconds'] == 360


def test_different_services_prepare_independently(semantic_case):
    prepared = threading.Barrier(2)
    values = [runtime(semantic_case, identity=str(index)) for index in range(2)]
    def complete(*args, **kwargs):
        prepared.wait(3)
        kwargs['before_send']()
        return response()
    for current in values:
        current.service.providers.complete = complete
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(invoke, current) for current in values]
        assert [future.result(4)[0] for future in results] == [[], []]


def test_foreign_concurrent_and_repeated_callbacks_release_once(semantic_case):
    current = runtime(semantic_case)
    original, calls = current._raise_cached_failure, []
    def check():
        calls.append(threading.get_ident())
        return original()
    current._raise_cached_failure = check
    caller, callback_threads = threading.get_ident(), []
    start = threading.Barrier(2)
    def complete(*args, **kwargs):
        def callback():
            callback_threads.append(threading.get_ident())
            start.wait(3)
            kwargs['before_send']()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(callback) for _ in range(2)]
            for future in futures:
                future.result(3)
        for _ in range(3):
            kwargs['before_send']()
        return response()
    current.service.providers.complete = complete
    assert invoke(current)[0] == []
    assert len(calls) == 2 and all(value != caller for value in callback_threads)
    current.service.providers.complete = lambda *args, **kwargs: (kwargs['before_send'](), response())[1]
    assert invoke(runtime(semantic_case, current.service, identity='next'))[0] == []


class ProviderAbort(BaseException):
    pass


@pytest.mark.parametrize('admitted', [False, True])
def test_base_exception_releases_after_original_failure_state(semantic_case, admitted):
    current = runtime(semantic_case)
    def fail(*args, **kwargs):
        if admitted:
            kwargs['before_send']()
        raise ProviderAbort('fixture abort')
    current.service.providers.complete = fail
    with pytest.raises(ProviderAbort):
        invoke(current)
    row = raw_tasks(current.store)[0]
    assert row[3] == ('needs_review' if admitted else 'planned')
    assert row[6] == ('model_call' if admitted else None)
    current.service.providers.complete = lambda *args, **kwargs: (kwargs['before_send'](), response())[1]
    assert invoke(runtime(semantic_case, current.service, identity='next'))[0] == []


def test_first_callback_failure_keeps_original_unsent_state_and_releases(semantic_case):
    current = runtime(semantic_case)
    original, checks = current._raise_cached_failure, []
    def check():
        checks.append(True)
        if len(checks) == 2:
            raise ToolError('fixture callback revoked', 409)
        return original()
    current._raise_cached_failure = check
    current.service.providers.complete = lambda *args, **kwargs: (kwargs['before_send'](), response())[1]
    with pytest.raises(ToolError, match='fixture callback revoked'):
        invoke(current)
    assert raw_tasks(current.store)[0][3] == 'planned'
    assert invoke(runtime(semantic_case, current.service, identity='next'))[0] == []


@pytest.mark.parametrize('failure_at', ['final_check_before_spawn', 'spawn_attempt', 'control_after_spawn'])
def test_native_launch_boundary_preserves_control_and_checkpoint(semantic_case, monkeypatch, failure_at):
    from businessmap import codex_provider as codex
    from businessmap.providers import ProviderService

    store = semantic_case[0]
    providers = ProviderService(store)
    monkeypatch.setattr(codex, 'status', lambda: {'status': 'ready', 'version': 'fixture'})
    monkeypatch.setattr(codex, 'find_codex', lambda: 'fake-codex')
    monkeypatch.setattr(codex, '_text_only_catalog', lambda *args: '{}')
    selected = providers.save({'name': 'fixture', 'kind': 'codex-local', 'model': 'fixture'})
    current = runtime(semantic_case, SimpleNamespace(store=store, providers=providers))
    current._snapshot['provider'] = selected
    launches = []
    def spawn(*args, stdout, **kwargs):
        launches.append(True)
        if failure_at == 'spawn_attempt':
            raise OSError('synthetic process creation error')
        for event in ({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"answer":{}}'}},
                      {'type': 'turn.completed', 'usage': {}}):
            stdout.write((json.dumps(event) + '\n').encode())
        stdout.flush()
        return SimpleNamespace(poll=lambda: 0, returncode=0)
    monkeypatch.setattr(codex.subprocess, 'Popen', spawn)
    monkeypatch.setattr(codex, '_stop', lambda process: None)

    refused = ToolError('fixture live control revoked', 409)
    admissions = []
    native = providers.complete
    def call(system, user, *, before_send, **kwargs):
        def checked():
            before_send()
            admissions.append(True)
            if failure_at == 'final_check_before_spawn' or failure_at == 'control_after_spawn' and launches:
                raise refused
        return native(system, user, before_send=checked, **kwargs)
    monkeypatch.setattr(providers, 'complete', call)

    with pytest.raises(ToolError) as error:
        invoke(current)
    row = raw_tasks(store)[0]
    if failure_at == 'final_check_before_spawn':
        assert admissions == [True] and not launches
        assert error.value is refused and (row[3], row[6]) == ('planned', None)
    else:
        assert launches == [True] and (row[3], row[6]) == ('needs_review', 'model_call')
        if failure_at == 'control_after_spawn':
            assert len(admissions) >= 2 and error.value is refused
        else:
            assert error.value.reason_code == 'model_call'


def test_cache_and_input_limit_returns_do_not_retain_permit(semantic_case):
    current = runtime(semantic_case)
    current.service.providers.complete = lambda *args, **kwargs: (kwargs['before_send'](), response())[1]
    assert invoke(current)[0] == []
    before = raw_tasks(current.store)
    assert invoke(current)[0] == [] and current._usage['cache_hits'] == 1
    assert raw_tasks(current.store) == before
    limited = runtime(semantic_case, current.service, identity='limited')
    limited.WIRE_BYTES = 1
    with pytest.raises(sem.SemanticValidationError) as error:
        invoke(limited)
    assert error.value.reason_code == 'input_limit'
    assert invoke(runtime(semantic_case, current.service, identity='next'))[0] == []


def test_returning_peer_persists_usage_and_result_during_other_admission(semantic_case, monkeypatch, caplog):
    from businessmap import codex_provider
    from businessmap.providers import ProviderService
    store = semantic_case[0]
    monkeypatch.setattr(codex_provider, 'status', lambda: {'status': 'ready', 'version': 'fixture'})
    providers = ProviderService(store)
    selected = providers.save({'name': 'fixture', 'kind': 'codex-local', 'model': 'fixture'})
    service = SimpleNamespace(store=store, providers=providers)
    paid = runtime(semantic_case, service, identity='paid')
    preparing = runtime(semantic_case, service, identity='preparing')
    for current in (paid, preparing):
        current._snapshot['provider'] = selected
    received, blocked, release = threading.Event(), threading.Event(), threading.Event()
    def complete(messages, model, *, before_send, **kwargs):
        before_send()
        received.set()
        assert blocked.wait(3)
        return response()
    monkeypatch.setattr(codex_provider, 'complete', complete)
    original = preparing._raise_cached_failure
    def check():
        blocked.set()
        assert release.wait(3)
        return original()
    preparing._raise_cached_failure = check
    with caplog.at_level('INFO'), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, paid)
        assert received.wait(3)
        second = pool.submit(invoke, preparing)
        try:
            assert first.result(2)[0] == []
            with store._db() as db:
                row = db.execute('SELECT stage_hash,status FROM business_semantic_tasks WHERE input_hash=?', (paid._input_hash,)).fetchone()
            assert row[1] == 'accepted'
            assert providers.usage()['requests'] == 1
            assert providers.usage()['input_tokens'] == 7
            events = [record for record in caplog.records if getattr(record, 'event', None) == 'semantic_checkpoint_committed'
                      and getattr(record, 'stage', None) == 'catalogue' and getattr(record, 'status', None) == 'accepted'
                      and getattr(record, 'input_hash', None) == paid._input_hash]
            assert len(events) == 1
        finally:
            release.set()
        assert second.result(3)[0] == []


def test_waiter_rechecks_current_corrupt_inventory_after_queue(semantic_case):
    holder, entered, release = held_call(semantic_case)
    waiting = threading.Event()
    current = runtime(semantic_case, holder.service, identity='waiter', guard=waiting.set)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, holder)
        assert entered.wait(3)
        second = pool.submit(invoke, current)
        assert waiting.wait(3)
        with current.store._db() as db:
            db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                       ('f' * 64, 'proposal', current._input_hash, 'accepted', 1,
                        json.dumps({'context': {'generator_hash': sem._semantic_generator_hash(current._snapshot),
                                                'budget_profile': current._profile()}}), None, 'fixture'))
        release.set()
        assert first.result(3)[0] == []
        with pytest.raises(sem.SemanticValidationError) as error:
            second.result(3)
        assert error.value.reason_code == 'cache_invalid'
    assert len(raw_tasks(current.store)) == 2


def test_provider_abort_waits_for_in_progress_foreign_callback(semantic_case):
    """A returning/raising provider cannot release admission during a live first callback."""
    current = runtime(semantic_case)
    entered, release, aborted = threading.Event(), threading.Event(), threading.Event()
    original, checks, callbacks = current._raise_cached_failure, [], []
    def check():
        checks.append(True)
        if len(checks) == 2:
            entered.set()
            assert release.wait(3)
        return original()
    current._raise_cached_failure = check
    with ThreadPoolExecutor(max_workers=2) as pool:
        def complete(*args, **kwargs):
            callbacks.append(pool.submit(kwargs['before_send']))
            assert entered.wait(3)
            aborted.set()
            raise ProviderAbort('fixture concurrent abort')
        current.service.providers.complete = complete
        operation = pool.submit(invoke, current)
        try:
            assert aborted.wait(3)
            with pytest.raises(TimeoutError):
                operation.result(.1)
        finally:
            release.set()
        with pytest.raises(ProviderAbort):
            operation.result(3)
        callbacks[0].result(3)
    assert raw_tasks(current.store)[0][3] == 'needs_review'


@pytest.mark.parametrize('foreign', [False, True])
def test_unrelated_or_caller_objects_cannot_replace_service_gate(semantic_case, foreign):
    current = runtime(semantic_case)
    if foreign:
        other = runtime(semantic_case, identity='other')
        current.service._semantic_admission_gate = sem._semantic_admission_gate(other)
    else:
        current.service._semantic_admission_gate = SimpleNamespace(acquire=lambda _: None)
    with pytest.raises(sem.SemanticValidationError) as error:
        invoke(current)
    assert error.value.reason_code == 'cache_invalid' and raw_tasks(current.store) == []


def test_consumed_recovery_finalizes_before_next_waiter_prepares(authorized_review):
    from businessmap.semantic_recovery import held_project_lock
    case, plan = authorized_review
    finalizing, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original = case.runtime._record_admission_incomplete
    receipts = []
    def finalize(*args):
        with case.store._db() as db:
            receipts.append(db.execute('SELECT * FROM business_semantic_recoveries WHERE child_stage_hash=?',
                                       (plan['child_stage_hash'],)).fetchone())
        finalizing.set()
        assert release.wait(3)
        return original(*args)
    case.runtime._record_admission_incomplete = finalize
    def complete(system, user, **kwargs):
        if json.loads(user)['stage'] == 'review':
            raise RuntimeError('fixture incomplete admission')
        kwargs['before_send']()
        return response()
    case.runtime.service.providers.complete = complete
    queued = runtime((case.store, case.snapshot, case.run, [], []), case.runtime.service,
                     identity='queued', guard=waiting.set)
    check = queued._raise_cached_failure
    def verify_finalized():
        with case.store._db() as db:
            row = db.execute('SELECT status,error_code FROM business_semantic_tasks WHERE stage_hash=?',
                             (plan['child_stage_hash'],)).fetchone()
            receipt = db.execute('SELECT * FROM business_semantic_recoveries WHERE child_stage_hash=?',
                                 (plan['child_stage_hash'],)).fetchone()
        assert row == ('needs_review', 'admission_incomplete') and receipt == receipts[0]
        return check()
    queued._raise_cached_failure = verify_finalized
    def integrate():
        with held_project_lock(case.snapshot['project']['id']):
            return case.runtime._integrate_packet(case.packet)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(integrate)
        assert finalizing.wait(5)
        second = pool.submit(invoke, queued)
        assert waiting.wait(3)
        release.set()
        with pytest.raises(RuntimeError, match='fixture incomplete admission'):
            first.result(5)
        assert second.result(5)[0] == []


@pytest.mark.parametrize('provider_raises', [False, True])
def test_callback_entering_after_provider_exit_cannot_reenter_admission(semantic_case, provider_raises):
    previous = runtime(semantic_case, identity='previous')
    callbacks = []
    def exited(*args, **kwargs):
        callbacks.append(kwargs['before_send'])
        if provider_raises:
            raise RuntimeError('fixture provider exited')
        return response()
    previous.service.providers.complete = exited
    if provider_raises:
        with pytest.raises(RuntimeError, match='fixture provider exited'):
            invoke(previous)
    else:
        assert invoke(previous)[0] == []
    current = runtime(semantic_case, previous.service, identity='next')
    preparing, release = threading.Event(), threading.Event()
    original = current._raise_cached_failure
    def blocked():
        preparing.set()
        assert release.wait(3)
        return original()
    current._raise_cached_failure = blocked
    current.service.providers.complete = lambda *args, **kwargs: (kwargs['before_send'](), response())[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(invoke, current)
        try:
            assert preparing.wait(3)
            before = raw_tasks(current.store)
            late = pool.submit(callbacks[0])
            with pytest.raises(sem.SemanticValidationError) as error:
                late.result(2)
            assert error.value.reason_code == 'needs_review'
            assert raw_tasks(current.store) == before
        finally:
            release.set()
        assert running.result(3)[0] == []


@pytest.mark.parametrize('changed', ['code', 'defaults', 'kwdefaults', 'closure'])
def test_legacy_mutation_stamp_tracks_admitted_body_without_function_replacement(monkeypatch, changed):
    from businessmap._prepared_mutation import _legacy_compiler_stamp
    function = sem.BusinessSemanticIntegration._call_admitted
    state = []
    if changed == 'closure':
        from types import FunctionType
        def template():
            return state
        function = FunctionType(template.__code__, function.__globals__, closure=template.__closure__)
        monkeypatch.setattr(sem.BusinessSemanticIntegration, '_call_admitted', function)
    identity = id(function)
    before = _legacy_compiler_stamp()
    assert before is not None
    if changed == 'code':
        monkeypatch.setattr(function, '__code__', function.__code__.replace(co_consts=(*function.__code__.co_consts, 'changed body')))
    elif changed == 'defaults':
        monkeypatch.setattr(function, '__defaults__', ('changed default',))
    elif changed == 'kwdefaults':
        monkeypatch.setattr(function, '__kwdefaults__', {'context': 'changed keyword default'})
    else:
        state.append('changed closure')
    assert id(sem.BusinessSemanticIntegration._call_admitted) == identity
    assert _legacy_compiler_stamp() != before
