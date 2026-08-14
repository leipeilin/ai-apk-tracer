import type { ReactNode } from 'react'
import { normalizeSeverity, severityMeta, statusLabel } from '../lib/format'

export function SeverityBadge({ severity }: { severity?: string }) {
  const value = normalizeSeverity(severity)
  return <span className={`severity severity-${value}`}>{severityMeta[value].label}</span>
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`}>
      <span className="status-dot" aria-hidden />
      {statusLabel[status] || status}
    </span>
  )
}

export function DataBadge({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="data-badge">
      <span>{label}</span>
      <strong>{children}</strong>
    </span>
  )
}

export const reviewLabel: Record<string, string> = {
  pending_ai: '待 AI 复核',
  pending_manual: '待人工复核',
  ai_false_positive: 'AI 反驳（有确定性依据）',
  manual_false_positive: '人工确认误报',
  confirmed: '人工已确认',
  pending: '待复核（旧）',
  false_positive: '误报（旧）',
  ai_candidate: '待 AI 复核（旧）',
}
