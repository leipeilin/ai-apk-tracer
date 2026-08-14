import { CaretRight, Funnel, MagnifyingGlass, ShieldSlash } from '@phosphor-icons/react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useMemo, useState } from 'react'
import { normalizeSeverity, severityMeta } from '../../lib/format'
import type { Finding, LegacyReviewStatus, ReviewStatus, Severity } from '../../lib/types'
import { DataBadge, reviewLabel, SeverityBadge } from '../../ui/Badge'
import { EmptyState } from '../../ui/StateView'
import { FindingDrawer } from './FindingDrawer'

const severities: Array<Severity | 'all'> = ['all', 'critical', 'high', 'medium', 'low', 'informational', 'pending']
const reviews: Array<ReviewStatus | LegacyReviewStatus | 'all'> = [
  'all', 'pending_ai', 'pending_manual', 'ai_false_positive', 'manual_false_positive', 'confirmed',
  'pending', 'false_positive', 'ai_candidate',
]
const analysisLabel: Record<string, string> = {
  rule_only: '仅规则',
  ai_skipped: '未执行',
  ai_failed: '失败',
  ai_incomplete: '上下文不足',
  ai_partial: '部分完成',
  ai_completed: '已完成',
  ai_budget_deferred: '预算延后',
  human_confirmed: '人工确认',
}

/** 按风险、复核状态和关键词筛选候选，并将选中项交给证据抽屉复核。 */
export function FindingsPanel({ findings, onChange }: { findings: Finding[]; onChange: (findings: Finding[]) => void }) {
  const [severity, setSeverity] = useState<Severity | 'all'>('all')
  const [review, setReview] = useState<ReviewStatus | LegacyReviewStatus | 'all'>('all')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Finding | null>(null)
  const reduceMotion = useReducedMotion()

  const filtered = useMemo(() => findings
    .filter((item) => severity === 'all' || normalizeSeverity(item.severity) === severity)
    .filter((item) => review === 'all' || item.review_status === review)
    .filter((item) => {
      const needle = query.trim().toLowerCase()
      return !needle || [item.title, item.description, item.category, item.cwe, item.location].some((value) => value?.toLowerCase().includes(needle))
    })
    .sort((a, b) => {
      // 后端 review_priority 表示人工复核紧迫度，优先于展示层的 severity 排序。
      const priorityDelta = (b.review_priority || 0) - (a.review_priority || 0)
      return priorityDelta || severityMeta[normalizeSeverity(a.severity)].rank - severityMeta[normalizeSeverity(b.severity)].rank
    }), [findings, query, review, severity])

  const updateFinding = (updated: Finding) => {
    const next = findings.map((item) => item.id === updated.id ? updated : item)
    onChange(next)
    setSelected(updated)
  }

  return (
    <section className="findings-section">
      <div className="section-heading">
        <div><span className="eyebrow">FINDINGS</span><h2>候选与安全发现</h2><p>L1 暴露事实、L2/L3 证据链、AI observation 与人工结论分层展示；语义闭链不等于漏洞成立。</p></div>
        <div className="finding-summary"><strong>{findings.length}</strong><span>项发现</span></div>
      </div>

      <div className="filters glass-panel">
        <label className="search-field"><MagnifyingGlass size={17} /><span className="sr-only">搜索发现</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、CWE 或位置" /></label>
        <label><Funnel size={15} /><span>风险</span><select value={severity} onChange={(event) => setSeverity(event.target.value as Severity | 'all')}>
          {severities.map((value) => <option key={value} value={value}>{value === 'all' ? '全部等级' : severityMeta[value].label}</option>)}
        </select></label>
        <label><span>复核</span><select value={review} onChange={(event) => setReview(event.target.value as ReviewStatus | LegacyReviewStatus | 'all')}>
          {reviews.map((value) => <option key={value} value={value}>{value === 'all' ? '全部状态' : reviewLabel[value]}</option>)}
        </select></label>
      </div>

      {filtered.length ? (
        <div className="findings-table" role="table" aria-label="安全发现列表">
          <div className="finding-row finding-head" role="row"><span>风险发现</span><span>证据信号</span><span>人工复核</span><span /></div>
          <AnimatePresence initial={false}>
            {filtered.map((finding, index) => (
              <motion.button
                layout
                key={finding.id}
                className="finding-row"
                onClick={() => setSelected(finding)}
                initial={reduceMotion ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ delay: reduceMotion ? 0 : Math.min(index * 0.035, 0.2) }}
                role="row"
              >
                <span className="finding-primary"><span><SeverityBadge severity={finding.severity} /><strong>{finding.title}</strong></span><small>{findingKindLabel(finding)} · {finding.category || finding.cwe || '未分类'}{finding.location ? ` · ${finding.location}` : ''}</small></span>
                <span className="finding-signals">
                  <DataBadge label="确定性事实">{finding.fact_integrity_status || '未记录'}</DataBadge>
                  <DataBadge label="L1 triage">{finding.triage_disposition || '未记录'}</DataBadge>
                  <DataBadge label="L2 verdict">{finding.candidate_verdict || '未记录'}</DataBadge>
                  <DataBadge label="Analysis status">{analysisLabel[finding.analysis_status || ''] || finding.analysis_status || '未记录'}</DataBadge>
                  <DataBadge label="证据决策">{finding.evidence_decision || finding.status_layers?.evidence || 'unresolved'}</DataBadge>
                  <DataBadge label="待处理 gap">{gapCount(finding)}</DataBadge>
                </span>
                <span className={`review-state review-${finding.review_status || 'pending_ai'}`}>{reviewLabel[finding.review_status || 'pending_ai'] || finding.review_status}</span>
                <CaretRight size={17} />
              </motion.button>
            ))}
          </AnimatePresence>
        </div>
      ) : (
        <EmptyState icon={<ShieldSlash size={27} />} title={findings.length ? '没有匹配的发现' : '暂未发现风险'} description={findings.length ? '调整风险等级、复核状态或搜索条件。' : '分析完成后，安全发现与证据会汇总在这里。'} />
      )}

      <FindingDrawer key={selected?.id || 'closed'} finding={selected} onClose={() => setSelected(null)} onUpdated={updateFinding} />
    </section>
  )
}

function findingKindLabel(finding: Finding): string {
  if (finding.evidence_level === 'L1') return 'L1 暴露事实'
  if (finding.evidence_level === 'L3') return 'L3 高置信证据链'
  if (finding.evidence_level === 'L2') return 'L2 静态证据链'
  return '旧版候选'
}

function gapCount(finding: Finding): number {
  return (finding.coverage_gaps?.length || 0) + (finding.blocking_gaps?.length || 0)
}

