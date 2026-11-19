"""Complete, finite paid catalogue operation closure. No writes or model calls."""
from copy import deepcopy
from ._frozen_routing import _prepared_copy
from . import business_semantics as s
from . import semantic_supersession as sup
from .semantic_recovery import recovery_inventory, receipt_hash

CONTRACT = 'complete-operation-frontier-v1'
TERMINAL_FIELDS = frozenset(('contract input_hash generator_hash page_hashes frontier operations_hash terminal_hash').split())


def _require(value):
    s._require(value, 'cache_invalid')


def _inputs(inputs):
    if isinstance(inputs, dict):
        return {key: {'_fact_id': key, 'kind': value['kind'], 'text': value['text'],
            '_observation': deepcopy(value.get('observation', value.get('_observation', {})))} for key, value in inputs.items()}
    _require(isinstance(inputs, list))
    result = {unit['_fact_id']: deepcopy(unit) for unit in inputs}
    _require(len(result) == len(inputs))
    return result


def _source(snapshot, input_hash, reader):
    _require(callable(getattr(reader, 'catalogue_runs', None))
        and callable(getattr(reader, 'catalogue_source', None)))
    matches, runs = [], []
    if callable(getattr(reader, 'catalogue_runs', None)):
        repositories = {row['id']: row['commit'] for row in snapshot.get('repositories', [])}
        runs = reader.catalogue_runs(snapshot['project']['id'])
        for run in runs:
            if (run.get('project_id') != snapshot['project']['id']
                    or run.get('project_revision') != snapshot['project']['revision']
                    or {row['repository_id']: row['commit'] for row in run.get('source_repositories', [])} != repositories):
                continue
            persisted, source, addenda = sup._source_inputs(snapshot, run, reader)
            if s.semantic_input_hash(snapshot, persisted, source['chunks'], addenda) == input_hash:
                matches.append({**persisted, 'semantic_input_hash': input_hash})
    _require(len(matches) == 1)
    run = matches[0]
    units, routing = sup._source_routing(snapshot, run, reader)
    return run, units, routing


def _leaf_page(row, units):
    data = row['data']
    request, context = data['request'], data['context']
    _require(set(request) == {'stage', 'project', 'items', 'membership_hash', 'mode'}
        and request['stage'] == 'catalogue' and request['mode'] == context['catalogue_mode'] == 'leaf'
        and set(context) == {'catalogue_mode', 'label_members', 'generator_hash', 'budget_profile'})
    labels, index = [], 0
    _require(request['items'] and len(request['items']) <= context['budget_profile']['catalogue_items'])
    _require([item['label_id'] for item in request['items']] == ['l'+str(i) for i in range(len(request['items']))]
        and set(context['label_members']) == {item['label_id'] for item in request['items']}
        and request['membership_hash'] == s._hash(context['label_members']))
    for item in request['items']:
        members = context['label_members'][item['label_id']]
        _require(set(item) == {'label_id', 'title', 'category', 'fact_count', 'member_facts'}
            and isinstance(item['title'], str) and bool(item['title'].strip()) and item['category'] in s.CATEGORIES
            and type(item['fact_count']) is int and item['fact_count'] == len(members) and members)
        expected = []
        for fid in members:
            _require(fid in units)
            expected.append(s.BusinessSemanticIntegration.grouping_fact(units[fid], 'f'+str(index)))
            index += 1
        _require(item['member_facts'] == expected)
        labels.append({'title': item['title'], 'category': item['category'], 'fact_ids': members, 'aliases': [item['title']]})
    sup._validate_result(data['result'], labels)
    return labels


def catalogue_frontier_plan(snapshot, expected_input_hash, *, read_checkpoint, inputs=None, partition_repair_pairs=(), allow_running=(), atomic_budget_chains=(), whole_histories=()):
    """Source-authoritative plan; exactly one persisted SOURCE run is required."""
    return _catalogue_plan(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
        inputs=inputs, source_required=True, partition_repair_pairs=partition_repair_pairs, allow_running=allow_running, atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories)


def checkpoint_catalogue_preview(snapshot, expected_input_hash, *, read_checkpoint, inputs):
    """Explicit direct-input preview. Never produces a sealable terminal.

    This separate component contract inspects paid checkpoint structure only;
    supplied facts are not authority for persisted SOURCE coverage or order.
    """
    return _catalogue_plan(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
        inputs=inputs, source_required=False)


def _packet_frontier(snapshot, expected_input_hash, *, read_checkpoint, atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=()):
    """Recovery binding needs the terminal, after the same complete validation."""
    result = _catalogue_plan(snapshot, expected_input_hash, read_checkpoint=read_checkpoint,
        inputs=None, source_required=True, atomic_budget_chains=atomic_budget_chains,
        whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs, _packet_only=True)
    return {key: result[key] for key in ('received_to_accept', 'terminal_candidate')}


def _catalogue_plan(snapshot, expected_input_hash, *, read_checkpoint, inputs, source_required, partition_repair_pairs=(), allow_running=(), atomic_budget_chains=(), whole_histories=(), _packet_only=False):
    try:
        _require(callable(getattr(read_checkpoint, 'task_rows', None)))
        all_rows = read_checkpoint.task_rows(expected_input_hash)
        rows = [row for row in all_rows if row['input_hash'] == expected_input_hash
            and row['data']['context'].get('generator_hash') == s._semantic_generator_hash(snapshot)]
        materialized = s.SemanticCheckpointRows(_prepared_copy(rows)())
        materialized.read_checkpoint = read_checkpoint
        inventory = s.semantic_task_inventory(materialized, snapshot=snapshot, expected_input_hash=expected_input_hash, partition_repair_pairs=partition_repair_pairs, allow_running=allow_running, atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories)
        recoveries = recovery_inventory(rows, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
        supersessions = sup.supersession_inventory(rows, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
        catalogue = {row['stage_hash']: row for row in rows if row['stage'] == 'catalogue'}
        paid = {key: row for key, row in catalogue.items() if row['status'] in {'accepted', 'received'}}
        _require(paid)
        replaced = {receipt['parent_stage_hash'] for receipt in recoveries+supersessions if receipt['child_stage_hash'] in paid}
        _require(set(catalogue)-set(paid) == replaced)
        _require(all(row['data']['request'].get('project') == snapshot['project']['name'] for row in catalogue.values()))
        run, source_units, routing = _source(snapshot, expected_input_hash, read_checkpoint) if source_required else (None, None, None)
        from ._source_material import _template, _template_material
        template = _template(read_checkpoint, 'catalogue_terminal' if _packet_only else 'catalogue',
            snapshot, {**(run or {}), 'semantic_input_hash': expected_input_hash},
            groups=(atomic_budget_chains, whole_histories, partition_repair_pairs)) if source_required else None
        prepared, inputs_available = template if template is not None else (None, False)
        # Default compiled consumers do not use normalized inputs. Supplied
        # input comparisons and uncached reconstruction retain their copies.
        units = None
        if prepared is None or not inputs_available or inputs is not None:
            if prepared is not None:
                units = _template(read_checkpoint, 'catalogue_inputs', snapshot,
                    {**run, 'semantic_input_hash': expected_input_hash},
                    groups=(atomic_budget_chains, whole_histories, partition_repair_pairs))
            if units is None:
                units = _inputs(source_units if source_required else inputs)
        if source_units is not None and inputs is not None:
            provided = _inputs(inputs)
            _require(set(provided) == set(units) and all(s.BusinessSemanticIntegration.grouping_fact(provided[fid], 'f')
                == s.BusinessSemanticIntegration.grouping_fact(unit, 'f') for fid, unit in units.items()))
        if prepared is not None and (not inputs_available or inputs is not None):
            # Caller deepcopy hooks can change code or policy during comparison.
            _template_material(read_checkpoint, snapshot, {**run, 'semantic_input_hash': expected_input_hash},
                groups=(atomic_budget_chains, whole_histories, partition_repair_pairs))
        if prepared is not None:
            # The inventory, receipts, paid/replaced membership and Source
            # scope above are current. Templates contain neither status nor
            # occupied/remaining counts; reconstruct these from this snapshot.
            _require(set(prepared['page_hashes']) == set(paid))
            prepared['recovery_receipt_hashes'] = sorted(receipt_hash(receipt) for receipt in recoveries)
            prepared['supersession_receipt_hashes'] = sorted(sup.supersession_receipt_hash(receipt) for receipt in supersessions)
            terminal = prepared['terminal_candidate']
            if terminal is not None:
                terminal.pop('terminal_hash')
                terminal['terminal_hash'] = s._hash({**terminal,
                    'recovery_receipt_hashes': prepared['recovery_receipt_hashes'],
                    'supersession_receipt_hashes': prepared['supersession_receipt_hashes']})
            return {**prepared, 'received_to_accept': sorted(key for key, row in paid.items() if row['status'] == 'received'),
                    'occupied_calls': inventory['occupied_calls']}
        legacy = any(row['data']['request'].get('mode') == 'merge' and 'merge_contract' not in row['data']['request'] for row in catalogue.values())
        if legacy:
            _require(run is not None)
            base = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=read_checkpoint)
            adopted = set(base['catalogue_task_hashes'])
            available = {(base['bindings'][operation['id']]['stage_hash'], operation['id']): deepcopy(operation) for operation in base['frontier']}
        else:
            leaf = {key: row for key, row in paid.items() if row['data']['request'].get('mode') == 'leaf'}
            adopted, available = set(leaf), {}
            for key, row in leaf.items():
                s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
                _leaf_page(row, units)
                for operation in row['data']['result']:
                    available[(key, operation['id'])] = deepcopy(operation)
            if routing is not None:
                runtime = s.BusinessSemanticIntegration.__new__(s.BusinessSemanticIntegration)
                runtime._snapshot, runtime._units = snapshot, units
                labels = runtime._catalogue_initial_labels(routing)
                page, expected = [], []
                for label in labels:
                    request, _, _ = runtime._catalogue_payload(page+[label])
                    if page and (len(page)+1 > runtime._profile()['catalogue_items'] or runtime._request_bytes('catalogue', '', request) > s.semantic_stage_limits(runtime._profile(), 'catalogue')['input_bytes']):
                        expected.append(runtime._catalogue_payload(page)[0]); page = []
                    page.append(label)
                if page: expected.append(runtime._catalogue_payload(page)[0])
                _require(len(leaf) == len(expected) and all(sum(row['data']['request'] == request for row in leaf.values()) == 1 for request in expected))
        facts = [fid for operation in available.values() for fid in operation['fact_ids']]
        _require(len(facts) == len(set(facts)) and set(facts) == set(units))
        pending = {key: row for key, row in paid.items() if key not in adopted}
        consumed = set()
        while pending:
            progressed = False
            for key in sorted(pending):
                row = pending[key]
                s._checkpoint_record(key, snapshot=snapshot, expected_input_hash=expected_input_hash, read_checkpoint=read_checkpoint)
                _require(row['data']['request'].get('merge_contract') == s.BOUNDED_MERGE_CONTRACT)
                parents = {(binding['stage_hash'], binding['operation_id']) for binding in row['data']['context']['catalogue_parents'].values()}
                _require(not parents.intersection(consumed))
                if not parents <= available.keys():
                    continue
                consumed.update(parents)
                for parent in parents: del available[parent]
                for operation in row['data']['result']:
                    _require((key, operation['id']) not in available)
                    available[(key, operation['id'])] = deepcopy(operation)
                adopted.add(key); del pending[key]; progressed = True
                break
            _require(progressed)
        _require(adopted == set(paid))
        order = {fid: index for index, fid in enumerate(units)}
        pairs = sorted(available, key=lambda pair: (min(order[fid] for fid in available[pair]['fact_ids']), pair[1], pair[0]))
        operations = [available[pair] for pair in pairs]
        facts = [fid for operation in operations for fid in operation['fact_ids']]
        _require(len(facts) == len(set(facts)) and set(facts) == set(units)
            and len({operation['id'] for operation in operations}) == len(operations))
        bindings = {operation['id']: {'stage_hash': pair[0], 'result_hash': paid[pair[0]]['data']['result_hash'], 'operation_id': pair[1]}
            for pair, operation in zip(pairs, operations)}
        recovery_hashes = sorted(receipt_hash(receipt) for receipt in recoveries)
        supersession_hashes = sorted(sup.supersession_receipt_hash(receipt) for receipt in supersessions)
        terminal = {'contract': CONTRACT, 'input_hash': expected_input_hash, 'generator_hash': s._semantic_generator_hash(snapshot),
            'page_hashes': sorted(adopted), 'frontier': [bindings[operation['id']] for operation in operations], 'operations_hash': s._hash(operations)}
        terminal['terminal_hash'] = s._hash({**terminal, 'recovery_receipt_hashes': recovery_hashes, 'supersession_receipt_hashes': supersession_hashes})
        return deepcopy({'operations': operations, 'bindings': bindings, 'page_hashes': sorted(adopted),
            'received_to_accept': sorted(key for key, row in paid.items() if row['status'] == 'received'),
            'terminal_candidate': terminal if source_required else None, 'recovery_receipt_hashes': recovery_hashes,
            'supersession_receipt_hashes': supersession_hashes, 'occupied_calls': inventory['occupied_calls']})
    except s.SemanticValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise s.SemanticValidationError('cache_invalid') from None


def validate_catalogue_terminal(projection, *, snapshot, expected_input_hash, read_checkpoint, partition_repair_pairs=(), atomic_budget_chains=(), whole_histories=()):
    """Final consumer: independently recompute, never seal a received preview."""
    terminal = projection['verification']['catalogue_terminal']
    _require(isinstance(terminal, dict) and set(terminal) == TERMINAL_FIELDS and terminal['contract'] == CONTRACT)
    plan = catalogue_frontier_plan(snapshot, expected_input_hash, read_checkpoint=read_checkpoint, inputs=projection['semantic_coverage']['inputs'], partition_repair_pairs=partition_repair_pairs, atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories)
    _require(not plan['received_to_accept'] and terminal == plan['terminal_candidate']
        and projection['semantic_coverage']['catalogue_roots'] == plan['operations'])
    rows = {row['stage_hash']: row for row in read_checkpoint.task_rows(expected_input_hash)}
    declared = projection['verification']['catalogue_task_hashes']
    _require(len(declared) == len(set(declared)) and set(plan['page_hashes']) == {key for key in declared if rows[key]['stage'] == 'catalogue'})
    return plan
