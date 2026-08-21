import { CheckCircle, ClockCounterClockwise, Gauge, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useState } from 'react'
import { api } from '../../lib/api'
import { formatDate } from '../../lib/format'
import { usePolling } from '../../lib/usePolling'
import { StatusBadge } from '../../ui/Badge'
import { Button } from '../../ui/Button'

const ACTIVE_STATUSES = new Set(['pending', 'running'])
/** 轮询悬挂提示阈值（评审 R-8：后端崩溃时 running 不终态，T1.3 D6 前端呈现）。 */
const STALE_HINT_MS = 30 * 60 * 1000

/**
 * 批次进度卡片：`?batch=<id>` 承载（评审 R-1——刷新可恢复），轮询至终态。
 * ai_skipped 分解徽标仅非零显示（评审 R-2）。
 */
export function BatchPanel({ batchId, onMissing }: { batchId: string; onMissing: () => void }) {
  const [startedAt] = useState(() => Date.now())
  const [stale, setStale] = useState(false)
  const { data: batch, loading, error, reload } = usePolling(
    () => api.getBatch(batchId),
    (current) => (current && ACTIVE_STATUSES.has(current.status) ? 2000 : false),
    [batchId],
  )

  // 轮询悬挂检测：活跃批次超过阈值仍未终态
  useEffect(() => {
    const timer = window.setInterval(() => setStale(Date.now() - startedAt > STALE_HINT_MS), 30_000)
    return () => window.clearInterval(timer)
  }, [startedAt])

  // 批次不存在（已删/无效 id）：静默清除 query（评审 R-1）
  useEffect(() => {
    if (error && 'status' in error && (error as { status?: number }).status === 404) onMissing()
  }, [error, onMissing])

  if (loading && !batch) {
    return (
      <section className="batch-panel glass-panel" aria-label="批次进度">
        <p className="muted">正在加载批次…</p>
      </section>
    )
  }

  return (
    <section className="batch-panel glass-panel" aria-label="批次进度">
      <div className="section-heading compact">
        <div>
          <span className="eyebrow">BATCH PROGRESS</span>
          <h2>批量扫描批次</h2>
          <p>
            批次 {batch?.id} · 创建于 {batch ? formatDate(batch.created_at) : '—'}
            {batch?.completed_at ? ` · 完成于 ${formatDate(batch.completed_at)}` : ''}
          </p>
        </div>
        <Button variant="ghost" onClick={() => void reload()} icon={<ClockCounterClockwise size={17} />}>刷新</Button>
      </div>

      {batch && (
        <div className="batch-body">
          <div className="batch-metrics">
            <span className="batch-metric"><small>状态</small><StatusBadge status={batch.status} /></span>
            <span className="batch-metric"><small>运行总数</small><strong>{batch.total_runs}</strong></span>
            <span className="batch-metric"><small>已完成</small><strong className="status-completed">{batch.completed_runs}</strong></span>
            <span className="batch-metric"><small>失败</small><strong className="status-failed">{batch.failed_runs}</strong></span>
          </div>

          {batch.ai_skipped > 0 && (
            <output className="batch-degrade">
              <Gauge size={17} weight="duotone" />
              <span>
                {batch.ai_skipped} 个 run 因批次预算/墙钟降级为仅确定性主链
                {batch.ai_skipped_by_budget > 0 && <em className="degrade-badge">预算 {batch.ai_skipped_by_budget}</em>}
                {batch.ai_skipped_by_wall_clock > 0 && <em className="degrade-badge">墙钟 {batch.ai_skipped_by_wall_clock}</em>}
              </span>
            </output>
          )}

          {stale && batch && ACTIVE_STATUSES.has(batch.status) && (
            <p className="form-error" role="alert">
              <WarningCircle size={15} /> 批次长时间未结束，后端可能异常（进程重启后批次状态不再推进）。
            </p>
          )}

          <ul className="batch-assets">
            {batch.assets.map((asset) => (
              <li key={asset.asset_id}>
                {batch.status === 'completed' || batch.completed_runs > 0 ? <CheckCircle size={14} weight="fill" className="status-completed" /> : <ClockCounterClockwise size={14} />}
                <strong>{asset.package_name}</strong>
                <small>{asset.apk_sha256.slice(0, 12)}…</small>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
