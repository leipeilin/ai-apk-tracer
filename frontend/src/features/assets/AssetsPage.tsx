import { ArrowRight, CheckCircle, Package, Plus, ShieldCheck, WarningCircle } from '@phosphor-icons/react'
import { motion, useReducedMotion } from 'framer-motion'
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError, api } from '../../lib/api'
import { formatDate } from '../../lib/format'
import { usePolling } from '../../lib/usePolling'
import type { Asset } from '../../lib/types'
import { StatusBadge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { EmptyState, ErrorState, SkeletonRows } from '../../ui/StateView'
import { BatchPanel } from './BatchPanel'
import { ImportAssetForm } from './ImportAssetForm'

/** 判定 503 ASSETS_DISABLED（功能未启用引导态，D3——与网络错误区分）。 */
function isAssetsDisabled(error: Error | null): boolean {
  return error instanceof ApiError && error.status === 503
}

/** 资产批量工作台：导入、多选发起批量扫描、批次进度（T1.5）。 */
export function AssetsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [showImport, setShowImport] = useState(searchParams.get('import') === '1')
  const batchId = searchParams.get('batch')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [batchAuthorized, setBatchAuthorized] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [creating, setCreating] = useState(false)
  const reduceMotion = useReducedMotion()

  const { data: assets, loading, error, reload } = usePolling(
    api.listAssets,
    (current) => (current?.some((asset) => asset.status === 'scanning') ? 2000 : false),
    [],
  )

  const stats = useMemo(() => {
    const list = assets || []
    return {
      total: list.length,
      ready: list.filter((item) => item.status === 'ready').length,
      scanning: list.filter((item) => item.status === 'scanning').length,
      failed: list.filter((item) => item.status === 'error').length,
    }
  }, [assets])

  const toggleSelect = (id: string) => {
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  const allSelected = !!assets && assets.length > 0 && selectedIds.length === assets.length

  const toggleSelectAll = () => {
    setSelectedIds(allSelected ? [] : (assets || []).map((asset) => asset.id))
  }

  const openImport = () => {
    setShowImport(true)
    setSearchParams((current) => { const next = new URLSearchParams(current); next.set('import', '1'); return next }, { replace: true })
  }

  const closeImport = () => {
    setShowImport(false)
    setSearchParams((current) => { const next = new URLSearchParams(current); next.delete('import'); return next }, { replace: true })
    void reload(true)
  }

  const launchBatch = async () => {
    if (selectedIds.length === 0 || !batchAuthorized) return
    setCreating(true)
    setBatchError('')
    try {
      const batch = await api.createBatch({ authorized: true, assetIds: selectedIds })
      // 评审 R-3：发起成功清空选中 + 重置授权（每动作独立授权）
      setSelectedIds([])
      setBatchAuthorized(false)
      // 评审 R-1：批次上下文进 URL（刷新可恢复）
      setSearchParams({ batch: batch.id }, { replace: true })
    } catch (value) {
      setBatchError(value instanceof Error ? value.message : '发起批量扫描失败')
    } finally {
      setCreating(false)
    }
  }

  const clearBatch = () => {
    setSearchParams((current) => { const next = new URLSearchParams(current); next.delete('batch'); return next }, { replace: true })
  }

  return (
    <div className="page-stack">
      <section className="page-hero">
        <div className="hero-copy">
          <div className="system-line"><span className="health-dot online" />资产批量扫描</div>
          <h1>资产库与<span>批量扫描</span></h1>
          <p>导入 APK 建立资产库，按任意子集发起批量扫描；批次预算耗尽时自动降级为仅确定性主链并留痕可审计。</p>
          <Button variant="primary" onClick={openImport} icon={<Plus size={18} />}>导入资产</Button>
        </div>
        <div className="hero-metrics glass-panel" aria-label="资产概览">
          <div className="metric-feature"><span>资产总数</span><strong>{stats.total.toString().padStart(2, '0')}</strong><small>本地 APK 注册库</small></div>
          <div><span>就绪</span><strong>{stats.ready}</strong></div>
          <div><span>扫描中</span><strong>{stats.scanning}</strong></div>
          <div><span>异常</span><strong>{stats.failed}</strong></div>
        </div>
      </section>

      {showImport && (
        <motion.div initial={reduceMotion ? false : { opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
          <ImportAssetForm onClose={closeImport} />
        </motion.div>
      )}

      {batchId && <BatchPanel batchId={batchId} onMissing={clearBatch} />}

      <section className="runs-section">
        <div className="section-heading">
          <div><span className="eyebrow">ASSET LIBRARY</span><h2>资产列表</h2><p>扫描中的资产每 2 秒自动同步状态；勾选资产后可发起批量扫描。</p></div>
          <Button variant="ghost" onClick={() => void reload()} icon={<ShieldCheck size={17} />}>刷新</Button>
        </div>

        {loading && !assets && <SkeletonRows count={3} />}
        {error && !assets && isAssetsDisabled(error) && (
          <EmptyState
            icon={<Package size={28} />}
            title="资产批量功能未启用"
            description="服务端配置 assets.enabled=false。启用后可导入 APK 资产并发起批量扫描（需在服务端配置中开启后重启）。"
          />
        )}
        {error && !assets && !isAssetsDisabled(error) && (
          <ErrorState error={error} onRetry={() => void reload()} />
        )}
        {!loading && !error && assets?.length === 0 && (
          <EmptyState
            icon={<Package size={28} />}
            title="还没有资产"
            description="导入第一个已授权的 APK 样本，建立可复用的批量扫描资产库。"
            action={<Button variant="primary" onClick={openImport} icon={<Plus size={17} />}>导入首个资产</Button>}
          />
        )}

        {assets && assets.length > 0 && (
          <>
            <div className="batch-launcher glass-panel" aria-label="批量操作">
              <label className="select-all">
                <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} />
                <span>全选（{assets.length}）</span>
              </label>
              <span className="launcher-count">已选 <strong>{selectedIds.length}</strong> 项</span>
              <label className="authorization-note compact">
                <input
                  type="checkbox"
                  checked={batchAuthorized}
                  disabled={selectedIds.length === 0}
                  onChange={(event) => setBatchAuthorized(event.target.checked)}
                />
                <span><strong>确认授权批量扫描</strong></span>
              </label>
              <Button
                variant="primary"
                loading={creating}
                disabled={selectedIds.length === 0 || !batchAuthorized}
                onClick={launchBatch}
                icon={<ShieldCheck size={18} />}
              >
                {creating ? '正在发起' : '发起批量扫描'}
              </Button>
            </div>
            {batchError && <p className="form-error" role="alert">{batchError}</p>}

            <div className="asset-list">
              {assets.map((asset: Asset, index: number) => {
                const selected = selectedIds.includes(asset.id)
                return (
                  <motion.div key={asset.id} initial={reduceMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: reduceMotion ? 0 : Math.min(index * 0.04, 0.2) }}>
                    <div className={`asset-row ${selected ? 'selected' : ''}`}>
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleSelect(asset.id)}
                        aria-label={`选择资产 ${asset.package_name}`}
                      />
                      <span className={`asset-icon asset-${asset.status}`}>
                        {asset.status === 'ready' ? <CheckCircle size={20} weight="duotone" /> : asset.status === 'error' ? <WarningCircle size={20} weight="duotone" /> : <ShieldCheck size={20} weight="duotone" />}
                      </span>
                      <span className="asset-main">
                        <strong>{asset.package_name}</strong>
                        <small>{asset.apk_filename} · sha256 {asset.apk_sha256.slice(0, 12)}…</small>
                      </span>
                      <span className="asset-meta"><small>导入时间</small><strong>{formatDate(asset.created_at)}</strong></span>
                      <span className="asset-meta"><small>最近任务</small>
                        {asset.last_run_id ? (
                          <Link to={`/runs/${asset.last_run_id}`} className="asset-run-link">查看 <ArrowRight size={13} /></Link>
                        ) : <strong>—</strong>}
                      </span>
                      <StatusBadge status={asset.status} />
                    </div>
                  </motion.div>
                )
              })}
            </div>
          </>
        )}
      </section>
    </div>
  )
}
