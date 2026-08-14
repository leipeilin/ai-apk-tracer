import { CheckCircle, FileArrowUp, ShieldCheck, X } from '@phosphor-icons/react'
import { useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import { Button } from '../../ui/Button'

/**
 * 收集已授权 APK、反编译开关并创建扫描任务；上传阶段通过 XHR 展示字节级进度。
 */
export function CreateRunForm({ onClose }: { onClose?: () => void }) {
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [sourceAnalysis, setSourceAnalysis] = useState(true)
  const [authorized, setAuthorized] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [progress, setProgress] = useState(0)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const selectFile = (next?: File) => {
    if (!next) return
    setError('')
    if (!next.name.toLowerCase().endsWith('.apk')) {
      setError('请选择 .apk 安装包')
      return
    }
    setFile(next)
  }

  const onDrop = (event: DragEvent) => {
    event.preventDefault()
    setDragging(false)
    selectFile(event.dataTransfer.files[0])
  }

  const submit = async () => {
    if (!file) {
      setError('请先选择待分析的 APK 文件')
      return
    }
    if (!authorized) {
      setError('请先确认你有权对该 APK 执行安全分析')
      return
    }
    setSubmitting(true)
    setError('')
    setProgress(1)
    try {
      // 上传成功后直接进入任务详情，后续阶段状态由详情页轮询更新。
      const run = await api.createRun(
        { file, authorized, sourceAnalysisEnabled: sourceAnalysis },
        ({ percent }) => setProgress(percent),
      )
      setProgress(100)
      navigate(`/runs/${run.id}`)
    } catch (value) {
      setError(value instanceof Error ? value.message : '创建任务失败')
      setSubmitting(false)
      setProgress(0)
    }
  }

  return (
    <section className="create-panel glass-panel" aria-label="创建分析任务">
      <div className="section-heading compact">
        <div>
          <span className="eyebrow">AUTHORIZED ANALYSIS</span>
          <h2>提交 APK 样本</h2>
          <p>上传后将自动完成解包、静态分析、证据归并与报告生成。</p>
        </div>
        {onClose && <Button variant="ghost" className="icon-button" onClick={onClose} aria-label="关闭创建面板"><X size={19} /></Button>}
      </div>

      <button
        type="button"
        className={`drop-zone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          accept=".apk,application/vnd.android.package-archive"
          onChange={(event: ChangeEvent<HTMLInputElement>) => selectFile(event.target.files?.[0])}
        />
        <span className="drop-icon">{file ? <CheckCircle size={27} weight="fill" /> : <FileArrowUp size={27} />}</span>
        {file ? (
          <span className="drop-copy"><strong>{file.name}</strong><small>{formatBytes(file.size)} · 点击重新选择</small></span>
        ) : (
          <span className="drop-copy"><strong>拖放 APK，或点击浏览</strong><small>仅接受 Android APK 安装包</small></span>
        )}
      </button>

      <div className="create-options">
        <label className="switch-row">
          <span><strong>启用反编译代码分析</strong><small>对 DEX 伪源码执行数据流与调用链检查</small></span>
          <input type="checkbox" checked={sourceAnalysis} onChange={(event) => setSourceAnalysis(event.target.checked)} />
          <span className="switch" aria-hidden />
        </label>
        <label className="authorization-note">
          <input
            type="checkbox"
            checked={authorized}
            onChange={(event) => setAuthorized(event.target.checked)}
          />
          <ShieldCheck size={20} weight="duotone" />
          <span><strong>确认合法测试授权</strong><small>我确认有权对该 APK 执行安全分析并承担相应责任。</small></span>
        </label>
      </div>

      {submitting && (
        <div className="upload-progress" aria-live="polite">
          <div><span>{progress < 100 ? '正在安全上传' : '任务已创建'}</span><strong>{progress}%</strong></div>
          <div className="progress-track"><span style={{ transform: `scaleX(${progress / 100})` }} /></div>
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="form-actions">
        <Button variant="primary" loading={submitting} disabled={!file || !authorized} onClick={submit} icon={<ShieldCheck size={18} />}>
          {submitting ? '正在提交' : '开始安全分析'}
        </Button>
        {onClose && <Button variant="ghost" onClick={onClose}>取消</Button>}
      </div>
    </section>
  )
}
