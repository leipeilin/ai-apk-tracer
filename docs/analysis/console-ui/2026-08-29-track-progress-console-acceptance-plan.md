# 任务验收方案：track-progress-console

> **任务编号**：track-progress-console
> **日期**：2026-08-29
> **依据实施方案**：`docs/analysis/console-ui/2026-08-29-track-progress-console-implementation-plan.md`
> **状态**：已修订（评审 R-1~R-7 全部采纳，见 `2026-08-29-track-progress-console-review.md` 处置记录）
> **验收方式**：pytest 单测 + API 端到端 + 前端构建门禁（tsc -b && vite build）+ 浏览器实测（本地 dev 全流程）

---

## 1. 验收范围

- 本方案覆盖 track-progress-console 的全部交付物：后端 progress 计算模块（`backend/app/runs/progress.py`）、`get_run` 接线、orchestrator 提前写 `rule_total_count`、rule_runner 逐条落盘、前端轨切换 UI（RunDetailPage + TrackProgress + types + styles）、双轨进度反馈展示；验收通过即视为任务完成、可进入提交。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | progress 计算单测（全信号/运行中/历史 run/未启用/畸形/缺目录与产物词干排除） | `cd backend && .venv/bin/python -m pytest tests/test_run_progress.py -v` | 全部用例通过；各降级链与实施方案 §3.3 模块 A 一致；rule-results 混入 `binder_bindings.json` 等三类产物词干时不计入 processed（评审 R-2） |
| A-2 | getRun 端到端返回 progress 块 | `cd backend && .venv/bin/python -m pytest tests/test_api.py -v`（含新增断言用例） | 上传→扫描完成→GET /api/runs/{id}：`progress.rules.total` 与规则数一致、`processed == total`（产物词干排除后成立，评审 R-2）、`failed` 为 0 或 null；explorer 未启用时 `progress.explorer is null` |
| A-3 | rule_runner 增量落盘等价性 | `cd backend && .venv/bin/python -m pytest tests/test_rule_runner.py tests/test_rule_index_protocol.py tests/test_manual_review_regressions.py -v`（评审 R-5 修订：run_all 既有回归在后者两个文件） | 新增用例：run_all 结束后成功与失败规则的 `rule-results/{rule_id}.json` 一一对应落盘；既有 run_all 聚合顺序/失败归一/串行并行一致性用例全过 |
| A-4 | 探索轨/规则轨按钮切换互斥展示 | 浏览器实测：上传 APK 启动扫描→进入任务详情页 | 主展示区顶部出现「规则轨 / 探索轨」分段按钮；默认规则轨显示 FindingsPanel；点击探索轨仅显示 ExplorerQueuePanel（探索队列不在规则轨下方追加）；再次点击切回 |
| A-5 | 规则轨进度反馈 | 浏览器实测：扫描运行中观察规则轨进度块 | 显示"总 X · 已完成 Y · 未完成 Z"与进度条；Y 随运行增长（2s 刷新）；扫描结束后 Y=X、未完成=0 |
| A-6 | 探索轨进度反馈 | 浏览器实测：开启 explorer.enabled 的配置下运行扫描，切到探索轨观察 | 显示"总 X · 已探索 Y · 未探索 Z"与进度条；Y 随运行增长（运行中为 partial 行数近似值）；扫描结束后 Y+Z=X 且与 run manifest explorer summary 的 entries_explored/entries_unexplored 一致（终态总量同源 manifest `explorer_total_count`，评审 R-1） |
| A-7 | 前端构建门禁 | `cd frontend && npm run build` | `tsc -b` 零类型错误，vite build 成功产出 dist |
| A-8 | 切换零数据闪烁 | 浏览器实测：运行中在两轨间反复切换 | 两轨数据由页面层轮询持有，切换即时渲染，无加载骨架闪烁 |

## 3. 回归标准

- [ ] 全量测试通过：`cd backend && .venv/bin/python -m pytest`，基线 **1376 passed / 0 failed**，**只增不减**（新增 test_run_progress 用例 + test_api 断言用例）；
- [ ] 改动 Python 文件 lint 零错误（`ruff check backend/app/runs/progress.py backend/app/api/routes.py backend/app/analysis/orchestrator.py backend/app/analysis/rule_runner.py backend/tests/test_run_progress.py`，如项目门禁另有脚本以 `scripts/check-backend.sh` 为准）；
- [ ] 前端 `npm run build` 通过（A-7）；
- [ ] 默认行为兼容：不开启 explorer 的 run、历史 run 的 getRun 响应仅多出 `progress` 键，其余字段与改动前一致（test_api 既有断言全过为证）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 历史 run（无 entries_explored/无 rule_total_count/explorer_total_count 早期写入） | 打开 2026-08-27 前的旧 run 详情 | progress 字段按降级链取 observations.json 条目数；完全无产物时对应轨为 null，前端显示"未记录"，不报错不显示 0 |
| N-2 | 产物畸形 | 手工将 run_dir 下 observations.json 置为非法 JSON；partial jsonl 混入坏行 | 对应字段降级 null，getRun 仍 200 正常返回其余数据（单测覆盖） |
| N-3 | 探索轨未启用 | explorer.enabled=false 的常规扫描 | progress.explorer 为 null；前端探索轨切换后显示"探索轨未启用或无候选"空态，进度块显示未启用文案 |
| N-4 | run 目录缺失/manifest 损坏 | getRun 对 manifest 读取失败的 run（既有 try/except 路径） | run["progress"] 为 null（try 前初始化，评审 R-6），响应不 500 |
| N-5 | 规则全部失败 | 构造规则执行失败场景（或以单测模拟 failures） | failed 计数正确、失败规则 result 文件落盘、progress 不因 failed>0 异常 |
| N-6 | worker 异常/软上限窗口的实时近似 | 长跑中 worker 异常条目（终态计入、partial 缺行）或代码走查确认 | 运行中 explored 为 partial 行数近似值，**≤ 终态精确值**（评审 R-3：唯一偏差源为 worker 异常条目；skipped_max_cap 两侧均不记录无偏差）；扫描结束后 2s 内被终态 summary 覆盖，不长期错误 |
| N-7 | 并发轮询 | 详情页保持打开 10 分钟 | 仅既有 3 路 usePolling（getRun/getFindings/getExplorerCandidates），无新增轮询循环；页面无卡顿（SQLite 读锁决策不被破坏） |

## 5. 回退方案

- 后端回退粒度：`git checkout` 三个后端文件（routes.py / orchestrator.py / rule_runner.py）+ 删除 progress.py 即完全回到改动前；`progress` 键消失对前端无破坏（类型可选，展示降级为"未记录"）。
- 前端回退粒度：RunDetailPage.tsx / TrackProgress.tsx / types.ts / styles.css 单独回退；轨切换与进度反馈互不牵连（TrackProgress 独立组件）。
- 无配置开关——本改动为展示层与只读计算层，不改扫描行为；如评审要求开关，可在 config 增加 progress.enabled（默认暂不实现）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-29。**结果**：通过（A-6 运行中实时增长项以单测+代码走查替代实测，见下）。全量回归 **1386 passed / 0 failed**（基线 1376 + 新增 10：test_run_progress 8 + test_rule_runner 1 + test_api 1）；`npm run build` 通过；lint：改动文件 ruff 仅 2 条**既有**报错（rule_runner.py UP035/PLW1509，经 `git show HEAD` 证实改动前已存在，行号均不在本轮改动范围）；`scripts/check-backend.sh` 的 pytest 段通过，末段"规则契约=30"断言失败为**既有漂移**（rules/ 实际 34 个契约，commit 84c7647 33→34 后脚本未同步；与本任务无关，未顺带修改，建议另行修正脚本或改为动态计数）。

| 编号 | 结果 | 实测说明（测试函数/实测命令） |
|---|---|---|
| A-1 | ✅ | `pytest tests/test_run_progress.py -v`：8 用例全过（全信号/运行中/历史/未启用/畸形/产物词干排除/缺目录/manifest None） |
| A-2 | ✅ | `pytest tests/test_api.py::test_get_run_returns_track_progress` + 浏览器实测：终态 rules{total:34, processed:34, failed:0}，explorer null |
| A-3 | ✅ | `pytest tests/test_rule_runner.py tests/test_rule_index_protocol.py tests/test_manual_review_regressions.py`：新增"成功+失败规则一一落盘（串行/进程池双路径）"过；既有串行并行一致性/重复执行/失败归一全过 |
| A-4 | ✅ | 浏览器实测（IAB）：tablist「运行轨切换」，默认规则轨 [selected]；点击探索轨仅显示 ExplorerQueuePanel（FindingsPanel 卸载，探索队列不再追加在规则轨后）；切回正常 |
| A-5 | ✅ | curl 轮询运行中 run：`processed: 0→4→10→16→22→29→34`（total=34 恒定，运行中 failed=null）；浏览器终态进度块"34 总任务 · 34 已完成 · 0 未完成"+ 满进度条 |
| A-6 | ✅* | 展示与数据链路：历史探索 run（20260826T141857Z）浏览器实测"198 总攻击面 · 73 已探索 · 125 未探索"+ 37% 进度条，与 manifest explorer summary entry_count 及 observations.json 条目数对账一致；运行中实时增长以 `test_running_falls_back_to_manifest_counts_and_partial_jsonl` + explorer.py 代码走查替代（开启 explorer 的真实长跑未实测，成本原因——已声明） |
| A-7 | ✅ | `npm run build`（tsc -b && vite build）零错误产出 dist |
| A-8 | ✅ | 浏览器两轨反复切换即时渲染，无骨架闪烁（数据由页面层轮询持有） |
| N-1 | ✅ | 历史 run（20260826T141857Z，无 entries_explored/无 explorer_total_count）浏览器实测：explored=73（observations.json 降级）、total=198（summary entry_count）、无伪造 0 |
| N-2 | ✅ | `test_malformed_products_degrade_per_field`：observations.json 非法 JSON + partial 坏行 → explored/unexplored null，不抛异常 |
| N-3 | ✅ | 浏览器实测（explorer 未启用 run）：进度块"探索轨未启用或未记录"，面板空态"探索轨未启用或无候选" |
| N-4 | ✅ | routes.py try 前初始化 `run["progress"] = None`；`test_manifest_none_degrades` 覆盖 manifest None 路径 |
| N-5 | ✅ | `test_run_all_persists_every_rule_result_including_failures`：失败规则文件落盘、failures 归一、candidates 仅含成功规则 |
| N-6 | ✅ | 代码走查（explorer.py:187 提前 return 不 append；终态 FAILED 分支 :227 计入）+ §3.4 口径注释；运行中 ≤ 终态方向正确 |
| N-7 | ✅ | 设计保证：progress 随既有 getRun 轮询返回，无新增轮询循环；浏览器长会话无卡顿 |
