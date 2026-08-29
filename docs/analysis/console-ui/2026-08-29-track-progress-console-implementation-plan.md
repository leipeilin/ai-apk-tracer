# 任务实施方案：track-progress-console

> **任务编号**：track-progress-console
> **日期**：2026-08-29
> **依据需求**：用户需求（2026-08-29 会话）：①任务详情页探索轨/规则轨运行展示改为按钮切换，不再把探索轨成果追加在规则轨之后；②提供双轨任务运行反馈——探索轨攻击面总量/已探索/未探索，规则轨任务总量/已完成/未完成。
> **状态**：已闭合（评审 R-1~R-7 全部采纳，见 `2026-08-29-track-progress-console-review.md` 处置记录）
> **前置依赖**：T2.7 探索轨 ✅（explorer stage 与 candidates.json 产物）；T2.10 人工队列 ✅（ExplorerQueuePanel 与 GET /explorer/candidates）；F4 覆盖透明化 ✅（explorer stage summary 的 entries_explored/entries_unexplored）

---

## 1. 任务目标与范围

- **目标**：任务详情页（RunDetailPage）主展示区改为「规则轨 / 探索轨」分段按钮互斥切换；并在 `GET /api/runs/{id}` 响应中新增 `progress` 块，为双轨提供运行反馈（探索轨：攻击面总量/已探索/未探索；规则轨：任务总量/已完成/未完成），运行中每 2 秒随既有轮询自动刷新。
- **范围（in scope）**：
  1. 后端新增 `backend/app/runs/progress.py`：`build_run_progress(run_dir, manifest)` 计算双轨进度，多级降级兼容历史 run。
  2. `backend/app/api/routes.py` 的 `get_run` 接线 `progress` 块（计算失败降级 None，不阻塞 run 响应）。
  3. `backend/app/analysis/orchestrator.py`：规则执行启动前把 `rule_total_count`、探索执行启动前把 `explorer_total_count` 提前写入 manifest 顶层（运行中双轨总量可知，评审 R-1 修订）。
  4. `backend/app/analysis/rule_runner.py`：`run_all` 由"全部跑完后批量落盘"改为"每条规则完成即落盘 `rule-results/{rule_id}.json`"（聚合顺序与语义不变）。
  5. 前端 `RunDetailPage`：新增轨切换分段按钮与 `TrackProgress` 进度组件，`FindingsPanel`（规则轨）与 `ExplorerQueuePanel`（探索轨）互斥渲染。
  6. 前端 `types.ts` 将遗留 `progress?: number`（无消费方，已核）**替换**为 `RunProgress` 类型（评审 R-4）；`styles.css` 新增切换栏/进度条样式（复用既有 `.progress-track` 模式）。
  7. 测试：`test_run_progress.py`（新）+ `test_rule_runner.py`（新，评审 R-5）+ `test_api.py` 端到端断言 + `test_rule_index_protocol.py`/`test_manual_review_regressions.py` 既有回归。
- **非范围（out of scope）**：
  - 阶段时间线（StageTimeline）重构——进度反馈独立于阶段展示，不改阶段语义。
  - ai_analysis 阶段的 AI 复核进度——用户需求仅涉及探索/规则两轨。
  - 探索轨运行中软上限（skipped_max_cap）/熔断（short_circuited）入口的实时精确口径——运行中"已探索"为近似值（partial jsonl 行数），终态以 stage summary 精确值覆盖（口径差异见 §3.4）。
  - 前端组件测试基建——仓库无前端测试框架（package.json 仅 `tsc -b && vite build`），前端验收采用构建门禁 + 浏览器实测（见验收方案）。

## 2. 现状锚点

> 全部锚点经读码核实（2026-08-29）。

- **任务详情页主区堆叠展示**：`frontend/src/features/runs/RunDetailPage.tsx:90-95` — `main.detail-findings` 内先渲染 `FindingsPanel`（规则轨发现），再无条件追加 `ExplorerQueuePanel`（探索轨人工队列，且仅 `explorerQueueState.data` 存在时）；即用户反馈的"探索轨成果追加在规则轨后"。本任务改为互斥切换。
- **既有轮询结构**：`frontend/src/features/runs/RunDetailPage.tsx:19-40` — `getRun`/`getFindings`/`getExplorerCandidates` 三个 usePolling；活跃时 2s。注释（:25-28）记录了 2026-08-15 评审 R-4 的锁优化决策：**避免新增并发轮询压 SQLite 读锁**。因此 progress 注入 `get_run` 响应而非新增轮询端点。
- **get_run 响应构造**：`backend/app/api/routes.py:139-160` — 已读 manifest 并把 `stages` 并入响应；已有 `storage.run_dir(run_id)` 访问（:153）与 try/except 降级（:158-159）。progress 计算接在同处。
- **explorer_candidates 端点先例**：`backend/app/api/routes.py:170-193` — 产物缺失/损坏降级空态而非 404；progress 计算遵循同一保守哲学。
- **探索轨终态进度（已有机器口径）**：`backend/app/analysis/orchestrator.py:1257-1274` — explorer stage summary 含 `entry_count`（有效入口数，`method_id` 非空过滤后，:1174）、`entries_explored`、`entries_unexplored`；但 `_record_stage` 在阶段结束时才写，**运行中不可见**。
- **探索轨运行中实时落盘（已有）**：`backend/app/analysis/explorer.py:247-272` — `_append_partial_record` 每入口完成即 append 一行到 `explorer/observations-partial.jsonl`（小记录：entry_id/terminated_by/rounds/candidate_count）；调用点在 worker 正常返回路径 `explorer.py:193`；正式 `observations.json` 在 `explore_all` 收尾写盘后删除 partial（explorer.py:241-244）。→ 运行中"已探索数" = partial 行数；终态后 = observations.json 条目数或 stage summary。
- **探索轨总量口径（评审 R-1 修订）**：原始 `api-surface/api_entry_table.json` 条目**没有** `method_id` 键（`api_surface.py:40-261`，binder 仅 `implementation_method_id` :187）；`orchestrator.py:1174` 过滤的 `method_id` 由 `call_tree.py:99-114 _entry_method_id` 经 SQLite reader（`resolve_component_lifecycle_methods`）解析——**JSON 静态计数不可行**。故 explorer 总量与规则总量同法：orchestrator 在 `explore_all` 前提前写 manifest 顶层 `explorer_total_count=len(effective)`（与终态 summary `entry_count` :1258 严格同源）。
- **规则轨终态汇总**：`backend/app/analysis/orchestrator.py:178-191` — rule_prescan stage 在 `run_all` **全部完成后**才 `_record_stage`（summary 含 `rule_total_count`/`rule_failures`/`candidate_count`）；运行中 stage summary 不存在 → 规则总量运行中不可知，需提前写入。
- **规则逐条结果落盘时机（核心缺口）**：`backend/app/analysis/rule_runner.py:104-131` — 进程池路径 `list(executor.map(...))`（:114-125）阻塞至全部完成，串行路径（:104-105）同样跑完才返回；随后 post-loop（:127-129）才逐条写 `rule-results/{rule_id}.json`。→ 运行中 rule-results 目录为空，"已完成数"无实时信号。本任务改为逐条完成即落盘。
- **rule-results 目录混有规则产物（评审 R-2 修订）**：`rule_runner.py:220` `_export_rule_artifacts` 向同一 `rule-results/` 目录写 `binder_bindings.json`/`receiver_registrations.json`/`webview_js_bridges.json`（键集 `RULE_ARTIFACT_KEYS` :29；消费方 `api_surface.py:153`、`attack_surface.py:242` 依赖该路径，不可挪目录）。→ progress 计数必须排除这三个词干。
- **规则结果原子写（可复用）**：`backend/app/analysis/rule_runner.py:153-166` — `_write_result` 已实现 tmp + `os.replace` 原子替换，增量落盘直接复用。
- **规则失败归一（安全前提）**：`backend/app/analysis/rule_runner.py:252-330` — `_run_one` 将超时/输出超限/非零退出/协议错误全部归一为失败 result dict，不向调用方抛异常；故 `as_completed` 改造不引入新异常路径。
- **每条规则均落盘（成功与失败，产物词干除外）**：rule_runner.py:127-129 对 `zip(rules, results)` 无差别 `_write_result`，失败仅额外进 `failures`（:150）。→ 规则 result 文件数 = 已处理数（成功+失败）；目录内另有 `RULE_ARTIFACT_KEYS` 三类产物文件（见上锚点），计数须排除。
- **manifest 通用更新能力（可复用）**：`backend/app/runs/storage.py:135-142` — `update_manifest(run_id, **changes)` 支持任意顶层键合并。
- **前端遗留 progress 字段（评审 R-4 修订）**：`frontend/src/lib/types.ts:50` — `AnalysisRun.progress?: number` 为遗留声明，全仓 grep 复核无组件消费（仅上传进度本地 state 与样式类名 `.progress-track` 同名异义）；本任务将其**替换**为 `progress?: RunProgress | null`。
- **前端进度条样式（可复用）**：`frontend/src/styles.css:163-164` — `.progress-track` + 内层 `span` transform 动画，上传进度条已用；轨进度条直接复用。
- **前端分段按钮先例**：`frontend/src/styles.css:94-96` — `.theme-switch button.active` 分段激活态；`frontend/src/ui/Button.tsx` 提供 `.button button-secondary` 基类。切换栏样式对齐两者。
- **历史 run 差异**：2026-08-27（F4）之前的 run 的 explorer summary 缺 `entries_explored/entries_unexplored`，但有 `observations.json`（T2.5b 起）；更早 run 可能连 api_surface/explorer stage 都没有 → 进度多级降级为 null，前端显示"未记录"。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/runs/progress.py` | 新增 | `build_run_progress(run_dir, manifest)`：双轨进度计算与多级降级 |
| `backend/app/api/routes.py` | 修改 | `get_run` 接线 progress 块（try/except 降级 None） |
| `backend/app/analysis/orchestrator.py` | 修改 | 规则/探索阶段启动前分别提前写 manifest 顶层 `rule_total_count`、`explorer_total_count`（评审 R-1） |
| `backend/app/analysis/rule_runner.py` | 修改 | `run_all` 逐条完成即 `_write_result`；聚合顺序不变 |
| `backend/tests/test_run_progress.py` | 新增 | progress 计算单测（全字段/降级/畸形/未启用/产物词干排除） |
| `backend/tests/test_api.py` | 修改 | getRun 端到端断言 progress 块 |
| `backend/tests/test_rule_runner.py` | 新增 | run_all 后成功/失败规则 result 文件齐全落盘（评审 R-5：既有 run_all 回归在 `test_rule_index_protocol.py:103-176`、`test_manual_review_regressions.py:210`，不在本文件） |
| `frontend/src/lib/types.ts` | 修改 | 遗留 `progress?: number` 替换为 `RunProgress` 类型组（评审 R-4） |
| `frontend/src/features/runs/RunDetailPage.tsx` | 修改 | 轨切换状态与互斥渲染、接线 TrackProgress |
| `frontend/src/features/runs/TrackProgress.tsx` | 新增 | 双轨进度条组件（总/已完成/未完成 + 进度条） |
| `frontend/src/styles.css` | 修改 | `.track-switcher` 分段按钮 + 进度摘要样式 |

### 3.2 数据结构与接口设计

后端 `GET /api/runs/{id}` 响应新增顶层 `progress` 字段（计算失败或 run 目录缺失时为 `null`）：

```json
{
  "progress": {
    "rules":    { "total": 33, "processed": 30, "failed": 2 },
    "explorer": { "total": 73, "explored": 70, "unexplored": 3 }
  }
}
```

- `rules.total`（int|null）：规则任务总量。口径 = `RuleRunner.discover()` 数（builtin 且有 detect.py）。
- `rules.processed`（int|null）：已处理规则数（成功+失败，rule-results 文件数）。
- `rules.failed`（int|null）：失败规则数（仅终态 summary 可知；运行中为 null）。
- `explorer.total`（int|null）：攻击面总量。口径 = 有效入口数（api_entries 中 dict 且 `method_id` 非空），与 stage summary `entry_count` 同口径。
- `explorer.explored`（int|null）：已探索入口数。
- `explorer.unexplored`（int|null）：未探索入口数。
- 任一字段无法可靠得出时为 `null`（不伪造 0）；`progress.rules`/`progress.explorer` 在对应轨完全无信号时整体为 `null`。
- 语义约定：`processed` 表"已处理"（含失败）；前端"已完成 = processed - failed（终态）或 processed（运行中）"，"未完成 = total - processed"。

前端类型（`types.ts`）：

```ts
export interface RulesProgress { total: number | null; processed: number | null; failed: number | null }
export interface ExplorerProgress { total: number | null; explored: number | null; unexplored: number | null }
export interface RunProgress { rules: RulesProgress | null; explorer: ExplorerProgress | null }
// AnalysisRun.progress：替换 types.ts:50 遗留的 progress?: number（无消费方，评审 R-4 已核）
```

### 3.3 分模块设计

**模块 A：`backend/app/runs/progress.py`（新）**

```python
def build_run_progress(run_dir: Path, manifest: dict | None) -> dict:
    """返回 {"rules": {...} | None, "explorer": {...} | None}；全部信号缺失时两轨可为 None。"""
```

- `rules` 计算（按优先级）：
  - `total`：manifest.stages 中 `name == "rule_prescan"` 最后一条的 `summary.rule_total_count` → fallback manifest 顶层 `rule_total_count`（orchestrator 运行中提前写入）→ `null`。
  - `processed`：`(run_dir / "rule-results").glob("*.json")` 计数并**排除 `RULE_ARTIFACT_KEYS` 三个产物词干**（`from app.analysis.rule_runner import RULE_ARTIFACT_KEYS`——`binder_bindings`/`receiver_registrations`/`webview_js_bridges` 与规则 result 同目录，评审 R-2；`_write_result` 原子替换的 tmp 文件名形如 `.RID.json.<uuid>.tmp`，不匹配 `*.json`，无污染）；目录不存在计 0。
  - `failed`：rule_prescan summary `rule_failures` 列表长度 → `null`。
  - gating：`total` 与 `processed` 全无信号（manifest 缺 rule_prescan 且无顶层计数且目录不存在）→ `rules = None`。
- `explorer` 计算（按优先级，`_stage_summary(manifest, "explorer")` 取最后一条同名 stage）：
  - `total`：explorer summary `entry_count` → fallback manifest 顶层 `explorer_total_count`（orchestrator 运行中提前写入，评审 R-1）→ `null`。**不做 api_entry_table JSON 计数**（条目无 `method_id` 键，静态计数恒 0——R-1 证据）。
  - `explored`：explorer summary `entries_explored`（终态精确）→ fallback `explorer/observations-partial.jsonl` 非空行数（运行中近似）→ fallback `explorer/observations.json` 的 `entries` 数组长度（历史终态 run）→ `null`。
  - `unexplored`：explorer summary `entries_unexplored` → fallback `total - explored`（两者均可算时；负差防御性钳位为 0——代码审查 C-2 采纳口径）→ `null`。
  - gating：explorer stage 不存在且 `explorer/` 目录无任何产物**且 manifest 顶层无 `explorer_total_count`** → `explorer = None`（探索轨未启用是常态，非异常；顶层键是探索运行早期窗口——stage 未落、首条 partial 未写——的唯一"在跑"信号，代码审查 C-1 采纳）。
- 错误处理：单个产物畸形（JSON 损坏/结构不符）→ 该字段按 `null` 降级，不抛异常（对齐 explorer_candidates 端点 :186-192 的保守哲学）；整函数不感知 HTTP。

**模块 B：`routes.py get_run` 接线**

- try 块前初始化 `run["progress"] = None`；manifest 读取成功后在 try 内调用 `build_run_progress(request.app.state.storage.run_dir(run_id), manifest)` 覆盖（评审 R-6：既有 except 分支（AppError/OSError/ValueError，:157-159）路径下 progress 天然保持 None，与验收 N-4 字面一致）。
- progress 计算自身已全容错；为绝对不阻塞 run 响应，外层再以 `except Exception: run["progress"] = None` 兜底（保守降级，记 warning 日志）。

**模块 C：orchestrator 提前写双轨总量（评审 R-1 修订：新增 explorer_total_count）**

- `_stage(run_id, "rule_prescan")` 之后、`run_all` 之前：

```python
rule_total_count = len(self.rule_runner.discover())
self.storage.update_manifest(run_id, rule_total_count=rule_total_count)
```

- 提前写入与终态 summary 的 `rule_total_count`（orchestrator.py:193）**共用同一次 `discover()` 结果**（代码审查 C-3：单次 scan 内避免 3 次全量 YAML 解析，两处口径绝对同源）。

- `_run_explorer_stage` 内 `effective` 计算后、`explore_all` 之前（orchestrator.py:1183 与 :1197 之间）：

```python
self.storage.update_manifest(run_id, explorer_total_count=len(effective))
```

- 两者与终态 summary 的 `rule_total_count`（orchestrator.py:189）/`entry_count`（:1258）数值严格同源；顶层键对既有消费者透明（manifest 未知键经 `_public_run` 透传）。
- 幂等：scan 单次执行各写一次；重复 scan 场景 manifest 重建（storage 初始化），无残留问题。

**模块 D：rule_runner 逐条落盘**

- 新增私有方法 `_persist_result(run_dir, rule, result)`：即现 post-loop 的落盘两行（rule_runner.py:127-129 的 path 构造 + `_write_result`）。
- 串行路径（max_workers ≤ 1，:104-105）：`for rule in rules` 循环内 `_run_one` 后立即 `_persist_result`。
- 进程池路径（:114-125）：`executor.map + list()` 改为 `executor.submit`（保留 rules 索引）+ `as_completed`；每个 future 完成即 `_persist_result`；全部完成后按索引排序还原 rules 原序 `results` 列表。
- post-loop（:126-150）聚合逻辑不变（candidates 按规则原序 extend、failures 收集、coverage gaps），仅移除其中的重复落盘（已前置）。
- 异常语义：`future.result()` 抛出时直接传播（`_run_one` 已归一全部预期失败，:252-330，实际仅资源类 OSError 可能抛出）——与原 `list(map)` 的传播语义一致；`with ProcessPoolExecutor` 退出时等待剩余任务的语义也不变。

**模块 E：前端 RunDetailPage 轨切换**

- `const [track, setTrack] = useState<'rules' | 'explorer'>('rules')`（默认规则轨——findings 是主产出）。
- `main.detail-findings` 顶部渲染轨切换栏（role="tablist"，两个 role="tab" 按钮，`id` + `aria-controls="track-panel"` 配对——代码审查 C-4；面板容器 `role="tabpanel"` + `aria-labelledby` 动态指向）：「规则轨」「探索轨」，各带进度摘要徽标（`progress` 数字，null 显示 "—"）。
- 切换栏下方渲染 `<TrackProgress track={track} progress={run.progress} />`。
- 互斥渲染：`track === 'rules' ? <FindingsPanel .../> : <ExplorerQueuePanel queue={explorerQueueState.data} />`。
  - ExplorerQueuePanel 已接受 `null` 并自带"探索轨未启用或无候选"空态（ExplorerQueuePanel.tsx:38-47），`explorerQueueState.data` 为 null 时也渲染（原 `&&` 守卫移除）。
  - 已知取舍：切换后 FindingsPanel 内部筛选条件（severity/review/query）重置——数据仍在页面层轮询保留，仅展示态重置；为最小改动接受（见 §4 风险）。
- findings/explorerQueue 轮询条件不变（两轨数据都持续轮询，切换零延迟）。

**模块 F：`TrackProgress.tsx`（新组件）**

- props：`{ track: 'rules' | 'explorer'; progress: RunProgress | null }`。
- rules 轨：标签"规则任务"；进度条 `processed/total`；计数行：`总 X · 已完成 Y · 未完成 Z`，`failed` 已知且 >0 时追加 `· 失败 W`；Y = processed - (failed ?? 0)（终态口径）。
- explorer 轨：标签"攻击面探索"；进度条 `explored/total`；计数行：`总 X · 已探索 Y · 未探索 Z`。
- 展示降级：轨为 null → "探索轨未启用或未记录"（explorer）/ "规则进度未记录"（rules）；单字段 null → "—"；total 为 null 时隐藏进度条仅显计数。
- 进度条复用 `.progress-track`（styles.css:163-164），宽度 `min(processed/total, 1)` transform 缩放；explorer 运行中为近似值，组件不区分标注（终态 2s 内被精确值覆盖）。

### 3.4 口径说明（写进代码注释与验收；评审 R-3 修订）

- 运行中 `explorer.explored` = partial jsonl 行数，与终态 `entries_explored` 的偏差源**仅有一个**：worker 异常条目（scheduler 捕获为 FAILED）在终态聚合中计入 `entries_explored`（explorer.py:227 分支），但其 partial 行不落盘（append 仅在 worker 正常返回路径 explorer.py:193 执行）→ **运行中略小于终态**。软上限 `skipped_max_cap` 两侧均不记录（worker 于 explorer.py:187 提前 return、append 不执行；终态聚合 :208-209 continue）——无偏差。运行中近似值终态后 2s 内被 stage summary 精确值覆盖。
- `explorer.total` 运行中取 manifest 顶层 `explorer_total_count`，与终态 summary `entry_count` 同源（同一 `effective` 列表，orchestrator.py:1174/:1258），无偏差（评审 R-1：api_entry_table JSON 静态计数不可行，已弃用）。

### 3.5 测试设计

- `test_run_progress.py`（新，直接测 `build_run_progress`，tmp_path 构造 run_dir/manifest）：
  1. 全信号：manifest 带 rule_prescan + explorer summary（含 entries_explored/unexplored）+ rule-results 3 文件 → 全字段精确。
  2. 运行中：manifest 顶层 `rule_total_count`/`explorer_total_count`、无 stage summary、partial jsonl 5 行 → rules{total, processed=3, failed=null}；explorer{total=8, explored=5, unexplored=3}（评审 R-1：验证 manifest 顶层口径而非 api_entry_table）。
  3. 历史 run：无 summary、无 partial、observations.json 4 entries → explorer{explored=4}，total 为 null（无 explorer_total_count 早期写入的历史 run 不伪造）。
  4. 探索轨未启用：无 explorer stage、无 explorer 目录 → `explorer is None`。
  5. 畸形产物：observations.json 非法 JSON、partial jsonl 空行/坏行 → 对应字段 null，不抛异常。
  6. rule-results 目录不存在 → processed=0（total 有值时）；**混入产物词干**：rule-results 放置 `binder_bindings.json` 等三类文件（评审 R-2）→ 不计入 processed。
- `test_api.py` 端到端：现有 upload→get_run 流程断言 `progress["rules"]["total"] >= 1` 且 `processed` 随 run 完成等于 total；explorer 未启用时 `progress["explorer"] is None`。
- `test_rule_runner.py`（新，评审 R-5）：run_all 结束后成功与失败规则的 `rule-results/{rule_id}.json` 一一对应落盘且失败 status 归一；构造方式参考 `test_rule_index_protocol.py:103-176` 既有模式。既有 run_all 回归（串行/并行一致性 :103-104、重复执行 :134-140、失败归一 :176）在 `test_rule_index_protocol.py` 与 `test_manual_review_regressions.py:210` 全量保留运行。
- 前端：`npm run build`（tsc -b && vite build）零错误；浏览器实测见验收方案。

## 4. 风险与回退

- **规则执行核心路径改造**（run_all）：聚合顺序经索引排序严格保持，`_write_result` 原子写复用，全量 pytest 回归兜底；回退 = 单文件 `git checkout rule_runner.py`。
- **getRun 每 2s 读目录/文件**：rule-results 目录 glob（几十条目）+ partial jsonl 行数（每行 ~200B×数百）——I/O 量级可忽略；manifest 顶层总量键直接读内存 manifest，无额外 I/O（评审 R-1 修订后不再解析 api_entry_table）。
- **manifest 新增顶层 `rule_total_count`/`explorer_total_count`**：未知键对既有消费者（前端/报告）透明（经 `_public_run` 透传已核）；回退 = 删除 orchestrator 两行。
- **前端 types.ts 遗留字段替换（评审 R-4）**：`progress?: number` → `progress?: RunProgress | null`，已核全仓无消费方，替换零波及；回退时恢复与否均无影响（无既有消费方依赖 number 语义）。
- **前端切换状态丢失**（FindingsPanel 筛选态）：可接受的最小实现；如评审认为必须保留，备选方案为双面板常驻 + `hidden` 属性切换（成本：探索轨空态常驻渲染）。
- **历史 run 显示"未记录"**：多级降级链已覆盖 2026-08-27 前后全部产物组合；无数据时显示 null 而非 0，不伪造进度。

## 5. 可观测与交接

- `GET /api/runs/{id}` 新增 `progress` 块——前端为唯一消费方；OpenAPI 由 FastAPI dict 响应自动透传，无 schema 注册负担。
- manifest 顶层新增 `rule_total_count`（提前写入）——运行审计可直接读取，与终态 summary 数值一致。
- 无新增日志/指标；progress 计算的降级路径统一走 logger.warning（routes 兜底处）。
