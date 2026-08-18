// Types for `system/latest-run.json`, the workflow's terminal summary. Hand-written
// on purpose: the daily batch writes that object from `baibai_batch.observability`,
// not from the read models, so it is outside the generated contract in ./types.ts.
// It is published to R2 by the batch, absent until the first run publishes one, and
// absent from the local API entirely.

export type RunOutcome =
  | 'succeeded'
  | 'skipped_non_business_day'
  | 'published_with_deferred_failure'
  | 'failed'
  // GitHub reports a job that hit `timeout-minutes` as cancelled, not failed.
  | 'cancelled'

export type RunPublishState = 'not_generated' | 'generated' | 'upload_failed' | 'published'

export interface RunErrorView {
  code: string
  stage: string
  impact: 'failed' | 'degraded'
  message: string
}

export interface RunBatchView {
  batch_name: string
  datasets: string[]
  status: 'ok' | 'degraded' | 'failed' | 'skipped'
  duration_seconds: number
  metrics: Record<string, unknown>
  errors: RunErrorView[]
}

export interface RunExecutionSummaryView {
  schema_version: number
  asof: string
  outcome: RunOutcome
  started_at: string
  finished_at: string
  duration_seconds: number
  batches: RunBatchView[]
  local_export: boolean
}

// Discriminated by `kind`; only the member for that kind is serialized, so the
// other keys are absent rather than null.
export type RunExecutionView =
  | { kind: 'available'; summary?: RunExecutionSummaryView }
  | { kind: 'not_started'; stage?: string }
  | { kind: 'unavailable'; error?: RunErrorView }

export interface WorkflowRunSummaryView {
  schema_version: number
  workflow: string
  repository: string
  trigger: string
  run_attempt: string
  run_url: string
  asof: string | null
  // When the run reached its terminal state. Without it a stale object (the
  // upload is best-effort) is indistinguishable from a fresh run.
  finished_at: string
  duration_seconds: number
  overall_outcome: RunOutcome
  publish_state: RunPublishState
  execution: RunExecutionView
  delivery: { status: string; detail: string | null }
  workflow_errors: RunErrorView[]
  // What the run published to the L1 lake. Null when the run never reached the
  // publication, which is a different fact from a publication that moved nothing.
  lake: LakeReleaseView | null
}

export interface LakeReleaseView {
  release_id: string
  data_as_of: string
  changed_partitions: number
  uploaded_objects: number
  uploaded_bytes: number
}

