import { Compass, Target } from '@phosphor-icons/react'
import type { ReactNode } from 'react'
import type { RunProgress } from '../../lib/types'

export type TrackId = 'rules' | 'explorer'

function fmt(value: number | null): string {
  return typeof value === 'number' ? String(value) : '—'
}

function ratio(done: number | null, total: number | null): number | null {
  return typeof done === 'number' && typeof total === 'number' && total > 0
    ? Math.min(done / total, 1)
    : null
}

interface ProgressShellProps {
  icon: ReactNode
  label: string
  ratio: number | null
  stats: Array<{ label: string; value: string }>
}

function ProgressShell({ icon, label, ratio: fill, stats }: ProgressShellProps) {
  return (
    <div className="track-progress glass-panel" role="status" aria-label={`${label}进度`}>
      <div className="track-progress-head">
        <span className="track-progress-label">{icon}{label}</span>
        <span className="track-progress-stats">
          {stats.map((stat) => (
            <span key={stat.label}><strong>{stat.value}</strong> {stat.label}</span>
          ))}
        </span>
      </div>
      {fill !== null && <div className="progress-track"><span style={{ transform: `scaleX(${fill})` }} /></div>}
    </div>
  )
}

/**
 * 双轨运行反馈（track-progress-console）：总/已完成/未完成计数 + 进度条。
 * 字段缺失（历史 run 无产物/轨未启用）显示"—"，不伪造 0；运行中每 2s
 * 随 getRun 轮询自动刷新——规则轨终态"已完成 = processed - failed"，
 * 探索轨运行中"已探索"为 partial jsonl 近似值（终态被 summary 覆盖）。
 */
export function TrackProgress({ track, progress }: { track: TrackId; progress: RunProgress | null }) {
  if (track === 'rules') {
    const rules = progress?.rules ?? null
    if (!rules) return <TrackProgressUnavailable icon={<Target size={15} />} label="规则任务" note="规则进度未记录" />
    const failed = typeof rules.failed === 'number' && rules.failed > 0 ? rules.failed : null
    const completed = typeof rules.processed === 'number'
      ? Math.max(rules.processed - (rules.failed ?? 0), 0)
      : null
    const remaining = typeof rules.total === 'number' && typeof rules.processed === 'number'
      ? Math.max(rules.total - rules.processed, 0)
      : null
    return (
      <ProgressShell
        icon={<Target size={15} />}
        label="规则任务"
        ratio={ratio(rules.processed, rules.total)}
        stats={[
          { label: '总任务', value: fmt(rules.total) },
          { label: '已完成', value: fmt(completed) },
          { label: '未完成', value: fmt(remaining) },
          ...(failed !== null ? [{ label: '失败', value: String(failed) }] : []),
        ]}
      />
    )
  }
  const explorer = progress?.explorer ?? null
  if (!explorer) return <TrackProgressUnavailable icon={<Compass size={15} />} label="攻击面探索" note="探索轨未启用或未记录" />
  return (
    <ProgressShell
      icon={<Compass size={15} />}
      label="攻击面探索"
      ratio={ratio(explorer.explored, explorer.total)}
      stats={[
        { label: '总攻击面', value: fmt(explorer.total) },
        { label: '已探索', value: fmt(explorer.explored) },
        { label: '未探索', value: fmt(explorer.unexplored) },
      ]}
    />
  )
}

function TrackProgressUnavailable({ icon, label, note }: { icon: ReactNode; label: string; note: string }) {
  return (
    <div className="track-progress glass-panel" role="status" aria-label={`${label}进度`}>
      <div className="track-progress-head">
        <span className="track-progress-label">{icon}{label}</span>
        <span className="track-progress-note">{note}</span>
      </div>
    </div>
  )
}
