"""Source-bound, exact-review-size planning for whole operations; no writes."""
from copy import deepcopy
import json
from . import business_semantics as s

CONTRACT = 'whole-operation-exact-fit-v1'


def authenticate_history(snapshot, expected_input_hash, *, read_checkpoint, keys):
    from .semantic_partition_repair import authenticate_parent
    s._require(isinstance(keys, tuple) and len(keys) == 2 and keys[0] != keys[1]
        and callable(getattr(read_checkpoint, 'raw_checkpoint_row', None)), 'cache_invalid')
    authenticate_parent(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
        parent_subdivision_hash=keys[0], parent_review_hash=keys[1])
    history = []
    for key in keys:
        row = s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
        raw = read_checkpoint.raw_checkpoint_row(key)
        error = row['data'].get('failure_code') if row['status'] == 'needs_review' else None
        s._require(isinstance(raw, tuple) and len(raw) == 8 and isinstance(raw[5], str)
            and tuple(raw[:5]) == tuple(row[k] for k in ('stage_hash','stage','input_hash','status','attempt'))
            and json.loads(raw[5]) == row['data'] and raw[6] == error
            and read_checkpoint.raw_error_code(key) == error, 'cache_invalid')
        history.append({'stage_hash': key, 'raw_digest': s._hash(list(raw))})
    return {'history': history, 'parent_operation': deepcopy(read_checkpoint(keys[1])['data']['context']['parent_operation'])}


def discover_histories(snapshot, expected_input_hash, *, read_checkpoint, selected_history_pairs=()):
    rows = [r for r in read_checkpoint.task_rows(expected_input_hash)
            if r['data']['context'].get('generator_hash') == s._semantic_generator_hash(snapshot)]
    selected = {tuple(pair) for pair in selected_history_pairs}
    whole_rows = [r for r in rows if 'whole_operation' in r['data']['context']]
    candidates = set(selected)
    if whole_rows:
        candidates.update((r['data']['context'].get('subdivision_hash'), r['stage_hash']) for r in rows
            if r['stage']=='partition_review' and r['status']=='needs_review' and not r['data']['context'].get('partition_revision'))
    effective = set(selected)
    for pair in candidates:
        try:
            hist=authenticate_history(snapshot, expected_input_hash, read_checkpoint=read_checkpoint, keys=pair)
        except (s.SemanticValidationError, TypeError, ValueError, KeyError):
            if pair in selected: raise
            continue
        if pair in selected: continue
        parent=hist['parent_operation']
        for page in rows:
            if page['stage']!='catalogue' or page['status'] not in {'accepted','received'} or parent not in (page['data']['result'] or []): continue
            binding={'stage_hash':page['stage_hash'],'result_hash':page['data']['result_hash'],'operation_id':parent['id']}
            lineage={'contract':CONTRACT,'input_hash':expected_input_hash,'generator_hash':s._semantic_generator_hash(snapshot),
                'root_operation_hash':s._hash(parent),'root_binding':binding,'history':hist['history']}
            if any(r['data']['context']['whole_operation']['lineage_hash']==s._hash(lineage) for r in whole_rows):
                effective.add(pair)
    return tuple(sorted(effective))


def _root_fit_binding(snapshot, operations, matching, profile):
    return s._hash({'snapshot': snapshot, 'operations': operations, 'matching': matching,
                    'profile': profile, 'partitions': []})


def _root_fit_data(snapshot, units, matching, operations, profile):
    """Cold pure single-root policy computation; contains no slot decisions."""
    from ._frozen_routing import _routing_copy_metadata
    if not _routing_copy_metadata((snapshot, units, matching, operations, profile)):
        return None
    runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
    runtime._snapshot, runtime._units, runtime._matching, runtime._partitions = snapshot, units, matching, []
    runtime._profile = lambda: deepcopy(profile)
    return {'binding': _root_fit_binding(snapshot, operations, matching, profile),
            'fits': [(s._hash(parent), runtime._packet_fits([parent])) for parent in operations]}


def whole_operation_plans(snapshot, expected_input_hash, *, read_checkpoint, selected_history_pairs=(), atomic_budget_chains=(), allow_running=(), partition_repair_pairs=()):
    from . import semantic_catalogue_frontier as frontier, semantic_supersession as sup
    try:
        s._require(s._structured_output(snapshot) and callable(getattr(read_checkpoint, 'task_rows', None)), 'cache_invalid')
        rows = read_checkpoint.task_rows(expected_input_hash)
        rows = [r for r in rows if r['data']['context'].get('generator_hash') == s._semantic_generator_hash(snapshot)]
        selected = {tuple(pair) for pair in selected_history_pairs}
        s._require(all(len(pair) == 2 for pair in selected), 'cache_invalid')
        # Discover a history only when its lineage is matched by an existing paid
        # whole slot below; mere presence of a rejection is never authorization.
        possible = set(selected)
        whole_rows = [r for r in rows if 'whole_operation' in r['data']['context']]
        if whole_rows:
            for row in rows:
                if row['stage'] == 'partition_review' and row['status'] == 'needs_review' and not row['data']['context'].get('partition_revision'):
                    possible.add((row['data']['context'].get('subdivision_hash'), row['stage_hash']))
        authenticated = {}
        for pair in possible:
            try:
                authenticated[pair] = authenticate_history(snapshot, expected_input_hash, read_checkpoint=read_checkpoint, keys=pair)
            except (s.SemanticValidationError, TypeError, ValueError, KeyError):
                if pair in selected: raise
        # Frontier needs the effective histories before it can admit their rejected
        # rows. Match candidate lineages against the physical catalogue bindings.
        # Binding reconstruction without exemptions is impossible for rejected rows;
        # use the physical parent operation's catalogue binding from its unique page.
        effective = set(selected)
        generator = s._semantic_generator_hash(snapshot)
        for pair, hist in authenticated.items():
            if pair in selected: continue
            parent = hist['parent_operation']
            for page in rows:
                if page['stage'] != 'catalogue' or page['status'] not in {'accepted','received'}: continue
                if parent not in (page['data']['result'] or []): continue
                binding={'stage_hash':page['stage_hash'],'result_hash':page['data']['result_hash'],'operation_id':parent['id']}
                lineage={'contract':CONTRACT,'input_hash':expected_input_hash,'generator_hash':generator,
                    'root_operation_hash':s._hash(parent),'root_binding':binding,'history':hist['history']}
                if any(r['data']['context']['whole_operation']['lineage_hash']==s._hash(lineage)
                       and r['status'] in {'planned','running','received','proposed','accepted','needs_review'} for r in whole_rows):
                    effective.add(pair)
        forest = frontier.catalogue_frontier_plan(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
            atomic_budget_chains=atomic_budget_chains, whole_histories=tuple(sorted(effective)), allow_running=allow_running, partition_repair_pairs=partition_repair_pairs)
        s._require(not forest['received_to_accept'], 'cache_invalid')
        run, source_units, _ = frontier._source(snapshot, expected_input_hash, read_checkpoint)
        units = frontier._inputs(source_units)
        _, _, addenda = sup._source_inputs(snapshot, run, read_checkpoint)
        matching = [item for addendum in addenda for item in addendum.get('matching_receipts', [])]
        profiles = [r['data']['context']['budget_profile'] for r in rows if r['stage']=='catalogue']
        s._require(profiles and all(p==profiles[0] for p in profiles), 'cache_invalid')
        profile = deepcopy(profiles[0])
        if profile != s.budget_profile(snapshot) or profile['candidate_bytes'] != 40000:
            s._require(not selected and not whole_rows, 'cache_invalid')
            return []
        runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
        runtime._snapshot, runtime._units, runtime._matching, runtime._partitions = snapshot, units, matching, []
        runtime._profile = lambda: deepcopy(profile)
        runtime._forbidden = s._unique(token for unit in units.values() for token in unit.get('_forbidden', ()) if isinstance(token,str) and token)
        by_parent = {}
        for pair in effective:
            hist = authenticated[pair]; parent = hist['parent_operation']
            s._require(parent in forest['operations'] and parent['id'] not in by_parent, 'cache_invalid')
            sub, review = (read_checkpoint(k) for k in pair)
            labels=[{'title':parent['title'],'category':parent['category'],'fact_ids':[fid],'aliases':parent['aliases']} for fid in parent['fact_ids']]
            original, _, ctx = runtime._catalogue_payload(labels,stage='subdivide')
            s._require(original == sub['data']['request'] and sub['data']['context'] == {**ctx,'budget_profile':deepcopy(profile),'generator_hash':generator}
                and review['data']['request']['matching_receipts']==matching
                and review['data']['request']['items']==[runtime._wire_fact(units[fid],'f'+str(i)) for i,fid in enumerate(parent['fact_ids'])], 'cache_invalid')
            by_parent[parent['id']] = (pair,hist['history'])
        atomic_parents = {read_checkpoint(chain[1])['data']['context']['parent_operation']['id'] for chain in atomic_budget_chains}
        from ._source_material import _template
        root_fits = _template(read_checkpoint, 'whole_root_fits', snapshot, run,
            groups=(atomic_budget_chains, tuple(sorted(effective)), partition_repair_pairs))
        if root_fits is not None:
            s._require(root_fits['binding'] == _root_fit_binding(snapshot, forest['operations'], matching, profile)
                and [key for key, fit in root_fits['fits']] == [s._hash(parent) for parent in forest['operations']], 'cache_invalid')
        plans=[]; used_whole=set()
        for position, parent in enumerate(forest['operations']):
            if parent['id'] in atomic_parents: continue
            fits = root_fits['fits'][position][1] if root_fits is not None else runtime._packet_fits([parent])
            if fits: continue
            pair, history = by_parent.get(parent['id'], (None, []))
            # Any prior foreign subdivision or ordinary packet reserves the root.
            related = [r for r in rows if (r['stage']=='subdivide' and r['data']['context'].get('label_members') == {'l'+str(i):[fid] for i,fid in enumerate(parent['fact_ids'])})
                or (r['stage']=='partition_review' and r['data']['context'].get('parent_operation')==parent)
                or (r['stage'] in {'proposal','review'} and parent['id'] in r['data']['context'].get('operation_ids', {}).values() and 'whole_operation' not in r['data']['context'])]
            if related and (pair is None or {r['stage_hash'] for r in related} != set(pair)):
                continue
            payload, refs, ops = runtime._packet([parent])
            lineage={'contract':CONTRACT,'input_hash':expected_input_hash,'generator_hash':generator,
                'root_operation_hash':s._hash(parent),'root_binding':deepcopy(forest['bindings'][parent['id']]),'history':deepcopy(history)}
            lineage_hash=s._hash(lineage)
            meta={'contract':CONTRACT,'lineage_hash':lineage_hash,'candidate_bytes':40000}
            payload['whole_operation']=deepcopy(meta)
            estimate=sum(len(item['text'].encode())+profile['candidate_member_overhead'] for item in payload['items'])+profile['candidate_operation_overhead']
            pbytes=s._semantic_request_bytes(snapshot,profile,'proposal','',payload)
            if estimate>40000 or pbytes>s.semantic_stage_limits(profile,'proposal')['input_bytes']:
                s._require(pair is None, 'input_limit'); continue
            packet_hash=s._hash(payload)
            context={'packet_hash':packet_hash,'input_ids':{k:v['_fact_id'] for k,v in refs.items()},
                'operation_ids':{k:v['id'] for k,v in ops.items()},'partition_review_hashes':payload['partition_review_hashes'],
                'revision':s._revision_context(),'budget_profile':deepcopy(profile),'generator_hash':generator,'whole_operation':deepcopy(meta)}
            pkey=s.semantic_task_identity_hash(snapshot,expected_input_hash,'proposal',payload,context)
            preview={**payload,'stage':'review','candidate':{},'proposal_hash':'0'*64,'packet_hash':'0'*64}
            base=s._semantic_request_bytes(snapshot,profile,'review','',preview)
            plan={'contract':CONTRACT,'status':'planned','blocked_reason':None,'lineage':lineage,'lineage_hash':lineage_hash,
                'parent_operation':deepcopy(parent),'whole_operation':meta,'candidate_bytes':40000,'candidate_estimate':estimate,
                'proposal_bytes':pbytes,'review_base_bytes':base,'review_actual_bytes':None,'packet_hash':packet_hash,
                'proposal_request':payload,'proposal_context':context,'proposal_hash':pkey,'review_request':None,
                'review_context':None,'review_hash':None,'required_task_hashes':sorted(pair or ()),'occupied_calls':forest['occupied_calls'],'remaining_maximum_calls':4}
            current={r['stage_hash']:r for r in whole_rows if r['data']['context']['whole_operation']==meta}
            known=set(pair or ())
            for index in (1,2):
                pkey=plan['proposal_hash']; proposal=current.get(pkey)
                if proposal is None: break
                proposal=s._checkpoint_record(pkey,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
                s._require(proposal['stage']=='proposal' and proposal['attempt']==index and proposal['data']['request']==plan['proposal_request'] and proposal['data']['context']==plan['proposal_context'], 'cache_invalid')
                known.add(pkey); state=proposal['status']; failure=None
                if state=='running': plan.update(status='blocked',blocked_reason='interrupted'); break
                if state=='planned': break
                if state=='needs_review': failure=proposal
                else:
                    s._require(state in {'received','proposed'}, 'cache_invalid')
                    value=proposal['data']['result']
                    request={**deepcopy(payload),'stage':'review','candidate':value,'proposal_hash':pkey,'packet_hash':packet_hash}
                    claim_ids={claim['id']:'claim:'+s._hash({'packet':packet_hash,'id':claim['id']})[:24] for claim in value['claims']}
                    rcontext={**plan['proposal_context'],'proposal_hash':pkey,'claim_ids':claim_ids}
                    rkey=s.semantic_task_identity_hash(snapshot,expected_input_hash,'review',request,rcontext)
                    size=s._semantic_request_bytes(snapshot,profile,'review','',request)
                    plan.update(review_request=request,review_context=rcontext,review_hash=rkey,review_actual_bytes=size,status='received' if state=='received' else 'planned')
                    if size>s.semantic_stage_limits(profile,'review')['input_bytes']:
                        s._require(rkey not in current, 'cache_invalid')
                        plan.update(status='blocked',blocked_reason='input_limit'); break
                    else:
                        review=current.get(rkey)
                        if review is None: break
                        s._require(state=='proposed', 'cache_invalid')
                        review=s._checkpoint_record(rkey,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
                        s._require(review['stage']=='review' and review['attempt']==index and review['data']['request']==request and review['data']['context']==rcontext, 'cache_invalid')
                        known.add(rkey)
                        if review['status']=='running': plan.update(status='blocked',blocked_reason='interrupted'); break
                        if review['status']=='planned': break
                        if review['status'] in {'accepted','received'}:
                            s._require(s.BusinessSemanticIntegration._review(review['data']['result'],refs,ops,value)==review['data']['result'], 'cache_invalid')
                            plan['status']='accepted' if review['status']=='accepted' else 'received'; break
                        s._require(review['status']=='needs_review', 'cache_invalid'); failure=review
                if index==2:
                    plan.update(status='blocked',blocked_reason=failure['data'].get('failure_code','needs_review')); break
                if s._failure_feedback(failure,proposal) is None:
                    plan.update(status='blocked',blocked_reason=failure['data'].get('failure_code','needs_review')); break
                next_request=s.semantic_revision_payload(snapshot,proposal,failure)
                revision=s._revision_context(2,proposal=proposal,failure=failure,snapshot=snapshot)
                next_context={**context,'revision':revision}
                next_hash=s.semantic_task_identity_hash(snapshot,expected_input_hash,'proposal',next_request,next_context)
                next_bytes=s._semantic_request_bytes(snapshot,profile,'proposal','',next_request)
                plan.update(proposal_request=next_request,proposal_context=next_context,proposal_hash=next_hash,proposal_bytes=next_bytes,
                    review_request=None,review_context=None,review_hash=None,review_actual_bytes=None,status='planned')
                if next_bytes>s.semantic_stage_limits(profile,'proposal')['input_bytes']:
                    plan.update(status='blocked',blocked_reason='input_limit'); break
            s._require(set(current)<=known, 'cache_invalid')
            for row in current.values():
                raw=read_checkpoint.raw_checkpoint_row(row['stage_hash']); error=row['data'].get('failure_code') if row['status']=='needs_review' else None
                s._require(isinstance(raw,tuple) and len(raw)==8 and tuple(raw[:5])==tuple(row[k] for k in ('stage_hash','stage','input_hash','status','attempt'))
                    and json.loads(raw[5])==row['data'] and raw[6]==error and read_checkpoint.raw_error_code(row['stage_hash'])==error,'cache_invalid')
            used_whole.update(current)
            plan['required_task_hashes']=sorted(known)
            paid=sum(r['status']!='planned' for r in current.values())
            plan['remaining_maximum_calls']=0 if plan['status']=='accepted' or (plan['status']=='blocked' and plan['blocked_reason']!='interrupted') else max(0,4-paid)
            plans.append(plan)
        s._require(used_whole=={row['stage_hash'] for row in whole_rows}, 'cache_invalid')
        return plans
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def coverage_histories(coverage):
    proofs = coverage.get('whole_operations', [])
    s._require(isinstance(proofs, list), 'cache_invalid')
    pairs=[]
    for proof in proofs:
        s._require(isinstance(proof,dict) and set(proof)=={'contract','lineage','lineage_hash','candidate_bytes','packet_hash','review_hash'}
            and proof['contract']==CONTRACT and isinstance(proof['lineage'],dict)
            and isinstance(proof['lineage'].get('history'),list) and len(proof['lineage']['history']) in (0,2), 'cache_invalid')
        history=proof['lineage']['history']
        if history:
            s._require(all(isinstance(item,dict) and set(item)=={'stage_hash','raw_digest'} for item in history), 'cache_invalid')
            pairs.append(tuple(item['stage_hash'] for item in history))
    s._require(len(set(pairs))==len(pairs), 'cache_invalid')
    return pairs


def validate_whole_operations(coverage, verification, *, snapshot, expected_input_hash, read_checkpoint, atomic_budget_chains=(), partition_repair_pairs=()):
    if 'whole_operations' not in coverage:
        if callable(getattr(read_checkpoint, 'task_rows', None)):
            rows = read_checkpoint.task_rows(expected_input_hash)
            s._require(not any('whole_operation' in row['data']['context']
                and row['data']['context'].get('generator_hash') == s._semantic_generator_hash(snapshot) for row in rows), 'cache_invalid')
        return []
    proofs=coverage['whole_operations']; pairs=coverage_histories(coverage)
    s._require(proofs and callable(read_checkpoint), 'cache_invalid')
    plans=whole_operation_plans(snapshot,expected_input_hash,read_checkpoint=read_checkpoint,
        selected_history_pairs=pairs,atomic_budget_chains=atomic_budget_chains,partition_repair_pairs=partition_repair_pairs)
    matched=[]
    for proof in proofs:
        candidates=[p for p in plans if p['lineage_hash']==proof['lineage_hash']]
        s._require(len(candidates)==1, 'cache_invalid'); plan=candidates[0]
        s._require(plan['status']=='accepted' and proof=={'contract':CONTRACT,'lineage':plan['lineage'],
            'lineage_hash':plan['lineage_hash'],'candidate_bytes':plan['candidate_bytes'],
            'packet_hash':plan['packet_hash'],'review_hash':plan['review_hash']}, 'cache_invalid')
        parent=plan['parent_operation']
        s._require(parent in coverage['catalogue_roots'] and sum(s._operation_membership(op)==parent for op in coverage['catalogue'])==1
            and not any(partition['parent']==parent for partition in coverage['partitions']), 'cache_invalid')
        s._require(any(packet==plan['packet_hash'] and review==plan['review_hash'] for packet,review in
            zip(verification['packet_hashes'],verification['accepted_review_hashes'])), 'cache_invalid')
        matched.append(plan['lineage_hash'])
    s._require(len(set(matched))==len(matched) and set(matched)=={p['lineage_hash'] for p in plans}, 'cache_invalid')
    return pairs
