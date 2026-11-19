"""Private explicitly selected ordinary first-review timeout recovery."""
from . import business_semantics as s
from .semantic_recovery import AUTH_FIELDS

POLICY = 'operator-normal-review-timeout-once-v1'
PLAN_FIELDS = frozenset(('policy parent_stage_hash logical_task_hash parent_failure_digest '
    'subject_proposal_hash subject_proposal_digest subject_result_hash input_hash run_id snapshot_id '
    'project_id project_revision vault_identity generator_hash destination_digest system_digest wire_digest schema_digest '
    'budget_profile_digest packet_hash source_packet_digest source_dependency_digest inventory_digest '
    'logical_draft_maximum physical_maximum_calls replacement_allowance '
    'atomic_budget_chains whole_histories partition_repair_pairs recovery_id child_stage_hash plan_hash').split())
EVIDENCE_FIELDS = frozenset(('checked_at local_owner_pids local_processes_drained upstream_result_or_billing_reconciled '
    'run_id parent_stage_hash parent_row_digest inventory_digest').split())
RECEIPT_FIELDS = PLAN_FIELDS | AUTH_FIELDS | {'authorization_ref', 'reconciliation_evidence'}


def _require_allowances(value):
    s._require(all(type(value.get(key)) is int and value[key]==expected for key,expected in (
        ('logical_draft_maximum',2),('physical_maximum_calls',5),('replacement_allowance',1))), 'cache_invalid')


def _eligible(row, snapshot):
    data=row['data'];context=data['context'];diagnostic=data.get('diagnostic',{})
    s._require(s._structured_output(snapshot) and row['stage']=='review' and type(row['attempt']) is int
        and row['attempt']==1 and row['status']=='needs_review' and data['request'].get('stage')=='review'
        and context.get('revision')==s._revision_context() and data.get('failure_code')=='model_call'
        and data['result'] is None and data['result_hash']==s._hash(None)
        and type(diagnostic.get('status_code')) is int and diagnostic['status_code']==408
        and diagnostic.get('codex_reason')=='timeout'
        and not {'recovery_id','logical_task_hash','supersession_id','feedback'}.intersection(data)
        and not {'whole_operation','atomic_budget','partition_revision'}.intersection(context)
        and not {'whole_operation','atomic_budget','partition_revision'}.intersection(data['request']), 'cache_invalid')


def _plan(snapshot, run, parent_stage_hash, *, read_checkpoint, baseline=None,
          atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=()):
    from .semantic_proposal_recovery import _without_packet, _packet_binding, _source_material
    try:
        s._require(all(callable(getattr(read_checkpoint,name,None)) for name in
            ('raw_digest','raw_checkpoint_row','raw_error_code','task_rows','recovery_receipts','supersession_receipts')), 'cache_invalid')
        identity=run['semantic_input_hash']
        parent=s._checkpoint_record(parent_stage_hash,snapshot=snapshot,expected_input_hash=identity,read_checkpoint=read_checkpoint)
        _eligible(parent,snapshot)
        s._require(read_checkpoint.raw_error_code(parent_stage_hash)=='model_call','cache_invalid')
        context=parent['data']['context'];subject_hash=context['proposal_hash']
        subject=s.semantic_attempt_chain(subject_hash,snapshot=snapshot,expected_input_hash=identity,read_checkpoint=read_checkpoint)
        s._require(subject['stage']=='proposal' and subject['status']=='proposed' and subject['attempt']==1
            and subject['data']['context']['revision']==s._revision_context(), 'cache_invalid')
        rows=read_checkpoint.task_rows(identity)
        siblings=[r for r in rows if r['stage'] in {'proposal','review'} and r['data']['context'].get('packet_hash')==context['packet_hash']]
        s._require(sum(r['stage_hash']==parent_stage_hash for r in siblings)==1
            and sum(r['stage_hash']==subject_hash for r in siblings)==1, 'cache_invalid')
        packet_receipts=[r for r in read_checkpoint.recovery_receipts(identity) if r.get('packet_hash')==context['packet_hash']]
        if baseline is None:
            s._require(len(siblings)==2 and not packet_receipts, 'cache_invalid')
        else:
            s._require(len(packet_receipts)==1 and packet_receipts[0].get('policy')==POLICY
                and packet_receipts[0].get('parent_stage_hash')==parent_stage_hash, 'cache_invalid')
        groups=(atomic_budget_chains,whole_histories,partition_repair_pairs)
        for group,length in zip(groups,(4,2,2)):
            s._require(isinstance(group,(tuple,list)) and all(isinstance(item,(tuple,list)) and len(item)==length
                and all(isinstance(k,str) and s.HEX.fullmatch(k) for k in item) and len(set(item))==length for item in group)
                and len({tuple(item) for item in group})==len(group) and list(map(tuple,group))==sorted(map(tuple,group)), 'cache_invalid')
        atomic_budget_chains,whole_histories,partition_repair_pairs=(tuple(map(tuple,group)) for group in groups)
        if baseline is None:
            scoped=_without_packet(read_checkpoint,identity,context['packet_hash'])
            materialized=s.SemanticCheckpointRows(scoped.task_rows(identity));materialized.read_checkpoint=scoped
            s.semantic_task_inventory(materialized,snapshot=snapshot,expected_input_hash=identity,
                atomic_budget_chains=atomic_budget_chains,whole_histories=whole_histories,partition_repair_pairs=partition_repair_pairs)
        source,forest,packet,dependencies=_packet_binding(snapshot,run,subject,read_checkpoint,
            atomic_budget_chains=atomic_budget_chains,whole_histories=whole_histories,partition_repair_pairs=partition_repair_pairs)
        candidate=subject['data']['result'];proposal_context=subject['data']['context']
        claim_ids={claim['id']:'claim:'+s._hash({'packet':context['packet_hash'],'id':claim['id']})[:24] for claim in candidate['claims']}
        review_payload={**packet,'stage':'review','candidate':candidate,'proposal_hash':subject_hash,'packet_hash':context['packet_hash']}
        s._require(parent['data']['request']==review_payload
            and context=={**proposal_context,'proposal_hash':subject_hash,'claim_ids':claim_ids},'cache_invalid')
        source_identity=_source_material(source)
        plan={'policy':POLICY,'parent_stage_hash':parent_stage_hash,'logical_task_hash':parent_stage_hash,
            'parent_failure_digest':read_checkpoint.raw_digest(parent_stage_hash),'subject_proposal_hash':subject_hash,
            'subject_proposal_digest':read_checkpoint.raw_digest(subject_hash),'subject_result_hash':subject['data']['result_hash'],
            'input_hash':identity,'run_id':run['run_id'],'snapshot_id':run['snapshot_id'],'project_id':snapshot['project']['id'],
            'project_revision':run['project_revision'],'vault_identity':s._hash(snapshot.get('vault_path')),
            'generator_hash':s._semantic_generator_hash(snapshot),'destination_digest':s._hash(s.provider_destination(snapshot['provider'])),
            'system_digest':s._hash(s.semantic_stage_system(snapshot,'review',review_payload)),
            'wire_digest':s._hash(s._json(review_payload)),'schema_digest':s._hash(s.semantic_output_schema('review',review_payload)),
            'budget_profile_digest':s._hash(context['budget_profile']),'packet_hash':context['packet_hash'],
            'source_packet_digest':s._hash({'source':source_identity,'packet':packet}),
            'source_dependency_digest':s._hash({'source':source_identity,'frontier':forest['terminal_candidate'],'raw_dependencies':dependencies}),
            'inventory_digest':baseline if baseline is not None else s._hash(sorted([list(read_checkpoint.raw_checkpoint_row(r['stage_hash'])) for r in rows],key=lambda r:r[0])),
            'logical_draft_maximum':2,'physical_maximum_calls':5,'replacement_allowance':1,
            'atomic_budget_chains':[list(x) for x in atomic_budget_chains], 'whole_histories':[list(x) for x in whole_histories],
            'partition_repair_pairs':[list(x) for x in partition_repair_pairs]}
        s._require(plan['parent_failure_digest'] is not None and plan['subject_proposal_digest'] is not None
            and snapshot['project']['revision']==run['project_revision'],'cache_invalid')
        plan['recovery_id']=s._hash({k:v for k,v in plan.items() if k!='inventory_digest'})
        plan['child_stage_hash']=s._hash({'policy':POLICY,'recovery_id':plan['recovery_id'],'parent_stage_hash':parent_stage_hash})
        plan['plan_hash']=s._hash(plan)
        return plan
    except s.SemanticValidationError:
        raise
    except (KeyError,TypeError,ValueError,AttributeError,StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def review_timeout_recovery_plan(snapshot, run, parent_stage_hash, *, read_checkpoint,
        atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=()):
    return _plan(snapshot,run,parent_stage_hash,read_checkpoint=read_checkpoint,atomic_budget_chains=atomic_budget_chains,
        whole_histories=whole_histories,partition_repair_pairs=partition_repair_pairs)


def authenticate_review_timeout_recovery(row, *, snapshot, expected_input_hash, read_checkpoint):
    from .semantic_proposal_recovery import _recorded_evidence
    from .semantic_review_exception import _bundle_if_present, _proof_parts
    bundle = _bundle_if_present(snapshot,expected_input_hash,read_checkpoint)
    if bundle is not None:
        _, _, read_checkpoint = _proof_parts(bundle,read_checkpoint,snapshot,expected_input_hash)
    try:
        matches=[item for item in read_checkpoint.recovery_receipts(expected_input_hash) if item['child_stage_hash']==row['stage_hash']]
        s._require(len(matches)==1,'cache_invalid')
        receipt=matches[0]
        s._require(set(receipt) in (RECEIPT_FIELDS|{'consumed_at','dispatch_id'}, RECEIPT_FIELDS|{'consumed_at','dispatch_id','authority_event'}) and receipt['policy']==POLICY,'cache_invalid')
        _require_allowances(receipt)
        run={key:receipt[key] for key in ('run_id','snapshot_id','project_revision')};run['semantic_input_hash']=expected_input_hash
        plan=_plan(snapshot,run,receipt['parent_stage_hash'],read_checkpoint=read_checkpoint,baseline=receipt['inventory_digest'],
            atomic_budget_chains=receipt['atomic_budget_chains'],whole_histories=receipt['whole_histories'],partition_repair_pairs=receipt['partition_repair_pairs'])
        s._require({key:receipt[key] for key in PLAN_FIELDS}==plan and receipt['operator_id']=='root'
            and receipt['authorization_source']=='user-fix-and-continue' and type(receipt['max_uses']) is int and receipt['max_uses']==1
            and type(receipt['authorized_at']) is int and type(receipt['expires_at']) is int
            and receipt['expires_at']-receipt['authorized_at']==3600
            and all(isinstance(receipt[key],str) and s.HEX.fullmatch(receipt[key]) for key in ('authorization_ref','reconciliation_hash','inventory_digest')),'cache_invalid')
        _recorded_evidence(receipt)
        authority=_resolved_review_authority(receipt,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
        parent=read_checkpoint(receipt['parent_stage_hash']);data=row['data']
        s._require(row['stage']=='review' and type(row['attempt']) is int and row['attempt']==1 and row['input_hash']==expected_input_hash
            and row['status'] in {'planned','running','received','accepted','needs_review'}
            and data.get('recovery_id')==receipt['recovery_id'] and data.get('logical_task_hash')==receipt['logical_task_hash']
            and 'supersession_id' not in data and data['request']==parent['data']['request'] and data['context']==parent['data']['context'], 'cache_invalid')
        if row['status']=='planned':
            s._require(receipt['consumed_at'] is None and receipt['dispatch_id'] is None,'cache_invalid')
        else:
            s._require(type(receipt['consumed_at']) is int and authority['authorized_at']<=receipt['consumed_at']<authority['expires_at']
                and isinstance(receipt['dispatch_id'],str) and s.HEX.fullmatch(receipt['dispatch_id']), 'cache_invalid')
        return receipt
    except s.SemanticValidationError:
        raise
    except (KeyError,TypeError,ValueError,AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def effective_review(logical_review_hash, *, snapshot, expected_input_hash, read_checkpoint):
    from .semantic_review_exception import _bundle_if_present, _proof_parts
    bundle = _bundle_if_present(snapshot,expected_input_hash,read_checkpoint)
    if bundle is not None:
        plan, receipts, _ = _proof_parts(bundle,read_checkpoint,snapshot,expected_input_hash)
        for member,receipt in zip(plan['members'],receipts):
            if logical_review_hash in {member['logical_review_hash'],member['parent_stage_hash']}:
                return member['child_stage_hash'],receipt
    parent=s._checkpoint_record(logical_review_hash,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
    s._require(parent['stage']=='review','cache_invalid')
    receipts=[item for item in read_checkpoint.recovery_receipts(expected_input_hash) if item['parent_stage_hash']==logical_review_hash]
    if not receipts:
        return logical_review_hash,None
    s._require(len(receipts)==1 and receipts[0]['policy']==POLICY,'cache_invalid')
    child=read_checkpoint(receipts[0]['child_stage_hash'])
    s._require(child is not None,'cache_invalid')
    authenticated=authenticate_review_timeout_recovery(child,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
    return child['stage_hash'],authenticated


def _prepare_authorization(service, snapshot, run, plan, *, source_material):
    """Validate complete current semantics outside the writer and seal one attempt."""
    from ._prepared_mutation import _capture_dynamic, _scope, _mutation_stamp, _PreparedMutation, _SEAL
    from ._source_material import bind_source_material
    s._require(set(plan) == PLAN_FIELDS and plan['policy'] == POLICY, 'cache_invalid')
    _require_allowances(plan)
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
        authenticate_review_timeout_recovery(reader(plan['child_stage_hash']), snapshot=snapshot,
            expected_input_hash=plan['input_hash'], read_checkpoint=reader)
        s.semantic_task_inventory(records, snapshot=snapshot, expected_input_hash=plan['input_hash'],
            atomic_budget_chains=tuple(map(tuple,plan['atomic_budget_chains'])),
            whole_histories=tuple(map(tuple,plan['whole_histories'])),
            partition_repair_pairs=tuple(map(tuple,plan['partition_repair_pairs'])))
    else:
        s._require(review_timeout_recovery_plan(snapshot,run,plan['parent_stage_hash'],read_checkpoint=reader,
            atomic_budget_chains=tuple(tuple(item) for item in plan['atomic_budget_chains']), whole_histories=tuple(tuple(item) for item in plan['whole_histories']),
            partition_repair_pairs=tuple(tuple(item) for item in plan['partition_repair_pairs'])) == plan, 'cache_invalid')
    return _PreparedMutation('authorize_review_recovery',_scope(snapshot,run),
        (plan['parent_stage_hash'],plan['plan_hash']),dynamic,_mutation_stamp(),source_material,_seal=_SEAL)

def authorize_review_timeout_recovery(service, snapshot, run, plan, *, authorization,
                                        source_material=None, prepared_mutation=None):
    """Authorize once after lock-free Source and dynamic preparation."""
    from copy import deepcopy
    import json
    from . import semantic_recovery as r
    from .semantic_proposal_recovery import _check_grant
    from ._prepared_mutation import _consume_prepared, _assert_authorization_delta, _fresh_packet_occupied, _AuthorizationPeerDrift
    from ._source_material import prepare_source_material
    s._require(set(plan) == PLAN_FIELDS and plan['policy'] == POLICY, 'cache_invalid')
    _require_allowances(plan)
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
                _consume_prepared(prepared_mutation, operation='authorize_review_recovery', snapshot=snapshot, run=run,
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
                s._require(task_row[:7]==(plan['child_stage_hash'],'review',plan['input_hash'],'planned',1,s._json(expected_data),None)
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
                task_row=(plan['child_stage_hash'],'review',plan['input_hash'],'planned',1,s._json(data),None,stamp)
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


# Explicit one-time renewal of an expired, unused ordinary review authority.
REAUTH_POLICY = 'operator-expired-unused-review-authority-once-v1'
REAUTH_PLAN_FIELDS = frozenset(('policy run_id snapshot_id project_id project_revision input_hash parent_stage_hash '
    'child_stage_hash recovery_id original_plan_hash original_data_digest original_row_digest original_receipt_hash '
    'child_raw_digest source_packet_digest source_dependency_digest current_inventory_digest scope_digest '
    'logical_draft_maximum physical_maximum_calls replacement_allowance event_limit plan_hash').split())


def _require_reauthorization_plan(plan):
    s._require(type(plan) is dict and set(plan)==REAUTH_PLAN_FIELDS and plan.get('policy')==REAUTH_POLICY
        and all(type(plan.get(key)) is int and plan[key]==expected for key,expected in (
            ('logical_draft_maximum',2),('physical_maximum_calls',5),('replacement_allowance',1),('event_limit',1)))
        and type(plan.get('plan_hash')) is str and s.HEX.fullmatch(plan['plan_hash'])
        and plan['plan_hash']==s._hash({k:v for k,v in plan.items() if k!='plan_hash'}), 'cache_invalid')


def _reauthorization_inventory(capture, input_hash):
    import base64, math
    from ._prepared_mutation import _DynamicCapture, _SEAL
    s._require(type(capture) is _DynamicCapture and capture._DynamicCapture__seal is _SEAL
        and capture._DynamicCapture__input == input_hash, 'cache_invalid')
    def cell(value):
        if value is None: return ['null', None]
        if type(value) is int: return ['integer', value]
        if type(value) is float:
            s._require(math.isfinite(value), 'cache_invalid')
            return ['real', value.hex()]
        if type(value) is str: return ['text', value]
        if type(value) is bytes: return ['blob', base64.b64encode(value).decode('ascii')]
        s._require(False, 'cache_invalid')
    tables = []
    for name, content in capture._DynamicCapture__tables:
        tables.append([name, None if content is None else [list(content[0]), [[cell(v) for v in row] for row in content[1]]]])
    s._require([v[0] for v in tables] == ['business_semantic_tasks','business_semantic_recoveries','business_semantic_supersessions'], 'cache_invalid')
    return s._hash({'tables': tables})


def _reauthorization_scope(snapshot, run):
    stable = {'project': {'id': snapshot['project']['id'], 'revision': snapshot['project']['revision']},
        'provider': {k:snapshot.get('provider', {}).get(k) for k in ('id','kind','base_url','model')},
        'vault_path':snapshot.get('vault_path'), 'policies':snapshot.get('policies', {}),
        'budget_profile':s.budget_profile(snapshot), 'generator_hash':s._semantic_generator_hash(snapshot)}
    return s._hash({'project_id':snapshot['project']['id'], 'run_id':run['run_id'], 'snapshot_id':run['snapshot_id'],
        'project_revision':run['project_revision'], 'input_hash':run['semantic_input_hash'], 'snapshot':stable})


def review_authority_reauthorization_plan(snapshot, run, child_stage_hash, *, read_checkpoint, original_row, dynamic_capture):
    import hashlib, time
    from .semantic_recovery import receipt_hash, _evidence_json
    from ._prepared_mutation import _DynamicCapture
    try:
        s._require(type(original_row) is tuple and len(original_row)==7 and type(original_row[4]) is str
            and len(original_row[4].encode('utf-8')) <= 262144 and original_row[5:] == (None,None)
            and type(dynamic_capture) is _DynamicCapture, 'cache_invalid')
        captured = dict(dynamic_capture._DynamicCapture__tables).get('business_semantic_recoveries')
        s._require(captured is not None and captured[0]==('recovery_id','parent_stage_hash','child_stage_hash','input_hash','data','consumed_at','dispatch_id')
            and sum(row==original_row for row in captured[1])==1, 'cache_invalid')
        from ._source_material import _capture
        _capture(read_checkpoint)
        tasks=dict(dynamic_capture._DynamicCapture__tables).get('business_semantic_tasks')
        s._require(tasks is not None and sum(item == read_checkpoint.raw_checkpoint_row(child_stage_hash) for item in tasks[1])==1,'cache_invalid')
        raw=original_row[4].encode('utf-8'); old=_evidence_json(raw)
        s._require(type(old) is dict and set(old)==RECEIPT_FIELDS and original_row[:4]==tuple(old[k] for k in
            ('recovery_id','parent_stage_hash','child_stage_hash','input_hash')) and old['child_stage_hash']==child_stage_hash, 'cache_invalid')
        child=read_checkpoint(child_stage_hash)
        checked=authenticate_review_timeout_recovery(child,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],read_checkpoint=read_checkpoint)
        s._require(checked=={**old,'consumed_at':None,'dispatch_id':None} and child['status']=='planned'
            and time.time()>=old['expires_at'] and run['run_id']==old['run_id'] and run['snapshot_id']==old['snapshot_id']
            and run['project_revision']==old['project_revision'] and snapshot['project']['id']==old['project_id'], 'cache_invalid')
        plan={'policy':REAUTH_POLICY,'run_id':run['run_id'],'snapshot_id':run['snapshot_id'],'project_id':old['project_id'],
            'project_revision':run['project_revision'],'input_hash':run['semantic_input_hash'],'parent_stage_hash':old['parent_stage_hash'],
            'child_stage_hash':child_stage_hash,'recovery_id':old['recovery_id'],'original_plan_hash':old['plan_hash'],
            'original_data_digest':hashlib.sha256(raw).hexdigest(),'original_row_digest':s._hash(list(original_row)),
            'original_receipt_hash':receipt_hash(checked),'child_raw_digest':read_checkpoint.raw_digest(child_stage_hash),
            'source_packet_digest':old['source_packet_digest'],'source_dependency_digest':old['source_dependency_digest'],
            'current_inventory_digest':_reauthorization_inventory(dynamic_capture,run['semantic_input_hash']),
            'scope_digest':_reauthorization_scope(snapshot,run),'logical_draft_maximum':2,'physical_maximum_calls':5,
            'replacement_allowance':1,'event_limit':1}
        plan['plan_hash']=s._hash(plan)
        s._require(set(plan)==REAUTH_PLAN_FIELDS,'cache_invalid')
        return plan
    except s.SemanticValidationError: raise
    except (KeyError,TypeError,ValueError,AttributeError,UnicodeError):
        raise s.SemanticValidationError('cache_invalid') from None

REAUTH_EVIDENCE_FIELDS = frozenset(('checked_at local_owner_pids local_processes_drained upstream_result_or_billing_reconciled '
    'zero_outbound nonambiguous run_id parent_stage_hash child_stage_hash original_row_digest child_raw_digest '
    'current_inventory_digest plan_hash').split())
AUTHORITY_EVENT_FIELDS = frozenset(('version plan original_row original_row_digest original_data_digest original_evidence '
    'grant_raw evidence_raw authorization_ref event_hash').split())


def _reauthorization_grant(raw_grant, raw_evidence, plan, *, fresh):
    import hashlib, os, time
    from pathlib import Path
    from .semantic_recovery import _evidence_json
    grant=_evidence_json(raw_grant); evidence=_evidence_json(raw_evidence)
    s._require(len(raw_grant)<=16384 and len(raw_evidence)<=16384 and type(grant) is dict and set(grant)==AUTH_FIELDS
        and type(evidence) is dict and set(evidence)==REAUTH_EVIDENCE_FIELDS
        and all(grant[k]==plan[k] for k in ('policy','plan_hash','parent_stage_hash','run_id'))
        and grant['operator_id']=='root' and grant['authorization_source']=='user-fix-and-continue'
        and type(grant['max_uses']) is int and grant['max_uses']==1
        and type(grant['authorized_at']) is int and type(grant['expires_at']) is int
        and grant['authorized_at']>=0 and grant['expires_at']-grant['authorized_at']==3600
        and type(grant['reconciliation_ref']) is str and Path(grant['reconciliation_ref']).is_absolute()
        and len(grant['reconciliation_ref'].encode('utf-8'))<=4096 and '\x00' not in grant['reconciliation_ref']
        and grant['reconciliation_hash']==hashlib.sha256(raw_evidence).hexdigest()
        and type(evidence['checked_at']) is int and evidence['checked_at']>=0
        and 0<=grant['authorized_at']-evidence['checked_at']<=60
        and evidence['local_processes_drained'] is True and evidence['zero_outbound'] is True
        and evidence['nonambiguous'] is True and evidence['upstream_result_or_billing_reconciled'] is False
        and all(evidence[k]==plan[k] for k in ('run_id','parent_stage_hash','child_stage_hash','original_row_digest','child_raw_digest','current_inventory_digest','plan_hash')), 'cache_invalid')
    pids=evidence['local_owner_pids']
    s._require(type(pids) is list and pids and all(type(pid) is int and pid>0 for pid in pids) and len(set(pids))==len(pids),'cache_invalid')
    if fresh:
        now=time.time()
        s._require(0<=now-evidence['checked_at']<=60 and grant['authorized_at']<=now<grant['expires_at'],'cache_invalid')
        for pid in pids:
            try: os.kill(pid,0)
            except ProcessLookupError: continue
            s._require(False,'cache_invalid')
    return grant,evidence


def _resolved_review_authority(receipt, *, snapshot, expected_input_hash, read_checkpoint):
    import hashlib
    from .semantic_recovery import _evidence_json, receipt_hash
    try:
        if 'authority_event' not in receipt:
            return {'version':1,'authorization_ref':receipt['authorization_ref'],'authorized_at':receipt['authorized_at'],
                'expires_at':receipt['expires_at'],'reconciliation_ref':receipt['reconciliation_ref'],
                'reconciliation_hash':receipt['reconciliation_hash'],'reconciliation_evidence':receipt['reconciliation_evidence'],'event_hash':None}
        event=receipt['authority_event'];s._require(type(event) is dict and set(event)==AUTHORITY_EVENT_FIELDS
            and type(event['version']) is int and event['version']==1 and event['event_hash']==s._hash({k:v for k,v in event.items() if k!='event_hash'}),'cache_invalid')
        plan=event['plan'];row=event['original_row']
        s._require(type(plan) is dict and set(plan)==REAUTH_PLAN_FIELDS and plan['policy']==REAUTH_POLICY
            and plan['plan_hash']==s._hash({k:v for k,v in plan.items() if k!='plan_hash'})
            and all(type(plan[k]) is int and plan[k]==v for k,v in (('logical_draft_maximum',2),('physical_maximum_calls',5),('replacement_allowance',1),('event_limit',1)))
            and type(row) is list and len(row)==7 and type(row[4]) is str and row[5:]==[None,None]
            and len(row[4].encode('utf-8'))<=262144 and event['original_row_digest']==s._hash(row)==plan['original_row_digest']
            and event['original_data_digest']==hashlib.sha256(row[4].encode('utf-8')).hexdigest()==plan['original_data_digest'],'cache_invalid')
        old=_evidence_json(row[4].encode('utf-8'))
        s._require(type(old) is dict and set(old)==RECEIPT_FIELDS and all(s._json(receipt[k])==s._json(old[k]) for k in RECEIPT_FIELDS)
            and row[:4]==[old[k] for k in ('recovery_id','parent_stage_hash','child_stage_hash','input_hash')]
            and event['original_evidence']==old['reconciliation_evidence'] and plan['original_receipt_hash']==receipt_hash(old)
            and all(plan[k]==old[k] for k in ('run_id','snapshot_id','project_id','project_revision','input_hash','parent_stage_hash','child_stage_hash','recovery_id','source_packet_digest','source_dependency_digest'))
            and plan['original_plan_hash']==old['plan_hash'] and expected_input_hash==plan['input_hash'],'cache_invalid')
        hash_fields=('input_hash','parent_stage_hash','child_stage_hash','recovery_id','original_plan_hash','original_data_digest','original_row_digest','original_receipt_hash','child_raw_digest','source_packet_digest','source_dependency_digest','current_inventory_digest','scope_digest','plan_hash')
        s._require(all(type(plan[k]) is str and s.HEX.fullmatch(plan[k]) for k in hash_fields),'cache_invalid')
        child=read_checkpoint(plan['child_stage_hash'])
        s._require(child is not None and (child['status']!='planned' or read_checkpoint.raw_digest(plan['child_stage_hash'])==plan['child_raw_digest']),'cache_invalid')
        run={'run_id':plan['run_id'],'snapshot_id':plan['snapshot_id'],'project_revision':plan['project_revision'],'semantic_input_hash':plan['input_hash']}
        s._require(plan['scope_digest']==_reauthorization_scope(snapshot,run) and type(event['grant_raw']) is str and type(event['evidence_raw']) is str,'cache_invalid')
        raw_grant=event['grant_raw'].encode('utf-8');raw_evidence=event['evidence_raw'].encode('utf-8')
        grant,evidence=_reauthorization_grant(raw_grant,raw_evidence,plan,fresh=False)
        s._require(event['authorization_ref']==hashlib.sha256(raw_grant).hexdigest() and grant['authorized_at']>=old['expires_at']
            and grant['reconciliation_ref']!=old['reconciliation_ref'] and event['authorization_ref']!=old['authorization_ref'],'cache_invalid')
        return {'version':2,'authorization_ref':event['authorization_ref'],'authorized_at':grant['authorized_at'],
            'expires_at':grant['expires_at'],'reconciliation_ref':grant['reconciliation_ref'],
            'reconciliation_hash':grant['reconciliation_hash'],'reconciliation_evidence':event['evidence_raw'],'event_hash':event['event_hash']}
    except s.SemanticValidationError: raise
    except (KeyError,TypeError,ValueError,AttributeError,UnicodeError): raise s.SemanticValidationError('cache_invalid') from None


def effective_review_authority(receipt, *, snapshot, expected_input_hash, read_checkpoint):
    """Return only the effective window of a fully authenticated review receipt."""
    try:
        s._require(type(receipt) is dict, 'cache_invalid')
        child=read_checkpoint(receipt['child_stage_hash'])
        authenticated=authenticate_review_timeout_recovery(child,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
        s._require(s._json(authenticated)==s._json(receipt),'cache_invalid')
        return _resolved_review_authority(authenticated,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
    except s.SemanticValidationError: raise
    except (KeyError,TypeError,ValueError,AttributeError): raise s.SemanticValidationError('cache_invalid') from None


def _selected_receipt_row(db, child):
    rows=db.execute('SELECT recovery_id,parent_stage_hash,child_stage_hash,input_hash,data,consumed_at,dispatch_id FROM business_semantic_recoveries WHERE child_stage_hash=?',(child,)).fetchall()
    s._require(len(rows)==1,'cache_invalid')
    return tuple(rows[0])


def prepare_review_authority_reauthorization(service, snapshot, run, plan, *, source_material):
    from ._prepared_mutation import _capture_dynamic,_scope,_mutation_stamp,_PreparedMutation,_SEAL
    from ._source_material import bind_source_material
    from .semantic_recovery import _current_context
    _require_reauthorization_plan(plan)
    service.project_analysis.guard(snapshot)
    with service.store._db() as db:
        db.execute('BEGIN');_current_context(db,snapshot,run)
        records=s.semantic_checkpoint_rows(db,run['semantic_input_hash'])
        row=_selected_receipt_row(db,plan['child_stage_hash']);dynamic=_capture_dynamic(db,run['semantic_input_hash'])
    service.project_analysis.guard(snapshot)
    records.read_checkpoint=bind_source_material(source_material,snapshot,run,read_checkpoint=records.read_checkpoint)
    rebuilt=review_authority_reauthorization_plan(snapshot,run,plan['child_stage_hash'],read_checkpoint=records.read_checkpoint,
        original_row=row,dynamic_capture=dynamic)
    s._require(s._json(rebuilt)==s._json(plan),'cache_invalid')
    old=__import__('json').loads(row[4]);s.semantic_task_inventory(records,snapshot=snapshot,expected_input_hash=run['semantic_input_hash'],
        atomic_budget_chains=tuple(map(tuple,old['atomic_budget_chains'])),whole_histories=tuple(map(tuple,old['whole_histories'])),
        partition_repair_pairs=tuple(map(tuple,old['partition_repair_pairs'])))
    return _PreparedMutation('reauthorize_review_recovery',_scope(snapshot,run),
        (plan['child_stage_hash'],plan['plan_hash'],plan['original_row_digest'],s._hash(plan)),dynamic,_mutation_stamp(),source_material,_seal=_SEAL)


def reauthorize_expired_unused_review_recovery(service, snapshot, run, plan, *, authorization, source_material, prepared_mutation):
    import hashlib,time
    from pathlib import Path
    from . import semantic_recovery as r
    from .semantic_proposal_recovery import _private_evidence_bytes
    from ._prepared_mutation import _consume_prepared,_capture_dynamic,_fresh_packet_occupied,_AuthorizationPeerDrift
    from ._source_material import _bind_raw_source
    from .semantic_recovery import _evidence_json
    from copy import deepcopy
    _require_reauthorization_plan(plan)
    plan=deepcopy(plan)
    s._require(source_material is not None and prepared_mutation is not None,'cache_invalid')
    def read_grant(old):
        grant_raw=_private_evidence_bytes(authorization);grant=_evidence_json(grant_raw)
        s._require(type(grant) is dict and 'reconciliation_ref' in grant,'cache_invalid')
        evidence_raw=_private_evidence_bytes(grant['reconciliation_ref'])
        parsed,_=_reauthorization_grant(grant_raw,evidence_raw,plan,fresh=True)
        paths=(str(Path(authorization)),parsed['reconciliation_ref'],old['reconciliation_ref'])
        s._require(all(Path(p).is_absolute() for p in paths) and len(set(paths))==3
            and hashlib.sha256(grant_raw).hexdigest()!=old['authorization_ref'],'cache_invalid')
        return grant_raw,evidence_raw,parsed
    with service._exclusive(snapshot['project']['id']):
        service.project_analysis.guard(snapshot)
        s._require(service._controls(run).get('paused') is True and service._controls(run).get('cancelled',False) is False,'cache_invalid')
        with service.store._lock,service.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                _consume_prepared(prepared_mutation,operation='reauthorize_review_recovery',snapshot=snapshot,run=run,
                    target=(plan['child_stage_hash'],plan['plan_hash'],plan['original_row_digest'],s._hash(plan)),material=source_material,db=db)
            except _AuthorizationPeerDrift:
                raise s.SemanticValidationError('cache_invalid') from None
            r._database_controls(db,run,paused=True);service.project_analysis.guard(snapshot)
            s._require((plan['parent_stage_hash'],plan['child_stage_hash']) in s.BusinessSemanticIntegration.review_timeout_recovery_allowlist,'cache_invalid')
            row=_selected_receipt_row(db,plan['child_stage_hash']);old=_evidence_json(row[4].encode('utf-8'))
            s._require(row[5:]==(None,None) and s._hash(list(row))==plan['original_row_digest'] and time.time()>=old['expires_at'],'cache_invalid')
            latest=db.execute('SELECT id FROM full_analysis_runs WHERE project_id=? AND snapshot_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 1',(snapshot['project']['id'],run['snapshot_id'])).fetchone()
            s._require(latest==(run['run_id'],) and 2<=_fresh_packet_occupied(db,run['semantic_input_hash'],old['packet_hash'])<=5,'cache_invalid')
            grant_raw,evidence_raw,grant=read_grant(old)
            event={'version':1,'plan':plan,'original_row':list(row),'original_row_digest':plan['original_row_digest'],
                'original_data_digest':plan['original_data_digest'],'original_evidence':old['reconciliation_evidence'],
                'grant_raw':grant_raw.decode('utf-8'),'evidence_raw':evidence_raw.decode('utf-8'),
                'authorization_ref':hashlib.sha256(grant_raw).hexdigest()}
            event['event_hash']=s._hash(event);new_data=s._json({**old,'authority_event':event})
            s._require(len(new_data.encode('utf-8'))<=524288,'cache_invalid')
            updated=db.execute('UPDATE business_semantic_recoveries SET data=? WHERE recovery_id=? AND parent_stage_hash=? AND child_stage_hash=? AND input_hash=? AND data=? AND consumed_at IS NULL AND dispatch_id IS NULL',(new_data,*row[:4],row[4])).rowcount
            s._require(updated==1,'cache_invalid')
            before=dict(prepared_mutation._PreparedMutation__dynamic._DynamicCapture__tables)
            after=dict(_capture_dynamic(db,run['semantic_input_hash'])._DynamicCapture__tables)
            for table in before:
                if table!='business_semantic_recoveries': s._require(before[table]==after[table],'cache_invalid');continue
                old_table=before[table];new_table=after[table]
                s._require(old_table is not None and new_table is not None and old_table[0]==new_table[0] and len(old_table[1])==len(new_table[1]),'cache_invalid')
                expected=tuple(item[:4]+(new_data,)+item[5:] if item==row else item for item in old_table[1])
                s._require(new_table[1]==expected,'cache_invalid')
            _bind_raw_source(source_material,snapshot,run,db=db);r._database_controls(db,run,paused=True);service.project_analysis.guard(snapshot)
            s._require(read_grant(old)==(grant_raw,evidence_raw,grant) and time.time()>=old['expires_at']
                and (plan['parent_stage_hash'],plan['child_stage_hash']) in s.BusinessSemanticIntegration.review_timeout_recovery_allowlist,'cache_invalid')
    return {**old,'authority_event':event,'consumed_at':None,'dispatch_id':None}
