import { Compass } from '@phosphor-icons/react'
import type { ExplorerQueueEntry, ExplorerQueueResponse } from '../../lib/types'
import { EmptyState } from '../../ui/StateView'

/**
 * 探索候选人工队列（T2.10，方案 §2.0/§5.4）：partial/unverified 按
 * 置信度 → deep_dive 证据 → 跳回查完整度排序（服务端预排序——评审 R-1），
 * validated 仅计数对照（已并入主链 findings）。数据由 RunDetailPage
 * usePolling 供给（对齐 FindingsPanel 的锁优化模式——评审 R-4）。
 */

const STATUS_META: Record<string, { label: string; cls: string }> = {
  partially_validated: { label: '部分验证', cls: 'explorer-tier-partial' },
  unverified: { label: '未验证', cls: 'explorer-tier-unverified' },
  pending: { label: '待校验', cls: 'explorer-tier-pending' },
}

function statusMeta(status: string) {
  return STATUS_META[status] || STATUS_META.pending
}

function confidenceLabel(confidence: string | null): string {
  if (confidence === 'high') return '高'
  if (confidence === 'medium') return '中'
  if (confidence === 'low') return '低'
  return '未知'
}

function entryTitle(entry: ExplorerQueueEntry): string {
  const source = entry.chain.source || '未知 source'
  const sink = entry.chain.sink || '未知 sink'
  return `${source} → ${sink}`
}

export function ExplorerQueuePanel({ queue }: { queue: ExplorerQueueResponse | null }) {
  const counts = queue?.counts
  const entries = queue?.entries || []
  if (!counts || counts.total === 0) {
    return (
      <section className="panel">
        <header className="panel-head">
          <h2><Compass size={16} />探索人工队列</h2>
        </header>
        <EmptyState icon={<Compass size={27} />} title="探索轨未启用或无候选" description="提交任务时开启「启用探索轨」后，部分验证与未验证的探索候选将在此按置信度与深挖证据排序展示。" />
      </section>
    )
  }
  return (
    <section className="panel">
      <header className="panel-head">
        <h2><Compass size={16} />探索人工队列</h2>
        <div className="explorer-queue-counts">
          <span className="receiver-tier-badge receiver-tier-gap">部分验证 {counts.partially_validated}</span>
          <span className="receiver-tier-badge receiver-tier-unresolved">未验证 {counts.unverified}</span>
          <span className="receiver-tier-badge receiver-tier-unknown">已验证并入主链 {counts.validated}</span>
          <span className="receiver-tier-badge receiver-tier-clean">深挖完成 {counts.deep_dive_completed}</span>
        </div>
      </header>
      {entries.length === 0 ? (
        <EmptyState
          icon={<Compass size={27} />}
          title={`全部 ${counts.total} 条候选已通过三档校验`}
          description="无待人工复核的探索候选（部分验证与未验证候选为零）。"
        />
      ) : (
        <ul className="explorer-queue-list">
          {entries.map((entry) => {
            const meta = statusMeta(entry.validation.status)
            const hops = entry.chain.hop_count
            const verified = entry.validation.verified_hop_count
            const deepDive = entry.deep_dive
            return (
              <li key={entry.candidate_id || `${entry.chain.sink}-${hops}`} className="explorer-queue-item">
                <div className="explorer-queue-main">
                  <span className={`receiver-tier-badge ${meta.cls}`}>{meta.label}</span>
                  <strong className="explorer-queue-chain" title={entryTitle(entry)}>
                    {entryTitle(entry)}
                  </strong>
                  <span className="explorer-queue-component">
                    {entry.component.kind || '未知'} · {entry.component.name || '未知组件'}
                  </span>
                </div>
                <div className="explorer-queue-meta">
                  <span>置信度 {confidenceLabel(entry.confidence)}</span>
                  <span>跳回查 {verified ?? '—'}/{hops}</span>
                  {deepDive ? (
                    <span title={`已证命题 ${deepDive.confirmed_fact_count} · 未决缺口 ${deepDive.remaining_gap_count} · AI 请求 ${deepDive.requests_used}`}>
                      深挖 {deepDive.status} · 证据 {deepDive.evidence_count}
                      {deepDive.evidence_truncated_count > 0 ? `（截断 ${deepDive.evidence_truncated_count}）` : ''}
                    </span>
                  ) : (
                    <span>未深挖</span>
                  )}
                  {entry.validation.custom_sink_proposal && <span>custom sink 待确认</span>}
                  {entry.validation.blocked_by_guard && <span>guard 阻断</span>}
                  {entry.validation.notes && (
                    <span className="explorer-queue-notes" title={entry.validation.notes || undefined}>
                      {entry.validation.notes}
                    </span>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
