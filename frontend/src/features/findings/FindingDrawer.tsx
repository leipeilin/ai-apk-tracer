import { BracketsCurly, CheckCircle, Circle, FileCode, MapPin, Prohibit, ShieldWarning, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useState } from 'react'
import { ApiError, api } from '../../lib/api'
import type { ContextSliceResponse, EvidenceItem, Finding, LegacyReviewStatus, ReviewStatus } from '../../lib/types'
import { Button } from '../../ui/Button'
import { DataBadge, reviewLabel, SeverityBadge } from '../../ui/Badge'
import { Drawer } from '../../ui/Drawer'
import { ReportPanel } from '../reports/ReportPanel'

interface EvidenceGroup {
  title: string
  items: EvidenceItem[]
}

/** v2026-08-09：把 ai_analysis 真正输出（summary/verdict/flaw_holds/confidence_tier/
 *  impact_vector CVSS 因子/blocking_gaps/uncertainties/confidence_rationale）呈现给用户。
 * AI 不定级铁律：仅展示 observation 性质的方向判定，不输出 severity 等级或 CVSS 分数。
 */
/** v2026-08-14：summary 超过该行数折叠（近似估算：换行 + 每 60 字符 1 行）。 */
const SUMMARY_MAX_LINES = 6
function AiAnalysisSummary({ analysis }: { analysis: Record<string, unknown> }) {
  const summary = typeof analysis.summary === 'string' ? analysis.summary : ''
  const verdict = typeof analysis.verdict === 'string' ? analysis.verdict : ''
  const candidateVerdict = typeof analysis.candidate_verdict === 'string' ? analysis.candidate_verdict : ''
  const flawHolds = analysis.flaw_holds === true ? true : analysis.flaw_holds === false ? false : null
  const confidenceTier = typeof analysis.confidence_tier === 'string' ? analysis.confidence_tier : ''
  const confidenceRationale = typeof analysis.confidence_rationale === 'string' ? analysis.confidence_rationale : ''
  const reachabilityClass = typeof analysis.reachability_class === 'string' ? analysis.reachability_class : ''
  const impactVector = (analysis.impact_vector && typeof analysis.impact_vector === 'object') ? analysis.impact_vector as Record<string, string> : null
  const blockingGaps = Array.isArray(analysis.blocking_gaps) ? analysis.blocking_gaps as Array<{ code?: string; message?: string; critical?: boolean }> : []
  const uncertainties = Array.isArray(analysis.uncertainties) ? analysis.uncertainties as Array<{ code?: string; message?: string }> : []
  const hasContent = summary || verdict || candidateVerdict || flawHolds !== null || confidenceTier || confidenceRationale
    || reachabilityClass || impactVector || blockingGaps.length || uncertainties.length
  // v2026-08-14：summary 可能很长，超过 SUMMARY_MAX_LINES 行折叠并显示展开/收起按钮
  const [summaryExpanded, setSummaryExpanded] = useState(false)
  const summaryLines = summary.split('\n').length + Math.ceil(summary.length / 60)
  const summaryLong = summaryLines > SUMMARY_MAX_LINES
  if (!hasContent) return null
  const tierColor: Record<string, string> = { high: 'severity-high', medium: 'severity-medium', low: 'severity-low' }
  return (
    <div className="ai-analysis-summary">
      {summary && (
        <div className={`ai-summary-wrap${summaryLong && !summaryExpanded ? ' is-collapsed' : ''}`}>
          <p className="ai-summary-paragraph">{summary}</p>
        </div>
      )}
      {summaryLong && (
        <button type="button" className="ai-summary-toggle" onClick={() => setSummaryExpanded((value) => !value)}>
          {summaryExpanded ? '收起' : '展开全文'}
        </button>
      )}
      <dl className="detail-grid">
        {flawHolds !== null && (
          <div><dt>缺陷判定</dt><dd>{flawHolds ? 'AI 认为缺陷成立' : 'AI 认为缺陷不成立'}</dd></div>
        )}
        {(verdict || candidateVerdict) && (
          <div><dt>裁决</dt><dd>{verdict || candidateVerdict}</dd></div>
        )}
        {confidenceTier && (
          <div><dt>置信档</dt><dd><span className={tierColor[confidenceTier] || ''}>{confidenceTier}</span></dd></div>
        )}
        {reachabilityClass && (
          <div><dt>可达性</dt><dd>{reachabilityClass}</dd></div>
        )}
      </dl>
      {impactVector && (
        <div className="ai-impact-vector">
          <span className="ai-section-label">影响因子（CVSS 描述，不输出分数）：</span>
          {Object.entries(impactVector).map(([k, v]) => (
            <DataBadge key={k} label={impactLabel[k] ?? k}>{String(v)}</DataBadge>
          ))}
        </div>
      )}
      {confidenceRationale && (
        <p className="ai-rationale"><strong>置信度理由：</strong>{confidenceRationale}</p>
      )}
      {(blockingGaps.length > 0 || uncertainties.length > 0) && (
        <ul className="ai-gap-list">
          {blockingGaps.map((g, i) => (
            <li key={`bg-${i}`}>
              <WarningCircle size={14} />
              <span>{g.code ? `[${g.code}] ` : ''}{g.message || ''}</span>
            </li>
          ))}
          {uncertainties.map((u, i) => (
            <li key={`uc-${i}`}>
              <span className="ai-uncertainty">{u.code ? `[${u.code}] ` : ''}{u.message || ''}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const impactLabel: Record<string, string> = {
  confidentiality: '保密性',
  integrity: '完整性',
  availability: '可用性',
  privileges_required: '所需权限',
  attack_complexity: '攻击复杂度',
  user_interaction: '用户交互',
}

function buildEvidenceGroups(finding: Finding | null): EvidenceGroup[] {
  if (!finding) return []
  const groups: EvidenceGroup[] = []
  const locations = finding.locations || []
  const sources = finding.sources || []
  const paths = finding.propagation_paths || []
  const sinks = finding.sinks || []
  if (locations.length) groups.push({ title: '证据位置', items: locations.map((item) => ({ ...item, title: '证据位置' })) })
  if (sources.length) groups.push({ title: '不可信输入', items: sources.map((item) => ({ ...item, title: '不可信输入' })) })
  if (paths.length) groups.push({ title: '传播路径', items: paths.map((item) => ({ ...item, title: '传播路径' })) })
  if (sinks.length) groups.push({ title: '敏感操作', items: sinks.map((item) => ({ ...item, title: '敏感操作' })) })
  if (finding.evidence) {
    if (typeof finding.evidence === 'string') groups.push({ title: '旧版 evidence', items: [{ description: finding.evidence }] })
    else if (Array.isArray(finding.evidence)) groups.push({ title: '旧版 evidence', items: finding.evidence })
    else groups.push({ title: '旧版 evidence', items: Object.entries(finding.evidence).map(([title, value]) => ({ title, value: typeof value === 'string' ? value : JSON.stringify(value, null, 2) })) })
  }
  return groups
}

const reviewOptions: Array<{ value: ReviewStatus; label: string; icon: typeof Circle }> = [
  { value: 'pending_manual', label: '待人工复核', icon: Circle },
  { value: 'confirmed', label: '确认有效', icon: CheckCircle },
  { value: 'manual_false_positive', label: '标记误报', icon: Prohibit },
]

function currentReviewStatus(status: Finding['review_status']): ReviewStatus | LegacyReviewStatus {
  return status || 'pending_ai'
}

const analysisLabels: Record<string, string> = {
  rule_only: '仅规则', ai_skipped: '未执行', ai_failed: '失败', ai_incomplete: '上下文不足',
  ai_partial: '部分完成', ai_completed: '已完成', ai_budget_deferred: '预算延后', human_confirmed: '人工确认',
}
const triageLabels: Record<string, string> = {
  potential_chain: '潜在链，待确定性验证', exposure_only: '仅暴露事实', insufficient: '上下文不足',
}
const decisionLabels: Record<string, string> = {
  supported: '证据支持', unresolved: '未解决', exposure_only: '仅暴露事实',
  deterministically_refuted: '确定性反驳', ai_false_positive: 'AI 反驳且有确定性依据', mixed: '聚合结果不一致',
}

/**
 * 汇总单条发现的风险、证据、方法级切片、报告和人工复核操作。
 * 切片按 finding 切换异步加载，关闭或切换发现后会忽略旧请求结果。
 */
export function FindingDrawer({ finding, onClose, onUpdated }: { finding: Finding | null; onClose: () => void; onUpdated: (finding: Finding) => void }) {
  const [saving, setSaving] = useState<ReviewStatus | null>(null)
  const [reviewReason, setReviewReason] = useState(finding?.review_reason || '')
  const [error, setError] = useState('')
  const [sliceData, setSliceData] = useState<ContextSliceResponse | null>(null)
  const [sliceLoading, setSliceLoading] = useState(false)
  const [sliceError, setSliceError] = useState('')
  const evidenceGroups = buildEvidenceGroups(finding)
  const aiPayload = finding ? [finding.ai_metadata, finding.ai_analysis, finding.ai_analysis_trace] : []
  const promptVersion = finding?.prompt_version || textValue(nestedValue(aiPayload, 'prompt_version'))
  const promptHash = finding?.prompt_hash || textValue(nestedValue(aiPayload, 'messages_hash', 'prompt_hash'))
  const schemaVersion = finding?.ai_schema_version || textValue(nestedValue(aiPayload, 'output_model_version', 'schema_version'))
  const schemaHash = finding?.schema_hash || finding?.output_schema_hash || textValue(nestedValue(aiPayload, 'output_schema_sha256', 'schema_hash'))
  const cacheHit = finding?.cache_hit ?? booleanValue(nestedValue(aiPayload, 'cache_hit'))

  useEffect(() => {
    setReviewReason(finding?.review_reason || '')
    setError('')
  }, [finding?.id, finding?.review_reason])

  useEffect(() => {
    let active = true
    setSliceData(null)
    setSliceError('')
    if (!finding?.slice_id) {
      setSliceLoading(false)
      return () => { active = false }
    }
    setSliceLoading(true)
    api.getFindingSlice(finding.id)
      .then((value) => { if (active) setSliceData(value) })
      .catch((value) => { if (active) setSliceError(value instanceof Error ? value.message : '代码切片加载失败') })
      .finally(() => { if (active) setSliceLoading(false) })
    return () => { active = false }
  }, [finding?.id, finding?.slice_id])

  const review = async (status: ReviewStatus) => {
    if (!finding || currentReviewStatus(finding.review_status) === status) return
    if (['confirmed', 'manual_false_positive'].includes(status) && !reviewReason.trim()) {
      setError(status === 'confirmed' ? '确认漏洞时必须填写判断依据' : '标记误报时必须填写原因')
      return
    }
    setSaving(status)
    setError('')
    try {
      const updated = await api.reviewFinding(finding.id, status, {
        reason: reviewReason.trim(),
        expectedStatus: currentReviewStatus(finding.review_status),
        requestId: crypto.randomUUID(),
      })
      onUpdated({ ...finding, ...updated, review_status: updated.review_status || status })
    } catch (value) {
      if (value instanceof ApiError && value.status === 409) {
        let message = '复核状态已被其他页面更新，请刷新列表后重试'
        if (finding.run_id) {
          try {
            const latest = (await api.getFindings(finding.run_id)).find((item) => item.id === finding.id)
            if (latest) {
              onUpdated({ ...finding, ...latest })
              message = '复核状态已被其他页面更新，当前数据已刷新，请重新确认'
            }
          } catch {
            // 保留原始冲突提示，避免刷新失败掩盖并发冲突。
          }
        }
        setError(message)
      } else {
        setError(value instanceof Error ? value.message : '复核状态保存失败')
      }
    } finally {
      setSaving(null)
    }
  }

  return (
    <Drawer open={Boolean(finding)} onClose={onClose} title={finding?.title || '发现详情'} eyebrow="EVIDENCE REVIEW">
      {finding && (
        <div className="finding-detail">
          <div className="finding-badges">
            <SeverityBadge severity={finding.severity} />
            <DataBadge label="对象">{findingKindLabel(finding)}</DataBadge>
            <DataBadge label="置信度">{finding.confidence ?? '—'}</DataBadge>
            <DataBadge label="确定性事实">{finding.fact_integrity_status || '未记录'}</DataBadge>
            <DataBadge label="语义链">{semanticLabel(finding.semantic_status)}</DataBadge>
            <DataBadge label="可利用性">{finding.exploitability_status || 'pending'}</DataBadge>
            <DataBadge label="AI 状态">{analysisLabels[finding.analysis_status || ''] || finding.analysis_status || '未记录'}</DataBadge>
            <DataBadge label="证据决策">{decisionLabels[finding.evidence_decision || 'unresolved'] || finding.evidence_decision}</DataBadge>
            <DataBadge label="复核">{reviewLabel[finding.review_status || 'pending_ai'] || finding.review_status}</DataBadge>
            {finding.analysis_incomplete && <DataBadge label="完整性">不完整</DataBadge>}
          </div>

          {finding.semantic_status === 'closed' && (
            <div className="integrity-alert" role="note">
              <ShieldWarning size={18} weight="fill" />
              <div><strong>语义链已闭合不等于漏洞已成立</strong><p>仍需结合确定性事实完整性、授权/Guard、可利用性状态和人工复核结论判断。</p></div>
            </div>
          )}

          <section className="detail-section">
            <h3><ShieldWarning size={18} />风险说明</h3>
            <p>{finding.description || '暂无补充说明。'}</p>
            <dl className="detail-grid">
              <div><dt>分类</dt><dd>{finding.category || '—'}</dd></div>
              <div><dt>CWE</dt><dd>{finding.cwe || '—'}</dd></div>
              <div><dt>位置</dt><dd>{finding.location || '—'}</dd></div>
              <div><dt>当前复核</dt><dd>{reviewLabel[finding.review_status || 'pending_ai']}</dd></div>
            </dl>
          </section>

          <section className="detail-section">
            <h3><ShieldWarning size={18} />四层状态</h3>
            <dl className="detail-grid">
              <div><dt>Funnel</dt><dd>{finding.status_layers?.funnel || finding.funnel_disposition || '未记录'}</dd></div>
              <div><dt>Analysis</dt><dd>{finding.status_layers?.analysis || finding.analysis_status || 'rule_only'}</dd></div>
              <div><dt>Evidence</dt><dd>{finding.status_layers?.evidence || finding.evidence_decision || 'unresolved'}</dd></div>
              <div><dt>Review</dt><dd>{reviewLabel[String(finding.status_layers?.review || finding.review_status || 'pending_ai')] || String(finding.status_layers?.review || finding.review_status || 'pending_ai')}</dd></div>
            </dl>
            <p className="muted-copy">四层分别表示候选路由、自动分析、证据决策和人工复核，不能互相替代。</p>
          </section>

          <section className="detail-section">
            <h3><FileCode size={18} />确定性事实</h3>
            <dl className="detail-grid">
              <div><dt>事实完整性</dt><dd>{finding.fact_integrity_status || '未记录'}</dd></div>
              <div><dt>外部可达</dt><dd>{finding.reachability_status || 'unknown'}</dd></div>
              <div><dt>数据流</dt><dd>{finding.dataflow_status || 'not_proven'}</dd></div>
              <div><dt>确定性闭链</dt><dd>{finding.deterministic_chain_verified === true ? '已验证' : finding.deterministic_chain_verified === false ? '未验证' : '未记录'}</dd></div>
              <div><dt>授权</dt><dd>{finding.authorization_status || 'unknown'}</dd></div>
              <div><dt>Guard</dt><dd>{finding.guard_status || 'unknown'}</dd></div>
              <div><dt>影响</dt><dd>{finding.impact_status || 'potential'}</dd></div>
              <div><dt>可利用性</dt><dd>{finding.exploitability_status || 'pending'}</dd></div>
            </dl>
          </section>

          <section className="detail-section">
            <h3><BracketsCurly size={18} />AI observation</h3>
            <p className="muted-copy">以下内容是 AI 对候选链路的观察，AI 不定级（不输出 severity 等级或 CVSS 分数），结论仅作为方向判定与决策证据。</p>
            {/* v2026-08-09（fix）：之前的实现只展示元数据键（轨道/Prompt/Schema/Provider），
                AI 真正输出（summary/verdict/flaw_holds/confidence_tier/impact_vector 等 30+ 字段）
                完全埋在 finding JSON 里未呈现给用户。现拆为"分析结论 + 版本元数据"两段。 */}
            {finding.ai_analysis && (
              <AiAnalysisSummary analysis={finding.ai_analysis} />
            )}
            <dl className="detail-grid">
              <div><dt>分析轨道</dt><dd>{finding.analysis_track || '未记录'}</dd></div>
              <div><dt>L1 triage</dt><dd>{triageLabels[finding.triage_disposition || ''] || finding.triage_disposition || '未记录'}</dd></div>
              <div><dt>L2 verdict</dt><dd>{finding.candidate_verdict || '未记录'}</dd></div>
              <div><dt>Analysis status</dt><dd>{analysisLabels[finding.analysis_status || ''] || finding.analysis_status || '未记录'}</dd></div>
              <div><dt>停止原因</dt><dd>{finding.ai_stop_reason || '正常完成或未记录'}</dd></div>
              <div><dt>Cache</dt><dd>{cacheHit === true ? '命中' : cacheHit === false ? '未命中' : '未记录'}</dd></div>
              <div><dt>Prompt</dt><dd>{versionHash(promptVersion, promptHash)}</dd></div>
              <div><dt>AI Schema</dt><dd>{versionHash(schemaVersion, schemaHash)}</dd></div>
              <div><dt>Provider / Model</dt><dd>{[finding.provider_kind || finding.provider, finding.model].filter(Boolean).join(' / ') || 'Finding API 未记录；以任务配置快照为准'}</dd></div>
            </dl>
          </section>

          {(finding.authorization_matrix?.length || finding.guard_coverage) && (
            <section className="detail-section">
              <h3><ShieldWarning size={18} />授权与 Guard 结论</h3>
              {finding.authorization_matrix?.length ? (
                <ul>
                  {finding.authorization_matrix.map((row, index) => (
                    <li key={`${row.operation || 'operation'}-${row.access || 'entry'}-${index}`}>
                      {row.operation || 'component_entry'} / {row.access || 'entry'}：
                      {row.authorization?.status || 'unknown'}
                      {row.path_region?.value ? `（${row.path_region.kind || 'path'}: ${row.path_region.value}）` : ''}
                      {row.attacker_prerequisites?.length ? `；前置条件：${row.attacker_prerequisites.join('；')}` : ''}
                    </li>
                  ))}
                </ul>
              ) : <p className="muted-copy">未生成 operation 级授权矩阵。</p>}
              {finding.guard_coverage && (
                <p className="muted-copy">
                  GuardCoverage：{finding.guard_coverage.status || finding.guard_status || 'unknown'}；
                  有效证据 {finding.guard_coverage.guards?.length || 0} 条，身份来源 {finding.guard_coverage.identity_sources?.length || 0} 条。
                </p>
              )}
            </section>
          )}

          {((finding.coverage_gaps?.length || 0) + (finding.blocking_gaps?.length || 0) > 0) && (
            <section className="detail-section gap-section">
              <h3><ShieldWarning size={18} />Pending gaps（覆盖与阻断条件）</h3>
              <p className="muted-copy">Gap 表示当前分析边界或待补证据，不能解释为“安全”，也不能单独解释为“漏洞成立”。</p>
              <ul>
                {[...(finding.coverage_gaps || []), ...(finding.blocking_gaps || [])].map((gap, index) => (
                  <li key={index} className={typeof gap === 'object' && gap.critical ? 'critical-gap' : ''}>
                    {formatGap(gap)}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {evidenceGroups.length > 0 ? (
            <section className="detail-section">
              <h3><FileCode size={18} />证据链</h3>
              {evidenceGroups.map((group) => (
                <div className="evidence-group" key={group.title}>
                  <h4 className="evidence-group-title">{group.title}</h4>
                  <div className="evidence-list">
                    {group.items.map((item, index) => (
                      <article className="evidence-item" key={item.id || index}>
                        <header>
                          <span>{String(index + 1).padStart(2, '0')}</span>
                          <strong>{item.title || item.type || group.title}</strong>
                          {item.level && <small>{item.level}</small>}
                        </header>
                        {(item.path || item.file) && <p className="evidence-path"><MapPin size={14} />{item.path || item.file}{item.line ? `:${item.line}` : ''}</p>}
                        {(item.description || item.value) && <p>{item.description || item.value}</p>}
                        {item.snippet && <pre>{item.snippet}</pre>}
                      </article>
                    ))}
                  </div>
                </div>
              ))}
            </section>
          ) : (
            <section className="detail-section">
              <h3><FileCode size={18} />证据链</h3>
              <p className="muted-copy">该发现没有结构化证据条目。</p>
            </section>
          )}

          {finding.slice_id && (
            <section className="detail-section">
              <h3><BracketsCurly size={18} />方法级代码切片</h3>
              {sliceLoading && <p className="muted-copy" aria-live="polite">正在加载可追溯代码上下文…</p>}
              {sliceError && <p className="form-error" role="alert">{sliceError}</p>}
              {sliceData && (
                <div className="slice-stack">
                  <div className="slice-summary">
                    <DataBadge label="分析轮次">{sliceData.round_count}</DataBadge>
                    <DataBadge label="上下文">{sliceData.slice.contexts.length}</DataBadge>
                    <DataBadge label="调用边">{sliceData.slice.edges.length}</DataBadge>
                  </div>
                  {sliceData.slice.contexts.map((context) => (
                    <article className="slice-context" key={context.context_id}>
                      <header>
                        <div>
                          <strong>{context.method_name || context.class_name || context.kind}</strong>
                          <small>{context.reason || '候选证据上下文'}</small>
                        </div>
                        <span>{context.start_line}-{context.end_line}</span>
                      </header>
                      <p className="evidence-path"><MapPin size={14} />{context.path}</p>
                      <pre>{context.content}</pre>
                    </article>
                  ))}
                  {sliceData.slice.request_history.length > 0 && (
                    <p className="muted-copy">模型已执行 {sliceData.slice.request_history.length} 次确定性上下文扩展请求。</p>
                  )}
                </div>
              )}
            </section>
          )}

          {finding.recommendation && <section className="detail-section recommendation"><h3>修复建议</h3><p>{finding.recommendation}</p></section>}

          <section className="detail-section">
            <h3>人工复核</h3>
            <label className="review-reason">
              <span>复核说明</span>
              <textarea
                value={reviewReason}
                onChange={(event) => setReviewReason(event.target.value)}
                placeholder="确认漏洞或标记误报时必须填写判断依据"
                rows={3}
              />
            </label>
            <div className="review-group" role="group" aria-label="复核状态">
              {reviewOptions.map(({ value, label, icon: Icon }) => (
                <Button
                  key={value}
                  variant={(finding.review_status || 'pending_ai') === value ? 'primary' : 'secondary'}
                  loading={saving === value}
                  disabled={saving !== null}
                  onClick={() => review(value)}
                  icon={<Icon size={17} weight={(finding.review_status || 'pending_ai') === value ? 'fill' : 'regular'} />}
                >{label}</Button>
              ))}
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
          </section>

          <ReportPanel findingId={finding.id} title={finding.title} />
        </div>
      )}
    </Drawer>
  )
}

function formatGap(gap: Record<string, unknown> | string): string {
  if (typeof gap === 'string') return gap
  const code = String(gap.code || 'ANALYSIS_GAP')
  const domain = formatDomain(gap.domain)
  const message = typeof gap.message === 'string' ? gap.message : ''
  return message ? `${code}${domain} · ${message}` : `${code}${domain}`
}

function formatDomain(value: unknown): string {
  if (typeof value === 'string' && value) return ` [${value}]`
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return ''
  const entries = Object.entries(value).filter(([, item]) => typeof item === 'string' && item)
  return entries.length ? ` [${entries.map(([key, item]) => `${key}=${item}`).join(', ')}]` : ''
}

function findingKindLabel(finding: Finding): string {
  if (finding.evidence_level === 'L1') return 'L1 exposure'
  if (finding.evidence_level === 'L2') return 'L2 静态链'
  if (finding.evidence_level === 'L3') return 'L3 高置信链'
  return finding.evidence_level || '旧版候选'
}

function semanticLabel(status?: string): string {
  if (status === 'closed') return '已闭链（非漏洞结论）'
  if (status === 'not_applicable') return '不适用'
  if (status === 'not_proven') return '未证明'
  return status || '未记录'
}

function versionHash(version?: string | null, hash?: string | null): string {
  if (!version && !hash) return '未记录'
  const shortHash = hash ? `${hash.slice(0, 12)}${hash.length > 12 ? '…' : ''}` : ''
  return [version, shortHash].filter(Boolean).join(' / ')
}

// 递归只从历史 AI metadata 中提取展示字段，不据此改写确定性事实或复核状态。
function nestedValue(value: unknown, ...keys: string[]): unknown {
  if (Array.isArray(value)) {
    for (let index = value.length - 1; index >= 0; index -= 1) {
      const found = nestedValue(value[index], ...keys)
      if (found !== undefined && found !== null) return found
    }
  } else if (typeof value === 'object' && value !== null) {
    const record = value as Record<string, unknown>
    for (const key of keys) {
      if (record[key] !== undefined && record[key] !== null) return record[key]
    }
    for (const child of Object.values(record)) {
      const found = nestedValue(child, ...keys)
      if (found !== undefined && found !== null) return found
    }
  }
  return undefined
}

function textValue(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}
