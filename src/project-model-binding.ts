import type { FullAnalysisStatus, RepositoryModelPolicy } from './types';

export function canContinueWithModel(policy: RepositoryModelPolicy | undefined, analysis: FullAnalysisStatus | undefined, providerId: string): boolean {
  return Boolean(
    policy?.status === 'ready' && policy.provider_id && providerId && providerId !== policy.provider_id
    && analysis?.run_id && !analysis.stale && (analysis.coverage?.completed_chunks || 0) > 0
    && ['paused', 'failed', 'cancelled', 'blocked'].includes(analysis.status),
  );
}

export function confirmsModelContinuation(analysis: FullAnalysisStatus | undefined, sourceRunId: string | undefined, providerId: string): boolean {
  return Boolean(sourceRunId && providerId && analysis?.receipt_migration?.source_run_id === sourceRunId
    && analysis.receipt_migration.target_provider?.id === providerId);
}
