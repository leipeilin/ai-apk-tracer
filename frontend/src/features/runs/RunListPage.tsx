import { ArrowRight, CheckCircle, ClockCounterClockwise, Package, Plus, ShieldCheck, WarningCircle } from '@phosphor-icons/react'
import { motion, useReducedMotion } from 'framer-motion'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatDate, isRunActive, runName } from '../../lib/format'
import { usePolling } from '../../lib/usePolling'
import type { AnalysisRun } from '../../lib/types'
import { StatusBadge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { EmptyState, ErrorState, SkeletonRows } from '../../ui/StateView'
import { CreateRunForm } from './CreateRunForm'

/** 展示本地扫描任务、服务健康状态和任务创建入口。 */
export function RunListPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [showCreate, setShowCreate] = useState(searchParams.get('create') === '1')
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const reduceMotion = useReducedMotion()
  const { data: visibleRuns, loading, error, reload } = usePolling(
    api.listRuns,
    (current) => current?.some((run) => isRunActive(run.status)) ? 2000 : false,
    [],
  )

  useEffect(() => {
    let active = true
    api.health().then(() => active && setHealth('online')).catch(() => active && setHealth('offline'))
    return () => { active = false }
  }, [])

  useEffect(() => {
    setShowCreate(searchParams.get('create') === '1')
  }, [searchParams])

  const closeCreate = () => {
    setShowCreate(false)
    setSearchParams({}, { replace: true })
  }

  const stats = useMemo(() => {
    const list = visibleRuns || []
    return {
      total: list.length,
      active: list.filter((item) => isRunActive(item.status)).length,
      completed: list.filter((item) => item.status === 'completed').length,
      findings: list.reduce((sum, item) => sum + (item.findings_count || 0), 0),
    }
  }, [visibleRuns])

  return (
    <div className="page-stack">
      <section className="page-hero">
        <div className="hero-copy">
          <div className="system-line"><span className={`health-dot ${health}`} />API {health === 'online' ? '已连接' : health === 'offline' ? '不可用' : '检测中'}</div>
          <h1>把每一条漏洞结论，<br /><span>落到可验证的证据上。</span></h1>
          <p>面向 Android 应用的静态安全分析工作台。集中管理样本、分析阶段、证据复核与可交付报告。</p>
          <Button variant="primary" onClick={() => setSearchParams({ create: '1' })} icon={<Plus size={18} />}>创建分析任务</Button>
        </div>
        <div className="hero-metrics glass-panel" aria-label="任务概览">
          <div className="metric-feature"><span>累计安全发现</span><strong>{stats.findings.toString().padStart(2, '0')}</strong><small>跨全部分析任务</small></div>
          <div><span>任务总数</span><strong>{stats.total}</strong></div>
          <div><span>运行中</span><strong>{stats.active}</strong></div>
          <div><span>已完成</span><strong>{stats.completed}</strong></div>
        </div>
      </section>

      {showCreate && (
        <motion.div initial={reduceMotion ? false : { opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
          <CreateRunForm onClose={closeCreate} />
        </motion.div>
      )}

      <section className="runs-section">
        <div className="section-heading">
          <div><span className="eyebrow">ANALYSIS RUNS</span><h2>分析任务</h2><p>运行中的任务每 2 秒自动同步状态。</p></div>
          <Button variant="ghost" onClick={() => void reload()} icon={<ClockCounterClockwise size={17} />}>刷新</Button>
        </div>

        {loading && !visibleRuns && <SkeletonRows count={4} />}
        {error && !visibleRuns && <ErrorState error={error} onRetry={() => void reload()} />}
        {!loading && visibleRuns?.length === 0 && (
          <EmptyState icon={<Package size={28} />} title="还没有分析任务" description="提交一个已授权的 APK 样本，开始建立第一条安全证据链。" action={<Button variant="primary" onClick={() => setSearchParams({ create: '1' })} icon={<Plus size={17} />}>创建首个任务</Button>} />
        )}
        {visibleRuns && visibleRuns.length > 0 && (
          <div className="run-list">
            {visibleRuns.map((run: AnalysisRun, index: number) => (
              <motion.div key={run.id} initial={reduceMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: reduceMotion ? 0 : Math.min(index * 0.05, 0.25) }}>
                <Link to={`/runs/${run.id}`} className="run-row">
                  <span className={`run-icon run-${run.status}`}>
                    {run.status === 'completed' ? <CheckCircle size={21} weight="duotone" /> : run.status === 'failed' ? <WarningCircle size={21} weight="duotone" /> : <ShieldCheck size={21} weight="duotone" />}
                  </span>
                  <span className="run-main"><strong>{runName(run)}</strong><small>{run.package_name || `ID ${run.id}`}</small></span>
                  <span className="run-meta"><small>发现</small><strong>{run.findings_count ?? '—'}</strong></span>
                  <span className="run-meta"><small>创建时间</small><strong>{formatDate(run.created_at)}</strong></span>
                  <StatusBadge status={run.status} />
                  <ArrowRight size={18} />
                </Link>
              </motion.div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
