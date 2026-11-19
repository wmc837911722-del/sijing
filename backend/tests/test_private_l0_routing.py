"""Contract seams: disposable production SQLite and captured structured logging."""
import copy
import json
import logging
import sqlite3

import pytest

from businessmap.full_synthesis import FullBusinessSynthesis, SynthesisValidationError, _json
from test_full_synthesis import synthesis_project

REAL731 = '内部普通查询先核对环境并校验查询语句；表结构查询仅接受当前库的安全裸表名。执行仅支持MySQL，使用只读事务，结束时始终回滚，并限制并发、超时和读取行数。'


def unit(text=REAL731, kind='finding'):
    return {'kind': kind, 'text': text, '_fact_id': 'fact:731', '_module_ids': ['module'], 'position': 731}


def route(title='查询规则'):
    return {'fact_id': 'f0', 'disposition': 'public', 'category': 'rules', 'title': title}


def usage():
    return dict(requests=0, cache_hits=0, input_tokens=0, output_tokens=0)


@pytest.mark.parametrize('text,kind', [(REAL731, 'finding'), ('订单仅在已支付且未退款时允许退款，最多5次；是否支持跨店尚未确认。', 'unknown')])
def test_navigation_needs_no_rewritten_body(text, kind):
    units = [unit(text, kind)]
    before = copy.deepcopy(units)
    result = FullBusinessSynthesis._validate({'assignments': [route()]}, units)
    assert result == {'assignments': [dict(route(), fact_id='fact:731')]}
    assert units == before


@pytest.mark.parametrize('change', [{'text': '删改正文'}, {'kind': 'finding'}, {'fact_id': 'foreign'}, {'title': 'RefundService.java'}, {'category': 'foreign'}, {'disposition': 'internal', 'classification': 'technical', 'reason': '内部'}])
def test_new_raw_is_strict(change):
    with pytest.raises(SynthesisValidationError):
        FullBusinessSynthesis._validate({'assignments': [route() | change]}, [unit()])


def test_legacy_hit_is_readonly_and_new_commit_is_logged_after_commit(synthesis_project, monkeypatch, caplog):
    from businessmap.l0_legacy import LegacyL0
    service, snapshot, run, _ = synthesis_project
    run['snapshot_id'] = 'fixed-snapshot'
    synth = FullBusinessSynthesis(service)
    units = [unit('每店最多5套。')]
    key = synth.cache_identity(snapshot, run, units, 0, legacy=True)
    cached = LegacyL0._validate({'assignments': [route('创建限制')]}, units)
    with service.store._db() as db:
        db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, _json(cached), 'fixed-time'))
    calls = []
    def complete(system, wire, **kwargs):
        calls.append(wire)
        return {'text': _json({'assignments': [route()]})}
    monkeypatch.setattr(service.providers, 'complete', complete)
    caplog.set_level(logging.INFO, logger='businessmap.full_synthesis')
    before = sqlite3.connect(service.store.db_path).execute('SELECT * FROM full_analysis_synthesis').fetchall()
    result = synth._request(snapshot, run, units, 0, usage())
    assert result == {'assignments': [dict(route('创建限制'), fact_id='fact:731')]}
    assert not calls
    with service.store._db() as db:
        assert db.execute('SELECT * FROM full_analysis_synthesis').fetchall() == before
    hit = [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_hit']
    assert len(hit) == 1 and hit[0].cache_version == 7 and hit[0].cache_key == key
    # A real independent connection observes committed state when the event fires.
    class Committed(logging.Handler):
        def emit(self, record):
            if getattr(record, 'event', None) == 'l0_cache_committed':
                with sqlite3.connect(service.store.db_path) as db:
                    assert db.execute('SELECT input_hash FROM full_analysis_synthesis WHERE cache_key=?', (record.cache_key,)).fetchone() == (record.cache_key,)
    logger = logging.getLogger('businessmap.full_synthesis')
    handler = Committed()
    logger.addHandler(handler)
    try:
        fresh = [dict(unit(), _fact_id='new')]
        assert synth._request(snapshot, run, fresh, 0, usage())['assignments'][0]['fact_id'] == 'new'
    finally:
        logger.removeHandler(handler)
    events = [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_committed']
    assert len(events) == 1 and events[0].cache_version == 8
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM full_analysis_synthesis').fetchone()[0] == 2
        assert db.execute('SELECT * FROM full_analysis_synthesis WHERE cache_key=?', (key,)).fetchall() == before
    assert len(calls) == 1


@pytest.mark.parametrize('status', ['accepted', 'rejected', 'failed'])
def test_any_semantic_row_requires_all_batches_cached_before_dispatch(synthesis_project, monkeypatch, status):
    from businessmap.business_semantics import BusinessSemanticIntegration, semantic_input_hash
    service, snapshot, run, chunks = synthesis_project
    synth = FullBusinessSynthesis(service)
    BusinessSemanticIntegration(service, guard=lambda: None)
    key = semantic_input_hash(snapshot, run, chunks, [])
    with service.store._db() as db:
        db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)', ('stage', 'catalogue', key, status, 1, '{}', None, 'fixed-time'))
    calls = []
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: calls.append(True))
    with pytest.raises(SynthesisValidationError) as error:
        synth.build(snapshot, run, chunks)
    assert error.value.reason_code == 'cache_invalid'
    assert calls == []
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM full_analysis_synthesis').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['input_hash', 'member', 'text', 'basis', 'extra', 'duplicate'])
def test_damaged_legacy_is_rejected_without_provider(synthesis_project, monkeypatch, damage):
    from businessmap.l0_legacy import LegacyL0
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    units = [unit('每店最多5套。')]
    key = synth.cache_identity(snapshot, run, units, 0, legacy=True)
    result = LegacyL0._validate({'assignments': [route()]}, units)
    if damage == 'member': result['assignments'][0]['fact_id'] = 'foreign'
    if damage == 'text': result['assignments'][0]['text'] = '删改正文'
    if damage == 'basis': result['assignments'][0]['classification_basis'] = {}
    if damage == 'extra': result['extra'] = True
    if damage == 'duplicate': result['assignments'] *= 2
    with service.store._db() as db:
        db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, 'bad' if damage == 'input_hash' else key, _json(result), 'fixed-time'))
    calls = []
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: calls.append(True))
    with pytest.raises(SynthesisValidationError) as error:
        synth._request(snapshot, run, units, 0, usage())
    assert error.value.reason_code == 'cache_invalid' and not calls


def test_both_versions_must_agree_on_consumed_navigation(synthesis_project, monkeypatch):
    from businessmap.l0_legacy import LegacyL0
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    units = [unit('每店最多5套。')]
    old = LegacyL0._validate({'assignments': [route('旧规则')]}, units)
    new = {'assignments': [dict(route('新规则'), fact_id='fact:731')]}
    with service.store._db() as db:
        for legacy, result in [(True, old), (False, new)]:
            key = synth.cache_identity(snapshot, run, units, 0, legacy=legacy)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, _json(result), 'fixed-time'))
    calls = []
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: calls.append(True))
    with pytest.raises(SynthesisValidationError) as error:
        synth._request(snapshot, run, units, 0, usage())
    assert error.value.reason_code == 'cache_invalid' and not calls


def test_invalid_new_candidate_never_falls_back_or_caches(synthesis_project, monkeypatch, caplog):
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    calls = []
    def complete(*args, **kwargs):
        calls.append(True)
        return {'text': _json({'assignments': [route() | {'text': '合法旧格式也禁止'}]})}
    monkeypatch.setattr(service.providers, 'complete', complete)
    caplog.set_level(logging.INFO, logger='businessmap.full_synthesis')
    with pytest.raises(SynthesisValidationError) as error:
        synth._request(snapshot, run, [unit('退款条件尚未确认。', 'unknown')], 0, usage())
    assert error.value.reason_code == 'assignment_format' and len(calls) == 1
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM full_analysis_synthesis').fetchone()[0] == 0
    assert not [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_committed']


def test_failed_database_write_has_no_success_event(synthesis_project, monkeypatch, caplog):
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    with service.store._db() as db:
        db.execute("CREATE TRIGGER fail_l0 BEFORE INSERT ON full_analysis_synthesis BEGIN SELECT RAISE(ABORT, 'fixture write rejected'); END")
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: {'text': _json({'assignments': [route()]})})
    caplog.set_level(logging.INFO, logger='businessmap.full_synthesis')
    with pytest.raises(sqlite3.IntegrityError):
        synth._request(snapshot, run, [unit()], 0, usage())
    with service.store._db() as db:
        assert db.execute('SELECT count(*) FROM full_analysis_synthesis').fetchone()[0] == 0
    assert not [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_committed']


@pytest.mark.parametrize('damage', ['member', 'text', 'basis', 'duplicate', 'extra'])
def test_v8_cache_is_fully_revalidated(synthesis_project, monkeypatch, damage):
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    units = [unit()]
    cached = {'assignments': [dict(route(), fact_id='fact:731')]}
    if damage == 'member': cached['assignments'][0]['fact_id'] = 'foreign'
    if damage == 'text': cached['assignments'][0]['text'] = 'uncontracted'
    if damage == 'basis': cached['assignments'][0]['classification_basis'] = {}
    if damage == 'duplicate': cached['assignments'] *= 2
    if damage == 'extra': cached['extra'] = True
    key = synth.cache_identity(snapshot, run, units, 0)
    with service.store._db() as db:
        db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)', (key, key, _json(cached), 'fixed-time'))
    calls = []
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: calls.append(True))
    with pytest.raises(SynthesisValidationError) as error:
        synth._request(snapshot, run, units, 0, usage())
    assert error.value.reason_code == 'cache_invalid' and not calls


def test_fixed_historical_boundaries_even_when_v8_is_smaller(synthesis_project):
    from businessmap.l0_legacy import LegacyL0
    _, snapshot, _, _ = synthesis_project
    synth = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    units = [dict(unit('退款条件尚未确认。' * 100), _fact_id=f'fact:{i}', position=i) for i in range(30)]
    synth.WIRE_BYTES = 10000
    frozen = LegacyL0()
    frozen.WIRE_BYTES = 10000
    actual = synth._batches(snapshot, units, 0)
    assert [[u['_fact_id'] for u in b] for b in actual] == [[u['_fact_id'] for u in b] for b in frozen._batches(snapshot, units, 0)]
    assert [u['_fact_id'] for b in actual for u in b] == [f'fact:{i}' for i in range(30)]


def test_all_originals_including_internal_reach_leaf_and_proposal(synthesis_project):
    from businessmap.business_semantics import BusinessSemanticIntegration
    service, snapshot, _, _ = synthesis_project
    units = [unit(), dict(unit('仅在已支付且未退款时允许退款，最多5次；跨店规则尚未确认。', 'unknown'), _fact_id='unknown'),
             dict(unit('测试验证序列化与构建处理。'), _fact_id='internal', _source_paths=['repo/tests/build.py'])]
    raw = {'assignments': [route(), route() | {'fact_id': 'f1'},
        {'fact_id': 'f2', 'disposition': 'internal', 'classification': 'test', 'reason': '构建测试'}]}
    routing = FullBusinessSynthesis._validate(raw, units)['assignments']
    builder = BusinessSemanticIntegration(service, guard=lambda: None)
    builder._snapshot, builder._units = snapshot, {u['_fact_id']: u for u in units}
    builder._matching, builder._partitions = [], []
    labels = [{'id': f'op{i}', 'title': r.get('title', '内部资料'), 'category': r.get('category', 'data'),
        'fact_ids': [r['fact_id']], 'aliases': []} for i, r in enumerate(routing)]
    leaf, _, _ = builder._catalogue_payload(labels)
    proposal, _, _ = builder._packet(labels)
    assert [(f['kind'], f['text']) for item in leaf['items'] for f in item['member_facts']] == [(u['kind'], u['text']) for u in units]
    assert [(f['kind'], f['text']) for f in proposal['items']] == [(u['kind'], u['text']) for u in units]
    assert {fid for label in labels for fid in label['fact_ids']} == {'fact:731', 'unknown', 'internal'}


def test_frozen_batch_input_limit_keeps_current_controlled_diagnostic(synthesis_project):
    _, snapshot, _, _ = synthesis_project
    synth = FullBusinessSynthesis.__new__(FullBusinessSynthesis)
    synth.WIRE_BYTES = 100
    with pytest.raises(SynthesisValidationError) as error:
        synth._batches(snapshot, [unit()], 0)
    assert error.value.safe_details() == {'reason_code': 'input_limit', 'fact_position': 731, 'fact_kind': 'finding'}


@pytest.mark.parametrize('new_title,new_category,accepted', [
    (' 创建限制 \t', 'rules', True),
    ('其他限制', 'rules', False),
    ('创建限制', 'data', False),
])
def test_dual_cache_compares_consumed_trimmed_title_without_rewriting_rows(
        synthesis_project, monkeypatch, caplog, new_title, new_category, accepted):
    from businessmap.l0_legacy import LegacyL0
    service, snapshot, run, _ = synthesis_project
    synth = FullBusinessSynthesis(service)
    units = [unit('每店最多5套。')]
    old = LegacyL0._validate({'assignments': [route('创建限制')]}, units)
    new = synth._validate({'assignments': [route(new_title) | {'category': new_category}]}, units)
    new_key = synth.cache_identity(snapshot, run, units, 0)
    with service.store._db() as db:
        for legacy, result in [(True, old), (False, new)]:
            key = synth.cache_identity(snapshot, run, units, 0, legacy=legacy)
            db.execute('INSERT INTO full_analysis_synthesis VALUES (?,?,?,?)',
                       (key, key, _json(result), 'fixed-legacy' if legacy else 'fixed-current'))
        before = db.execute('SELECT cache_key,input_hash,CAST(data AS BLOB),created_at FROM full_analysis_synthesis ORDER BY cache_key').fetchall()
    calls = []
    monkeypatch.setattr(service.providers, 'complete', lambda *a, **k: calls.append(True))
    caplog.set_level(logging.INFO, logger='businessmap.full_synthesis')
    counters = usage()
    if accepted:
        assert synth._request(snapshot, run, units, 0, counters) == new
        assert counters == dict(requests=0, cache_hits=1, input_tokens=0, output_tokens=0)
        hits = [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_hit']
        assert len(hits) == 1 and hits[0].cache_key == new_key and hits[0].cache_version == 8
    else:
        with pytest.raises(SynthesisValidationError) as error:
            synth._request(snapshot, run, units, 0, counters)
        assert error.value.reason_code == 'cache_invalid'
        assert counters == usage()
        assert not [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_hit']
    assert not calls
    assert not [r for r in caplog.records if getattr(r, 'event', None) == 'l0_cache_committed']
    with service.store._db() as db:
        assert db.execute('SELECT cache_key,input_hash,CAST(data AS BLOB),created_at FROM full_analysis_synthesis ORDER BY cache_key').fetchall() == before
