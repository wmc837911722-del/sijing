"""One operator-confirmed resubmission of a failed second draft in a consumed v2 pair."""
import json
import time
import uuid
from typing import NamedTuple

from . import business_semantics as s
from . import semantic_review_exception as v
from ._prepared_mutation import _SEAL

POLICY = 'v2-rejected-d2-proposal-once-v1'
CASE = frozenset('run_id input_hash bundle_id parent_stage_hash rejected_review_hash accepted_review_hash candidate_sha256'.split())
GRANT = frozenset('policy case plan_hash operator_id authorization_source authorized_at expires_at proposal_limit normal_review_limit reconciliation_ref reconciliation_hash'.split())
EVIDENCE = frozenset('case plan_hash checked_at old_owner_drained native_starts_after_two_d1 duplicate_fee_risk_accepted'.split())
TABLE = 'business_semantic_d2_attempts'


def _require(condition):
    s._require(condition, 'cache_invalid')


def read_audits(db, identity):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    records = []
    for key, encoded, consumed, dispatch in db.execute(
            'SELECT stage_hash,data,consumed_at,dispatch_id FROM business_semantic_d2_attempts WHERE input_hash=?', (identity,)):
        record = json.loads(encoded)
        _require(set(record) == {'plan','original','grant_raw','evidence_raw'} and s._json(record) == encoded
                 and record['plan']['case']['parent_stage_hash'] == key and record['plan']['case']['input_hash'] == identity)
        records.append({**record, 'consumed_at': consumed, 'dispatch_id': dispatch})
    _require(len(records) <= 1)
    return records


def _validate_case(case):
    _require(type(case) is dict and set(case) == CASE and type(case['run_id']) is str and case['run_id'])
    for name in CASE - {'run_id'}:
        v._sha(case[name])
    _require(len({case[k] for k in ('parent_stage_hash','rejected_review_hash','accepted_review_hash')}) == 3)


def _reconstruct(snapshot, case, bundle, receipts, reader, original):
    _validate_case(case)
    _require(bundle['bundle_id'] == case['bundle_id'] and bundle['run_id'] == case['run_id']
             and bundle['input_hash'] == case['input_hash'] and len(receipts) == 2
             and {r['child_stage_hash'] for r in receipts} == {case['accepted_review_hash'],case['rejected_review_hash']})
    accepted, rejected = [reader(case[n]) for n in ('accepted_review_hash','rejected_review_hash')]
    _require(accepted['status'] == 'accepted' and rejected['status'] == 'needs_review'
             and rejected['data']['failure_code'] == 'review_rejected'
             and all(type(r['consumed_at']) is int and s.HEX.fullmatch(r['dispatch_id']) for r in receipts))
    _require(type(original) in (list,tuple) and len(original) == 8
             and tuple(original[:5]) == (case['parent_stage_hash'],'proposal',case['input_hash'],'needs_review',2)
             and original[6] == 'model_call')
    old = json.loads(original[5])
    _require(set(old) == {'request','context','request_hash','result','result_hash','failure_code','diagnostic'}
             and old['failure_code'] == 'model_call' and old['diagnostic'] == {'status_code':422}
             and old['result'] is None and old['result_hash'] == s._hash(None)
             and old['request_hash'] == case['parent_stage_hash'])
    subject = reader(rejected['data']['context']['proposal_hash'])
    payload = s._revision_payload_primitive(snapshot,subject,rejected)
    revision = s._revision_context(2,proposal=subject,failure=rejected)
    revision['feedback_hash'] = s._hash(payload['revision_feedback'])
    _require(old['request'] == payload and old['context'] == {**subject['data']['context'],'revision':revision}
             and s.semantic_task_identity_hash(snapshot,case['input_hash'],'proposal',payload,old['context']) == old['request_hash'])
    base = {'policy':POLICY,'case':case,'snapshot_hash':s._hash(snapshot),'bundle_plan_hash':bundle['plan_hash'],
            'packet_hash':old['context']['packet_hash'],'original_digest':s._hash(list(original)),
            'accepted_digest':reader.raw_digest(accepted['stage_hash']),'rejected_digest':reader.raw_digest(rejected['stage_hash']),
            'receipt_pair_hash':s._hash(receipts),'old_candidate_hash':subject['data']['result_hash']}
    return {**base,'plan_hash':s._hash(base)}


def d2_retry_plan(snapshot, run, *, read_checkpoint, expected_case):
    _validate_case(expected_case)
    identity = run['semantic_input_hash']
    _require(expected_case['run_id'] == run['run_id'] and expected_case['input_hash'] == identity)
    proof = v.authenticate_review_exception_bundle(snapshot=snapshot,expected_input_hash=identity,read_checkpoint=read_checkpoint)
    bundle, receipts, _ = v._proof_parts(proof,read_checkpoint,snapshot,identity)
    _require(not read_checkpoint.d2_retry_rows(identity))
    return _reconstruct(snapshot,expected_case,bundle,receipts,read_checkpoint,
                        read_checkpoint.raw_checkpoint_row(expected_case['parent_stage_hash']))


def _authority(case, plan_hash, grant_raw, evidence_raw):
    grant = v._closed_json(grant_raw, GRANT, v.AUTH_BYTES)
    evidence = v._closed_json(evidence_raw, EVIDENCE, v.AUTH_BYTES)
    _require(grant['case'] == evidence['case'] == case and grant['plan_hash'] == evidence['plan_hash'] == plan_hash
             and grant['policy'] == POLICY and grant['operator_id'] == 'root'
             and grant['authorization_source'] == 'user-confirmed-d2-once'
             and all(type(grant[k]) is int and grant[k] == 1 for k in ('proposal_limit','normal_review_limit'))
             and type(grant['authorized_at']) is type(grant['expires_at']) is type(evidence['checked_at']) is int
             and 0 <= grant['authorized_at'] - evidence['checked_at'] <= 60
             and 0 < grant['expires_at'] - grant['authorized_at'] <= 86400
             and grant['reconciliation_hash'] == v._raw_hash(evidence_raw)
             and evidence['old_owner_drained'] is True and evidence['duplicate_fee_risk_accepted'] is True
             and type(evidence['native_starts_after_two_d1']) is int and evidence['native_starts_after_two_d1'] == 0)
    return grant


class _Authorization:
    __slots__ = ('service','path','case_raw','grant_raw','evidence_raw','seal')

    def __init__(self, service, path, case, grant, evidence, *, seal):
        _require(seal is _SEAL)
        for name, value in zip(self.__slots__, (service,str(path),s._json(case),grant,evidence,seal)):
            object.__setattr__(self,name,value)

    def __setattr__(self, name, value):
        raise AttributeError('immutable authorization')

    def __reduce__(self):
        raise TypeError('local authorization cannot be serialized')


def load_d2_retry_authorization(service, grant_path, *, expected_case):
    _validate_case(expected_case)
    raw = v._authority_text(grant_path)
    grant = v._closed_json(raw,GRANT,v.AUTH_BYTES)
    evidence = v._authority_text(grant['reconciliation_ref'])
    v._sha(grant['plan_hash'])
    _authority(expected_case,grant['plan_hash'],raw,evidence)
    cap = _Authorization(service,grant_path,expected_case,raw,evidence,seal=_SEAL)
    service._d2_retry_authorization = cap
    return cap


def _live(cap, service, plan, *, fresh=False):
    _require(type(cap) is _Authorization and cap.seal is _SEAL and cap.service is service
             and getattr(service,'_d2_retry_authorization',None) is cap and cap.case_raw == s._json(plan['case']))
    raw = v._authority_text(cap.path)
    _require(raw == cap.grant_raw)
    grant = v._closed_json(raw,GRANT,v.AUTH_BYTES)
    evidence = v._authority_text(grant['reconciliation_ref'])
    _require(evidence == cap.evidence_raw)
    _authority(plan['case'],plan['plan_hash'],raw,evidence)
    now = time.time()
    _require(grant['authorized_at'] <= now < grant['expires_at']
             and (not fresh or now - grant['authorized_at'] <= 60))
    return raw,evidence


def authenticate_audit(snapshot, bundle, receipts, reader, audit):
    plan = audit['plan']
    case = plan['case']
    _require(plan == _reconstruct(snapshot,case,bundle,receipts,reader,audit['original']))
    grant = _authority(case,plan['plan_hash'],audit['grant_raw'],audit['evidence_raw'])
    row = reader(case['parent_stage_hash'])
    old = json.loads(audit['original'][5])
    _require(row['attempt'] == 2 and row['stage'] == 'proposal' and row['data']['request'] == old['request']
             and row['data']['context'] == old['context'] and row['data']['d2_retry_id'] == plan['plan_hash']
             and reader.raw_checkpoint_row(row['stage_hash'])[7] == audit['original'][7])
    if row['status'] == 'planned':
        _require(audit['consumed_at'] is None and audit['dispatch_id'] is None)
    else:
        _require(type(audit['consumed_at']) is int and grant['authorized_at'] <= audit['consumed_at'] < grant['expires_at'])
        v._sha(audit['dispatch_id'])


class _PreparedRetry(NamedTuple):
    plan_raw: str
    scope: tuple
    dynamic: object
    source: object
    original: tuple


def prepare_d2_retry(service, snapshot, run, plan):
    """Prove the failed attempt against one read snapshot before an operator signs."""
    from ._prepared_mutation import _scope
    from ._source_material import _capture
    from .semantic_recovery import _current_context, _database_controls

    identity = run['semantic_input_hash']
    with service.store._db() as db:
        db.execute('BEGIN')
        _current_context(db, snapshot, run)
        _database_controls(db, run, paused=True)
        reader = s.semantic_checkpoint_rows(db, identity).read_checkpoint
    _require(plan == d2_retry_plan(snapshot, run, read_checkpoint=reader, expected_case=plan['case']))
    ready = _PreparedRetry(s._json(plan), _scope(snapshot, run), reader._raw_dynamic_capture(identity),
                           _capture(reader), reader.raw_checkpoint_row(plan['case']['parent_stage_hash']))
    service._prepared_d2_retry = ready
    return ready


def authorize_d2_retry(service, snapshot, run, plan, *, authorization, prepared=None):
    from ._prepared_mutation import _capture_dynamic, _same_dynamic, _scope
    from ._source_material import _capture_raw_source, _same_raw
    from .semantic_recovery import _database_controls

    identity = run['semantic_input_hash']
    case = plan['case']
    ready = prepare_d2_retry(service, snapshot, run, plan) if prepared is None else prepared
    with service._exclusive(snapshot['project']['id']):
        _require(type(ready) is _PreparedRetry and ready is getattr(service, '_prepared_d2_retry', None)
                 and ready.plan_raw == s._json(plan) and ready.scope == _scope(snapshot, run))
        service.project_analysis.guard(snapshot)
        original = ready.original
        grant,evidence = _live(authorization,service,plan,fresh=True)
        encoded = s._json(dict(plan=plan,original=list(original),grant_raw=grant,evidence_raw=evidence))
        with service.store._lock,service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _require(_same_dynamic(ready.dynamic,_capture_dynamic(db,identity))
                     and _same_raw(ready.source,_capture_raw_source(db,identity)) and not read_audits(db,identity))
            # Source capture ignores progress timestamps; recheck their effect on run selection.
            latest = db.execute('SELECT id FROM full_analysis_runs WHERE project_id=? '
                                'ORDER BY updated_at DESC,rowid DESC LIMIT 1',(snapshot['project']['id'],)).fetchone()
            _require(latest == (run['run_id'],))
            _database_controls(db,run,paused=True)
            db.execute('CREATE TABLE IF NOT EXISTS business_semantic_d2_attempts (stage_hash TEXT PRIMARY KEY, input_hash TEXT NOT NULL UNIQUE, data TEXT NOT NULL, consumed_at INTEGER, dispatch_id TEXT)')
            db.execute('INSERT INTO business_semantic_d2_attempts VALUES (?,?,?,NULL,NULL)',(case['parent_stage_hash'],identity,encoded))
            data = json.loads(original[5])
            del data['failure_code'], data['diagnostic']
            data['d2_retry_id'] = plan['plan_hash']
            changed = db.execute("UPDATE business_semantic_tasks SET status='planned',data=?,error_code=NULL WHERE stage_hash=? AND status='needs_review' AND data=? AND error_code='model_call'",
                                 (s._json(data),case['parent_stage_hash'],original[5])).rowcount
            _require(changed == 1)
            _require(_live(authorization,service,plan,fresh=True) == (grant,evidence))
            service.project_analysis.guard(snapshot)
    return plan['plan_hash']


def claim(runtime, db, audit, key, *, reader):
    from ._prepared_mutation import _capture_dynamic, _same_dynamic
    from ._source_material import _capture, _capture_raw_source, _same_raw
    from .semantic_recovery import _database_controls, _require_project_lock
    plan = audit['plan']
    _require_project_lock(runtime._snapshot['project']['id'])
    identity = runtime._input_hash
    _require(_same_dynamic(reader._raw_dynamic_capture(identity), _capture_dynamic(db,identity))
             and _same_raw(_capture(reader), _capture_raw_source(db,identity)))
    latest = db.execute('SELECT id FROM full_analysis_runs WHERE project_id=? '
                        'ORDER BY updated_at DESC,rowid DESC LIMIT 1',(runtime._snapshot['project']['id'],)).fetchone()
    _require(latest == (runtime._run['run_id'],))
    _database_controls(db,runtime._run,paused=False)
    runtime.guard()
    _require(read_audits(db,runtime._input_hash) == [audit] and key == plan['case']['parent_stage_hash'])
    authority = _live(getattr(runtime.service,'_d2_retry_authorization',None),runtime.service,plan)
    _require(authority == (audit['grant_raw'],audit['evidence_raw']))
    stamp,dispatch = int(time.time()),uuid.uuid4().hex+uuid.uuid4().hex
    changed = db.execute('UPDATE business_semantic_d2_attempts SET consumed_at=?,dispatch_id=? WHERE stage_hash=? AND consumed_at IS NULL AND dispatch_id IS NULL',
                         (stamp,dispatch,key)).rowcount
    _require(changed == 1)
    return dispatch


def send_guard(runtime, audit, key, stage, context):
    plan = audit['plan']; case = plan['case']
    _require(stage == 'proposal' and key == case['parent_stage_hash'] or stage == 'review'
             and context['proposal_hash'] == case['parent_stage_hash'] and context['revision']['parent_failure_hash'] == case['rejected_review_hash'])
    authority = _live(getattr(runtime.service,'_d2_retry_authorization',None),runtime.service,plan)
    _require(authority == (audit['grant_raw'],audit['evidence_raw']))
    from .semantic_recovery import _database_controls, _require_project_lock
    _require_project_lock(runtime._snapshot['project']['id'])
    with runtime.store._db() as db:
        reader = s.semantic_checkpoint_reader(db)
        _database_controls(db,runtime._run,paused=False)
        rows = read_audits(db,runtime._input_hash)
        _require(len(rows) == 1 and rows[0]['plan'] == plan and rows[0]['consumed_at'] is not None
                 and reader(key)['status'] == 'running' and key in runtime._owned_slots)


def check_projection(proof, reader, identity):
    audits = reader.d2_retry_rows(identity) if callable(getattr(reader,'d2_retry_rows',None)) else []
    if audits or 'd2_retry_attempt_hash' in proof:
        _require(len(audits) == 1 and audits[0]['consumed_at'] is not None
                 and proof.get('d2_retry_attempt_hash') == s._hash(audits[0]))
