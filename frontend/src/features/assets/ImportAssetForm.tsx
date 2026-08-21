import { CheckCircle, FileArrowUp, Package, ShieldCheck, X } from '@phosphor-icons/react'
import { useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { api } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import { Button } from '../../ui/Button'

/**
 * 资产导入表单：拖放 APK + package_name + 授权确认（T1.5）。
 * 409 重复导入提示既有资产（T1.4 评审遗留 D4：不自动跳转，列表刷新即见）。
 */
export function ImportAssetForm({ onClose }: { onClose?: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [packageName, setPackageName] = useState('')
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
      setError('请先选择要导入的 APK 文件')
      return
    }
    if (!packageName.trim()) {
      setError('请填写应用包名（package_name）')
      return
    }
    if (!authorized) {
      setError('请先确认你有权持有并分析该 APK')
      return
    }
    setSubmitting(true)
    setError('')
    setProgress(1)
    try {
      await api.importAsset(
        { file, packageName: packageName.trim(), authorized },
        ({ percent }) => setProgress(percent),
      )
      setProgress(100)
      // 导入成功：关闭表单，资产列表轮询/刷新即见新条目
      onClose?.()
    } catch (value) {
      setError(value instanceof Error ? value.message : '导入资产失败')
      setSubmitting(false)
      setProgress(0)
    }
  }

  return (
    <section className="create-panel glass-panel" aria-label="导入资产">
      <div className="section-heading compact">
        <div>
          <span className="eyebrow">ASSET IMPORT</span>
          <h2>导入 APK 资产</h2>
          <p>注册到资产库后可随时对任意资产子集发起批量扫描。</p>
        </div>
        {onClose && <Button variant="ghost" className="icon-button" onClick={onClose} aria-label="关闭导入面板"><X size={19} /></Button>}
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
          <span className="drop-copy"><strong>拖放 APK，或点击浏览</strong><small>导入时会校验 sha256，重复 APK 将被拒绝</small></span>
        )}
      </button>

      <div className="create-options">
        <label className="field-row">
          <span className="field-label"><Package size={16} /> 应用包名</span>
          <input
            type="text"
            value={packageName}
            placeholder="com.example.app"
            onChange={(event) => setPackageName(event.target.value)}
          />
        </label>
        <label className="authorization-note">
          <input
            type="checkbox"
            checked={authorized}
            onChange={(event) => setAuthorized(event.target.checked)}
          />
          <ShieldCheck size={20} weight="duotone" />
          <span><strong>确认合法测试授权</strong><small>我确认有权持有并分析该 APK，并承担相应责任。</small></span>
        </label>
      </div>

      {submitting && (
        <div className="upload-progress" aria-live="polite">
          <div><span>{progress < 100 ? '正在导入' : '资产已注册'}</span><strong>{progress}%</strong></div>
          <div className="progress-track"><span style={{ transform: `scaleX(${progress / 100})` }} /></div>
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="form-actions">
        <Button variant="primary" loading={submitting} disabled={!file || !authorized || !packageName.trim()} onClick={submit} icon={<ShieldCheck size={18} />}>
          {submitting ? '正在导入' : '导入资产'}
        </Button>
        {onClose && <Button variant="ghost" onClick={onClose}>取消</Button>}
      </div>
    </section>
  )
}
