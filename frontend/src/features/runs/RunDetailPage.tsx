import { ArrowLeft, Clock, Compass, File, Fingerprint, ShieldCheck, Target, Warning } from '@phosphor-icons/react'
import { Link, useParams } from 'react-router-dom'
import { useState } from 'react'
import { api } from '../../lib/api'
import { formatBytes, formatDate, isRunActive, runName } from '../../lib/format'
import { usePolling } from '../../lib/usePolling'
import type { Finding } from '../../lib/types'
import { StatusBadge } from '../../ui/Badge'
import { ErrorState, SkeletonRows } from '../../ui/StateView'
import { CleanupPanel } from '../cleanup/CleanupPanel'
import { FindingsPanel } from '../findings/FindingsPanel'
import { ExplorerQueuePanel } from './ExplorerQueuePanel'
import { StageTimeline } from './StageTimeline'
import { TrackProgress, type TrackId } from './TrackProgress'

/**
 * 展示单次扫描的双轨运行情况（规则轨发现 / 探索轨人工队列，分段按钮切换——
 * track-progress-console）、双轨进度反馈与清理能力；仅在任务活跃时轮询，
 * 完成后自动停止。
 */

function trackBadge(done: number | null | undefined, total: number | null | undefined): string {
  const doneText = typeof done === 'number' ? String(done) : '—'
  const totalText = typeof total === 'number' ? String(total) : '—'
  return `${doneText}/${totalText}`
}

export function RunDetailPage() {
  const { id = '' } = useParams()
  const [track, setTrack] = useState<TrackId>('rules')
  const runState = usePolling(
    () => api.getRun(id),
    (current) => current && isRunActive(current.status) ? 2000 : false,
    [id],
  )
  const active = runState.data ? isRunActive(runState.data.status) : true
  // 锁/并发优化（2026-08-15）：findings 轮询在 run 数据就绪后才启动，且与
  // getRun 共用同一活跃判定——避免扫描运行中两个 2s 轮询并发压测 SQLite 读锁
  // （页面卡顿根因之一）。run 未返回时不发起 findings 请求（findings_count 已在
  // getRun 响应中，hero 区数字不受影响）。
  const findingState = usePolling(
    () => api.getFindings(id),
    runState.data ? (active ? 2000 : false) : false,
    [id],
  )
  // T2.10：探索人工队列轮询——与 findings 同一活跃判定（评审 R-4：
  // explorer/candidates.json 在探索阶段末落盘，进行中轮询以捕捉产出）。
  const explorerQueueState = usePolling(
    () => api.getExplorerCandidates(id),
    runState.data ? (active ? 2000 : false) : false,
    [id],
  )

  if (runState.loading && !runState.data) return <div className="detail-loading"><SkeletonRows count={5} /></div>
  if (runState.error && !runState.data) return <ErrorState error={runState.error} onRetry={() => void runState.reload()} />
  if (!runState.data) return null
  const run = runState.data
  const coverageGaps = run.manifest?.coverage_gaps || []
  // run 级 gap 描述任务覆盖边界，不表示该任务的每一条 finding 都被阻断。
  const analysisIncomplete = run.manifest?.analysis_incomplete === true || coverageGaps.length > 0

  return (
    <div className="page-stack detail-page">
      <div className="detail-nav"><Link to="/"><ArrowLeft size={17} />返回任务列表</Link><span>任务 ID · {run.id}</span></div>
      <section className="detail-hero">
        <div>
          <div className="detail-title-line"><StatusBadge status={run.status} /><span>{active ? '状态每 2 秒自动同步' : '分析流程已结束'}</span></div>
          <h1>{runName(run)}</h1>
          <p>{run.package_name || '尚未识别应用包名'}</p>
        </div>
        <dl className="detail-facts glass-panel">
          <div><dt><File size={16} />样本大小</dt><dd>{formatBytes(run.file_size)}</dd></div>
          <div><dt><Clock size={16} />创建时间</dt><dd>{formatDate(run.created_at)}</dd></div>
          <div><dt><Fingerprint size={16} />源码分析</dt><dd>{run.source_analysis_enabled === false ? '未启用' : '已启用'}</dd></div>
          <div><dt><ShieldCheck size={16} />发现数量</dt><dd>{findingState.data?.length ?? run.findings_count ?? '—'}</dd></div>
        </dl>
      </section>

      {run.error && <div className="run-error"><strong>分析异常</strong><p>{run.error}</p></div>}
      {analysisIncomplete && (
        <section className="integrity-alert" role="status" aria-live="polite">
          <Warning size={20} weight="fill" />
          <div>
            <strong>本次扫描存在覆盖缺口</strong>
            <p>当前结果不能解释为“未发现漏洞”。请结合失败阶段、反编译缺口和人工复核判断。</p>
            {coverageGaps.length > 0 && (
              <ul>
                {coverageGaps.map((gap, index) => (
                  <li key={index}>{formatGap(gap)}</li>
                ))}
              </ul>
            )}
          </div>
        </section>
      )}

      <div className="detail-grid-layout">
        <aside className="detail-aside glass-panel">
          <StageTimeline run={run} />
          <CleanupPanel runId={run.id} onCleaned={() => void runState.reload(true)} />
        </aside>
        <main className="detail-findings">
          <div className="track-switcher glass-panel" role="tablist" aria-label="运行轨切换">
            <button
              type="button"
              role="tab"
              id="track-tab-rules"
              aria-selected={track === 'rules'}
              aria-controls="track-panel"
              className={track === 'rules' ? 'track-tab active' : 'track-tab'}
              onClick={() => setTrack('rules')}
            >
              <Target size={15} />规则轨
              <span className="track-tab-count">
                {trackBadge(run.progress?.rules?.processed, run.progress?.rules?.total)}
              </span>
            </button>
            <button
              type="button"
              role="tab"
              id="track-tab-explorer"
              aria-selected={track === 'explorer'}
              aria-controls="track-panel"
              className={track === 'explorer' ? 'track-tab active' : 'track-tab'}
              onClick={() => setTrack('explorer')}
            >
              <Compass size={15} />探索轨
              <span className="track-tab-count">
                {trackBadge(run.progress?.explorer?.explored, run.progress?.explorer?.total)}
              </span>
            </button>
          </div>
          <TrackProgress track={track} progress={run.progress ?? null} />
          <div id="track-panel" role="tabpanel" aria-labelledby={track === 'rules' ? 'track-tab-rules' : 'track-tab-explorer'}>
            {track === 'rules' ? (
              <>
                {findingState.loading && !findingState.data && <SkeletonRows count={5} />}
                {findingState.error && !findingState.data && <ErrorState error={findingState.error} onRetry={() => void findingState.reload()} />}
                {findingState.data && <FindingsPanel findings={findingState.data} onChange={(findings: Finding[]) => findingState.setData(findings)} />}
              </>
            ) : (
              <ExplorerQueuePanel queue={explorerQueueState.data} />
            )}
          </div>
        </main>
      </div>
    </div>
  )
}

function formatGap(gap: Record<string, unknown> | string): string {
  if (typeof gap === 'string') return gap
  const code = String(gap.code || 'COVERAGE_GAP')
  const message = typeof gap.message === 'string' ? gap.message : ''
  return message ? `${code} · ${message}` : code
}
