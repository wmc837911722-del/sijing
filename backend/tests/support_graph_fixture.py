"""Encode verified positive canonical fixtures, never normalize invalid candidates."""
from copy import deepcopy


def positive_graph(value, payload):
    if payload.get('proposal_format') != 'support-graph-v1':
        return value
    from businessmap.business_semantics import BusinessSemanticIntegration
    refs = {item['fact_id']: {'_fact_id': item['fact_id'], 'kind': item['kind'], 'text': item['text']} for item in payload['items']}
    operations = {item['operation_id']: {'fact_ids': item['fact_ids']} for item in payload['operations']}
    # Invalid old forward/backlink fixtures must fail here, never get repaired
    # by discarding their invalid parts. Mutations belong after this encoding.
    assert BusinessSemanticIntegration._proposal(value, refs, operations) == value
    return {'operations': deepcopy(value['operations']),
            'claims': [{key: val for key, val in claim.items() if key != 'id'} for claim in value['claims']],
            'decisions': [{'input_id': row['input_id'], 'parts': [
                {key: part[key] for key in ('outcome', 'rationale', 'resolved_by_ids')
                 if key in part and (key != 'resolved_by_ids' or part['outcome'] == 'resolved')}
                for part in row['parts'] if part['outcome'] not in {'claim', 'retained'}]} for row in value['decisions']]}
