export type RunStatus = 'queued' | 'pending' | 'uploading' | 'analyzing' | 'running' | 'completed' | 'failed' | 'cancelled'
export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'informational' | 'pending'
export type ReviewStatus = 'pending_ai' | 'pending_manual' | 'ai_false_positive' | 'manual_false_positive' | 'confirmed'
export type LegacyReviewStatus = 'pending' | 'false_positive' | 'ai_candidate'
export type CleanupMode = 'prune_intermediates' | 'clear_sensitive_content' | 'delete_run'

export interface AIConfigSnapshot {
  enabled?: boolean
  allow_external_code?: boolean
  provider_kind?: 'openai-compatible'
  model?: string | null
  [key: string]: unknown
}

export interface RunConfigSnapshot {
  ai?: AIConfigSnapshot
  [key: string]: unknown
}

export interface RunStage {
  id?: string
  name: string
  status: 'pending' | 'running' | 'completed' | 'partial' | 'skipped' | 'failed' | 'unknown'
  started_at?: string | null
  ended_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  message?: string | null
  summary?: Record<string, unknown>
}

export interface AnalysisRun {
  id: string
  trace_id?: string
  pipeline_version?: string
  schema_version?: string
  artifact_schema_versions?: Record<string, string>
  status: RunStatus
  stage?: string
  apk_filename?: string
  file_name?: string
  filename?: string
  app_name?: string
  package_name?: string
  file_size?: number
  source_analysis_enabled?: boolean
  created_at: string
  updated_at?: string
  completed_at?: string | null
  progress?: number
  findings_count?: number
  stages?: RunStage[]
  manifest?: {
    pipeline_version?: string
    schema_version?: string
    artifact_schema_versions?: Record<string, string>
    stages?: RunStage[]
    apk?: { size_bytes?: number }
    analysis_incomplete?: boolean
    coverage_gaps?: Array<Record<string, unknown> | string>
    stop_reason?: string | null
    cache_hit?: boolean | null
    prompt_version?: string | null
    [key: string]: unknown
  } | null
  config?: RunConfigSnapshot
  error?: string | null
  error_message?: string | null
}

export interface EvidenceItem {
  id?: string
  evidence_id?: string
  type?: string
  kind?: string
  level?: string
  status?: string
  title?: string
  description?: string
  text?: string
  file?: string
  path?: string
  line?: number
  snippet?: string
  value?: string
}

export interface Finding {
  id: string
  run_id?: string
  pipeline_version?: string
  schema_version?: string
  rule_ids?: string[]
  rule_id?: string
  title: string
  description?: string
  severity: Severity
  severity_reason?: string[]
  confidence?: number | string
  evidence_level?: 'L1' | 'L2' | 'L3' | string
  funnel_disposition?: string
  triage_disposition?: 'potential_chain' | 'exposure_only' | 'insufficient' | string
  evidence_decision?: string
  analysis_status?: 'rule_only' | 'ai_skipped' | 'ai_failed' | 'ai_incomplete' | 'ai_partial' | 'ai_completed' | 'ai_budget_deferred' | 'human_confirmed' | string
  analysis_track?: 'l1_triage' | 'l2_review' | string
  candidate_verdict?: string
  fact_integrity_status?: 'invalid' | 'verified' | 'incomplete' | string
  semantic_status?: 'not_applicable' | 'closed' | 'not_proven' | string
  exploitability_status?: 'dynamically_confirmed' | 'statically_gradeable' | 'pending' | string
  status_layers?: {
    funnel?: string | null
    analysis?: string | null
    evidence?: string | null
    review?: string | null
    [key: string]: unknown
  }
  review_state?: {
    status?: string
    reason?: string
    evidence_decision?: string
    false_positive_basis?: string[]
    [key: string]: unknown
  }
  dataflow_status?: 'not_applicable' | 'not_proven' | 'intraprocedural' | 'interprocedural' | 'verified'
  authorization_status?: 'unknown' | 'conditional' | 'unprotected' | 'protected' | 'strongly_protected'
  guard_status?: 'absent' | 'present_effective' | 'present_partial' | 'present_bypassable' | 'unknown'
  /** R-1（2026-08-15）：动态 receiver 暴露面的可判定性分级（规则层标注）。 */
  receiver_flag_tier?: 'confirmed_exported_clean' | 'confirmed_exported_gap' | 'unresolved_flag' | string
  /** R-4（2026-08-15）：动态 receiver 分组语义（funnel 写回，随候选透传）。 */
  receiver_semantics?: {
    flag_tier?: string
    owner?: string
    actions?: string[]
  }
  authorization_matrix?: Array<{
    operation?: string
    access?: string
    path_region?: { kind?: string; value?: string | null }
    reachability?: string
    authorization?: { status?: string }
    attacker_prerequisites?: string[]
  }>
  guard_coverage?: {
    status?: string
    guards?: Array<Record<string, unknown>>
    identity_sources?: Array<Record<string, unknown>>
    blocking_gaps?: Array<Record<string, unknown>>
  }
  impact_status?: 'potential' | 'statically_confirmed' | 'dynamically_confirmed'
  analysis_incomplete?: boolean
  review_priority?: number
  flow_kind?: string
  component?: string
  component_name?: string
  receiver_binding?: {
    registration?: { path?: string; line?: number; text?: string; kind?: string; method_name?: string }
    actions?: string[]
    flag_status?: string
    export_status?: string
    [key: string]: unknown
  }
  category?: string
  cwe?: string
  location?: string
  remediation?: string[]
  recommendation?: string
  review_status?: ReviewStatus | LegacyReviewStatus
  review_reason?: string | null
  false_positive_basis?: string[]
  deterministic_chain_verified?: boolean
  dynamic_validation_status?: string
  reachability_status?: string
  ai_required?: boolean
  ai_eligible?: boolean
  ai_budget_deferred?: boolean
  ai_stop_reason?: string | null
  cache_hit?: boolean | null
  prompt_version?: string | null
  ai_schema_version?: string | null
  prompt_hash?: string | null
  schema_hash?: string | null
  input_schema_hash?: string | null
  output_schema_hash?: string | null
  provider?: string | null
  provider_kind?: string | null
  model?: string | null
  ai_metadata?: Record<string, unknown>
  ai_analysis?: Record<string, unknown>
  ai_analysis_trace?: Array<Record<string, unknown>>
  locations?: EvidenceItem[]
  sources?: EvidenceItem[]
  sinks?: EvidenceItem[]
  propagation_paths?: EvidenceItem[]
  blocking_gaps?: Array<Record<string, unknown> | string>
  coverage_gaps?: Array<Record<string, unknown> | string>
  slice_id?: string
  slice_refs?: string[]
  context_requests?: Array<Record<string, unknown>>
  evidence?: EvidenceItem[] | string | Record<string, unknown>
  created_at?: string
  updated_at?: string
}

export interface SliceContext {
  context_id: string
  kind: string
  path: string
  start_line: number
  end_line: number
  class_name?: string
  method_name?: string
  reason?: string
  content_sha256: string
  content: string
}

export interface ContextSliceResponse {
  finding_id: string
  slice_id: string
  round_count: number
  latest_round: string
  source?: 'live_slice' | 'report_evidence'
  slice: {
    contexts: SliceContext[]
    edges: Array<{ from: string; to: string; type: string; status: string }>
    guards: Array<Record<string, unknown>>
    request_history: Array<Record<string, unknown>>
    unresolved: Array<Record<string, unknown>>
    limitations: string[]
  }
}

export interface ListResponse<T> {
  items: T[]
  total?: number
}

export interface CreateRunInput {
  file: File
  authorized: boolean
  sourceAnalysisEnabled: boolean
}

export interface CreateRunProgress {
  loaded: number
  total: number
  percent: number
}

/** 资产注册表条目（GET /api/assets；apk_path 已由后端脱敏剔除）。 */
export interface Asset {
  id: string
  package_name: string
  apk_filename: string
  apk_sha256: string
  source: string
  status: 'ready' | 'scanning' | 'error'
  last_run_id: string | null
  created_at: string
  updated_at: string
}

/** batches.assets 快照元素（创建时固化，资产删除后仍可审计）。 */
export interface BatchAssetSnapshot {
  asset_id: string
  package_name: string
  apk_sha256: string
}

/** 探索候选人工队列条目（GET /api/runs/{id}/explorer/candidates；T2.10——
 *  投影脱 hops 全文与轮审计；服务端预排序：置信度 → deep_dive 证据 → 跳完整度）。 */
export interface ExplorerQueueEntry {
  candidate_id: string | null
  component: { kind: string | null; name: string | null; entry_method: string | null }
  chain: { source: string | null; sink: string | null; hop_count: number }
  validation: {
    status: 'validated' | 'partially_validated' | 'unverified' | 'pending'
    verified_hop_count: number | null
    failed_hop_indices: number[]
    blocked_by_guard: boolean
    custom_sink_proposal: boolean
    notes: string | null
  }
  deep_dive: {
    status: string | null
    evidence_count: number
    confirmed_fact_count: number
    remaining_gap_count: number
    unverifiable_evidence_count: number
    evidence_truncated_count: number
    requests_used: number
  } | null
  confidence: string | null
  sort_keys: { confidence_rank: number; deep_dive_evidence: number; hop_ratio: number }
}

/** 人工队列响应（partial/unverified/pending 主体；validated 仅计数对照）。 */
export interface ExplorerQueueResponse {
  entries: ExplorerQueueEntry[]
  counts: {
    validated: number
    partially_validated: number
    unverified: number
    pending: number
    total: number
    queue_length: number
    deep_dive_completed: number
  }
}

/** 批量扫描记录与 runs 聚合汇总（GET /api/batches/{id}；assets_json 已剔除）。 */
export interface BatchSummary {
  id: string
  status: 'pending' | 'running' | 'completed' | 'partial' | 'failed'
  max_ai_calls: number | null
  max_wall_seconds: number | null
  ai_skipped_count: number
  assets: BatchAssetSnapshot[]
  created_at: string
  updated_at: string
  completed_at: string | null
  total_runs: number
  completed_runs: number
  failed_runs: number
  ai_skipped: number
  ai_skipped_by_budget: number
  ai_skipped_by_wall_clock: number
}
