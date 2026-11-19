"""Source compiler tests use only fixture-owned SQLite databases."""
import pytest
from copy import deepcopy
from businessmap import business_semantics as s
from test_proposal_timeout_recovery import source_timeout
from test_semantic_supersession import completed_legacy_catalogue, legacy_case
from test_whole_operation_exact_fit import mixed_source_case, whole_semantic_case, whole_parent_case


def capture(c):
    store = c.store if hasattr(c, 'store') else c.service.store
    with store._db() as db:
        return s.semantic_checkpoint_rows(db, c.run['semantic_input_hash'])


def compile_case(c):
    from businessmap._source_material import prepare_source_material
    return prepare_source_material(c.snapshot, c.run, read_checkpoint=capture(c).read_checkpoint)


def bound_rows(c, material):
    from businessmap._source_material import bind_source_material
    rows = capture(c)
    rows.read_checkpoint = bind_source_material(material, c.snapshot, c.run, read_checkpoint=rows.read_checkpoint)
    return rows


def test_material_binds_only_equal_fresh_raw(source_timeout):
    from businessmap._source_material import prepare_source_material, bind_source_material
    c = source_timeout
    with c.store._db() as db:
        first = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    material = prepare_source_material(c.snapshot, c.run, read_checkpoint=first)
    with c.store._db() as db:
        second = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    bound = bind_source_material(material, c.snapshot, c.run, read_checkpoint=second)
    assert bound is not first and bound is not second
    assert bound.task_rows(c.run['semantic_input_hash']) == second.task_rows(c.run['semantic_input_hash'])
    with c.store._db() as db:
        db.execute("UPDATE full_analysis_chunks SET data=data || ' '")
    with c.store._db() as db:
        changed = s.semantic_checkpoint_rows(db, c.run['semantic_input_hash']).read_checkpoint
    with pytest.raises(s.SemanticValidationError):
        bind_source_material(material, c.snapshot, c.run, read_checkpoint=changed)


@pytest.mark.parametrize('mutation', ('chunk_bytes', 'l0_bytes', 'extra_l0', 'missing_l0',
    'extra_run', 'unknown_run', 'stale', 'modules', 'extra_static_task', 'missing_static_task', 'addendum', 'matching',
    'schema', 'missing_table'))
def test_material_raw_membership_and_unknown_fields_invalidate(source_timeout, mutation):
    c = source_timeout
    material = compile_case(c)
    with c.store._db() as db:
        if mutation == 'chunk_bytes':
            db.execute("UPDATE full_analysis_chunks SET data=data || ' '")
        elif mutation == 'l0_bytes':
            db.execute("UPDATE full_analysis_synthesis SET data=data || ' '")
        elif mutation == 'extra_l0':
            db.execute("INSERT INTO full_analysis_synthesis SELECT 'other',input_hash,data,created_at FROM full_analysis_synthesis LIMIT 1")
        elif mutation == 'missing_l0':
            db.execute('DELETE FROM full_analysis_synthesis')
        elif mutation == 'extra_run':
            run = {**c.run, 'run_id': 'extra'}
            db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)', ('extra',run['project_id'],run['snapshot_id'],s._json(run),'extra'))
        elif mutation in ('unknown_run', 'stale', 'modules'):
            run = deepcopy(c.run)
            if mutation == 'modules': run['modules'].append({'id': 'changed'})
            else: run[mutation] = True
            db.execute('UPDATE full_analysis_runs SET data=?', (s._json(run),))
        elif mutation == 'extra_static_task':
            db.execute("INSERT INTO business_semantic_tasks SELECT 'extra',stage,input_hash,status,attempt,data,error_code,created_at FROM business_semantic_tasks WHERE stage='catalogue' LIMIT 1")
        elif mutation == 'missing_static_task':
            db.execute("DELETE FROM business_semantic_tasks WHERE stage='catalogue'")
        elif mutation == 'schema': db.execute('ALTER TABLE full_analysis_chunks ADD COLUMN extra TEXT')
        elif mutation == 'missing_table': db.execute('DROP TABLE analysis_addenda')
        else:
            table = 'analysis_addenda' if mutation == 'addendum' else 'analysis_matching_receipts'
            db.execute('INSERT INTO '+table+' VALUES (?,?,?)', ('extra',c.run['run_id'],'{}'))
    with pytest.raises(s.SemanticValidationError):
        bound_rows(c, material)


def test_material_run_progress_allowlist(source_timeout):
    from businessmap._source_material import _RUN_PROGRESS
    c = source_timeout
    material = compile_case(c)
    changed = {**c.run, **{name: 'new-progress' for name in _RUN_PROGRESS}}
    with c.store._db() as db:
        db.execute('UPDATE full_analysis_runs SET data=?,updated_at=?', (s._json(changed),'new-progress'))
    rows = bound_rows(c, material)
    assert rows.read_checkpoint.catalogue_source(c.run['run_id'])['run'] == changed


def test_progress_replacement_preserves_keyed_source_membership(source_timeout):
    c = source_timeout
    other = {**c.run,'run_id':'z-other','project_id':'other-project'}
    with c.store._db() as db:
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',
            (other['run_id'],other['project_id'],other['snapshot_id'],s._json(other),'other'))
    material = compile_case(c)
    changed = {**c.run,'status':'running','updated_at':'new-progress'}
    with c.store._db() as db:
        # The production table has a primary key: INSERT OR REPLACE changes
        # rowid. This fixture's simpler schema emulates its delete/insert.
        db.execute('DELETE FROM full_analysis_runs WHERE id=?',(c.run['run_id'],))
        db.execute('INSERT INTO full_analysis_runs VALUES (?,?,?,?,?)',
            (c.run['run_id'],c.run['project_id'],c.run['snapshot_id'],s._json(changed),'new-progress'))
    assert bound_rows(c,material).read_checkpoint.catalogue_source(c.run['run_id'])['run'] == changed


def test_ambiguous_chunk_positions_never_enter_compiled_material(source_timeout):
    from businessmap._source_material import prepare_source_material
    c = source_timeout
    material = compile_case(c)
    with c.store._db() as db:
        db.execute('INSERT INTO full_analysis_chunks SELECT run_id,position,data FROM full_analysis_chunks LIMIT 1')
    rows = capture(c)
    # A raw reader still describes the actual SQL snapshot. The compiler
    # rejects ambiguous tie order rather than silently treating it as a set.
    assert len(rows.read_checkpoint.catalogue_source(c.run['run_id'])['chunks']) == 2
    with pytest.raises(s.SemanticValidationError):
        prepare_source_material(c.snapshot,c.run,read_checkpoint=rows.read_checkpoint)
    with pytest.raises(s.SemanticValidationError):
        bound_rows(c,material)


def test_material_rejects_forged_readers_and_values(source_timeout):
    from businessmap._source_material import SourceMaterial, _RawSourceCapture, prepare_source_material, bind_source_material
    from businessmap._frozen_routing import FrozenReader
    c = source_timeout
    with pytest.raises(s.SemanticValidationError):
        SourceMaterial(None, None, None, None, None)
    with pytest.raises(s.SemanticValidationError):
        _RawSourceCapture((), c.run['semantic_input_hash'])
    with pytest.raises(s.SemanticValidationError):
        prepare_source_material(c.snapshot,c.run,read_checkpoint=lambda _: None)
    with pytest.raises(s.SemanticValidationError):
        bind_source_material({},c.snapshot,c.run,read_checkpoint=capture(c).read_checkpoint)
    with pytest.raises(s.SemanticValidationError):
        prepare_source_material(c.snapshot,c.run,read_checkpoint=object.__new__(FrozenReader))
    material = compile_case(c)
    with pytest.raises(AttributeError): material.extra = True
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(material)


@pytest.mark.parametrize('change', ('scope', 'input', 'policy', 'function', 'code', 'default', 'closure', 'schema',
                                  'decoder', 'encoder', 'yaml_policy', 'module_alias'))
def test_material_policy_and_scope_invalidation(source_timeout, monkeypatch, change):
    from businessmap import _source_material as m, codex_provider
    c = source_timeout
    material = compile_case(c)
    reader = capture(c).read_checkpoint
    snapshot, run = deepcopy(c.snapshot), deepcopy(c.run)
    if change == 'scope': snapshot['vault_path'] += '-changed'
    elif change == 'input': run['semantic_input_hash'] = '0'*64
    elif change == 'policy': monkeypatch.setattr(s,'CATEGORIES',tuple(s.CATEGORIES)+('changed',))
    elif change == 'function': monkeypatch.setattr(s,'semantic_output_schema',lambda *a: {})
    elif change == 'code':
        def replacement(value): return 'changed'
        monkeypatch.setattr(s._hash, '__code__', replacement.__code__)
    elif change == 'default': monkeypatch.setattr(codex_provider._request_payload,'__defaults__',({'changed':True},))
    elif change == 'closure':
        state = {'changed': True}
        monkeypatch.setattr(s,'semantic_output_schema',lambda *a: state)
    elif change == 'schema': monkeypatch.setattr(codex_provider,'_request_payload',lambda *a,**k: {})
    elif change == 'decoder': monkeypatch.setattr(m.json._default_decoder,'parse_constant',lambda value: None)
    elif change == 'encoder': monkeypatch.setattr(m.json.encoder.JSONEncoder,'iterencode',lambda *a,**k: iter(()))
    elif change == 'yaml_policy':
        import yaml
        monkeypatch.setattr(yaml.resolver.Resolver,'yaml_implicit_resolvers',{})
    elif change == 'module_alias':
        from types import ModuleType
        from businessmap import semantic_supersession as sup
        monkeypatch.setattr(sup,'s',ModuleType('untrusted'))
    with pytest.raises(s.SemanticValidationError):
        m.bind_source_material(material,snapshot,run,read_checkpoint=reader)


def test_material_rechecks_code_on_already_bound_reader(source_timeout, monkeypatch):
    from businessmap import semantic_supersession as sup
    c = source_timeout
    reader = bound_rows(c, compile_case(c)).read_checkpoint
    monkeypatch.setattr(s,'PROPOSAL_SYSTEM',s.PROPOSAL_SYSTEM+' changed')
    with pytest.raises(s.SemanticValidationError):
        sup._source_routing(c.snapshot,c.run,reader)


def test_material_reuses_pure_templates_but_not_dynamic_inventory(source_timeout):
    from businessmap import semantic_proposal_recovery as recovery, semantic_supersession as sup
    import sys
    c = source_timeout
    expected_rows = capture(c)
    expected = recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=expected_rows.read_checkpoint)
    material = compile_case(c)
    calls = []
    codes = {sup._derive_source_routing.__code__, recovery._packet_templates.__code__}
    def profile(frame,event,arg):
        if event == 'call' and frame.f_code in codes: calls.append(frame.f_code.co_name)
    sys.setprofile(profile)
    try:
        for _ in range(2):
            rows = bound_rows(c,material)
            assert recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,c.parent['stage_hash'],read_checkpoint=rows.read_checkpoint) == expected
            first = sup._source_routing(c.snapshot,c.run,rows.read_checkpoint)
            first[0].clear()
            assert sup._source_routing(c.snapshot,c.run,rows.read_checkpoint)[0]
    finally:
        sys.setprofile(None)
    assert calls == []
    with c.store._db() as db:
        db.execute("INSERT INTO business_semantic_tasks SELECT 'extra',stage,input_hash,status,attempt,data,error_code,created_at FROM business_semantic_tasks WHERE stage='proposal' LIMIT 1")
    rows = bound_rows(c, material)
    with pytest.raises(s.SemanticValidationError):
        s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])


def test_material_project_lock_is_fresh_not_compiler_data(source_timeout):
    from businessmap import semantic_recovery as recovery
    from businessmap._source_material import _bind_raw_source
    c = source_timeout
    material = compile_case(c)
    with recovery.held_project_lock(c.snapshot['project']['id']):
        recovery._require_project_lock(c.snapshot['project']['id'])
        bound_rows(c, material)
        with c.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            assert _bind_raw_source(material,c.snapshot,c.run,db=db) is None
    with pytest.raises(s.SemanticValidationError):
        recovery._require_project_lock(c.snapshot['project']['id'])


def test_runtime_allowlists_are_not_source_compiler_inputs(source_timeout, monkeypatch):
    from businessmap._source_material import _RUNTIME_ALLOWLISTS
    c = source_timeout
    material = compile_case(c)
    for name in _RUNTIME_ALLOWLISTS:
        monkeypatch.setattr(s.BusinessSemanticIntegration,name,frozenset({'current-selection'}))
    assert bound_rows(c,material).read_checkpoint(c.parent['stage_hash']) == c.parent


def test_compiler_stamp_and_fresh_binding_are_concurrent_stable(source_timeout):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap._source_material import _compiler_stamp
    c = source_timeout
    material = compile_case(c)
    stamp = _compiler_stamp()
    barrier = Barrier(2)
    def read():
        barrier.wait()
        for _ in range(8):
            rows = bound_rows(c,material)
            assert _compiler_stamp() == stamp
            assert rows.read_checkpoint(c.parent['stage_hash']) == c.parent
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:read(),range(2)))


_OLD_DEPENDENCIES = [
    ('businessmap.full_synthesis', name) for name in ('_json','_hash','_unique','_business_clauses',
        '_stylesheet_statement','_internal_basis','_text_policy','source_fact_units','_observation')]
_OLD_DEPENDENCIES += [('businessmap.l0_legacy',name) for name in
    ('_json','_hash','_unique','_business_clauses','_stylesheet_statement','_internal_basis','_text_policy')]
_OLD_DEPENDENCIES += [('businessmap.business_content',name) for name in
    ('_reject','_canonical','implementation_identifiers','public_business_metadata','validate_public_business_text','unquote')]
_OLD_DEPENDENCIES += [(module+'.'+cls,name) for module,cls,names in (
    ('businessmap.full_synthesis','FullBusinessSynthesis',('VERSION','L0_ROUTING_VERSION','WIRE_BYTES','MAX_OUTPUT','MAX_ITEMS','SYSTEM','_wire','_batches','_validate','cache_identity','validate_cache','navigation')),
    ('businessmap.l0_legacy','LegacyL0',('VERSION','L0_ROUTING_VERSION','WIRE_BYTES','MAX_OUTPUT','MAX_ITEMS','SYSTEM','_wire','_batches','_validate')))
    for name in names]
_OLD_DEPENDENCIES += [(module,name) for module,names in (
    ('html',('unescape',)),('unicodedata',('normalize',)),('yaml',('safe_load',)),
    ('json',('dumps','loads')),('hashlib',('sha256',)),('re',('search','finditer','sub','split')),
    ('businessmap.providers',('provider_destination',)),
    ('businessmap.semantic_supersession',('_derive_source_routing','_require')),
    ('businessmap.business_semantics',('_require','_hash','_json'))) for name in names]


@pytest.mark.parametrize('owner,name',_OLD_DEPENDENCIES)
def test_canonical_stamp_covers_old_routing_dependency_values(monkeypatch,owner,name):
    import importlib
    from types import BuiltinFunctionType
    from businessmap._source_material import _compiler_stamp
    from businessmap._frozen_routing import dependency_stamp
    try: target=importlib.import_module(owner)
    except ModuleNotFoundError:
        module,cls=owner.rsplit('.',1)
        target=getattr(importlib.import_module(module),cls)
    before=_compiler_stamp()
    original=getattr(target,name)
    if isinstance(original,BuiltinFunctionType):
        if owner=='unicodedata':
            # Change the compiler's imported module binding, not the process
            # Unicode normalizer also used by pytest's report rendering.
            from types import ModuleType
            from businessmap import business_content
            isolated=ModuleType('unicodedata')
            isolated.__dict__.update(vars(target))
            monkeypatch.setattr(business_content,'unicodedata',isolated)
            target=isolated
        replacement=target.sha512 if owner=='hashlib' else target.is_normalized
        monkeypatch.setattr(target,name,replacement)
    elif callable(original):
        def changed(*args,**kwargs): return original(*args,**kwargs)
        changed.__module__=original.__module__
        monkeypatch.setattr(target,name,changed)
    else:
        monkeypatch.setattr(target,name,original+1 if type(original) is int else original+' changed')
    # Same-origin callable replacements stay eligible, but must change the
    # canonical value stamp after removing the unstable marshal digest.
    assert dependency_stamp() is not None
    assert _compiler_stamp() != before


def test_foreign_source_observer_remains_uncacheable(source_timeout,monkeypatch):
    from businessmap import full_synthesis,semantic_supersession as sup
    c=source_timeout
    original=full_synthesis.source_fact_units
    observed=[]
    def observed_source(*args,**kwargs):
        observed.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(full_synthesis,'source_fact_units',observed_source)
    with pytest.raises(s.SemanticValidationError): compile_case(c)
    with c.store._db() as db:
        units,_=sup._source_routing(c.snapshot,c.run,s.semantic_checkpoint_reader(db))
    assert units and observed==[1]


def test_unknown_imported_callable_remains_uncacheable(source_timeout,monkeypatch):
    import unicodedata
    c=source_timeout
    original=unicodedata.normalize
    class Normalizer:
        def __call__(self,*args): return original(*args)
    monkeypatch.setattr(unicodedata,'normalize',Normalizer())
    with pytest.raises(s.SemanticValidationError): compile_case(c)


def test_material_legacy_sup_full_inventory_parity(completed_legacy_catalogue):
    from businessmap import semantic_supersession as sup, semantic_proposal_recovery as recovery
    import sys
    c = completed_legacy_catalogue
    raw = capture(c)
    expected = s.semantic_task_inventory(raw,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    material = compile_case(c)
    codes = {sup._derive_source_routing.__code__, recovery._packet_templates.__code__}
    calls = []
    def profile(frame,event,arg):
        if event == 'call' and frame.f_code in codes: calls.append(frame.f_code.co_name)
    sys.setprofile(profile)
    try:
        for _ in range(2):
            rows = bound_rows(c,material)
            assert s.semantic_task_inventory(rows,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash']) == expected
    finally:
        sys.setprofile(None)
    assert calls == []


def test_material_atomic_whole_templates(mixed_source_case):
    from test_proposal_timeout_recovery import test_source_plan_accepts_exact_atomic_and_whole_histories
    from businessmap import _source_material as m, semantic_proposal_recovery as recovery
    c = mixed_source_case
    test_source_plan_accepts_exact_atomic_and_whole_histories(c)
    rows = capture(c)
    pair = next((row['data']['context']['subdivision_hash'],row['stage_hash']) for row in rows
        if row['stage']=='partition_review' and row['data']['context'].get('parent_operation',{}).get('title')=='父业务B')
    groups = {'atomic_budget_chains':(c.four_keys,), 'whole_histories':(pair,)}
    parent = next(row for row in rows if row['stage']=='proposal')
    expected = recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=rows.read_checkpoint,**groups)
    material = m.prepare_source_material(c.snapshot,c.run,read_checkpoint=rows.read_checkpoint,**groups)
    bound = bound_rows(c,material)
    assert recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=bound.read_checkpoint,**groups)==expected


def test_material_accepted_repair_templates(whole_parent_case):
    from test_proposal_timeout_recovery import test_source_plan_reconstructs_selected_accepted_partition_repair
    from businessmap import _source_material as m, semantic_proposal_recovery as recovery
    c = whole_parent_case
    test_source_plan_reconstructs_selected_accepted_partition_repair(c)
    groups = {'partition_repair_pairs':tuple(c.runtime.partition_repair_allowlist)}
    rows = capture(c)
    parent = next(row for row in rows if row['stage']=='proposal')
    expected = recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=rows.read_checkpoint,**groups)
    material = m.prepare_source_material(c.snapshot,c.run,read_checkpoint=rows.read_checkpoint,**groups)
    bound = bound_rows(c,material)
    assert recovery.proposal_timeout_recovery_plan(c.snapshot,c.run,parent['stage_hash'],read_checkpoint=bound.read_checkpoint,**groups)==expected


def test_material_new_recovery_receipt_preserves_current_inventory(source_timeout, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from test_proposal_timeout_recovery import proposal_grant
    from businessmap import semantic_proposal_recovery as recovery
    c = source_timeout
    material = compile_case(c)
    plan, grant = proposal_grant(c,tmp_path)
    service = SimpleNamespace(store=c.store,_exclusive=lambda _:nullcontext(),_controls=lambda _:{'paused':True,'cancelled':False},
        project_analysis=SimpleNamespace(guard=lambda _:None))
    recovery.authorize_proposal_timeout_recovery(service,c.snapshot,c.run,plan,authorization=grant,source_material=material)
    raw, bound = capture(c), bound_rows(c,material)
    expected = s.semantic_task_inventory(raw,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'])
    assert s.semantic_task_inventory(bound,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash']) == expected
    assert bound.read_checkpoint.recovery_receipts(c.run['semantic_input_hash'])


@pytest.mark.parametrize('change', ('implementation', 'generator', 'lookup', 'hex_policy', 'ascii_policy', 'unsupported'))
def test_url_normalizer_transitives_invalidate_fresh_material(source_timeout, monkeypatch, change):
    import urllib.parse as url
    import re
    from businessmap import business_content, _source_material as material
    c = source_timeout
    assert business_content._canonical('%41') == 'A'
    compiled = compile_case(c)
    before = material._compiler_stamp()
    if change == 'implementation':
        monkeypatch.setattr(url, '_unquote_impl', lambda value: b'changed')
    elif change == 'generator':
        monkeypatch.setattr(url, '_generate_unquoted_parts', lambda *args: iter(('changed',)))
    elif change == 'lookup':
        monkeypatch.setattr(url, '_hextobyte', {**url._hextobyte, b'41': b'changed'})
    elif change == 'hex_policy':
        monkeypatch.setattr(url, '_hextobyte', None)
        monkeypatch.setattr(url, '_hexdig', '0')
    elif change == 'ascii_policy':
        monkeypatch.setattr(url, '_asciire', re.compile('(never-matches)'))
    else:
        class Decoder:
            def __call__(self, value): return b'changed'
        monkeypatch.setattr(url, '_unquote_impl', Decoder())
    assert business_content._canonical('%41') != 'A'
    assert before is not None
    assert material._compiler_stamp() != before
    with pytest.raises(s.SemanticValidationError):
        bound_rows(c, compiled)


def test_url_lazy_lookup_initialization_is_semantically_stable(monkeypatch):
    import urllib.parse as url
    from businessmap import business_content, _source_material as material
    monkeypatch.setattr(url, '_hextobyte', None)
    before = material._compiler_stamp()
    assert before is not None
    assert business_content._canonical('%41') == 'A'
    assert material._compiler_stamp() == before


@pytest.mark.parametrize('change', ('missing', 'extra', 'wrong', 'subtype', 'hex'))
def test_url_lookup_requires_exact_builtin_complete_table(source_timeout, monkeypatch, change):
    import urllib.parse as url
    from businessmap import business_content, _source_material as material
    assert business_content._canonical('%41') == 'A'
    compiled = compile_case(source_timeout)
    altered = dict(url._hextobyte)
    if change == 'missing': altered.pop(b'41')
    elif change == 'extra': altered[b'GG'] = b'X'
    elif change == 'wrong': altered[b'41'] = b'X'
    elif change == 'subtype':
        class Table(dict): pass
        altered = Table(altered)
    else: monkeypatch.setattr(url, '_hexdig', '0')
    monkeypatch.setattr(url, '_hextobyte', altered)
    assert material._compiler_stamp() is None
    with pytest.raises(s.SemanticValidationError): bound_rows(source_timeout, compiled)


def test_url_cold_and_hot_decode_remain_concurrent_stable(monkeypatch):
    import urllib.parse as url
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from businessmap import business_content, _source_material as material
    monkeypatch.setattr(url, '_hextobyte', None)
    stamp = material._compiler_stamp()
    assert stamp is not None
    barrier = Barrier(2)
    def decode(_):
        barrier.wait()
        for _ in range(8):
            assert business_content._canonical('%41') == 'A'
            assert material._compiler_stamp() == stamp
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(decode, range(2)))


@pytest.mark.parametrize('fixture_name', ('source_timeout', 'completed_legacy_catalogue'))
def test_bound_catalogue_reuses_normalized_source_without_recopying_rows(request, fixture_name):
    import copy
    import sys
    from businessmap import semantic_catalogue_frontier as frontier
    from businessmap.semantic_proposal_recovery import _without_packet
    c = request.getfixturevalue(fixture_name)
    raw = capture(c)
    raw_reader = _without_packet(raw.read_checkpoint, c.run['semantic_input_hash'], '', source_only=True)
    expected = frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=raw_reader)
    material = compile_case(c)
    reader = _without_packet(bound_rows(c, material).read_checkpoint, c.run['semantic_input_hash'], '', source_only=True)
    calls = {'inputs': 0, 'row_deepcopy': 0, 'inventory': 0}
    def profile(frame, event, arg):
        if event != 'call': return
        if frame.f_code is frontier._inputs.__code__: calls['inputs'] += 1
        if frame.f_code is s.semantic_task_inventory.__code__: calls['inventory'] += 1
        if frame.f_code is copy.deepcopy.__code__ and frame.f_back.f_code is frontier._catalogue_plan.__code__:
            calls['row_deepcopy'] += 1
    sys.setprofile(profile)
    try:
        for _ in range(2):
            actual = frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=reader)
            assert actual == expected and s._hash(actual) == s._hash(expected)
    finally: sys.setprofile(None)
    assert calls['inventory'] >= 2
    assert calls['inputs'] == 0
    assert calls['row_deepcopy'] == 0


def test_compiled_catalogue_inputs_preserve_per_unit_aliases_cycles_and_return_isolation():
    from businessmap._source_material import _catalogue_template
    shared = []
    shared.append(shared)
    units = [{'_fact_id':'first', 'x':shared, 'alias':shared},
             {'_fact_id':'second', 'x':shared, 'alias':shared}]
    clone = _catalogue_template({'operations':[], 'received_to_accept':['runtime'], 'occupied_calls':3}, units)
    plan, projected = clone()
    assert plan == {'operations':[]}
    assert projected['first']['x'] is projected['first']['alias']
    assert projected['first']['x'][0] is projected['first']['x']
    assert projected['first']['x'] is not projected['second']['x']
    projected['first']['x'].append('caller')
    plan['operations'].append('caller')
    other_plan, other = clone()
    assert other_plan == {'operations':[]}
    assert len(other['first']['x']) == 1
    assert other['first']['x'] is not projected['first']['x']


def test_compiled_catalogue_inputs_decline_custom_graph_before_copy_hooks():
    from businessmap._source_material import _catalogue_template
    from businessmap.semantic_catalogue_frontier import _inputs
    calls = []
    class Custom:
        def __deepcopy__(self, memo):
            calls.append(1)
            return {'copy':len(calls)}
    units = [{'_fact_id':'first','custom':Custom()}]
    clone = _catalogue_template({'operations':[]}, units)
    assert calls == []
    assert clone() == ({'operations':[]}, None)
    assert calls == []
    assert _inputs(units)['first']['custom'] == {'copy':1}
    assert _inputs(units)['first']['custom'] == {'copy':2}


def test_catalogue_projection_rechecks_policy_after_supplied_input_copy(source_timeout, monkeypatch):
    from businessmap import semantic_catalogue_frontier as frontier, semantic_supersession as sup
    from businessmap.semantic_proposal_recovery import _without_packet
    c = source_timeout
    material = compile_case(c)
    reader = _without_packet(bound_rows(c, material).read_checkpoint, c.run['semantic_input_hash'], '', source_only=True)
    units, _ = sup._source_routing(c.snapshot, c.run, reader)
    original = s.PROPOSAL_SYSTEM
    class MutatingInput:
        def __deepcopy__(self, memo):
            monkeypatch.setattr(s, 'PROPOSAL_SYSTEM', original + ' changed')
            return None
    units[0]['copy_probe'] = MutatingInput()
    with pytest.raises(s.SemanticValidationError):
        frontier.catalogue_frontier_plan(c.snapshot, c.run['semantic_input_hash'], read_checkpoint=reader, inputs=units)


@pytest.mark.parametrize('mutation', ('policy', 'planner'))
def test_new_review_recovery_compiler_dependency_invalidates_material(source_timeout, monkeypatch, mutation):
    from businessmap import semantic_review_recovery as review
    material = compile_case(source_timeout)
    if mutation == 'policy': monkeypatch.setattr(review, 'POLICY', review.POLICY + '-changed')
    else: monkeypatch.setattr(review, 'review_timeout_recovery_plan', lambda *args, **kwargs: {})
    with pytest.raises(s.SemanticValidationError): bound_rows(source_timeout, material)


@pytest.mark.parametrize('shape', ('float', 'list_subtype', 'str_subtype'))
def test_compiled_catalogue_inputs_leave_unsupported_types_on_original_path(shape):
    from businessmap._source_material import _catalogue_template
    from businessmap.semantic_catalogue_frontier import _inputs
    class CustomList(list): pass
    class CustomStr(str): pass
    value = {'float':1.25, 'list_subtype':CustomList([1]), 'str_subtype':CustomStr('x')}[shape]
    units = [{'_fact_id':'first','payload':value}]
    assert _catalogue_template({'operations':[]}, units)() == ({'operations':[]}, None)
    projected = _inputs(units)
    assert type(projected['first']['payload']) is type(value)
    assert projected == deepcopy({unit['_fact_id']:unit for unit in units})
