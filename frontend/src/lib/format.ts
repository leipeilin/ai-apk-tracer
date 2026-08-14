import type { AnalysisRun, RunStage, Severity } from './types'

export const severityMeta: Record<Severity, { label: string; rank: number }> = {
  critical: { label: '严重', rank: 0 },
  high: { label: '高危', rank: 1 },
  medium: { label: '中危', rank: 2 },
  low: { label: '低危', rank: 3 },
  informational: { label: '提示', rank: 4 },
  pending: { label: '待定', rank: 5 },
}

export function normalizeSeverity(value?: string): Severity {
  const key = value?.toLowerCase()
  // severe/moderate/info 是旧产物的等级别名，仅在展示层归一化。
  if (key === 'critical' || key === 'severe') return 'critical'
  if (key === 'high') return 'high'
  if (key === 'medium' || key === 'moderate') return 'medium'
  if (key === 'low') return 'low'
  if (key === 'info' || key === 'informational') return 'informational'
  return 'pending'
}

export function runName(run: AnalysisRun) {
  return run.app_name || run.file_name || run.filename || run.package_name || `任务 ${run.id.slice(0, 8)}`
}

export function formatDate(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatBytes(value?: number) {
  if (value == null || Number.isNaN(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let current = value
  let unit = 0
  while (current >= 1024 && unit < units.length - 1) {
    current /= 1024
    unit += 1
  }
  return `${current.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function formatDuration(ms?: number | null) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function isRunActive(status: string) {
  // 仅明确的进行中状态允许轮询，未知或未来新增状态不会造成无限请求。
  return ['queued', 'pending', 'uploading', 'analyzing', 'running'].includes(status)
}

export const statusLabel: Record<string, string> = {
  queued: '排队中',
  pending: '等待中',
  uploading: '上传中',
  analyzing: '分析中',
  running: '分析中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

// 旧任务没有 stages 时不能由 run 终态倒推出各阶段结果，只展示一个明确的兼容占位。
export function fallbackStages(): RunStage[] {
  return [{ name: '阶段记录', status: 'unknown', message: '旧任务未记录阶段时间线' }]
}
