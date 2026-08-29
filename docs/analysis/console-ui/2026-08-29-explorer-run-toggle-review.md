# 任务审查报告：explorer-run-toggle（方案审查）

> **任务编号**：explorer-run-toggle
> **审查日期**：2026-08-29
> **审查对象**：`docs/analysis/console-ui/2026-08-29-explorer-run-toggle-implementation-plan.md`、`docs/analysis/console-ui/2026-08-29-explorer-run-toggle-acceptance-plan.md`
> **审查模型**：glm-5.3-flash（独立子代理，只读审查）
> **状态**：第 1 轮（待处置 / 已闭合）

---

## 1. 审查结论摘要

- **总体结论：修订后可进入实施。** 方案核心设计（复刻 `source_analysis_enabled` 链路：Form 字段 → `build_run_config` 快照 → orchestrator run 级读取）与代码库事实完全吻合，范围划分克制（batch/其它配置项/探索参数覆盖均正确划出），无重复造轮子。但存在 1 个高严重度问题（`test_batch.py` 的 golden 精确相等断言必然失败，两份方案的文件变更清单均未列入该文件）与 2 个中严重度问题（验收 A-5 的"API 核对"通道不可行；pytest 场景下"`（True）`"预期不成立），须先修订。
- **审查方法**：两份方案 §2 全部 12 条现状锚点逐一读码回查（基于 HEAD `1094f30`，与方案声明一致）；关键声称（唯一门禁消费点、batch 兼容性、`_safe_config_snapshot` 行为、既有测试兼容性）独立 grep/读码复核；`pytest --collect-only` 实测基线 1388 用例（只收集未执行，符合只读约束）。

## 2. 锚点真实性核验

> 逐条核对方案 §2 现状锚点与关键声称的代码事实；不实锚点必须列入问题清单。

| 方案声称 | 代码事实 | 结论（属实/偏差/不实） |
|---|---|---|
| `create_run` 以 `source_analysis_enabled: bool = Form(default=True)`（:108）接收，经 `build_run_config`（:121）写入快照、`storage.ingest(..., config)`（:122）落 manifest、:139 后台启动 scan（routes.py:103-140） | `backend/app/api/routes.py:103-140` 全部吻合（:108/:121/:122/:139 逐行核对） | ✅ 属实 |
| `build_run_config` 已有 `source_analysis_enabled: bool = True` 与 `ai_enabled: bool \| None = None` 两种形态；source_analysis 段 `{**settings.source_analysis.model_dump(mode="json"), "enabled": ...}`（run_config.py:17-46，段写法 :40-43） | `backend/app/runs/run_config.py:13-44`；参数在 :16-18，source_analysis 段 dict 字面量 :39-42 | ✅ 属实（行号微偏 1-4 行，见 R-4） |
| batch 编排调用同一 `build_run_config`（batch.py:216），不传新参数 → None → 沿用全局 | `backend/app/assets/batch.py:216-221` 精确吻合（传 `source_analysis_enabled=True, ai_enabled=...降级, ai_skip_reason`） | ✅ 属实 |
| orchestrator `scan()` 从 DB run 行读 `run.get("config",{}).get("source_analysis",{}).get("enabled", settings...)`（:113-116） | `backend/app/analysis/orchestrator.py:112`（`run = self.repository.get_run(run_id)`）+ :114-116（source_enabled 读取）；读取模式声称属实 | ✅ 属实（行号微偏，见 R-4） |
| 探索门禁 `if self.settings.explorer.enabled:`（:231）为全后端唯一 run 时消费点 | grep `backend/app/` 全仓仅 `orchestrator.py:231` 一处命中 | ✅ 属实 |
| ExplorerSettings 模型（config.py:206-220+）、`Settings.explorer`（config.py:300） | `backend/app/config.py:206-230`（12 个字段）、:300 | ✅ 属实 |
| `config/default.yaml:178-179` `explorer.enabled: true`（add9ef0） | `config/default.yaml:178-179` 精确吻合；`git show add9ef0` 确认该提交将 yaml 默认置 true 并同步 test_config.py:96 | ✅ 属实 |
| `CreateRunForm.tsx:15` sourceAnalysis 默认 true；:99-114 `.switch-row`；:52-54 调 createRun | `frontend/src/features/runs/CreateRunForm.tsx:15`、:99-114、:52-55 均吻合 | ✅ 属实 |
| `api.ts:150-156` createRun 逐字段 append | `frontend/src/lib/api.ts:150-155`（createRun :150 起，`source_analysis_enabled` append :154） | ✅ 属实 |
| `types.ts:260-264` CreateRunInput | `frontend/src/lib/types.ts:260-264` 精确吻合 | ✅ 属实 |
| track-progress progress 块对探索轨未启用已降级（explorer null → "探索轨未启用或未记录"） | `frontend/src/features/runs/TrackProgress.tsx:72`；后端 `backend/app/runs/progress.py:104-157` explorer 键恒输出（None 兜底） | ✅ 属实（页签空态文案另有出处，见 R-5） |
| `_public_run` 对 manifest.config 只脱敏 ai 段（routes.py:44-46）；`_safe_config_snapshot` 只取 config["ai"] | `backend/app/api/routes.py:43-51`（_public_run）、:30-40（_safe_config_snapshot 仅取 ai 4 键） | ✅ 属实（行号微偏，见 R-4） |
| FastAPI `bool \| None = Form(default=None)`：缺省 → None，bool 自动解析，非法值 422 | 与既有 `source_analysis_enabled` 同构（routes.py:108；test_api.py 以 `"true"/"false"` 字符串传参）；FastAPI/Pydantic 标准行为 | ✅ 属实 |
| scan() 中 run 变量含完整 config | `backend/app/shared/repository.py:457-476` get_run → `_run_row`（:671-678）`json.loads(config_json)` 返回完整 config dict；orchestrator :112 加载一次 | ✅ 属实 |
| api_surface 关闭时探索轨 degraded 空跑（default.yaml:206-207 注释已记录） | `config/default.yaml:205-207` 注释 + `orchestrator.py:1284` `"degraded_entry_table": degraded` | ✅ 属实 |
| 基线 1388 passed（commit 1094f30 实测） | `pytest --collect-only` 实测 **1388 tests collected**（HEAD=1094f30） | ✅ 属实 |
| 既有 golden 固化文件"如存在则同步"（实施时确认） | **确定存在**：`backend/tests/test_batch.py:466-493` golden dict 精确相等断言，未在变更清单中 | ❌ 遗漏（见 R-1） |

## 3. 问题清单（按严重度排序）

**【R-1】【高】** `test_batch.py` 的 golden 精确相等断言必然失败，但两份方案的文件变更清单均未列入 `test_batch.py`。
证据：`backend/tests/test_batch.py:482` `assert build_run_config(settings, source_analysis_enabled=False) == golden`，其中 golden（:468-481）为只含 `analysis_platform_api / source_analysis / ai` 三键的精确 dict；实施方案 §3.3 模块 A 无条件返回新增 `"explorer"` 段 → dict 不相等 → 该用例必破。实施方案 §3.1 文件清单只列 `test_run_config.py`（如无则并入 test_api.py）与 `test_api.py`，无 test_batch.py；§3.5 仅以"grep 定位……如存在则同步期望（新增 explorer 段）；实施时确认"含糊带过，且 grep 结果（唯一命中即 test_batch.py）在方案期即可确定，无须留待实施。验收方案 A-1/A-2 均未覆盖该 golden 同步。
修订建议：实施方案 §3.1 文件变更清单增加一行 `backend/tests/test_batch.py | 修改 | golden dict 增加 explorer 段期望（:466-493 精确相等断言同步）`；§3.5 第一条改为确定性表述："既有 golden 位于 test_batch.py:466-493，golden dict 追加 `"explorer": {**settings.explorer.model_dump(mode="json"), "enabled": True}`（以该测试 settings 的实际 explorer 值为准）"；验收方案 A-1 验收方式中补充"同步 test_batch golden"。

**【R-2】【中】** 验收 A-5 的"API 核对"通道不可行：HTTP API 响应中 `config.explorer.enabled` 不可见。
证据：`backend/app/api/routes.py:30-40` `_safe_config_snapshot` 仅透出 `config["ai"]` 的 4 个键；:43-51 `_public_run` 对 `run.config` 与 `manifest.config` 均套用同一脱敏。实施方案 §4 也明确决策"_safe_config_snapshot 不受影响——它只取 config["ai"]"（即不扩展暴露）。因此 A-5"浏览器实测 + API 核对：……读各自 manifest → manifest config.explorer.enabled 与表单勾选一致"按字面无法执行——API 返回的 manifest.config 已被裁剪为 ai 段。
修订建议：A-5 验收方式改为"浏览器实测创建任务 + 直读落盘清单核对：读取 `<storage.data_root>/runs/<run_id>/manifest.json`（或 pytest 内 `storage.read_manifest`）中 `config.explorer.enabled` 与表单勾选一致；API 侧仅核对旁证（progress.explorer / stages 无 explorer 项）"。同时可在验收 §1 补一句说明"config 快照审计以落盘 manifest 为准（API 响应按既有脱敏设计只透 ai 段）"。

**【R-3】【中】** pytest 场景下"缺省 = True"的预期不成立：`Settings()` 直构不加载 default.yaml。
证据：`backend/app/config.py:307-319` `settings_customise_sources` 仅含 env/dotenv/init 三源，default.yaml 仅由 `get_settings()`（config.py:344-349）加载；既有 API 测试均以 `Settings(...)` 直构（`test_api.py:16-22` client_for、`test_api.py` 均未传 explorer → 模型默认 `enabled=False`，config.py:209）。因此实施方案 §3.5 case 3 的"`config.explorer.enabled == settings.explorer.enabled`（True）"与验收 N-2 的"config.explorer.enabled = 全局值（True）"在 pytest 上下文中括注的 True 是错的（实际为 False）；运行时服务走 get_settings 链路才为 True。照抄该预期写测试会得到失败用例。
修订建议：两处改为"`config.explorer.enabled is settings.explorer.enabled`（断言与测试所用 Settings 实例的值一致）"；若测试需要覆盖 True 缺省形态，显式构造 `Settings(..., explorer=ExplorerSettings(enabled=True))`，并在用例注释注明"运行时默认 True 由 default.yaml/get_settings 提供（add9ef0），Settings() 直构默认 False"。

**【R-4】【低】** 部分锚点行号微偏（均 ±1~4 行，不影响实施方向）。
证据：`run_config.py` 函数实际 :13-44（方案写 :17-46）、source_analysis 段实际 :39-42（方案写 :40-43）；orchestrator source_enabled 读取实际 :114-116、run 加载 :112（方案写 :113-116）；`_public_run` 实际 :43-51、`_safe_config_snapshot` 实际 :30-40（方案 §4 写 :44-46）。
修订建议：按上述实际行号校正；或保持现状但在 §2 开头的"全部锚点经读码核实"注中说明行号口径为"函数体范围近似"。属可接受不修项。

**【R-5】【低】** 验收 A-4/N-4 的降级展示文案归位不精确。
证据："探索轨未启用或未记录"出自双轨进度块 `frontend/src/features/runs/TrackProgress.tsx:72`；探索轨候选页签的空态文案是"探索轨未启用或无候选"（`frontend/src/features/runs/ExplorerQueuePanel.tsx:44`）。A-4 写"探索轨页签显示'探索轨未启用或未记录'"混用了两处出处。
修订建议：A-4/N-4 拆分为两个可判定观察点：①详情页双轨进度块 label="攻击面探索" 的 note 为"探索轨未启用或未记录"（TrackProgress.tsx:72）；②探索轨页签 EmptyState 标题为"探索轨未启用或无候选"（ExplorerQueuePanel.tsx:44）。

## 4. 认可项

1. **链路复刻正确且与代码事实同构**：Form 字段（routes.py:107-108 先例）→ `build_run_config` 快照（run_config.py:39-42 先例）→ `storage.ingest` 落 manifest（storage.py:85 `"config": config`）→ orchestrator 从 `run.config` 读取（orchestrator.py:114-116 先例）——四处先例锚点全部核实，方案无一处凭记忆书写的主链声称。
2. **三态语义复用既有先例**：`explorer_enabled: bool | None = None` 对齐 `ai_enabled`（run_config.py:17、:30）的"None 沿用 settings"形态，而非新造第三种覆盖语义；`scan()` 门禁改 run 级后与 `source_enabled` 同源同读取时机（:112 一次 get_run，`_run_row` 已 json 解析完整 config，repository.py:671-678），无额外 DB 访问。
3. **范围划分准确**：batch（batch.py:216-221 确实不传新参数 → 零行为变化）、探索参数任务级覆盖、api_surface 联动（orchestrator.py:1284 既有 degraded 路径）均正确划出且理由有代码依据。
4. **兼容性预判成立**：`test_api.py` 既有用例（client_for 直构 Settings，explorer 默认 False）改动后行为不变；`test_explorer.py:318-345`（A-10，Settings 显式 `ExplorerSettings(enabled=True)` + API 无 form 字段 → None → 沿用 True → 阶段照跑）天然回归；`_run_config()`（规则上下文，orchestrator.py:1657-1662）不含 explorer 段，不受影响。
5. **回退方案可行**：后端三文件 + 前端三文件 checkout 完全回滚；config 新增段对既有读取方（orchestrator 已知键提取、_public_run 脱敏、_run_row 的 source_analysis_enabled 提取）均为未知键忽略。
6. **基线数字可复现**：验收 §3 的 1388 基线与 `pytest --collect-only` 实测一致。

## 5. 边界检查表

| 边界 | 结论 |
|---|---|
| 兼容 | API 缺省沿用全局、老 run config 无 explorer 段回退全局、batch 不传参零变化——三项论证均与代码吻合；唯一确定性破坏点为 test_batch.py:482 golden（R-1，修订变更清单后消除）。前端三文件接线完整（state → submit → types → form.append），无遗漏消费点。 |
| 回滚 | git checkout 六文件可行；manifest config 新增段对旧代码透明成立（无 schema 严格校验拒绝 config 的读取方）。 |
| 异常 | 非法 form 值 422（与 source_analysis_enabled 同语义，routes.py:108 先例）；api_surface 全局关闭时探索轨 degraded 空跑有据（orchestrator.py:1284）；AI 缺失下探索阶段可完成（test_explorer.py:318-345 A-10 先例）。缺口：A-5 验证通道（R-2）。 |
| 回归 | 基线 1388 实测一致；R-1/R-3 未修正前全量 pytest 预计出现 ≥1 失败（test_batch golden）或用例编写返工；A-2② 端到端开启用例中探索阶段会真实执行（AI 不可用零候选、run completed），断言仅盯 config 与完成态即可，无阻塞。 |
| 数据质量 | 快照可审计链成立（storage.py:85 config 落盘 manifest）；但 API 响应按既有脱敏设计不透出 explorer 段，审计核对必须走落盘 manifest（R-2 需在验收中明示），否则"可审计"验收项会误判为失败。 |

---

## 6. 处置记录（主代理回填，2026-08-29）

> 主代理逐条独立复核：R-1 经读码证实（`test_batch.py:482` `assert build_run_config(settings, source_analysis_enabled=False) == golden`，golden 为三键精确 dict，新增 explorer 键必破）；R-3 经读码证实（`config.py:307-319` `settings_customise_sources` 仅 env/dotenv/init 三源，default.yaml 仅由 `get_settings()` 加载——`Settings()` 直构下 explorer.enabled 为模型默认 False）；R-2 与主代理此前对 `_safe_config_snapshot`（routes.py:26-40，仅取 ai 4 键）的读码一致；R-5 两处文案均为实现者本人所写（TrackProgress "未启用或未记录" / ExplorerQueuePanel "未启用或无候选"），归位批评成立。**五条全部采纳，两份文档已修订。**

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 高 | **采纳**：实施方案 §3.1 变更清单增加 `backend/tests/test_batch.py`（golden dict 同步 explorer 段期望）；§3.5 第一条改为确定性表述（golden 位置 :466-493、追加方式、以该测试 settings 实际 explorer 值为准）；验收 A-1 补"同步 test_batch golden" | 实施方案 §3.1/§3.5；验收 A-1 |
| R-2 | 中 | **采纳**：验收 A-5 验证通道改为"直读落盘 manifest（`storage.read_manifest` / `<data_root>/runs/<id>/manifest.json`）核对 config.explorer.enabled；API 响应仅作旁证（progress.explorer / stages）"；§1 补审计口径说明（API 按既有脱敏设计只透 ai 段） | 验收 A-5/§1 |
| R-3 | 中 | **采纳**：实施方案 §3.5 case 3 与验收 N-2 的缺省预期改为"`config.explorer.enabled is settings.explorer.enabled`（与测试所用 Settings 实例一致）"，并注明"运行时 True 由 default.yaml/get_settings 提供（add9ef0），pytest `Settings()` 直构默认 False" | 实施方案 §3.5；验收 N-2 |
| R-4 | 低 | **采纳**：校正关键行号（run_config.py :13-44、source_analysis 段 :39-42、orchestrator run 加载 :112、`_safe_config_snapshot` :30-40、`_public_run` :43-51） | 实施方案 §2/§4 |
| R-5 | 低 | **采纳**：验收 A-4/N-4 拆分为两个可判定观察点——①进度块 note"探索轨未启用或未记录"（TrackProgress.tsx:72）；②探索轨页签空态标题"探索轨未启用或无候选"（ExplorerQueuePanel.tsx:44） | 验收 A-4/N-4 |

**闭合结论**：R-1~R-5 全部采纳并完成两份方案文档修订（状态更新为"已闭合"）；无开放决策项；**方案可进入实施**。R-1 为实施前置条件（否则全量回归必红），R-3 防止测试用例按错误预期编写返工。
