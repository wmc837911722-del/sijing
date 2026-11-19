import hashlib
import json
import time

import pytest

from businessmap import business_semantics as s
from businessmap import semantic_d2_retry as retry
from businessmap.providers import ToolError
from businessmap.semantic_recovery import held_project_lock
from review_exception_fixture import two_review_history
from test_business_semantics import independent_candidate, review
from test_review_exception_bundle import authorize_fixture


def test_failed_second_draft_is_archived_and_resubmitted_once(two_review_history, tmp_path):
    c = two_review_history
    bundle, _, _ = authorize_fixture(c, tmp_path)
    control = 'full_analysis_control:' + c.run['run_id']
    c.store.config_set(control, {'paused': False, 'cancelled': False})
    submitted = []

    def complete(system, text, **kwargs):
        payload = json.loads(text)
        kwargs['before_send']()
        submitted.append(payload['stage'])
        if len(submitted) == 3:
            raise ToolError('synthetic admission failure',422)
        result = independent_candidate(payload) if payload['stage'] == 'proposal' else review(payload, reject=len(submitted) == 2)
        return {'text': json.dumps(result, ensure_ascii=False), 'usage': {}, 'output_limited': False}

    c.service.providers.complete = complete
    with held_project_lock(c.snapshot['project']['id']):
        first = c.runtime._integrate_packet(c.packets[0])
        with pytest.raises(s.SemanticValidationError):
            c.runtime._integrate_packet(c.packets[1])
    assert submitted == ['review', 'review', 'proposal']
    identity = c.run['semantic_input_hash']
    c.store.config_set(control, {'paused': True, 'cancelled': False})
    with c.store._db() as db:
        rows = s.semantic_checkpoint_rows(db, identity)
    failed, = [r for r in rows if r['attempt'] == 2]
    old = rows.read_checkpoint.raw_checkpoint_row(failed['stage_hash'])
    case = dict(run_id=c.run['run_id'], input_hash=identity, bundle_id=bundle['bundle_id'],
                parent_stage_hash=failed['stage_hash'], accepted_review_hash=first['review_hash'],
                rejected_review_hash=failed['data']['context']['revision']['parent_failure_hash'],
                candidate_sha256='7' * 64)
    plan = retry.d2_retry_plan(c.snapshot, c.run, read_checkpoint=rows.read_checkpoint, expected_case=case)
    prepared = retry.prepare_d2_retry(c.service, c.snapshot, c.run, plan)
    stamp = int(time.time())
    evidence = dict(case=case, plan_hash=plan['plan_hash'], checked_at=stamp, old_owner_drained=True,
                    native_starts_after_two_d1=0, duplicate_fee_risk_accepted=True)
    evidence_path = tmp_path/'d2-evidence.json'
    evidence_path.write_text(json.dumps(evidence)); evidence_path.chmod(0o600)
    grant = dict(policy=retry.POLICY, case=case, plan_hash=plan['plan_hash'], operator_id='root',
                 authorization_source='user-confirmed-d2-once', authorized_at=stamp, expires_at=stamp+3600,
                 proposal_limit=1, normal_review_limit=1, reconciliation_ref=str(evidence_path),
                 reconciliation_hash=hashlib.sha256(evidence_path.read_bytes()).hexdigest())
    grant_path = tmp_path/'d2-grant.json'
    grant_path.write_text(json.dumps(grant)); grant_path.chmod(0o600)
    cap = retry.load_d2_retry_authorization(c.service, grant_path, expected_case=case)
    c.service._review_exception_authorizations.clear()
    with held_project_lock(c.snapshot['project']['id']):
        retry.authorize_d2_retry(c.service,c.snapshot,c.run,plan,authorization=cap,prepared=prepared)
        with pytest.raises(s.SemanticValidationError):
            retry.authorize_d2_retry(c.service,c.snapshot,c.run,plan,authorization=cap,prepared=prepared)
    c.store.config_set(control, {'paused': False, 'cancelled': False})
    with held_project_lock(c.snapshot['project']['id']):
        result = c.runtime._integrate_packet(c.packets[1])
        assert c.runtime._integrate_packet(c.packets[1]) == result
    assert submitted == ['review', 'review', 'proposal', 'proposal', 'review']
    with c.store._db() as db:
        final = s.semantic_checkpoint_rows(db, identity)
    assert final.read_checkpoint(result['review_hash'])['status'] == 'accepted'
    for name in ('accepted_review_hash','rejected_review_hash'):
        assert rows.read_checkpoint.raw_checkpoint_row(case[name]) == final.read_checkpoint.raw_checkpoint_row(case[name])
    assert final.read_checkpoint.d2_retry_rows(identity)[0]['original'] == list(old)
    inventory = s.semantic_task_inventory(final,snapshot=c.snapshot,expected_input_hash=identity)
    assert inventory['occupied_calls'] == sum(r['status'] != 'planned' for r in final) + 1
    with held_project_lock(c.snapshot['project']['id']):
        projection = c.runtime.build(c.snapshot,c.run,units=c.units,routing=c.routing)
    assert projection['verification']['d2_retry_attempt_hash'] == s._hash(final.read_checkpoint.d2_retry_rows(identity)[0])
    assert s.semantic_support_context(projection,snapshot=c.snapshot,expected_input_hash=identity,
                                      read_checkpoint=final.read_checkpoint)
    assert submitted == ['review', 'review', 'proposal', 'proposal', 'review']
