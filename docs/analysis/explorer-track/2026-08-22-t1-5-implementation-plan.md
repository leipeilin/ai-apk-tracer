# 任务实施方案：T1.5（前端资产/批量页面）

> **任务编号**：T1.5
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` Phase 1 L155-156（资产列表、导入、批量扫描、按批次 findings 汇总）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T1.5
> - T1.4 评审遗留预判项（ASSETS_DISABLED 503 分支、409 details.asset_id 跳转提示）
> **状态**：起草
> **前置依赖**：T1.4（四端点）

---

## 1. 任务目标与范围

- **目标**：`frontend/src/features/assets/` 资产/批量页面——资产列表、APK 导入（authorized + package_name + 进度）、资产多选发起批量扫描、批次进度/汇总（含 ai_skipped 降级分解）。
- **范围**：
  - `lib/types.ts` 补 Asset/Batch 类型；`lib/api.ts` 补四端点客户端 + XHR 上传共享提取；
  - `features/assets/`（AssetsPage + ImportAssetForm + BatchPanel）；
  - 路由 `/assets` + AppShell 导航入口 + 必要样式；
- **非范围**：批次列表页（无 `GET /api/batches` 端点，T1.4 D5——批次进度经 `?batch=<id>` 恢复，见 D1）；"按包/按批次 findings 汇总"的独立聚合视图（方案 L156——**Phase 1 弱化**：批次卡片仅 runs/降级计数聚合，findings 级聚合待批次列表端点后一并设计；评审边界处置标注）；前端组件测试框架引入（项目现状 tsc+build 校验，见 §3.5）。

## 2. 现状锚点

- **技术栈**：React + react-router-dom + framer-motion + Phosphor icons + 玻璃拟态 CSS（glass-panel/page-stack/section-heading 等既有 class 体系）。
- **页面模式**（RunListPage）：stats hero + `usePolling`（interval 函数按状态判定停止）+ Empty/Error/Skeleton 状态组件 + `?create=1` query 开关表单。
- **表单模式**（CreateRunForm）：拖放区 + authorized checkbox + XHR 字节级进度 + ApiError message 展示。
- **api.ts**：类型化客户端 + `ApiError(status, details)`；`createRun` 内联 XHR 上传（L115-139）。
- **路由/导航**：App.tsx Routes + AppShell NavLink（任务/新建分析两个入口）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `frontend/src/lib/types.ts` | 修改 | `Asset`/`BatchSummary`/`BatchAssetSnapshot` 类型 |
| `frontend/src/lib/api.ts` | 修改 | `listAssets`/`importAsset`/`createBatch`/`getBatch` + XHR `postFormData` 共享提取（createRun 改调用） |
| `frontend/src/features/assets/AssetsPage.tsx` | 新增 | 资产页（列表 + 多选 + 批量发起 + 批次卡片） |
| `frontend/src/features/assets/ImportAssetForm.tsx` | 新增 | 导入表单（拖放 + package_name + authorized + 进度 + 409 提示） |
| `frontend/src/features/assets/BatchPanel.tsx` | 新增 | 批次进度卡片（轮询至终态 + 降级分解展示） |
| `frontend/src/app/App.tsx` | 修改 | `/assets` 路由 |
| `frontend/src/ui/AppShell.tsx` | 修改 | 导航入口"资产批量" |
| `frontend/src/styles.css` | 修改 | 资产页/批次卡必要样式（优先复用既有 class） |

### 3.2 组件设计

**AssetsPage**（`/assets`）：
- 数据：`usePolling(api.listAssets)`（interval：存在 scanning 资产 → 2000ms，否则 false）；
- **ASSETS_DISABLED 引导**（D3）：error 为 `ApiError` 且 `status===503` → 展示 EmptyState"资产批量功能未启用"（配置 `assets.enabled` 引导文案），非重试 ErrorState；
- 结构：hero（资产总数/ready/error/scanning 统计）+ `?import=1` 开关 ImportAssetForm + 资产列表行（checkbox 多选 + package_name + sha256 前 12 位 + status Badge + last_run_id 链接到 `/runs/{id}`，**新 asset-row class 不改共享 run-row**，评审边界处置）+ 批量操作栏（选中数 + 授权确认 + 发起；**发起成功清空选中 + 重置授权**，评审 R-3）+ BatchPanel（`?batch=<id>`）。

**ImportAssetForm**（CreateRunForm 模式复刻）：
- 拖放 .apk + `package_name` 文本输入（必填）+ authorized checkbox；
- `api.importAsset`（XHR 进度共享 helper）；
- **409 处理**（D4）：`ApiError.status===409` → 展示"该 APK 已注册" + 既有 asset_id 提示（Phase 1 文案提示，不自动跳转——资产列表刷新即见）。

**BatchPanel**：
- 输入：URL query `?batch=<id>`（评审 R-1：对齐 `?create=1` 先例——发起成功 setSearchParams 携带；刷新/分享可恢复，404 静默清除 query）；
- `usePolling(() => api.getBatch(id))`：pending/running → 2000ms；终态（completed/partial/failed）→ false；
- 展示：状态 Badge + runs 汇总（total/completed/failed）+ ai_skipped 分解（by_budget/by_wall_clock 徽标，**仅非零显示**，评审 R-2）+ assets 快照列表（package_name）；
- 轮询悬挂提示（评审 R-8）：持续轮询超 30 分钟显示"后端可能异常"提示。

### 3.3 API 客户端（api.ts）

```typescript
// XHR 上传共享提取（createRun 与 importAsset 共用，消除复制；评审 R-4）：
// 进度传完整 {loaded, total, percent}；错误分支不访问 responseText
// （responseType='json' 下访问会抛 InvalidStateError——提取时修复既有隐患）
function postFormData<T>(
  path: string, form: FormData,
  onProgress: (progress: { loaded: number; total: number; percent: number }) => void,
): Promise<T>

api.listAssets: () => normalizeList<Asset>('/api/assets')   // items envelope 模式
api.importAsset: (input: { file, packageName, authorized }, onProgress) => postFormData('/api/assets/import', ...)
api.createBatch: (input: { authorized, assetIds }) => request<BatchSummary>('/api/batches', { method: 'POST', body: JSON.stringify({ authorized, asset_ids }) })
api.getBatch: (id) => request<BatchSummary>(`/api/batches/${id}`)
```

### 3.4 类型（types.ts）

```typescript
interface Asset { id; package_name; apk_filename; apk_sha256; source; status; last_run_id; created_at; updated_at }
interface BatchAssetSnapshot { asset_id; package_name; apk_sha256 }
interface BatchSummary {
  id; status; max_ai_calls; max_wall_seconds; ai_skipped_count; assets: BatchAssetSnapshot[];
  created_at; updated_at; completed_at;
  total_runs; completed_runs; failed_runs; ai_skipped;
  ai_skipped_by_budget; ai_skipped_by_wall_clock;
}
```

### 3.5 测试/验收方式

- 项目无前端组件测试框架（check-all 前端 = `tsc + vite build`）——不为本任务引入测试框架（超范围决策，记录）；
- **验收 = build 通过（类型检查）+ 手工验收清单**（验收方案 §2，浏览器逐项操作核对）。

### 3.6 关键设计决策

**D1：批次进度经 URL query 恢复（`?batch=<id>`，评审 R-1 修订）**
- 后端无 `GET /api/batches` 列表端点（T1.4 D5）——批次列表页不做；但 `GET /api/batches/{id}` 已存在，**刷新丢失不可接受**（批量扫描分钟级耗时，"按批次汇总"是上级 L160 验收要求）；
- 采纳：发起成功 `setSearchParams({ batch: id })`；BatchPanel 读 query 轮询；404 静默清除 query（批次被删场景）。

**D2：资产多选批量发起（"按包批量"的前端承载）**
- 列表行 checkbox + 全选；发起栏展示选中数与授权确认——对应方案 L138"给定 package list 批量创建 run"的按包筛选+批量语义（T1.4 D1 的前端侧承接）。

**D3：ASSETS_DISABLED 专用引导态**
- 503 与普通网络错误区分（ApiError.status 判定）——展示功能未启用引导而非误导性"重试"。

**D4：409 重复导入提示不自动跳转**
- 取值链（评审 R-6）：`ApiError.details`（响应 body）→ `details.error.details.asset_id`；
- 展示提示文案（"该 APK 已注册，资产列表可见"）；自动跳转（滚动定位/高亮）为锦上添花，Phase 1 不做。

**D5：样式最小新增**
- 复用 glass-panel/status badge 体系（**新 asset-row class，不改共享 run-row**——防破坏任务页回归基线，评审边界处置）；新增：资产多选行、批量操作栏、批次卡片三类局部样式 + `.status-scanning/.status-ready/.status-error/.status-partial` 四个状态色变体（评审 R-5：StatusBadge 动态拼 class，既有仅 run 状态色）。

**D6：批次轮询悬挂提示（评审 R-8）**
- 后端崩溃时 batch 停留 running（T1.3 D6 已知限制的前端呈现）；BatchPanel 持续轮询超 30 分钟显示"后端可能异常，请检查"提示（不停止轮询——恢复后自动继续）。

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 大 APK 导入进度（XHR） | 请求慢 | 与 createRun 同模式（既有行为） | 无 |
| 批次轮询悬挂（后端崩溃 running 不终态） | 前端持续轮询 | 轮询上限（如 30 分钟后停止 + 提示）——简化：沿用 usePolling interval 函数（running 持续 2s 轮询；D6 已知限制文档化） | 手动刷新 |
| 类型与后端契约漂移 | 前端字段缺失静默 undefined | types.ts 按后端响应实测固化（T1.4 测试已断言字段集） | 无 |

## 5. 依赖

- 前置：T1.4 端点（契约以 test_assets_api.py 断言为准）。
