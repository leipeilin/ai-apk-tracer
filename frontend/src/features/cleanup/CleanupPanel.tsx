import { Broom, Code, Trash, Warning } from '@phosphor-icons/react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import type { CleanupMode } from '../../lib/types'
import { Button } from '../../ui/Button'

const modes: Array<{ value: CleanupMode; title: string; description: string; icon: typeof Broom }> = [
  { value: 'prune_intermediates', title: '精简中间产物', description: '删除 APK 副本、反编译文件、切片和缓存；保留发现、报告及自包含证据。', icon: Broom },
  { value: 'clear_sensitive_content', title: '清除敏感内容', description: '删除代码、模型缓存、发现和报告，仅保留脱敏任务摘要。', icon: Code },
  { value: 'delete_run', title: '完全删除任务', description: '永久删除当前 run_id 的全部本地数据，不影响原始 APK。', icon: Trash },
]

/**
 * 执行单任务三种清理策略；完全删除前进行明确确认，并在删除后返回任务列表。
 */
export function CleanupPanel({ runId, onCleaned }: { runId: string; onCleaned?: () => void }) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<CleanupMode>('prune_intermediates')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const cleanup = async () => {
    if (selected === 'delete_run' && !window.confirm('确认完全删除当前任务？此操作不可撤销。')) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api.cleanupRun(runId, selected)
      setMessage(`清理状态：${result.status || 'completed'}`)
      if (selected === 'delete_run') navigate('/')
      else onCleaned?.()
    } catch (value) {
      setError(value instanceof Error ? value.message : '清理失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="cleanup-panel">
      <div className="section-heading compact"><div><span className="eyebrow">DATA HYGIENE</span><h2>任务清理</h2><p>三种模式边界明确，完全删除不可撤销。</p></div></div>
      <div className="cleanup-options">
        {modes.map(({ value, title, description, icon: Icon }) => (
          <label key={value} className={`cleanup-option ${selected === value ? 'selected' : ''}`}>
            <input type="radio" name="cleanup" value={value} checked={selected === value} onChange={() => { setSelected(value); setMessage(''); setError('') }} />
            <Icon size={20} weight={selected === value ? 'duotone' : 'regular'} />
            <span><strong>{title}</strong><small>{description}</small></span>
          </label>
        ))}
      </div>
      {selected === 'prune_intermediates' && <p className="danger-note"><Warning size={17} />保留的报告证据仍可能包含敏感代码。</p>}
      {selected === 'delete_run' && <p className="danger-note"><Warning size={17} weight="fill" />此操作会永久移除当前任务的全部分析数据。</p>}
      {message && <p className="success-note" aria-live="polite">{message}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <Button variant={selected === 'delete_run' ? 'danger' : 'secondary'} loading={loading} onClick={cleanup} icon={<Broom size={17} />}>
        执行清理
      </Button>
    </section>
  )
}
