import { Check, Circle, CircleNotch, Warning } from '@phosphor-icons/react'
import { motion, useReducedMotion } from 'framer-motion'
import { fallbackStages, formatDate, formatDuration } from '../../lib/format'
import type { AnalysisRun, RunStage } from '../../lib/types'

const stageLabel: Record<string, string> = {
  basic_check: '基础检查',
  decompiling: '反编译',
  rule_prescan: '规则预筛选',
  candidate_funnel: '候选漏斗与精确去重',
  code_slicing: '代码切片',
  ai_analysis: 'AI 语义复核',
  evidence_integrity_validation: '证据完整性校验',
  evidence_validation: '证据校验（旧）',
  aggregation: '发现聚合',
}

/** 将 run manifest 中的阶段状态、耗时和覆盖说明渲染为可追溯时间线。 */
export function StageTimeline({ run }: { run: AnalysisRun }) {
  const stages = run.stages?.length ? run.stages : fallbackStages()
  const reduceMotion = useReducedMotion()
  const ai = run.config?.ai
  const aiStage = stages.find((stage) => stage.name === 'ai_analysis')
  const provider = ai?.provider_kind || '未记录'
  const aiStatus = !ai
    ? '旧任务未记录配置快照'
    : ai.enabled === undefined
      ? '启用状态未记录'
      : ai.enabled === false
        ? '未启用，不外发代码'
        : ai.allow_external_code === undefined
          ? '已启用，代码外发授权未记录'
          : ai.allow_external_code === false
            ? '已启用，但禁止外发代码切片'
            : `已启用并允许外发代码切片${aiStage ? `；阶段状态 ${aiStage.status}` : ''}`
  return (
    <section className="timeline-section">
      <div className="section-heading compact">
        <div><span className="eyebrow">PIPELINE</span><h2>分析阶段</h2></div>
        <span className="mono-label">{stages.filter((stage) => stage.status === 'completed').length}/{stages.length} DONE</span>
      </div>
      <div className="integrity-alert" role="note">
        <Warning size={18} weight="fill" />
        <div>
          <strong>AI 外发与隐私</strong>
          <p>默认配置开启 AI 且允许向外部模型服务发送方法级代码切片。当前任务：{aiStatus}。Provider：{ai ? provider : '未记录'}；Model：{ai?.model || '未记录'}。界面不会显示服务地址或密钥。</p>
        </div>
      </div>
      <ol className="timeline-list">
        {stages.map((stage, index) => (
          <motion.li
            key={stage.id || `${stage.name}-${index}`}
            className={`timeline-item timeline-${stage.status}`}
            initial={reduceMotion ? false : { opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: reduceMotion ? 0 : index * 0.07 }}
          >
            <span className="timeline-node">
              {stage.status === 'completed' && <Check size={14} weight="bold" />}
              {stage.status === 'running' && <CircleNotch size={15} className="animate-spin" />}
              {(stage.status === 'failed' || stage.status === 'partial') && <Warning size={14} weight="fill" />}
              {(stage.status === 'pending' || stage.status === 'skipped' || stage.status === 'unknown') && <Circle size={11} weight="fill" />}
            </span>
            <div className="timeline-copy">
              <div><strong>{stageLabel[stage.name] || stage.name}</strong><span>{formatDuration(stage.duration_ms)}</span></div>
              <p>{stage.message || stageSummary(stage)}</p>
              {(stage.started_at || stage.completed_at || stage.ended_at) && <small>{formatDate(stage.started_at)}{(stage.completed_at || stage.ended_at) ? ` → ${formatDate(stage.completed_at || stage.ended_at)}` : ''}</small>}
            </div>
          </motion.li>
        ))}
      </ol>
    </section>
  )
}

function stageSummary(stage: RunStage): string {
  const summary = stage.summary || {}
  if (stage.name === 'decompiling') {
    return `DEX 反编译伪源码 ${summaryValue(summary, 'source_file_count')} 个，错误 ${summaryValue(summary, 'error_count')} 个`
  }
  if (stage.name === 'rule_prescan') {
    return `规则失败 ${arrayCount(summary.rule_failures)}/${summaryValue(summary, 'rule_total_count')}，组件缺口 ${arrayCount(summary.component_coverage_gaps)}，候选 ${summaryValue(summary, 'candidate_count')} 个`
  }
  if (stage.name === 'candidate_funnel') {
    return [
      `候选 ${summaryValue(summary, 'candidate_count')}`,
      `精确去重 ${summaryValue(summary, 'deduplicated_count')}`,
      `AI 代表 ${summaryValue(summary, 'ai_representative_count')}`,
      `L1 selected ${summaryValue(summary, 'l1_ai_selected_count', 'selected')}`,
      `deferred ${summaryValue(summary, 'l1_ai_deferred_count', 'deferred')}`,
    ].join('，')
  }
  if (stage.name === 'ai_analysis') {
    const preflight = asRecord(summary.preflight)
    if (preflight && preflight.status !== 'passed') {
      return `预检 ${String(preflight.classification || 'failed')} · ${String(summary.reason || preflight.message || 'AI 服务不可用')}`
    }
    if (stage.status === 'skipped') return String(summary.reason || '按当前配置跳过')
    const stopSummary = summary.stop_reason ?? summary.circuit_reason ?? summary.final_stop_reason
    const parts = [
      `完成 ${summaryValue(summary, 'completed', 'analyzed')}`,
      `失败 ${summaryValue(summary, 'failed')}`,
      `未完成 ${summaryValue(summary, 'incomplete')}`,
      `峰值并发 ${summaryValue(summary, 'peak_concurrent', 'peak_concurrency')}`,
      `cache ${summaryValue(summary, 'cache_hits', 'cache_hit_count', 'cache_hit')}`,
    ]
    if (stopSummary) parts.push(`stop ${String(stopSummary)}`)
    const l1 = asRecord(summary.l1_triage)
    const l2 = asRecord(summary.l2_review)
    if (l1) parts.push(`L1 selected ${summaryValue(l1, 'selected', 'eligible')} / deferred ${summaryValue(l1, 'deferred')}`)
    if (l2) parts.push(`L2 ${summaryValue(l2, 'completed', 'analyzed')}`)
    return parts.join('，')
  }
  if (stage.name === 'evidence_integrity_validation') {
    return `候选 ${summaryValue(summary, 'candidates_checked')}，闭合链 ${summaryValue(summary, 'deterministic_chains_closed')}，可定级 ${summaryValue(summary, 'gradeable_findings')}，待复核 ${summaryValue(summary, 'findings_pending_review')}`
  }
  if (stage.status === 'running') return '分析引擎正在处理当前阶段…'
  if (stage.status === 'pending') return '等待上游阶段完成'
  if (stage.status === 'skipped') return '按当前配置跳过'
  if (stage.status === 'partial') return '部分完成，存在覆盖缺口'
  if (stage.status === 'failed') return '此阶段未能完成'
  const entries = Object.entries(summary).filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
  if (entries.length) return entries.slice(0, 4).map(([key, value]) => `${key} ${String(value)}`).join('，')
  return '未汇总'
}

function summaryValue(summary: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    if (summary[key] !== undefined && summary[key] !== null) return String(summary[key])
  }
  return '未汇总'
}

function arrayCount(value: unknown): string {
  return Array.isArray(value) ? String(value.length) : '未汇总'
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}
