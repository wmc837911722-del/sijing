"""Closed two-member adjudicated review policy; a plan is never send authority."""
from copy import deepcopy
import hashlib
import json
from . import business_semantics as s

POLICY = 'operator-adjudicated-review-recovery-v2'
CASE_FIELDS = frozenset('version run_id input_hash project_id snapshot_id project_revision members'.split())
CASE_MEMBER_FIELDS = frozenset('role logical_review_hash parent_stage_hash subject_proposal_hash packet_hash'.split())
MEMBER_BASE_FIELDS = frozenset(('role logical_review_hash parent_stage_hash parent_raw_digest subject_proposal_hash '
    'subject_raw_digest subject_result_hash packet_hash request_digest context_digest wire_digest system_digest '
    'schema_digest budget_profile_digest legacy_receipt_ids legacy_receipt_raw_digests').split())
MEMBER_FIELDS = MEMBER_BASE_FIELDS | {'recovery_id', 'child_stage_hash'}
PLAN_BASE_FIELDS = frozenset(('policy run_id input_hash snapshot_id project_id project_revision vault_identity generator_hash '
    'destination_digest source_dependency_digest base_dynamic_digest base_receipt_set_digest atomic_budget_chains whole_histories '
    'partition_repair_pairs selection_binding_digest logical_draft_maximum physical_maximum_calls additional_replacements_per_target '
    'max_additional_calls bundle_limit protected_task_raw protected_receipt_raw old_task_bindings outside_input_task_raw members').split())
PLAN_FIELDS = PLAN_BASE_FIELDS | {'bundle_id', 'plan_hash'}
GRANT_FIELDS = frozenset(('policy plan_hash bundle_id run_id input_hash selection_binding_digest operator_id authorization_source '
    'authorized_at expires_at max_uses use_unit reconciliation_ref reconciliation_hash').split())
EVIDENCE_FIELDS = frozenset(('checked_at local_owner_pids local_processes_drained upstream_outcome_unknown '
    'duplicate_analysis_fee_risk_accepted analysis_only run_id input_hash bundle_id plan_hash base_dynamic_digest selection_binding_digest').split())
RECEIPT_FIELDS = frozenset(('policy bundle_id role recovery_id parent_stage_hash logical_task_hash child_stage_hash input_hash run_id '
    'packet_hash plan grant_raw evidence_raw authorization_ref dispatch_limit').split())
PLAN_BYTES = 131072
RECEIPT_BYTES = 262144
AUTH_BYTES = 16384


def _require(value):
    s._require(value, 'cache_invalid')


def _raw_hash(text):
    _require(type(text) is str)
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError:
        raise s.SemanticValidationError('cache_invalid') from None


def _closed_json(raw, fields, maximum):
    _require(type(raw) is str)
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result
    try:
        _require(len(raw.encode('utf-8')) <= maximum)
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _require(False))
        _require(type(value) is dict and set(value) == fields)
        return value
    except (ValueError, TypeError, UnicodeError):
        raise s.SemanticValidationError('cache_invalid') from None


def _sha(value):
    _require(type(value) is str and s.HEX.fullmatch(value))
    return value


def _case(plan):
    return {'version': 2, **{key: plan[key] for key in CASE_FIELDS - {'version', 'members'}},
        'members': [{key: member[key] for key in CASE_MEMBER_FIELDS} for member in plan['members']]}


def _validate_case(case, snapshot, run):
    _require(type(case) is dict and set(case) == CASE_FIELDS and type(case['version']) is int and case['version'] == 2
        and case['run_id'] == run['run_id'] and case['input_hash'] == run['semantic_input_hash']
        and case['project_id'] == snapshot['project']['id'] and case['snapshot_id'] == run['snapshot_id']
        and case['project_revision'] == run['project_revision'] == snapshot['project']['revision'])
    _require(type(case['members']) is list and len(case['members']) == 2)
    for role, member in zip(('A', 'B'), case['members']):
        _require(type(member) is dict and set(member) == CASE_MEMBER_FIELDS and member['role'] == role)
        for key in CASE_MEMBER_FIELDS - {'role'}:
            _sha(member[key])
    for key in CASE_MEMBER_FIELDS - {'role'}:
        _require(len({member[key] for member in case['members']}) == 2)


def _immutable_binding(row):
    data = json.loads(row[5])
    return s._hash({'stage_hash': row[0], 'stage': row[1], 'input_hash': row[2], 'attempt': row[4],
        'request_hash': data['request_hash'], 'request': data['request'], 'context': data['context'],
        'created_at': row[7], 'lineage': {key: data[key] for key in ('recovery_id', 'logical_task_hash', 'supersession_id') if key in data}})


def _derived_plan(base):
    _require(set(base) == PLAN_BASE_FIELDS)
    base = deepcopy(base)
    bundle = s._hash({'domain': 'review-exception-bundle-v2', 'plan_base': base})
    for member in base['members']:
        _require(set(member) == MEMBER_BASE_FIELDS)
        recovery = s._hash({'domain': 'review-exception-member-v2', 'bundle_id': bundle,
            'role': member['role'], 'parent_stage_hash': member['parent_stage_hash']})
        member.update(recovery_id=recovery, child_stage_hash=s._hash({'domain': 'review-exception-child-v2',
            'bundle_id': bundle, 'recovery_id': recovery, 'parent_stage_hash': member['parent_stage_hash']}))
    plan = {**base, 'bundle_id': bundle}
    plan['plan_hash'] = s._hash(plan)
    _require(len(s._json(plan).encode('utf-8')) <= PLAN_BYTES)
    return plan


def _reader_capture(reader, identity, capture=None):
    from ._frozen_routing import FrozenReader
    _require(type(reader) is FrozenReader and reader._FrozenReader__view_root is reader)
    value = reader._raw_dynamic_capture(identity)
    _require(capture is None or capture is value)
    return value


def _groups(receipts):
    groups = []
    for name, length in (('atomic_budget_chains', 4), ('whole_histories', 2), ('partition_repair_pairs', 2)):
        found = set()
        for receipt in receipts:
            value = receipt.get(name, [])
            _require(type(value) is list)
            for entry in value:
                _require(type(entry) is list and len(entry) == length and len(set(entry)) == length)
                for item in entry:
                    _sha(item)
                found.add(tuple(entry))
        groups.append(sorted(found))
    return dict(zip(('atomic_budget_chains', 'whole_histories', 'partition_repair_pairs'), groups))


def _reconstruct_plan(snapshot, run, *, read_checkpoint, raw_tasks, raw_receipts, raw_outside_tasks, case_binding, dynamic_digest, baseline_bindings=None):
    from .semantic_proposal_recovery import _packet_binding, _source_material, POLICY as PROPOSAL_POLICY
    from .semantic_review_recovery import POLICY as REVIEW_POLICY, _eligible
    identity = run['semantic_input_hash']
    _validate_case(case_binding,snapshot,run)
    _require(all(row[2] == identity for row in raw_tasks) and all(row[2] != identity for row in raw_outside_tasks)
        and not ({row[0] for row in raw_tasks} & {row[0] for row in raw_outside_tasks}))
    receipts = read_checkpoint.recovery_receipts(identity)
    _require(len(receipts) == len(raw_receipts) and all(item.get('policy') != POLICY for item in receipts))
    # All old receipts remain charged and must already have a complete claim.
    for receipt in receipts:
        _require(type(receipt['consumed_at']) is int)
        _sha(receipt['dispatch_id'])
        s._checkpoint_record(receipt['child_stage_hash'], snapshot=snapshot,
            expected_input_hash=identity, read_checkpoint=read_checkpoint)
    groups = _groups(receipts)
    raw_by_key = {row[0]: row for row in raw_tasks}
    receipt_by_id = {row[0]: row for row in raw_receipts}
    protected, members, source_digests = set(), [], []
    rows = read_checkpoint.task_rows(identity)
    for selected in case_binding['members']:
        parent = s._checkpoint_record(selected['parent_stage_hash'], snapshot=snapshot,
            expected_input_hash=identity, read_checkpoint=read_checkpoint)
        subject = s.semantic_attempt_chain(selected['subject_proposal_hash'], snapshot=snapshot,
            expected_input_hash=identity, read_checkpoint=read_checkpoint)
        _require(subject['stage'] == 'proposal' and subject['status'] == 'proposed' and subject['attempt'] == 1
            and parent['stage'] == 'review' and parent['attempt'] == 1
            and parent['data']['context']['proposal_hash'] == subject['stage_hash']
            and parent['data']['context']['packet_hash'] == selected['packet_hash'])
        if selected['role'] == 'A':
            matches = [r for r in receipts if r['child_stage_hash'] == parent['stage_hash']]
            _require(len(matches) == 1 and matches[0]['policy'] == REVIEW_POLICY
                and matches[0]['logical_task_hash'] == selected['logical_review_hash']
                and parent['status'] == 'running' and parent['data']['result'] is None)
        else:
            _eligible(parent, snapshot)
            _require(parent['stage_hash'] == selected['logical_review_hash'])
            matches = [r for r in receipts if r['child_stage_hash'] == subject['stage_hash']]
            _require(len(matches) == 1 and matches[0]['policy'] == PROPOSAL_POLICY)
        s._ordinary_checkpoint_primitive(parent, snapshot=snapshot, expected_input_hash=identity,
            logical_task_hash=selected['logical_review_hash'])
        siblings = [row for row in rows if row['stage'] in {'proposal', 'review'}
            and row['data']['context'].get('packet_hash') == selected['packet_hash']]
        _require(len(siblings) == 3 and all(row['attempt'] == 1 and row['status'] != 'planned' for row in siblings)
            and not any(r['parent_stage_hash'] == parent['stage_hash'] for r in receipts))
        source, forest, packet, dependencies = _packet_binding(snapshot, run, subject, read_checkpoint, **groups)
        context = subject['data']['context'];candidate = subject['data']['result']
        request = {**packet, 'stage': 'review', 'candidate': candidate,
            'proposal_hash': subject['stage_hash'], 'packet_hash': selected['packet_hash']}
        expected_context = {**context, 'proposal_hash': subject['stage_hash'],
            'claim_ids': {claim['id']: 'claim:' + s._hash({'packet': selected['packet_hash'], 'id': claim['id']})[:24] for claim in candidate['claims']}}
        _require(parent['data']['request'] == request and parent['data']['context'] == expected_context)
        keys = {row['stage_hash'] for row in siblings} | {key for key, _ in dependencies}
        protected.update(keys)
        legacy = sorted(r['recovery_id'] for r in receipts if r['child_stage_hash'] in keys)
        members.append({**selected, 'parent_raw_digest': s._hash(list(raw_by_key[parent['stage_hash']])),
            'subject_raw_digest': s._hash(list(raw_by_key[subject['stage_hash']])), 'subject_result_hash': subject['data']['result_hash'],
            'request_digest': s._hash(request), 'context_digest': s._hash(expected_context),
            'wire_digest': s._hash(s._json(request)), 'system_digest': s._hash(s.semantic_stage_system(snapshot, 'review', request)),
            'schema_digest': s._hash(s.semantic_output_schema('review', request)), 'budget_profile_digest': s._hash(context['budget_profile']),
            'legacy_receipt_ids': legacy, 'legacy_receipt_raw_digests': [s._hash(list(receipt_by_id[key])) for key in legacy]})
        source_digests.append(s._hash({'source': _source_material(source), 'frontier': forest['terminal_candidate'], 'raw_dependencies': dependencies}))
    _require(len(set(source_digests)) == 1)
    base = {'policy': POLICY, 'run_id': run['run_id'], 'input_hash': identity, 'snapshot_id': run['snapshot_id'],
        'project_id': snapshot['project']['id'], 'project_revision': run['project_revision'],
        'vault_identity': s._hash(snapshot.get('vault_path')), 'generator_hash': s._semantic_generator_hash(snapshot),
        'destination_digest': s._hash(s.provider_destination(snapshot['provider'])), 'source_dependency_digest': source_digests[0],
        'base_dynamic_digest': dynamic_digest, 'base_receipt_set_digest': s._hash([list(row) for row in raw_receipts]),
        **{name: [list(item) for item in value] for name, value in groups.items()},
        'selection_binding_digest': s._hash(case_binding), 'logical_draft_maximum': 2, 'physical_maximum_calls': 6,
        'additional_replacements_per_target': 1, 'max_additional_calls': 6, 'bundle_limit': 1,
        'protected_task_raw': [{'stage_hash': key, 'raw8_digest': s._hash(list(raw_by_key[key]))} for key in sorted(protected)],
        'protected_receipt_raw': [{'recovery_id': row[0], 'sql7_digest': s._hash(list(row))} for row in raw_receipts],
        'outside_input_task_raw': [{'stage_hash': row[0], 'raw8_digest': s._hash(list(row))} for row in raw_outside_tasks],
        'old_task_bindings': [{'stage_hash': row[0], 'baseline_status': row[3], 'baseline_raw8_digest': s._hash(list(row)),
            'immutable_binding_digest': _immutable_binding(row)} for row in raw_tasks], 'members': members}
    if baseline_bindings is not None:
        base['old_task_bindings'] = deepcopy(baseline_bindings)
    return _derived_plan(base)


def review_exception_bundle_plan(snapshot, run, *, read_checkpoint, dynamic_capture, case_binding):
    """Reconstruct a powerless selection from factory SQL; caller CASE is not a pin."""
    from ._prepared_mutation import _raw_recovery_rows, _raw_task_rows, _raw_all_task_rows, _dynamic_digest
    try:
        identity = run['semantic_input_hash']
        capture = _reader_capture(read_checkpoint,identity,dynamic_capture)
        all_tasks = _raw_all_task_rows(capture,input_hash=identity)
        current = _raw_task_rows(capture,input_hash=identity)
        outside = tuple(row for row in all_tasks if row[2] != identity)
        _require(len(current) + len(outside) == len(all_tasks)
            and {row[0] for row in current} | {row[0] for row in outside} == {row[0] for row in all_tasks})
        return _reconstruct_plan(snapshot,run,read_checkpoint=read_checkpoint,
            raw_tasks=current,raw_outside_tasks=outside,raw_receipts=_raw_recovery_rows(capture,input_hash=identity),
            case_binding=case_binding,dynamic_digest=_dynamic_digest(capture,input_hash=identity))
    except s.SemanticValidationError:
        raise
    except (KeyError,ValueError,TypeError,AttributeError,StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def _validate_plan(plan):
    _require(type(plan) is dict and set(plan) == PLAN_FIELDS and plan['policy'] == POLICY
        and len(s._json(plan).encode('utf-8')) <= PLAN_BYTES)
    for key, expected in (('logical_draft_maximum',2), ('physical_maximum_calls',6),
            ('additional_replacements_per_target',1), ('max_additional_calls',6), ('bundle_limit',1)):
        _require(type(plan[key]) is int and plan[key] == expected)
    for key in ('input_hash','vault_identity','generator_hash','destination_digest','source_dependency_digest',
            'base_dynamic_digest','base_receipt_set_digest','selection_binding_digest','bundle_id','plan_hash'):
        _sha(plan[key])
    _require(type(plan['members']) is list and len(plan['members']) == 2)
    for role, member in zip(('A','B'),plan['members']):
        _require(type(member) is dict and set(member) == MEMBER_FIELDS and member['role'] == role)
        for key in MEMBER_FIELDS - {'role','legacy_receipt_ids','legacy_receipt_raw_digests'}:
            _sha(member[key])
        ids, digests = member['legacy_receipt_ids'], member['legacy_receipt_raw_digests']
        _require(type(ids) is list and type(digests) is list and len(ids) == len(digests) and ids == sorted(set(ids)))
        for value in ids + digests:
            _sha(value)
    for name, key, fields in (
            ('protected_task_raw','stage_hash',{'stage_hash','raw8_digest'}),
            ('outside_input_task_raw','stage_hash',{'stage_hash','raw8_digest'}),
            ('protected_receipt_raw','recovery_id',{'recovery_id','sql7_digest'}),
            ('old_task_bindings','stage_hash',{'stage_hash','baseline_status','baseline_raw8_digest','immutable_binding_digest'})):
        values = plan[name]
        _require(type(values) is list and len(values) <= 8192 and all(type(item) is dict and set(item) == fields for item in values))
        keys = [item[key] for item in values]
        _require(keys == sorted(set(keys)))
        for item in values:
            for field in fields - {'baseline_status'}:
                _sha(item[field])
    _require(not ({item['stage_hash'] for item in plan['old_task_bindings']}
        & {item['stage_hash'] for item in plan['outside_input_task_raw']}))
    base = {key: deepcopy(plan[key]) for key in PLAN_BASE_FIELDS}
    base['members'] = [{key:value for key,value in member.items() if key in MEMBER_BASE_FIELDS} for member in base['members']]
    _require(_derived_plan(base) == plan and s._hash(_case(plan)) == plan['selection_binding_digest'])


def _recorded_authority(plan, grant_raw, evidence_raw):
    grant = _closed_json(grant_raw, GRANT_FIELDS, AUTH_BYTES)
    evidence = _closed_json(evidence_raw, EVIDENCE_FIELDS, AUTH_BYTES)
    _require(grant['policy'] == POLICY and grant['operator_id'] == 'root'
        and grant['authorization_source'] == 'user-fix-and-continue' and type(grant['max_uses']) is int
        and grant['max_uses'] == 1 and grant['use_unit'] == 'atomic_two_target_authorization'
        and type(grant['authorized_at']) is int and type(grant['expires_at']) is int
        and grant['expires_at'] - grant['authorized_at'] == 3600
        and type(evidence['checked_at']) is int
        and 0 <= grant['authorized_at'] - evidence['checked_at'] <= 60
        and grant['reconciliation_hash'] == _raw_hash(evidence_raw))
    for key in ('run_id','input_hash','bundle_id','plan_hash','selection_binding_digest'):
        _require(grant[key] == evidence[key] == plan[key])
    _require(evidence['base_dynamic_digest'] == plan['base_dynamic_digest'])
    for key in ('local_processes_drained','upstream_outcome_unknown','duplicate_analysis_fee_risk_accepted','analysis_only'):
        _require(evidence[key] is True)
    pids = evidence['local_owner_pids']
    _require(type(pids) is list and bool(pids) and all(type(pid) is int and pid > 0 for pid in pids)
        and len(pids) == len(set(pids)))
    return grant, evidence


def _authority_text(value):
    """Read bounded local authority bytes from one regular0600 nofollow fd."""
    import os
    import stat
    from pathlib import Path
    _require(isinstance(value, (str,Path)) and Path(value).is_absolute())
    try:
        fd = os.open(value,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            raw = stream.read(AUTH_BYTES + 1)
            _require(len(raw) <= AUTH_BYTES)
            return raw.decode('utf-8')
    except (OSError,UnicodeError,TypeError,ValueError):
        raise s.SemanticValidationError('cache_invalid') from None


def _live_authority(path, plan, *, expected_pin, fresh):
    import os
    import time
    _require(expected_pin == plan['selection_binding_digest'])
    grant_raw = _authority_text(path)
    parsed = _closed_json(grant_raw, GRANT_FIELDS, AUTH_BYTES)
    evidence_raw = _authority_text(parsed['reconciliation_ref'])
    grant, evidence = _recorded_authority(plan, grant_raw, evidence_raw)
    now = time.time()
    _require(grant['authorized_at'] <= now < grant['expires_at'])
    if fresh:
        _require(0 <= now - grant['authorized_at'] <= 60 and 0 <= now - evidence['checked_at'] <= 60)
    for pid in evidence['local_owner_pids']:
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            continue
        except OSError:
            _require(False)
        _require(False)
    return grant_raw, evidence_raw


class _V2RawMembers:
    __slots__ = ('__capture','__encoded','__seal')
    def __init__(self, capture, plan, receipts, *, _seal=None):
        from ._prepared_mutation import _SEAL
        _require(_seal is _SEAL)
        object.__setattr__(self,'_V2RawMembers__capture',capture)
        object.__setattr__(self,'_V2RawMembers__encoded',s._json([plan,receipts]))
        object.__setattr__(self,'_V2RawMembers__seal',_SEAL)
    def __setattr__(self,name,value):
        raise AttributeError('sealed raw members')
    def __reduce__(self):
        raise TypeError('raw members cannot be serialized')


class _V2Ancestry:
    __slots__ = ('__capture','__members','__hidden','__seal')
    def __init__(self, capture, members, hidden, *, _seal=None):
        from ._prepared_mutation import _SEAL
        _require(_seal is _SEAL and type(members) is _V2RawMembers and members._V2RawMembers__capture is capture)
        object.__setattr__(self,'_V2Ancestry__capture',capture)
        object.__setattr__(self,'_V2Ancestry__members',members)
        object.__setattr__(self,'_V2Ancestry__hidden',frozenset(hidden))
        object.__setattr__(self,'_V2Ancestry__seal',_SEAL)
    def __setattr__(self,name,value):
        raise AttributeError('sealed ancestry')
    def __reduce__(self):
        raise TypeError('ancestry cannot be serialized')


class _V2BundleProof:
    __slots__ = ('__capture','__ancestry','__snapshot','__seal')
    def __init__(self, capture, ancestry, snapshot, *, _seal=None):
        from ._prepared_mutation import _SEAL
        _require(_seal is _SEAL and type(ancestry) is _V2Ancestry and ancestry._V2Ancestry__capture is capture)
        object.__setattr__(self,'_V2BundleProof__capture',capture)
        object.__setattr__(self,'_V2BundleProof__ancestry',ancestry)
        object.__setattr__(self,'_V2BundleProof__snapshot',s._hash(snapshot))
        object.__setattr__(self,'_V2BundleProof__seal',_SEAL)
    def __setattr__(self,name,value):
        raise AttributeError('sealed bundle proof')
    def __reduce__(self):
        raise TypeError('bundle proof cannot be serialized')


def _bootstrap_members(snapshot, *, input_hash, raw_capture, read_checkpoint):
    from ._prepared_mutation import _raw_recovery_rows, _raw_task_rows, _raw_all_task_rows, _SEAL
    raw_receipts = _raw_recovery_rows(raw_capture,input_hash=input_hash)
    candidates = []
    for raw in raw_receipts:
        parsed = json.loads(raw[4])
        if parsed.get('policy') == POLICY:
            parsed = _closed_json(raw[4], RECEIPT_FIELDS, RECEIPT_BYTES)
            _require(s._json(parsed) == raw[4])
            _require(tuple(parsed[key] for key in ('recovery_id','parent_stage_hash','child_stage_hash','input_hash')) == raw[:4])
            candidates.append({**parsed,'consumed_at':raw[5],'dispatch_id':raw[6]})
    _require(len(candidates) == 2)
    candidates.sort(key=lambda row:row['role'])
    plan = candidates[0]['plan']
    _validate_plan(plan)
    run = {key:plan[key] for key in ('run_id','snapshot_id','project_revision')}
    run['semantic_input_hash'] = input_hash
    _validate_case(_case(plan),snapshot,run)
    _require(s._structured_output(snapshot) and plan['generator_hash'] == s._semantic_generator_hash(snapshot)
        and plan['destination_digest'] == s._hash(s.provider_destination(snapshot['provider']))
        and plan['vault_identity'] == s._hash(snapshot.get('vault_path')))
    all_raws = {row[0]:row for row in _raw_all_task_rows(raw_capture,input_hash=input_hash)}
    for committed in plan['outside_input_task_raw']:
        row = all_raws.get(committed['stage_hash'])
        _require(row is not None and row[2] != input_hash and s._hash(list(row)) == committed['raw8_digest'])
    raws = {key:row for key,row in all_raws.items() if row[2] == input_hash}
    for receipt, member in zip(candidates,plan['members']):
        _require(receipt['plan'] == plan and receipt['grant_raw'] == candidates[0]['grant_raw']
            and receipt['evidence_raw'] == candidates[0]['evidence_raw'] and receipt['policy'] == POLICY
            and receipt['bundle_id'] == plan['bundle_id'] and receipt['role'] == member['role']
            and receipt['run_id'] == plan['run_id'] and receipt['input_hash'] == input_hash
            and receipt['logical_task_hash'] == member['logical_review_hash']
            and type(receipt['dispatch_limit']) is int and receipt['dispatch_limit'] == 1)
        for key in ('recovery_id','parent_stage_hash','child_stage_hash','packet_hash'):
            _require(receipt[key] == member[key])
        grant,_ = _recorded_authority(plan,receipt['grant_raw'],receipt['evidence_raw'])
        _require(receipt['authorization_ref'] == _raw_hash(receipt['grant_raw']))
        parent = read_checkpoint(member['parent_stage_hash'])
        subject = read_checkpoint(member['subject_proposal_hash'])
        child = read_checkpoint(member['child_stage_hash'])
        _require(parent is not None and subject is not None and child is not None)
        _require(s._hash(list(raws[parent['stage_hash']])) == member['parent_raw_digest']
            and s._hash(list(raws[subject['stage_hash']])) == member['subject_raw_digest']
            and subject['data']['result_hash'] == member['subject_result_hash'])
        for row,logical in ((parent,member['logical_review_hash']),(child,member['logical_review_hash']),
                (subject,subject['data'].get('logical_task_hash',subject['stage_hash']))):
            s._ordinary_checkpoint_primitive(row,snapshot=snapshot,expected_input_hash=input_hash,logical_task_hash=logical)
        _require(subject['status'] == 'proposed' and subject['attempt'] == 1 and child['attempt'] == 1
            and child['data'].get('recovery_id') == member['recovery_id']
            and child['data'].get('logical_task_hash') == member['logical_review_hash']
            and child['data']['request'] == parent['data']['request'] and child['data']['context'] == parent['data']['context'])
        context = parent['data']['context'];request = parent['data']['request']
        expected = {'request_digest':s._hash(request),'context_digest':s._hash(context),
            'wire_digest':s._hash(s._json(request)),'system_digest':s._hash(s.semantic_stage_system(snapshot,'review',request)),
            'schema_digest':s._hash(s.semantic_output_schema('review',request)),'budget_profile_digest':s._hash(context['budget_profile'])}
        _require(all(member[key] == value for key,value in expected.items()))
        if child['status'] == 'planned':
            _require(receipt['consumed_at'] is None and receipt['dispatch_id'] is None)
        else:
            _require(type(receipt['consumed_at']) is int and grant['authorized_at'] <= receipt['consumed_at'] < grant['expires_at'])
            _sha(receipt['dispatch_id'])
        if child['status'] in {'accepted','received'}:
            _require(s.BusinessSemanticIntegration._review(child['data']['result'],set(context['input_ids']),set(context['operation_ids']),subject['data']['result']) == child['data']['result'])
    for protected in plan['protected_task_raw']:
        _require(protected['stage_hash'] in raws and s._hash(list(raws[protected['stage_hash']])) == protected['raw8_digest'])
    legacy = [row for row in raw_receipts if row[0] not in {member['recovery_id'] for member in plan['members']}]
    _require(plan['protected_receipt_raw'] == [{'recovery_id':row[0],'sql7_digest':s._hash(list(row))} for row in legacy]
        and plan['base_receipt_set_digest'] == s._hash([list(row) for row in legacy]))
    for binding in plan['old_task_bindings']:
        row = raws.get(binding['stage_hash'])
        _require(row is not None and _immutable_binding(row) == binding['immutable_binding_digest'])
        if row[3] == binding['baseline_status']:
            _require(s._hash(list(row)) == binding['baseline_raw8_digest'])
        else:
            allowed = {'planned':{'running','received','proposed','accepted','needs_review'},
                'running':{'received','proposed','accepted','needs_review'},'received':{'proposed','accepted','needs_review'}}
            _require(row[1] in {'proposal','review'} and row[3] in allowed.get(binding['baseline_status'],set()))
    return _V2RawMembers(raw_capture,plan,candidates,_seal=_SEAL)


def _bootstrap_d2(snapshot, *, raw_members, raw_capture, read_checkpoint):
    from ._prepared_mutation import _SEAL
    _require(type(raw_members) is _V2RawMembers and raw_members._V2RawMembers__capture is raw_capture)
    plan,receipts = json.loads(raw_members._V2RawMembers__encoded)
    identity = plan['input_hash'];hidden = {member['child_stage_hash'] for member in plan['members']}
    old = {item['stage_hash'] for item in plan['old_task_bindings']}
    from .semantic_d2_retry import authenticate_audit
    audits = read_checkpoint.d2_retry_rows(identity)
    for audit in audits:
        authenticate_audit(snapshot,plan,receipts,read_checkpoint,audit)
    audit_keys = {a['plan']['case']['parent_stage_hash'] for a in audits}
    _require({r['stage_hash'] for r in read_checkpoint.task_rows(identity) if 'd2_retry_id' in r['data']} == audit_keys)
    for member,receipt in zip(plan['members'],receipts):
        subject = read_checkpoint(member['subject_proposal_hash']);d1 = read_checkpoint(member['child_stage_hash'])
        rows = [row for row in read_checkpoint.task_rows(identity) if row['stage'] in {'proposal','review'}
            and row['data']['context'].get('packet_hash') == member['packet_hash']]
        extras = [row for row in rows if row['stage_hash'] not in old | hidden]
        proposals = [row for row in extras if row['stage'] == 'proposal']
        reviews = [row for row in extras if row['stage'] == 'review']
        _require(len(proposals) <= 1 and len(reviews) <= len(proposals) and len(rows) <= 6)
        negative = d1['status'] == 'needs_review' and d1['data'].get('failure_code') == 'review_rejected'
        payload = None
        if negative:
            _require(receipt['consumed_at'] is not None and d1['data']['result'] is not None)
            payload = s._revision_payload_primitive(snapshot,subject,d1)
        if not extras:
            continue
        _require(negative)
        proposal = proposals[0];context = subject['data']['context']
        revision = s._revision_context(2,proposal=subject,failure=d1)
        revision['feedback_hash'] = s._hash(payload['revision_feedback'])
        expected_context = {**context,'revision':revision}
        _require(proposal['attempt'] == 2 and proposal['data']['request'] == payload and proposal['data']['context'] == expected_context
            and not {'recovery_id','logical_task_hash','supersession_id'}.intersection(proposal['data']))
        s._ordinary_checkpoint_primitive(proposal,snapshot=snapshot,expected_input_hash=identity,logical_task_hash=proposal['stage_hash'])
        if proposal['status'] in {'received','proposed'}:
            s.validate_revision_preservation(proposal['data']['result'],subject,payload['preservation'],review=d1['data']['result'])
        if reviews:
            review = reviews[0];candidate = proposal['data']['result']
            expected_request = {**subject['data']['request'],'stage':'review','candidate':candidate,
                'proposal_hash':proposal['stage_hash'],'packet_hash':member['packet_hash']}
            expected_review_context = {**expected_context,'proposal_hash':proposal['stage_hash'],
                'claim_ids':{claim['id']:'claim:'+s._hash({'packet':member['packet_hash'],'id':claim['id']})[:24] for claim in candidate['claims']}}
            _require(proposal['status'] == 'proposed' and review['attempt'] == 2 and review['data']['request'] == expected_request
                and review['data']['context'] == expected_review_context
                and not {'recovery_id','logical_task_hash','supersession_id'}.intersection(review['data']))
            s._ordinary_checkpoint_primitive(review,snapshot=snapshot,expected_input_hash=identity,logical_task_hash=review['stage_hash'])
            if review['status'] in {'received','accepted'}:
                _require(s.BusinessSemanticIntegration._review(review['data']['result'],set(context['input_ids']),set(context['operation_ids']),candidate) == review['data']['result'])
        hidden.update(row['stage_hash'] for row in extras)
    return _V2Ancestry(raw_capture,raw_members,hidden,_seal=_SEAL)


def authenticate_review_exception_bundle(*, snapshot, expected_input_hash, read_checkpoint):
    """P0 raw -> P1 D2 -> P2 strict history/Source -> P3 whole sealed proof."""
    from ._prepared_mutation import _raw_task_rows,_raw_recovery_rows,_raw_all_task_rows,_SEAL
    try:
        capture = _reader_capture(read_checkpoint,expected_input_hash)
        memo = read_checkpoint._FrozenReader__v2_proofs
        cache_key = (expected_input_hash,s._hash(snapshot))
        found = memo.get(cache_key)
        if found is not None:
            _require(type(found) is _V2BundleProof and found._V2BundleProof__capture is capture)
            return found
        members = _bootstrap_members(snapshot,input_hash=expected_input_hash,raw_capture=capture,read_checkpoint=read_checkpoint)
        ancestry = _bootstrap_d2(snapshot,raw_members=members,raw_capture=capture,read_checkpoint=read_checkpoint)
        history = read_checkpoint._v2_history_view(ancestry)
        plan,receipts = json.loads(members._V2RawMembers__encoded)
        old_keys = {item['stage_hash'] for item in plan['old_task_bindings']}
        current = _raw_task_rows(capture,input_hash=expected_input_hash)
        _require(old_keys <= {row[0] for row in current})
        old = tuple(row for row in current if row[0] in old_keys)
        outside_keys = {item['stage_hash'] for item in plan['outside_input_task_raw']}
        outside = tuple(row for row in _raw_all_task_rows(capture,input_hash=expected_input_hash) if row[0] in outside_keys)
        legacy = tuple(row for row in _raw_recovery_rows(capture,input_hash=expected_input_hash)
            if row[0] not in {member['recovery_id'] for member in plan['members']})
        run = {key:plan[key] for key in ('run_id','snapshot_id','project_revision')};run['semantic_input_hash']=expected_input_hash
        rebuilt = _reconstruct_plan(snapshot,run,read_checkpoint=history,raw_tasks=old,raw_receipts=legacy,raw_outside_tasks=outside,
            case_binding=_case(plan),dynamic_digest=plan['base_dynamic_digest'],baseline_bindings=plan['old_task_bindings'])
        _require(rebuilt == plan)
        for row in history.task_rows(expected_input_hash):
            s._checkpoint_record(row['stage_hash'],snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=history)
        proof = _V2BundleProof(capture,ancestry,snapshot,_seal=_SEAL)
        memo[cache_key] = proof
        return proof
    except s.SemanticValidationError:
        raise
    except (KeyError,ValueError,TypeError,AttributeError,StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def _proof_parts(proof, reader, snapshot, identity):
    from ._prepared_mutation import _SEAL
    capture = _reader_capture(reader,identity)
    _require(type(proof) is _V2BundleProof and proof._V2BundleProof__seal is _SEAL
        and proof._V2BundleProof__capture is capture and proof._V2BundleProof__snapshot == s._hash(snapshot))
    ancestry = proof._V2BundleProof__ancestry
    plan,receipts = json.loads(ancestry._V2Ancestry__members._V2RawMembers__encoded)
    return plan,receipts,reader._v2_history_view(ancestry)


def _bundle_if_present(snapshot, identity, reader):
    receipts = reader.recovery_receipts(identity) if callable(getattr(reader,'recovery_receipts',None)) else []
    if not any(receipt.get('policy') == POLICY for receipt in receipts):
        return None
    return authenticate_review_exception_bundle(snapshot=snapshot,expected_input_hash=identity,read_checkpoint=reader)


class _ReviewExceptionAuthorization:
    """Local-file identity binding only; no cached permission or live decisions."""
    __slots__ = ('__service','__path','__case','__grant','__evidence','__seal')
    def __init__(self, service, path, case, grant, evidence, *, _seal=None):
        from ._prepared_mutation import _SEAL
        _require(_seal is _SEAL)
        for name,value in (('service',service),('path',path),('case',s._json(case)),('grant',grant),('evidence',evidence),('seal',_SEAL)):
            object.__setattr__(self,'_ReviewExceptionAuthorization__'+name,value)
    def __setattr__(self,name,value):
        raise AttributeError('sealed local authority binding')
    def __reduce__(self):
        raise TypeError('local authority cannot be serialized')


def _load_review_exception_authorization(service, grant_path, *, expected_case):
    """Private reviewed operator supplies its source-pinned CASE; never creates a grant."""
    from ._prepared_mutation import _SEAL
    try:
        _require(type(expected_case) is dict and set(expected_case) == CASE_FIELDS)
        fake_snapshot={'project':{'id':expected_case['project_id'],'revision':expected_case['project_revision']}}
        run={key:expected_case[key] for key in ('run_id','snapshot_id','project_revision')}
        run['semantic_input_hash']=expected_case['input_hash']
        _validate_case(expected_case,fake_snapshot,run)
        grant_raw=_authority_text(grant_path);grant=_closed_json(grant_raw,GRANT_FIELDS,AUTH_BYTES)
        evidence_raw=_authority_text(grant['reconciliation_ref']);evidence=_closed_json(evidence_raw,EVIDENCE_FIELDS,AUTH_BYTES)
        identity={key:grant[key] for key in ('run_id','input_hash','bundle_id','plan_hash','selection_binding_digest')}
        identity['base_dynamic_digest']=evidence['base_dynamic_digest']
        _require(all(grant[key]==expected_case[key] for key in ('run_id','input_hash'))
            and grant['selection_binding_digest']==s._hash(expected_case))
        _recorded_authority(identity,grant_raw,evidence_raw)
        cap=_ReviewExceptionAuthorization(service,str(grant_path),expected_case,grant_raw,evidence_raw,_seal=_SEAL)
        mapping=getattr(service,'_review_exception_authorizations',None)
        if mapping is None:
            mapping={};service._review_exception_authorizations=mapping
        _require(type(mapping) is dict and grant['bundle_id'] not in mapping)
        mapping[grant['bundle_id']]=cap
        return cap
    except (KeyError,ValueError,TypeError,AttributeError,UnicodeError):
        raise s.SemanticValidationError('cache_invalid') from None


def _check_cap(cap, service, plan, *, fresh):
    from ._prepared_mutation import _SEAL
    _require(type(cap) is _ReviewExceptionAuthorization and cap._ReviewExceptionAuthorization__seal is _SEAL
        and cap._ReviewExceptionAuthorization__service is service
        and getattr(service,'_review_exception_authorizations',{}).get(plan['bundle_id']) is cap
        and json.loads(cap._ReviewExceptionAuthorization__case)==_case(plan))
    raw=_live_authority(cap._ReviewExceptionAuthorization__path,plan,expected_pin=plan['selection_binding_digest'],fresh=fresh)
    _require(raw==(cap._ReviewExceptionAuthorization__grant,cap._ReviewExceptionAuthorization__evidence))
    return raw


def _authorization_baseline_exemptions(prepared, plan, *, snapshot, identity, reader):
    """Exact historical blockers, bound to this not-yet-returned preparation."""
    from ._prepared_mutation import _PreparedMutation,_scope
    _validate_plan(plan)
    run={key:plan[key] for key in ('run_id','snapshot_id','project_revision')}
    run['semantic_input_hash']=identity
    _require(type(prepared) is _PreparedMutation and prepared._PreparedMutation__used == [False]
        and prepared._PreparedMutation__operation == 'authorize_review_exception_bundle'
        and prepared._PreparedMutation__scope == _scope(snapshot,run)
        and plan['input_hash'] == identity
        and prepared._PreparedMutation__target == (plan['bundle_id'],plan['plan_hash'])
        and prepared._PreparedMutation__dynamic is _reader_capture(reader,identity))
    # Validate even rows that ordinary inventory would skip for another generator.
    for row in reader.task_rows(identity):
        s._checkpoint_record(row['stage_hash'],snapshot=snapshot,expected_input_hash=identity,read_checkpoint=reader)
    a,b=plan['members']
    first,second=reader(a['parent_stage_hash']),reader(b['parent_stage_hash'])
    _require(first['status'] == 'running' and first['data']['result'] is None
        and second['status'] == 'needs_review' and second['data'].get('failure_code') == 'model_call')
    return {a['parent_stage_hash']:'running',b['parent_stage_hash']:'model_call'}


def prepare_review_exception_bundle(service, snapshot, run, plan, *, source_material):
    from ._prepared_mutation import _PreparedMutation,_SEAL,_scope,_mutation_stamp
    from ._source_material import bind_source_material
    from .semantic_recovery import _current_context
    _validate_plan(plan)
    service.project_analysis.guard(snapshot)
    with service.store._db() as db:
        db.execute('BEGIN');_current_context(db,snapshot,run)
        records=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
    reader=bind_source_material(source_material,snapshot,run,read_checkpoint=records.read_checkpoint)
    dynamic=reader._raw_dynamic_capture(run['semantic_input_hash'])
    _require(review_exception_bundle_plan(snapshot,run,read_checkpoint=reader,dynamic_capture=dynamic,case_binding=_case(plan))==plan)
    prepared = _PreparedMutation('authorize_review_exception_bundle',_scope(snapshot,run),(plan['bundle_id'],plan['plan_hash']),
        dynamic,_mutation_stamp(),source_material,_seal=_SEAL)
    records.read_checkpoint=reader
    inventory=s._semantic_task_inventory(records,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],
        atomic_budget_chains=tuple(map(tuple,plan['atomic_budget_chains'])),
        whole_histories=tuple(map(tuple,plan['whole_histories'])),
        partition_repair_pairs=tuple(map(tuple,plan['partition_repair_pairs'])),
        _authorization_baseline=(prepared,plan))
    _require(inventory['task_hashes'] == sorted(row['stage_hash'] for row in records)
        and inventory['occupied_calls'] == sum(row['status'] != 'planned' for row in records))
    return prepared


def authorize_review_exception_bundle(service, snapshot, run, plan, *, authorization, source_material, prepared_mutation):
    from ._prepared_mutation import _consume_prepared,_assert_bundle_authorization_delta,_fresh_packet_occupied
    from ._source_material import _bind_raw_source
    from .semantic_recovery import _current_context,_database_controls,log_event
    from .core import now
    _validate_plan(plan)
    receipts=[]
    with service._exclusive(snapshot['project']['id']):
        service.project_analysis.guard(snapshot)
        controls=service._controls(run)
        _require(controls.get('paused') is True and controls.get('cancelled',False) is False)
        grant_raw,evidence_raw=_check_cap(authorization,service,plan,fresh=True)
        with service.store._lock,service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _consume_prepared(prepared_mutation,operation='authorize_review_exception_bundle',snapshot=snapshot,run=run,
                target=(plan['bundle_id'],plan['plan_hash']),material=source_material,db=db)
            _current_context(db,snapshot,run);_database_controls(db,run,paused=True)
            tasks=[];raw_receipts=[]
            for member in plan['members']:
                _require(_fresh_packet_occupied(db,run['semantic_input_hash'],member['packet_hash'])==3)
                _require(not db.execute('SELECT 1 FROM business_semantic_recoveries WHERE parent_stage_hash=? OR child_stage_hash=?',
                    (member['parent_stage_hash'],member['child_stage_hash'])).fetchone())
                raw=db.execute('SELECT data FROM business_semantic_tasks WHERE stage_hash=?',(member['parent_stage_hash'],)).fetchone()
                _require(raw is not None);parent=json.loads(raw[0])
                data={'request_hash':member['child_stage_hash'],'request':parent['request'],'context':parent['context'],
                    'result':None,'result_hash':s._hash(None),'recovery_id':member['recovery_id'],'logical_task_hash':member['logical_review_hash']}
                task=(member['child_stage_hash'],'review',plan['input_hash'],'planned',1,s._json(data),None,now())
                receipt={'policy':POLICY,'bundle_id':plan['bundle_id'],'role':member['role'],'recovery_id':member['recovery_id'],
                    'parent_stage_hash':member['parent_stage_hash'],'logical_task_hash':member['logical_review_hash'],
                    'child_stage_hash':member['child_stage_hash'],'input_hash':plan['input_hash'],'run_id':plan['run_id'],
                    'packet_hash':member['packet_hash'],'plan':plan,'grant_raw':grant_raw,'evidence_raw':evidence_raw,
                    'authorization_ref':_raw_hash(grant_raw),'dispatch_limit':1}
                encoded=s._json(receipt);_require(len(encoded.encode('utf-8'))<=RECEIPT_BYTES)
                recovery=(member['recovery_id'],member['parent_stage_hash'],member['child_stage_hash'],plan['input_hash'],encoded,None,None)
                db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',task)
                db.execute('INSERT INTO business_semantic_recoveries VALUES (?,?,?,?,?,?,?)',recovery)
                tasks.append(task);raw_receipts.append(recovery);receipts.append({**receipt,'consumed_at':None,'dispatch_id':None})
            _assert_bundle_authorization_delta(prepared_mutation,db,task_rows=tuple(tasks),receipt_rows=tuple(raw_receipts))
            _bind_raw_source(source_material,snapshot,run,db=db)
            _current_context(db,snapshot,run);_database_controls(db,run,paused=True);service.project_analysis.guard(snapshot)
            _require(_check_cap(authorization,service,plan,fresh=True)==(grant_raw,evidence_raw))
    for receipt in receipts:
        log_event('authorized',receipt)
    return receipts


def _consume_review_exception(db, snapshot, run, child_stage_hash, *, source_material, prepared_mutation):
    import time
    import uuid
    from ._prepared_mutation import _PreparedMutation,_consume_prepared,_fresh_packet_occupied,_assert_proposal_consumption_delta,_dynamic_digest
    from .semantic_recovery import _current_context,_database_controls
    from ._source_material import _bind_raw_source
    _require(type(prepared_mutation) is _PreparedMutation and source_material is not None)
    runtime=prepared_mutation._PreparedMutation__runtime
    _require(runtime is not None and child_stage_hash in runtime._task_bindings)
    proof=prepared_mutation._PreparedMutation__review_exception_proof
    _require(type(proof) is _V2BundleProof and proof._V2BundleProof__capture is prepared_mutation._PreparedMutation__dynamic)
    plan,receipts=json.loads(proof._V2BundleProof__ancestry._V2Ancestry__members._V2RawMembers__encoded)
    found=[item for item in receipts if item['child_stage_hash']==child_stage_hash]
    _require(len(found)==1);receipt=found[0]
    cap=prepared_mutation._PreparedMutation__review_exception_authorization
    authority=_check_cap(cap,runtime.service,plan,fresh=False)
    _dynamic_digest(prepared_mutation._PreparedMutation__dynamic,input_hash=run['semantic_input_hash'])
    _consume_prepared(prepared_mutation,operation='consume_recovery',snapshot=snapshot,run=run,
        target=(child_stage_hash,s._hash(runtime._task_bindings[child_stage_hash])),material=source_material,db=db)
    _current_context(db,snapshot,run);_database_controls(db,run,paused=False);runtime.guard()
    _require(3 <= _fresh_packet_occupied(db,run['semantic_input_hash'],receipt['packet_hash']) < 6
        and receipt['consumed_at'] is None and receipt['dispatch_id'] is None
        and authority==(receipt['grant_raw'],receipt['evidence_raw']))
    stamp=int(time.time());dispatch=uuid.uuid4().hex+uuid.uuid4().hex
    _require(db.execute('UPDATE business_semantic_recoveries SET consumed_at=?,dispatch_id=? WHERE recovery_id=? AND consumed_at IS NULL AND dispatch_id IS NULL',
        (stamp,dispatch,receipt['recovery_id'])).rowcount==1)
    _require(db.execute("UPDATE business_semantic_tasks SET status='running' WHERE stage_hash=? AND status='planned'",(child_stage_hash,)).rowcount==1)
    _assert_proposal_consumption_delta(prepared_mutation,db,child=child_stage_hash,stamp=stamp,dispatch=dispatch)
    _bind_raw_source(source_material,snapshot,run,db=db);_current_context(db,snapshot,run);_database_controls(db,run,paused=False);runtime.guard()
    _require(_check_cap(cap,runtime.service,plan,fresh=False)==authority)
    return {**receipt,'consumed_at':stamp,'dispatch_id':dispatch}
