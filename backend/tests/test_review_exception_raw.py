"""SQL evidence tests use hand-authored bytes, never a producer hash oracle."""
import sqlite3
import pytest
from businessmap import business_semantics as s
from businessmap import _prepared_mutation as m

INPUT='a'*64

def fixture_db():
 db=sqlite3.connect(':memory:')
 db.execute('CREATE TABLE business_semantic_tasks(stage_hash TEXT,stage TEXT,input_hash TEXT,status TEXT,attempt INTEGER,data TEXT,error_code TEXT,created_at TEXT)')
 db.execute('CREATE TABLE business_semantic_recoveries(recovery_id TEXT,parent_stage_hash TEXT,child_stage_hash TEXT,input_hash TEXT,data TEXT,consumed_at INTEGER,dispatch_id TEXT)')
 raw=' {"recovery_id":"r","parent_stage_hash":"p","child_stage_hash":"c","input_hash":"'+INPUT+'"} '
 db.execute('INSERT INTO business_semantic_recoveries VALUES (?,?,?,?,?,?,?)',('r','p','c',INPUT,raw,None,None))
 return db,raw


def test_raw_sql7_stays_exact_after_connection_changes_and_packet_filter():
 db,raw=fixture_db()
 reader=s.semantic_checkpoint_rows(db,INPUT).read_checkpoint
 accessor=getattr(reader,'_raw_dynamic_capture',None)
 assert callable(accessor), 'factory reader must retain exact dynamic SQL capture'
 capture=accessor(INPUT)
 rows=m._raw_recovery_rows(capture,input_hash=INPUT)
 assert rows==( ('r','p','c',INPUT,raw,None,None), )
 db.execute("UPDATE business_semantic_recoveries SET data=trim(data)")
 newer=s.semantic_checkpoint_rows(db,INPUT).read_checkpoint
 assert m._raw_recovery_rows(newer._raw_dynamic_capture(INPUT),input_hash=INPUT)[0][4]==raw.strip()
 assert m._raw_recovery_rows(capture,input_hash=INPUT)[0][4]==raw
 view=reader._without_packet(INPUT,'packet',source_only=True)
 assert view._raw_dynamic_capture(INPUT) is capture
 with pytest.raises(s.SemanticValidationError):accessor('b'*64)
 with pytest.raises(s.SemanticValidationError):m._raw_recovery_rows(rows,input_hash=INPUT)
 db.close()


@pytest.mark.parametrize('change',['duplicate','schema','nontext'])
def test_raw_receipt_evidence_rejects_ambiguous_sql(change):
 db,raw=fixture_db()
 if change=='duplicate':db.execute('INSERT INTO business_semantic_recoveries SELECT * FROM business_semantic_recoveries')
 if change=='schema':db.execute('ALTER TABLE business_semantic_recoveries ADD COLUMN surprise TEXT')
 if change=='nontext':db.execute('UPDATE business_semantic_recoveries SET data=?',(raw.encode(),))
 reader=s.semantic_checkpoint_rows(db,INPUT).read_checkpoint
 accessor=getattr(reader,'_raw_dynamic_capture',None)
 assert callable(accessor), 'raw evidence access must validate SQL schema and exact TEXT'
 with pytest.raises(s.SemanticValidationError):m._raw_recovery_rows(accessor(INPUT),input_hash=INPUT)
 db.close()


def test_dynamic_digest_binds_schema_presence_and_sql_scalar_types():
 db,_=fixture_db()
 before=m._capture_dynamic(db,INPUT)
 digest=getattr(m,'_dynamic_digest',None)
 assert callable(digest), 'complete dynamic CAS needs schema and typed SQL scalar digest'
 baseline=digest(before,input_hash=INPUT)
 db.execute('CREATE TABLE business_semantic_supersessions(supersession_id TEXT,parent_stage_hash TEXT,child_stage_hash TEXT,input_hash TEXT,data TEXT,consumed_at INTEGER,dispatch_id TEXT)')
 after=m._capture_dynamic(db,INPUT)
 assert baseline!=digest(after,input_hash=INPUT)
 assert not m._same_dynamic(before,after)
 # SQLite coercion is avoided by storing the same bytes in a BLOB versus TEXT.
 db.execute('UPDATE business_semantic_recoveries SET data=CAST(data AS BLOB)')
 blob=m._capture_dynamic(db,INPUT)
 assert digest(after,input_hash=INPUT)!=digest(blob,input_hash=INPUT)
 with pytest.raises(s.SemanticValidationError):digest(before,input_hash='b'*64)
 db.close()


def test_raw_bounds_cover_whole_capture_not_only_requested_table():
 db,_=fixture_db()
 db.executemany('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',
  ((format(index,'064x'),'proposal',INPUT,'planned',1,'{}',None,'fixture') for index in range(8192)))
 capture=m._capture_dynamic(db,INPUT)
 with pytest.raises(s.SemanticValidationError):m._raw_recovery_rows(capture,input_hash=INPUT)
 db.close()


def test_raw_capture_rejects_changed_declared_sql_type():
 db,raw=fixture_db()
 db.execute('ALTER TABLE business_semantic_recoveries RENAME TO old_receipts')
 db.execute('CREATE TABLE business_semantic_recoveries(recovery_id TEXT,parent_stage_hash TEXT,child_stage_hash TEXT,input_hash TEXT,data BLOB,consumed_at INTEGER,dispatch_id TEXT)')
 db.execute('INSERT INTO business_semantic_recoveries SELECT * FROM old_receipts')
 capture=m._capture_dynamic(db,INPUT)
 with pytest.raises(s.SemanticValidationError):m._raw_recovery_rows(capture,input_hash=INPUT)
 db.close()


def test_unfiltered_raw_task_accessor_keeps_factory_epoch_and_scope():
 db,_=fixture_db()
 outside=('e'*64,'old','b'*64,'unknown',9,' {"old": true} ',None,'old')
 db.execute('INSERT INTO business_semantic_tasks VALUES (?,?,?,?,?,?,?,?)',outside)
 capture=m._capture_dynamic(db,INPUT)
 accessor=getattr(m,'_raw_all_task_rows',None)
 assert callable(accessor),'same-factory unfiltered task accessor is missing'
 assert accessor(capture,input_hash=INPUT)==(outside,)
 assert m._raw_task_rows(capture,input_hash=INPUT)==()
 db.execute("UPDATE business_semantic_tasks SET data='changed'")
 assert accessor(capture,input_hash=INPUT)==(outside,)
 with pytest.raises(s.SemanticValidationError):accessor(capture,input_hash='b'*64)
 with pytest.raises(s.SemanticValidationError):accessor((outside,),input_hash=INPUT)
 db.close()


def test_unfiltered_raw_accessor_participates_in_prepared_fingerprint(monkeypatch):
 before=m._mutation_stamp()
 assert before is not None
 monkeypatch.setattr(m,'_raw_all_task_rows',lambda *args,**kwargs:())
 assert m._mutation_stamp()!=before
