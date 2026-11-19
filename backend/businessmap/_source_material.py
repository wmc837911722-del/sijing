"""Ephemeral compiler-owned Source data; never an authorization cache.

Only the SQL snapshot factory supplies raw provenance. Public readers and live
callbacks cannot manufacture a material binding with a digest or builder.
"""
import json
import hashlib
import importlib
import inspect
import marshal
import re
import functools
import urllib.parse
from types import (BuiltinFunctionType, CodeType, FunctionType, ModuleType,
                   MemberDescriptorType, MethodDescriptorType, WrapperDescriptorType)

_SEAL = object()
_RUN_PROGRESS = frozenset(('updated_at status error summary paused cancelled coverage interrupted '
    'synthesis synthesis_version synthesis_failure publication_complete conflicts notes_created notes_updated').split())


def _require(value):
    from .business_semantics import SemanticValidationError
    if not value:
        raise SemanticValidationError('cache_invalid')


class _RawSourceCapture:
    __slots__ = ('__rows', '__input', '__ambiguous', '__seal')

    def __init__(self, rows, input_hash, *, _seal=None, _ambiguous=False):
        _require(_seal is _SEAL)
        object.__setattr__(self, '_RawSourceCapture__rows', rows)
        object.__setattr__(self, '_RawSourceCapture__input', input_hash)
        object.__setattr__(self, '_RawSourceCapture__ambiguous', _ambiguous)
        object.__setattr__(self, '_RawSourceCapture__seal', _SEAL)

    def __setattr__(self, name, value):
        raise AttributeError('sealed Source capture')

    def __reduce__(self):
        raise TypeError('Source capture cannot be serialized')


def _capture_raw_source(db, expected_input_hash):
    """Factory-only capture in its already-open, coherent SQL transaction.

    The returned opaque object contains immutable raw SQL scalars, not a caller
    digest. Keep the snapshot factory signature unchanged and assign this to
    ``frozen._source_capture`` before constructing FrozenReader.
    """
    _require(db.in_transaction)
    tables, ambiguous = [], False
    names = ('full_analysis_runs', 'full_analysis_chunks', 'analysis_addenda',
             'analysis_matching_receipts', 'full_analysis_synthesis',
             'business_semantic_tasks', 'business_semantic_recoveries',
             'business_semantic_supersessions')
    for name in names:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not exists:
            # A first ordinary proposal receipt may create this dynamic table.
            # Its entire membership is checked by the mutation's fresh reader.
            tables.append((name, () if name == 'business_semantic_recoveries' else None))
            continue
        schema = tuple(tuple(row) for row in db.execute('PRAGMA table_info(' + name + ')'))
        columns = tuple(row[1] for row in schema)
        if name == 'business_semantic_tasks':
            rows = db.execute("SELECT * FROM business_semantic_tasks WHERE input_hash=? AND stage NOT IN ('proposal','review') ORDER BY rowid", (expected_input_hash,)).fetchall()
        elif name == 'business_semantic_recoveries':
            rows = db.execute("SELECT r.* FROM business_semantic_recoveries r LEFT JOIN business_semantic_tasks t ON t.stage_hash=r.child_stage_hash WHERE r.input_hash=? AND (t.stage IS NULL OR t.stage NOT IN ('proposal','review')) ORDER BY r.rowid", (expected_input_hash,)).fetchall()
        elif name == 'business_semantic_supersessions':
            rows = db.execute('SELECT * FROM business_semantic_supersessions WHERE input_hash=? ORDER BY rowid', (expected_input_hash,)).fetchall()
        else:
            # Match or conservatively exceed all candidate-run and all-L0
            # selectors. Membership/order and unknown SQL columns stay bound.
            order = {'full_analysis_runs': 'id', 'full_analysis_chunks': 'run_id,position',
                'analysis_addenda': 'id', 'analysis_matching_receipts': 'id',
                'full_analysis_synthesis': 'cache_key'}[name]
            rows = db.execute('SELECT * FROM ' + name + ' ORDER BY ' + order).fetchall()
        rows = [tuple(row) for row in rows]
        if name == 'full_analysis_chunks':
            keys = [(row[columns.index('run_id')], row[columns.index('position')]) for row in rows]
            # ORDER BY position does not define a semantic tie order. Existing
            # raw readers remain usable; only compiled reuse is unsupported.
            ambiguous = ambiguous or len(set(keys)) != len(keys)
        if name == 'full_analysis_runs':
            data_at = columns.index('data')
            normalized = []
            for row in rows:
                values = list(row)
                run = json.loads(values[data_at])
                _require(type(run) is dict)
                values[data_at] = json.dumps({k: v for k, v in run.items() if k not in _RUN_PROGRESS}, ensure_ascii=False, separators=(',', ':'))
                if 'updated_at' in columns:
                    values[columns.index('updated_at')] = None
                normalized.append(tuple(values))
            rows = normalized
        tables.append((name, (schema, tuple(rows)) if rows or name != 'business_semantic_recoveries' else ()))
    return _RawSourceCapture(tuple(tables), expected_input_hash, _seal=_SEAL, _ambiguous=ambiguous)


def _capture(reader):
    from ._frozen_routing import FrozenReader
    _require(type(reader) is FrozenReader)
    value = getattr(reader, '_source_capture', None)
    _require(type(value) is _RawSourceCapture and value._RawSourceCapture__seal is _SEAL)
    return value


def _same_raw(left, right):
    return (not left._RawSourceCapture__ambiguous and not right._RawSourceCapture__ambiguous
            and left._RawSourceCapture__input == right._RawSourceCapture__input
            and left._RawSourceCapture__rows == right._RawSourceCapture__rows)


_COMPILER_MODULES = ('_source_material', '_frozen_routing', 'business_semantics', 'semantic_supersession', 'semantic_catalogue_frontier',
    'semantic_proposal_recovery', 'semantic_review_recovery', 'semantic_review_exception', 'semantic_recovery', 'semantic_atomic_budget',
    'semantic_whole_operation', 'semantic_partition_repair',
    'full_synthesis', 'l0_legacy', 'business_content', 'analysis_addenda', 'analysis_output',
    'full_analysis', 'providers', 'codex_provider')
_INFRASTRUCTURE_GLOBALS = frozenset(('_LOG', '_LOGGER', 'logger', 'annotations', '_PROJECT_LOCK_STATE', '_PROJECT_LOCKS',
    '_LOCKS', '_LOCKS_LOCK', '_SECRET_LOCK', '_SECRET_MEMORY', '_STATUS_CACHE', '_STATUS_LOCK', '_CATALOG_CACHE', '_CATALOG_LOCK'))
_RUNTIME_ALLOWLISTS = frozenset(('partition_repair_allowlist', 'atomic_budget_allowlist',
                               'whole_operation_allowlist', 'proposal_timeout_recovery_allowlist',
                               'review_timeout_recovery_allowlist'))


def _policy_value(value, seen, code_memo=None):
    """Closed representation of code and policy, with no user serialization."""
    kind = type(value)
    if value is None or kind in (bool, int, float, str, bytes):
        return (kind.__name__, repr(value))
    if value is Ellipsis:
        return ('ellipsis',)
    if id(value) in seen:
        return ('ref', id(value))
    seen.add(id(value))
    if isinstance(value, re.Pattern):
        return ('regex', value.pattern, value.flags)
    if kind in (tuple, list, set, frozenset):
        items = [_policy_value(item, seen, code_memo) for item in value]
        return (kind.__name__, tuple(sorted(items, key=repr)) if kind in (set, frozenset) else tuple(items))
    if kind is dict:
        return ('dict', tuple((_policy_value(k, seen, code_memo), _policy_value(v, seen, code_memo)) for k, v in value.items()))
    if kind is FunctionType:
        return ('function', id(value), _code_value(value.__code__, code_memo),
                _policy_value(value.__defaults__, seen, code_memo), _policy_value(value.__kwdefaults__, seen, code_memo),
                tuple(_policy_value(cell.cell_contents, seen, code_memo) for cell in value.__closure__ or ()))
    if kind is functools._lru_cache_wrapper:
        return ('stdlib-lru', id(value), _policy_value(value.__wrapped__, seen, code_memo))
    if kind is BuiltinFunctionType or inspect.isbuiltin(value):
        return ('builtin', id(value))
    if kind in (MethodDescriptorType, WrapperDescriptorType):
        return ('builtin-descriptor', id(value))
    if inspect.isclass(value):
        if value.__module__.startswith(('businessmap.', 'json.', 'yaml.')):
            return ('class', id(value), tuple((name, _policy_value(
                member.__func__ if isinstance(member, (staticmethod, classmethod)) else member, seen, code_memo))
                for name, member in vars(value).items()
                if not isinstance(member, MemberDescriptorType)
                and not (value.__name__ == 'BusinessSemanticIntegration' and name in _RUNTIME_ALLOWLISTS)
                and (not name.startswith('__') or isinstance(member, (FunctionType, staticmethod, classmethod)))))
        return ('external-class', id(value))
    raise TypeError('unsupported compiler dependency')


def _immutable_code_constant(value):
    kind = type(value)
    if value is None or value is Ellipsis or kind in (bool, int, float, str, bytes):
        return True
    if kind is CodeType:
        return all(_immutable_code_constant(item) for item in value.co_consts)
    if kind in (tuple, frozenset):
        return all(_immutable_code_constant(item) for item in value)
    return False


def _code_value(code, memo=None):
    # A memo lives only inside one compiler stamp. Retain the code object to
    # prevent id reuse; mutable/custom constants always use the original walk.
    if memo is not None and id(code) in memo:
        return memo[id(code)][1]
    reusable = memo is not None and _immutable_code_constant(code)
    # marshal's reference flags can change after nested code is executed;
    # code content and literal constants are the actual dependency.
    result = (code.co_code, code.co_exceptiontable, code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
        code.co_kwonlyargcount, code.co_flags, code.co_stacksize,
        tuple(_code_value(item, memo) if type(item) is CodeType else _policy_value(item, set(), memo)
              for item in code.co_consts))
    if reusable:
        memo[id(code)] = (code, result)
    return result

# Retain the actual stdlib implementation's immutable code at module load, not
# a module-name assertion. Lazy-table equivalence is limited to this code;
# the live function, defaults, globals and all callers are still bound below.
_URL_LAZY_CODE = _code_value(urllib.parse._unquote_impl.__code__)
_URL_HEXDIG = '0123456789ABCDEFabcdef'


def _url_normalizer_value(value, seen):
    """Close the imported URL decoder over its actual reachable globals."""
    kind = type(value)
    if value is None or kind in (bool, int, float, str, bytes, BuiltinFunctionType, re.Pattern):
        return _policy_value(value, set())
    if inspect.isclass(value):
        if value not in (str, bytes, bytearray, KeyError):
            raise TypeError('unsupported URL decoder class')
        return ('builtin-class', id(value))
    if id(value) in seen:
        return ('ref', id(value))
    seen.add(id(value))
    if kind in (tuple, list):
        return (kind.__name__, tuple(_url_normalizer_value(item, seen) for item in value))
    if kind is dict:
        return ('dict', tuple((_url_normalizer_value(k, seen), _url_normalizer_value(v, seen))
                              for k, v in value.items()))
    if kind is not FunctionType:
        raise TypeError('unsupported URL decoder dependency')
    names = set()
    def code_names(code):
        names.update(code.co_names)
        for item in code.co_consts:
            if type(item) is CodeType:
                code_names(item)
    code_names(value.__code__)
    dependencies = []
    for name in sorted(names):
        namespace = value.__globals__ if name in value.__globals__ else value.__builtins__
        if name not in namespace:
            continue  # Attribute names share co_names with global loads.
        item = namespace[name]
        if namespace is vars(urllib.parse) and name == '_hextobyte':
            impl = namespace.get('_unquote_impl')
            if (type(impl) is not FunctionType or _code_value(impl.__code__) != _URL_LAZY_CODE
                    or namespace.get('_hexdig') != _URL_HEXDIG):
                raise TypeError('unsupported URL lazy decoder')
            standard = {(a + b).encode(): bytes.fromhex(a + b)
                        for a in _URL_HEXDIG for b in _URL_HEXDIG}
            if item is not None and (type(item) is not dict or
                    any(type(k) is not bytes or type(v) is not bytes for k, v in item.items())
                    or item != standard):
                raise TypeError('changed URL decode lookup')
            # None and the exact complete table are the same pure lookup.
            item = tuple(sorted(standard.items()))
        dependencies.append((name, _url_normalizer_value(item, seen)))
    return ('url-function', id(value), _code_value(value.__code__),
            _url_normalizer_value(value.__defaults__, seen),
            _url_normalizer_value(value.__kwdefaults__, seen),
            tuple(_url_normalizer_value(cell.cell_contents, seen) for cell in value.__closure__ or ()),
            tuple(dependencies))


def _compiler_stamp():
    """Bind active Python code/defaults/closures and complete policy globals.

    Infrastructure modules are not traversed wholesale: their referenced
    functions are bound below. No filesystem I/O occurs at an admission.
    """
    from ._frozen_routing import dependency_stamp
    try:
        # Preserve the original conservative callable-origin/representation
        # gate. Its marshal(code) digest contains reference-sharing flags
        # which vary under concurrent execution, so it is not an identity.
        # Every dependency it covers is represented canonically below.
        if dependency_stamp() is None:
            return None
        code_memo = {}
        entries = [('routing-eligible', True)]
        for short_name in _COMPILER_MODULES:
            module = importlib.import_module('.' + short_name, __package__)
            for name, value in sorted(vars(module).items()):
                if name.startswith('__'):
                    continue
                if isinstance(value, ModuleType):
                    entries.append((module.__name__, name, 'module', id(value)))
                    continue
                if short_name == '_source_material' and name == '_SEAL':
                    entries.append((module.__name__, name, id(value)))
                    continue
                # Logging objects and thread-local synchronization are not
                # read by pure Source compilation. Everything else is closed.
                if name in _INFRASTRUCTURE_GLOBALS:
                    continue
                entries.append((module.__name__, name, _policy_value(value, set(), code_memo)))
        # Serialized requests and schemas are transitive compiler dependencies.
        from . import providers, business_content
        external = [json, json.encoder, json.decoder, re, hashlib, business_content.html,
                    business_content.html.entities, business_content.unicodedata, business_content.yaml]
        external.extend(importlib.import_module('yaml.' + name) for name in
            ('loader', 'reader', 'scanner', 'parser', 'composer', 'constructor', 'resolver', 'nodes', 'events', 'tokens', 'error'))
        for module in external:
            entries.append((module.__name__, tuple((name, _policy_value(value, set(), code_memo))
                for name, value in vars(module).items()
                if not name.startswith('__') and name not in ('_cache', '_cache2')
                and (isinstance(value, (FunctionType, BuiltinFunctionType, re.Pattern))
                    or inspect.isclass(value) or type(value) in (str, int, bool, tuple, frozenset, dict)))))
        # These imported call boundaries were explicit in the routing stamp.
        # Do not let an unsupported callable disappear through the broader
        # external-module census's type filter.
        for owner, names in ((business_content, ('unquote',)), (business_content.html, ('unescape',)),
                (business_content.unicodedata, ('normalize',)), (business_content.yaml, ('safe_load',)),
                (json, ('dumps', 'loads')), (hashlib, ('sha256',)), (re, ('search', 'finditer', 'sub', 'split'))):
            for name in names:
                entries.append(('imported', owner.__name__, name, _policy_value(getattr(owner, name), set(), code_memo)))
        entries.append(('url-normalizer', _url_normalizer_value(business_content.unquote, set())))
        # JSON's singleton decoder can be customized without replacing loads.
        # Bind its callable hooks and scanner identity as well as decoder code.
        entries.append(('json-decoder', id(json._default_decoder), tuple(
            (name, id(value) if name == 'scan_once' else _policy_value(value, set(), code_memo))
            for name, value in vars(json._default_decoder).items())))
        entries.append(('json-encoder', id(json._default_encoder), _policy_value(vars(json._default_encoder), set(), code_memo)))
        return hashlib.sha256(repr(entries).encode()).hexdigest()
    except (TypeError, ValueError, AttributeError, RecursionError):
        return None


def _scope(snapshot, run):
    from .business_semantics import _hash
    return _hash({'snapshot': snapshot, 'run': {key: run.get(key) for key in
        ('run_id', 'snapshot_id', 'project_revision', 'semantic_input_hash')}})


def _groups(atomic_budget_chains, whole_histories, partition_repair_pairs):
    return tuple(tuple(tuple(keys) for keys in group) for group in
                 (atomic_budget_chains, whole_histories, partition_repair_pairs))


class SourceMaterial:
    __slots__ = ('__capture', '__scope', '__stamp', '__groups', '__templates', '__seal')

    def __init__(self, capture, scope, stamp, groups, templates, *, _seal=None):
        _require(_seal is _SEAL)
        for name, value in (('capture', capture), ('scope', scope), ('stamp', stamp),
                            ('groups', groups), ('templates', tuple(templates.items())), ('seal', _SEAL)):
            object.__setattr__(self, '_SourceMaterial__' + name, value)

    def __setattr__(self, name, value):
        raise AttributeError('sealed Source material')

    def __reduce__(self):
        raise TypeError('Source material cannot be serialized')


def _catalogue_template(forest, units, *, separate=False):
    """Prepare only builtin Source input data; custom copies stay per-entry."""
    from ._frozen_routing import _prepared_copy, _routing_copy_metadata
    from .semantic_catalogue_frontier import _inputs
    # Check before normalization so an unsupported custom deepcopy hook never
    # runs early. Normalizing per unit preserves the original alias boundary.
    normalized = _inputs(units) if _routing_copy_metadata(units) else None
    plan = {key: value for key, value in forest.items()
            if key not in ('received_to_accept', 'occupied_calls')}
    if separate:
        terminal = {key: plan[key] for key in ('page_hashes', 'terminal_candidate')} if _routing_copy_metadata(plan) else plan
        return {'catalogue': _prepared_copy((plan, normalized is not None)),
                'catalogue_inputs': _prepared_copy(normalized),
                'catalogue_terminal': _prepared_copy((terminal, normalized is not None))}
    return _prepared_copy((plan, normalized))


def prepare_source_material(snapshot, run, *, read_checkpoint, atomic_budget_chains=(),
                            whole_histories=(), partition_repair_pairs=()):
    """Compile once from the already-closed factory snapshot, outside locks."""
    from . import semantic_supersession as sup, semantic_catalogue_frontier as frontier
    from . import semantic_proposal_recovery as proposal, business_semantics as s
    from ._frozen_routing import _prepared_copy, _routing_copy_metadata
    capture = _capture(read_checkpoint)
    _require(capture._RawSourceCapture__input == run['semantic_input_hash'] and not capture._RawSourceCapture__ambiguous)
    stamp = _compiler_stamp()
    _require(stamp is not None)
    group_key = _groups(atomic_budget_chains, whole_histories, partition_repair_pairs)
    routing = sup._source_routing(snapshot, run, read_checkpoint)
    templates = {'routing': _prepared_copy(routing), 'routing_builtin': bool(_routing_copy_metadata(routing))}
    rows = read_checkpoint.task_rows(run['semantic_input_hash'])
    if any(row['stage'] == 'catalogue' and row['data']['request'].get('mode') == 'merge'
           and 'merge_contract' not in row['data']['request'] for row in rows):
        legacy = sup.reconstruct_catalogue_frontier(snapshot, run, read_checkpoint=read_checkpoint)
        terminal = legacy.pop('completed_root')
        legacy['terminal_page'] = legacy['catalogue_task_hashes'][-1] if terminal is not None else None
        templates['legacy'] = _prepared_copy(legacy)
    scoped = proposal._without_packet(read_checkpoint, run['semantic_input_hash'], '', source_only=True)
    forest = frontier.catalogue_frontier_plan(snapshot, run['semantic_input_hash'], read_checkpoint=scoped,
        atomic_budget_chains=atomic_budget_chains, whole_histories=whole_histories,
        partition_repair_pairs=partition_repair_pairs)
    _require(not forest['received_to_accept'])
    templates.update(_catalogue_template(forest, routing[0], separate=True))
    from .semantic_whole_operation import _root_fit_data
    from ._frozen_routing import _routing_copy_metadata
    if _routing_copy_metadata(routing[0]):
        _, _, addenda = sup._source_inputs(snapshot, run, scoped)
        matching = [item for addendum in addenda for item in addendum.get('matching_receipts', [])]
        fits = _root_fit_data(snapshot, frontier._inputs(routing[0]), matching,
                              forest['operations'], s.budget_profile(snapshot))
        if fits is not None:
            templates['whole_root_fits'] = _prepared_copy(fits)
    _, _, packets, dependencies = proposal._packet_templates(snapshot, run, scoped,
        budget_profile=s.budget_profile(snapshot), atomic_budget_chains=atomic_budget_chains,
        whole_histories=whole_histories, partition_repair_pairs=partition_repair_pairs)
    # Enumeration consumers need the original aggregate graph, including
    # aliases across packets. Keep it beside the selective binding index.
    templates['packets'] = _prepared_copy((packets, dependencies))
    # Keep each packet separately encoded: authentication selects one exact
    # request/context, never needs to decode every other packet's Source text.
    if _routing_copy_metadata((packets, dependencies)):
        selected = {}
        for entry in packets:
            key = s._hash([entry['payload'], entry['binding']])
            selected.setdefault(key, []).append(_prepared_copy((entry, dependencies)))
        templates['packet_bindings'] = tuple((key, tuple(clones)) for key, clones in selected.items())
    else:
        # Custom hooks retain the original whole-graph copy and ordering.
        templates['packet_bindings'] = None
    _require(stamp == _compiler_stamp())
    return SourceMaterial(capture, _scope(snapshot, run), stamp, group_key, templates, _seal=_SEAL)


def bind_source_material(material, snapshot, run, *, read_checkpoint):
    """Attach only to a NEW reader after current trusted SQL equivalence."""
    from ._frozen_routing import FrozenReader
    current = _capture(read_checkpoint)
    _require(type(material) is SourceMaterial and material._SourceMaterial__seal is _SEAL
        and material._SourceMaterial__scope == _scope(snapshot, run)
        and material._SourceMaterial__stamp == _compiler_stamp()
        and _same_raw(material._SourceMaterial__capture, current))
    # A filtered reader cannot become a new root with hidden physical rows.
    _require(read_checkpoint._FrozenReader__view_root is read_checkpoint)
    result = object.__new__(FrozenReader)
    for slot in FrozenReader.__slots__:
        name = '_FrozenReader' + slot if slot.startswith('__') else slot
        object.__setattr__(result, name, getattr(read_checkpoint, name))
    object.__setattr__(result, '_FrozenReader__view_root', result)
    object.__setattr__(result, '_FrozenReader__views', {})
    object.__setattr__(result, '_FrozenReader__v2_proofs', {})
    object.__setattr__(result, '_catalogue_frontiers', {})
    object.__setattr__(result, '_FrozenReader__material', material)
    return result


def _bind_raw_source(material, snapshot, run, *, db):
    """Short-transaction RAW equivalence; no decoded reader or compilation."""
    current = _capture_raw_source(db, run['semantic_input_hash'])
    _require(type(material) is SourceMaterial and material._SourceMaterial__seal is _SEAL
        and material._SourceMaterial__scope == _scope(snapshot, run)
        and material._SourceMaterial__stamp == _compiler_stamp()
        and _same_raw(material._SourceMaterial__capture, current))


def _template_material(reader, snapshot, run, *, groups=None):
    from ._frozen_routing import FrozenReader
    if type(reader) is not FrozenReader:
        return None
    material = getattr(reader, '_FrozenReader__material', None)
    if material is None:
        return None
    _require(type(material) is SourceMaterial and material._SourceMaterial__seal is _SEAL
        and material._SourceMaterial__scope == _scope(snapshot, run)
        and material._SourceMaterial__stamp == _compiler_stamp())
    if groups is not None:
        _require(material._SourceMaterial__groups == _groups(*groups))
    return material


def _template(reader, name, snapshot, run, *, groups=None):
    material = _template_material(reader, snapshot, run, groups=groups)
    if material is None:
        return None
    clone = next((value for key, value in material._SourceMaterial__templates if key == name), None)
    return clone() if clone is not None else None


def _packet_template(reader, snapshot, run, request, context, *, groups):
    """Select only compiler-owned data after the original fresh stamp check."""
    from .business_semantics import _hash
    material = _template_material(reader, snapshot, run, groups=groups)
    if material is None:
        return None
    index = next(value for key, value in material._SourceMaterial__templates if key == 'packet_bindings')
    if index is None:
        clone = next(value for key, value in material._SourceMaterial__templates if key == 'packets')
        packets, dependencies = clone()
        candidates = [(entry, dependencies) for entry in packets]
    else:
        identity = _hash([request, context])
        clones = next((clones for key, clones in index if key == identity), ())
        candidates = [clone() for clone in clones]
    matches = [(entry['payload'], dependencies) for entry, dependencies in candidates
               if entry['payload'] == request and entry['binding'] == context]
    _require(len(matches) == 1)
    return matches[0]


def _legacy_routing(reader, snapshot, run):
    """Retain the routing check and all custom hooks; omit only builtin copies."""
    material = _template_material(reader, snapshot, run)
    if material is None:
        return False, None
    entries = dict(material._SourceMaterial__templates)
    return True, None if entries['routing_builtin'] else entries['routing']()
