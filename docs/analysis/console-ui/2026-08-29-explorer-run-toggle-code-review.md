# 代码审查报告：explorer-run-toggle（实现审查）

> **任务编号**：explorer-run-toggle
> **审查日期**：2026-08-29
> **审查对象**：本轮代码变更（`git diff` + 未跟踪新文件 `backend/tests/test_run_config.py` 等）+ 依据实施方案 `docs/analysis/console-ui/2026-08-29-explorer-run-toggle-implementation-plan.md` + 验收方案 `docs/analysis/console-ui/2026-08-29-explorer-run-toggle-acceptance-plan.md`
> **审查模型**：glm-5.3-flash（独立子代理，只读审查）
> **状态**：第 1 轮（待处置 / 已闭合）

---

## 1. 审查结论摘要

- **总体结论：实现忠实落地方案 §3 模块 A~E，R-1~R-5 落实质量良好，无关键/高严重度缺陷；存在 2 个中严重度补全项（N-1 老 run 回退分支零测试覆盖且验收记录归因不实、`docs/05-API参考.md` 未同步新 form 字段），建议处置后提交。**
- **审查方法**：方案 §3 各模块逐条与 `git diff` 对照（后端 3 文件 + 前端 3 文件 + 3 个测试文件逐一读码）；关键声称独立 grep 复核（explorer.enabled 全部 run 时消费点、build_run_config 全部调用方、manifest config 全部读取方）；复跑全量 pytest（exit 0，1392 collected）与改动文件 ruff；前端以 `tsc -p tsconfig.app.json --noEmit`（exit 0）只读核验类型（vite build 因只读约束未复跑）；另核验了方案评审 R-1/R-3/R-5 的落实证据链。

## 2. 方案落地对照

| 方案项（§条目/A-x） | 实现（文件:行号） | 结论 |
|---|---|---|
| §3.3-A `build_run_config` 新增 `explorer_enabled: bool \| None = None` + explorer 段（全量 dump + enabled 覆盖，与 source_analysis 段同构）+ 文档字符串 | `backend/app/runs/run_config.py:19`（签名）、`:28-30`（docstring 参数说明）、`:48-53`（explorer 段；`:52` 内联 `settings.explorer.enabled if explorer_enabled is None else explorer_enabled`，与方案的 `resolved` 写法等价） | ✅ |
| §3.3-B `create_run` 新增 `explorer_enabled: bool \| None = Form(default=None)` 并透传 | `backend/app/api/routes.py:109`（Form 字段）、`:113-115`（docstring 补充）、`:124-128`（透传 `build_run_config`） | ✅ |
| §3.3-C 探索门禁 run 级化（`source_enabled` 旁读取 + `:231` 门禁改局部变量，老 run 回退全局） | `backend/app/analysis/orchestrator.py:117-121`（读取 + 兜底注释）、`:236`（`if explorer_enabled:`；读取与门禁同在 `_run` 函数作用域——方案所称 "scan() 门禁" 的锚点行号本即指 `_run` 内 `source_enabled` 读取处，实现与锚点一致，非偏离） | ✅ |
| §3.3-D CreateRunForm「启用探索轨」开关（默认开、switch-row 同构、提交传参、头注释同步） | `frontend/src/features/runs/CreateRunForm.tsx:11-12`（头注释）、`:16-17`（state 默认 true + 注释）、`:56`（提交含 `explorerEnabled`）、`:108-112`（switch-row，位于 source_analysis 开关之后） | ✅ |
| §3.3-E `CreateRunInput.explorerEnabled` + `createRun` 追加 form 字段 | `frontend/src/lib/types.ts:264-265`、`frontend/src/lib/api.ts:155` | ✅ |
| 验收 A-1 `test_run_config.py` 三态单测 | `backend/tests/test_run_config.py:20-37`（3 用例：显式 True / 显式 False / None 沿用 settings + 字段透传断言） | ✅ |
| 评审 R-1：`test_batch.py` golden 精确相等断言同步 explorer 段 | `backend/tests/test_batch.py:481-486`（golden 追加 explorer 段，`enabled: settings.explorer.enabled` 以实例值固化——比评审建议的硬编码更稳健，全仓唯一精确相等断言即此处，无第二处遗漏） | ✅ |
| 评审 R-3：缺省断言"与 Settings 实例一致"而非硬编码 True | `backend/tests/test_api.py:156-160`（③ `is client.app.state.settings.explorer.enabled`，注释说明直构默认 False / 运行时 True 由 get_settings 提供）；`backend/tests/test_run_config.py:12-13`（同款注释） | ✅ |
| 评审 R-5：空态文案两处归位 | `frontend/src/features/runs/ExplorerQueuePanel.tsx:44`（标题"探索轨未启用或无候选"保持不变，仅 description 更新）；`frontend/src/features/runs/TrackProgress.tsx:72`（"探索轨未启用或未记录"未被触碰） | ✅ |
| 顺带修正（超出 §3.1 文件清单）：ExplorerQueuePanel 空态引导文案 | `frontend/src/features/runs/ExplorerQueuePanel.tsx:44` description "开启 explorer.enabled 后…" → "提交任务时开启「启用探索轨」后…"；已在验收方案 §6 实施勘误①声明且理由成立（原文案引导改配置文件，与本需求目标直接矛盾），仅文案字符串变更、无逻辑影响 | ✅（已声明的清单外顺带项） |

## 3. 正确性核验

> 抽查关键实现的正确性：边界条件、错误处理路径、并发/状态安全、与现有代码的交互。

- **三态解析语义**：`run_config.py:52` None → `settings.explorer.enabled`、显式值直接覆盖；与 `ai_enabled`（`run_config.py:34`）的"None 沿用 settings"先例同构。`explorer` 段其余键经 `model_dump(mode="json")` 输出（含 `call_tree` 嵌套模型、`custom_sink_taxonomy_path: Path | None` 均为 JSON 可序列化形态），落盘 manifest 无序列化风险；`max_candidates_per_run: null` 等原值如实透传（`test_run_config.py:24/30/37` 断言）。
- **orchestrator 老 run 回退分支（N-1）**：`orchestrator.py:119-121` `run.get("config", {}).get("explorer", {}).get("enabled", self.settings.explorer.enabled)`——config 无 explorer 段（改动前创建的历史 run）或 config 为空 dict 时回退全局，行为与改动前 `if self.settings.explorer.enabled:` 等价；`run["config"]` 由 `repository.py:674` `json.loads(config_json)` 解析为 dict（`create_run`/batch 均传 dict，`repository.py:406` 落库），`config_json` 无 NULL 路径。分支本身正确，但**无任何自动化测试执行该回退默认值**（见 C-1）。
- **门禁作用域与读取时机**：读取在 `_run`（`orchestrator.py:103` 起）内 `:119`，门禁在 `:236`，同一函数局部变量直接引用；`scan()`（`:85-89`）委托 `_run` 并收敛异常，无跨函数传递问题。与 `source_enabled`（`:114-116`）同源同读取时机（`:112` 一次 `get_run`），无额外 DB 访问。
- **唯一 run 时消费点核实**：grep 全 `backend/app`，`explorer.enabled` 的 run 时门禁仅 `orchestrator.py:236`（经新局部变量）；`_run_explorer_stage`（`:1124` 起）内部仍用 `self.settings.explorer` 的预算/并发等参数——任务级只覆盖 enabled 旗标，符合方案 §3.3-C 与 out-of-scope 声明。
- **batch 兼容**：`backend/app/assets/batch.py:216-221` 调用 `build_run_config` 未传 `explorer_enabled` → None → 沿用 settings，批量行为零变化；golden 同步后 `pytest tests/test_batch.py` 全过（本次复跑含）。
- **`_safe_config_snapshot` / `_public_run` 不受影响**：`routes.py:30-40`（仅取 config["ai"] 4 键）、`:43-51`（脱敏）本轮未改动，explorer 段不透出 HTTP API；新增测试经 `client.app.state.storage.read_manifest` 直读落盘 manifest 断言（`test_api.py:135/142/149/163`），与验收 R-2 审计口径一致。
- **manifest config 新增键对既有读取方透明**：全仓 config 消费点仅 `orchestrator.py:114/119/298`（`.get` 链带默认值）、`repository.py:677`（source_analysis 段）、`routes.py:33-35`（ai 段）；`_run_config()`（规则上下文，`orchestrator.py:1662-1668`）自 settings 构造、不含 explorer 段，不受影响。无任何严格键校验拒绝未知键。
- **FastAPI `bool | None = Form(default=None)`**：与相邻 `source_analysis_enabled`（`routes.py:108`）同机制；"字段缺省 → None → 沿用 settings"由新增测试③实证（缺省 POST 正常 202 且 config 值与实例一致，非 422）；非法值 422 无用例（见 C-3，验收已声明）。

## 4. 问题清单（按严重度排序）

**【C-1】【中】** N-1 老 run 回退分支零自动化测试覆盖，且验收记录 N-1 的覆盖归因不实。
证据：回退默认值分支位于 `backend/app/analysis/orchestrator.py:119-121`（`.get("explorer", {}).get("enabled", self.settings.explorer.enabled)` 第三参）。全 tests 目录 grep 核实：改动后经 API/batch 创建的 run 的 config 恒含 explorer 段（`build_run_config` 无条件写入，`run_config.py:50-53`），故既有探索用例均未命中回退分支——`test_explorer.py` 内无任何 `repository.create_run` 调用（`_instance_orchestrator` 仅建目录与 manifest、直调 `_run_explorer_stage`，绕过 `_run` 门禁）；A-10 `test_orchestrator_explorer_stage`（test_explorer.py:318-345）经 API 创建 run → config 含 explorer 段 → 走的是快照值而非回退默认值；`test_api.py:420` 的 `config={}` 用例 run 已 completed、不触发扫描。而验收方案 §6 记录 N-1 为 ✅ 并声称"直接构造 run 行（config 无 explorer 段）的既有 test_explorer 用例全过 → 回退全局分支生效"——该类用例不存在，记录归因与代码事实不符。
修订建议：二选一或并用——①补一条用例（放 `test_explorer.py` 或 `test_api.py` 均可）：`repository.create_run({..., "config": {"source_analysis": {...}}})` 构造无 explorer 段的 run 行 + `Settings(explorer=ExplorerSettings(enabled=True))` + `asyncio_run(ScanOrchestrator(...).scan(run_id))`（可仿 A-10 的最小 APK payload 与 source 关闭设置），断言 manifest 含 explorer 阶段（回退全局 True 生效）；②若不补测试，则将验收记录 N-1 改为"回退分支与 source_enabled（orchestrator.py:114-116）同构、代码走查确认"，消除不实归因（记录准确性问题同样需闭环）。

**【C-2】【中】** `docs/05-API参考.md` §3 "POST /api/runs" 的 Multipart 字段表未同步新增的 `explorer_enabled` 字段。
证据：`docs/05-API参考.md:40` 字段表仅列 `file`/`authorized`/`source_analysis_enabled` 三行；`routes.py:109` 新 form 字段已随本变更生效。实施方案 §3.1 文件变更清单与验收 A-1~A-6 均未包含文档同步项（方案评审 R-1~R-5 亦未覆盖）——API 契约文档与本变更脱节。
修订建议：在字段表追加一行 `| explorer_enabled | Boolean | 否 | 任务级探索轨开关（explorer-run-toggle）；缺省沿用服务端 explorer.enabled |`；可顺带在该节补一句审计口径说明："任务级配置以落盘 manifest 的 `config.explorer` 段为准（HTTP 响应按脱敏设计仅透 ai 段）"。

**【C-3】【低】** N-3（非法 form 值 → 422）无用例，验收记录自报为 "✅*（代码走查确认）"。
证据：`backend/tests/` 无 `explorer_enabled` 非法值用例；与既有 `source_analysis_enabled` 同机制且后者同样无用例，属既有实践的一致性缺口而非新缺陷。
修订建议：可并入 C-1 的用例补充时顺手加一条 `data={..., "explorer_enabled": "abc"}` → 断言 422，成本一行；不补亦可接受（已声明）。

**【C-4】【低】** `explorer.enabled: true` 的 yaml 变更提交归因不精确：注释与方案锚点写 "add9ef0"，实际 yaml 置值落在 b2d4d15。
证据：`git show add9ef0 --name-only` 仅含 `backend/tests/test_config.py`（同步 yaml 默认值断言）；`git show b2d4d15 -- config/default.yaml` 含 `-  enabled: false` → `+  enabled: true`（explorer 与 api_surface 两段）及"2026-08-29 用户决策"注释——即 add9ef0 提交信息声称的 default.yaml 改动实际在其父提交 b2d4d15 中。受影响处：`frontend/src/features/runs/CreateRunForm.tsx:16` 注释"（2026-08-29 用户决策 add9ef0：explorer.enabled: true）"、实施方案 §2 锚点"`config/default.yaml:178-179`（commit add9ef0…）"（方案评审文档 §2 同样复述了该归因）。默认值为 true 的事实本身两处一致，无行为影响。
修订建议：将 `CreateRunForm.tsx:16` 注释的提交号改为 b2d4d15（或表述为"b2d4d15 置值 / add9ef0 同步断言"）；方案文档锚点如需修订由主代理一并处理，不阻塞提交。

> 严重度定义：关键（验收落空/引入回归）/ 高（明显缺陷须修复）/ 中（不完整应补充）/ 低（建议性）。

## 5. 测试覆盖核验

| 覆盖点 | 测试（函数名） | 结论 |
|---|---|---|
| A-1 三态构造（显式 True / 显式 False / None 沿用 settings + 字段透传） | `test_explorer_enabled_explicit_true` / `test_explorer_enabled_explicit_false` / `test_explorer_enabled_none_follows_settings`（test_run_config.py:20-37） | ✅（无空断言/恒真断言；`is` 身份断言 + 透传字段值断言有效） |
| A-1 golden 精确相等同步（R-1） | `test_run_config_golden`（test_batch.py:466-493） | ✅（golden 含 explorer 段；降级分支与默认分支子字段断言不变） |
| A-2① 显式关闭 → config False + 无 explorer 阶段 | `test_create_run_explorer_toggle`（test_api.py:130-140） | ✅（落盘 manifest 直读，负存在断言有效） |
| A-2② 显式开启 → config True | 同上（test_api.py:142-147） | ✅（探索阶段完整行为由既有 test_explorer 覆盖，与方案口径一致） |
| A-2③ / N-2 缺省沿用 Settings 实例值（R-3） | 同上（test_api.py:149-163） | ✅（`is client.app.state.settings.explorer.enabled` 实例一致断言 + 注释说明直构默认 False 的语境） |
| A-3 orchestrator 门禁回归（config 含 explorer 段路径） | `test_orchestrator_explorer_stage`（test_explorer.py:318-345）等既有探索用例 | ✅ |
| N-1 老 run / config 无 explorer 段回退全局 | 无 | ❌（C-1：分支未被任何测试执行，验收记录归因不实） |
| N-3 非法 form 值 422 | 无 | ⚠️（C-3：验收已声明 ✅*，与 source_analysis_enabled 既有实践一致） |
| N-5 batch 不传参零变化 | `test_run_config_golden` + test_batch 全量 | ✅ |
| N-4 关闭任务的 progress/空态降级、N-6 api_surface 关闭 degraded 空跑 | 浏览器实测（A-4 记录）/ 代码走查（degraded 路径 `orchestrator.py` 既有逻辑未改动，本次核实成立） | ✅*（浏览器项超出本只读审查可复现范围，采信验收记录） |

## 6. 回归核验

- **全量测试**：审查者复跑 `pytest -q`（backend venv）exit 0、无失败；`pytest --collect-only` 实测 **1392 tests collected**，与验收记录"基线 1388 + 新增 4（test_run_config 3 + test_api 1）"吻合，只增不减。目标集复跑 `test_api.py + test_explorer.py + test_batch.py` 全过。
- **lint**：改动后端 6 文件 `ruff check` All checks passed，无新增 lint 债务；无 `# noqa` 等绕过。
- **前端**：审查者以 `npx tsc -p tsconfig.app.json --noEmit`（只读、无 emit）核验 exit 0；`vite build` 因只读约束未复跑，采信验收 A-6 记录。`CreateRunInput.explorerEnabled` 为必填字段，前端唯一调用方 `CreateRunForm.tsx:56` 已同步传参（grep 核实无其它调用方），无遗漏消费点。
- **默认行为兼容**：既有 API 调用方不传 `explorer_enabled` → 沿用 settings（运行时 get_settings 加载 default.yaml 为 true，与改动前全局门禁行为一致）；batch 不传参零变化（golden 固化 + 全量 batch 测试佐证）；报告/评估/规则上下文（`_run_config`）不读 explorer 段；`_safe_config_snapshot`/`_public_run` 脱敏面不变。manifest 新增 config 键对旧代码透明（无严格 schema 校验 config 的读取方）。
- **回退**：后端 3 文件 + 前端 3 文件 + 测试 3 文件 checkout 即回滚，方案 §4 论证成立。

---

## 7. 处置记录（主代理回填，2026-08-29）

> 主代理逐条独立复核：C-1 经 grep 证实（`test_explorer.py` 无 `repository.create_run`/`.scan(` 调用，全部直调 `_run_explorer_stage` 绕过 `_run` 门禁；A-10 经 API 创建 → config 恒含 explorer 段）——本代理验收记录 N-1 的覆盖归因**确属不实**，予以纠正；C-2 经读码证实（`docs/05-API参考.md:36-40` 字段表仅三行）；C-3/C-4 属实。**四条全部采纳，已完成修订并复验。**

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| C-1 | 中 | **采纳**（采建议①并纠正记录）：新增 `test_scan_explorer_fallback_for_legacy_config`（test_api.py）——`build_run_config` 后 `del config["explorer"]` 模拟历史 run + `storage.ingest`/`repository.create_run` 直构 + `asyncio.run(scan)`，断言回退全局 True → explorer 阶段照常执行；验收记录 N-1 归因同步更正 | `backend/tests/test_api.py`、验收 §6 N-1 |
| C-2 | 中 | **采纳**：`docs/05-API参考.md` §3 字段表追加 `explorer_enabled` 行 + 审计口径说明（"任务级配置以落盘 manifest config 快照为准，HTTP 响应按脱敏设计仅透 ai 段"） | `docs/05-API参考.md` |
| C-3 | 低 | **采纳**：`test_create_run_explorer_toggle` 追加 ④ `explorer_enabled="abc"` → 422 断言 | `backend/tests/test_api.py` |
| C-4 | 低 | **采纳**：CreateRunForm.tsx 注释改为"b2d4d15 置值、add9ef0 同步断言"；实施方案 §2 锚点提交号同步更正 | `frontend/src/features/runs/CreateRunForm.tsx`、实施方案 §2 |

**闭合结论**：C-1~C-4 全部采纳并修订完成；复验——全量 pytest **1393 passed / 0 failed**（基线 1388 + 新增 5：test_run_config 3 + test_api 2），`npm run build` 通过，无未处置的关键/高意见，无开放决策项。**达到提交门槛。**
