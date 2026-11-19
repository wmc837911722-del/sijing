"""An ordinary review recovery after a completed two-member recovery and D2 retry."""
import hashlib
import json
import logging
import time
from types import SimpleNamespace

import pytest

from businessmap import business_semantics as s
from businessmap import semantic_d2_retry as d2
from businessmap import semantic_review_recovery as v1
from businessmap.providers import ToolError
from businessmap.semantic_recovery import held_project_lock
from review_exception_fixture import two_review_history
from test_business_semantics import independent_candidate, review
from test_review_exception_bundle import authorize_fixture


def _evidenced_grant(directory, name, evidence, grant):
    evidence_path = directory / (name + '-evidence.json')
    evidence_path.write_text(json.dumps(evidence), encoding='utf-8')
    evidence_path.chmod(0o600)
    grant_path = directory / (name + '-grant.json')
    if grant['policy'] in (v1.POLICY, v1.REAUTH_POLICY):
        grant = {**grant, 'authorization_path': str(grant_path.resolve())}
    grant_path.write_text(json.dumps({
        **grant,
        'reconciliation_ref': str(evidence_path),
        'reconciliation_hash': hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }), encoding='utf-8')
    grant_path.chmod(0o600)
    return grant_path


def _pause(c, paused):
    c.store.config_set('full_analysis_control:' + c.run['run_id'], {'paused': paused, 'cancelled': False})


def _read(c):
    with c.store._db() as db:
        return s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])


def _ledger(c):
    with c.store._db() as db:
        return tuple({
            row[0]: row for row in db.execute('SELECT * FROM ' + table)
        } for table in ('business_semantic_tasks', 'business_semantic_recoveries', d2.TABLE))


def _complete_bundle(c, directory, dispatches):
    bundle, _, _ = authorize_fixture(c, directory)
    _pause(c, False)
    with held_project_lock(c.snapshot['project']['id']):
        accepted = c.runtime._integrate_packet(c.packets[0])
        with pytest.raises(s.SemanticValidationError):
            c.runtime._integrate_packet(c.packets[1])
    assert [call[1] for call in dispatches] == ['review', 'review', 'proposal']
    _pause(c, True)
    rows = _read(c)
    failed, = [row for row in rows if row['attempt'] == 2]
    case = {
        'run_id': c.run['run_id'], 'input_hash': c.run['semantic_input_hash'],
        'bundle_id': bundle['bundle_id'], 'parent_stage_hash': failed['stage_hash'],
        'accepted_review_hash': accepted['review_hash'],
        'rejected_review_hash': failed['data']['context']['revision']['parent_failure_hash'],
        'candidate_sha256': '7' * 64,
    }
    plan = d2.d2_retry_plan(c.snapshot, c.run, read_checkpoint=rows.read_checkpoint, expected_case=case)
    prepared = d2.prepare_d2_retry(c.service, c.snapshot, c.run, plan)
    now = int(time.time())
    grant = _evidenced_grant(directory, 'historical-d2', {
        'case': case, 'plan_hash': plan['plan_hash'], 'checked_at': now, 'old_owner_drained': True,
        'native_starts_after_two_d1': 0, 'duplicate_fee_risk_accepted': True,
    }, {
        'policy': d2.POLICY, 'case': case, 'plan_hash': plan['plan_hash'], 'operator_id': 'root',
        'authorization_source': 'user-confirmed-d2-once', 'authorized_at': now, 'expires_at': now + 3600,
        'proposal_limit': 1, 'normal_review_limit': 1,
    })
    cap = d2.load_d2_retry_authorization(c.service, grant, expected_case=case)
    c.service._review_exception_authorizations.clear()
    with held_project_lock(c.snapshot['project']['id']):
        d2.authorize_d2_retry(c.service, c.snapshot, c.run, plan, authorization=cap, prepared=prepared)
        _pause(c, False)
        revised = c.runtime._integrate_packet(c.packets[1])
    assert [call[1] for call in dispatches] == ['review', 'review', 'proposal', 'proposal', 'review']
    grant.unlink()
    return bundle, {accepted['review_hash'], revised['review_hash']}


@pytest.fixture
def review_after_bundle(two_review_history, tmp_path):
    c = two_review_history
    expected = [
        ('review', 'accept'), ('review', 'reject'), ('proposal', 'local_failure'),
        ('proposal', 'propose'), ('review', 'accept'),
        ('proposal', 'propose'), ('review', 'timeout'),
        ('review', 'reject'), ('proposal', 'propose'), ('review', 'accept'),
    ]
    dispatches = []

    def complete(system, text, **kwargs):
        kwargs['before_send']()
        payload = json.loads(text)
        stage, outcome = expected[len(dispatches)]
        assert payload['stage'] == stage
        physical = kwargs['before_send']._semantic_owned_snapshot()['stage_hash']
        dispatches.append((physical, stage, text))
        if outcome == 'local_failure':
            raise ToolError('synthetic D2 submission failure', 422)
        if outcome == 'timeout':
            error = ToolError('synthetic ordinary review timeout', 408)
            error.codex_reason = 'timeout'
            raise error
        result = independent_candidate(payload) if stage == 'proposal' else review(payload, reject=outcome == 'reject')
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'output_limited': False}

    c.service.providers.complete = complete
    bundle, accepted = _complete_bundle(c, tmp_path, dispatches)
    with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError) as failure:
        c.runtime._integrate_packet(c.packets[2])
    assert failure.value.reason_code == 'model_call'
    parent = _read(c).read_checkpoint(dispatches[-1][0])
    assert parent['data']['diagnostic'] == {'status_code': 408, 'codex_reason': 'timeout'}
    assert parent['data']['result'] is None
    _pause(c, True)
    return c, bundle, accepted, parent, dispatches


def _ordinary_grant(tmp_path, c, plan, now):
    key = plan['parent_stage_hash']
    return _evidenced_grant(tmp_path, 'ordinary-v1', {
        'checked_at': now, 'local_owner_pids': [99999999], 'local_processes_drained': True,
        'upstream_result_or_billing_reconciled': False, 'run_id': c.run['run_id'],
        'parent_stage_hash': key, 'parent_row_digest': plan['parent_failure_digest'], 'inventory_digest': plan['inventory_digest'],
    }, {
        'policy': v1.POLICY, 'plan_hash': plan['plan_hash'], 'parent_stage_hash': key, 'run_id': c.run['run_id'],
        'operator_id': 'root', 'authorization_source': 'user-fix-and-continue', 'authorized_at': now,
        'expires_at': now + 3600, 'max_uses': 1,
    })


@pytest.mark.parametrize('two_review_history', [True], indirect=True)
def test_ordinary_review_recovers_once_and_completes_projection_after_archived_d2(review_after_bundle, tmp_path, caplog, monkeypatch):
    c, bundle, accepted, parent, dispatches = review_after_bundle
    identity = c.run['semantic_input_hash']
    key = parent['stage_hash']
    before = _ledger(c)
    assert len(before[2]) == 1
    archive, = before[2].values()
    assert archive[3] is not None and archive[4]
    assert [call[1] for call in dispatches[-2:]] == ['proposal', 'review']

    # Evaluate both kinds of drift on full live SQLite readers without disturbing the valid fixture.
    for statement, params in (
        ("UPDATE full_analysis_chunks SET data=replace(data,'核对','变更')", ()),
        ("UPDATE business_semantic_tasks SET data=' '||data WHERE stage_hash=?", (c.targets[0]['stage_hash'],)),
    ):
        with c.store._db() as db:
            db.execute('BEGIN')
            try:
                assert db.execute(statement, params).rowcount > 0
                rows = s.semantic_checkpoint_rows(db, identity)
                with pytest.raises(s.SemanticValidationError):
                    v1.review_timeout_recovery_plan(c.snapshot, c.run, key, read_checkpoint=rows.read_checkpoint)
            finally:
                db.rollback()
    plan = v1.review_timeout_recovery_plan(c.snapshot, c.run, key, read_checkpoint=c.runtime._read_tasks().read_checkpoint)
    assert plan['prior_bundle_id'] == bundle['bundle_id']
    assert (plan['logical_draft_maximum'], plan['physical_maximum_calls'], plan['replacement_allowance']) == (2, 5, 1)
    assert plan['subject_proposal_hash'] == parent['data']['context']['proposal_hash']
    assert _ledger(c) == before and len(dispatches) == 7

    material = c.runtime._source_material
    prepared = v1._prepare_authorization(c.service, c.snapshot, c.run, plan, source_material=material)
    with pytest.raises(s.SemanticValidationError):
        v1.authorize_review_timeout_recovery(
            c.service, c.snapshot, c.run, plan, authorization=tmp_path / 'missing-v1-grant',
            source_material=material, prepared_mutation=prepared,
        )
    assert _ledger(c) == before and len(dispatches) == 7
    now = int(time.time())
    grant = _ordinary_grant(tmp_path, c, plan, now)
    caplog.set_level(logging.INFO)
    receipt = v1.authorize_review_timeout_recovery(
        c.service, c.snapshot, c.run, plan, authorization=grant, source_material=material, prepared_mutation=prepared,
    )
    child = plan['child_stage_hash']
    assert receipt['consumed_at'] is None
    registered = _ledger(c)
    assert registered[0].keys() - before[0].keys() == {child}
    assert registered[1].keys() - before[1].keys() == {receipt['recovery_id']}
    assert registered[0][child][3] == 'planned' and len(dispatches) == 7
    with c.store._db() as db:
        db.execute('BEGIN')
        try:
            stored = registered[1][receipt['recovery_id']]
            forged = {**json.loads(stored[4]), 'prior_bundle_id': '0' * 64}
            db.execute('UPDATE business_semantic_recoveries SET data=? WHERE recovery_id=?',
                       (s._json(forged), receipt['recovery_id']))
            current = s.semantic_checkpoint_rows(db, identity).read_checkpoint
            with pytest.raises(s.SemanticValidationError):
                v1.authenticate_review_timeout_recovery(current(child), snapshot=c.snapshot,
                                                       expected_input_hash=identity, read_checkpoint=current)
        finally:
            db.rollback()
    assert _ledger(c) == registered
    reader = _read(c).read_checkpoint
    with pytest.raises(s.SemanticValidationError):
        v1.review_timeout_recovery_plan(c.snapshot, c.run, key, read_checkpoint=reader)
    # A newly opened service has the durable records and the original live authority only.
    fresh_service = SimpleNamespace(store=c.store, providers=c.service.providers, _exclusive=c.service._exclusive,
                                    _controls=c.service._controls, project_analysis=c.service.project_analysis)
    active = s.BusinessSemanticIntegration(fresh_service, guard=c.runtime.guard)
    _pause(c, False)
    evidence = tmp_path / 'ordinary-v1-evidence.json'
    def assert_no_consumption():
        blocked = s.BusinessSemanticIntegration(fresh_service, guard=c.runtime.guard)
        with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError):
            blocked.build(c.snapshot, c.run, units=c.units, routing=c.routing)
        assert _ledger(c) == registered and len(dispatches) == 7

    grant.rename(grant.with_suffix('.revoked'))
    try:
        assert_no_consumption()
    finally:
        grant.with_suffix('.revoked').rename(grant)
    with monkeypatch.context() as clock:
        clock.setattr(time, 'time', lambda: now + 3601)
        assert_no_consumption()
    with held_project_lock(c.snapshot['project']['id']):
        projection = active.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert projection['complete'] and set(projection['verification']['accepted_review_hashes']) == accepted | {dispatches[-1][0]}
    assert [call[1] for call in dispatches[7:]] == ['review', 'proposal', 'review']
    assert dispatches[7] == (child, 'review', dispatches[6][2])
    new_dispatches = {call[0] for call in dispatches[7:]}
    assert len(new_dispatches) == 3 and new_dispatches.isdisjoint(before[0])
    assert (active._usage['requests'], active._usage['input_tokens'], active._usage['output_tokens']) == (3, 21, 9)
    final_rows = _read(c)
    assert final_rows.read_checkpoint(child)['data']['failure_code'] == 'review_rejected'
    final = _ledger(c)
    for old_table, new_table in zip(before, final):
        assert all(new_table[k] == row for k, row in old_table.items())
    assert final[2] == before[2] and final[1].keys() == registered[1].keys()
    saved = final[1][receipt['recovery_id']]
    assert type(saved[5]) is int and len(saved[6]) == 64
    counts = s.semantic_task_inventory(final_rows, snapshot=c.snapshot, expected_input_hash=identity)
    assert counts['occupied_calls'] == sum(row['status'] != 'planned' for row in final_rows) + 1
    assert counts['occupied_calls'] == sum(row[3] != 'planned' for row in before[0].values()) + len(before[2]) + 3
    for event in ('authorized', 'consumed'):
        records = [r for r in caplog.records if getattr(r, 'event', '') == 'semantic_recovery_' + event
                   and getattr(r, 'recovery_id', None) == receipt['recovery_id']]
        assert len(records) == 1

    grant.unlink()
    evidence.unlink()
    replay = s.BusinessSemanticIntegration(fresh_service, guard=c.runtime.guard)
    with held_project_lock(c.snapshot['project']['id']):
        replayed = replay.build(c.snapshot, c.run, units=c.units, routing=c.routing)
    assert s._projection_hash(replayed) == s._projection_hash(projection)
    s.validate_stored_semantic_projection(projection, snapshot=c.snapshot, expected_input_hash=identity, store=c.store)
    assert s.semantic_support_context(projection, snapshot=c.snapshot, expected_input_hash=identity,
                                      read_checkpoint=final_rows.read_checkpoint)
    assert len(dispatches) == 10 and replay._usage['requests'] == 0
    assert _ledger(c) == final


@pytest.mark.parametrize('two_review_history', [True], indirect=True)
@pytest.mark.parametrize('outcome', ['renewed-timeout', 'revoked-before-launch', 'concurrent-claims'])
def test_post_bundle_only_one_review_can_be_claimed(review_after_bundle, tmp_path, monkeypatch, outcome):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap._prepared_mutation import _capture_dynamic
    from businessmap._codex_dispatch import native_attempt
    from businessmap.semantic_recovery import consume_recovery

    c, _, _, parent, original_calls = review_after_bundle
    key, identity = parent['stage_hash'], c.run['semantic_input_hash']
    plan = v1.review_timeout_recovery_plan(c.snapshot, c.run, key, read_checkpoint=c.runtime._read_tasks().read_checkpoint)
    child = plan['child_stage_hash']
    material = c.runtime._source_material
    prepared = v1._prepare_authorization(c.service, c.snapshot, c.run, plan, source_material=material)
    stamp = int(time.time())
    grant = _ordinary_grant(tmp_path, c, plan, stamp)
    # Another location holding identical grant bytes is not this authority.
    alternate = tmp_path / 'copy.json'
    alternate.write_bytes(grant.read_bytes())
    alternate.chmod(0o600)
    with pytest.raises(s.SemanticValidationError):
        v1.authorize_review_timeout_recovery(c.service, c.snapshot, c.run, plan, authorization=alternate,
                                            source_material=material, prepared_mutation=prepared)
    receipt = v1.authorize_review_timeout_recovery(c.service, c.snapshot, c.run, plan, authorization=grant,
                                                  source_material=material, prepared_mutation=prepared)
    before = _ledger(c)
    assert before[0][child][3] == 'planned'
    service = SimpleNamespace(store=c.store, providers=SimpleNamespace(), _exclusive=c.service._exclusive,
                              _controls=c.service._controls, project_analysis=c.service.project_analysis)
    if outcome == 'renewed-timeout':
        now = stamp + 3601
        monkeypatch.setattr(time, 'time', lambda: now)
        with c.store._db() as db:
            db.execute('BEGIN')
            records = s.semantic_checkpoint_rows(db, identity)
            capture = _capture_dynamic(db, identity)
            original = v1._selected_receipt_row(db, child)
        renewal = v1.review_authority_reauthorization_plan(c.snapshot, c.run, child,
            read_checkpoint=records.read_checkpoint, original_row=original, dynamic_capture=capture)
        prepared = v1.prepare_review_authority_reauthorization(service, c.snapshot, c.run, renewal, source_material=material)
        bound = {k: renewal[k] for k in ('run_id','parent_stage_hash','child_stage_hash','original_row_digest',
                                        'child_raw_digest','current_inventory_digest','plan_hash')}
        renewed_grant = _evidenced_grant(tmp_path, 'permitted-renewal', {
            **bound, 'checked_at': now, 'local_owner_pids': [99999999], 'local_processes_drained': True,
            'upstream_result_or_billing_reconciled': False, 'zero_outbound': True, 'nonambiguous': True,
        }, {**{k: renewal[k] for k in ('policy','plan_hash','parent_stage_hash','run_id')},
            'operator_id': 'root', 'authorization_source': 'user-fix-and-continue', 'authorized_at': now,
            'expires_at': now + 3600, 'max_uses': 1})
        renewed = v1.reauthorize_expired_unused_review_recovery(service, c.snapshot, c.run, renewal,
            authorization=renewed_grant, source_material=material, prepared_mutation=prepared)
        assert renewed['authority_event']['original_row'] == list(original)
        assert renewed['authority_event']['authorization_path'] == str(renewed_grant.resolve())
        # Historical authentication must keep the new grant format after the live file expires.
        with c.store._db() as db:
            db.execute('BEGIN')
            try:
                encoded, = db.execute('SELECT data FROM business_semantic_recoveries WHERE recovery_id=?',
                                      (receipt['recovery_id'],)).fetchone()
                changed = json.loads(encoded)
                event = changed['authority_event']
                event['authorization_path'] = None
                downgraded = json.loads(event['grant_raw'])
                del downgraded['authorization_path']
                event['grant_raw'] = s._json(downgraded)
                event['authorization_ref'] = hashlib.sha256(event['grant_raw'].encode()).hexdigest()
                event['event_hash'] = s._hash({k: v for k, v in event.items() if k != 'event_hash'})
                db.execute('UPDATE business_semantic_recoveries SET data=? WHERE recovery_id=?',
                           (s._json(changed), receipt['recovery_id']))
                reader = s.semantic_checkpoint_rows(db, identity).read_checkpoint
                with pytest.raises(s.SemanticValidationError):
                    v1.authenticate_review_timeout_recovery(reader(child), snapshot=c.snapshot,
                                                            expected_input_hash=identity, read_checkpoint=reader)
            finally:
                db.rollback()
        assert _ledger(c)[0] == before[0] and _ledger(c)[2] == before[2]
        grant.unlink()
    _pause(c, False)
    active = s.BusinessSemanticIntegration(service, guard=c.runtime.guard)
    assert not active.review_timeout_recovery_allowlist
    if outcome == 'concurrent-claims':
        c.runtime._task_bindings[child] = {k: _read(c).read_checkpoint(child)['data'][k]
                                            for k in ('request', 'context', 'recovery_id', 'logical_task_hash')}
        first, second = (c.runtime._prepare_mutation(child, operation='consume_recovery') for _ in range(2))
        barrier = Barrier(2)

        def claim(candidate):
            barrier.wait()
            try:
                with c.store._lock, c.store._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    consume_recovery(db, c.snapshot, c.run, child, source_material=material, prepared_mutation=candidate)
                return True
            except s.SemanticValidationError:
                return False

        with held_project_lock(c.snapshot['project']['id']), ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, (first, second))) == [False, True]
        assert len(original_calls) == 7 and _ledger(c)[0][child][3] == 'running'
    else:
        launches = []

        def complete(system, text, **options):
            callback = options['before_send']
            assert callback._semantic_owned_snapshot()['stage_hash'] == child
            callback()
            if outcome == 'revoked-before-launch':
                grant.unlink()
            with native_attempt() as dispatch:
                dispatch.check_control(callback)
                dispatch.launch_attempted = True
                launches.append(child)
                error = ToolError('simulated repeated timeout', 408)
                error.codex_reason = 'timeout'
                raise error

        service.providers.complete = complete
        with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError):
            active.build(c.snapshot, c.run, units=c.units, routing=c.routing)
        assert launches == ([child] if outcome == 'renewed-timeout' else [])
        terminal = _read(c).read_checkpoint(child)
        assert terminal['status'] == 'needs_review' and terminal['data']['result'] is None
        assert terminal['data']['failure_code'] == ('model_call' if outcome == 'renewed-timeout' else 'admission_incomplete')
        final = _ledger(c)
        with held_project_lock(c.snapshot['project']['id']), pytest.raises(s.SemanticValidationError):
            active.build(c.snapshot, c.run, units=c.units, routing=c.routing)
        assert _ledger(c) == final and len(launches) == (outcome == 'renewed-timeout')
    saved = _ledger(c)
    assert saved[1][receipt['recovery_id']][5] is not None
    assert saved[0].keys() == before[0].keys() and saved[2] == before[2]
    assert all(saved[0][k] == row for k,row in before[0].items() if k != child)
