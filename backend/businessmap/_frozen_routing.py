"""Reader-local routing memo and conservative runtime dependency manifest.

The manifest covers both L0 implementations and their content normalizer. A
value we cannot faithfully represent disables reuse, rather than guessing.
"""
import hashlib
import inspect
import marshal
import re
from copy import deepcopy
from types import FunctionType, ModuleType


def _value(value, seen):
    if value is None or type(value) in (bool, int, float, str, bytes):
        return repr(value)
    if isinstance(value, re.Pattern):
        return repr((value.pattern, value.flags))
    if isinstance(value, FunctionType):
        if id(value) in seen:
            return ('ref', id(value))
        seen.add(id(value))
        closure = tuple(_value(c.cell_contents, seen) for c in (value.__closure__ or ()))
        return ('fn', id(value), hashlib.sha256(marshal.dumps(value.__code__)).hexdigest(),
                _value(value.__defaults__, seen), _value(value.__kwdefaults__, seen), closure)
    if type(value) in (tuple, list, set, frozenset):
        items = [_value(v, seen) for v in value]
        return (type(value).__name__, tuple(sorted(items, key=repr)) if isinstance(value, (set, frozenset)) else tuple(items))
    if type(value) is dict:
        return ('dict', tuple(sorted(((_value(k, seen), _value(v, seen)) for k, v in value.items()), key=repr)))
    raise TypeError('unrepresentable routing dependency')


def dependency_stamp():
    from . import full_synthesis, l0_legacy, business_content, providers, semantic_supersession, business_semantics
    try:
        # Compute the shared closure first: canonical code inspection can
        # materialize code attributes before the legacy marshal fingerprint.
        from ._source_material import _url_normalizer_value
        url_normalizer = _url_normalizer_value(business_content.unquote, set())
        entries = []
        # Names that can be reached by source normalization and either L0
        # strategy, including their dynamically imported content functions.
        required = {
            full_synthesis: ('_json', '_hash', '_unique', '_business_clauses', '_stylesheet_statement', '_internal_basis', '_text_policy', 'source_fact_units', '_observation'),
            l0_legacy: ('_json', '_hash', '_unique', '_business_clauses', '_stylesheet_statement', '_internal_basis', '_text_policy'),
            business_content: ('_reject', '_canonical', 'implementation_identifiers', 'public_business_metadata', 'validate_public_business_text'),
        }
        for module, names in required.items():
            if any(not isinstance(getattr(module, name, None), FunctionType)
                   or getattr(module, name).__module__ != module.__name__ for name in names):
                return None
        for module in (full_synthesis, l0_legacy, business_content):
            for name, value in sorted(vars(module).items()):
                if name.startswith('__') or isinstance(value, ModuleType) or inspect.isclass(value):
                    continue
                if isinstance(value, FunctionType) and value.__module__ != module.__name__ and name not in required[module]:
                    continue
                if not isinstance(value, (FunctionType, str, int, float, bool, bytes, tuple, list, set, frozenset, dict, re.Pattern)) and value is not None:
                    continue
                entries.append((module.__name__, name, _value(value, set())))
        for cls, names in ((full_synthesis.FullBusinessSynthesis,
                            ('VERSION', 'L0_ROUTING_VERSION', 'WIRE_BYTES', 'MAX_OUTPUT', 'MAX_ITEMS', 'SYSTEM', '_wire', '_batches', '_validate', 'cache_identity', 'validate_cache', 'navigation')),
                           (l0_legacy.LegacyL0,
                            ('VERSION', 'L0_ROUTING_VERSION', 'WIRE_BYTES', 'MAX_OUTPUT', 'MAX_ITEMS', 'SYSTEM', '_wire', '_batches', '_validate'))):
            entries.append(('class', id(cls)))
            for name in names:
                value = getattr(cls, name)
                if isinstance(value, FunctionType) and value.__module__ != cls.__module__:
                    return None
                entries.append((name, _value(value, set())))
        # Imported normalizers are not defined in the three modules above.
        # Bind their identities as well as Python code where available.
        for name, fn in (('unquote', business_content.unquote),
                         ('html.unescape', business_content.html.unescape),
                         ('unicode.normalize', business_content.unicodedata.normalize),
                         ('yaml.safe_load', business_content.yaml.safe_load),
                         ('json.dumps', full_synthesis.json.dumps),
                         ('json.loads', full_synthesis.json.loads),
                         ('sha256', full_synthesis.hashlib.sha256),
                         ('re.search', full_synthesis.re.search),
                         ('re.finditer', full_synthesis.re.finditer),
                         ('re.sub', full_synthesis.re.sub),
                         ('re.split', full_synthesis.re.split)):
            entries.append((name, _value(fn, set()) if isinstance(fn, FunctionType) else id(fn)))
        entries.append(('url-normalizer', url_normalizer))
        for module, name in ((providers, 'provider_destination'),
                             (semantic_supersession, '_derive_source_routing'),
                             (semantic_supersession, '_require'),
                             (business_semantics, '_require'),
                             (business_semantics, '_hash'),
                             (business_semantics, '_json')):
            fn = getattr(module, name)
            if not isinstance(fn, FunctionType) or fn.__module__ != module.__name__:
                return None
            entries.append(('boundary', _value(fn, set())))
        return hashlib.sha256(repr(entries).encode()).hexdigest()
    except (TypeError, ValueError, AttributeError, RecursionError):
        return None


def _routing_copy_metadata(value):
    """Encode only validated builtin graphs; bytes never come from a caller."""
    stack, visited = [value], set()
    while stack:
        item = stack.pop()
        kind = type(item)
        if kind in (str, int, bool, bytes, type(None)):
            continue
        # Reject the whole graph before encoding: subclasses/custom objects
        # retain deepcopy behavior; even floats can carry NaN identity.
        if kind not in (dict, list, tuple):
            return ()
        if id(item) in visited:
            continue
        visited.add(id(item))
        if kind is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        else:
            stack.extend(item)
    try:
        # Version 4 preserves mutable aliases and cycles. This is private
        # in-memory copy metadata, never persisted or loaded from input bytes.
        return marshal.dumps(value, 4)
    except (TypeError, ValueError, RecursionError):
        return ()


def _copy_routing(value, metadata):
    return marshal.loads(metadata) if metadata else deepcopy(value)


def _prepared_copy(value):
    """Prepare one captured raw value, without copying custom objects early."""
    metadata = _routing_copy_metadata(value)
    if metadata:
        return lambda: marshal.loads(metadata)
    return lambda: deepcopy(value)


class FrozenReader:
    """Immutable callback facade; routing cache accepts no caller-built values."""
    __slots__ = ('__reader', '__route', '__sealed', '_catalogue_frontiers',
                 'catalogue_source', 'catalogue_runs', 'task_rows', 'supersession_receipts',
                 'raw_digest', 'raw_checkpoint_row', 'raw_error_code', 'recovery_receipts',
                 'd2_retry_rows', '__view_root', '__hidden', '__views', '_source_capture', '__material', '_dynamic_capture', '__v2_proofs', '__prior_bundle_id')

    def __init__(self, reader):
        from .business_semantics import semantic_checkpoint_rows
        if (not isinstance(reader, FunctionType) or not any(
                reader.__code__ is code and code.co_name == 'frozen'
                for code in semantic_checkpoint_rows.__code__.co_consts
                if inspect.iscode(code))):
            raise TypeError('reader must be created by semantic_checkpoint_rows')
        object.__setattr__(self, '_FrozenReader__reader', reader)
        object.__setattr__(self, '_source_capture', None)
        object.__setattr__(self, '_FrozenReader__material', None)
        object.__setattr__(self, '_FrozenReader__prior_bundle_id', None)
        for name, value in reader.__dict__.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, '_FrozenReader__view_root', self)
        object.__setattr__(self, '_FrozenReader__hidden', frozenset())
        object.__setattr__(self, '_FrozenReader__views', {})
        object.__setattr__(self, '_FrozenReader__v2_proofs', {})
        cache = {}
        def route(snapshot, run, source, addenda):
            from .semantic_supersession import _derive_source_routing
            from .business_semantics import _hash
            before = dependency_stamp()
            key = _hash({'snapshot': snapshot, 'run': run, 'source': source, 'addenda': addenda, 'dependencies': before}) if before else None
            cached_entry = cache.get(key) if key is not None else None
            if cached_entry is not None:
                cached, metadata = cached_entry
                return _copy_routing(cached, metadata)
            result = _derive_source_routing(snapshot, run, source, addenda)
            if key is not None and before == dependency_stamp():
                cache.clear()
                source_metadata = _routing_copy_metadata(result)
                stored = _copy_routing(result, source_metadata)
                metadata = _routing_copy_metadata(stored)
                cache[key] = (stored, metadata)
                return _copy_routing(result, source_metadata)
            return deepcopy(result)
        object.__setattr__(self, '_FrozenReader__route', route)
        object.__setattr__(self, '_FrozenReader__sealed', True)

    def __setattr__(self, name, value):
        if getattr(self, '_FrozenReader__sealed', False):
            raise AttributeError('frozen reader')
        object.__setattr__(self, name, value)

    def __call__(self, key):
        return self.__reader(key)

    def _routing(self, snapshot, run, source, addenda):
        return self.__route(snapshot, run, source, addenda)

    def _raw_dynamic_capture(self, input_hash):
        from ._prepared_mutation import _DynamicCapture, _SEAL, _require
        root = self.__view_root
        capture = root._dynamic_capture
        _require(type(self) is FrozenReader and type(root) is FrozenReader
            and type(capture) is _DynamicCapture and capture._DynamicCapture__seal is _SEAL
            and capture._DynamicCapture__input == input_hash and self._dynamic_capture is capture)
        return capture

    def _v2_history_view(self, ancestry, *, original_receipts_only=False):
        from .semantic_review_exception import _V2Ancestry
        from ._prepared_mutation import _SEAL, _require
        _require(type(self) is FrozenReader and self.__view_root is self
            and type(ancestry) is _V2Ancestry and ancestry._V2Ancestry__seal is _SEAL
            and ancestry._V2Ancestry__capture is self._dynamic_capture)
        hidden = ancestry._V2Ancestry__hidden
        if original_receipts_only:
            hidden = hidden | ancestry._V2Ancestry__later_children
        view = object.__new__(FrozenReader)
        for slot in FrozenReader.__slots__:
            name = '_FrozenReader' + slot if slot.startswith('__') else slot
            object.__setattr__(view, name, getattr(self,name))
        object.__setattr__(view,'_FrozenReader__prior_bundle_id',ancestry._V2Ancestry__bundle_id)
        object.__setattr__(view,'_FrozenReader__hidden',hidden)
        object.__setattr__(view,'_FrozenReader__reader',self)
        object.__setattr__(view,'_catalogue_frontiers',{})
        object.__setattr__(view,'task_rows',lambda identity:[row for row in self.task_rows(identity) if row['stage_hash'] not in hidden])
        object.__setattr__(view,'recovery_receipts',lambda identity:[row for row in self.recovery_receipts(identity) if row['child_stage_hash'] not in hidden])
        object.__setattr__(view,'d2_retry_rows',lambda identity:[a for a in self.d2_retry_rows(identity)
            if a['plan']['case']['parent_stage_hash'] not in hidden])
        return view

    def _without_packet(self, input_hash, packet_hash, *, source_only=False):
        """Derive a sealed task-filtered view while retaining SOURCE provenance.

        Only this exact factory-created reader can make a view; callers cannot
        supply accessors, arbitrary builders, routing values or an exclusion set.
        Equivalent cumulative selections reuse one view; distinct selections
        own independent frontier memos. Routing shares the original
        SOURCE-keyed, revalidated closure. Other reads delegate unchanged.
        """
        if type(self) is not FrozenReader:
            raise TypeError('frozen reader required')
        root = self.__view_root
        hidden = self.__hidden | frozenset(row['stage_hash'] for row in root.task_rows(input_hash)
            if row['stage'] in {'proposal', 'review'}
            and (source_only or row['data']['context'].get('packet_hash') == packet_hash))
        key = (input_hash, hidden)
        if key in root.__views:
            return root.__views[key]
        view = object.__new__(FrozenReader)
        object.__setattr__(view, '_FrozenReader__reader', root)
        object.__setattr__(view, '_FrozenReader__view_root', root)
        object.__setattr__(view, '_FrozenReader__hidden', hidden)
        object.__setattr__(view, '_FrozenReader__views', root.__views)
        object.__setattr__(view, '_FrozenReader__v2_proofs', root.__v2_proofs)
        object.__setattr__(view, '_FrozenReader__route', self.__route)
        object.__setattr__(view, '_source_capture', self._source_capture)
        object.__setattr__(view, '_dynamic_capture', root._dynamic_capture)
        object.__setattr__(view, '_FrozenReader__material', self.__material)
        object.__setattr__(view, '_FrozenReader__prior_bundle_id', self.__prior_bundle_id)
        object.__setattr__(view, '_catalogue_frontiers', {})
        for name in ('catalogue_source', 'catalogue_runs', 'raw_digest', 'raw_checkpoint_row',
                     'raw_error_code', 'supersession_receipts'):
            object.__setattr__(view, name, getattr(root, name))
        object.__setattr__(view, 'task_rows', lambda identity: [
            row for row in root.task_rows(identity) if row['stage_hash'] not in hidden])
        object.__setattr__(view, 'recovery_receipts', lambda identity: [
            row for row in root.recovery_receipts(identity) if row['child_stage_hash'] not in hidden])
        object.__setattr__(view,'d2_retry_rows',lambda identity:[a for a in root.d2_retry_rows(identity)
            if a['plan']['case']['parent_stage_hash'] not in hidden])
        object.__setattr__(view, '_FrozenReader__sealed', True)
        root.__views[key] = view
        return view
