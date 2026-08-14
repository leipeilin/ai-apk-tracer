import { DownloadSimple, FileMd, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useState } from 'react'
import { api } from '../../lib/api'
import { Button } from '../../ui/Button'
import { SkeletonRows } from '../../ui/StateView'

/** 按需加载固定模板 Markdown 报告，并以纯文本方式安全预览或下载。 */
export function ReportPanel({ findingId, title }: { findingId: string; title: string }) {
  const [report, setReport] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    api.getReport(findingId)
      .then((value) => active && setReport(value))
      .catch((value) => active && setError(value instanceof Error ? value.message : '报告加载失败'))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [findingId])

  const download = () => {
    const blob = new Blob([report], { type: 'text/markdown;charset=utf-8' })
    const href = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = `${title.replace(/[^\w\u4e00-\u9fa5-]+/g, '-').slice(0, 60) || findingId}.md`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(href)
  }

  return (
    <section className="report-panel">
      <div className="report-toolbar">
        <div><FileMd size={19} /><span><strong>Markdown 报告</strong><small>以安全纯文本方式呈现</small></span></div>
        <Button variant="ghost" onClick={download} disabled={!report || loading} icon={<DownloadSimple size={17} />}>下载 .md</Button>
      </div>
      {loading && <SkeletonRows count={3} />}
      {error && <div className="inline-error"><WarningCircle size={18} />{error}</div>}
      {!loading && !error && report && <pre className="report-content" tabIndex={0}>{report}</pre>}
      {!loading && !error && !report && <p className="muted-copy">该发现暂未生成报告。</p>}
    </section>
  )
}
