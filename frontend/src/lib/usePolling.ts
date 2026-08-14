import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 周期加载异步资源，并在调用方判定任务结束后停止轮询。
 * 静默刷新保留已有数据，组件卸载后不会继续写入 React 状态。
 */
export function usePolling<T>(
  loader: () => Promise<T>,
  interval: number | false | ((current: T | null) => number | false),
  dependencies: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const mounted = useRef(true)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const next = await loader()
      if (mounted.current) {
        setData(next)
        setError(null)
      }
      return next
    } catch (value) {
      if (mounted.current) setError(value instanceof Error ? value : new Error('加载失败'))
      return null
    } finally {
      if (mounted.current && !silent) setLoading(false)
    }
  }, dependencies) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
    }
  }, [load])

  useEffect(() => {
    const wait = typeof interval === 'function' ? interval(data) : interval
    if (wait === false) return
    const timer = window.setInterval(() => void load(true), wait)
    return () => window.clearInterval(timer)
  }, [data, interval, load])

  return { data, setData, loading, error, reload: load }
}
