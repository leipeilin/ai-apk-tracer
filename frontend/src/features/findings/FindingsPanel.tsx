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

// R-4（2026-08-15）：动态 Receiver 暴露面按注册模块（owner）分组展示，
// confirmed_exported_clean 组与组内候选置顶。rank 越小越靠前。
const RECEIVER_TIER_META: Record<string, { label: string; rank: number; cls: string }> = {
  confirmed_exported_clean: { label: '已确认导出·无缺口', rank: 0, cls: 'receiver-tier-clean' },
  confirmed_exported_gap: { label: '已确认导出·带缺口', rank: 1, cls: 'receiver-tier-gap' },
  unresolved_flag: { label: 'flag 未解析', rank: 2, cls: 'receiver-tier-unresolved' },
  tier_unknown: { label: '未分级', rank: 3, cls: 'receiver-tier-unknown' },
}

function receiverTierKey(finding: Finding): string {
  return finding.receiver_semantics?.flag_tier || finding.receiver_flag_tier || 'tier_unknown'
}

/** R-4：owner 优先取后端写回的 receiver_semantics；旧 run 无该字段时按
 *  注册点路径前 3 段回退推导（与后端 _pipeline_registration_owner 口径一致）。 */
function receiverOwner(finding: Finding): string {
  const owner = finding.receiver_semantics?.owner
  if (owner) return owner
  const raw = String(finding.receiver_binding?.registration?.path || finding.component_name || '')
  const path = raw.startsWith('dynamic:') ? raw.slice('dynamic:'.length) : raw
  const parts = path.split('/').filter(Boolean)
  return parts.length >= 3 ? parts.slice(0, 3).join('/') : (path || 'owner_unknown')
}

interface ReceiverGroup {
  owner: string
  items: Finding[]
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

  // R-4：receiver_exposure 候选按 owner 分组（组内 tier 置顶排序、组间最高 tier 排序），
  // 其余候选保持原平铺列表。旧 run 数据（无 receiver_semantics）走前端回退推导。
  const { receiverGroups, otherFindings } = useMemo(() => {
    const byOwner = new Map<string, Finding[]>()
    for (const item of filtered) {
      if (item.flow_kind !== 'receiver_exposure') continue
      const owner = receiverOwner(item)
      byOwner.set(owner, [...(byOwner.get(owner) || []), item])
    }
    const groups: ReceiverGroup[] = [...byOwner.entries()].map(([owner, items]) => ({
      owner,
      items: [...items].sort((a, b) => {
        const rankDelta = RECEIVER_TIER_META[receiverTierKey(a)].rank - RECEIVER_TIER_META[receiverTierKey(b)].rank
        return rankDelta || (b.review_priority || 0) - (a.review_priority || 0) || a.id.localeCompare(b.id)
      }),
    }))
    groups.sort((a, b) => {
      const aBest = Math.min(...a.items.map((item) => RECEIVER_TIER_META[receiverTierKey(item)].rank))
      const bBest = Math.min(...b.items.map((item) => RECEIVER_TIER_META[receiverTierKey(item)].rank))
      return aBest - bBest || a.owner.localeCompare(b.owner)
    })
    return {
      receiverGroups: groups,
      otherFindings: filtered.filter((item) => item.flow_kind !== 'receiver_exposure'),
    }
  }, [filtered])

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
        <div className="findings-list">
          {receiverGroups.map((group) => (
            <section className="receiver-group" key={group.owner}>
              <header className="receiver-group-head">
                <span className="receiver-group-owner" title={group.owner}>{group.owner}</span>
                <span className="receiver-group-count">{group.items.length} 组候选</span>
                <span className="receiver-group-tiers">
                  {['confirmed_exported_clean', 'confirmed_exported_gap', 'unresolved_flag', 'tier_unknown']
                    .map((tier) => {
                      const count = group.items.filter((item) => receiverTierKey(item) === tier).length
                      return count ? <span key={tier} className={`receiver-tier-dot ${RECEIVER_TIER_META[tier].cls}`}>{count} {RECEIVER_TIER_META[tier].label}</span> : null
                    })}
                </span>
              </header>
              <div className="findings-table" role="table" aria-label={`动态 Receiver 暴露面分组：${group.owner}`}>
                <div className="finding-row finding-head" role="row"><span>风险发现</span><span>证据信号</span><span>人工复核</span><span /></div>
                <AnimatePresence initial={false}>
                  {group.items.map((finding, index) => (
                    <FindingRow key={finding.id} finding={finding} index={index} reduceMotion={reduceMotion} onSelect={() => setSelected(finding)} />
                  ))}
                </AnimatePresence>
              </div>
            </section>
          ))}
          {otherFindings.length > 0 && (
            <div className="findings-table" role="table" aria-label="安全发现列表">
              <div className="finding-row finding-head" role="row"><span>风险发现</span><span>证据信号</span><span>人工复核</span><span /></div>
              <AnimatePresence initial={false}>
                {otherFindings.map((finding, index) => (
                  <FindingRow key={finding.id} finding={finding} index={index} reduceMotion={reduceMotion} onSelect={() => setSelected(finding)} />
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>
      ) : (
        <EmptyState icon={<ShieldSlash size={27} />} title={findings.length ? '没有匹配的发现' : '暂未发现风险'} description={findings.length ? '调整风险等级、复核状态或搜索条件。' : '分析完成后，安全发现与证据会汇总在这里。'} />
      )}

      <FindingDrawer key={selected?.id || 'closed'} finding={selected} onClose={() => setSelected(null)} onUpdated={updateFinding} />
    </section>
  )
}

function FindingRow({ finding, index, reduceMotion, onSelect }: {
  finding: Finding
  index: number
  reduceMotion: boolean | null
  onSelect: () => void
}) {
  const tier = receiverTierKey(finding)
  const tierMeta = RECEIVER_TIER_META[tier]
  return (
    <motion.button
      layout
      key={finding.id}
      className="finding-row"
      onClick={onSelect}
      initial={reduceMotion ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ delay: reduceMotion ? 0 : Math.min(index * 0.035, 0.2) }}
      role="row"
    >
      <span className="finding-primary">
        <span>
          {finding.flow_kind === 'receiver_exposure' && tierMeta && (
            <span className={`receiver-tier-badge ${tierMeta.cls}`} title={`动态 Receiver 可判定性：${tierMeta.label}`}>{tierMeta.label}</span>
          )}
          <SeverityBadge severity={finding.severity} /><strong>{finding.title}</strong>
        </span>
        <small>{findingKindLabel(finding)} · {finding.category || finding.cwe || '未分类'}{finding.location ? ` · ${finding.location}` : ''}</small>
      </span>
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

