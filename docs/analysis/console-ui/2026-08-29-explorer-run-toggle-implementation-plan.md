# 任务实施方案：explorer-run-toggle

> **任务编号**：explorer-run-toggle
> **日期**：2026-08-29
> **依据需求**：用户需求（2026-08-29 会话）：开启探索轨需要在前端提交任务界面设置按钮控制（随任务传入），而不是每次修改配置文件再重启服务。
> **状态**：已闭合（评审 R-1~R-5 全部采纳，见 `2026-08-29-explorer-run-toggle-review.md` 处置记录）
> **前置依赖**：T2.7 探索轨 ✅（orchestrator 探索门禁与阶段实现）；track-progress-console ✅（progress 块对探索轨未启用已有降级展示）；add9ef0 ✅（config 默认开启探索轨——explorer.enabled: true，2026-08-29 用户决策）

---

## 1. 任务目标与范围

- **目标**：探索轨启用与否改为**任务级**开关——前端提交任务界面勾选控制，随任务写入 manifest config 快照（可审计），orchestrator 按 run 级值执行；全局配置退化为 API 未显式传参时的缺省值，不再需要改配置重启服务。
- **范围（in scope）**：
  1. `backend/app/runs/run_config.py`：`build_run_config` 新增 `explorer_enabled: bool | None = None` 参数（None 沿用 `settings.explorer.enabled`），config 快照新增 `explorer` 段（完整 ExplorerSettings dump + enabled 覆盖——对齐 source_analysis 段的既有模式）。
  2. `backend/app/api/routes.py`：`create_run` 新增 form 字段 `explorer_enabled: bool | None = Form(default=None)`，透传 `build_run_config`。
  3. `backend/app/analysis/orchestrator.py`：`scan()` 探索门禁（`if self.settings.explorer.enabled:`）改为 run 级判定——读 `run.config.explorer.enabled`，缺失时回退全局配置（对齐 `source_enabled` 的既有读取模式）。
  4. 前端 `CreateRunForm.tsx` 新增「启用探索轨」开关（默认**开**——对齐 config 默认 true 的用户决策），`types.ts` 的 `CreateRunInput` 增加 `explorerEnabled`，`api.ts` 的 `createRun` 追加 form 字段。
  5. 测试：`test_api.py` 三态端到端（显式开/显式关/缺省）+ orchestrator 门禁回归（既有 explorer 测试 run 无 config 时行为不变）+ `test_run_config`（如已有 golden 固化文件则同步）+ 前端 `npm run build`。
- **非范围（out of scope）**：
  - 资产批量（batch）界面的探索开关——batch 编排（`assets/batch.py:216`）不传新参数 → None → 沿用全局配置，行为与改动前完全一致；批量粒度的开关后续可按同模式追加。
  - 其它任务级配置项（AI 模型、预算、api_surface 开关等）——本任务只做探索轨开关。
  - 探索轨其它参数（max_candidates_per_run、entry_concurrency 等）的任务级定制——快照中如实记录全局值，但不开放任务级覆盖。

## 2. 现状锚点

> 全部锚点经读码核实（2026-08-29，基于 commit 1094f30）。

- **单任务创建端点（先例模式）**：`backend/app/api/routes.py:103-140` — `create_run` 以 `source_analysis_enabled: bool = Form(default=True)`（:108）接收任务级开关，经 `build_run_config(settings, source_analysis_enabled=...)`（:121）写入 config 快照，`storage.ingest(..., config)`（:122）落 manifest，:139 后台启动 `orchestrator.scan`。本任务完全复刻该链路新增 explorer 开关。
- **config 快照构造（复用点）**：`backend/app/runs/run_config.py:13-44` — `build_run_config` 已有 `source_analysis_enabled: bool = True` 参数与 `ai_enabled: bool | None = None`（None 沿用 settings）两种覆盖形态；source_analysis 段写法 `{**settings.source_analysis.model_dump(mode="json"), "enabled": source_analysis_enabled}`（:39-42）。explorer 段按同模式追加；`ai_enabled: None 沿用 settings` 的三态语义即 explorer_enabled 需要的语义。
- **批量共享调用点（兼容性证据）**：`backend/app/assets/batch.py:216-221` — batch 编排调用同一 `build_run_config`（传 source_analysis_enabled=True、ai 预算降级参数）；不传新参数时 explorer_enabled=None → 沿用全局配置，批量行为零变化。
- **orchestrator run 级开关读取模式（先例）**：`backend/app/analysis/orchestrator.py:112`（`run = self.repository.get_run(run_id)`，`_run_row` 已 json 解析完整 config）+ :114-116 — `source_enabled = run.get("config", {}).get("source_analysis", {}).get("enabled", self.settings.source_analysis.enabled)`——任务级值优先、缺失回退全局。explorer 门禁照此模式。
- **探索门禁现状（唯一全局硬编码点）**：`backend/app/analysis/orchestrator.py:231` — `if self.settings.explorer.enabled:` 直接读全局配置；全后端仅此一处消费 `explorer.enabled` 做 run 时门禁（grep 核实）。
- **ExplorerSettings 模型**：`backend/app/config.py:206-220+` — pydantic BaseModel（含 `model_dump(mode="json")` 能力），`Settings.explorer: ExplorerSettings`（config.py:300）。
- **全局配置现状**：`config/default.yaml:178-179` — `explorer.enabled: true`（commit b2d4d15 置值、add9ef0 同步 test_config 断言；2026-08-29 用户决策默认开启）。因此表单默认勾选 = 开与全局缺省一致；API 未传字段的老调用方行为也与其改动前一致（此前默认即 true）。
- **前端表单（开关 UI 先例）**：`frontend/src/features/runs/CreateRunForm.tsx:15` — `sourceAnalysis` state 默认 true；:99-114 `.create-options` 内 `.switch-row` 开关样式（label + checkbox + `.switch` 滑块）；:52-54 调用 `api.createRun({ file, authorized, sourceAnalysisEnabled: sourceAnalysis }, ...)`。
- **前端 API 层**：`frontend/src/lib/api.ts:150-156` — `createRun` 逐字段 `form.append('source_analysis_enabled', String(...))` 后经共享 `postFormData` 上传。
- **前端类型**：`frontend/src/lib/types.ts:260-264` — `CreateRunInput { file; authorized; sourceAnalysisEnabled }`。
- **进度展示兼容**：track-progress-console 的 progress 块对探索轨未启用已降级（explorer 轨 null → "探索轨未启用或未记录"），任务级关闭的 run 自动获得一致展示，无需改动。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/runs/run_config.py` | 修改 | `build_run_config` 新增 `explorer_enabled: bool \| None = None`；config 快照新增 `explorer` 段 |
| `backend/app/api/routes.py` | 修改 | `create_run` 新增 `explorer_enabled` Form 字段并透传 |
| `backend/app/analysis/orchestrator.py` | 修改 | 探索门禁改 run 级判定（回退全局） |
| `backend/tests/test_run_config.py`（新增；golden 相关同步见下行） | 新增 | build_run_config explorer 段三态单测 |
| `backend/tests/test_batch.py` | 修改 | golden dict 精确相等断言（:466-493）同步追加 explorer 段期望（评审 R-1：非可选，否则全量回归必红） |
| `backend/tests/test_api.py` | 修改 | 三态端到端：manifest config.explorer.enabled 断言 |
| `frontend/src/lib/types.ts` | 修改 | `CreateRunInput.explorerEnabled: boolean` |
| `frontend/src/lib/api.ts` | 修改 | `createRun` 追加 `explorer_enabled` form 字段 |
| `frontend/src/features/runs/CreateRunForm.tsx` | 修改 | 「启用探索轨」开关（switch-row 复用） |

### 3.2 数据结构与接口设计

**API**：`POST /api/runs` multipart 新增可选字段：

```
explorer_enabled: boolean（可选；缺省 = 沿用服务端 settings.explorer.enabled）
```

**manifest config 快照新增 `explorer` 段**（对齐 source_analysis 段形态）：

```json
{
  "explorer": {
    "enabled": true,
    "max_candidates_per_run": null,
    "auto_promote": false,
    "allow_external_code": true,
    "prompt_version": "explorer/1.0.0",
    "...": "ExplorerSettings 其余字段原值记录（审计用，不做任务级覆盖）"
  }
}
```

**前端类型**：

```ts
export interface CreateRunInput {
  file: File
  authorized: boolean
  sourceAnalysisEnabled: boolean
  explorerEnabled: boolean
}
```

### 3.3 分模块设计

**模块 A：`run_config.build_run_config`**

- 签名追加 `explorer_enabled: bool | None = None`；解析：`resolved = settings.explorer.enabled if explorer_enabled is None else explorer_enabled`。
- 返回 dict 新增：

```python
"explorer": {
    **settings.explorer.model_dump(mode="json"),
    "enabled": resolved,
},
```

- 与 source_analysis 段完全同构（全量 dump + enabled 覆盖）；文档字符串补参数说明。

**模块 B：`routes.create_run`**

- 签名追加 `explorer_enabled: bool | None = Form(default=None)`（FastAPI 对 bool Form 自动解析 "true"/"false"；字段缺省时为 None）。
- `build_run_config(settings, source_analysis_enabled=source_analysis_enabled, explorer_enabled=explorer_enabled)`。
- 校验：无需新增——授权校验既有；explorer_enabled 任意 bool 合法。

**模块 C：`orchestrator.scan` 探索门禁**

- `source_enabled` 读取（orchestrator.py:113-116）之后增加：

```python
explorer_enabled = (
    run.get("config", {}).get("explorer", {}).get("enabled", self.settings.explorer.enabled)
)
```

- 门禁（orchestrator.py:231）改为 `if explorer_enabled:`。
- 边界：老 run 的 config 无 explorer 段 → 回退全局配置（行为与改动前一致）；config 缺失（None）→ 同样回退。`explorer_enabled` 判定放在 `source_enabled` 旁，:231 处直接引用局部变量。
- 注意：`_run_explorer_stage` 内部仍用 `self.settings.explorer` 的其余参数（预算/并发等）——任务级只覆盖 enabled 旗标，其余参数全局生效（out of scope 已声明）。

**模块 D：前端 CreateRunForm**

- 新增 state：`const [explorerEnabled, setExplorerEnabled] = useState(true)`（对齐 config 默认 true——add9ef0 用户决策）。
- `.create-options` 内、「启用反编译代码分析」开关之后追加同构 `.switch-row`：

```tsx
<label className="switch-row">
  <span><strong>启用探索轨</strong><small>AI 检索循环探索攻击面入口（消耗 AI 请求预算）</small></span>
  <input type="checkbox" checked={explorerEnabled} onChange={...} />
  <span className="switch" aria-hidden />
</label>
```

- 提交：`api.createRun({ file, authorized, sourceAnalysisEnabled: sourceAnalysis, explorerEnabled }, ...)`。
- 组件头注释同步提及探索轨开关。

**模块 E：前端 api.ts / types.ts**

- `CreateRunInput` 增加 `explorerEnabled: boolean`；`createRun` 追加 `form.append('explorer_enabled', String(input.explorerEnabled))`。

### 3.4 错误处理与边界

- **API 缺省字段**：None → settings.explorer.enabled（batch 与既有 API 调用方零变化）。
- **老 run / config 缺失**：orchestrator 回退全局配置（与 source_enabled 同款兜底）。
- **非法值**：FastAPI bool Form 解析失败自动 422（与 source_analysis_enabled 同语义，无需额外处理）。
- **探索轨关闭的任务**：跳过 explorer 阶段（无 explorer stage、无产物）→ progress.explorer 为 null、探索轨页签显示未启用（track-progress-console 既有降级，回归项）。
- **api_surface 联动**：不在本任务范围——explorer 任务级开启而 api_surface 全局关闭时，探索轨按既有 degraded 空跑路径运行（config/default.yaml:206-207 注释已记录该语义），不新增联动逻辑。

### 3.5 测试设计

- 既有 golden（评审 R-1 修订，确定存在）：`backend/tests/test_batch.py:466-493` `test_run_config_golden` 为 dict **精确相等**断言——golden 追加 `"explorer": {**settings.explorer.model_dump(mode="json"), "enabled": True}`（以该测试 `_make_stack` 构造的 settings 实际 explorer 值为准）；降级分支与默认分支的子字段断言不变。
- `test_run_config.py`（新）：三态——显式 True / 显式 False / None（沿用 settings）→ `config["explorer"]["enabled"]` 断言 + 其余字段原值透传断言。
- `test_api.py` 端到端：
  1. `explorer_enabled=false` 上传 → manifest `config.explorer.enabled is False`、扫描完成后 manifest.stages 无 explorer 项；
  2. `explorer_enabled=true` 上传 → `config.explorer.enabled is True`（真实探索阶段执行依赖 AI/runtime，端到端仅断言配置与完成态；完整探索行为由既有 test_explorer 覆盖）；
  3. 不传字段 → `config.explorer.enabled is settings.explorer.enabled`（与**测试所用 Settings 实例**一致——评审 R-3：pytest `Settings()` 直构不加载 default.yaml，模型默认 False；运行时 True 由 get_settings 提供，add9ef0。如需覆盖 True 缺省形态，显式构造 `Settings(..., explorer=ExplorerSettings(enabled=True))` 并注释说明）。
- orchestrator 门禁回归：既有 `test_explorer.py` 各用例的 run 无 config explorer 段 → 回退全局（测试 settings explorer.enabled 需为真才能跑探索——实施时核对这些用例的 Settings 构造，若依赖全局 enabled=True 则天然回归；若显式关闭则需补 config 注入）。
- 前端：`npm run build`（tsc -b && vite build）零错误；浏览器实测见验收方案。

## 4. 风险与回退

- **行为默认值变化风险**：无——API 缺省沿用全局配置，全局当前默认 true 与改动前一致；表单默认勾选开也与全局一致。
- **config 快照体积**：explorer 段全量 dump 约十几个字段，对 manifest 体积影响可忽略；`_safe_config_snapshot`（routes.py:30-40）只取 config["ai"]、`_public_run`（:43-51）仅脱敏 ai 段，explorer 段含非敏感参数（无密钥），响应脱敏行为不受影响、按既有设计不透出 explorer 段（审计以落盘 manifest 为准——评审 R-2）。
- **门禁读取位置**：`scan()` 在 :113 读 run 行一次；explorer_enabled 与 source_enabled 同源同读取时机，无额外 DB 访问。
- **回退**：三文件后端 checkout + 前端三文件 checkout 即完全回滚；config 快照新增段对旧代码读取方透明（未知键忽略）。
