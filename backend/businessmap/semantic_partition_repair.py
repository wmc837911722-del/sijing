"""Read-only authentication for bounded partition-repair ancestry."""
from . import business_semantics as s

CONTRACT = 'bounded-partition-repair-v1'
REPAIR_SYSTEM_SUFFIX = ('分区修正仅依据原始事实重新分组；prior_children 和 partition_feedback 是待核对的历史分组及评审意见，'
                        '不是新增业务事实。保留全部事实及条件，不为通过评审编造内容。')


def authenticate_parent(snapshot, expected_input_hash, *, read_checkpoint,
                        parent_subdivision_hash, parent_review_hash):
    """Authenticate only the two original records; does not establish SOURCE or frontier."""
    try:
        sub = s._checkpoint_record(parent_subdivision_hash, snapshot=snapshot,
            expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
        review = s._checkpoint_record(parent_review_hash, snapshot=snapshot,
            expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
        s._require(callable(getattr(read_checkpoint, 'raw_error_code', None))
            and read_checkpoint.raw_error_code(parent_subdivision_hash) is None
            and read_checkpoint.raw_error_code(parent_review_hash) == 'review_rejected', 'cache_invalid')
        sd, rd = sub['data'], review['data']
        sr, sc, rr, rc = sd['request'], sd['context'], rd['request'], rd['context']
        s._require(sub['stage'] == 'subdivide' and sub['status'] == 'accepted' and sub['attempt'] == 1
            and review['stage'] == 'partition_review' and review['status'] == 'needs_review' and review['attempt'] == 1
            and rd.get('failure_code') == 'review_rejected' and rd.get('diagnostic', {}) == {} and rd['result'] is None
            and 'partition_revision' not in sr and 'partition_revision' not in sc
            and 'partition_revision' not in rr and 'partition_revision' not in rc
            and rc['subdivision_hash'] == parent_subdivision_hash
            and sc['budget_profile'] == rc['budget_profile'], 'cache_invalid')
        parent, children = rc['parent_operation'], sd['result']
        s._require(isinstance(parent, dict) and parent == s._operation_membership(parent)
            and isinstance(children, list) and len(children) > 1
            and all(isinstance(child, dict) and child == s._operation_membership(child) for child in children), 'cache_invalid')
        facts = parent['fact_ids']
        s._require(isinstance(facts, list) and facts and len(facts) == len(set(facts))
            and [fid for child in children for fid in child['fact_ids']]
                and len([fid for child in children for fid in child['fact_ids']]) == len(facts)
            and set(fid for child in children for fid in child['fact_ids']) == set(facts)
            and all(child['aliases'] == parent['aliases'] for child in children), 'cache_invalid')
        for child in children:
            s._require(isinstance(child['title'], str) and child['title'].strip() == child['title']
                and 0 < len(child['title']) <= 60 and child['category'] in s.CATEGORIES
                and child['id'] == 'operation:' + s._hash({'title': child['title'], 'category': child['category'],
                                                           'members': sorted(child['fact_ids'])})[:20], 'cache_invalid')
            s._public(child['title'])
        s._require(len({child['id'] for child in children}) == len(children), 'cache_invalid')
        s._require(sc['label_members'] == {'l'+str(i): [fid] for i, fid in enumerate(facts)}
            and sr['membership_hash'] == s._hash(sc['label_members'])
            and sr['stage'] == 'subdivide' and sr['project'] == snapshot['project']['name']
            and [item['label_id'] for item in sr['items']] == ['l'+str(i) for i in range(len(facts))]
            and rc['input_ids'] == {'f'+str(i): fid for i, fid in enumerate(facts)}
            and rc['operation_ids'] == {'o'+str(i): child['id'] for i, child in enumerate(children)}
            and rc['partition_hash'] == s._hash({'parent': parent, 'children': children}), 'cache_invalid')
        inverse = {fid: key for key, fid in rc['input_ids'].items()}
        def wire(op, key):
            return {'operation_id': key, 'title': op['title'], 'category': op['category'],
                'fact_ids': [inverse[fid] for fid in op['fact_ids']]}
        s._require(rr['stage'] == 'partition_review' and rr['project'] == snapshot['project']['name']
            and rr['parent'] == wire(parent, 'p0')
            and rr['children'] == [wire(child, 'o'+str(i)) for i, child in enumerate(children)]
            and rr['membership_hash'] == rc['partition_hash']
            and [item['fact_id'] for item in rr['items']] == ['f'+str(i) for i in range(len(facts))], 'cache_invalid')
        issues = rd['issues']
        s._require(isinstance(issues, list) and issues and all(isinstance(issue, dict)
            and issue.get('code') in s.ISSUES - {'privacy'} and isinstance(issue.get('input_ids'), list)
            and all(isinstance(fid, str) and fid in rc['input_ids'] for fid in issue['input_ids'])
            for issue in issues), 'cache_invalid')
        return {'contract': CONTRACT, 'draft_index': 2, 'parent_subdivision_hash': parent_subdivision_hash,
            'parent_review_hash': parent_review_hash, 'parent_partition_hash': rc['partition_hash'],
            'feedback_hash': s._hash({'issues': issues})}
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def partition_repair_plan(snapshot, expected_input_hash, *, read_checkpoint,
                          parent_subdivision_hash, parent_review_hash, allow_running=()):
    """Reconstruct a selected revision; allow_running is structural, never authorization."""
    from copy import deepcopy
    from . import semantic_catalogue_frontier as frontier
    from . import semantic_supersession as sup
    try:
        draft = _draft(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
            parent_subdivision_hash=parent_subdivision_hash, parent_review_hash=parent_review_hash)
        revision = draft['partition_revision']
        parent_sub = read_checkpoint(parent_subdivision_hash)
        parent_review = read_checkpoint(parent_review_hash)
        rd, sd = parent_review['data'], parent_sub['data']
        parent = rd['context']['parent_operation']
        pair = (parent_subdivision_hash, parent_review_hash)
        # The frontier path authenticates persisted SOURCE, receipts, and every page.
        for key in allow_running:
            row = read_checkpoint(key)
            s._require(row is not None and (row['status'] != 'running'
                or row['stage'] in {'subdivide', 'partition_review', 'proposal', 'review'}), 'cache_invalid')
        catalogue = frontier.catalogue_frontier_plan(snapshot, expected_input_hash,
            read_checkpoint=read_checkpoint, partition_repair_pairs=(pair,), allow_running=set(allow_running))
        s._require(parent in catalogue['operations'] and not catalogue['received_to_accept'], 'cache_invalid')
        run, source_units, _ = frontier._source(snapshot, expected_input_hash, read_checkpoint)
        units = frontier._inputs(source_units)
        runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
        runtime._snapshot, runtime._units = snapshot, units
        runtime._partitions = []
        runtime._profile = lambda: deepcopy(sd['context']['budget_profile'])
        labels = [{'title': parent['title'], 'category': parent['category'], 'fact_ids': [fid],
                   'aliases': parent['aliases']} for fid in parent['fact_ids']]
        s._require(len(labels) <= runtime._profile()['catalogue_items'], 'cache_invalid')
        original, _, context = runtime._catalogue_payload(labels, stage='subdivide')
        s._require(original == sd['request'] and sd['context'] == {**context,
            'budget_profile': runtime._profile(), 'generator_hash': s._semantic_generator_hash(snapshot)}, 'cache_invalid')
        _, source, addenda = sup._source_inputs(snapshot, run, read_checkpoint)
        matching = [item for addendum in addenda for item in addendum.get('matching_receipts', [])]
        s._require(rd['request']['matching_receipts'] == matching
            and rd['request']['items'] == [runtime._wire_fact(units[fid], 'f'+str(i))
                                          for i, fid in enumerate(parent['fact_ids'])], 'cache_invalid')
        runtime._matching = matching
        runtime._forbidden = s._unique(token for unit in units.values() for token in unit.get('_forbidden', ()) if isinstance(token, str) and token)
        for child in sd['result']:
            s._public(child['title'], runtime._forbidden)
            s._require(runtime._packet_fits([child]), 'cache_invalid')
        if read_checkpoint(draft['subdivision_hash']) and read_checkpoint(draft['subdivision_hash'])['status'] in {'received', 'accepted'}:
            children = read_checkpoint(draft['subdivision_hash'])['data']['result']
            for child in children:
                s._public(child['title'], runtime._forbidden)
            s._require(not runtime._packet_fits([parent]) and all(runtime._packet_fits([child]) for child in children), 'cache_invalid')
        else:
            s._require(not runtime._packet_fits([parent]), 'cache_invalid')
        draft['occupied_calls'] = catalogue['occupied_calls']
        return draft
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def _draft(snapshot, expected_input_hash, *, read_checkpoint, parent_subdivision_hash, parent_review_hash):
    """Reconstruct exact slots from authenticated parents; never authorizes a call."""
    from copy import deepcopy
    revision = authenticate_parent(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
        parent_subdivision_hash=parent_subdivision_hash, parent_review_hash=parent_review_hash)
    sd, rd = read_checkpoint(parent_subdivision_hash)['data'], read_checkpoint(parent_review_hash)['data']
    parent = rd['context']['parent_operation']
    request = {**deepcopy(sd['request']), 'partition_revision': revision,
               'prior_children': deepcopy(rd['request']['children']),
               'partition_feedback': {'issues': deepcopy(rd['issues'])}}
    context = {**deepcopy(sd['context']), 'partition_revision': revision}
    key = s.semantic_task_identity_hash(snapshot, expected_input_hash, 'subdivide', request, context)
    result = {'contract': CONTRACT, 'status': 'planned', 'blocked_reason': None,
        'partition_revision': revision, 'parent_operation': deepcopy(parent),
        'subdivision_request': request, 'subdivision_context': context, 'subdivision_hash': key,
        'review_request': None, 'review_context': None, 'review_hash': None,
        'required_task_hashes': sorted([parent_subdivision_hash, parent_review_hash]),
        'remaining_maximum_calls': 2}
    row = read_checkpoint(key)
    if not row:
        return result
    row = s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
    s._require(read_checkpoint.raw_error_code(key) == (row['data'].get('failure_code') if row['status'] == 'needs_review' else None), 'cache_invalid')
    s._require(row['stage'] == 'subdivide' and row['attempt'] == 1 and row['data']['request'] == request
        and row['data']['context'] == context, 'cache_invalid')
    result['required_task_hashes'].append(key)
    result['required_task_hashes'].sort()
    status = row['status']
    result['remaining_maximum_calls'] = 2 if status == 'planned' else 1
    if status in {'running', 'needs_review'}:
        result.update(status='blocked', blocked_reason='interrupted' if status == 'running' else row['data'].get('failure_code', 'needs_review'))
        return result
    if status == 'planned':
        return result
    s._require(status in {'accepted', 'received'}, 'cache_invalid')
    children = row['data']['result']
    s._require(isinstance(children, list) and len(children) > 1, 'cache_invalid')
    facts = parent['fact_ids']
    all_facts = []
    for child in children:
        s._require(isinstance(child, dict) and child == s._operation_membership(child)
            and isinstance(child['title'], str) and child['title'].strip() == child['title']
            and 0 < len(child['title']) <= 60 and child['category'] in s.CATEGORIES
            and isinstance(child['fact_ids'], list) and child['fact_ids']
            and child['aliases'] == parent['aliases']
            and child['id'] == 'operation:' + s._hash({'title': child['title'], 'category': child['category'],
                                                       'members': sorted(child['fact_ids'])})[:20], 'cache_invalid')
        all_facts.extend(child['fact_ids'])
    s._require(len(all_facts) == len(facts) and len(set(all_facts)) == len(facts)
        and set(all_facts) == set(facts) and len({child['id'] for child in children}) == len(children), 'cache_invalid')
    inverse = {fid: 'f'+str(i) for i, fid in enumerate(facts)}
    wires = [{'operation_id': 'o'+str(i), 'title': child['title'], 'category': child['category'],
              'fact_ids': [inverse[fid] for fid in child['fact_ids']]} for i, child in enumerate(children)]
    partition_hash = s._hash({'parent': parent, 'children': children})
    review_request = {**deepcopy(rd['request']), 'children': wires,
                      'membership_hash': partition_hash, 'partition_revision': revision}
    review_context = {**deepcopy(rd['context']), 'operation_ids': {'o'+str(i): child['id'] for i, child in enumerate(children)},
                      'subdivision_hash': key, 'partition_hash': partition_hash, 'partition_revision': revision}
    review_key = s.semantic_task_identity_hash(snapshot, expected_input_hash, 'partition_review', review_request, review_context)
    result.update(review_request=review_request, review_context=review_context, review_hash=review_key,
                  status='received' if status == 'received' else 'planned')
    reviewed = read_checkpoint(review_key)
    if not reviewed:
        return result
    reviewed = s._checkpoint_record(review_key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
    s._require(read_checkpoint.raw_error_code(review_key) == (reviewed['data'].get('failure_code') if reviewed['status'] == 'needs_review' else None), 'cache_invalid')
    s._require(status == 'accepted' and reviewed['stage'] == 'partition_review' and reviewed['attempt'] == 1
        and reviewed['data']['request'] == review_request and reviewed['data']['context'] == review_context, 'cache_invalid')
    result['required_task_hashes'].append(review_key)
    result['required_task_hashes'].sort()
    rstatus = reviewed['status']
    result['remaining_maximum_calls'] = 1 if rstatus == 'planned' else 0
    if rstatus in {'running', 'needs_review'}:
        result.update(status='blocked', blocked_reason='interrupted' if rstatus == 'running' else reviewed['data'].get('failure_code', 'needs_review'))
    elif rstatus in {'received', 'accepted'}:
        value = reviewed['data']['result']
        s._require(isinstance(value, dict) and set(value) == {'decision', 'issues', 'parent_checks', *s.PARTITION_CHECKS} and value.get('decision') == 'accept' and value.get('issues') == []
            and isinstance(value.get('parent_checks'), dict) and set(value['parent_checks']) == s.PARTITION_PARENT_CHECKS
            and all(v is True for v in value['parent_checks'].values()), 'cache_invalid')
        for field, (id_field, checks) in s.PARTITION_CHECKS.items():
            expected = set(inverse.values()) if field == 'input_checks' else {'o'+str(i) for i in range(len(children))}
            checks_rows = value.get(field)
            s._require(isinstance(checks_rows, list) and len(checks_rows) == len(expected)
                and all(isinstance(check, dict) and set(check) == {id_field, 'checks'}
                    and isinstance(check['checks'], dict) and set(check['checks']) == checks
                    and all(v is True for v in check['checks'].values()) for check in checks_rows)
                and {check[id_field] for check in checks_rows} == expected, 'cache_invalid')
        result['status'] = 'accepted' if rstatus == status == 'accepted' else 'received'
    else:
        s._require(rstatus == 'planned', 'cache_invalid')
    return result
