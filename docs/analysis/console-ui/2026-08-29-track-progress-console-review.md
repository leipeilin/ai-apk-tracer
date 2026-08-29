# 任务审查报告：track-progress-console（方案审查）

> **任务编号**：track-progress-console
> **审查日期**：2026-08-29
> **审查对象**：`docs/analysis/console-ui/2026-08-29-track-progress-console-implementation-plan.md`、`docs/analysis/console-ui/2026-08-29-track-progress-console-acceptance-plan.md`
> **审查模型**：glm-5.3-flash（独立子代理，只读审查）
> **状态**：第 1 轮（待处置 / 已闭合）

---

## 1. 审查结论摘要

- **结论：暂不可进入实施，须先修订 R-1（关键）、R-2（高）后实施。** 方案整体结构完整、需求两项均覆盖、无范围私自扩大；但 `explorer.total` 的运行中降级数据源在真实产物上不可达（原始 api_entry_table 根本没有 `method_id` 字段，有效口径依赖 SQLite reader 解析），以及 `rules.processed` 的 glob 计数会把 rule-results 目录里的 3 类规则产物文件误计入（终态 `processed == total` 的验收断言必失败）。另有 §3.4 口径说明与代码事实相反（skipped_max_cap 并不会写入 partial jsonl，偏差方向说反）、前端类型与既有 `progress?: number` 字段冲突、回归文件指向不存在的 `test_rule_runner.py` 三处需修订。
- **审查方法**：方案 §2 全部锚点与 §3 关键声称逐条读码核验（routes.py / orchestrator.py / rule_runner.py / explorer.py / call_tree.py / api_surface.py / storage.py / 前端 5 文件）；验收基线 1376 经 `pytest --collect-only` 实测累计核对；as_completed 等价性、降级链、前端轮询兼容性逐一推演。

## 2. 锚点真实性核验

> 逐条核对方案 §2 现状锚点与关键声称的代码事实；不实锚点必须列入问题清单。

| 方案声称 | 代码事实 | 结论（属实/偏差/不实） |
|---|---|---|
| 主区堆叠展示：`RunDetailPage.tsx:90-95` FindingsPanel 后无条件追加 ExplorerQueuePanel | 实际 `:90-95`：`:93` FindingsPanel、`:94` `{explorerQueueState.data && <ExplorerQueuePanel/>}` | ✅ 属实 |
| 既有轮询结构 `:19-40` 三路 usePolling（活跃 2s），`:25-28` 注释记录 2026-08-15 R-4 锁优化决策 | `RunDetailPage.tsx:19-40` 三路轮询属实；注释 `:25-28` 原文吻合 | ✅ 属实 |
| get_run 响应构造 `routes.py:139-160`，`run_dir` 访问 `:153`、try/except 降级 `:158-159` | `:139-160` 属实；`run_dir` 访问实际 `:152-153`；except 实际 `:157-159` | ✅ 属实（行号 ±1） |
| explorer_candidates 端点先例 `routes.py:170-193`，降级空态 `:186-192` | `:170-193` 属实；try/except 实际 `:187-192` | ✅ 属实 |
| explorer stage summary `orchestrator.py:1257-1274` 含 entry_count/entries_explored/entries_unexplored，`_record_stage` 阶段结束才写 | `:1257-1274` 逐字段吻合；`_record_stage`（`:1635-1642`）确为终态 append | ✅ 属实 |
| `orchestrator.py:1174` 以 method_id 非空过滤 effective | `:1174` 原文吻合（但 method_id 经 `_entry_method_id` 解析，见 R-1） | ✅ 属实 |
| `_append_partial_record` 每入口完成即 append（`explorer.py:250-278`）；observations.json 收尾写盘后删 partial（`:237-244`） | 定义实际 `:247-272`、调用点 `:193`（worker 内）；删除实际 `:241-244`。实质成立 | ⚠️ 偏差（行号漂移） |
| 探索轨总量口径：进度模块可按 api_entry_table "dict 且 method_id 非空" 计数，与 `entry_count` 同口径 | **不成立**：原始 api_entry_table.json 条目无 `method_id` 键（`api_surface.py:40-261` 全部字段清单可查，binder 仅 `implementation_method_id` `:187`）；effective 的 method_id 由 `call_tree.py:99-114 _entry_method_id` 经 `resolve_component_lifecycle_methods(self._reader, ...)` 查 SQLite 索引解析 | ❌ 不实（R-1） |
| rule_prescan 终态汇总 `orchestrator.py:178-191`（summary 含 rule_total_count/rule_failures/candidate_count），运行中不可知 | `:181-191` `_record_stage`，`rule_total_count` `:189`，确在 run_all（`:167-178`）完成后 | ✅ 属实 |
| `rule_runner.py:113-131` 进程池 `list(executor.map)` 阻塞（`:119-125`）、串行 `:112`、post-loop `:127-131` 落盘 | 串行实际 `:104-105`；map 实际 `:114-125`；post-loop 写盘实际 `:127-129`。阻塞语义属实 | ⚠️ 偏差（行号漂移） |
| `_write_result` tmp + os.replace 原子写（`rule_runner.py:133-148`） | 实际 `:153-166`（`:156-158` tmp 命名、`:164` os.replace），实质成立 | ⚠️ 偏差（行号漂移） |
| "每条规则均落盘（成功与失败）→ 落盘文件数 = 已处理数" | **不成立**：`_export_rule_artifacts` 还向同一 `rule-results/` 写 `binder_bindings.json`/`receiver_registrations.json`/`webview_js_bridges.json`（`rule_runner.py:220`，键集 `:29`） | ❌ 不实（R-2） |
| tmp 文件名 `.RID.json.<uuid>.tmp` 不匹配 `*.json` glob | `:156-158` `.{name}.{uuid}.tmp` 后缀非 .json | ✅ 属实 |
| `_run_one` 将全部预期失败归一为 result dict 不抛异常（`:252-326`） | `:252-330`：超时/输出超限/非零退出/协议错误均 `_failure` 归一 | ✅ 属实 |
| as_completed 改造等价性（异常传播、`with` 退出等待语义不变） | `_run_one` 归一切实；`ProcessPoolExecutor.__exit__` shutdown(wait=True) 语义与 map 一致；差异仅异常浮出顺序（map=规则序 / as_completed=完成序），方案已限定为资源类异常 | ✅ 逻辑成立 |
| `storage.py:135` `update_manifest(run_id, **changes)` 任意顶层键合并 | `:135-142` 原文吻合 | ✅ 属实 |
| `styles.css:163-164` `.progress-track`；`:94-96` `.theme-switch button.active`；`Button.tsx` `.button button-secondary` | 三处均属实（`.progress-track`+内层 span transform `:163-164`） | ✅ 属实 |
| `ExplorerQueuePanel.tsx:38-47` 接受 null 并自带空态 | props `queue: ExplorerQueueResponse \| null`（`:35`），空态 `:38-47` | ✅ 属实 |
| package.json 仅 `tsc -b && vite build`，无前端测试框架 | `frontend/package.json` scripts 属实 | ✅ 属实 |
| 前端 types.ts 新增 progress 类型（"AnalysisRun 增加 progress?: RunProgress \| null"） | `types.ts:50` **已存在** `progress?: number`（遗留字段，全仓无组件消费）——同名重复声明将 tsc 报错 | ❌ 不实（R-4） |
| `test_rule_runner.py` 既有 run_all 回归（如无则以 `test_explorer.py` 为准） | backend/tests/ 无 test_rule_runner.py；run_all 回归实际在 `test_rule_index_protocol.py:103-176` 与 `test_manual_review_regressions.py:210`；test_explorer.py 不含 run_all | ❌ 不实（R-5） |
| §3.4：`_append_partial_record` 对 skipped_max_cap 也 append（explorer.py:209-217 worker 返回 None 后仍回调），运行中可能略大于终态 | **相反**：worker 在 `explorer.py:187` 提前 `return None, "skipped_max_cap", []`，`:193` append 不执行（`:181-182` 注释明确"不启动不记 observations"）；终态 continue 在 `:208-209`；`:227` 是 FAILED 分支 `entries_explored += 1`。真实偏差=worker 异常条目终态计入而 partial 缺行（运行中略小于终态） | ❌ 不实（R-3） |
| §3.4：explorer.total 运行中取 api_entry_table 口径与终态 entry_count "完全同源同过滤，无偏差" | 同 R-1，不成立 | ❌ 不实（R-1） |
| 验收基线 1376 passed | `pytest --collect-only -q` 实测累计 **1376** | ✅ 属实 |
| manifest 未知顶层键对既有消费者透明 | `_public_run`（`routes.py:39-47`）仅脱敏 config，其余键透传；`RunStage`/manifest 前端类型均带 `[key: string]: unknown` 或可选 | ✅ 属实 |

## 3. 问题清单（按严重度排序）

**【R-1】【关键】`explorer.total` 运行中降级数据源不可达：原始 api_entry_table.json 没有 `method_id` 字段，"同口径计数"无法实现**
证据：方案 §2 锚点"探索轨总量口径"（implementation-plan `:38`）、§3.3 模块 A explorer.total fallback（`:113`）、§3.4 第二条（`:164`）、§3.5 用例 2（`:170`）。代码事实：`backend/app/analysis/api_surface.py:40-261` 生成的条目字段为 entry_id/entry_method/kind/component_name/source/exported/...，binder 条目仅有 `implementation_method_id`（`:187`），全部条目均无 `method_id` 键；orchestrator `:1174` 过滤的 `method_id` 来自 `call_tree.py:99-114 _entry_method_id`——manifest/dynrcv 类需 `resolve_component_lifecycle_methods(self._reader, ...)` 查 `index/analysis.sqlite3`（SQLiteCodeIndexReader）。按方案实现的 progress.py 对真实产物计数恒为 0：运行中 explorer.total=0 而 explored 增长，需求②的探索轨运行反馈在唯一运行窗口失效；且 §3.5 用例 2 用"8 条（2 条 method_id 空）"造数会让单测通过而生产破坏（测试掩盖缺陷）。
修订建议：放弃 JSON 解析路线，改为与模块 C 对称的一行埋点——orchestrator 在 `effective` 计算后、`explore_all` 之前（`orchestrator.py:1183` 与 `:1197` 之间）`self.storage.update_manifest(run_id, explorer_total_count=len(effective))`（与终态 `entry_count` `:1258` 严格同源）；progress.py 的 explorer.total 降级链改为 `explorer summary.entry_count → manifest 顶层 explorer_total_count → null`；删除 api_entry_table 计数 fallback；§3.5 用例 2 改为验证 manifest `explorer_total_count`；§3.4 第二条与 §2 对应锚点同步改写（历史 run 终态有 `entry_count`，2026-08-27 前 run 的 total 仍可得出，无回归损失）。

**【R-2】【高】`rules.processed` glob 计数混入规则产物文件：终态 processed > total，A-2 验收断言必失败**
证据：方案 §3.2（`:81` "`processed` = rule-results 文件数"）、§3.3 模块 A（`:109`）、§2（`:43` "落盘文件数 = 已处理数（成功+失败）"）。代码事实：`rule_runner.py:220` `_export_rule_artifacts` 将 `binder_bindings.json`、`receiver_registrations.json`、`webview_js_bridges.json`（键集 `RULE_ARTIFACT_KEYS` `:29`）写入同一 `rule-results/` 目录（消费方 `api_surface.py:153`、`attack_surface.py:242` 依赖该路径，不可挪目录）。凡任一规则产出产物，终态 processed = 规则数 + ≤3，A-2 的 "processed == total"（acceptance-plan `:20`）必失败；方案自身 §3.5 与 A-2 自相矛盾。
修订建议：progress.py 计数排除产物词干——`from app.analysis.rule_runner import RULE_ARTIFACT_KEYS`，`processed = sum(1 for p in (run_dir/"rule-results").glob("*.json") if p.stem not in RULE_ARTIFACT_KEYS)`（不动产物落盘路径，零波及既有消费方）；同步修订 §2 `:43` 声称与 §3.5；可新增单测断言"rule-results 混入 binder_bindings.json 时不计入 processed"。

**【R-3】【中】§3.4 口径说明与代码事实相反，且按方案声明将写入代码注释与验收**
证据：方案 §3.4（`:163`）声称"_append_partial_record 对 skipped_max_cap 也 append（explorer.py:209-217 worker 返回 None 后仍回调）……运行中近似值可能略大于终态精确值"，并称终态 continue 在 `explorer.py:227-229`；§3.4 开头声明"写进代码注释与验收"。代码事实：worker 于 `explorer.py:187` 软上限提前 `return None, "skipped_max_cap", []`，`:193` 的 append 不执行（`:181-182` 注释"超限入口不启动不记 observations"）；终态对 None 的 continue 在 `:208-209`；`:227` 为 FAILED 分支 `entries_explored += 1`（worker 异常条目终态计入、partial 无行——因 append 仅在 worker 正常返回路径执行）。真实偏差方向为"运行中略小于终态"，且 N-6（acceptance `:44`）"可略大于终态值"描述同样反向。
修订建议：§3.4 改写为——skipped_max_cap 两侧均不记录、无偏差；唯一偏差源为 worker 异常条目（终态计入 entries_explored、partial 缺行，运行中略小）；N-6 预期改为"运行中 explored ≤ 终态值，终态后 2s 内被 summary 覆盖"；代码注释按改写后口径落。

**【R-4】【中】前端类型接线与既有 `progress?: number` 字段冲突，照方案实施将 tsc 报错**
证据：方案 §3.2（`:95` "AnalysisRun 增加 progress?: RunProgress \| null"）。代码事实：`frontend/src/lib/types.ts:50` 已声明 `progress?: number`（遗留字段；全仓 grep 确认无组件读取 run 级该字段，仅有上传进度本地 state 与 `CreateRunProgress` 同名无关使用）。同名重复声明在 TS interface 中为 duplicate identifier，`tsc -b` 门禁（A-7）直接失败。
修订建议：§3.1 types.ts 条目与 §3.2 改述为"将 `types.ts:50` 的 `progress?: number`（遗留无消费，已核）**替换**为 `progress?: RunProgress \| null`"；§4/回退方案同步注明回退时该字段恢复与否均无消费方影响。

**【R-5】【中】run_all 回归文件指向不存在的 `test_rule_runner.py`，兜底文件名也指错**
证据：方案 §3.1（`:61` "backend/tests/test_rule_runner.py（如无则 test_explorer.py 内既有规则回归为准）"）、§3.5（`:176`）、验收 A-3（acceptance `:21`）。代码事实：backend/tests/ 目录无 test_rule_runner.py；引用 run_all 的既有回归为 `test_rule_index_protocol.py`（`:103-104` 串行/并行一致性、`:134-140` 重复执行、`:176` 失败归一）与 `test_manual_review_regressions.py:210`；`test_explorer.py` 不含 run_all。A-3 照抄执行将对不存在文件报 collect 错误。
修订建议：§3.1 明确 `test_rule_runner.py` 为**新增**文件（变更类型改"新增"）；§3.5 与 A-3 的"既有回归"改指 `pytest tests/test_rule_index_protocol.py tests/test_manual_review_regressions.py`。

**【R-6】【低】manifest 读取失败路径下 `progress` 键缺失，与 N-4 字面预期不一致**
证据：方案模块 B（`:119-122`）仅在现有 try 块内写 `run["progress"]`；`routes.py:157-159` 既有 except 分支（捕获 AppError/OSError/ValueError，manifest 损坏时 `json.loads` 抛 JSONDecodeError⊂ValueError 命中）不设置 progress。N-4（acceptance `:42`）预期 "run['progress'] 为 null"——按现方案该场景键缺失而非 null，若测试严格断言将 KeyError。前端可选类型下行为等价（均渲染"未记录"）。
修订建议：模块 B 补一句"在既有 except 分支同步 `run['progress'] = None`（或在 try 前初始化）"；或 N-4 预期改为"progress 为 null 或键缺失，响应不 500"。

**【R-7】【低】多处锚点行号漂移（实质均成立，实施前宜校准防误读）**
证据：`_write_result` 实际 `rule_runner.py:153-166`（方案 `:41` 写 133-148）；串行路径实际 `:104-105`（方案写 `:112`）；executor.map 实际 `:114-125`（方案写 119-125）；`_append_partial_record` 实际 `:247-272`（方案 `:37` 写 250-278）；partial 删除实际 `:241-244`（方案写 237-244）；`routes.py` run_dir 访问实际 `:152-153`（方案写 153）。
修订建议：按本报告 §2 核验表校准方案 §2/§3 引用行号；不改语义。

## 4. 认可项

1. **progress 注入 `get_run` 响应而非新增轮询端点**：与 `RunDetailPage.tsx:25-28` 记录的 2026-08-15 R-4 SQLite 读锁优化决策一致，N-7 的"无新增轮询"验收可守住既有性能约束。
2. **as_completed 改造的等价性论证成立**：`_run_one`（`rule_runner.py:252-330`）确将全部预期失败归一为 result dict；`with ProcessPoolExecutor` 退出 shutdown(wait=True) 语义与 `list(map)` 一致；方案对"仅资源类异常可能抛出"的限定准确。索引排序还原原序保住聚合顺序。
3. **多级降级 + null 不伪造 0 的设计**：`processed`/`explored` 的 fallback 链（除 R-1/R-2 两处数据源问题外）逐级可落地；探索轨未启用按常态 None 处理（对齐 `explorer_candidates` 端点 `routes.py:186-192` 的保守哲学）。
4. **复用充分、无重复造轮子**：`update_manifest`（storage.py:135）、`_write_result` 原子写、`.progress-track` 样式、`ExplorerQueuePanel` null 空态、`Button` 基类全部复用既有实现；tmp 文件名与 `*.json` glob 不冲突的判断经核属实。
5. **范围划分克制**：out of scope（StageTimeline、AI 复核进度、软上限精确口径、前端测试基建）合理；FindingsPanel 筛选态重置的取舍已在 §3.3/§4 显式声明并给出备选方案。
6. **验收基线数字准确**：1376 经 `pytest --collect-only` 实测吻合；A-1~A-8/N-1~N-7 基本可判定（除 R-2/R-5 牵连项），负例覆盖畸形输入、降级、未启用、失败、软上限、并发轮询。

## 5. 边界检查表

| 边界 | 结论 |
|---|---|
| 兼容 | manifest 顶层新键（rule_total_count/explorer_total_count）经 `_public_run` 透传、既有消费者无感；默认不开 explorer 的 run 与历史 run 走降级链（N-1/N-3 覆盖）；getRun 响应仅多 progress 键。遗留风险：R-4 的 types.ts 替换须按"替换"而非"新增"实施。 |
| 回滚 | 后端三文件 checkout + 删 progress.py 可整体回退；前端按组件粒度回退可行。注意 types.ts 回退后 `progress?: number` 处置需在方案中写明（R-4）。 |
| 异常 | 畸形产物（N-2）、目录缺失、未启用（N-3）、规则全失败（N-5）均有判定标准；R-6 修复后 manifest 损坏路径与 N-4 字面一致。R-1/R-2 修复前，探索轨运行反馈与规则轨终态数字为错误值——属预算/降级边界内的实质性缺口。 |
| 回归 | 基线 1376 实测准确、只增不减可执行；A-3 指向文件须按 R-5 修正；建议补一条前端交互回归：切换到探索轨再切回后 FindingsPanel 复核/筛选仍可用（既有行为不变，方案 §3.3 已声明筛选态重置取舍，验收宜显式记录该现象）。 |
| 数据质量 | R-1/R-2 修复后双轨数字与终态 stage summary 可严格对账（A-2 "processed==total"、A-6 "Y+Z=X 且等于 summary" 断言方可落地）；§3.4 按 R-3 改写后写入代码注释的口径与机器口径一致，无漂移。 |

---

## 6. 处置记录（主代理回填，2026-08-29）

> 主代理逐条独立复核（不盲从）：R-1~R-5 经读码二次验证全部属实——api_entry_table.json 条目确无 `method_id` 键（仅 binder 有 `implementation_method_id`，`api_surface.py:187`；`call_tree.py:99-114` 经 `resolve_component_lifecycle_methods` 查 SQLite）；`_export_rule_artifacts` 确向 `rule-results/` 写 `RULE_ARTIFACT_KEYS` 三类产物（`rule_runner.py:29` 键集、`:220` 写入点）；worker 确在 `explorer.py:187` 提前 return、`:193` append 不执行；`types.ts:50` 确有遗留 `progress?: number`（主代理 grep 复核全仓无组件消费，仅样式类名 `.progress-track` 同名异义）；`test_rule_runner.py` 确不存在，run_all 回归确在 `test_rule_index_protocol.py:103-176` 与 `test_manual_review_regressions.py:210`。R-6/R-7 属实（逻辑推演与行号核对）。**七条全部采纳。**

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 关键 | **采纳**：放弃 api_entry_table JSON 计数路线；orchestrator 在 `explore_all` 前提前 `update_manifest(run_id, explorer_total_count=len(effective))`；progress 的 explorer.total 降级链改为 `explorer summary.entry_count → manifest 顶层 explorer_total_count → null` | 实施方案 §2 锚点改写、§3.2/§3.3 模块 A/C、§3.4、§3.5 用例 2；验收 N-6 同步 |
| R-2 | 高 | **采纳**：progress 计数排除 `RULE_ARTIFACT_KEYS` 词干（`from app.analysis.rule_runner import RULE_ARTIFACT_KEYS`，产物落盘路径不动零波及）；新增单测"混入 binder_bindings.json 不计入" | 实施方案 §2/§3.2/§3.3 模块 A、§3.5；验收 A-2/A-1 |
| R-3 | 中 | **采纳**：§3.4 口径改写——skipped_max_cap 两侧均不记录无偏差；唯一偏差源为 worker 异常条目（终态计入、partial 缺行，运行中略小于终态）；N-6 预期反向修正 | 实施方案 §3.4；验收 N-6 |
| R-4 | 中 | **采纳**：types.ts 按"替换"实施——删除 `types.ts:50` 遗留 `progress?: number`（已核全仓无消费方）改为 `progress?: RunProgress \| null`；回退方案注明该字段处置 | 实施方案 §3.1/§3.2/§4 |
| R-5 | 中 | **采纳**：`test_rule_runner.py` 明确为新增文件；既有 run_all 回归指向 `test_rule_index_protocol.py` + `test_manual_review_regressions.py` | 实施方案 §3.1/§3.5；验收 A-3 |
| R-6 | 低 | **采纳**：get_run 在 try 前初始化 `run["progress"] = None`，except 分支天然保 null | 实施方案 §3.3 模块 B；验收 N-4 |
| R-7 | 低 | **采纳**：按审查报告 §2 核验表校准 §2/§3 引用行号（`_write_result` :153-166、串行 :104-105、map :114-125、`_append_partial_record` :247-272、partial 删除 :241-244、routes run_dir :152-153） | 实施方案 §2/§3.3 |

**闭合结论**：R-1~R-7 全部采纳并已完成两份方案文档修订（状态更新为"已闭合"）；无开放决策项；**方案可进入实施**。实施顺序按修订后方案执行，R-1/R-2 为数据源正确性前提，验收 A-2/A-6 的对账断言以其修复为成立条件。
