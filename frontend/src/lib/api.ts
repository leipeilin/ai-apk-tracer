import type {
  AnalysisRun,
  Asset,
  BatchSummary,
  CleanupMode,
  ContextSliceResponse,
  CreateRunInput,
  CreateRunProgress,
  ExplorerQueueResponse,
  Finding,
  LegacyReviewStatus,
  ListResponse,
  ReviewStatus,
} from './types'

const API_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')

/** 统一承载 HTTP 状态码与服务端结构化错误详情。 */
export class ApiError extends Error {
  status: number
  details?: unknown

  constructor(message: string, status: number, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.details = details
  }
}

async function parseError(response: Response) {
  const contentType = response.headers.get('content-type') || ''
  let details: unknown
  try {
    details = contentType.includes('json') ? await response.json() : await response.text()
  } catch {
    details = undefined
  }
  const message = (() => {
    if (typeof details !== 'object' || !details) return `请求失败（${response.status}）`
    if ('detail' in details) return String((details as { detail: unknown }).detail)
    if ('error' in details) {
      const error = (details as { error?: { message?: string } }).error
      if (error?.message) return error.message
    }
    return `请求失败（${response.status}）`
  })()
  return new ApiError(message, response.status, details)
}

/**
 * 执行类型化 API 请求；JSON 请求自动补充 Content-Type，非成功响应统一映射为 ApiError。
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// 当前 API 使用 items envelope；其余分支只负责读取历史部署已返回过的列表外壳。
function normalizeList<T>(payload: T[] | ListResponse<T> | { data?: T[]; results?: T[] }): T[] {
  if (Array.isArray(payload)) return payload
  if ('items' in payload && Array.isArray(payload.items)) return payload.items
  if ('data' in payload && Array.isArray(payload.data)) return payload.data
  if ('results' in payload && Array.isArray(payload.results)) return payload.results
  return []
}

/**
 * XHR 上传 FormData 并暴露字节级进度（createRun/importAsset 共享）。
 * 注意：responseType='json' 下不得访问 responseText（会抛 InvalidStateError），
 * 解析失败时回退为状态码文案。
 */
function postFormData<T>(
  options: { path: string; form: FormData; fallbackTotal: number },
  onProgress: (progress: CreateRunProgress) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}${options.path}`)
    xhr.responseType = 'json'
    xhr.upload.addEventListener('progress', (event) => {
      const total = event.lengthComputable ? event.total : options.fallbackTotal
      onProgress({ loaded: event.loaded, total, percent: total ? Math.round((event.loaded / total) * 100) : 0 })
    })
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as T)
        return
      }
      const detail = xhr.response?.error?.message || xhr.response?.detail || `上传失败（${xhr.status}）`
      reject(new ApiError(String(detail), xhr.status, xhr.response))
    })
    xhr.addEventListener('error', () => reject(new ApiError('网络连接失败，请检查 API 地址', 0)))
    xhr.send(options.form)
  })
}

/** 本地扫描服务的类型化客户端；上传使用 XHR 以暴露真实进度事件。 */
export const api = {
  health: () => request<{ status?: string }>('/health'),
  async listRuns() {
    const payload = await request<AnalysisRun[] | ListResponse<AnalysisRun> | { data?: AnalysisRun[] }>('/api/runs')
    return normalizeList(payload)
  },
  getRun: (id: string) => request<AnalysisRun>(`/api/runs/${encodeURIComponent(id)}`),
  async getFindings(id: string) {
    const payload = await request<Finding[] | ListResponse<Finding> | { data?: Finding[] }>(
      `/api/runs/${encodeURIComponent(id)}/findings`,
    )
    return normalizeList(payload)
  },
  getFindingSlice: (id: string) =>
    request<ContextSliceResponse>(`/api/findings/${encodeURIComponent(id)}/slice`),
  getExplorerCandidates: (id: string) =>
    request<ExplorerQueueResponse>(`/api/runs/${encodeURIComponent(id)}/explorer/candidates`),
  reviewFinding: (
    id: string,
    status: ReviewStatus,
    input: { reason?: string; expectedStatus: ReviewStatus | LegacyReviewStatus; requestId: string },
  ) => request<Finding>(`/api/findings/${encodeURIComponent(id)}/review`, {
    method: 'PATCH',
    body: JSON.stringify({
      status,
      reason: input.reason || null,
      expected_status: input.expectedStatus,
      request_id: input.requestId,
    }),
  }),
  async getReport(id: string) {
    const response = await fetch(`${API_BASE}/api/findings/${encodeURIComponent(id)}/report`)
    if (!response.ok) throw await parseError(response)
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('json')) {
      const value = (await response.json()) as { markdown?: string; report?: string; content?: string }
      return value.markdown || value.report || value.content || JSON.stringify(value, null, 2)
    }
    return response.text()
  },
  cleanupRun: (id: string, mode: CleanupMode) =>
    request<{ status?: string; deleted?: string[] }>(`/api/runs/${encodeURIComponent(id)}/cleanup`, {
      method: 'POST',
      body: JSON.stringify({ mode, confirm_delete: mode === 'delete_run' }),
    }),
  createRun(input: CreateRunInput, onProgress: (progress: CreateRunProgress) => void) {
    const form = new FormData()
    form.append('file', input.file)
    form.append('authorized', String(input.authorized))
    form.append('source_analysis_enabled', String(input.sourceAnalysisEnabled))
    form.append('explorer_enabled', String(input.explorerEnabled))
    return postFormData<AnalysisRun>({ path: '/api/runs', form, fallbackTotal: input.file.size }, onProgress)
  },
  async listAssets() {
    const payload = await request<Asset[] | ListResponse<Asset> | { data?: Asset[] }>('/api/assets')
    return normalizeList(payload)
  },
  importAsset(
    input: { file: File; packageName: string; authorized: boolean },
    onProgress: (progress: CreateRunProgress) => void,
  ) {
    const form = new FormData()
    form.append('file', input.file)
    form.append('package_name', input.packageName)
    form.append('authorized', String(input.authorized))
    return postFormData<Asset>({ path: '/api/assets/import', form, fallbackTotal: input.file.size }, onProgress)
  },
  createBatch: (input: { authorized: boolean; assetIds: string[] }) =>
    request<BatchSummary>('/api/batches', {
      method: 'POST',
      body: JSON.stringify({ authorized: input.authorized, asset_ids: input.assetIds }),
    }),
  getBatch: (id: string) => request<BatchSummary>(`/api/batches/${encodeURIComponent(id)}`),
}
