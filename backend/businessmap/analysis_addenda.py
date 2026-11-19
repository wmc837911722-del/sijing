"""Explicit, bounded source supplements; original source receipts stay immutable."""
from __future__ import annotations

import copy
import json

from .analysis_output import AnalysisOutputError
from .core import now
from .full_analysis import FullProjectAnalysis, _hash, _json, _normalize_source_response
from .providers import ToolError, provider_destination


def _reject():
    return ToolError('补充资料或独立核对凭据未通过当前范围校验，原进度保留。', 422)


def _boundary(snapshot):
    project = snapshot['project']
    return _hash({'project': {key: project.get(key) for key in ('id', 'revision', 'repository_ids', 'repository_roles')},
                  'repositories': [{key: repo.get(key) for key in ('id', 'path', 'commit', 'vault_path')} for repo in snapshot['repositories']],
                  'policies': snapshot['policies'], 'provider': provider_destination(snapshot['provider']), 'vault': snapshot['vault_path']})


def _segments(chunks):
    result = {}
    for chunk in chunks:
        if chunk.get('status') != 'completed':
            continue
        for segment in chunk.get('segments', []):
            key = chunk['id'] + ':' + segment['evidence_id']
            canonical = {**segment, 'evidence_id': key, 'source_chunk_id': chunk['id'], 'source_evidence_id': segment['evidence_id']}
            if key in result and result[key] != canonical:
                raise _reject()
            result[key] = canonical
    return result


def _sources(snapshot):
    return [{'repository_id': repo['id'], 'commit': repo['commit']} for repo in snapshot['repositories']]


def validate_addendum_receipt(snapshot, run, receipt, chunks):
    """Pure readback validation; no Store, Git reads, providers or mutation."""
    try:
        if (receipt['status'] != 'accepted' or receipt['snapshot_id'] != run['snapshot_id']
                or receipt['authorization_hash'] != _boundary(snapshot)
                or receipt['destination'] != provider_destination(snapshot['provider'])
                or receipt['source_repositories'] != _sources(snapshot)):
            raise _reject()
        evidence = receipt['evidence']
        ids = [item['evidence_id'] for item in evidence]
        available = _segments(chunks)
        if not ids or len(ids) != len(set(ids)) or any(item != available.get(item['evidence_id']) for item in evidence):
            raise _reject()
        payload = receipt['payload']
        if (payload['review_question'] != receipt['question'] or payload['gap_id'] != receipt['gap_id']
                or len(payload['files']) != len(evidence) or _hash(_json(payload)) != receipt['input_hash']
                or len(_json(payload).encode()) + len(FullProjectAnalysis.SYSTEM.encode()) > FullProjectAnalysis.WIRE_BYTES):
            raise _reject()
        for index, (file, segment) in enumerate(zip(payload['files'], evidence), 1):
            if (file['evidence_id'] != 's' + str(index) or _hash(file['content']) != segment['content_hash']
                    or any(file[key] != segment[key] for key in ('repository_id', 'path', 'line_start', 'line_end', 'kind', 'redacted'))):
                raise _reject()
        result = _normalize_source_response({'text': _json(receipt['result'])}, evidence)
        if result != receipt['result'] or _hash(result) != receipt['result_hash']:
            raise _reject()
        identity = {'version': 1, 'system': FullProjectAnalysis.SYSTEM, 'snapshot_id': run['snapshot_id'],
                    'authorization_hash': _boundary(snapshot), 'input_hash': receipt['input_hash']}
        if receipt['addendum_id'] != 'addendum:' + _hash(identity)[:24]:
            raise _reject()
    except (KeyError, TypeError, ValueError, AnalysisOutputError):
        raise _reject() from None


def validate_matching_receipt(snapshot, run, addendum, matching):
    """Validate an operator-recorded static review, never model assertions."""
    try:
        evidence = {item['evidence_id']: item for item in addendum['evidence']}
        ids = matching['evidence_ids']
        if (matching['status'] != 'accepted' or matching['addendum_id'] != addendum['addendum_id']
                or matching['snapshot_id'] != run['snapshot_id'] or matching['input_hash'] != addendum['input_hash']
                or matching['authorization_hash'] != _boundary(snapshot)
                or matching['destination'] != provider_destination(snapshot['provider'])
                or matching['source_repositories'] != _sources(snapshot)
                or matching['gap_id'] != addendum['gap_id']
                or matching['conclusion'] != 'static_request_chain' or matching['reviewer'] != 'independent_static_review'
                or not isinstance(matching['operation'], str) or not matching['operation'].strip()
                or not isinstance(matching['review_note'], str) or not matching['review_note'].strip()
                or not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
                or len(set(ids)) != len(ids) or not set(ids).issubset(evidence)
                or matching['evidence_hashes'] != {key: evidence[key]['content_hash'] for key in ids}):
            raise _reject()
        identity = {key: value for key, value in matching.items() if key not in {'match_id', 'created_at'}}
        if matching['match_id'] != 'match:' + _hash(identity)[:24]:
            raise _reject()
    except (KeyError, TypeError, ValueError):
        raise _reject() from None


class AnalysisAddenda:
    def __init__(self, service, *, guard):
        self.service, self.store, self.guard = service, service.store, guard
        self.full = FullProjectAnalysis(service)
        with self.store._lock, self.store._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS analysis_addenda (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                input_hash TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_matching_receipts (
                id TEXT PRIMARY KEY, addendum_id TEXT NOT NULL, run_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);''')

    def _guard(self, snapshot):
        self.full.project_analysis.guard(snapshot)
        self.guard()

    def accepted(self, snapshot, run):
        self._guard(snapshot)
        with self.store._lock, self.store._db() as db:
            rows = db.execute('SELECT data FROM analysis_addenda WHERE run_id=? ORDER BY id', (run['run_id'],)).fetchall()
            matches = [json.loads(row[0]) for row in db.execute('SELECT data FROM analysis_matching_receipts WHERE run_id=? ORDER BY id', (run['run_id'],))]
        result, chunks = [], self.full._chunks(run)
        for row in rows:
            receipt = json.loads(row[0])
            if receipt.get('status') != 'accepted':
                continue
            validate_addendum_receipt(snapshot, run, receipt, chunks)
            attached = [item for item in matches if item.get('addendum_id') == receipt['addendum_id']]
            for matching in attached:
                validate_matching_receipt(snapshot, run, receipt, matching)
            if attached:
                receipt['matching_receipts'] = attached
            result.append(receipt)
        return result

    def collect(self, snapshot, run, request):
        self._guard(snapshot)
        if (not isinstance(request, dict) or request.get('snapshot_id') != run.get('snapshot_id')
                or self.full._snapshot_id(snapshot) != run.get('snapshot_id')
                or any(not isinstance(request.get(key), str) or not request[key].strip() for key in ('gap_id', 'question'))):
            raise _reject()
        ids = request.get('evidence_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
            raise _reject()
        chunks = self.full._chunks(run)
        available = _segments(chunks)
        if not set(ids).issubset(available):
            raise _reject()
        evidence = [copy.deepcopy(available[key]) for key in ids]
        # The exact accepted/requested evidence can be recognized from its
        # frozen metadata without re-reading paid source bytes from Git.
        with self.store._lock, self.store._db() as db:
            previous = [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM analysis_addenda WHERE run_id=? AND snapshot_id=? ORDER BY id',
                (run['run_id'], run['snapshot_id']))]
        for receipt in previous:
            if (receipt.get('gap_id') == request['gap_id'] and receipt.get('question') == request['question']
                    and receipt.get('evidence') == evidence and receipt.get('authorization_hash') == _boundary(snapshot)):
                validate_addendum_receipt(snapshot, run, receipt, chunks)
                return receipt
        module_id = next(chunk['module_id'] for chunk in chunks if chunk['id'] == evidence[0]['source_chunk_id'])
        temporary = {'id': 'supplement', 'module_id': module_id, 'segments': evidence}
        payload = json.loads(self.full._payload(snapshot, run, temporary))
        payload.update(gap_id=request['gap_id'], review_question=request['question'])
        wire = _json(payload)
        if len(wire.encode()) + len(self.full.SYSTEM.encode()) > self.full.WIRE_BYTES:
            raise ToolError('补充资料超过单次输入上限，未发送；原进度保留。', 422)
        input_hash = _hash(wire)
        identity = {'version': 1, 'system': self.full.SYSTEM, 'snapshot_id': run['snapshot_id'],
                    'authorization_hash': _boundary(snapshot), 'input_hash': input_hash}
        aid = 'addendum:' + _hash(identity)[:24]
        with self.store._lock, self.store._db() as db:
            row = db.execute('SELECT data FROM analysis_addenda WHERE id=?', (aid,)).fetchone()
        if row:
            receipt = json.loads(row[0])
            validate_addendum_receipt(snapshot, run, receipt, chunks)
            return receipt
        receipt = {'addendum_id': aid, 'gap_id': request['gap_id'], 'question': request['question'],
                   'snapshot_id': run['snapshot_id'], 'authorization_hash': _boundary(snapshot),
                   'source_repositories': _sources(snapshot), 'evidence': evidence, 'input_hash': input_hash,
                   'destination': provider_destination(snapshot['provider']), 'payload': payload, 'status': 'planned'}
        self._save(run, receipt)
        try:
            response = self.service.providers.complete(self.full.SYSTEM, wire, provider_id=snapshot['provider']['id'],
                max_output=2800, json_mode=True, expected_destination=provider_destination(snapshot['provider']),
                before_send=lambda: self._guard(snapshot))
            self.full.project_analysis.guard(snapshot)
            result = _normalize_source_response(response, evidence)
            receipt.update(result=result, result_hash=_hash(result), status='accepted',
                usage={key: value for key, value in (response.get('usage') or {}).items()
                       if key in {'input_tokens', 'output_tokens'} and type(value) is int and value >= 0})
        except Exception:
            receipt['status'] = 'needs_review'
            receipt['error_code'] = 'supplement_not_accepted'
            self._save(run, receipt)
            # Re-check authorization/pause so a controlled interruption wins.
            self._guard(snapshot)
            raise ToolError('补充资料尚未通过核验，已保存待审进度，没有自动重试。', 422) from None
        self._save(run, receipt)
        self._guard(snapshot)
        return receipt

    def _save(self, run, receipt):
        timestamp = now()
        if receipt['status'] != 'planned':
            receipt['completed_at'] = timestamp
        with self.store._lock, self.store._db() as db:
            db.execute('INSERT OR REPLACE INTO analysis_addenda VALUES (?,?,?,?,?,?)',
                (receipt['addendum_id'], run['run_id'], receipt['snapshot_id'], receipt['input_hash'], _json(receipt), timestamp))

    def attest_matching(self, snapshot, run, addendum_id, attestation):
        self._guard(snapshot)
        addendum = next((item for item in self.accepted(snapshot, run) if item['addendum_id'] == addendum_id), None)
        fields = ('operation', 'gap_id', 'evidence_ids', 'evidence_hashes', 'conclusion', 'reviewer', 'review_note')
        if not addendum or not isinstance(attestation, dict) or any(key not in attestation for key in fields):
            raise _reject()
        matching = {key: copy.deepcopy(attestation[key]) for key in fields}
        matching.update(status='accepted', addendum_id=addendum_id, snapshot_id=run['snapshot_id'],
                        input_hash=addendum['input_hash'], destination=provider_destination(snapshot['provider']),
                        authorization_hash=_boundary(snapshot), source_repositories=_sources(snapshot))
        matching['match_id'] = 'match:' + _hash(matching)[:24]
        with self.store._lock, self.store._db() as db:
            row = db.execute('SELECT data FROM analysis_matching_receipts WHERE id=?', (matching['match_id'],)).fetchone()
        if row:
            saved = json.loads(row[0])
            validate_matching_receipt(snapshot, run, addendum, saved)
            self._guard(snapshot)
            return saved
        matching['created_at'] = now()
        validate_matching_receipt(snapshot, run, addendum, matching)
        self._guard(snapshot)
        with self.store._lock, self.store._db() as db:
            db.execute('INSERT OR REPLACE INTO analysis_matching_receipts VALUES (?,?,?,?,?,?)',
                (matching['match_id'], addendum_id, run['run_id'], run['snapshot_id'], _json(matching), matching['created_at']))
        return matching
