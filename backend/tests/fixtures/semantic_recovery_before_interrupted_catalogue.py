"""Private operator-only, one-shot transport ancestry. Never a model retry policy."""
from copy import deepcopy
import json
import logging
import time
import uuid
from . import business_semantics as s

POLICY = 'operator-catalogue-provider502-once-v1'
BOUNDED_MERGE_POLICY = 'operator-bounded-catalogue-provider502-once-v1'
_LOG = logging.getLogger(__name__)

def _require(value):
    s._require(value, 'cache_invalid')

def _eligible(row, snapshot):
    data = row['data']
    diagnostic = data.get('diagnostic', {})
    _require(s._structured_output(snapshot) and row['stage'] == 'catalogue'
        and type(row['attempt']) is int and row['attempt'] == 1 and row['status'] == 'needs_review'
        and data['context'].get('catalogue_mode') == data['request'].get('mode') == 'leaf'
        and data.get('failure_code') == 'model_call' and data['result'] is None
        and diagnostic.get('codex_reason') == 'provider_error'
        and type(diagnostic.get('status_code')) is int and diagnostic['status_code'] == 502
        and 'recovery_id' not in data and 'logical_task_hash' not in data)

def _recovery_policy(row, snapshot):
    data = row['data']
    if data['request'].get('mode') == 'leaf':
        _eligible(row, snapshot)
        return POLICY
    diagnostic = data.get('diagnostic', {})
    _require(s._structured_output(snapshot) and row['stage'] == 'catalogue'
        and type(row['attempt']) is int and row['attempt'] == 1 and row['status'] == 'needs_review'
        and data['request'].get('stage') == 'catalogue'
        and data['context'].get('catalogue_mode') == data['request'].get('mode') == 'merge'
        and data['context'].get('merge_contract') == data['request'].get('merge_contract') == s.BOUNDED_MERGE_CONTRACT
        and data.get('failure_code') == 'model_call' and data['result'] is None
        and data['result_hash'] == s._hash(None)
        and diagnostic.get('codex_reason') == 'provider_error'
        and type(diagnostic.get('status_code')) is int and diagnostic['status_code'] == 502
        and not {'recovery_id', 'logical_task_hash', 'supersession_id'}.intersection(data))
    return BOUNDED_MERGE_POLICY

def _contract(snapshot, row):
    data = row['data']
    return {'generator_hash': s._semantic_generator_hash(snapshot),
        'destination_digest': s._hash(s.provider_destination(snapshot['provider'])),
        'system_digest': s._hash(s.semantic_stage_system(snapshot, 'catalogue', data['request'])),
        'wire_digest': s._hash(s._json(data['request'])),
        'schema_digest': s._hash(s.semantic_output_schema('catalogue', data['request'])),
        'budget_profile_digest': s._hash(data['context']['budget_profile'])}

def catalogue_recovery_plan(snapshot, run, parent_stage_hash, *, read_checkpoint):
    """Pure read-only plan; requires trusted complete current SQLite reader."""
    _require(callable(getattr(read_checkpoint, 'raw_digest', None)) and callable(getattr(read_checkpoint, 'recovery_receipts', None)))
    input_hash = run['semantic_input_hash']
    parent = s._checkpoint_record(parent_stage_hash,snapshot=snapshot,expected_input_hash=input_hash,read_checkpoint=read_checkpoint)
    policy = _recovery_policy(parent, snapshot)
    plan = {'policy':policy,'parent_stage_hash':parent_stage_hash,'logical_task_hash':parent_stage_hash,
        'parent_failure_digest':read_checkpoint.raw_digest(parent_stage_hash),'input_hash':input_hash,
        'run_id':run['run_id'],'snapshot_id':run['snapshot_id'],'project_id':snapshot['project']['id'],
        'project_revision':run['project_revision'],'vault_identity':s._hash(snapshot.get('vault_path')), **_contract(snapshot,parent)}
    _require(plan['parent_failure_digest'] is not None and snapshot['project']['revision'] == run['project_revision'])
    plan['recovery_id'] = s._hash(plan)
    plan['child_stage_hash'] = s._hash({'policy':policy,'recovery_id':plan['recovery_id'],'parent_stage_hash':parent_stage_hash})
    plan['plan_hash'] = s._hash(plan)
    return plan

PLAN_FIELDS = frozenset(('policy parent_stage_hash logical_task_hash parent_failure_digest input_hash run_id snapshot_id project_id project_revision vault_identity generator_hash destination_digest system_digest wire_digest schema_digest budget_profile_digest recovery_id child_stage_hash plan_hash').split())
AUTH_FIELDS = frozenset(('policy plan_hash parent_stage_hash run_id operator_id authorization_source authorized_at expires_at max_uses reconciliation_ref reconciliation_hash').split())
RECEIPT_FIELDS = PLAN_FIELDS | AUTH_FIELDS | {'authorization_ref'}

def _check_grant(authorization, plan):
    import hashlib
    import os
    import stat
    from pathlib import Path
    path = Path(authorization)
    _require(path.is_absolute() and stat.S_ISREG(path.stat().st_mode) and path.stat().st_mode & 0o777 == 0o600 and not path.is_symlink())
    raw = path.read_bytes()
    auth = json.loads(raw)
    _require(set(auth) == AUTH_FIELDS)
    _require(all(auth[k] == plan[k] for k in ('policy','plan_hash','parent_stage_hash','run_id')))
    _require(auth['operator_id'] == 'root' and auth['authorization_source'] == 'user-fix-and-continue'
        and type(auth['max_uses']) is int and auth['max_uses'] == 1
        and type(auth['authorized_at']) is int and type(auth['expires_at']) is int
        and auth['expires_at'] - auth['authorized_at'] == 3600
        and auth['authorized_at'] <= time.time() < auth['expires_at'])
    evidence = Path(auth['reconciliation_ref'])
    _require(evidence.is_absolute())
    raw_evidence = evidence.read_bytes()
    _require(hashlib.sha256(raw_evidence).hexdigest() == auth['reconciliation_hash'])
    check = json.loads(raw_evidence)
    _require(set(check) == {'checked_at','local_owner_pids','local_processes_drained','upstream_result_or_billing_reconciled'}
        and type(check['checked_at']) is int and 0 <= time.time()-check['checked_at'] <= 60
        and check['local_processes_drained'] is True and check['upstream_result_or_billing_reconciled'] is False
        and isinstance(check['local_owner_pids'], list) and bool(check['local_owner_pids'])
        and len(set(check['local_owner_pids'])) == len(check['local_owner_pids']))
    for pid in check['local_owner_pids']:
        _require(type(pid) is int and pid > 0)
        try: os.kill(pid, 0)
        except ProcessLookupError: pass
        else: _require(False)
    return {**auth,'authorization_ref':hashlib.sha256(raw).hexdigest()}

def _receipt_plan(receipt):
    return {key:receipt[key] for key in PLAN_FIELDS}

def authenticate_recovery(row, *, snapshot, expected_input_hash, read_checkpoint):
    """Authenticate transport ancestry using trusted current rows, never inline proof."""
    _require(callable(getattr(read_checkpoint,'recovery_receipts',None)))
    matches = [r for r in read_checkpoint.recovery_receipts(expected_input_hash) if r['child_stage_hash'] == row['stage_hash']]
    _require(len(matches) == 1)
    receipt = matches[0]
    _require(set(receipt) == RECEIPT_FIELDS | {'consumed_at','dispatch_id'})
    run = {k:receipt[k] for k in ('run_id','snapshot_id','project_revision')}
    run['semantic_input_hash'] = expected_input_hash
    plan = catalogue_recovery_plan(snapshot,run,receipt['parent_stage_hash'],read_checkpoint=read_checkpoint)
    _require(_receipt_plan(receipt) == plan and receipt['operator_id'] == 'root'
        and receipt['authorization_source'] == 'user-fix-and-continue'
        and type(receipt['max_uses']) is int and receipt['max_uses'] == 1
        and type(receipt['authorized_at']) is int and type(receipt['expires_at']) is int
        and receipt['expires_at']-receipt['authorized_at'] == 3600
        and all(isinstance(receipt[k],str) and s.HEX.fullmatch(receipt[k]) for k in ('authorization_ref','reconciliation_hash')))
    if receipt['policy'] == BOUNDED_MERGE_POLICY:
        from pathlib import Path
        _require(isinstance(receipt['reconciliation_ref'], str) and Path(receipt['reconciliation_ref']).is_absolute())
        _require(type(row['attempt']) is int and row['attempt'] == 1
            and row['status'] in {'planned', 'running', 'received', 'accepted', 'needs_review'})
        if row['status'] in {'accepted', 'received'}:
            _require(isinstance(row['data']['result'], list) and bool(row['data']['result']))
    parent = read_checkpoint(receipt['parent_stage_hash'])
    data = row['data']
    _require(row['stage']=='catalogue' and row['attempt']==1 and row['input_hash']==expected_input_hash
        and 'supersession_id' not in data
        and data.get('recovery_id')==receipt['recovery_id'] and data.get('logical_task_hash')==receipt['logical_task_hash']
        and data['request']==parent['data']['request'] and data['context']==parent['data']['context'])
    if row['status']=='planned':
        _require(receipt['consumed_at'] is None and receipt['dispatch_id'] is None)
    else:
        _require(type(receipt['consumed_at']) is int and receipt['authorized_at']<=receipt['consumed_at']<receipt['expires_at']
            and isinstance(receipt['dispatch_id'],str) and s.HEX.fullmatch(receipt['dispatch_id']))
    return receipt

def recovery_inventory(rows, *, snapshot, expected_input_hash, read_checkpoint):
    """Return the exact authenticated receipt closure including unused authorizations."""
    receipts = read_checkpoint.recovery_receipts(expected_input_hash) if callable(getattr(read_checkpoint,'recovery_receipts',None)) else []
    by_key = {r['stage_hash']:r for r in rows}
    children = [r for r in rows if 'recovery_id' in r['data'] or 'logical_task_hash' in r['data']]
    _require(len(receipts) == len(children))
    verified = [authenticate_recovery(r,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint) for r in children]
    _require(len({r['parent_stage_hash'] for r in verified}) == len(verified)
        and all(r['parent_stage_hash'] in by_key for r in verified))
    return verified

def receipt_hash(receipt):
    return s._hash({key:receipt[key] for key in RECEIPT_FIELDS})

def authorize_catalogue_recovery(service, snapshot, run, plan, *, authorization):
    """Operator control only: lock, current guard, paused state, atomic receipt+child."""
    _require(set(plan)==PLAN_FIELDS)
    created = False
    with service._exclusive(snapshot['project']['id']):
        service.project_analysis.guard(snapshot)
        controls = service._controls(run)
        _require(controls.get('paused') is True and not controls.get('cancelled'))
        auth = _check_grant(authorization, plan)
        with service.store._lock, service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            service.project_analysis.guard(snapshot)
            _current_context(db, snapshot, run)
            _database_controls(db, run, paused=True)
            reader = s.semantic_checkpoint_reader(db)
            _require(catalogue_recovery_plan(snapshot,run,plan['parent_stage_hash'],read_checkpoint=reader)==plan)
            db.execute('''CREATE TABLE IF NOT EXISTS business_semantic_recoveries (
                recovery_id TEXT PRIMARY KEY, parent_stage_hash TEXT NOT NULL UNIQUE,
                child_stage_hash TEXT NOT NULL UNIQUE,input_hash TEXT NOT NULL,data TEXT NOT NULL,
                consumed_at INTEGER,dispatch_id TEXT,
                CHECK ((consumed_at IS NULL) = (dispatch_id IS NULL)))''')
            existing = reader.recovery_receipts(plan['input_hash'])
            found = [r for r in existing if r['parent_stage_hash']==plan['parent_stage_hash']]
            if found:
                receipt = found[0]
                _require(_receipt_plan(receipt)==plan and receipt['authorization_ref']==auth['authorization_ref'])
                authenticate_recovery(reader(plan['child_stage_hash']),snapshot=snapshot,expected_input_hash=plan['input_hash'],read_checkpoint=reader)
            else:
                receipt = {**plan,**auth}
                parent = reader(plan['parent_stage_hash'])
                data = {'request':deepcopy(parent['data']['request']),'context':deepcopy(parent['data']['context']),
                    'request_hash':plan['child_stage_hash'],'result':None,'result_hash':s._hash(None),
                    'recovery_id':plan['recovery_id'],'logical_task_hash':plan['logical_task_hash']}
                db.execute('INSERT INTO business_semantic_recoveries VALUES (?,?,?,?,?,NULL,NULL)',
                    (plan['recovery_id'],plan['parent_stage_hash'],plan['child_stage_hash'],plan['input_hash'],s._json(receipt)))
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                    (plan['child_stage_hash'],'catalogue',plan['input_hash'],'planned',1,s._json(data),None,s.now()))
                receipt = {**receipt,'consumed_at':None,'dispatch_id':None}
                created = True
    if created: log_event('authorized', receipt)
    return receipt

def consume_recovery(db, snapshot, run, child_stage_hash):
    """Caller owns transaction and logs only after commit. CAS burns before dispatch."""
    _require_project_lock(snapshot['project']['id'])
    _current_context(db, snapshot, run)
    _database_controls(db, run, paused=False)
    reader = s.semantic_checkpoint_reader(db)
    row = reader(child_stage_hash)
    receipt = authenticate_recovery(row,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=reader)
    _require(all(receipt[k]==run[k] for k in ('run_id','snapshot_id','project_revision')))
    _check_consumption_reconciliation(receipt)
    stamp = int(time.time())
    _require(row['status']=='planned' and receipt['authorized_at']<=stamp<receipt['expires_at'])
    dispatch_id = uuid.uuid4().hex + uuid.uuid4().hex
    _require(db.execute('UPDATE business_semantic_recoveries SET consumed_at=?,dispatch_id=? WHERE recovery_id=? AND consumed_at IS NULL',
        (stamp,dispatch_id,receipt['recovery_id'])).rowcount==1)
    _require(db.execute("UPDATE business_semantic_tasks SET status='running' WHERE stage_hash=? AND status='planned'",(child_stage_hash,)).rowcount==1)
    return {**receipt,'consumed_at':stamp,'dispatch_id':dispatch_id}

def log_event(status, receipt):
    _LOG.info('semantic_recovery_'+status,extra={'event':'semantic_recovery_'+status,'status':status,
        **{key:receipt[key] for key in ('run_id','input_hash','logical_task_hash','parent_stage_hash','recovery_id','child_stage_hash','dispatch_id')},
        **({'policy': receipt['policy']} if receipt['policy'] == BOUNDED_MERGE_POLICY else {})})

def _current_context(db, snapshot, run):
    """Rebuild the semantic scope from current persisted run/chunks/addenda, under write lock."""
    from .analysis_addenda import validate_addendum_receipt, validate_matching_receipt
    persisted = db.execute('SELECT project_id,snapshot_id,data FROM full_analysis_runs WHERE id=?',(run['run_id'],)).fetchone()
    _require(persisted is not None and persisted[:2] == (snapshot['project']['id'],run['snapshot_id']))
    latest = db.execute('SELECT id FROM full_analysis_runs WHERE project_id=? AND snapshot_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 1',
        (snapshot['project']['id'],run['snapshot_id'])).fetchone()
    _require(latest == (run['run_id'],))
    current = json.loads(persisted[2])
    _require(all(current.get(k)==run.get(k) for k in ('run_id','snapshot_id','project_revision','source_repositories','catalogue_override_revision')))
    chunks = [json.loads(r[0]) for r in db.execute('SELECT data FROM full_analysis_chunks WHERE run_id=? ORDER BY position',(run['run_id'],))]
    addenda = []
    matches = [json.loads(r[0]) for r in db.execute('SELECT data FROM analysis_matching_receipts WHERE run_id=? ORDER BY id',(run['run_id'],))]
    for row in db.execute('SELECT data FROM analysis_addenda WHERE run_id=? ORDER BY id',(run['run_id'],)):
        receipt = json.loads(row[0])
        if receipt.get('status')!='accepted': continue
        validate_addendum_receipt(snapshot,current,receipt,chunks)
        attached = [m for m in matches if m.get('addendum_id')==receipt['addendum_id']]
        for matching in attached: validate_matching_receipt(snapshot,current,receipt,matching)
        if attached: receipt['matching_receipts'] = attached
        addenda.append(receipt)
    _require(s.semantic_input_hash(snapshot,current,chunks,addenda)==run['semantic_input_hash'])
    return current

# This token records an OS flock already acquired by FullProjectAnalysis. It is
# not authorization by itself and forked children cannot inherit ownership.
from contextlib import contextmanager
import os
_PROJECT_LOCKS = set()

@contextmanager
def held_project_lock(project_id):
    key = (os.getpid(), project_id)
    _require(key not in _PROJECT_LOCKS)
    _PROJECT_LOCKS.add(key)
    try: yield
    finally: _PROJECT_LOCKS.discard(key)

def _require_project_lock(project_id):
    _require((os.getpid(), project_id) in _PROJECT_LOCKS)

def _check_consumption_reconciliation(receipt):
    import hashlib
    from pathlib import Path
    raw = Path(receipt['reconciliation_ref']).read_bytes()
    _require(hashlib.sha256(raw).hexdigest()==receipt['reconciliation_hash'])
    evidence = json.loads(raw)
    for pid in evidence['local_owner_pids']:
        try: os.kill(pid, 0)
        except ProcessLookupError: pass
        else: _require(False)


def _database_controls(db, run, *, paused):
    row = db.execute('SELECT value FROM config WHERE key=?', ('full_analysis_control:'+run['run_id'],)).fetchone()
    controls = json.loads(row[0]) if row else {'paused':False,'cancelled':False}
    _require(controls.get('paused') is paused and controls.get('cancelled',False) is False)
