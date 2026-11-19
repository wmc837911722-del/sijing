"""Explicit, operator-authorized transition to a different catalogue contract.

This module never interprets a stored HTTP fallback as upstream failure evidence.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from . import business_semantics as s

POLICY = 'operator-catalogue-merge-contract-once-v1'
PLAN_FIELDS = frozenset(('policy supersession_id parent_stage_hash parent_failure_digest child_stage_hash input_hash generator_hash run_id snapshot_id project_id project_revision vault_identity destination_digest old_contract new_contract old_system_digest new_system_digest old_wire_digest new_wire_digest schema_digest budget_profile_digest label_members_digest parent_bindings_digest new_request new_context plan_hash').split())
AUTH_FIELDS = frozenset(('policy plan_hash supersession_id parent_stage_hash child_stage_hash run_id operator_id authorization_source authorized_at expires_at max_uses reconciliation_ref reconciliation_hash').split())
RECONCILIATION_FIELDS = frozenset(('checked_at run_id parent_stage_hash local_owner_pids local_processes_drained upstream_result_or_billing_reconciled observed_error_type observed_http_status observed_codex_reason actual_trace_available evidence_ref evidence_hash').split())
RECEIPT_FIELDS = PLAN_FIELDS | AUTH_FIELDS | {'authorization_ref'}


def _require(value):
    s._require(value, 'cache_invalid')


def _private_bytes(path):
    """Open once with no symlink traversal at the file boundary; validate fd."""
    path = Path(path)
    _require(path.is_absolute())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600)
        return handle.read()


def _check_reconciliation(auth):
    raw = _private_bytes(auth['reconciliation_ref'])
    _require(hashlib.sha256(raw).hexdigest() == auth['reconciliation_hash'])
    check = json.loads(raw)
    _require(set(check) == RECONCILIATION_FIELDS
        and type(check['checked_at']) is int
        and 0 <= auth['authorized_at'] - check['checked_at'] <= 60
        and check['run_id'] == auth['run_id']
        and check['parent_stage_hash'] == auth['parent_stage_hash']
        and check['local_processes_drained'] is True
        and check['upstream_result_or_billing_reconciled'] is False
        and check['observed_error_type'] == 'OperationalError'
        and check['observed_http_status'] is None and check['observed_codex_reason'] is None
        and check['actual_trace_available'] is False)
    pids = check['local_owner_pids']
    _require(isinstance(pids, list) and pids and all(type(pid) is int and pid > 0 for pid in pids)
        and len(set(pids)) == len(pids))
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            _require(False)
    evidence = _private_bytes(check['evidence_ref'])
    _require(hashlib.sha256(evidence).hexdigest() == check['evidence_hash'])
    # Evidence is operator observation, bound to this parent and the recorded type.
    text = evidence.decode('utf-8')
    _require(auth['parent_stage_hash'] in text and 'OperationalError' in text)
    return check


def _check_grant(authorization: Path, plan: dict) -> dict:
    """Pure file preflight. Must run before constructing a mutable Store."""
    try:
        _require(set(plan) == PLAN_FIELDS)
        raw = _private_bytes(authorization)
        auth = json.loads(raw)
        _require(set(auth) == AUTH_FIELDS
            and all(auth[key] == plan[key] for key in ('policy', 'plan_hash', 'supersession_id', 'parent_stage_hash', 'child_stage_hash', 'run_id'))
            and auth['policy'] == POLICY
            and auth['operator_id'] == 'root' and auth['authorization_source'] == 'user-fix-and-continue'
            and type(auth['max_uses']) is int and auth['max_uses'] == 1
            and type(auth['authorized_at']) is int and type(auth['expires_at']) is int
            and auth['expires_at'] - auth['authorized_at'] == 3600
            and auth['authorized_at'] <= time.time() < auth['expires_at'])
        _check_reconciliation(auth)
        return {**auth, 'authorization_ref': hashlib.sha256(raw).hexdigest()}
    except s.SemanticValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def supersession_receipt_hash(receipt):
    return s._hash({key: receipt[key] for key in RECEIPT_FIELDS})


def _source_inputs(snapshot, run, reader):
    """Authenticate persisted source scope before deriving its input identity."""
    from .analysis_addenda import validate_addendum_receipt, validate_matching_receipt
    _require(callable(getattr(reader, 'catalogue_source', None)))
    source = reader.catalogue_source(run['run_id'])
    _require(isinstance(source, dict) and set(source) == {'run', 'chunks', 'addenda', 'matching_receipts', 'l0_rows'})
    persisted = source['run']
    _require(isinstance(persisted, dict) and all(persisted.get(key) == run[key] for key in ('run_id', 'snapshot_id', 'project_revision'))
        and persisted.get('project_id') == snapshot['project']['id']
        and persisted['project_revision'] == snapshot['project']['revision'])
    _require({r['repository_id']: r['commit'] for r in persisted.get('source_repositories', [])}
        == {r['id']: r['commit'] for r in snapshot.get('repositories', [])})
    run = persisted
    chunks, addenda = source['chunks'], []
    for original in source['addenda']:
        if original.get('status') != 'accepted':
            continue
        receipt = deepcopy(original)
        receipt.pop('matching_receipts', None)
        validate_addendum_receipt(snapshot, run, receipt, chunks)
        matches = [m for m in source['matching_receipts'] if m.get('addendum_id') == receipt['addendum_id']]
        for match in matches:
            validate_matching_receipt(snapshot, run, receipt, match)
        if matches:
            receipt['matching_receipts'] = matches
        addenda.append(receipt)
    return run, source, addenda


def _routing_scope(snapshot, run, reader):
    expected_input_hash = run['semantic_input_hash']
    run, source, addenda = _source_inputs(snapshot, run, reader)
    _require(s.semantic_input_hash(snapshot, run, source['chunks'], addenda) == expected_input_hash)
    return {**run, 'semantic_input_hash': expected_input_hash}, source, addenda


def _unprepared_routing(snapshot, run, reader, source, addenda):
    from ._frozen_routing import FrozenReader
    if type(reader) is FrozenReader:
        return FrozenReader._routing(reader, snapshot, run, source, addenda)
    return _derive_source_routing(snapshot, run, source, addenda)


def _source_routing(snapshot, run, reader):
    """Recompute original batch and response order from authenticated SOURCE/L0."""
    run, source, addenda = _routing_scope(snapshot, run, reader)
    from ._source_material import _template
    prepared = _template(reader, 'routing', snapshot, run)
    if prepared is not None:
        return prepared
    return _unprepared_routing(snapshot, run, reader, source, addenda)


def _derive_source_routing(snapshot, run, source, addenda):
    from .full_synthesis import FullBusinessSynthesis, source_fact_units
    units = source_fact_units(snapshot, run, source['chunks'], addenda)
    synthesis = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    l0 = {row[0]: row for row in source['l0_rows']}
    _require(len(l0) == len(source['l0_rows']))
    routing = []
    for batch in synthesis._batches(snapshot, units, 0):
        found = []
        for legacy in (False, True):
            key = synthesis.cache_identity(snapshot, run, batch, 0, legacy=legacy)
            if key in l0:
                found.append(synthesis.validate_cache(l0[key], key, batch, legacy=legacy))
        _require(found and (len(found) == 1 or synthesis.navigation(found[0]) == synthesis.navigation(found[1])))
        routing.extend(found[0]['assignments'])
    return units, routing


def _validate_result(result, labels):
    _require(isinstance(result, list) and result)
    used = set()
    for operation in result:
        members = operation['fact_ids']
        selected = [i for i, label in enumerate(labels) if set(label['fact_ids']) <= set(members)]
        _require(selected and not used.intersection(selected) and len(members) == len(set(members))
            and set(members) == {fid for i in selected for fid in labels[i]['fact_ids']})
        selected.sort(key=lambda i: members.index(labels[i]['fact_ids'][0]))
        _require(set(operation) == {'id', 'title', 'category', 'fact_ids', 'aliases'}
            and isinstance(operation['title'], str) and 0 < len(operation['title']) <= 60
            and operation['category'] in s.CATEGORIES
            and members == [fid for i in selected for fid in labels[i]['fact_ids']]
            and operation['aliases'] == s._unique(alias for i in selected for alias in labels[i]['aliases'])
            and operation['id'] == 'operation:' + s._hash({'title': operation['title'], 'category': operation['category'], 'members': sorted(members)})[:20])
        used.update(selected)
    _require(used == set(range(len(labels))))


def reconstruct_catalogue_frontier(snapshot, run, *, read_checkpoint):
    """Pure exact legacy replay; never synthesize missing paid pages or ancestry.

    Return frontier (ordered operations), bindings (operation ID -> parent),
    catalogue_task_hashes (ordered adopted pages), failed_pages (stage_hash,
    labels, parent_bindings), and completed_root (one accepted result or None).
    """
    _require(callable(getattr(read_checkpoint, 'task_rows', None)))
    run, source, addenda = _routing_scope(snapshot, run, read_checkpoint)
    from ._source_material import _template, _legacy_routing
    # Retain the routing-template dependency check without decoding values
    # unused by the prepared legacy branch. Legacy has its own check below.
    routing_present, copied_routing = _legacy_routing(read_checkpoint, snapshot, run)
    prepared = _template(read_checkpoint, 'legacy', snapshot, run)
    if prepared is not None:
        # Only layout is compiled. Re-read physical rows for the current
        # Source-stage states; completed_root is never stored as a decision.
        for key in prepared['catalogue_task_hashes']:
            _require(read_checkpoint(key)['status'] in {'accepted', 'received'})
        for page in prepared['failed_pages']:
            _require(read_checkpoint(page['stage_hash'])['status'] == 'needs_review')
        terminal = prepared.pop('terminal_page')
        prepared['completed_root'] = deepcopy(read_checkpoint(terminal)['data']['result']) if terminal else None
        return prepared
    if routing_present:
        units, routing = copied_routing if copied_routing is not None else _template(read_checkpoint, 'routing', snapshot, run)
    else:
        units, routing = _unprepared_routing(snapshot, run, read_checkpoint, source, addenda)
    memo = getattr(read_checkpoint, '_catalogue_frontiers', None)
    from ._frozen_routing import dependency_stamp
    stamp = dependency_stamp()
    memo_key = s._hash({'routing': [units, routing], 'dependencies': stamp, 'snapshot': snapshot, 'input_hash': run['semantic_input_hash'],
        'run_id': run['run_id'], 'snapshot_id': run['snapshot_id'], 'project_revision': run['project_revision']})
    if memo is not None and stamp is not None and memo_key in memo:
        return deepcopy(memo[memo_key])
    runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
    runtime._snapshot = snapshot
    runtime._units = {unit['_fact_id']: unit for unit in units}
    current = runtime._catalogue_initial_labels(routing)
    profile = runtime._profile()
    rows = [row for row in read_checkpoint.task_rows(run['semantic_input_hash']) if row['stage'] == 'catalogue'
        and row['data']['context'].get('generator_hash') == s._semantic_generator_hash(snapshot)]
    adopted, bindings, failures, used = [], {}, [], set()
    completed = None
    import math
    maximum = math.ceil(math.log2(max(1, len(current)))) + 1
    for level in range(maximum):
        mode = 'leaf' if level == 0 else 'merge'
        pages, page = [], []
        for label in current:
            tentative = page + [label]
            request, _, _ = runtime._catalogue_payload(tentative, mode=mode, legacy=True)
            fits = len(tentative) <= profile['catalogue_items'] and runtime._request_bytes('catalogue', s.CATALOGUE_SYSTEM, request) <= s.semantic_stage_limits(profile, 'catalogue')['input_bytes']
            if not fits:
                _require(page)
                pages.append(page)
                page = [label]
            else:
                page = tentative
        if page:
            pages.append(page)
        next_frontier, full_layer = [], True
        for labels in pages:
            request, _, context = runtime._catalogue_payload(labels, mode=mode, legacy=True)
            context.update(budget_profile=profile, generator_hash=s._semantic_generator_hash(snapshot))
            matches = [r for r in rows if r['data']['request'] == request and r['data']['context'] == context]
            # v1 recovery parent/child share wire; authenticate and retain the child.
            live = [r for r in matches if r['status'] in {'accepted', 'received'}]
            _require(len(live) <= 1)
            if live:
                row = s._checkpoint_record(live[0]['stage_hash'], snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=read_checkpoint)
                _require(row['stage_hash'] not in used)
                _validate_result(row['data']['result'], labels)
                adopted.append(row['stage_hash'])
                used.add(row['stage_hash'])
                result = row['data']['result']
                next_frontier.extend(result)
                for operation in result:
                    bindings[operation['id']] = {'stage_hash': row['stage_hash'], 'result_hash': row['data']['result_hash'], 'operation_id': operation['id']}
            else:
                _require(mode == 'merge')
                full_layer = False
                next_frontier.extend(labels)
                _require(len(matches) <= 1)
                if matches:
                    row = s._checkpoint_record(matches[0]['stage_hash'], snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=read_checkpoint)
                    _require(row['status'] == 'needs_review')
                    failures.append({'stage_hash': row['stage_hash'], 'labels': deepcopy(labels),
                        'parent_bindings': {f'l{i}': deepcopy(bindings[label['id']]) for i, label in enumerate(labels)}})
                    used.add(row['stage_hash'])
        current = next_frontier
        if full_layer and len(pages) == 1:
            completed = deepcopy(current)
            break
        if not full_layer:
            break
    # Every historical page belongs to this precise frontier. The only old
    # terminal leaf exceptions are independently authenticated v1 parents.
    from .semantic_recovery import authenticate_recovery, POLICY
    # Legacy reconstruction is below mixed-policy authentication in the DAG.
    # Authenticate only its leaf children here; the outer inventory validates
    # every receipt, including bounded recovery descendants of supersessions.
    recoveries = [authenticate_recovery(row, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'],
        read_checkpoint=read_checkpoint) for row in rows
        if row['data']['request'].get('mode') == 'leaf' and 'recovery_id' in row['data']]
    _require(all(receipt['policy'] == POLICY for receipt in recoveries))
    recovery_parents = {receipt['parent_stage_hash'] for receipt in recoveries}
    _require(all(row['stage_hash'] in used or row['stage_hash'] in recovery_parents
        for row in rows if 'merge_contract' not in row['data']['request']))
    facts = [fid for operation in current for fid in operation['fact_ids']]
    _require(len(facts) == len(set(facts)) and set(facts) == set(runtime._units))
    result = {'frontier': current, 'bindings': {op['id']: bindings[op['id']] for op in current},
        'catalogue_task_hashes': adopted, 'failed_pages': failures, 'completed_root': completed}
    if memo is not None and stamp is not None and stamp == dependency_stamp():
        memo[memo_key] = deepcopy(result)
    return deepcopy(result)


def catalogue_supersession_plan(snapshot, run, parent_stage_hash, *, read_checkpoint):
    _require(all(callable(getattr(read_checkpoint, name, None)) for name in ('raw_digest', 'task_rows', 'recovery_receipts', 'supersession_receipts', 'catalogue_source')))
    input_hash = run['semantic_input_hash']
    parent = s._checkpoint_record(parent_stage_hash, snapshot=snapshot, expected_input_hash=input_hash, read_checkpoint=read_checkpoint)
    data = parent['data']
    _require(s._structured_output(snapshot) and parent['stage'] == 'catalogue' and type(parent['attempt']) is int
        and parent['attempt'] == 1 and parent['status'] == 'needs_review' and data.get('failure_code') == 'model_call'
        and data['result'] is None and data['request'].get('mode') == 'merge' and 'merge_contract' not in data['request']
        and not {'recovery_id', 'logical_task_hash', 'supersession_id'}.intersection(data))
    frontier = reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=read_checkpoint)
    pages = [page for page in frontier['failed_pages'] if page['stage_hash'] == parent_stage_hash]
    _require(len(pages) == 1 and len(frontier['failed_pages']) == 1)
    page = pages[0]
    runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
    runtime._snapshot = snapshot
    runtime._catalogue_origins = {label['id']: page['parent_bindings'][f'l{i}'] for i, label in enumerate(page['labels'])}
    request, _, context = runtime._catalogue_payload(page['labels'], mode='merge')
    context.update(budget_profile=deepcopy(data['context']['budget_profile']), generator_hash=s._semantic_generator_hash(snapshot))
    child = s.semantic_task_identity_hash(snapshot, input_hash, 'catalogue', request, context)
    _require(snapshot['project']['revision'] == run['project_revision'])
    plan = {'policy': POLICY, 'parent_stage_hash': parent_stage_hash, 'parent_failure_digest': read_checkpoint.raw_digest(parent_stage_hash),
        'child_stage_hash': child, 'input_hash': input_hash, 'generator_hash': s._semantic_generator_hash(snapshot),
        'run_id': run['run_id'], 'snapshot_id': run['snapshot_id'], 'project_id': snapshot['project']['id'],
        'project_revision': run['project_revision'], 'vault_identity': s._hash(snapshot.get('vault_path')),
        'destination_digest': s._hash(s.provider_destination(snapshot['provider'])),
        'old_contract': s.CATALOGUE_POLICY, 'new_contract': s.BOUNDED_MERGE_CONTRACT,
        'old_system_digest': s._hash(s.semantic_stage_system(snapshot, 'catalogue', data['request'])),
        'new_system_digest': s._hash(s.semantic_stage_system(snapshot, 'catalogue', request)),
        'old_wire_digest': s._hash(s._json(data['request'])), 'new_wire_digest': s._hash(s._json(request)),
        'schema_digest': s._hash(s.semantic_output_schema('catalogue', request)),
        'budget_profile_digest': s._hash(context['budget_profile']), 'label_members_digest': s._hash(context['label_members']),
        'parent_bindings_digest': s._hash(context['catalogue_parents']), 'new_request': request, 'new_context': context}
    _require(isinstance(plan['parent_failure_digest'], str) and s.HEX.fullmatch(plan['parent_failure_digest']))
    plan['supersession_id'] = s._hash(plan)
    plan['plan_hash'] = s._hash(plan)
    _require(set(plan) == PLAN_FIELDS)
    return plan


def authenticate_supersession(row, *, snapshot, expected_input_hash, read_checkpoint):
    _require(callable(getattr(read_checkpoint, 'supersession_receipts', None)))
    matches = [receipt for receipt in read_checkpoint.supersession_receipts(expected_input_hash) if receipt['child_stage_hash'] == row['stage_hash']]
    _require(len(matches) == 1)
    receipt = matches[0]
    _require(set(receipt) == RECEIPT_FIELDS | {'consumed_at', 'dispatch_id'})
    run = {key: receipt[key] for key in ('run_id', 'snapshot_id', 'project_revision')}
    run['semantic_input_hash'] = expected_input_hash
    plan = catalogue_supersession_plan(snapshot, run, receipt['parent_stage_hash'], read_checkpoint=read_checkpoint)
    _require({key: receipt[key] for key in PLAN_FIELDS} == plan
        and receipt['operator_id'] == 'root' and receipt['authorization_source'] == 'user-fix-and-continue'
        and type(receipt['max_uses']) is int and receipt['max_uses'] == 1
        and type(receipt['authorized_at']) is int and type(receipt['expires_at']) is int
        and receipt['expires_at'] - receipt['authorized_at'] == 3600
        and all(isinstance(receipt[key], str) and s.HEX.fullmatch(receipt[key]) for key in ('authorization_ref', 'reconciliation_hash'))
        and isinstance(receipt['reconciliation_ref'], str) and Path(receipt['reconciliation_ref']).is_absolute())
    data = row['data']
    _require(row['stage'] == 'catalogue' and type(row['attempt']) is int and row['attempt'] == 1
        and row['input_hash'] == expected_input_hash and data.get('supersession_id') == receipt['supersession_id']
        and not {'recovery_id', 'logical_task_hash'}.intersection(data)
        and data['request'] == plan['new_request'] and data['context'] == plan['new_context'])
    if row['status'] == 'planned':
        _require(receipt['consumed_at'] is None and receipt['dispatch_id'] is None)
    else:
        _require(type(receipt['consumed_at']) is int and receipt['authorized_at'] <= receipt['consumed_at'] < receipt['expires_at']
            and isinstance(receipt['dispatch_id'], str) and s.HEX.fullmatch(receipt['dispatch_id']))
    return receipt


def supersession_inventory(rows, *, snapshot, expected_input_hash, read_checkpoint):
    receipts = read_checkpoint.supersession_receipts(expected_input_hash) if callable(getattr(read_checkpoint, 'supersession_receipts', None)) else []
    children = [row for row in rows if 'supersession_id' in row['data']]
    _require(len(receipts) == len(children))
    verified = [authenticate_supersession(row, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint) for row in children]
    keys = {row['stage_hash'] for row in rows}
    _require(len({r['parent_stage_hash'] for r in verified}) == len(verified)
        and all(r['parent_stage_hash'] in keys for r in verified))
    return verified


def log_event(status, receipt):
    import logging
    logging.getLogger(__name__).info('semantic_supersession_' + status, extra={
        'event': 'semantic_supersession_' + status, 'status': status,
        **{key: receipt[key] for key in ('run_id', 'input_hash', 'parent_stage_hash', 'supersession_id', 'child_stage_hash', 'dispatch_id')}})


def authorize_catalogue_supersession(service, snapshot, run, plan, *, authorization):
    from .semantic_recovery import _current_context, _database_controls
    _require(set(plan) == PLAN_FIELDS)
    auth = _check_grant(authorization, plan)
    created = False
    with service._exclusive(snapshot['project']['id']):
        service.project_analysis.guard(snapshot)
        controls = service._controls(run)
        _require(controls.get('paused') is True and not controls.get('cancelled'))
        with service.store._lock, service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            service.project_analysis.guard(snapshot)
            _current_context(db, snapshot, run)
            _database_controls(db, run, paused=True)
            auth = _check_grant(authorization, plan)
            reader = s.semantic_checkpoint_reader(db)
            _require(catalogue_supersession_plan(snapshot, run, plan['parent_stage_hash'], read_checkpoint=reader) == plan)
            existing = reader.supersession_receipts(plan['input_hash'])
            found = [receipt for receipt in existing if receipt['parent_stage_hash'] == plan['parent_stage_hash']]
            _require(len(found) <= 1)
            if found:
                receipt = found[0]
                _require({key: receipt[key] for key in PLAN_FIELDS} == plan and receipt['authorization_ref'] == auth['authorization_ref'])
                authenticate_supersession(reader(plan['child_stage_hash']), snapshot=snapshot, expected_input_hash=plan['input_hash'], read_checkpoint=reader)
            else:
                _require(reader(plan['child_stage_hash']) is None)
                db.execute('''CREATE TABLE IF NOT EXISTS business_semantic_supersessions (
                    supersession_id TEXT PRIMARY KEY, parent_stage_hash TEXT NOT NULL UNIQUE,
                    child_stage_hash TEXT NOT NULL UNIQUE, input_hash TEXT NOT NULL, data TEXT NOT NULL,
                    consumed_at INTEGER, dispatch_id TEXT,
                    CHECK ((consumed_at IS NULL) = (dispatch_id IS NULL)))''')
                receipt = {**plan, **auth}
                data = {'request': deepcopy(plan['new_request']), 'context': deepcopy(plan['new_context']),
                    'request_hash': plan['child_stage_hash'], 'result': None, 'result_hash': s._hash(None),
                    'supersession_id': plan['supersession_id']}
                db.execute('INSERT INTO business_semantic_supersessions VALUES (?,?,?,?,?,NULL,NULL)',
                    (plan['supersession_id'], plan['parent_stage_hash'], plan['child_stage_hash'], plan['input_hash'], s._json(receipt)))
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
                    (plan['child_stage_hash'], 'catalogue', plan['input_hash'], 'planned', 1, s._json(data), None, s.now()))
                receipt = {**receipt, 'consumed_at': None, 'dispatch_id': None}
                created = True
    if created:
        log_event('authorized', receipt)
    return receipt


def consume_supersession(db, snapshot, run, child_stage_hash):
    """Caller owns BEGIN IMMEDIATE and emits success only after committing."""
    import uuid
    from .semantic_recovery import _require_project_lock, _current_context, _database_controls
    _require(db.in_transaction)
    _require_project_lock(snapshot['project']['id'])
    _current_context(db, snapshot, run)
    _database_controls(db, run, paused=False)
    reader = s.semantic_checkpoint_reader(db)
    row = reader(child_stage_hash)
    receipt = authenticate_supersession(row, snapshot=snapshot, expected_input_hash=run['semantic_input_hash'], read_checkpoint=reader)
    _require(all(receipt[key] == run[key] for key in ('run_id', 'snapshot_id', 'project_revision')))
    _check_reconciliation(receipt)
    stamp = int(time.time())
    _require(row['status'] == 'planned' and receipt['authorized_at'] <= stamp < receipt['expires_at'])
    dispatch = uuid.uuid4().hex + uuid.uuid4().hex
    _require(db.execute('UPDATE business_semantic_supersessions SET consumed_at=?,dispatch_id=? WHERE supersession_id=? AND consumed_at IS NULL',
        (stamp, dispatch, receipt['supersession_id'])).rowcount == 1)
    _require(db.execute("UPDATE business_semantic_tasks SET status='running' WHERE stage_hash=? AND status='planned'", (child_stage_hash,)).rowcount == 1)
    return {**receipt, 'consumed_at': stamp, 'dispatch_id': dispatch}
