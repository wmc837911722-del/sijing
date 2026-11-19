from copy import deepcopy
import pytest
from businessmap import business_semantics as s
from test_business_semantics import review
from review_exception_fixture import two_review_history


def test_revision_reconstruction_uses_bound_normalized_negative_without_dispatch(two_review_history,monkeypatch):
 c=two_review_history
 parent=c.rows.read_checkpoint(c.a['subject_proposal_hash'])
 failed=deepcopy(c.targets[0]);data=failed['data'];failed['status']='needs_review'
 try:s.BusinessSemanticIntegration._review(review(data['request'],reject=True),set(parent['data']['context']['input_ids']),set(parent['data']['context']['operation_ids']),parent['data']['result'])
 except s.SemanticValidationError as error:
  assert error.reason_code=='review_rejected';result=error.normalized_result
 data.update(result=result,result_hash=s._hash(result),failure_code='review_rejected',feedback=s._review_feedback(result,parent['data']['result'],parent['data']['request']))
 primitive=getattr(s,'_revision_payload_primitive',None)
 assert callable(primitive), 'D2 bootstrap must reconstruct without recovery-dispatch recursion'
 def recursive(*args,**kwargs):raise AssertionError('bootstrap reentered checkpoint dispatch')
 monkeypatch.setattr(s,'_checkpoint_record',recursive)
 payload=primitive(c.snapshot,parent,failed)
 assert {'prior_candidate','revision_feedback','preservation'}<=set(payload)
 assert payload['stage']=='proposal'
 for mutate in ('timeout','null','forged-feedback','wrong-subject'):
  broken=deepcopy(failed)
  if mutate=='timeout':broken['data']['failure_code']='model_call'
  if mutate=='null':broken['data']['result']=None;broken['data']['result_hash']=s._hash(None)
  if mutate=='forged-feedback':broken['data']['feedback']['reason_code']='candidate_invalid'
  if mutate=='wrong-subject':broken['data']['context']['proposal_hash']='f'*64
  with pytest.raises(s.SemanticValidationError):primitive(c.snapshot,parent,broken)


def test_ordinary_primitive_checks_identity_even_when_recovery_id_exists(two_review_history):
 c=two_review_history
 primitive=getattr(s,'_ordinary_checkpoint_primitive',None)
 assert callable(primitive), 'bootstrap primitive must not dispatch recovery authentication'
 row=deepcopy(c.targets[0])
 primitive(row,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],logical_task_hash=c.a['logical_task_hash'])
 row['data']['context']['packet_hash']='f'*64
 with pytest.raises(s.SemanticValidationError):primitive(row,snapshot=c.snapshot,expected_input_hash=c.run['semantic_input_hash'],logical_task_hash=c.a['logical_task_hash'])
