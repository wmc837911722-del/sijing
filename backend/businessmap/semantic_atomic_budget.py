"""Authenticated, source-bound atomic parent budget planning; no I/O mutations."""
from copy import deepcopy
import json
from . import business_semantics as s

CONTRACT = 'atomic-parent-budget-v1'
SAFETY_BYTES = 1024
RESERVE_FACTOR = 3


def authenticate_history(snapshot, expected_input_hash, *, read_checkpoint, keys):
    """Authenticate only the closed four-row physical history; never trust inline data."""
    try:
        s._require(isinstance(keys, tuple) and len(keys) == 4 and len(set(keys)) == 4
                   and callable(getattr(read_checkpoint, 'raw_checkpoint_row', None)), 'cache_invalid')
        from .semantic_partition_repair import _draft
        draft = _draft(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
                       parent_subdivision_hash=keys[0], parent_review_hash=keys[1])
        s._require(draft['status'] == 'blocked' and draft['blocked_reason'] == 'review_rejected'
                   and draft['subdivision_hash'] == keys[2] and draft['review_hash'] == keys[3]
                   and set(draft['required_task_hashes']) == set(keys), 'cache_invalid')
        rows, history = [], []
        for key in keys:
            row = s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
            raw = read_checkpoint.raw_checkpoint_row(key)
            s._require(isinstance(raw, tuple) and len(raw) == 8 and isinstance(raw[5], str)
                and tuple(raw[:5]) == tuple(row[k] for k in ('stage_hash','stage','input_hash','status','attempt'))
                and json.loads(raw[5]) == row['data'] and raw[6] == read_checkpoint.raw_error_code(key), 'cache_invalid')
            rows.append(row)
            history.append({'stage_hash': key, 'raw_digest': s._hash(list(raw))})
        sub, rejected, repaired, second = rows
        s._require([(r['stage'],r['status'],r['attempt']) for r in rows]
                   == [('subdivide','accepted',1),('partition_review','needs_review',1),
                       ('subdivide','accepted',1),('partition_review','needs_review',1)]
                   and second['data'].get('failure_code') == 'review_rejected'
                   and second['data'].get('diagnostic', {}) == {} and second['data']['result'] is None
                   and read_checkpoint.raw_error_code(keys[3]) == 'review_rejected', 'cache_invalid')
        refs = second['data']['context']['input_ids']
        issues = second['data'].get('issues')
        s._require(isinstance(issues,list) and issues and all(isinstance(issue,dict)
            and issue.get('code') in s.ISSUES-{'privacy'} and isinstance(issue.get('input_ids'),list)
            and all(isinstance(fid,str) and fid in refs for fid in issue['input_ids']) for issue in issues), 'cache_invalid')
        return {'history':history, 'parent_operation':deepcopy(draft['parent_operation']), 'draft':draft}
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise s.SemanticValidationError('cache_invalid') from None


def atomic_budget_plan(snapshot, expected_input_hash, *, read_checkpoint, original_subdivision_hash,
                       original_review_hash, repaired_subdivision_hash, repaired_review_hash, allow_running=(), whole_histories=()):
    """Source-authenticated pure preview; this is not dispatch authorization."""
    try:
        from . import semantic_catalogue_frontier as frontier
        from . import semantic_supersession as sup
        keys=(original_subdivision_hash,original_review_hash,repaired_subdivision_hash,repaired_review_hash)
        s._require(s._structured_output(snapshot), 'cache_invalid')
        authenticated=authenticate_history(snapshot,expected_input_hash,read_checkpoint=read_checkpoint,keys=keys)
        forest=frontier.catalogue_frontier_plan(snapshot,expected_input_hash,read_checkpoint=read_checkpoint,
            atomic_budget_chains=(keys,),allow_running=allow_running,whole_histories=whole_histories)
        parent=authenticated['parent_operation']
        s._require(parent in forest['operations'] and not forest['received_to_accept'], 'cache_invalid')
        binding=forest['bindings'][parent['id']]
        run, source_units, _ = frontier._source(snapshot,expected_input_hash,read_checkpoint)
        units=frontier._inputs(source_units)
        _,_,addenda=sup._source_inputs(snapshot,run,read_checkpoint)
        matching=[item for addendum in addenda for item in addendum.get('matching_receipts', [])]
        profile=read_checkpoint(keys[0])['data']['context']['budget_profile']
        runtime=s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
        runtime._snapshot,runtime._units,runtime._matching,runtime._partitions=snapshot,units,matching,[]
        runtime._profile=lambda: deepcopy(profile)
        runtime._forbidden=s._unique(token for unit in units.values() for token in unit.get('_forbidden', ()) if isinstance(token,str) and token)
        old_sub=read_checkpoint(keys[0]); old_review=read_checkpoint(keys[1]); d2=read_checkpoint(keys[2])
        labels=[{'title':parent['title'],'category':parent['category'],'fact_ids':[fid],'aliases':parent['aliases']} for fid in parent['fact_ids']]
        s._require(len(labels)<=profile['catalogue_items'], 'cache_invalid')
        original,_,original_context=runtime._catalogue_payload(labels,stage='subdivide')
        s._require(original == old_sub['data']['request'] and old_sub['data']['context'] == {**original_context,
            'budget_profile':deepcopy(profile),'generator_hash':s._semantic_generator_hash(snapshot)}, 'cache_invalid')
        s._require(old_review['data']['request']['matching_receipts']==matching
            and old_review['data']['request']['items']==[runtime._wire_fact(units[fid],'f'+str(i)) for i,fid in enumerate(parent['fact_ids'])]
            and not runtime._packet_fits([parent]), 'cache_invalid')
        for child in old_sub['data']['result']+d2['data']['result']:
            s._public(child['title'],runtime._forbidden)
            s._require(runtime._packet_fits([child]),'cache_invalid')
        payload,refs,ops=runtime._packet([parent])
        lineage={'contract':CONTRACT,'input_hash':expected_input_hash,'generator_hash':s._semantic_generator_hash(snapshot),
                 'root_operation_hash':s._hash(parent),'root_binding':deepcopy(binding),'history':authenticated['history']}
        lineage_hash=s._hash(lineage)
        estimate=sum(len(item['text'].encode())+profile['candidate_member_overhead'] for item in payload['items'])+profile['candidate_operation_overhead']
        limit=s.semantic_stage_limits(profile,'review')['input_bytes']
        proposal_limit=s.semantic_stage_limits(profile,'proposal')['input_bytes']
        def candidate(cap):
            meta={'contract':CONTRACT,'lineage_hash':lineage_hash,'candidate_bytes':cap}
            request={**deepcopy(payload),'budget':{'candidate_bytes':cap},'atomic_budget':meta}
            review={**request,'stage':'review','candidate':{},'proposal_hash':'0'*64,'packet_hash':'0'*64}
            pbytes=s._semantic_request_bytes(snapshot,profile,'proposal','',request)
            base=s._semantic_request_bytes(snapshot,profile,'review','',review)
            return pbytes,base,request,meta
        lo,hi=1,profile['candidate_bytes']
        while lo<=hi:
            mid=(lo+hi)//2; pbytes,base,_,_=candidate(mid)
            if pbytes<=proposal_limit and base+RESERVE_FACTOR*mid+SAFETY_BYTES<=limit:
                lo=mid+1
            else: hi=mid-1
        cap=hi
        s._require(cap>0 and estimate<=cap, 'input_limit')
        pbytes,base,request,meta=candidate(cap)
        packet_hash=s._hash(request)
        context={'packet_hash':packet_hash,'input_ids':{key:unit['_fact_id'] for key,unit in refs.items()},
            'operation_ids':{key:operation['id'] for key,operation in ops.items()},
            'partition_review_hashes':request['partition_review_hashes'],'revision':s._revision_context(),
            'budget_profile':deepcopy(profile),'generator_hash':s._semantic_generator_hash(snapshot),'atomic_budget':deepcopy(meta)}
        proposal_hash=s.semantic_task_identity_hash(snapshot,expected_input_hash,'proposal',request,context)
        plan={'contract':CONTRACT,'status':'planned','blocked_reason':None,'lineage':lineage,'lineage_hash':lineage_hash,
            'parent_operation':parent,'atomic_budget':meta,'candidate_bytes':cap,'candidate_estimate':estimate,
            'proposal_bytes':pbytes,'review_base_bytes':base,'review_actual_bytes':None,'packet_hash':packet_hash,
            'proposal_request':request,'proposal_context':context,'proposal_hash':proposal_hash,
            'review_request':None,'review_context':None,'review_hash':None,'required_task_hashes':sorted(keys),
            'occupied_calls':forest['occupied_calls'],'remaining_maximum_calls':4}
        current=[row for row in read_checkpoint.task_rows(expected_input_hash)
                 if row['data']['context'].get('generator_hash')==s._semantic_generator_hash(snapshot)
                 and 'atomic_budget' in row['data']['context']]
        s._require(all(row['data']['context']['atomic_budget']==meta for row in current), 'cache_invalid')
        for row in current:
            raw=read_checkpoint.raw_checkpoint_row(row['stage_hash'])
            expected_error=row['data'].get('failure_code') if row['status']=='needs_review' else None
            s._require(isinstance(raw,tuple) and len(raw)==8 and isinstance(raw[5],str)
                and tuple(raw[:5])==tuple(row[key] for key in ('stage_hash','stage','input_hash','status','attempt'))
                and json.loads(raw[5])==row['data'] and raw[6]==expected_error
                and read_checkpoint.raw_error_code(row['stage_hash'])==expected_error, 'cache_invalid')
        known=set(keys)
        current_by_key={row['stage_hash']:row for row in current}
        for index in (1,2):
            pkey=plan['proposal_hash']; proposal=current_by_key.get(pkey)
            if proposal is None:
                break
            proposal=s._checkpoint_record(pkey,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
            s._require(proposal['stage']=='proposal' and proposal['attempt']==index
                and proposal['data']['request']==plan['proposal_request'] and proposal['data']['context']==plan['proposal_context'], 'cache_invalid')
            known.add(pkey)
            state=proposal['status']; failure=None
            if state=='running':
                plan.update(status='blocked',blocked_reason='interrupted'); break
            if state=='planned': break
            if state=='needs_review':
                failure=proposal
            else:
                s._require(state in {'received','proposed'}, 'cache_invalid')
                value=proposal['data']['result']
                base_payload=s.semantic_revision_base_packet(proposal,snapshot=snapshot,read_checkpoint=read_checkpoint)
                review_request={**base_payload,'stage':'review','candidate':value,'proposal_hash':pkey,'packet_hash':packet_hash}
                claim_ids={claim['id']:'claim:'+s._hash({'packet':packet_hash,'id':claim['id']})[:24] for claim in value['claims']}
                review_context={**plan['proposal_context'],'proposal_hash':pkey,'claim_ids':claim_ids}
                review_hash=s.semantic_task_identity_hash(snapshot,expected_input_hash,'review',review_request,review_context)
                actual=s._semantic_request_bytes(snapshot,profile,'review','',review_request)
                plan.update(review_request=review_request,review_context=review_context,review_hash=review_hash,
                            review_actual_bytes=actual,status='received' if state=='received' else 'planned')
                canonical_bytes=len(s._json(value).encode())
                # The outer ensure_ascii=False JSON quoting can at most double
                # the canonical candidate bytes. Generated claim IDs are ASCII;
                # their review-schema enum plus separators is bounded by the
                # IDs already present in that canonical JSON. The remaining
                # fixed empty-vs-nonempty schema delta is covered by 1024 bytes.
                # Still measure the complete real review before claiming a slot.
                if (canonical_bytes>cap or actual>limit
                        or actual>base+RESERVE_FACTOR*canonical_bytes+SAFETY_BYTES):
                    plan.update(status='blocked',blocked_reason='input_limit'); break
                review=current_by_key.get(review_hash)
                if review is None: break
                s._require(state=='proposed', 'cache_invalid')
                review=s._checkpoint_record(review_hash,snapshot=snapshot,expected_input_hash=expected_input_hash,read_checkpoint=read_checkpoint)
                s._require(review['stage']=='review' and review['attempt']==index and review['data']['request']==review_request
                    and review['data']['context']==review_context, 'cache_invalid')
                known.add(review_hash)
                if review['status']=='running':
                    plan.update(status='blocked',blocked_reason='interrupted'); break
                if review['status']=='planned': break
                if review['status'] in {'accepted','received'}:
                    s._require(actual<=limit, 'cache_invalid')
                    normalized=s.BusinessSemanticIntegration._review(review['data']['result'], refs, ops, value)
                    s._require(normalized==review['data']['result'], 'cache_invalid')
                    plan['status']='accepted' if review['status']=='accepted' else 'received'
                    break
                s._require(review['status']=='needs_review', 'cache_invalid')
                failure=review
            if index==2:
                plan.update(status='blocked',blocked_reason=failure['data'].get('failure_code','needs_review')); break
            feedback=s._failure_feedback(failure,proposal)
            if feedback is None:
                plan.update(status='blocked',blocked_reason=failure['data'].get('failure_code','needs_review')); break
            revision=s._revision_context(2,proposal=proposal,failure=failure,snapshot=snapshot)
            next_request=s.semantic_revision_payload(snapshot,proposal,failure)
            next_context={**context,'revision':revision}
            next_hash=s.semantic_task_identity_hash(snapshot,expected_input_hash,'proposal',next_request,next_context)
            next_pbytes=s._semantic_request_bytes(snapshot,profile,'proposal','',next_request)
            preview={**request,'stage':'review','candidate':{},'proposal_hash':'0'*64,'packet_hash':'0'*64}
            next_base=s._semantic_request_bytes(snapshot,profile,'review','',preview)
            plan.update(proposal_request=next_request,proposal_context=next_context,proposal_hash=next_hash,
                        proposal_bytes=next_pbytes,review_base_bytes=next_base,review_request=None,
                        review_context=None,review_hash=None,review_actual_bytes=None,status='planned')
            if next_pbytes>proposal_limit or next_base+RESERVE_FACTOR*cap+SAFETY_BYTES>limit:
                plan.update(status='blocked',blocked_reason='input_limit'); break
        s._require({row['stage_hash'] for row in current} <= known, 'cache_invalid')
        plan['required_task_hashes']=sorted(known)
        paid=sum(row['status']!='planned' for row in current)
        plan['remaining_maximum_calls']=0 if plan['status']=='accepted' or (plan['status']=='blocked' and plan['blocked_reason']!='interrupted') else max(0,4-paid)
        return plan
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def validate_atomic_budgets(coverage, verification, *, snapshot, expected_input_hash, read_checkpoint, whole_histories=()):
    """Authenticate every sealed atomic proof; no inline lineage is authoritative."""
    try:
        if 'atomic_budgets' not in coverage:
            return []
        proofs=coverage['atomic_budgets']
        s._require(isinstance(proofs,list) and len(proofs)==1, 'cache_invalid')
        chains=[]
        for proof in proofs:
            s._require(isinstance(proof,dict) and set(proof)=={'contract','lineage','lineage_hash','candidate_bytes','packet_hash','review_hash'}
                and proof['contract']==CONTRACT and isinstance(proof['lineage'],dict)
                and isinstance(proof['lineage'].get('history'),list) and len(proof['lineage']['history'])==4, 'cache_invalid')
            keys=tuple(item['stage_hash'] for item in proof['lineage']['history'])
            plan=atomic_budget_plan(snapshot,expected_input_hash,read_checkpoint=read_checkpoint,
                original_subdivision_hash=keys[0],original_review_hash=keys[1],
                repaired_subdivision_hash=keys[2],repaired_review_hash=keys[3],whole_histories=whole_histories)
            s._require(plan['status']=='accepted' and proof=={'contract':CONTRACT,'lineage':plan['lineage'],
                'lineage_hash':plan['lineage_hash'],'candidate_bytes':plan['candidate_bytes'],
                'packet_hash':plan['packet_hash'],'review_hash':plan['review_hash']}, 'cache_invalid')
            parent=plan['parent_operation']
            s._require(parent in coverage['catalogue_roots']
                and sum(s._operation_membership(op)==parent for op in coverage['catalogue'])==1
                and not any(partition['parent']==parent for partition in coverage['partitions']), 'cache_invalid')
            packets=verification['packet_hashes']; reviews=verification['accepted_review_hashes']
            s._require(len(packets)==len(reviews) and any(packet==plan['packet_hash'] and review==plan['review_hash']
                for packet,review in zip(packets,reviews)), 'cache_invalid')
            chains.append(keys)
        return chains
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None
