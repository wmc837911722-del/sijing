"""Private one-attempt mutation evidence, never an authorization cache."""
_SEAL = object()


def _require(value):
    from .business_semantics import SemanticValidationError
    if not value:
        raise SemanticValidationError('cache_invalid')


class _DynamicCapture:
    __slots__ = ('__tables', '__input', '__seal', '__schemas')

    def __init__(self, tables, input_hash, *, schemas=(), _seal=None):
        _require(_seal is _SEAL)
        object.__setattr__(self, '_DynamicCapture__tables', tables)
        object.__setattr__(self, '_DynamicCapture__schemas', schemas)
        object.__setattr__(self, '_DynamicCapture__input', input_hash)
        object.__setattr__(self, '_DynamicCapture__seal', _SEAL)

    def __setattr__(self, name, value):
        raise AttributeError('sealed dynamic capture')

    def __reduce__(self):
        raise TypeError('dynamic capture cannot be serialized')


def _capture_dynamic(db, input_hash):
    """Capture complete current task and receipt raw SQL membership in one transaction."""
    _require(db.in_transaction)
    tables=[]
    schemas=[]
    for name in _RAW_SCHEMAS:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
        if not exists:
            if name == 'business_semantic_d2_attempts':
                continue  # Legacy database captures retain their exact digest.
            tables.append((name,None))
            schemas.append((name,None))
            continue
        schema=tuple(tuple(row) for row in db.execute('PRAGMA table_info('+name+')'))
        schemas.append((name,schema))
        columns=tuple(row[1] for row in schema)
        rows=tuple(tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid'))
        tables.append((name,(columns,rows)))
    return _DynamicCapture(tuple(tables),input_hash,schemas=tuple(schemas),_seal=_SEAL)


_RAW_SCHEMAS = {
    'business_semantic_tasks': ('stage_hash','stage','input_hash','status','attempt','data','error_code','created_at'),
    'business_semantic_recoveries': ('recovery_id','parent_stage_hash','child_stage_hash','input_hash','data','consumed_at','dispatch_id'),
    'business_semantic_supersessions': ('supersession_id','parent_stage_hash','child_stage_hash','input_hash','data','consumed_at','dispatch_id'),
    'business_semantic_d2_attempts': ('stage_hash','input_hash','data','consumed_at','dispatch_id'),
}


def _raw_table(capture, *, input_hash, table):
    """Read actual SQL scalars; bound the whole root before returning a table."""
    _require(type(capture) is _DynamicCapture and capture._DynamicCapture__seal is _SEAL
        and capture._DynamicCapture__input == input_hash)
    tables = dict(capture._DynamicCapture__tables)
    schemas = dict(capture._DynamicCapture__schemas)
    count = sum(len(entry[1]) for entry in tables.values() if entry is not None)
    _require(count <= 8192)
    total = 0
    for name, entry in tables.items():
        if entry is None:
            continue
        columns, rows = entry
        _require(columns == _RAW_SCHEMAS[name])
        expected_types = tuple('INTEGER' if column in {'attempt','consumed_at'} else 'TEXT' for column in columns)
        _require(tuple(row[2].upper() for row in schemas[name]) == expected_types
            and len({row[0] for row in rows}) == len(rows))
        for row in rows:
            _require(len(row) == len(columns) and type(row[columns.index('data')]) is str)
            for value in row:
                _require(type(value) in (str, int, float, bytes, type(None)))
                if type(value) is str:
                    try:
                        total += len(value.encode('utf-8'))
                    except UnicodeError:
                        _require(False)
                elif type(value) is bytes:
                    total += len(value)
                else:
                    total += 16
            _require(total <= 512 * 1024 * 1024)
    entry = tables.get(table)
    _require(entry is not None)
    columns, rows = entry
    return tuple(sorted(rows, key=lambda row: row[0]))


def _raw_recovery_rows(capture, *, input_hash):
    return tuple(row for row in _raw_table(capture, input_hash=input_hash, table='business_semantic_recoveries') if row[3] == input_hash)


def _raw_task_rows(capture, *, input_hash):
    return tuple(row for row in _raw_all_task_rows(capture, input_hash=input_hash) if row[2] == input_hash)


def _raw_all_task_rows(capture, *, input_hash):
    """Unfiltered actual RAW8, anchored to the selected factory's input/epoch."""
    return _raw_table(capture, input_hash=input_hash, table='business_semantic_tasks')


def _dynamic_digest(capture, *, input_hash):
    """Complete schema/presence/row-order CAS digest with typed SQLite scalars."""
    import struct
    from .business_semantics import _hash, _json
    _require(type(capture) is _DynamicCapture and capture._DynamicCapture__seal is _SEAL
        and capture._DynamicCapture__input == input_hash)
    def typed(value):
        if value is None: return ['null', None]
        if type(value) is str: return ['text', value]
        if type(value) is bytes: return ['blob', value.hex()]
        if type(value) is int: return ['integer', str(value)]
        if type(value) is float: return ['real', struct.pack('>d',value).hex()]
        _require(False)
    encoded=[]
    count=0
    schemas=dict(capture._DynamicCapture__schemas)
    for table, entry in capture._DynamicCapture__tables:
        if entry is None:
            encoded.append([table,None,None]);continue
        columns, rows=entry
        count += len(rows)
        _require(count <= 8192)
        encoded.append([table,[[typed(v) for v in row] for row in schemas[table]],
            [[typed(v) for v in row] for row in rows]])
    try:
        _require(len(_json(encoded).encode('utf-8')) <= 32 * 1024 * 1024)
    except UnicodeError:
        _require(False)
    return _hash(encoded)


def _same_dynamic(left,right):
    _require(type(left) is _DynamicCapture and type(right) is _DynamicCapture
        and left._DynamicCapture__seal is _SEAL and right._DynamicCapture__seal is _SEAL)
    return (left._DynamicCapture__input==right._DynamicCapture__input
        and left._DynamicCapture__tables==right._DynamicCapture__tables
        and left._DynamicCapture__schemas==right._DynamicCapture__schemas)


class _PreparedMutation:
    """Sealed evidence for exactly one validation/commit attempt; not authority."""
    __slots__=('__operation','__scope','__target','__dynamic','__stamp','__material','__runtime','__used','__review_exception_proof','__review_exception_authorization')

    def __init__(self, operation, scope, target, dynamic, stamp, material, *, runtime=None, review_exception_proof=None, review_exception_authorization=None, _seal=None):
        _require(_seal is _SEAL and operation in {'authorize_proposal_recovery','authorize_review_recovery','reauthorize_review_recovery','authorize_review_exception_bundle','consume_recovery','consume_supersession','save_proposed','save_accepted'})
        for name,value in (('operation',operation),('scope',scope),('target',target),('dynamic',dynamic),('stamp',stamp),('material',material),('runtime',runtime),('review_exception_proof',review_exception_proof),('review_exception_authorization',review_exception_authorization),('used',[False])):
            object.__setattr__(self,'_PreparedMutation__'+name,value)

    def __setattr__(self,name,value):
        raise AttributeError('sealed prepared mutation')

    def __reduce__(self):
        raise TypeError('prepared mutation cannot be serialized')


class _ClaimEvidence:
    """Opaque evidence of a claim returned only after its transaction commits."""
    __slots__ = ('__encoded','__seal')
    def __init__(self, receipt, *, _seal=None):
        import json
        _require(_seal is _SEAL and type(receipt) is dict)
        object.__setattr__(self, '_ClaimEvidence__encoded', json.dumps(receipt,sort_keys=True,ensure_ascii=False,separators=(',',':')))
        object.__setattr__(self, '_ClaimEvidence__seal', _SEAL)
    def __setattr__(self, name, value):
        raise AttributeError('sealed claim evidence')
    def __reduce__(self):
        raise TypeError('claim evidence cannot be serialized')


def _claim_evidence(receipt):
    return _ClaimEvidence(receipt,_seal=_SEAL)


def _claim_matches(evidence, receipt):
    import json
    return (type(evidence) is _ClaimEvidence and evidence._ClaimEvidence__seal is _SEAL
        and type(receipt) is dict and evidence._ClaimEvidence__encoded == json.dumps(receipt,sort_keys=True,ensure_ascii=False,separators=(',',':')))


class _LegacyMaterial:
    """One-attempt raw provenance for legacy catalogue before Source completion."""
    __slots__ = ('__capture','__seal')
    def __init__(self, reader, *, _seal=None):
        from ._source_material import _capture
        _require(_seal is _SEAL)
        object.__setattr__(self, '_LegacyMaterial__capture', _capture(reader))
        object.__setattr__(self, '_LegacyMaterial__seal', _SEAL)
    def __setattr__(self, name, value):
        raise AttributeError('sealed legacy capture')
    def __reduce__(self):
        raise TypeError('legacy capture cannot be serialized')


def _legacy_compiler_stamp():
    """Bind compiler code and closures without requiring cached-routing provenance."""
    import hashlib, importlib, inspect, json, re
    from types import ModuleType, FunctionType, BuiltinFunctionType
    from . import _source_material as m, business_content
    try:
        entries=[('url-normalizer',m._url_normalizer_value(business_content.unquote,set()))]
        for short_name in m._COMPILER_MODULES:
            module=importlib.import_module('.'+short_name, __package__)
            for name,value in sorted(vars(module).items()):
                if name.startswith('__') or name in m._INFRASTRUCTURE_GLOBALS:
                    continue
                if isinstance(value,ModuleType):
                    entries.append((module.__name__,name,'module',id(value)))
                elif short_name=='business_semantics' and name=='BusinessSemanticIntegration':
                    names=('_call','_call_admitted','_prepare_mutation','_save_task_once','_consume_prepared_supersession')
                    entries.append((module.__name__,name,id(value),tuple((n,m._policy_value(getattr(value,n),set())) for n in names)))
                elif short_name=='_source_material' and name=='_SEAL':
                    entries.append((module.__name__,name,id(value)))
                else:
                    entries.append((module.__name__,name,m._policy_value(value,set())))
        external=[json,json.encoder,json.decoder,re,hashlib,business_content.html,
            business_content.html.entities,business_content.unicodedata,business_content.yaml]
        external.extend(importlib.import_module('yaml.'+name) for name in
            ('loader','reader','scanner','parser','composer','constructor','resolver','nodes','events','tokens','error'))
        for module in external:
            entries.append((module.__name__,tuple((name,m._policy_value(value,set()))
                for name,value in vars(module).items() if not name.startswith('__') and name not in ('_cache','_cache2')
                and (isinstance(value,(FunctionType,BuiltinFunctionType,re.Pattern)) or inspect.isclass(value)
                     or type(value) in (str,int,bool,tuple,frozenset,dict)))))
        entries.append(('json-decoder',id(json._default_decoder),tuple((name,id(value) if name=='scan_once' else m._policy_value(value,set()))
            for name,value in vars(json._default_decoder).items())))
        entries.append(('json-encoder',id(json._default_encoder),m._policy_value(vars(json._default_encoder),set())))
        return hashlib.sha256(repr(entries).encode()).hexdigest()
    except (TypeError,ValueError,AttributeError,RecursionError):
        return None


def _mutation_stamp(*, legacy=False):
    """Bind all active Source policy plus this helper's code and closed roles."""
    from ._source_material import _compiler_stamp,_policy_value
    try:
        source=_legacy_compiler_stamp() if legacy else _compiler_stamp()
        _require(source is not None)
        names=('_require','_raw_table','_raw_recovery_rows','_raw_task_rows','_raw_all_task_rows','_dynamic_digest','_assert_bundle_authorization_delta','_claim_evidence','_claim_matches','_capture_dynamic','_same_dynamic','_legacy_compiler_stamp','_mutation_stamp','_scope','_known_peer_drift','_consume_prepared','_assert_authorization_delta','_assert_proposal_consumption_delta','_assert_postwrite_closure','_fresh_packet_occupied')
        return (source,tuple((name,_policy_value(globals()[name],set())) for name in names),
            tuple(sorted(_RAW_SCHEMAS.items())),
            ('authorize_proposal_recovery','authorize_review_recovery','reauthorize_review_recovery','authorize_review_exception_bundle','consume_recovery','consume_supersession','save_proposed','save_accepted'),
            tuple(_policy_value(fn,set()) for fn in (_DynamicCapture.__init__,_PreparedMutation.__init__,_PreparedMutation.__setattr__,_ClaimEvidence.__init__,_ClaimEvidence.__setattr__,_LegacyMaterial.__init__,_LegacyMaterial.__setattr__)))
    except (TypeError,ValueError,AttributeError,RecursionError):
        from .business_semantics import SemanticValidationError
        raise SemanticValidationError('cache_invalid') from None


def _scope(snapshot,run):
    from .business_semantics import _hash
    return (snapshot['project']['id'],run['run_id'],run['semantic_input_hash'],run['snapshot_id'],run['project_revision'],_hash(snapshot))



class _KnownPeerDrift(Exception):
    """Fresh prepare required; the selected child has not been consumed."""


class _AuthorizationPeerDrift(Exception):
    """An authorization snapshot changed before any write in this transaction."""


def _known_peer_drift(before, after, *, target, runtime):
    import json
    old=dict(before._DynamicCapture__tables);new=dict(after._DynamicCapture__tables)
    if old.get('business_semantic_d2_attempts') != new.get('business_semantic_d2_attempts'):
        return False
    if old['business_semantic_supersessions'] != new['business_semantic_supersessions']:
        return False
    left=old['business_semantic_tasks'];right=new['business_semantic_tasks']
    if left is None or right is None or left[0]!=right[0] or len(left[1])>len(right[1]): return False
    changed=set()
    for a,b in zip(left[1],right[1]):
        if a==b: continue
        if (a[0]!=b[0] or a[0]==target or a[1] not in {'proposal','review'} or a[:3]!=b[:3]
                or a[4]!=b[4] or a[7]!=b[7]): return False
        allowed={'planned':{'running'},'running':{'received','proposed','accepted','needs_review'},
                 'received':{'proposed','accepted','needs_review'}}
        if b[3] not in allowed.get(a[3],set()): return False
        da,db=json.loads(a[5]),json.loads(b[5])
        keys=('request_hash','request','context','recovery_id','logical_task_hash','supersession_id')
        if any(da.get(k)!=db.get(k) for k in keys): return False
        if a[3]=='planned' and (a[5]!=b[5] or a[6]!=b[6]): return False
        changed.add(a[0])
    for row in right[1][len(left[1]):]:
        if (row[0]==target or row[1] not in {'proposal','review'} or row[2]!=before._DynamicCapture__input
                or row[3] not in {'running','received','proposed','accepted','needs_review'}): return False
        data=json.loads(row[5])
        if data.get('request_hash')!=row[0] or not isinstance(data.get('request'),dict) or not isinstance(data.get('context'),dict): return False
        changed.add(row[0])
    if not changed: return False
    # SQL was already captured before this append-only live ownership read.
    with runtime.store._lock: owned=frozenset(runtime._owned_slots)
    if not changed <= owned: return False
    old_rec=old['business_semantic_recoveries'];new_rec=new['business_semantic_recoveries']
    if old_rec==new_rec: return True
    if old_rec is None or new_rec is None or old_rec[0]!=new_rec[0] or len(old_rec[1])!=len(new_rec[1]): return False
    for a,b in zip(old_rec[1],new_rec[1]):
        if a==b: continue
        if a[:5]!=b[:5] or a[2] not in changed or a[2]==target or a[5:]!=(None,None): return False
        if type(b[5]) is not int or not isinstance(b[6],str) or len(b[6])!=64: return False
    return True

def _consume_prepared(prepared, *, operation, snapshot, run, target, material, db):
    """Burn one prepared attempt after verifying current raw and code identity."""
    from ._source_material import _bind_raw_source
    _require(type(prepared) is _PreparedMutation and prepared._PreparedMutation__used==[False]
        and prepared._PreparedMutation__operation==operation and prepared._PreparedMutation__scope==_scope(snapshot,run)
        and prepared._PreparedMutation__target==target and prepared._PreparedMutation__material is material
        and prepared._PreparedMutation__stamp==_mutation_stamp(legacy=type(material) is _LegacyMaterial))
    prepared._PreparedMutation__used[0]=True
    fresh_dynamic = _capture_dynamic(db,run['semantic_input_hash'])
    if type(material) is _LegacyMaterial:
        from ._source_material import _capture_raw_source, _same_raw
        _require(material._LegacyMaterial__seal is _SEAL and operation in {'consume_recovery','consume_supersession','save_proposed','save_accepted'}
            and _same_raw(material._LegacyMaterial__capture, _capture_raw_source(db,run['semantic_input_hash'])))
    else:
        _bind_raw_source(material, snapshot, run, db=db)
    runtime = prepared._PreparedMutation__runtime
    if not _same_dynamic(prepared._PreparedMutation__dynamic,fresh_dynamic):
        if operation in {'authorize_proposal_recovery','authorize_review_recovery','reauthorize_review_recovery','authorize_review_exception_bundle'}:
            raise _AuthorizationPeerDrift()
        if runtime is not None and _known_peer_drift(prepared._PreparedMutation__dynamic,fresh_dynamic,target=target[0],runtime=runtime):
            raise _KnownPeerDrift()
        _require(False)
    if operation not in {'authorize_proposal_recovery','authorize_review_recovery','reauthorize_review_recovery','authorize_review_exception_bundle'}:
        from .business_semantics import BusinessSemanticIntegration, _hash
        _require(type(runtime) is BusinessSemanticIntegration and runtime._input_hash == run['semantic_input_hash']
            and runtime._run['run_id'] == run['run_id'] and target[0] in runtime._task_bindings
            and _hash(runtime._task_bindings[target[0]]) == target[1])
        running = [row[0] for row in db.execute("SELECT stage_hash FROM business_semantic_tasks WHERE input_hash=? AND status='running'", (run['semantic_input_hash'],))]
        # SQL first; this getter is the live runtime's own append-only set.
        with runtime.store._lock:
            owned = frozenset(runtime._owned_slots)
        import json
        historical = set()
        table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_semantic_recoveries'").fetchone()
        if table:
            for raw in db.execute('SELECT data FROM business_semantic_recoveries WHERE input_hash=?',(run['semantic_input_hash'],)):
                item=json.loads(raw[0])
                if item.get('policy')=='operator-bounded-catalogue-interrupted-once-v1':
                    historical.add(item['parent_stage_hash'])
        proof = prepared._PreparedMutation__review_exception_proof
        if proof is not None:
            from .semantic_review_exception import _V2BundleProof
            _require(type(proof) is _V2BundleProof and proof._V2BundleProof__seal is _SEAL
                and proof._V2BundleProof__capture is prepared._PreparedMutation__dynamic
                and proof._V2BundleProof__snapshot == _hash(snapshot))
            plan = json.loads(proof._V2BundleProof__ancestry._V2Ancestry__members._V2RawMembers__encoded)[0]
            historical.update(member['parent_stage_hash'] for member in plan['members'])
        _require(all(key in owned or key in historical for key in running))
        runtime.guard()
    return runtime


def _assert_authorization_delta(prepared, db, *, task_row, receipt_row):
    """Require exactly the two intended writes, including all other raw rows."""
    before=dict(prepared._PreparedMutation__dynamic._DynamicCapture__tables)
    after=dict(_capture_dynamic(db,prepared._PreparedMutation__dynamic._DynamicCapture__input)._DynamicCapture__tables)
    old_tasks=before['business_semantic_tasks'];new_tasks=after['business_semantic_tasks']
    _require(old_tasks is not None and new_tasks is not None and old_tasks[0]==new_tasks[0]
        and new_tasks[1]==old_tasks[1]+(task_row,))
    old_receipts=before['business_semantic_recoveries'];new_receipts=after['business_semantic_recoveries']
    expected_columns=('recovery_id','parent_stage_hash','child_stage_hash','input_hash','data','consumed_at','dispatch_id')
    _require(new_receipts is not None and new_receipts[0]==expected_columns
        and (old_receipts is None or old_receipts[0]==expected_columns)
        and new_receipts[1]==(() if old_receipts is None else old_receipts[1])+(receipt_row,))
    _require(before.get('business_semantic_d2_attempts') == after.get('business_semantic_d2_attempts'))
    _require(before['business_semantic_supersessions']==after['business_semantic_supersessions'])


def _assert_bundle_authorization_delta(prepared, db, *, task_rows, receipt_rows):
    """Exactly four new rows; every old SQL byte and the third table stay fixed."""
    _require(len(task_rows) == len(receipt_rows) == 2)
    before=dict(prepared._PreparedMutation__dynamic._DynamicCapture__tables)
    after_capture=_capture_dynamic(db,prepared._PreparedMutation__dynamic._DynamicCapture__input)
    after=dict(after_capture._DynamicCapture__tables)
    _require(prepared._PreparedMutation__dynamic._DynamicCapture__schemas == after_capture._DynamicCapture__schemas)
    for table, extra in (('business_semantic_tasks',task_rows),('business_semantic_recoveries',receipt_rows)):
        old,new=before[table],after[table]
        _require(old is not None and new is not None and old[0] == new[0] == _RAW_SCHEMAS[table]
            and new[1] == old[1] + extra)
    _require(before['business_semantic_supersessions'] == after['business_semantic_supersessions'])


def _assert_proposal_consumption_delta(prepared, db, *, child, stamp, dispatch):
    before=dict(prepared._PreparedMutation__dynamic._DynamicCapture__tables)
    after=dict(_capture_dynamic(db,prepared._PreparedMutation__dynamic._DynamicCapture__input)._DynamicCapture__tables)
    _require(before.get('business_semantic_d2_attempts') == after.get('business_semantic_d2_attempts'))
    _require(before['business_semantic_supersessions']==after['business_semantic_supersessions'])
    for table in ('business_semantic_tasks','business_semantic_recoveries'):
        a,b=before[table],after[table]
        _require(a is not None and b is not None and a[0]==b[0] and len(a[1])==len(b[1]))
        changed=0
        for old,new in zip(a[1],b[1]):
            if old==new: continue
            if table=='business_semantic_tasks':
                _require(old[0]==child and old[3]=='planned' and new==old[:3]+('running',)+old[4:])
            else:
                _require(old[2]==child and old[5:] == (None,None) and new==old[:5]+(stamp,dispatch))
            changed+=1
        _require(changed==1)


def _assert_postwrite_closure(prepared, db, *, snapshot, run, child, status, data=None, error=None,
                              receipt_table=None, stamp=None, dispatch=None):
    """Validate only the intended task/receipt writes and fresh factory RAW Source."""
    from ._source_material import _capture_raw_source, _bind_raw_source
    material=prepared._PreparedMutation__material
    legacy=type(material) is _LegacyMaterial
    _require(prepared._PreparedMutation__stamp==_mutation_stamp(legacy=legacy))
    before=dict(prepared._PreparedMutation__dynamic._DynamicCapture__tables)
    after=dict(_capture_dynamic(db,run['semantic_input_hash'])._DynamicCapture__tables)
    _require(before.get('business_semantic_d2_attempts') == after.get('business_semantic_d2_attempts'))
    changes={}
    for table in ('business_semantic_tasks','business_semantic_recoveries','business_semantic_supersessions'):
        old,new=before[table],after[table]
        _require((old is None)==(new is None))
        if old is None: continue
        _require(old[0]==new[0] and len(old[1])==len(new[1]))
        expected=[];changed=0
        for row in old[1]:
            replacement=row
            if table=='business_semantic_tasks' and row[0]==child:
                _require(row[3] in ({'planned'} if receipt_table else {'running','received'}))
                replacement=(row[:3]+(status,row[4],data,error,row[7])) if receipt_table is None else (row[:3]+(status,)+row[4:])
                changed+=1
            elif table==receipt_table and row[2]==child:
                _require(row[5:]==(None,None) and type(stamp) is int and isinstance(dispatch,str))
                replacement=row[:5]+(stamp,dispatch);changed+=1
            if replacement!=row: changes.setdefault(table,[]).append((row,replacement))
            expected.append(replacement)
        _require(tuple(expected)==new[1])
        if table=='business_semantic_tasks' or table==receipt_table:
            _require(changed==1)
    if not legacy:
        _bind_raw_source(material,snapshot,run,db=db)
        return
    _require(material._LegacyMaterial__seal is _SEAL)
    raw_before=material._LegacyMaterial__capture
    raw_after=_capture_raw_source(db,run['semantic_input_hash'])
    _require(raw_before._RawSourceCapture__input==raw_after._RawSourceCapture__input)
    expected_source=[]
    for table,entry in raw_before._RawSourceCapture__rows:
        if table in changes and entry:
            schema,rows=entry
            replacements=dict(changes[table])
            entry=(schema,tuple(replacements.get(row,row) for row in rows))
        expected_source.append((table,entry))
    _require(tuple(expected_source)==raw_after._RawSourceCapture__rows)


def _fresh_packet_occupied(db, input_hash, packet_hash):
    """Recount physical proposal/review occupancies from current raw SQL."""
    import json
    count=0
    for stage,status,encoded in db.execute("SELECT stage,status,data FROM business_semantic_tasks WHERE input_hash=? AND stage IN ('proposal','review')",(input_hash,)):
        data=json.loads(encoded)
        if data['context'].get('packet_hash')==packet_hash and status!='planned':
            count+=1
    return count
