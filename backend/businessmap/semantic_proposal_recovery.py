"""Private, explicitly selected ordinary proposal timeout recovery."""
from . import business_semantics as s

POLICY = 'operator-normal-proposal-timeout-once-v1'


def _eligible(row, snapshot):
    data = row['data']
    context = data['context']
    diagnostic = data.get('diagnostic', {})
    s._require(s._structured_output(snapshot) and row['stage'] == 'proposal'
        and type(row['attempt']) is int and row['attempt'] == 1
        and row['status'] == 'needs_review' and data['request'].get('stage') == 'proposal'
        and context.get('revision') == s._revision_context()
        and data.get('failure_code') == 'model_call' and data['result'] is None
        and data['result_hash'] == s._hash(None)
        and type(diagnostic.get('status_code')) is int and diagnostic['status_code'] == 408
        and diagnostic.get('codex_reason') == 'timeout'
        and not {'recovery_id', 'logical_task_hash', 'supersession_id', 'feedback'}.intersection(data)
        and not {'whole_operation', 'atomic_budget', 'partition_revision'}.intersection(context)
        and not {'whole_operation', 'atomic_budget', 'partition_revision'}.intersection(data['request']), 'cache_invalid')


def _without_packet(reader, input_hash, packet_hash, *, source_only=False):
    """Read-only view used only to reconstruct the selected packet's source forest.

    The outer admission path must separately authenticate every physical row.
    The source-only view omits all proposal/review rows to break the inventory
    recursion and avoid coupling source reconstruction to concurrent statuses.
    """
    from ._frozen_routing import FrozenReader
    if type(reader) is FrozenReader:
        return FrozenReader._without_packet(reader, input_hash, packet_hash, source_only=source_only)
    rows = reader.task_rows(input_hash)
    hidden = {r['stage_hash'] for r in rows if r['stage'] in {'proposal', 'review'}
              and (source_only or r['data']['context'].get('packet_hash') == packet_hash)}
    def read(key):
        return reader(key)
    for name in ('catalogue_source', 'catalogue_runs', 'raw_digest', 'raw_checkpoint_row', 'raw_error_code', 'supersession_receipts'):
        setattr(read, name, getattr(reader, name))
    read.task_rows = lambda identity: [r for r in reader.task_rows(identity) if r['stage_hash'] not in hidden]
    read.recovery_receipts = lambda identity: [r for r in reader.recovery_receipts(identity) if r['child_stage_hash'] not in hidden]
    return read


def _source_material(source):
    """Bind SOURCE and stable run inputs, excluding mutable controller progress."""
    run = source['run']
    stable = {key: run.get(key) for key in ('run_id', 'project_id', 'snapshot_id', 'project_revision',
        'source_repositories', 'catalogue_override_revision')}
    stable['module_ids'] = [module['id'] for module in run['modules']]
    return {**source, 'run': stable}


def _packet_binding(snapshot, run, parent, reader, *, atomic_budget_chains, whole_histories, partition_repair_pairs):
    from ._source_material import _packet_template
    data, context = parent['data'], parent['data']['context']
    groups = (atomic_budget_chains, whole_histories, partition_repair_pairs)
    prepared = _packet_template(reader, snapshot, run, data['request'], context, groups=groups)
    if prepared is None:
        source, forest, candidates, raw_dependencies = _packet_templates(snapshot, run, reader,
            budget_profile=context['budget_profile'], atomic_budget_chains=atomic_budget_chains,
            whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)
    else:
        from . import semantic_catalogue_frontier as frontier
        scoped = _without_packet(reader, run['semantic_input_hash'], '', source_only=True)
        # Current physical inventory/receipt and source-phase checks are never
        # supplied by a template. Only expensive wire/layout construction is.
        forest = frontier._packet_frontier(snapshot, run['semantic_input_hash'], read_checkpoint=scoped,
            atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories,
            partition_repair_pairs=partition_repair_pairs)
        s._require(not forest['received_to_accept'], 'cache_invalid')
        source = reader.catalogue_source(run['run_id'])
        packet, raw_dependencies = prepared
        return source, forest, packet, raw_dependencies
    selected = [entry['payload'] for entry in candidates
        if entry['payload'] == data['request'] and entry['binding'] == context]
    s._require(len(selected) == 1, 'cache_invalid')
    return source, forest, selected[0], raw_dependencies


def _packet_templates(snapshot, run, reader, *, budget_profile, atomic_budget_chains, whole_histories, partition_repair_pairs):
    from copy import deepcopy
    from . import semantic_catalogue_frontier as frontier
    from . import semantic_supersession as sup
    input_hash = run['semantic_input_hash']
    scoped = _without_packet(reader, input_hash, '', source_only=True)
    forest = frontier.catalogue_frontier_plan(snapshot, input_hash, read_checkpoint=scoped,
        atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)
    s._require(not forest['received_to_accept'], 'cache_invalid')
    persisted, source_units, _ = frontier._source(snapshot, input_hash, scoped)
    s._require(all(persisted.get(key) == run.get(key) for key in ('run_id', 'snapshot_id', 'project_revision')), 'cache_invalid')
    _, source, addenda = sup._source_inputs(snapshot, persisted, scoped)
    units = frontier._inputs(source_units)
    runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
    runtime._snapshot, runtime._units = snapshot, units
    runtime._matching = [m for a in addenda for m in a.get('matching_receipts', [])]
    runtime._partitions = []
    runtime._profile = lambda: deepcopy(budget_profile)
    runtime._forbidden = s._unique(token for unit in units.values() for token in unit.get('_forbidden', ()) if isinstance(token, str) and token)
    all_rows = scoped.task_rows(input_hash)
    reviews = [r for r in all_rows if r['stage'] == 'partition_review' and r['status'] == 'accepted']
    from .semantic_atomic_budget import authenticate_history as authenticate_atomic_history
    from .semantic_whole_operation import whole_operation_plans
    atomic_roots = [authenticate_atomic_history(snapshot, input_hash, read_checkpoint=scoped, keys=tuple(chain))['parent_operation']
                    for chain in atomic_budget_chains]
    whole_plans = whole_operation_plans(snapshot, input_hash, read_checkpoint=scoped,
        selected_history_pairs=whole_histories, atomic_budget_chains=atomic_budget_chains, partition_repair_pairs=partition_repair_pairs)
    special_roots = atomic_roots + [plan['parent_operation'] for plan in whole_plans]
    expanded = []
    for operation in forest['operations']:
        matches = [r for r in reviews if r['data']['context'].get('parent_operation') == operation]
        s._require(len(matches) <= 1, 'cache_invalid')
        if not matches:
            # A separate oversized whole/atomic root always forms a boundary.
            if runtime._packet_fits([operation]):
                expanded.append(operation)
            else:
                s._require(operation in special_roots, 'cache_invalid')
                expanded.append(None)
            continue
        review = matches[0]
        revision = review['data']['context'].get('partition_revision')
        if revision is not None:
            from .semantic_partition_repair import partition_repair_plan
            pair = (revision.get('parent_subdivision_hash'), revision.get('parent_review_hash'))
            s._require(pair in set(map(tuple, partition_repair_pairs)), 'cache_invalid')
            repair = partition_repair_plan(snapshot, input_hash, read_checkpoint=scoped,
                parent_subdivision_hash=pair[0], parent_review_hash=pair[1])
            s._require(repair['status'] == 'accepted' and repair['review_hash'] == review['stage_hash']
                and repair['parent_operation'] == operation and repair['partition_revision'] == revision, 'cache_invalid')
            sub = scoped(repair['subdivision_hash'])
            children = sub['data']['result']
            runtime._partitions.append({'parent': deepcopy(operation), 'children': deepcopy(children),
                'partition_hash': review['data']['context']['partition_hash'], 'subdivision_hash': repair['subdivision_hash'],
                'review_hash': review['stage_hash'], 'repair': {**deepcopy(revision),
                'subdivision_hash': repair['subdivision_hash'], 'review_hash': review['stage_hash']}})
            expanded.extend(children)
            continue
        s._checkpoint_record(review['stage_hash'], snapshot=snapshot, expected_input_hash=input_hash, read_checkpoint=scoped)
        sub = s._checkpoint_record(review['data']['context']['subdivision_hash'], snapshot=snapshot, expected_input_hash=input_hash, read_checkpoint=scoped)
        s._require(sub['stage'] == 'subdivide' and sub['status'] == 'accepted' and not runtime._packet_fits([operation]), 'cache_invalid')
        labels = [{'title': operation['title'], 'category': operation['category'], 'fact_ids': [fid], 'aliases': operation['aliases']} for fid in operation['fact_ids']]
        expected_request, _, expected_context = runtime._catalogue_payload(labels, stage='subdivide')
        s._require(sub['data']['request'] == expected_request and sub['data']['context'] == {**expected_context,
            'budget_profile': budget_profile, 'generator_hash': s._semantic_generator_hash(snapshot)}, 'cache_invalid')
        children = sub['data']['result']
        sup._validate_result(children, labels)
        s._require(len(children) > 1 and all(runtime._packet_fits([child]) for child in children), 'cache_invalid')
        for child in children:
            s._public(child['title'], runtime._forbidden)
        def review_call(stage, system, payload, normalize, *, context):
            s._require(stage == 'partition_review' and review['data']['request'] == payload
                and review['data']['context'] == {**context, 'budget_profile': runtime._profile(),
                    'generator_hash': s._semantic_generator_hash(snapshot)}
                and normalize(review['data']['result']) == review['data']['result'], 'cache_invalid')
            return review['data']['result'], review['stage_hash']
        runtime._call = review_call
        runtime._verify_partition(operation, children, sub['stage_hash'])
        expanded.extend(children)
    s._require(len(runtime._partitions) == len(reviews), 'cache_invalid')
    packets, packet = [], []
    for operation in expanded:
        if operation is None:
            if packet:
                packets.append(packet)
                packet = []
            continue
        if packet and not runtime._packet_fits(packet + [operation]):
            packets.append(packet)
            packet = []
        packet.append(operation)
    if packet:
        packets.append(packet)
    candidates = []
    for packet in packets:
        payload, refs, ops = runtime._packet(packet)
        binding = {'packet_hash': s._hash(payload), 'input_ids': {k: v['_fact_id'] for k, v in refs.items()},
                   'operation_ids': {k: v['id'] for k, v in ops.items()}, 'partition_review_hashes': payload['partition_review_hashes'],
                   'budget_profile': budget_profile, 'generator_hash': s._semantic_generator_hash(snapshot),
                   'revision': s._revision_context()}
        candidates.append({'payload': payload, 'binding': binding})
    source_keys = set(forest["page_hashes"])
    for partition in runtime._partitions:
        source_keys.update((partition["subdivision_hash"], partition["review_hash"]))
    for group in (atomic_budget_chains, whole_histories, partition_repair_pairs):
        for history in group:
            source_keys.update(history)
    raw_dependencies = [[key, reader.raw_digest(key)] for key in sorted(source_keys)]
    s._require(all(isinstance(digest, str) and s.HEX.fullmatch(digest) for _, digest in raw_dependencies), "cache_invalid")
    return source, forest, candidates, raw_dependencies


def _plan(snapshot, run, parent_stage_hash, *, read_checkpoint, baseline=None, atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=()):
    try:
        s._require(all(callable(getattr(read_checkpoint, name, None)) for name in (
            'raw_digest', 'raw_checkpoint_row', 'raw_error_code', 'task_rows', 'recovery_receipts', 'supersession_receipts')), 'cache_invalid')
        identity = run['semantic_input_hash']
        parent = s._checkpoint_record(parent_stage_hash, snapshot=snapshot, expected_input_hash=identity, read_checkpoint=read_checkpoint)
        _eligible(parent, snapshot)
        s._require(read_checkpoint.raw_error_code(parent_stage_hash) == 'model_call', 'cache_invalid')
        rows = read_checkpoint.task_rows(identity)
        siblings = [r for r in rows if r['stage'] in {'proposal', 'review'} and r['data']['context'].get('packet_hash') == parent['data']['context']['packet_hash']]
        s._require(sum(r['stage_hash'] == parent_stage_hash for r in siblings) == 1
            and not any(r['parent_stage_hash'] == parent_stage_hash for r in read_checkpoint.supersession_receipts(identity)), 'cache_invalid')
        if baseline is None:
            s._require(len(siblings) == 1 and not any(r['parent_stage_hash'] == parent_stage_hash
                for r in read_checkpoint.recovery_receipts(identity)), 'cache_invalid')
        groups = (atomic_budget_chains, whole_histories, partition_repair_pairs)
        for group, length in zip(groups, (4, 2, 2)):
            s._require(isinstance(group, (tuple, list)) and all(isinstance(item, (tuple, list)) and len(item) == length
                and all(isinstance(k, str) and s.HEX.fullmatch(k) for k in item) and len(set(item)) == length for item in group)
                and len({tuple(item) for item in group}) == len(group) and list(map(tuple, group)) == sorted(map(tuple, group)), 'cache_invalid')
        atomic_budget_chains, whole_histories, partition_repair_pairs = (tuple(map(tuple, group)) for group in groups)
        if baseline is None:
            scoped_inventory = _without_packet(read_checkpoint, identity, parent['data']['context']['packet_hash'])
            materialized = s.SemanticCheckpointRows(scoped_inventory.task_rows(identity))
            materialized.read_checkpoint = scoped_inventory
            s.semantic_task_inventory(materialized, snapshot=snapshot, expected_input_hash=identity,
                atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)
        source, forest, packet, raw_dependencies = _packet_binding(snapshot, run, parent, read_checkpoint,
            atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)
        data = parent['data']
        source_material = _source_material(source)
        plan = {'policy': POLICY, 'parent_stage_hash': parent_stage_hash, 'logical_task_hash': parent_stage_hash,
            'parent_failure_digest': read_checkpoint.raw_digest(parent_stage_hash), 'input_hash': identity,
            'run_id': run['run_id'], 'snapshot_id': run['snapshot_id'], 'project_id': snapshot['project']['id'],
            'project_revision': run['project_revision'], 'vault_identity': s._hash(snapshot.get('vault_path')),
            'generator_hash': s._semantic_generator_hash(snapshot), 'destination_digest': s._hash(s.provider_destination(snapshot['provider'])),
            'system_digest': s._hash(s.semantic_stage_system(snapshot, 'proposal', packet)),
            'wire_digest': s._hash(s._json(packet)), 'schema_digest': s._hash(s.semantic_output_schema('proposal', packet)),
            'budget_profile_digest': s._hash(data['context']['budget_profile']), 'packet_hash': data['context']['packet_hash'],
            'source_packet_digest': s._hash({'source': source_material, 'packet': packet}),
            'source_dependency_digest': s._hash({'source': source_material, 'frontier': forest['terminal_candidate'], 'raw_dependencies': raw_dependencies}),
            'inventory_digest': (baseline if baseline is not None else s._hash(sorted([list(read_checkpoint.raw_checkpoint_row(r['stage_hash'])) for r in rows], key=lambda r:r[0]))),
            'logical_draft_maximum': 2, 'physical_maximum_calls': 5, 'replacement_allowance': 1,
            'atomic_budget_chains': [list(x) for x in atomic_budget_chains], 'whole_histories': [list(x) for x in whole_histories],
            'partition_repair_pairs': [list(x) for x in partition_repair_pairs]}
        s._require(plan['parent_failure_digest'] is not None and snapshot['project']['revision'] == run['project_revision'], 'cache_invalid')
        identity_part = {k: v for k,v in plan.items() if k != 'inventory_digest'}
        plan['recovery_id'] = s._hash(identity_part)
        plan['child_stage_hash'] = s._hash({'policy': POLICY, 'recovery_id': plan['recovery_id'], 'parent_stage_hash': parent_stage_hash})
        plan['plan_hash'] = s._hash(plan)
        return plan
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def proposal_timeout_recovery_plan(snapshot, run, parent_stage_hash, *, read_checkpoint, atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=()):
    """Read-only initial plan; an existing replacement is never reauthorized."""
    return _plan(snapshot, run, parent_stage_hash, read_checkpoint=read_checkpoint, atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)


PLAN_FIELDS = frozenset(('policy parent_stage_hash logical_task_hash parent_failure_digest input_hash run_id snapshot_id '
    'project_id project_revision vault_identity generator_hash destination_digest system_digest wire_digest schema_digest '
    'budget_profile_digest packet_hash source_packet_digest source_dependency_digest inventory_digest logical_draft_maximum '
    'physical_maximum_calls replacement_allowance atomic_budget_chains whole_histories partition_repair_pairs recovery_id child_stage_hash plan_hash').split())
EVIDENCE_FIELDS = frozenset(('checked_at local_owner_pids local_processes_drained upstream_result_or_billing_reconciled '
    'run_id parent_stage_hash parent_row_digest inventory_digest').split())
from .semantic_recovery import AUTH_FIELDS
RECEIPT_FIELDS = PLAN_FIELDS | AUTH_FIELDS | {'authorization_ref', 'reconciliation_evidence'}



def _private_evidence_bytes(path):
    import os
    import stat
    from pathlib import Path
    try:
        s._require(isinstance(path, (str, Path)) and Path(path).is_absolute(), 'cache_invalid')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            s._require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, 'cache_invalid')
            raw = stream.read(16385)
        s._require(len(raw) <= 16384, 'cache_invalid')
        return raw
    except (OSError, TypeError, ValueError):
        raise s.SemanticValidationError('cache_invalid') from None

def _check_grant(authorization, plan):
    import hashlib
    import os
    import time
    from . import semantic_recovery as r
    try:
        raw = _private_evidence_bytes(authorization)
        auth = r._evidence_json(raw)
        s._require(set(auth) == r.AUTH_FIELDS and all(auth[k] == plan[k] for k in
            ('policy', 'plan_hash', 'parent_stage_hash', 'run_id')) and auth['operator_id'] == 'root'
            and auth['authorization_source'] == 'user-fix-and-continue' and type(auth['max_uses']) is int and auth['max_uses'] == 1
            and type(auth['authorized_at']) is int and type(auth['expires_at']) is int
            and auth['expires_at'] - auth['authorized_at'] == 3600
            and auth['authorized_at'] <= time.time() < auth['expires_at'], 'cache_invalid')
        evidence_raw = _private_evidence_bytes(auth['reconciliation_ref'])
        s._require(len(evidence_raw) <= 16384, 'cache_invalid')
        evidence_text = evidence_raw.decode('utf-8')
        evidence = r._evidence_json(evidence_raw)
        s._require(hashlib.sha256(evidence_raw).hexdigest() == auth['reconciliation_hash'] and set(evidence) == EVIDENCE_FIELDS
            and type(evidence['checked_at']) is int and 0 <= time.time() - evidence['checked_at'] <= 60
            and 0 <= auth['authorized_at'] - evidence['checked_at'] <= 60
            and evidence['local_processes_drained'] is True and evidence['upstream_result_or_billing_reconciled'] is False
            and evidence['run_id'] == plan['run_id'] and evidence['parent_stage_hash'] == plan['parent_stage_hash']
            and evidence['parent_row_digest'] == plan['parent_failure_digest'] and evidence['inventory_digest'] == plan['inventory_digest'], 'cache_invalid')
        pids = evidence['local_owner_pids']
        s._require(isinstance(pids, list) and pids and all(type(p) is int and p > 0 for p in pids) and len(set(pids)) == len(pids), 'cache_invalid')
        for pid in pids:
            try: os.kill(pid, 0)
            except ProcessLookupError: pass
            else: s._require(False, 'cache_invalid')
        return {**auth, 'authorization_ref': hashlib.sha256(raw).hexdigest(), 'reconciliation_evidence': evidence_text}
    except s.SemanticValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def authenticate_proposal_timeout_recovery(row, *, snapshot, expected_input_hash, read_checkpoint):
    from . import semantic_recovery as r
    try:
        matches = [item for item in read_checkpoint.recovery_receipts(expected_input_hash) if item['child_stage_hash'] == row['stage_hash']]
        s._require(len(matches) == 1, 'cache_invalid')
        receipt = matches[0]
        s._require(set(receipt) == RECEIPT_FIELDS | {'consumed_at', 'dispatch_id'}, 'cache_invalid')
        run = {k: receipt[k] for k in ('run_id', 'snapshot_id', 'project_revision')}
        run['semantic_input_hash'] = expected_input_hash
        plan = _plan(snapshot, run, receipt['parent_stage_hash'], read_checkpoint=read_checkpoint, baseline=receipt['inventory_digest'],
            atomic_budget_chains=receipt['atomic_budget_chains'], whole_histories=receipt['whole_histories'], partition_repair_pairs=receipt['partition_repair_pairs'])
        s._require({k: receipt[k] for k in PLAN_FIELDS} == plan and receipt['policy'] == POLICY
            and receipt['operator_id'] == 'root' and receipt['authorization_source'] == 'user-fix-and-continue'
            and type(receipt['max_uses']) is int and receipt['max_uses'] == 1
            and type(receipt['authorized_at']) is int and type(receipt['expires_at']) is int
            and receipt['expires_at'] - receipt['authorized_at'] == 3600
            and all(isinstance(receipt[k], str) and s.HEX.fullmatch(receipt[k]) for k in ('authorization_ref', 'reconciliation_hash', 'inventory_digest')), 'cache_invalid')
        _recorded_evidence(receipt)
        parent = read_checkpoint(receipt['parent_stage_hash'])
        data = row['data']
        s._require(row['stage'] == 'proposal' and type(row['attempt']) is int and row['attempt'] == 1 and row['input_hash'] == expected_input_hash
            and row['status'] in {'planned', 'running', 'received', 'proposed', 'needs_review'}
            and data.get('recovery_id') == receipt['recovery_id'] and data.get('logical_task_hash') == receipt['logical_task_hash']
            and 'supersession_id' not in data and data['request'] == parent['data']['request'] and data['context'] == parent['data']['context'], 'cache_invalid')
        if row['status'] == 'planned':
            s._require(receipt['consumed_at'] is None and receipt['dispatch_id'] is None, 'cache_invalid')
        else:
            s._require(type(receipt['consumed_at']) is int and receipt['authorized_at'] <= receipt['consumed_at'] < receipt['expires_at']
                and isinstance(receipt['dispatch_id'], str) and s.HEX.fullmatch(receipt['dispatch_id']), 'cache_invalid')
        return receipt
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None



def _prepare_authorization(service, snapshot, run, plan, *, source_material):
    """Validate complete current semantics outside the writer and seal one attempt."""
    from ._prepared_mutation import _capture_dynamic, _scope, _mutation_stamp, _PreparedMutation, _SEAL
    from ._source_material import bind_source_material
    s._require(set(plan) == PLAN_FIELDS and plan['policy'] == POLICY, 'cache_invalid')
    service.project_analysis.guard(snapshot)
    with service.store._db() as db:
        from .semantic_recovery import _current_context
        db.execute('BEGIN')
        _current_context(db, snapshot, run)
        records = s.semantic_checkpoint_rows(db, plan['input_hash'])
        dynamic = _capture_dynamic(db, plan['input_hash'])
    records.read_checkpoint = bind_source_material(source_material, snapshot, run, read_checkpoint=records.read_checkpoint)
    reader = records.read_checkpoint
    found = [item for item in reader.recovery_receipts(plan['input_hash']) if item['parent_stage_hash'] == plan['parent_stage_hash']]
    if found:
        s._require(len(found) == 1 and {key:found[0][key] for key in PLAN_FIELDS} == plan, 'cache_invalid')
        authenticate_proposal_timeout_recovery(reader(plan['child_stage_hash']), snapshot=snapshot,
            expected_input_hash=plan['input_hash'], read_checkpoint=reader)
        s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=plan['input_hash'],
            atomic_budget_chains=tuple(map(tuple,plan['atomic_budget_chains'])),
            whole_histories=tuple(map(tuple,plan['whole_histories'])),
            partition_repair_pairs=tuple(map(tuple,plan['partition_repair_pairs'])))
    else:
        s._require(proposal_timeout_recovery_plan(snapshot,run,plan['parent_stage_hash'],read_checkpoint=reader,
            atomic_budget_chains=tuple(tuple(item) for item in plan['atomic_budget_chains']), whole_histories=tuple(tuple(item) for item in plan['whole_histories']),
            partition_repair_pairs=tuple(tuple(item) for item in plan['partition_repair_pairs'])) == plan, 'cache_invalid')
    return _PreparedMutation('authorize_proposal_recovery',_scope(snapshot,run),
        (plan['parent_stage_hash'],plan['plan_hash']),dynamic,_mutation_stamp(),source_material,_seal=_SEAL)

def authorize_proposal_timeout_recovery(service, snapshot, run, plan, *, authorization,
                                        source_material=None, prepared_mutation=None):
    """Authorize once after lock-free Source and dynamic preparation."""
    from copy import deepcopy
    import json
    from . import semantic_recovery as r
    from ._prepared_mutation import _consume_prepared, _assert_authorization_delta, _fresh_packet_occupied, _AuthorizationPeerDrift
    from ._source_material import prepare_source_material
    s._require(set(plan) == PLAN_FIELDS and plan['policy'] == POLICY, 'cache_invalid')
    if source_material is None:
        with service.store._db() as db:
            records = s.semantic_checkpoint_rows(db, plan['input_hash'])
        source_material = prepare_source_material(snapshot, run, read_checkpoint=records.read_checkpoint,
            atomic_budget_chains=tuple(tuple(item) for item in plan['atomic_budget_chains']), whole_histories=tuple(tuple(item) for item in plan['whole_histories']),
            partition_repair_pairs=tuple(tuple(item) for item in plan['partition_repair_pairs']))
    if prepared_mutation is None:
        prepared_mutation = _prepare_authorization(service, snapshot, run, plan, source_material=source_material)
    created = False
    with service._exclusive(snapshot['project']['id']):
        service.project_analysis.guard(snapshot)
        controls = service._controls(run)
        s._require(controls.get('paused') is True and controls.get('cancelled', False) is False, 'cache_invalid')
        auth = _check_grant(authorization, plan)
        with service.store._lock, service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                _consume_prepared(prepared_mutation, operation='authorize_proposal_recovery', snapshot=snapshot, run=run,
                    target=(plan['parent_stage_hash'],plan['plan_hash']), material=source_material, db=db)
            except _AuthorizationPeerDrift:
                # Only an identical already-committed authorization is idempotent.
                task_row=db.execute('SELECT * FROM business_semantic_tasks WHERE stage_hash=?',(plan['child_stage_hash'],)).fetchone()
                receipt_row=db.execute('SELECT * FROM business_semantic_recoveries WHERE child_stage_hash=?',(plan['child_stage_hash'],)).fetchone()
                parent_row=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(plan['parent_stage_hash'],)).fetchone()
                s._require(task_row is not None and receipt_row is not None and parent_row is not None, 'cache_invalid')
                parent=json.loads(parent_row[0])
                expected_data={'request':parent['request'],'context':parent['context'],
                    'request_hash':plan['child_stage_hash'],'result':None,'result_hash':s._hash(None),
                    'recovery_id':plan['recovery_id'],'logical_task_hash':plan['logical_task_hash']}
                s._require(task_row[:7]==(plan['child_stage_hash'],'proposal',plan['input_hash'],'planned',1,s._json(expected_data),None)
                    and isinstance(task_row[7],str)
                    and receipt_row==(plan['recovery_id'],plan['parent_stage_hash'],plan['child_stage_hash'],plan['input_hash'],s._json({**plan,**auth}),None,None), 'cache_invalid')
                _assert_authorization_delta(prepared_mutation,db,task_row=task_row,receipt_row=receipt_row)
            service.project_analysis.guard(snapshot)
            r._database_controls(db, run, paused=True)
            s._require(1 <= _fresh_packet_occupied(db, plan['input_hash'], plan['packet_hash']) <= 5, 'cache_invalid')
            latest=db.execute('SELECT id FROM full_analysis_runs WHERE project_id=? AND snapshot_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 1',
                (snapshot['project']['id'],run['snapshot_id'])).fetchone()
            s._require(latest==(run['run_id'],), 'cache_invalid')
            has_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_semantic_recoveries'").fetchone()
            found=db.execute('SELECT data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE parent_stage_hash=?',
                (plan['parent_stage_hash'],)).fetchall() if has_table else []
            if found:
                s._require(len(found)==1, 'cache_invalid')
                receipt={**json.loads(found[0][0]),'consumed_at':found[0][1],'dispatch_id':found[0][2]}
                s._require({key:receipt[key] for key in PLAN_FIELDS}==plan and receipt['authorization_ref']==auth['authorization_ref'], 'cache_invalid')
            else:
                db.execute('''CREATE TABLE IF NOT EXISTS business_semantic_recoveries (
                    recovery_id TEXT PRIMARY KEY, parent_stage_hash TEXT NOT NULL UNIQUE,
                    child_stage_hash TEXT NOT NULL UNIQUE,input_hash TEXT NOT NULL,data TEXT NOT NULL,
                    consumed_at INTEGER,dispatch_id TEXT, CHECK ((consumed_at IS NULL) = (dispatch_id IS NULL)))''')
                receipt={**plan,**auth}
                parent_row=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(plan['parent_stage_hash'],)).fetchone()
                s._require(parent_row is not None, 'cache_invalid')
                parent=json.loads(parent_row[0]);stamp=s.now()
                data={'request':deepcopy(parent['request']),'context':deepcopy(parent['context']),
                    'request_hash':plan['child_stage_hash'],'result':None,'result_hash':s._hash(None),
                    'recovery_id':plan['recovery_id'],'logical_task_hash':plan['logical_task_hash']}
                receipt_row=(plan['recovery_id'],plan['parent_stage_hash'],plan['child_stage_hash'],plan['input_hash'],s._json(receipt),None,None)
                task_row=(plan['child_stage_hash'],'proposal',plan['input_hash'],'planned',1,s._json(data),None,stamp)
                db.execute('INSERT INTO business_semantic_recoveries VALUES (?,?,?,?,?,?,?)',receipt_row)
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',task_row)
                _assert_authorization_delta(prepared_mutation,db,task_row=task_row,receipt_row=receipt_row)
                receipt={**receipt,'consumed_at':None,'dispatch_id':None}
                created=True
            from ._source_material import _bind_raw_source
            _bind_raw_source(source_material, snapshot, run, db=db)
            service.project_analysis.guard(snapshot)
            r._database_controls(db, run, paused=True)
            s._require(_check_grant(authorization,plan)==auth, 'cache_invalid')
    if created: r.log_event('authorized',receipt)
    return receipt


def _recorded_evidence(receipt):
    """Validate persisted bytes without clocks, filesystem or current PID state."""
    import hashlib
    from . import semantic_recovery as r
    try:
        text = receipt['reconciliation_evidence']
        s._require(isinstance(text, str), 'cache_invalid')
        raw = text.encode('utf-8')
        s._require(len(raw) <= 16384 and hashlib.sha256(raw).hexdigest() == receipt['reconciliation_hash'], 'cache_invalid')
        check = r._evidence_json(raw)
        s._require(set(check) == EVIDENCE_FIELDS and check['run_id'] == receipt['run_id']
            and check['parent_stage_hash'] == receipt['parent_stage_hash']
            and check['parent_row_digest'] == receipt['parent_failure_digest'] and check['inventory_digest'] == receipt['inventory_digest']
            and type(check['checked_at']) is int and 0 <= receipt['authorized_at'] - check['checked_at'] <= 60
            and check['local_processes_drained'] is True and check['upstream_result_or_billing_reconciled'] is False, 'cache_invalid')
        pids = check['local_owner_pids']
        s._require(isinstance(pids, list) and pids and all(type(pid) is int and pid > 0 for pid in pids)
            and len(set(pids)) == len(pids), 'cache_invalid')
        return check
    except s.SemanticValidationError:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def _check_consumption_evidence(receipt):
    import os
    check = _recorded_evidence(receipt)
    raw = _private_evidence_bytes(receipt['reconciliation_ref'])
    s._require(raw == receipt['reconciliation_evidence'].encode('utf-8'), 'cache_invalid')
    for pid in check['local_owner_pids']:
        try: os.kill(pid, 0)
        except ProcessLookupError: pass
        else: s._require(False, 'cache_invalid')
