"""Accepted semantic-stage fixtures for source, L0, and publication tests.

These fixtures replace the external semantic stage only. They are sealed through
its real pure validator; separate semantic/integration tests exercise decisions.
"""
import copy
import hashlib


def seal(projection):
    from businessmap.business_semantics import _hash, _projection_hash
    coverage = projection['semantic_coverage']
    coverage['catalogue_roots'] = [{key: copy.deepcopy(operation[key])
        for key in ('id', 'title', 'category', 'fact_ids', 'aliases')} for operation in coverage['catalogue']]
    coverage['partitions'] = []
    projection['verification'] = {'input_hash': projection['semantic_input_hash'],
        'catalogue_hash': _hash(projection['semantic_coverage']['catalogue']),
        'packet_hashes': [_hash('fixture packet')] if projection['semantic_coverage']['input_ids'] else [],
        'accepted_review_hashes': [_hash('fixture independent review')] if projection['semantic_coverage']['input_ids'] else [],
        'accepted_partition_review_hashes': [],
        'accepted': True, 'projection_hash': _projection_hash(projection)}
    return projection


def projection_from_units(snapshot, run, units, routing, addenda=()):
    from businessmap.business_semantics import _render_operation, validate_semantic_projection
    inputs = {unit['_fact_id']: {'kind': unit['kind'], 'text': unit['text'], 'chunk_id': unit.get('chunk_id'),
        'evidence_ids': unit.get('_evidence_ids', []), 'source_paths': unit.get('_source_paths', []),
        'observation': copy.deepcopy(unit.get('_observation', {}))} for unit in units}
    docs, claims, decisions, operations, sources = {}, [], {}, {}, {}
    for row in routing:
        fid = row['fact_id']
        if row['disposition'] == 'internal':
            decisions[fid] = {'parts': [{'outcome': 'technical', 'claim_ids': [], 'support_ids': [fid], 'resolved_by_ids': [], 'rationale': row['reason']}]}
            operation_id = 'operation:' + hashlib.sha256('private-fixture'.encode()).hexdigest()[:20]
            operations.setdefault(operation_id, {'id': operation_id, 'title': '内部资料', 'category': 'data',
                'purpose': '', 'summary': '', 'support_ids': [], 'fact_ids': [], 'aliases': []})['fact_ids'].append(fid)
            continue
        key = 'topic:' + hashlib.sha256((row['category'] + row['title']).encode()).hexdigest()[:20]
        operation_id = 'operation:' + key.partition(':')[2]
        op = operations.setdefault(operation_id, {'id': operation_id, 'title': row['title'], 'category': row['category'],
            'purpose': '了解业务操作条件。', 'summary': '业务条件与待确认事项分别保留。', 'support_ids': [], 'fact_ids': [], 'aliases': []})
        op['support_ids'].append(fid)
        op['fact_ids'].append(fid)
        cid = 'claim:' + hashlib.sha256(fid.encode()).hexdigest()[:20]
        unknown = inputs[fid]['kind'] == 'unknown'
        claims.append({'id': cid, 'operation_id': operation_id, 'kind': 'unknown' if unknown else 'rule',
            'text': inputs[fid]['text'], 'support_ids': [fid], 'scope': '对应业务操作', 'conditions': [], 'section': 'unknowns' if unknown else 'conditions'})
        decisions[fid] = {'parts': [{'outcome': 'retained' if unknown else 'claim', 'claim_ids': [cid], 'support_ids': [fid], 'resolved_by_ids': [], 'rationale': '原始业务说明保留。'}]}
        docs.setdefault(key, {'key': key, 'title': row['title'], 'category': row['category'], 'body': '# ' + row['title'], 'fact_ids': []})['fact_ids'].append(fid)
    for key, doc in docs.items():
        selected = [claim for claim in claims if claim['operation_id'] == 'operation:' + key.partition(':')[2]]
        doc['body'] = _render_operation(operations['operation:' + key.partition(':')[2]], selected)
        sources[key] = {'evidence_ids': list(dict.fromkeys(e for fid in doc['fact_ids'] for e in inputs[fid]['evidence_ids'])),
                        'source_paths': list(dict.fromkeys(p for fid in doc['fact_ids'] for p in inputs[fid]['source_paths']))}
    result = {'complete': True, 'semantic_input_hash': run['semantic_input_hash'], 'public_documents': list(docs.values()),
        'overview_body': '# ' + snapshot['project']['name'] + ' · 业务地图\n\n业务操作与条件。\n',
        'summary_body': '# 业务梳理总结\n\n业务条件与实际运行边界仍需核验。\n', 'source_mapping': sources,
        'semantic_coverage': {'schema': 8, 'input_ids': list(inputs), 'inputs': inputs, 'decisions': decisions, 'claims': claims, 'catalogue': list(operations.values())}, 'usage': {}}
    seal(result)
    validate_semantic_projection(result, expected_input_hash=run['semantic_input_hash'])
    return result


def public_result(snapshot, run, chunks, addenda=()):
    from businessmap.full_synthesis import source_fact_units
    from businessmap.business_semantics import semantic_input_hash
    units = source_fact_units(snapshot, run, chunks, addenda)
    routing = [{'fact_id': unit['_fact_id'], 'disposition': 'public', 'category': 'rules', 'title': unit['title'], 'text': unit['text']} for unit in units]
    return projection_from_units(snapshot, {**run, 'semantic_input_hash': semantic_input_hash(snapshot, run, chunks, addenda)}, units, routing, addenda)


def isolate_stubbed_semantic_receipts(monkeypatch):
    """Source/publication unit seam for an already stubbed semantic stage.

    The synthetic projection has no model receipts. Keep its pure schema,
    coverage, backlinks and privacy validation; these tests do not claim full
    semantic acceptance. Real semantic integration fixtures restore the stored
    validator explicitly. Publication, ownership, source and CAS guards stay real.
    """
    import businessmap.business_semantics as semantics
    def validate_stub_projection(projection, *, snapshot, expected_input_hash, store):
        semantics.validate_semantic_projection(projection, expected_input_hash=expected_input_hash)
    monkeypatch.setattr(semantics, "validate_stored_semantic_projection", validate_stub_projection)
