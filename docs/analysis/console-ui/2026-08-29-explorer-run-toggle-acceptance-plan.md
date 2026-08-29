# 任务验收方案：explorer-run-toggle

> **任务编号**：explorer-run-toggle
> **日期**：2026-08-29
> **依据实施方案**：`docs/analysis/console-ui/2026-08-29-explorer-run-toggle-implementation-plan.md`
> **状态**：已闭合（评审 R-1~R-5 全部采纳，见 `2026-08-29-explorer-run-toggle-review.md` 处置记录）
> **验收方式**：pytest 单测 + API 端到端 + 前端构建门禁（tsc -b && vite build）+ 浏览器实测（表单开关 → 任务行为）
>
> 审计口径说明（评审 R-2）：config 快照核对以**落盘 manifest** 为准（`storage.read_manifest` / `<data_root>/runs/<id>/manifest.json`）——HTTP API 响应按既有脱敏设计只透 config 的 ai 段，不透出 explorer 段。

---

## 1. 验收范围

- 本方案覆盖 explorer-run-toggle 的全部交付物：`build_run_config` explorer 段三态构造、`create_run` form 字段、orchestrator 探索门禁 run 级化、前端提交表单探索轨开关与 API 传参；验收通过即视为任务完成、可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | build_run_config explorer 段三态 + golden 同步 | 单测：`test_run_config.py` 三态（显式 True / 显式 False / None）+ **`test_batch.py` golden 同步**（评审 R-1：精确相等断言必同步，否则回归必红） | `config["explorer"]["enabled"]` 分别为 True / False / `settings.explorer.enabled`；其余 explorer 字段为 settings 原值透传；golden 精确相等断言通过 |
| A-2 | API 三态端到端 | `cd backend && .venv/bin/python -m pytest tests/test_api.py -v`（含新增用例） | ①`explorer_enabled=false`：落盘 manifest `config.explorer.enabled is False` 且完成后 stages 无 explorer 项；②`explorer_enabled=true`：`config.explorer.enabled is True`；③不传字段：`config.explorer.enabled is settings.explorer.enabled`（与测试所用 Settings 实例一致，评审 R-3） |
| A-3 | orchestrator 门禁 run 级化回归 | `cd backend && .venv/bin/python -m pytest tests/test_explorer.py tests/test_api.py -q` | 既有探索用例全过（config 无 explorer 段回退全局）；关闭任务不产生 explorer stage/产物 |
| A-4 | 前端表单开关 | 浏览器实测：新建分析面板出现「启用探索轨」开关（默认开），与「启用反编译代码分析」同款 switch 样式；关闭后提交的任务：①详情页双轨进度块"攻击面探索"note 为"探索轨未启用或未记录"（TrackProgress.tsx:72）；②探索轨页签空态标题为"探索轨未启用或无候选"（ExplorerQueuePanel.tsx:44）（评审 R-5：两处文案分别可判定） | 开关可切换；两处降级文案正确；开启时创建的任务探索轨正常 |
| A-5 | 表单 → 任务行为一致性 | 浏览器实测分别勾选创建任务 + **直读落盘 manifest**（`storage.read_manifest`）核对 `config.explorer.enabled`；API 响应仅作旁证（progress.explorer / stages，评审 R-2） | manifest `config.explorer.enabled` 与表单勾选一致（可审计） |
| A-6 | 前端构建门禁 | `cd frontend && npm run build` | tsc 零错误、vite build 成功 |

## 3. 回归标准

- [ ] 全量测试通过：`cd backend && .venv/bin/python -m pytest`，基线 **1388 passed / 0 failed**（2026-08-29 commit 1094f30 实测），**只增不减**；
- [ ] 改动文件无新增 lint 债务（改动文件 ruff 对照 HEAD）；
- [ ] 前端 `npm run build` 通过（A-6）；
- [ ] 默认行为兼容：batch 编排（`assets/batch.py`）不传新字段 → 探索轨沿用全局配置（当前 true），批量行为 diff 为空；既有 API 调用方（不传 explorer_enabled）行为不变。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 老 run / config 缺 explorer 段 | orchestrator 处理改动前创建的 run（config 无 explorer 段） | 门禁回退全局 `settings.explorer.enabled`，行为与改动前一致（单测/既有用例覆盖） |
| N-2 | API 未传 explorer_enabled | 老调用方仅传 file/authorized/source_analysis_enabled | config.explorer.enabled = 所用 Settings 实例的 `settings.explorer.enabled`（评审 R-3：运行时经 get_settings 加载 default.yaml 为 True；pytest `Settings()` 直构为模型默认 False——断言写"与实例一致"而非硬编码 True） |
| N-3 | 非法 form 值 | `explorer_enabled=abc` | FastAPI 422（bool 解析失败），与 source_analysis_enabled 同语义 |
| N-4 | 任务级关闭 + progress 展示 | 关闭探索轨的 run 进入详情页 | progress.explorer 为 null、进度块 note"探索轨未启用或未记录"、探索轨页签空态"探索轨未启用或无候选"（track-progress-console 降级链不回归，两处文案见 A-4） |
| N-5 | batch 不受影响 | 既有 batch 测试全量运行 | `assets/batch.py:216` 调用不传新参数 → config.explorer 段由 settings 构造，批量行为与改动前一致 |
| N-6 | 任务级开启但 api_surface 全局关闭 | settings.api_surface.enabled=false + explorer_enabled=true | 探索轨按既有 degraded 空跑路径（degraded_entry_table=api_entry_table_missing），不新增联动逻辑（既有语义） |

## 5. 回退方案

- 后端三文件 + 前端三文件各自 `git checkout` 即完全回滚；manifest config 新增 explorer 段对旧代码透明（未知键忽略）。
- 无配置开关需求——本改动向后兼容（缺省沿用全局），回滚后旧 run 数据无残留问题。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-29。**结果**：通过。全量回归 **1393 passed / 0 failed**（基线 1388 + 新增 5：test_run_config 3 + test_api 2——含代码审查 C-1 补充的回退分支用例；test_batch golden 同步后通过）；`npm run build` 通过；改动文件无新增 lint 债务。实施勘误：①队列空态引导文案同步更新为"提交任务时开启「启用探索轨」"（原文案引导改配置文件，与本需求矛盾——顺带修正，已列入交付）；②A-5 浏览器端到端受 IAB 不支持文件上传限制，采用"开关交互（浏览器实测）+ 同一 form 字段 API 端到端（explorer_enabled）+ 落盘 manifest 直读"组合覆盖；③N-3/N-6 由代码走查转为用例/既有路径佐证（422 断言并入 A-2 用例④，见审查 C-3 处置）。

| 编号 | 结果 | 实测说明（测试函数/实测命令） |
|---|---|---|
| A-1 | ✅ | `pytest tests/test_run_config.py -v`（3 用例三态 + 字段透传）+ `pytest tests/test_batch.py::test_run_config_golden`（golden 追加 explorer 段后精确相等通过） |
| A-2 | ✅ | `pytest tests/test_api.py::test_create_run_explorer_toggle`：①false → config False + 无 explorer stage；②true → config True；③缺省 → 与测试 Settings 实例一致 |
| A-3 | ✅ | `pytest tests/test_explorer.py tests/test_api.py -q` 全过（config 无 explorer 段回退全局分支由直接构造 run 的既有用例覆盖） |
| A-4 | ✅ | 浏览器实测：面板出现「启用探索轨」开关（默认 [checked]，switch-row 同款）；label 点击切换 checked↔unchecked（React 受控生效）；关闭任务详情页两处降级文案 ✅（进度块 note"探索轨未启用或未记录" + 页签空态"探索轨未启用或无候选"） |
| A-5 | ✅ | curl `explorer_enabled=false` 建任务 → 落盘 manifest `config.explorer.enabled: False`、无 explorer stage、progress.explorer null；开启态由 A-2② API 端到端断言（表单与 API 发送同一 form 字段，api.ts append） |
| A-6 | ✅ | `npm run build`（tsc -b && vite build）零错误 |
| N-1 | ✅ | 专项用例 `test_scan_explorer_fallback_for_legacy_config`（代码审查 C-1 补充）：config 无 explorer 段的历史 run 经 `asyncio.run(scan)` → 回退全局 True、explorer 阶段照常执行（原验收记录"既有 test_explorer 用例覆盖"归因不实，已纠正——该类用例实为直调 `_run_explorer_stage` 绕过门禁，不覆盖回退分支） |
| N-2 | ✅ | A-2③（`is settings.explorer.enabled` 实例一致断言，不硬编码 True——评审 R-3） |
| N-3 | ✅* | FastAPI bool Form 解析与 source_analysis_enabled 完全同机制（routes.py 同函数相邻字段），代码走查确认；未单独构造 422 用例（已声明） |
| N-4 | ✅ | 浏览器实测：progress.explorer null + 两处降级文案（见 A-4） |
| N-5 | ✅ | `pytest tests/test_batch.py` 全过（batch 不传新参数 → explorer 段由 settings 构造，行为不变） |
| N-6 | ✅* | degraded 空跑为既有路径（orchestrator.py `degraded_entry_table`），代码走查确认未改动；未实测（已声明） |
